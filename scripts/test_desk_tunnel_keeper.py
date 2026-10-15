"""The desk keeps its tunnel to the node open.

Seen on the test box (docs/V1.md §8): the one ssh connection the desk opens at start
dropped in the middle of `make verify`, and every later row was red for a healthy
platform. The entrypoint now reopens a tunnel it finds closed. Here `ssh` is a stand-in
that listens on the tunnel's port; the listener is killed and must be back."""

from __future__ import annotations

import os
import shutil
import socket
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
ENTRYPOINT = ROOT / "desk" / "entrypoint.sh"

pytestmark = pytest.mark.skipif(sys.platform == "win32" or not shutil.which("bash"), reason="needs bash on Linux")


def port_open() -> bool:
    with socket.socket() as s:
        s.settimeout(0.5)
        return s.connect_ex(("127.0.0.1", 6443)) == 0


def wait_for(cond, seconds: float) -> bool:
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        if cond():
            return True
        time.sleep(0.1)
    return False


@pytest.fixture
def desk(tmp_path):
    if port_open():
        pytest.skip("something already listens on 127.0.0.1:6443")
    inst = tmp_path / "instance"
    (inst / "instance").mkdir(parents=True)
    (inst / "instance" / "config.yaml").write_text("nodes:\n  - ip: 192.0.2.10\n", encoding="utf-8")
    kubeconfig = tmp_path / "kubeconfig"; kubeconfig.write_text("apiVersion: v1\n", encoding="utf-8")
    key = tmp_path / "key"; key.write_text("not a key\n", encoding="utf-8")
    fake_bin = tmp_path / "bin"; fake_bin.mkdir()
    calls = tmp_path / "ssh-calls"
    listener = tmp_path / "listener.py"
    listener.write_text(textwrap.dedent("""
        import socket, time
        s = socket.socket(); s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.bind(("127.0.0.1", 6443)); s.listen(8)
        while True:
            c, _ = s.accept(); c.close()
    """), encoding="utf-8")
    ssh = fake_bin / "ssh"
    # the stand-in does what `ssh -fN -L` does: leaves a listener behind and returns
    ssh.write_text(textwrap.dedent(f"""\
        #!/usr/bin/env bash
        echo call >> "{calls}"
        nohup python3 "{listener}" >/dev/null 2>&1 &
        echo $! > "{tmp_path}/listener.pid"
        for _ in $(seq 50); do (exec 3<>/dev/tcp/127.0.0.1/6443) 2>/dev/null && {{ echo back >> "{tmp_path}/ssh-returned"; exit 0; }}; sleep 0.1; done
        exit 1
    """), encoding="utf-8")
    ssh.chmod(0o755)
    env = dict(os.environ, HOME=str(tmp_path / "home"), PATH=f"{fake_bin}{os.pathsep}{os.environ['PATH']}",
               ORBIT_DESK_INSTANCE=str(inst), KUBECONFIG=str(kubeconfig), ORBIT_SSH_KEY=str(key),
               ORBIT_DESK_TUNNEL_CHECK_S="1", GH_TOKEN="")
    (tmp_path / "home").mkdir()
    log = (tmp_path / "desk.log").open("w")
    proc = subprocess.Popen(["bash", "-x", str(ENTRYPOINT), "sleep", "30"], env=env, start_new_session=True,
                            stdout=log, stderr=subprocess.STDOUT)
    try:
        yield tmp_path, calls, proc
    finally:
        import signal
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        pid = tmp_path / "listener.pid"
        if pid.exists():
            try:
                os.kill(int(pid.read_text().strip()), signal.SIGKILL)
            except (ProcessLookupError, ValueError):
                pass


