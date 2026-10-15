"""The same question, asked where the program actually runs.

The watch beside this one captures what a command writes and splits it into what the writer
produced and what something else did. It found two leaks and the repairs are in. Then a
review found that one of those repairs holds only under pytest: it moved a warning from
printing to logging, and pytest's logging plugin installs a root handler that swallows it.
The desk's own command line configures no logging at all, so the standard library's last
resort writes the same sentence to the error stream in a real run. The case saw a repair
that does not exist outside the test process.

That is this node's recurring failure in its newest form -- the harness was the thing being
measured. So these rows run the real entry point in a child process with no pytest in it,
carrying the same watch: the child patches the writer to record where its output began and
ended, and everything outside those windows is a second way out.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

# The child: install the watch, run the real entry point, report what leaked.
CHILD = r'''
import io, json, os, sys
import orbitd.client as desk

windows = []
real = desk.write_outcome


def watched(outcome, *, machine):
    sys.stdout.flush(); sys.stderr.flush()
    windows.append(["open", out.tell(), err.tell()])
    try:
        return real(outcome, machine=machine)
    finally:
        sys.stdout.flush(); sys.stderr.flush()
        windows.append(["shut", out.tell(), err.tell()])


out = open(sys.argv[1], "w+", buffering=1)
err = open(sys.argv[2], "w+", buffering=1)
saved_out, saved_err = os.dup(1), os.dup(2)
os.dup2(out.fileno(), 1)
os.dup2(err.fileno(), 2)
desk.write_outcome = watched
code = 99
try:
    code = desk.main(json.loads(sys.argv[3]))
except SystemExit as e:
    code = e.code if isinstance(e.code, int) else 1
finally:
    sys.stdout.flush(); sys.stderr.flush()
    out.flush(); err.flush()
    os.dup2(saved_out, 1)
    os.dup2(saved_err, 2)
    os.close(saved_out)
    os.close(saved_err)
    print(json.dumps({"code": code, "windows": windows}))
'''


def run(argv, tmp_path):
    """Run the desk for real and return what it wrote, split by the writer's windows."""
    out_path, err_path = tmp_path / "out", tmp_path / "err"
    env = dict(os.environ)
    env["XDG_CONFIG_HOME"] = str(tmp_path / "xdg")
    env["NO_PROXY"] = "127.0.0.1,localhost"
    done = subprocess.run(
        [sys.executable, "-c", CHILD, str(out_path), str(err_path), json.dumps(argv)],
        capture_output=True, text=True, timeout=120, env=env,
        cwd=str(Path(__file__).resolve().parents[1]))
    assert done.returncode == 0, (
        "the child did not run, so this case is measuring nothing: " + done.stderr[:500])
    report = json.loads(done.stdout.strip().splitlines()[-1])
    written = {"out": out_path.read_text(), "err": err_path.read_text()}
    inside = []
    for opened, shut in zip(report["windows"][::2], report["windows"][1::2]):
        inside.append(written["out"][opened[1]:shut[1]])
        inside.append(written["err"][opened[2]:shut[2]])
    leaked = written["out"] + written["err"]
    for piece in inside:
        if piece:
            leaked = leaked.replace(piece, "", 1)
    return report["code"], "".join(inside), leaked


# Every command, and a failure each one really has outside a test process.
ROWS = (
    (["call"], "a usage error"),
    (["tools", "--url", "http://127.0.0.1:9/mcp"], "no session for that surface"),
    (["call", "orbit_project_list", "--url", "http://127.0.0.1:9/mcp"],
     "no session for that surface"),
    (["login", "--url", "http://127.0.0.1:9/mcp"], "a surface that is not there"),
)


@pytest.mark.parametrize("argv,what", ROWS, ids=[w + " " + a[0] for a, w in ROWS])
class TestNothingLeavesTheRealDeskExceptThroughTheWriter:
    def test_nothing_leaked(self, tmp_path, argv, what):
        code, inside, leaked = run(argv, tmp_path)
        assert code != 0, f"{what} reported success"
        assert not leaked.strip(), (
            f"{what} put text in front of the operator that the one writer did not "
            f"produce, in a real run: {leaked[:400]!r}")

    def test_the_writer_wrote_something(self, tmp_path, argv, what):
        """Anti-vacuity: a command that wrote nothing would satisfy the case above."""
        _, inside, _ = run(argv, tmp_path)
        assert inside.strip(), f"{what} left the operator with nothing at all"


@pytest.mark.parametrize("argv,what", ROWS, ids=[w + " " + a[0] for a, w in ROWS])
def test_asking_for_machine_output_gets_a_document(tmp_path, argv, what):
    """The writer's rule is that the form follows what the caller asked for, never which
    failure happened. A usage error is a failure like any other."""
    _, inside, leaked = run(argv + ["--json"], tmp_path)
    everything = (inside + leaked).strip()
    assert everything, f"{what} wrote nothing at all"
    json.loads(everything.splitlines()[-1])


# The repair that held only under pytest. Moving a warning from printing to logging removes
# it from the operator's stream only if something is configured to take it. pytest's logging
# plugin installs a root handler; the desk's command line installs nothing, so the standard
# library's last resort writes it to the error stream anyway. This asks the real process what
# happens to a warning, rather than asking the source where the call went.
WARNS = r'''
import logging, sys
import orbitd.client as desk
try:
    desk.main(["tools", "--url", "http://127.0.0.1:9/mcp"])
except SystemExit:
    pass
logging.getLogger("orbitd.client").warning("A WARNING THE OPERATOR MUST NOT SEE")
sys.stderr.flush()
'''


def test_a_warning_does_not_reach_the_operator_in_a_real_run(tmp_path):
    env = dict(os.environ)
    env["XDG_CONFIG_HOME"] = str(tmp_path / "xdg")
    env["NO_PROXY"] = "127.0.0.1,localhost"
    done = subprocess.run([sys.executable, "-c", WARNS], capture_output=True, text=True,
                          timeout=120, env=env,
                          cwd=str(Path(__file__).resolve().parents[1]))
    assert "A WARNING THE OPERATOR MUST NOT SEE" not in done.stderr, (
        "the desk configures nothing to take its own warnings, so the standard library's "
        "last resort writes them straight at the operator -- which is what moving a "
        "message from printing to logging was supposed to stop:\n" + done.stderr[:400])


def test_the_child_really_runs_the_desk(tmp_path):
    """Anti-vacuity for the case above: a child that died before logging would pass it."""
    env = dict(os.environ)
    env["XDG_CONFIG_HOME"] = str(tmp_path / "xdg")
    env["NO_PROXY"] = "127.0.0.1,localhost"
    done = subprocess.run([sys.executable, "-c", WARNS], capture_output=True, text=True,
                          timeout=120, env=env,
                          cwd=str(Path(__file__).resolve().parents[1]))
    assert done.returncode == 0, done.stderr[:400]
    assert "not logged in" in (done.stdout + done.stderr), (
        "the child never reached the desk at all: " + (done.stdout + done.stderr)[:300])
