"""`orbit onboard run` (docs/V1.md M3): the form is read once and never echoed; the
config is filled where it still holds the template's example values, comments kept; the
credentials directory is generated without rotating anything that exists."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import urllib.error
from pathlib import Path

import pytest
import yaml

from orbitd import onboard_run
from orbitd.onboard_run import FormError, fill_config, load_form, make_credentials

REPO = Path(__file__).resolve().parents[3]
TOKEN = "SECRET-TOKEN-0123456789abcdef"
FORM = {
    "domain": "acme.example",
    "github": {"owner": "acme-org"},
    "hetzner": {"token": "HCLOUD-" + TOKEN, "server_type": "cx43", "location": "fsn1"},
    "object_storage": {"access_key": "S3KEY-" + TOKEN, "secret_key": "S3SECRET-" + TOKEN, "location": "nbg1"},
    "cloudflare": {"token": "CF-" + TOKEN, "zone": "acme.example"},
    "telegram": {"bot_token": "123456:" + TOKEN, "chat_id": "987654321"},
    "acme_email": "ops@acme.example",
    # the whole platform: every optional feature on (a form that names none is core alone,
    # tests/test_onboard_features.py)
    "features": {name: True for name in ("logs", "traces", "dashboards", "uptime", "databases", "backups", "alerts-telegram")},
}


def template_instance(inst: Path) -> Path:
    """The least of core's template a run reads before its first step: the example config
    and the features registry."""
    (inst / "instance").mkdir(parents=True)
    (inst / "provisioning").mkdir()
    shutil.copy(REPO / "template" / "instance" / "config.yaml.example", inst / "instance" / "config.yaml")
    shutil.copy(REPO / "template" / "provisioning" / "features.yaml", inst / "provisioning" / "features.yaml")
    return inst


def write_form(tmp_path: Path, doc: dict) -> Path:
    p = tmp_path / "form.yaml"
    p.write_text(yaml.safe_dump(doc))
    p.chmod(0o600)
    return p


def test_a_complete_form_loads_and_its_repr_carries_no_token(tmp_path):
    form = load_form(write_form(tmp_path, FORM))
    assert (form.name, form.domain, form.s3_endpoint) == ("acme", "acme.example", "https://nbg1.your-objectstorage.com")
    assert TOKEN not in repr(form)


@pytest.mark.parametrize("path,value,named", [
    (("hetzner", "token"), None, "hetzner.token"),
    (("telegram", "chat_id"), "not-a-chat " + TOKEN, "telegram.chat_id"),
    (("domain",), "acme.other", "inside `cloudflare.zone`"),
])
def test_a_bad_field_is_named_and_its_value_never_echoed(tmp_path, path, value, named):
    doc = yaml.safe_load(yaml.safe_dump(FORM))
    cur = doc
    for p in path[:-1]:
        cur = cur[p]
    if value is None:
        cur.pop(path[-1])
    else:
        cur[path[-1]] = value
    with pytest.raises(FormError) as e:
        load_form(write_form(tmp_path, doc))
    assert named in str(e.value)
    assert TOKEN not in str(e.value)


@pytest.mark.skipif(os.name == "nt", reason="POSIX modes")
@pytest.mark.skipif(os.name == "nt", reason="POSIX modes")
def test_a_form_others_can_read_is_made_private_and_refused_when_it_cannot_be(tmp_path, monkeypatch):
    """The form holds every token. On a credentials directory mounted from a Windows machine
    it reads as 0777 until narrowed from inside the desk, and a chmod on the Windows side
    changes nothing: the run narrows it itself (docs/V1.md §8). A file that stays readable
    by others (a mount that ignores the chmod, someone else's file) is refused as before."""
    p = write_form(tmp_path, FORM)
    p.chmod(0o644)
    assert load_form(p).name == "acme"
    assert p.stat().st_mode & 0o077 == 0
    p.chmod(0o644)
    monkeypatch.setattr(onboard_run.os, "chmod", lambda *a, **k: None)          # a mount that ignores it
    with pytest.raises(FormError, match="readable by others .* could not be made private"):
        load_form(p)

    def refused(*a, **k):
        raise PermissionError("not yours")
    monkeypatch.setattr(onboard_run.os, "chmod", refused)
    with pytest.raises(FormError, match="readable by others"):
        load_form(p)


@pytest.mark.skipif(os.name == "nt", reason="POSIX modes")
def test_make_private_narrows_only_what_others_can_reach(tmp_path):
    d = tmp_path / "creds"; d.mkdir(mode=0o777); d.chmod(0o777)
    f = d / "github-app.json"; f.write_text("{}"); f.chmod(0o666)
    assert onboard_run.make_private(d, 0o700) is True and d.stat().st_mode & 0o777 == 0o700
    assert onboard_run.make_private(f) is True and f.stat().st_mode & 0o777 == 0o600
    assert onboard_run.make_private(f) is False                                  # already private: left alone
    assert onboard_run.make_private(d / "absent") is False


def test_the_config_is_filled_where_it_holds_example_values_and_keeps_its_comments(tmp_path):
    (tmp_path / "instance").mkdir()
    example = (REPO / "template" / "instance" / "config.yaml.example").read_text()
    cfg = tmp_path / "instance" / "config.yaml"
    cfg.write_text(example)
    form = load_form(write_form(tmp_path, FORM))
    written = fill_config(tmp_path, form)
    doc = yaml.safe_load(cfg.read_text())
    assert doc["instance"]["name"] == "acme"
    assert doc["git"]["repoUrl"] == "git@github.com:acme-org/acme-platform.git"
    assert doc["domains"] == {"primary": "acme.example", "dev": "dev.acme.example", "dnsZones": ["acme.example"]}
    assert doc["objectStorage"]["endpoint"] == "https://nbg1.your-objectstorage.com"
    assert doc["objectStorage"]["buckets"] == {"platform": "acme-orbit-platform", "appData": "acme-orbit-apps"}
    assert doc["contacts"]["telegramChatId"] == "987654321"
    assert "# short id; prefixes global names" in cfg.read_text()
    assert TOKEN not in cfg.read_text()
    assert "instance.name" in written
    # a re-run writes nothing: no example value is left to replace, and a hand edit stays
    cfg.write_text(cfg.read_text().replace("acme-orbit-apps", "hand-picked-bucket"))
    assert fill_config(tmp_path, form) == []
    assert "hand-picked-bucket" in cfg.read_text()


@pytest.mark.skipif(not (shutil.which("ssh-keygen") and shutil.which("age-keygen")), reason="needs ssh-keygen and age-keygen")
def test_credentials_are_generated_once_and_private(tmp_path):
    form = load_form(write_form(tmp_path, FORM))
    d = tmp_path / "creds"
    created = make_credentials(d, form)
    assert set(created) == {"node-key", "node-key.pub", "ssh-key-path", "age.key", "env"}
    env = (d / "env").read_text()
    assert "export HCLOUD_TOKEN=HCLOUD-" in env and "export CLOUDFLARE_API_TOKEN=CF-" in env
    if os.name != "nt":
        for f in ("node-key", "age.key", "env"):
            assert (d / f).stat().st_mode & 0o077 == 0, f
    age_before = (d / "age.key").read_text()
    assert make_credentials(d, form) == []
    assert (d / "age.key").read_text() == age_before
    assert onboard_run.age_recipient(d).startswith("age1")


def test_the_key_pointer_names_the_operator_machines_path_when_the_desk_says_where(tmp_path, monkeypatch):
    """Inside the desk the credentials directory is a mount. The pointer is read on the
    operator machine, so it must name the key there (docs/V1.md §8)."""
    form = load_form(write_form(tmp_path, FORM))
    monkeypatch.setenv("ORBIT_HOST_CONFIG_DIR", "/c/Users/someone/.config/acme-platform")
    d = tmp_path / "creds"
    make_credentials(d, form)
    assert (d / "ssh-key-path").read_text() == "/c/Users/someone/.config/acme-platform/node-key\n"
    monkeypatch.delenv("ORBIT_HOST_CONFIG_DIR")
    e = tmp_path / "creds-native"
    make_credentials(e, form)
    assert (e / "ssh-key-path").read_text() == str(e / "node-key") + "\n"


@pytest.mark.skipif(os.name == "nt", reason="POSIX modes")
def test_a_key_others_can_read_is_used_from_a_private_copy(tmp_path, monkeypatch):
    """A directory mounted from a Windows machine shows the key as 0777 and ssh refuses it:
    the rebuilt server would never have accepted the node key (docs/V1.md §8)."""
    monkeypatch.delenv("ORBIT_SSH_KEY", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    creds = tmp_path / "creds"; creds.mkdir()
    key = creds / "node-key"
    key.write_text("PRIVATE\n", encoding="utf-8")
    key.chmod(0o777)
    used = Path(onboard_run.private_key(creds))
    assert used == tmp_path / "home" / ".ssh" / "node-key"
    assert used.read_text() == "PRIVATE\n" and used.stat().st_mode & 0o077 == 0
    key.chmod(0o600)
    assert onboard_run.private_key(creds) == str(key)
    monkeypatch.setenv("ORBIT_SSH_KEY", "/somewhere/else")
    assert onboard_run.private_key(creds) == "/somewhere/else"


def _tunnel_world(tmp_path, server="https://127.0.0.1:6443"):
    inst = tmp_path / "inst"
    (inst / "instance").mkdir(parents=True)
    (inst / "instance" / "config.yaml").write_text("nodes:\n  - ip: 192.0.2.7\n", encoding="utf-8")
    kubeconfig = tmp_path / "kubeconfig"
    kubeconfig.write_text(f"clusters:\n- cluster:\n    server: {server}\n", encoding="utf-8")
    creds = tmp_path / "creds"; creds.mkdir()
    (creds / "node-key").write_text("key\n", encoding="utf-8")
    (creds / "node-key").chmod(0o600)
    return inst, creds, kubeconfig


def test_the_run_opens_the_api_tunnel_when_nobody_listens(tmp_path, monkeypatch):
    """A run that starts before the node exists starts without the desk's tunnel; its first
    step that asks the cluster found nobody at the loopback (docs/V1.md §8)."""
    monkeypatch.delenv("ORBIT_SSH_KEY", raising=False)
    inst, creds, kubeconfig = _tunnel_world(tmp_path)
    rec, state = Recorder(), {"open": False}

    def run(argv, **kw):
        state["open"] = True                      # the stand-in ssh "opens" it
        return rec(argv, **kw)

    assert onboard_run.ensure_tunnel(inst, creds, str(kubeconfig), run=run, port_open=lambda: state["open"], sleep=lambda s: None) is True
    (call,) = rec.calls
    argv = call["argv"]
    assert argv[0] == "ssh" and "root@192.0.2.7" in argv and "-fN" in argv
    assert "127.0.0.1:6443:127.0.0.1:6443" in argv and str(creds / "node-key") in argv
    assert f"UserKnownHostsFile={creds / 'known_hosts'}" in argv


def test_a_tunnel_that_is_there_or_not_needed_is_left_alone(tmp_path):
    inst, creds, kubeconfig = _tunnel_world(tmp_path)
    rec = Recorder()
    assert onboard_run.ensure_tunnel(inst, creds, str(kubeconfig), run=rec, port_open=lambda: True) is False
    inst2, creds2, direct = _tunnel_world(tmp_path / "direct", server="https://203.0.113.9:6443")
    assert onboard_run.ensure_tunnel(inst2, creds2, str(direct), run=rec, port_open=lambda: False) is False
    assert rec.calls == []


def test_a_tunnel_that_does_not_open_stops_the_step_in_one_sentence(tmp_path):
    inst, creds, kubeconfig = _tunnel_world(tmp_path)
    with pytest.raises(FormError) as caught:
        onboard_run.ensure_tunnel(inst, creds, str(kubeconfig), run=Recorder(), port_open=lambda: False, sleep=lambda s: None)
    assert "did not open" in str(caught.value)


# ----------------------------------------------------------------------------- phase 2

needs_sops = pytest.mark.skipif(not (shutil.which("sops") and shutil.which("age-keygen")), reason="needs sops and age")


def _instance_with_key(tmp_path):
    from orbitd import secrets as sx
    inst, creds = tmp_path / "inst", tmp_path / "creds"
    inst.mkdir()
    form = load_form(write_form(tmp_path, FORM))
    make_credentials(creds, form)
    sx.write_sops_yaml(inst / ".sops.yaml", onboard_run.age_recipient(creds))
    return inst, creds, form


def _decrypt(path, creds):
    env = dict(os.environ, SOPS_AGE_KEY_FILE=str(creds / "age.key"))
    out = subprocess.run(["sops", "--config", os.devnull, "decrypt", "--input-type", "yaml", "--output-type", "yaml", str(path)],
                         capture_output=True, text=True, env=env, check=True).stdout
    return yaml.safe_load(out)["spec"]["secretTemplates"][0]


@needs_sops
def test_a_sealed_file_decrypts_to_the_value_and_holds_no_plaintext(tmp_path):
    import base64
    inst, creds, _ = _instance_with_key(tmp_path)
    assert onboard_run.seal(inst, "monitoring", "alertmanager-telegram", {"bot-token": "VALUE-" + TOKEN})
    path = inst / "clusters/platform/secrets/monitoring/alertmanager-telegram.yaml"
    assert TOKEN not in path.read_text()
    tpl = _decrypt(path, creds)
    assert base64.b64decode(tpl["data"]["bot-token"]).decode() == "VALUE-" + TOKEN
    assert not onboard_run.seal(inst, "monitoring", "alertmanager-telegram", {"bot-token": "other"})


@needs_sops
def test_the_platform_set_is_sealed_once_with_generated_values_kept_private(tmp_path):
    inst, creds, form = _instance_with_key(tmp_path)
    app = {"id": "123", "installation_id": "456", "pem": "-----BEGIN RSA PRIVATE KEY-----\nX\n-----END RSA PRIVATE KEY-----"}
    written = onboard_run.seal_platform_secrets(inst, creds, form, ghcr_token="ghp_" + TOKEN, app=app)
    assert set(written) == {"cert-manager/cloudflare-api-token", "monitoring/alertmanager-telegram", "monitoring/grafana-admin",
                            "monitoring/grafana-image-renderer-token", "monitoring/orbit-alert-webhook", "monitoring/alertmanager-intake",
                            "orbit/orbit-alert-webhook", "orbit/ghcr-pull", "orbit/dev-basic-auth", "orbit/orbitd-registry", "loki/orbit-s3", "orbit/orbit-s3",
                            "tempo/orbit-s3", "orbit/orbitd-github-app"}
    for f in (inst / "clusters/platform/secrets").rglob("*.yaml"):
        assert TOKEN not in f.read_text(), f
    gh = _decrypt(inst / "clusters/platform/secrets/orbit/orbitd-github-app.yaml", creds)
    assert sorted(gh["data"]) == ["app-id", "github-app-id", "github-app-installation-id", "github-app-key.pem",
                                  "installation-id", "private-key.pem"]
    if os.name != "nt":
        for f in ("grafana-admin-password", "dev-basic-auth-password", "orbit-alert-webhook-token"):
            assert (creds / f).stat().st_mode & 0o077 == 0, f
    assert onboard_run.seal_platform_secrets(inst, creds, form, ghcr_token="x" * 20, app=app) == []


@pytest.mark.skipif(not shutil.which("openssl"), reason="needs openssl")
def test_the_dev_basic_auth_line_verifies_with_openssl(tmp_path):
    line = onboard_run.htpasswd_apr1("orbit", "pw-" + TOKEN)
    user, digest = line.split(":", 1)
    salt = digest.split("$")[2]
    again = subprocess.run(["openssl", "passwd", "-apr1", "-salt", salt, "-stdin"], input="pw-" + TOKEN + "\n",
                           capture_output=True, text=True, check=True).stdout.strip()
    assert (user, digest) == ("orbit", again)
    assert TOKEN not in line


def test_orbitd_bootstrap_output_goes_to_a_private_file_and_only_once(tmp_path):
    calls = []

    def run(argv, **kw):
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0, stdout='notice\n{"client_id": "admin", "client_secret": "S-' + TOKEN + '"}\n', stderr="")
    assert onboard_run.capture_orbitd_admin(tmp_path, run=run)
    assert '"client_secret"' in (tmp_path / "orbitd-admin.json").read_text()
    if os.name != "nt":
        assert (tmp_path / "orbitd-admin.json").stat().st_mode & 0o077 == 0
    assert not onboard_run.capture_orbitd_admin(tmp_path, run=run)
    assert len(calls) == 1 and calls[0][-2:] == ["orbitd", "bootstrap"]


def test_the_recovery_kit_carries_the_three_and_is_private(tmp_path):
    form = load_form(write_form(tmp_path, FORM))
    for name, text in (("age.key", "# public key: age1abc\nAGE-SECRET-KEY-1XYZ"), ("grafana-admin-password", "gpw"),
                       ("orbitd-admin.json", '{"client_id": "admin"}')):
        (tmp_path / name).write_text(text)
    kit = onboard_run.write_recovery_kit(tmp_path, form)
    text = kit.read_text()
    assert "AGE-SECRET-KEY-1XYZ" in text and "gpw" in text and '"client_id": "admin"' in text
    assert "https://grafana.acme.example" in text
    if os.name != "nt":
        assert kit.stat().st_mode & 0o077 == 0


# ----------------------------------------------------------------------------- the GitHub App flow

def test_the_app_flow_creates_installs_and_records_without_printing_a_credential(tmp_path):
    import socket
    import threading
    import urllib.request
    form = load_form(write_form(tmp_path, FORM))
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0)); port = s.getsockname()[1]
    said, out = [], {}

    def convert(code):
        assert code == "one-time-code"
        return {"id": 4242, "slug": "acme-orbit-ab12", "pem": "PEM-" + TOKEN, "client_id": "Iv1", "client_secret": "CS-" + TOKEN}

    t = threading.Thread(target=lambda: out.update(onboard_run.github_app_flow(
        tmp_path, form, owner_is_org=True, owner_id=7, repo_id=99, port=port, bind="127.0.0.1", timeout_s=20,
        convert=convert, announce=said.append)))
    t.start()
    base = f"http://127.0.0.1:{port}"
    for _ in range(50):
        try:
            page = urllib.request.urlopen(base + "/", timeout=2).read().decode(); break
        except OSError:
            import time; time.sleep(0.1)
    assert "https://github.com/organizations/acme-org/settings/apps/new?state=" in page
    state = page.split("?state=")[1].split('"')[0]
    import urllib.parse
    state = urllib.parse.unquote(state.replace("&amp;", "&"))

    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *a, **k): return None
    opener = urllib.request.build_opener(NoRedirect)
    try:
        opener.open(f"{base}/created?code=one-time-code&state={urllib.parse.quote(state)}", timeout=5)
    except urllib.error.HTTPError as e:
        assert e.code == 302
        assert e.headers["Location"] == ("https://github.com/apps/acme-orbit-ab12/installations/new/permissions"
                                         "?suggested_target_id=7&repository_ids[]=99")
    urllib.request.urlopen(f"{base}/installed?installation_id=555&setup_action=install", timeout=5).read()
    t.join(10)
    assert out == {"id": "4242", "slug": "acme-orbit-ab12", "pem": "PEM-" + TOKEN, "client_id": "Iv1",
                   "client_secret": "CS-" + TOKEN, "installation_id": "555"}
    assert json.loads((tmp_path / "github-app.json").read_text())["installation_id"] == "555"
    assert all(TOKEN not in line for line in said)
    # a finished flow is not run again
    assert onboard_run.github_app_flow(tmp_path, form, owner_is_org=True, owner_id=7, repo_id=99, port=port,
                                       timeout_s=1, convert=convert, announce=said.append)["installation_id"] == "555"