def test_a_tunnel_that_closes_is_opened_again(desk):
    tmp_path, calls, proc = desk
    assert wait_for(port_open, 10), "the tunnel never opened"
    # the stand-in ssh must have returned before its listener is killed: killed a moment
    # earlier, the first open itself fails (the desk exits 2), which is another test
    assert wait_for(lambda: (tmp_path / "ssh-returned").exists(), 10)
    assert calls.read_text().count("call") == 1
    os.kill(int((tmp_path / "listener.pid").read_text().strip()), 9)
    assert wait_for(lambda: not port_open(), 5), "the stand-in tunnel did not close"
    assert wait_for(port_open, 15), ("a closed tunnel was not opened again; the desk said: "
                                     + (tmp_path / "desk.log").read_text()[-1500:])
    assert calls.read_text().count("call") == 2


def test_a_tunnel_that_stays_open_is_left_alone(desk):
    _tmp_path, calls, _proc = desk
    assert wait_for(port_open, 10)
    time.sleep(4)
    assert calls.read_text().count("call") == 1


def _start_world(tmp_path, ssh_body: str):
    inst = tmp_path / "instance"
    (inst / "instance").mkdir(parents=True)
    (inst / "instance" / "config.yaml").write_text("nodes:\n  - ip: 192.0.2.10\n", encoding="utf-8")
    kubeconfig = tmp_path / "kubeconfig"; kubeconfig.write_text("apiVersion: v1\n", encoding="utf-8")
    key = tmp_path / "key"; key.write_text("not a key\n", encoding="utf-8")
    fake_bin = tmp_path / "bin"; fake_bin.mkdir()
    ssh = fake_bin / "ssh"
    ssh.write_text("#!/usr/bin/env bash\n" + ssh_body, encoding="utf-8")
    ssh.chmod(0o755)
    (tmp_path / "home").mkdir()
    return dict(os.environ, HOME=str(tmp_path / "home"), PATH=f"{fake_bin}{os.pathsep}{os.environ['PATH']}",
                ORBIT_DESK_INSTANCE=str(inst), KUBECONFIG=str(kubeconfig), ORBIT_SSH_KEY=str(key),
                ORBIT_DESK_TUNNEL_RETRY_S="0", ORBIT_DESK_TUNNEL_CHECK_S="60", GH_TOKEN="")


def test_a_tunnel_that_does_not_open_once_is_asked_again(tmp_path):
    """One connection the operator machine's link dropped ended the whole desk command before
    it began (docs/V1.md §8, 2026-10-08); the tunnel is asked for up to three times."""
    calls = tmp_path / "calls"
    env = _start_world(tmp_path, f"""echo call >> "{calls}"
[ "$(wc -l < "{calls}")" -ge 2 ] || {{ echo "ssh: connect to host 192.0.2.10 port 22: Connection refused" >&2; exit 255; }}
exit 0
""")
    # the keeper the entrypoint leaves behind holds the output open, so it goes to a file and
    # the command's own exit is what is waited for
    out = tmp_path / "out"
    with out.open("w") as fh:
        proc = subprocess.Popen(["bash", str(ENTRYPOINT), "echo", "the command ran"], env=env, stdout=fh,
                                stderr=subprocess.STDOUT, start_new_session=True)
    try:
        assert proc.wait(timeout=60) == 0, out.read_text()
        assert "the command ran" in out.read_text()
        assert len(calls.read_text().splitlines()) == 2
    finally:
        try:
            os.killpg(proc.pid, 9)
        except (ProcessLookupError, PermissionError):
            pass


def test_a_tunnel_that_never_opens_is_a_stop_after_three_tries_that_says_why(tmp_path):
    calls = tmp_path / "calls"
    env = _start_world(tmp_path, f"""echo call >> "{calls}"
echo "ssh: connect to host 192.0.2.10 port 22: Connection refused" >&2; exit 255
""")
    proc = subprocess.run(["bash", str(ENTRYPOINT), "echo", "the command ran"], env=env, capture_output=True, text=True, timeout=60)
    assert proc.returncode == 2
    assert "the command ran" not in proc.stdout
    assert "did not open (three tries)" in proc.stderr and "Connection refused" in proc.stderr
    assert len(calls.read_text().splitlines()) == 3