def test_a_created_callback_with_the_wrong_state_is_refused(tmp_path):
    import socket
    import threading
    import urllib.request
    form = load_form(write_form(tmp_path, FORM))
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0)); port = s.getsockname()[1]
    errors = []

    def go():
        try:
            onboard_run.github_app_flow(tmp_path, form, owner_is_org=False, owner_id=1, repo_id=2, port=port,
                                        bind="127.0.0.1", timeout_s=3, convert=lambda c: pytest.fail("converted"),
                                        announce=lambda m: None)
        except FormError as e:
            errors.append(str(e))
    t = threading.Thread(target=go); t.start()
    import time
    for _ in range(50):
        try:
            urllib.request.urlopen(f"http://127.0.0.1:{port}/", timeout=2); break
        except OSError:
            time.sleep(0.1)
    with pytest.raises(urllib.error.HTTPError) as e:
        urllib.request.urlopen(f"http://127.0.0.1:{port}/created?code=x&state=forged", timeout=5)
    assert e.value.code == 400
    t.join(10)
    assert errors and "did not finish" in errors[0]
    assert not (tmp_path / "github-app.json").exists()


# ----------------------------------------------------------------------------- phase 4 and the run

class Recorder:
    """A subprocess.run stand-in: records argv, env and stdin; answers by the first match."""
    def __init__(self, answers=None):
        self.calls, self.answers = [], answers or []

    def __call__(self, argv, **kw):
        self.calls.append({"argv": list(argv), "env": kw.get("env"), "input": kw.get("input")})
        for match, rc, out in self.answers:
            if match in " ".join(argv):
                return subprocess.CompletedProcess(argv, rc, stdout=out, stderr="")
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")


def test_the_runs_lines_reach_a_log_file_as_they_are_printed(tmp_path):
    """The skill starts the run writing to a log and reads the log. Block-buffered, the
    step lines reached the file when the run ended: an hour of an empty file (docs/V1.md §8)."""
    import sys
    import textwrap
    script = tmp_path / "probe.py"
    script.write_text(textwrap.dedent("""
        import sys, time, types
        from orbitd import onboard_run
        onboard_run.run = lambda *a, **k: (print("config: wrote x"), time.sleep(30), 0)[2]
        args = types.SimpleNamespace(instance_dir=".", form="f", config_dir=None, kubeconfig=None, only=None, port=1)
        sys.exit(onboard_run.cmd_run(args))
    """), encoding="utf-8")
    log = tmp_path / "run.log"
    env = dict(os.environ, PYTHONPATH=os.pathsep.join(p for p in sys.path if p))
    env.pop("PYTHONUNBUFFERED", None)
    with open(log, "w") as fh:
        proc = subprocess.Popen([sys.executable, str(script)], stdout=fh, stderr=subprocess.STDOUT, env=env)
    try:
        import time
        deadline = time.time() + 20
        while time.time() < deadline and "config: wrote x" not in log.read_text():
            time.sleep(0.2)
        assert "config: wrote x" in log.read_text(), "the line was still in a buffer while the run went on"
        assert proc.poll() is None, "the run had already ended: this proved nothing"
    finally:
        proc.kill(); proc.wait()


def test_a_second_config_step_writes_nothing_and_says_so(tmp_path):
    """A form whose bucket region is the example's (`fsn1`) had `config: wrote
    objectStorage.endpoint` on every run, and a commit for it (docs/V1.md §8)."""
    import copy
    doc = copy.deepcopy(FORM); doc["object_storage"]["location"] = "fsn1"
    form = load_form(write_form(tmp_path, doc))
    inst = tmp_path / "inst"
    (inst / "instance").mkdir(parents=True)
    example = Path(onboard_run.__file__).resolve().parents[3] / "template" / "instance" / "config.yaml.example"
    (inst / "instance" / "config.yaml").write_text(example.read_text(encoding="utf-8").replace("name: example ", f"name: {form.name} "), encoding="utf-8")
    first = onboard_run.fill_config(inst, form)
    assert "objectStorage.buckets.platform" in first and "objectStorage.endpoint" not in first
    before = (inst / "instance" / "config.yaml").read_text(encoding="utf-8")
    assert onboard_run.fill_config(inst, form) == []
    assert (inst / "instance" / "config.yaml").read_text(encoding="utf-8") == before


def test_tofu_init_is_tried_again_and_apply_is_not(tmp_path):
    """`tofu init` asks the provider registry and changes nothing; one dropped connection
    stopped a resumed run at it (docs/V1.md §8). `apply` changes things: once."""
    form = load_form(write_form(tmp_path, FORM))
    (tmp_path / "env").write_text(f"export HCLOUD_TOKEN={form.hetzner_token}\nexport CLOUDFLARE_API_TOKEN={form.cloudflare_token}\n")
    calls, waits = [], []

    def run(argv, **kw):
        calls.append(argv[2] if len(argv) > 2 else argv[0])
        rc = 1 if (argv[2:3] == ["init"] and calls.count("init") < 3) else 0
        return subprocess.CompletedProcess(argv, rc, stdout="198.51.100.7" if "output" in argv else "", stderr="")

    assert onboard_run.tofu_apply(tmp_path, tmp_path, run=run, sleep=waits.append) == "198.51.100.7"
    assert calls == ["init", "init", "init", "apply", "output"] and waits == [20, 20]

    def never(argv, **kw):
        calls.append(argv[2])
        return subprocess.CompletedProcess(argv, 1, stdout="", stderr="")

    del calls[:]
    with pytest.raises(FormError, match="init` failed three times .* to read why"):
        onboard_run.tofu_apply(tmp_path, tmp_path, run=never, sleep=lambda s: None)
    assert calls == ["init", "init", "init"]

    def apply_fails(argv, **kw):
        calls.append(argv[2])
        return subprocess.CompletedProcess(argv, 1 if argv[2] == "apply" else 0, stdout="", stderr="")

    del calls[:]
    with pytest.raises(FormError, match="apply` failed"):
        onboard_run.tofu_apply(tmp_path, tmp_path, run=apply_fails, sleep=lambda s: None)
    assert calls == ["init", "apply"]


def test_the_acceptance_of_a_platform_minutes_old_is_run_again_and_only_a_green_run_passes():
    """M4's first acceptance ran the moment the last app was applied and was red on rows
    that were green ten minutes later (docs/V1.md §8)."""
    said, waits = [], []
    runs = iter([(2, 6), (0, 0)])
    assert onboard_run.accept_with_patience(lambda: next(runs), said.append, sleep=waits.append, settle_s=300) \
        == "make verify: every stage passed (on run 2; the platform was still settling before)"
    assert waits == [300] and said == ["acceptance: 6 row(s) not passing on run 1; a platform minutes old is still settling, again in 5 minutes"]
    assert onboard_run.accept_with_patience(lambda: (0, 0), said.append, sleep=waits.append) == "make verify: every stage passed"
    assert waits == [300]
    runs = iter([(2, 6), (2, 4), (2, 4)])
    with pytest.raises(FormError, match=r"did not pass \(4 FAIL line\(s\), three runs\)"):
        onboard_run.accept_with_patience(lambda: next(runs), said.append, sleep=waits.append)
    assert waits == [300, 300, 300]                                       # two more waits, none after the last run
    with pytest.raises(FormError, match="make is not installed"):
        onboard_run.accept_with_patience(lambda: (None, 0), said.append, sleep=waits.append)


def test_the_platform_apply_on_a_new_cluster_is_run_until_one_pass_holds(tmp_path):
    """M4's eighth run: the apply stopped twice on a cluster minutes old (the apps that own
    admission webhooks were not serving yet) and a third pass applied everything (§8)."""
    said, waits = [], []
    answers = iter([(1, '{"ok": false, "reason": "webhook not ready"}'), (1, '{"ok": false, "reason": "second"}'), (0, '{"ok": true}')])

    def apply():
        rc, text = next(answers)
        return subprocess.CompletedProcess(["orbit"], rc, stdout=text, stderr="")

    assert onboard_run.apply_until_it_holds(apply, said.append, tmp_path, sleep=waits.append) == 3
    assert waits == [60, 60] and len(said) == 2 and said[0].startswith("platform: pass 1 did not apply everything")
    log = tmp_path / "platform-apply.log"
    assert log.read_text() == '{"ok": true}'                      # the last pass's
    if os.name != "nt":
        assert log.stat().st_mode & 0o077 == 0
    never = lambda: subprocess.CompletedProcess(["orbit"], 1, stdout='{"reason": "still refused"}', stderr="boom")
    with pytest.raises(FormError, match=r"failed 5 times \(exit 1\); the last pass's output is in platform-apply.log"):
        onboard_run.apply_until_it_holds(never, said.append, tmp_path, sleep=waits.append)
    assert len(waits) == 2 + 4 and "still refused" in log.read_text() and "boom" in log.read_text()
    assert onboard_run.apply_until_it_holds(lambda: subprocess.CompletedProcess(["orbit"], 0, stdout="", stderr=""), said.append, tmp_path, sleep=waits.append) == 1


class FakeGit:
    """git in a checkout whose origin is a GitHub repository; `refuse` is the stderr GitHub
    gives a direct push to main (empty: the push is taken)."""
    def __init__(self, refuse="", ff=0):
        self.calls, self.refuse, self.ff = [], refuse, ff

    def __call__(self, argv, **kw):
        args = argv[5:] if argv[:2] == ["git", "-C"] else argv[1:]          # after -C <dir> -c credential.helper=...
        self.calls.append(args)
        rc, out, err = 0, "", ""
        if args[:2] == ["diff", "--cached"]:
            rc = 1                                                           # there is something to commit
        elif args[:1] == ["push"] and "HEAD:main" in args and self.refuse:
            rc, err = 1, self.refuse
        elif args[:2] == ["remote", "get-url"]:
            out = "https://github.com/acme/acme-platform.git\n"
        elif args[:1] == ["rev-parse"]:
            out = "0123456789ab\n"
        elif args[:1] == ["merge"]:
            rc = self.ff
        return subprocess.CompletedProcess(argv, rc, stdout=out, stderr=err)


GH013 = ("remote: error: GH013: Repository rule violations found for refs/heads/main.\n"
         "remote: - Changes must be made through a pull request.\n ! [remote rejected] HEAD -> main\n")


def test_a_main_that_takes_pull_requests_only_gets_the_commit_through_one(tmp_path):
    """M4's second live run stopped at `repository`: the organization's repository keeps direct
    pushes off main (docs/V1.md §8). The rule is kept: a branch, a pull request, a merge, and
    the checkout follows."""
    fake, api_calls = FakeGit(refuse=GH013), []

    def api(method, path, body=None):
        api_calls.append((method, path, body))
        return {"number": 7} if method == "POST" else {}

    assert onboard_run.commit_and_push(tmp_path, "onboard: the instance", run=fake, api=api) is True
    assert ["push", "-q", "origin", "HEAD:refs/heads/orbit-onboard/0123456789ab"] in fake.calls
    assert api_calls[0][:2] == ("POST", "/repos/acme/acme-platform/pulls")
    assert api_calls[0][2]["head"] == "orbit-onboard/0123456789ab" and api_calls[0][2]["base"] == "main"
    assert api_calls[1] == ("PUT", "/repos/acme/acme-platform/pulls/7/merge", {"merge_method": "merge"})
    assert api_calls[2][:2] == ("DELETE", "/repos/acme/acme-platform/git/refs/heads/orbit-onboard/0123456789ab")
    assert fake.calls[-2:] == [["fetch", "-q", "origin", "main"], ["merge", "-q", "--ff-only", "FETCH_HEAD"]]


def test_a_push_main_takes_needs_no_pull_request_and_any_other_refusal_is_a_stop(tmp_path):
    fake, api_calls = FakeGit(), []
    assert onboard_run.commit_and_push(tmp_path, "m", run=fake, api=lambda *a: api_calls.append(a)) is True
    assert api_calls == [] and not any(c[:1] == ["fetch"] for c in fake.calls)
    with pytest.raises(FormError, match="git push to the instance repository failed$"):
        onboard_run.commit_and_push(tmp_path, "m", run=FakeGit(refuse="fatal: Authentication failed"), api=lambda *a: api_calls.append(a))
    assert api_calls == []


def test_a_pull_request_that_cannot_be_merged_stops_in_one_sentence(tmp_path):
    def api(method, path, body=None):
        if method == "PUT":
            raise FormError("GitHub PUT /repos/acme/acme-platform/pulls/7/merge answered 405")
        return {"number": 7}

    with pytest.raises(FormError, match="pull request #7 could not be merged .* Merge it, then run again"):
        onboard_run.commit_and_push(tmp_path, "m", run=FakeGit(refuse=GH013), api=api)


def _ruleset(**over):
    rs = {"id": 5, "name": "protected lines", "target": "branch", "enforcement": "active", "source_type": "Repository",
          "bypass_actors": [{"actor_id": 111, "actor_type": "Integration", "bypass_mode": "always"}],
          "conditions": {"ref_name": {"exclude": [], "include": ["refs/heads/main", "refs/heads/dev"]}},
          "rules": [{"type": "deletion"}, {"type": "pull_request", "parameters": {"required_approving_review_count": 0}}]}
    rs.update(over)
    return rs


def _rules_api(rulesets, puts):
    def api(method, path, body=None):
        if method == "GET" and path.startswith("/repos/acme/acme-platform/rulesets?"):
            return [{"id": r["id"]} for r in rulesets]
        if method == "GET":
            return next(r for r in rulesets if f"/rulesets/{r['id']}?" in path)
        puts.append((method, path, body))
        return {}
    return api


def test_the_app_is_added_to_a_rule_that_keeps_pushes_off_main_and_nothing_else_changes():
    """Orbit writes to main as its App: a rule that keeps direct pushes off main, with only
    another App on its bypass list, lets the platform come up and fail at its first write
    (docs/V1.md §8)."""
    rs, puts = _ruleset(), []
    assert onboard_run.let_the_app_write("acme/acme-platform", 222, api=_rules_api([rs], puts)) == ["protected lines"]
    ((method, path, body),) = puts
    assert (method, path) == ("PUT", "/repos/acme/acme-platform/rulesets/5")
    assert body["bypass_actors"] == [{"actor_id": 111, "actor_type": "Integration", "bypass_mode": "always"},
                                     {"actor_id": 222, "actor_type": "Integration", "bypass_mode": "always"}]
    assert (body["name"], body["target"], body["enforcement"]) == ("protected lines", "branch", "active")
    assert body["rules"] == rs["rules"] and body["conditions"] == rs["conditions"]


def test_rules_that_do_not_keep_the_app_off_main_are_left_alone():
    puts = []
    quiet = [_ruleset(id=1, bypass_actors=[{"actor_id": 222, "actor_type": "Integration", "bypass_mode": "always"}]),  # already there
             _ruleset(id=2, enforcement="disabled"),
             _ruleset(id=3, target="tag"),
             _ruleset(id=4, conditions={"ref_name": {"include": ["refs/heads/release"], "exclude": []}}),
             _ruleset(id=6, rules=[{"type": "deletion"}, {"type": "non_fast_forward"}])]                                # blocks no push
    assert onboard_run.let_the_app_write("acme/acme-platform", 222, api=_rules_api(quiet, puts)) == []
    assert puts == []

    def none(method, path, body=None):
        raise FormError("GitHub GET /repos/acme/acme-platform/rulesets answered 403")      # no rulesets on this plan
    assert onboard_run.let_the_app_write("acme/acme-platform", 222, api=none) == []


def test_a_rule_on_the_organization_is_not_the_runs_to_change():
    puts = []
    with pytest.raises(FormError, match="on the organization keeps direct pushes off main .* bypass list"):
        onboard_run.let_the_app_write("acme/acme-platform", 222, api=_rules_api([_ruleset(source_type="Organization")], puts))
    assert puts == []


def test_tokens_reach_subprocesses_only_by_environment_or_stdin(tmp_path):
    form = load_form(write_form(tmp_path, FORM))
    (tmp_path / "env").write_text(f"export HCLOUD_TOKEN={form.hetzner_token}\nexport CLOUDFLARE_API_TOKEN={form.cloudflare_token}\n")
    rec = Recorder([("output -raw server_ipv4", 0, "198.51.100.7"), ("curl", 0, "200")])
    assert onboard_run.tofu_apply(tmp_path, tmp_path, run=rec) == "198.51.100.7"
    assert onboard_run.create_buckets(form, ["acme-orbit-platform"], run=rec) == ["acme-orbit-platform"]
    for c in rec.calls:
        assert TOKEN not in " ".join(c["argv"]), c["argv"]
    tofu = [c for c in rec.calls if c["argv"][0] == "tofu"]
    assert all(c["env"]["HCLOUD_TOKEN"] == form.hetzner_token for c in tofu)
    curl = [c for c in rec.calls if c["argv"][0] == "curl"][0]
    assert form.s3_secret_key in curl["input"] and "-K" in curl["argv"]


def test_the_address_is_written_once_and_a_different_one_refused(tmp_path):
    (tmp_path / "instance").mkdir()
    cfg = tmp_path / "instance" / "config.yaml"
    cfg.write_text((REPO / "template" / "instance" / "config.yaml.example").read_text())
    assert onboard_run.write_ip(tmp_path, "198.51.100.7")
    assert yaml.safe_load(cfg.read_text())["nodes"][0]["ip"] == "198.51.100.7"
    assert not onboard_run.write_ip(tmp_path, "198.51.100.7")
    with pytest.raises(FormError, match="neither the placeholder"):
        onboard_run.write_ip(tmp_path, "198.51.100.8")


def test_adopting_a_server_writes_the_import_and_creating_one_does_not(tmp_path):
    form = load_form(write_form(tmp_path, FORM))
    a, b = tmp_path / "a", tmp_path / "b"
    onboard_run.write_tfvars(a, tmp_path, form, server_id="12345678", zone_id="z1")
    assert 'server_id            = "12345678"' in (a / "tofu" / "instance.auto.tfvars").read_text()
    assert "id = var.server_id" in (a / "tofu" / "import.tf").read_text()
    onboard_run.write_tfvars(b, tmp_path, form, server_id=None, zone_id="z1")
    assert not (b / "tofu" / "import.tf").exists()
    assert TOKEN not in (a / "tofu" / "instance.auto.tfvars").read_text()


def test_a_run_without_a_server_to_adopt_or_a_yes_to_buy_stops_before_spending(tmp_path, monkeypatch):
    inst = template_instance(tmp_path / "inst")
    form_path = write_form(tmp_path, FORM)
    said = []
    monkeypatch.setattr(onboard_run, "make_credentials", lambda d, f, run=None: [])
    monkeypatch.setattr(onboard_run, "age_recipient", lambda d: "age1x")
    monkeypatch.setattr(onboard_run, "ensure_repo", lambda *a, **k: {"id": 1, "full_name": "acme-org/acme-platform"})
    monkeypatch.setattr(onboard_run, "commit_and_push", lambda *a, **k: True)
    monkeypatch.setattr(onboard_run, "github_api", lambda m, p, b=None, **k: {"type": "Organization", "id": 9})
    monkeypatch.setattr(onboard_run, "github_app_flow", lambda *a, **k: {"id": 4242, "slug": "s", "installation_id": "5"})
    zone = []
    monkeypatch.setattr(onboard_run, "cloudflare_zone_id", lambda f: zone.append(1) or "z")
    rec = Recorder()
    rc = onboard_run.run(inst, form_path, config_dir=tmp_path / "creds", out=said.append, sub=rec)
    assert rc == 1
    assert [l.split(":")[0] for l in said] == ["config", "credentials", "repository", "github-app", "infrastructure"]
    assert "STOPPED" in said[-1] and "costs money" in said[-1]
    assert zone == [] and not any(c["argv"][0] == "tofu" for c in rec.calls)
    assert all(TOKEN not in l for l in said)


def _adopt_instance(tmp_path):
    inst = tmp_path / "inst"; (inst / "instance").mkdir(parents=True)
    (inst / "instance" / "config.yaml").write_text(
        "instance:\n  name: acme\ndomains:\n  primary: acme.example\n  dev: dev.acme.example\n")
    return inst


def test_adopting_takes_over_what_the_old_node_left_and_nothing_else(tmp_path):
    form = load_form(write_form(tmp_path, FORM))
    inst = _adopt_instance(tmp_path)
    asked = []
    def cf(m, p, b=None):
        asked.append(p)
        return {"result": [{"id": "rec-primary"}]} if "%2A.acme.example" in p else {"result": [{"id": "rec-dev"}]}
    def hz(m, p, b=None):
        asked.append(p)
        if p.startswith("/firewalls?name=acme-platform-prod"):
            return {"firewalls": [{"id": 77}]}
        if p.startswith("/ssh_keys?name=acme-operator"):
            return {"ssh_keys": [{"id": 88}]}
        return {}
    taken = onboard_run.adopt_imports(inst, form, zone_id="z1", hz=hz, cf=cf)
    assert taken == ["cloudflare_dns_record.wildcard_primary", "cloudflare_dns_record.wildcard_dev",
                     "hcloud_firewall.prod", "hcloud_firewall_attachment.prod", "hcloud_ssh_key.operator"]
    tf = (inst / "tofu" / "adopt.tf").read_text()
    assert 'id = "z1/rec-primary"' in tf and 'id = "z1/rec-dev"' in tf
    assert 'id = "77"' in tf and 'id = "88"' in tf
    assert TOKEN not in tf


def test_adopting_a_bare_account_writes_no_imports_and_two_records_for_one_name_stop(tmp_path):
    form = load_form(write_form(tmp_path, FORM))
    inst = _adopt_instance(tmp_path)
    empty = lambda m, p, b=None: {}
    assert onboard_run.adopt_imports(inst, form, zone_id="z1", hz=empty, cf=lambda m, p, b=None: {"result": []}) == []
    assert not (inst / "tofu" / "adopt.tf").exists()
    with pytest.raises(FormError, match="2 A records"):
        onboard_run.adopt_imports(inst, form, zone_id="z1", hz=empty,
                                  cf=lambda m, p, b=None: {"result": [{"id": "a"}, {"id": "b"}]})


def test_a_rebuild_authorizes_the_node_key_waits_for_ssh_and_happens_once(tmp_path):
    form = load_form(write_form(tmp_path, FORM))
    creds = tmp_path / "creds"; creds.mkdir()
    (creds / "node-key.pub").write_text("ssh-ed25519 AAAAPUBLIC acme-node\n")
    (creds / "node-key").write_text("private\n")
    calls, polls = [], iter([{"action": {"id": 5, "status": "running"}}, {"action": {"id": 5, "status": "success"}}])
    def hz(m, p, b=None):
        calls.append((m, p, b))
        if p.endswith("/actions/rebuild"):
            return {"action": {"id": 5, "status": "running"}}
        if p == "/actions/5":
            return next(polls)
        if p == "/servers/42":
            return {"server": {"public_net": {"ipv4": {"ip": "198.51.100.9"}}}}
        raise AssertionError(p)
    probes = iter([255, 0])
    rec = []
    def run(argv, **kw):
        rec.append(argv)
        return subprocess.CompletedProcess(argv, next(probes), stdout="", stderr="")
    assert onboard_run.rebuild_server(creds, form, "42", hz=hz, run=run, sleep=lambda s: None)
    m, p, body = calls[0]
    assert (m, p, body["image"]) == ("POST", "/servers/42/actions/rebuild", onboard_run.NODE_IMAGE)
    assert body["user_data"].startswith("#cloud-config\n") and "ssh-ed25519 AAAAPUBLIC acme-node" in body["user_data"]
    # the provider's root password is never the thing that keeps the key out (docs/V1.md §8)
    doc = yaml.safe_load(body["user_data"])
    assert doc["ssh_authorized_keys"] == ["ssh-ed25519 AAAAPUBLIC acme-node"]
    assert doc["chpasswd"] == {"expire": False}
    # the provider's own first-boot command is kept: this list replaces the provider's
    assert doc["runcmd"] == ["udevadm trigger -c add -s block -p ID_VENDOR=HC --verbose -p ID_MODEL=Volume",
                             'chage -d "$(date +%F)" -M -1 root']
    assert len(rec) == 2 and rec[-1][-2] == "root@198.51.100.9"
    assert (creds / "rebuilt-server-42").exists()
    calls.clear()
    assert not onboard_run.rebuild_server(creds, form, "42", hz=hz, run=run, sleep=lambda s: None)
    assert calls == [], "a resumed run must never rebuild again"


def test_a_failed_rebuild_stops_and_leaves_no_marker(tmp_path):
    form = load_form(write_form(tmp_path, FORM))
    creds = tmp_path / "creds"; creds.mkdir()
    (creds / "node-key.pub").write_text("ssh-ed25519 AAAAPUBLIC acme-node\n")
    hz = lambda m, p, b=None: {"action": {"id": 5, "status": "error"}}
    with pytest.raises(FormError, match="as error"):
        onboard_run.rebuild_server(creds, form, "42", hz=hz, run=Recorder(), sleep=lambda s: None)
    assert not (creds / "rebuilt-server-42").exists()


def test_adopting_without_a_yes_to_the_rebuild_stops_before_touching_anything(tmp_path, monkeypatch):
    inst = template_instance(tmp_path / "inst")
    doc = json.loads(json.dumps(FORM)); doc["hetzner"]["server_id"] = 42
    form_path = write_form(tmp_path, doc)
    assert load_form(form_path).rebuild is False
    doc["hetzner"]["rebuild"] = True
    assert load_form(write_form(tmp_path, doc)).rebuild is True
    doc["hetzner"].pop("rebuild"); form_path = write_form(tmp_path, doc)
    said = []
    for name, fn in (("make_credentials", lambda d, f, run=None: []), ("age_recipient", lambda d: "age1x"),
                     ("ensure_repo", lambda *a, **k: {"id": 1, "full_name": "acme-org/acme-platform"}),
                     ("commit_and_push", lambda *a, **k: True),
                     ("github_api", lambda m, p, b=None, **k: {"type": "Organization", "id": 9}),
                     ("github_app_flow", lambda *a, **k: {"id": 4242, "slug": "s", "installation_id": "5"})):
        monkeypatch.setattr(onboard_run, name, fn)
    touched = []
    monkeypatch.setattr(onboard_run, "cloudflare_zone_id", lambda f: touched.append("zone") or "z")
    monkeypatch.setattr(onboard_run, "hetzner_api", lambda *a, **k: touched.append("hetzner"))
    rec = Recorder()
    assert onboard_run.run(inst, form_path, config_dir=tmp_path / "creds", out=said.append, sub=rec) == 1
    assert said[-1].startswith("infrastructure: STOPPED") and "hetzner.rebuild: true" in said[-1]
    assert touched == [] and not any(c["argv"][0] == "tofu" for c in rec.calls)


def test_orbit_onboard_run_is_a_desk_command():
    import argparse
    from orbitd import onboard
    p = argparse.ArgumentParser(); sub = p.add_subparsers(dest="cmd")
    onboard.add_parser(sub)
    args = p.parse_args(["onboard", "run", ".", "--form", "f.yaml", "--only", "buckets"])
    assert (args.onboard_command, args.form, args.only, args.port) == ("run", "f.yaml", "buckets", 8765)
