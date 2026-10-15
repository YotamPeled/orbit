"""orbit secrets export/verify: sops ciphertext of ExternalSecret targets."""

from __future__ import annotations

import base64
import binascii
import logging
import os
import re
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from orbitd.secrets import (
    ENCRYPTED_REGEX, EXIT_DIFF, EXIT_OK, EXIT_USAGE, PATH_REGEX,
    cmd_export, cmd_verify, export_secrets, main, verify_secrets,
)
from orbitd.testing import FakeKube

SOPS = shutil.which("sops")
AGE_KEYGEN = shutil.which("age-keygen")

PLANT_TOKEN = "plantval_export_aa11zz"
PLANT_TOKEN_B64 = base64.b64encode(PLANT_TOKEN.encode()).decode()
PLANT_DOCKER = "plantval_docker_bb22yy"
PLANT_DOCKER_B64 = base64.b64encode(PLANT_DOCKER.encode()).decode()
PLANT_INFISICAL = "plantval_infisical_cc33xx"
PLANT_INFISICAL_B64 = base64.b64encode(PLANT_INFISICAL.encode()).decode()
PLANT_ANNOT = "plantval_annotation_dd44ww"
PLANT_CHANGED = "plantval_changed_ee55vv"
PLANT_CHANGED_B64 = base64.b64encode(PLANT_CHANGED.encode()).decode()
PLANTS = (
    PLANT_TOKEN, PLANT_TOKEN_B64,
    PLANT_DOCKER, PLANT_DOCKER_B64,
    PLANT_INFISICAL, PLANT_INFISICAL_B64,
    PLANT_ANNOT, PLANT_CHANGED, PLANT_CHANGED_B64,
)
RECIPIENT = "age1fakeRecipientPublicKeyNotASecret0000"
FAKE_PREFIX = "ENC[fake:"
FAKE_SUFFIX = "]"
DOCKER_TYPE = "kubernetes.io/dockerconfigjson"
DOCKER_KEY = ".dockerconfigjson"


def _b64(text: str) -> str:
    return base64.b64encode(text.encode()).decode()


def _es(namespace: str, name: str, target: str | None = None) -> dict:
    spec: dict = {}
    if target is not None:
        spec["target"] = {"name": target}
    return {
        "apiVersion": "external-secrets.io/v1",
        "kind": "ExternalSecret",
        "metadata": {"name": name, "namespace": namespace},
        "spec": spec,
    }


def _secret(namespace: str, name: str, *, secret_type: str = "Opaque",
            data: dict[str, str] | None = None) -> dict:
    return {
        "apiVersion": "v1",
        "kind": "Secret",
        "metadata": {
            "name": name,
            "namespace": namespace,
            "resourceVersion": "9",
            "annotations": {
                "kubectl.kubernetes.io/last-applied-configuration": PLANT_ANNOT,
            },
        },
        "type": secret_type,
        "data": dict(data or {}),
    }


def _flag(argv: list[str], name: str) -> str | None:
    if name in argv:
        idx = argv.index(name)
        if idx + 1 < len(argv):
            return argv[idx + 1]
    prefix = name + "="
    for item in argv:
        if item.startswith(prefix):
            return item[len(prefix):]
    return None


def _seal(text: str) -> str:
    if text.startswith(FAKE_PREFIX) and text.endswith(FAKE_SUFFIX):
        return text
    hx = bytes(b ^ 0x5A for b in text.encode("utf-8")).hex()
    return f"{FAKE_PREFIX}{hx}{FAKE_SUFFIX}"


def _unseal(text: str) -> str:
    if not (isinstance(text, str) and text.startswith(FAKE_PREFIX) and text.endswith(FAKE_SUFFIX)):
        return text
    hx = text[len(FAKE_PREFIX):-len(FAKE_SUFFIX)]
    try:
        raw = bytes(int(hx[i:i + 2], 16) ^ 0x5A for i in range(0, len(hx), 2))
        return raw.decode("utf-8")
    except (ValueError, UnicodeDecodeError, binascii.Error):
        return text


def _transform(obj, regex: str, fn):
    if isinstance(obj, dict):
        out = {}
        for key, value in obj.items():
            if key == "sops":
                out[key] = value
                continue
            if re.search(regex, str(key)):
                if isinstance(value, dict):
                    out[key] = {
                        inner: fn(inner_val) if isinstance(inner_val, str) else inner_val
                        for inner, inner_val in value.items()
                    }
                elif isinstance(value, str):
                    out[key] = fn(value)
                elif isinstance(value, list):
                    out[key] = [fn(item) if isinstance(item, str) else item for item in value]
                else:
                    out[key] = value
            else:
                out[key] = _transform(value, regex, fn)
        return out
    if isinstance(obj, list):
        return [_transform(item, regex, fn) for item in obj]
    return obj


class FakeSops:
    """Records argv/stdin/env. Encrypts data/stringData leaves; refuses like sops."""

    def __init__(self) -> None:
        self.calls: list[dict] = []

    def run(self, argv: list[str], *, stdin: str = "", env=None,
            cwd=None) -> subprocess.CompletedProcess[str]:
        recorded_env = dict(os.environ if env is None else env)
        self.calls.append({
            "argv": list(argv), "stdin": stdin, "env": recorded_env, "cwd": cwd,
        })
        if not argv or argv[0] != "sops":
            return subprocess.CompletedProcess(argv, 2, "", "usage")
        if "encrypt" in argv:
            return self._encrypt(argv, stdin)
        if "decrypt" in argv:
            return self._decrypt(argv, stdin)
        return subprocess.CompletedProcess(argv, 2, "", "unknown command")

    def _encrypt(self, argv: list[str], stdin: str) -> subprocess.CompletedProcess[str]:
        if _flag(argv, "--filename-override") is None:
            return subprocess.CompletedProcess(argv, 1, "", "filename-override required")
        recipient = _flag(argv, "--age") or _flag(argv, "-a")
        if not recipient:
            return subprocess.CompletedProcess(argv, 1, "", "no age recipient")
        regex = _flag(argv, "--encrypted-regex") or ENCRYPTED_REGEX
        try:
            loaded = yaml.safe_load(stdin) or {}
        except yaml.YAMLError:
            return subprocess.CompletedProcess(argv, 1, "", "invalid yaml")
        if not isinstance(loaded, dict):
            return subprocess.CompletedProcess(argv, 1, "", "invalid yaml")
        sealed = _transform(loaded, regex, _seal)
        sealed["sops"] = {
            "encrypted_regex": regex,
            "age": [{"recipient": recipient}],
            "mac": "ENC[fake:00]",
        }
        text = yaml.safe_dump(sealed, sort_keys=False)
        return subprocess.CompletedProcess(argv, 0, text if text.endswith("\n") else text + "\n", "")

    def _decrypt(self, argv: list[str], stdin: str) -> subprocess.CompletedProcess[str]:
        if _flag(argv, "--filename-override") is None:
            return subprocess.CompletedProcess(argv, 1, "", "filename-override required")
        try:
            loaded = yaml.safe_load(stdin) or {}
        except yaml.YAMLError:
            return subprocess.CompletedProcess(argv, 1, "", "invalid yaml")
        if not isinstance(loaded, dict) or "sops" not in loaded:
            return subprocess.CompletedProcess(argv, 1, "", "not sops encrypted")
        regex = str((loaded.get("sops") or {}).get("encrypted_regex") or ENCRYPTED_REGEX)
        loaded.pop("sops", None)
        opened = _transform(loaded, regex, _unseal)
        text = yaml.safe_dump(opened, sort_keys=False)
        return subprocess.CompletedProcess(argv, 0, text if text.endswith("\n") else text + "\n", "")


def _world(tmp_path: Path) -> tuple[Path, FakeKube, Path, FakeSops]:
    instance = tmp_path / "instance"
    instance.mkdir()
    pub = tmp_path / "recipient.pub"
    pub.write_text(RECIPIENT + "\n")
    kube = FakeKube()
    kube.add_object(_es("proj-dev", "app-config"))
    kube.add_object(_secret("proj-dev", "app-config", data={"TOKEN": PLANT_TOKEN_B64, "OTHER": _b64("other")}))
    kube.add_object(_es("proj-dev", "ghcr-pull"))
    kube.add_object(_secret(
        "proj-dev", "ghcr-pull",
        secret_type=DOCKER_TYPE,
        data={DOCKER_KEY: PLANT_DOCKER_B64},
    ))
    kube.add_object(_es("infisical", "vault-bootstrap"))
    kube.add_object(_secret("infisical", "vault-bootstrap", data={"KEY": PLANT_INFISICAL_B64}))
    return instance, kube, pub, FakeSops()


def _age_key(tmp_path: Path, monkeypatch) -> Path:
    path = tmp_path / "age.agekey"
    path.write_text("AGE-SECRET-KEY-FAKE-NOT-USED-BY-FAKE-SOPS\n")
    monkeypatch.setenv("SOPS_AGE_KEY_FILE", str(path))
    return path


def _args(instance: Path, kube: FakeKube, fake: FakeSops, pub: Path | None = None):
    return SimpleNamespace(
        instance_dir=str(instance),
        kube=kube,
        kubeconfig=None,
        recipient_file=str(pub) if pub is not None else None,
        sops_run=fake.run,
    )


def _joined_calls(fake: FakeSops) -> str:
    parts: list[str] = []
    for call in fake.calls:
        parts.append(" ".join(call["argv"]))
        parts.extend(str(v) for v in call["env"].values())
        parts.extend(str(k) for k in call["env"].keys())
    return "\n".join(parts)


def assert_no_plants(*blobs: str) -> None:
    text = "\n".join(blobs)
    for plant in PLANTS:
        assert plant not in text


def _decrypt_file(fake: FakeSops, path: Path) -> dict:
    proc = fake.run(
        ["sops", "--config", os.devnull, "decrypt",
         "--input-type", "yaml", "--output-type", "yaml",
         "--filename-override", path.name],
        stdin=path.read_text(),
    )
    assert proc.returncode == 0
    return yaml.safe_load(proc.stdout)


# ------------------------------------------------------------------ export


def test_export_writes_one_file_per_target_skips_infisical_keeps_dockerconfigjson(
        tmp_path, capsys, caplog):
    caplog.set_level(logging.DEBUG)
    instance, kube, pub, fake = _world(tmp_path)
    rc = export_secrets(instance, kube, recipient_file=str(pub), sops_run=fake.run)
    out, err = capsys.readouterr()
    assert rc == EXIT_OK
    app = instance / "clusters/platform/secrets/proj-dev/app-config.yaml"
    pull = instance / "clusters/platform/secrets/proj-dev/ghcr-pull.yaml"
    skipped = instance / "clusters/platform/secrets/infisical/vault-bootstrap.yaml"
    assert app.is_file() and pull.is_file()
    assert not skipped.exists()
    assert not (instance / "clusters/platform/secrets/infisical").exists()
    app_doc = _decrypt_file(fake, app)
    pull_doc = _decrypt_file(fake, pull)
    app_t = app_doc["spec"]["secretTemplates"][0]
    pull_t = pull_doc["spec"]["secretTemplates"][0]
    assert app_t["name"] == "app-config"
    assert app_t["type"] == "Opaque"
    assert set(app_t["data"]) == {"TOKEN", "OTHER"}
    assert app_t["data"]["TOKEN"] == PLANT_TOKEN_B64
    assert pull_t["name"] == "ghcr-pull"
    assert pull_t["type"] == DOCKER_TYPE
    assert list(pull_t["data"]) == [DOCKER_KEY]
    assert pull_t["data"][DOCKER_KEY] == PLANT_DOCKER_B64
    assert "last-applied-configuration" not in yaml.safe_dump(app_doc)
    sops_yaml = yaml.safe_load((instance / ".sops.yaml").read_text())
    rule = sops_yaml["creation_rules"][0]
    assert rule["path_regex"] == PATH_REGEX
    assert rule["encrypted_regex"] == ENCRYPTED_REGEX
    assert rule["age"] == RECIPIENT
    lines = [ln for ln in out.splitlines() if ln.strip()]
    assert any(ln.startswith("proj-dev/app-config Opaque OTHER,TOKEN written") for ln in lines)
    assert any(ln.startswith(f"proj-dev/ghcr-pull {DOCKER_TYPE} {DOCKER_KEY} written") for ln in lines)
    assert "infisical" not in out
    assert_no_plants(out, err, caplog.text)


def test_plaintext_reaches_sops_only_on_stdin(tmp_path, capsys, caplog):
    caplog.set_level(logging.DEBUG)
    instance, kube, pub, fake = _world(tmp_path)
    rc = export_secrets(instance, kube, recipient_file=str(pub), sops_run=fake.run)
    assert rc == EXIT_OK
    encrypts = [c for c in fake.calls if "encrypt" in c["argv"]]
    assert encrypts
    saw_token = False
    for call in encrypts:
        argv_blob = " ".join(call["argv"])
        env_blob = "\n".join([*call["env"].keys(), *map(str, call["env"].values())])
        assert_no_plants(argv_blob, env_blob)
        if PLANT_TOKEN_B64 in call["stdin"] or PLANT_DOCKER_B64 in call["stdin"]:
            saw_token = True
        assert "--age" in call["argv"]
        assert "--filename-override" in call["argv"]
        assert call["argv"][0] == "sops"
        assert "encrypt" in call["argv"]
    assert saw_token
    out, err = capsys.readouterr()
    assert_no_plants(out, err, caplog.text, _joined_calls(fake))


def test_export_stdout_stderr_logs_have_no_planted_values(tmp_path, capsys, caplog):
    caplog.set_level(logging.DEBUG)
    instance, kube, pub, fake = _world(tmp_path)
    rc = export_secrets(instance, kube, recipient_file=str(pub), sops_run=fake.run)
    out, err = capsys.readouterr()
    assert rc == EXIT_OK
    assert_no_plants(out, err, caplog.text)
    for path in (instance / "clusters/platform/secrets").rglob("*.yaml"):
        assert_no_plants(path.read_text())


def test_reexport_unchanged_cluster_leaves_files_byte_identical(tmp_path, capsys):
    instance, kube, pub, fake = _world(tmp_path)
    assert export_secrets(instance, kube, recipient_file=str(pub), sops_run=fake.run) == EXIT_OK
    capsys.readouterr()
    before = {
        p: p.read_bytes()
        for p in sorted((instance / "clusters/platform/secrets").rglob("*.yaml"))
    }
    encrypts_before = sum(1 for c in fake.calls if "encrypt" in c["argv"])
    rc = export_secrets(instance, kube, recipient_file=str(pub), sops_run=fake.run)
    out, err = capsys.readouterr()
    assert rc == EXIT_OK
    after = {
        p: p.read_bytes()
        for p in sorted((instance / "clusters/platform/secrets").rglob("*.yaml"))
    }
    assert after == before
    encrypts_after = sum(1 for c in fake.calls if "encrypt" in c["argv"])
    assert encrypts_after == encrypts_before
    assert "unchanged" in out
    assert "written" not in out
    assert_no_plants(out, err)


def test_sops_yaml_not_rewritten_when_present(tmp_path):
    instance, kube, pub, fake = _world(tmp_path)
    existing = instance / ".sops.yaml"
    existing.write_text(f"creation_rules:\n- path_regex: already\n  age: {RECIPIENT}\n")
    original = existing.read_bytes()
    rc = export_secrets(instance, kube, recipient_file=str(pub), sops_run=fake.run)
    assert rc == EXIT_OK
    assert existing.read_bytes() == original


def test_export_recipient_disagreeing_with_sops_yaml_is_usage_and_changes_nothing(tmp_path, capsys):
    instance, kube, pub, fake = _world(tmp_path)
    existing = instance / ".sops.yaml"
    existing.write_text("creation_rules:\n- path_regex: already\n  age: age1alreadythere\n")
    original = existing.read_bytes()
    rc = export_secrets(instance, kube, recipient_file=str(pub), sops_run=fake.run)
    out, err = capsys.readouterr()
    assert rc == EXIT_USAGE
    assert pub.name in err and ".sops.yaml" in err
    assert existing.read_bytes() == original
    assert sorted(p for p in instance.rglob("*")) == [existing]
    assert fake.calls == []
    assert_no_plants(out, err)


def test_export_rewrites_file_when_value_changes_but_keys_do_not(tmp_path, capsys):
    instance, kube, pub, fake = _world(tmp_path)
    assert export_secrets(instance, kube, recipient_file=str(pub), sops_run=fake.run) == EXIT_OK
    capsys.readouterr()
    app = instance / "clusters/platform/secrets/proj-dev/app-config.yaml"
    pull = instance / "clusters/platform/secrets/proj-dev/ghcr-pull.yaml"
    app_before, pull_before = app.read_bytes(), pull.read_bytes()
    live_app = next(
        o for o in kube.objects
        if o.get("kind") == "Secret" and o["metadata"]["name"] == "app-config"
    )
    live_app["data"]["TOKEN"] = PLANT_CHANGED_B64
    rc = export_secrets(instance, kube, recipient_file=str(pub), sops_run=fake.run)
    out, err = capsys.readouterr()
    assert rc == EXIT_OK
    assert "proj-dev/app-config Opaque OTHER,TOKEN written" in out
    assert f"proj-dev/ghcr-pull {DOCKER_TYPE} {DOCKER_KEY} unchanged" in out
    assert app.read_bytes() != app_before
    assert pull.read_bytes() == pull_before
    assert _decrypt_file(fake, app)["spec"]["secretTemplates"][0]["data"]["TOKEN"] == PLANT_CHANGED_B64
    assert_no_plants(out, err)


def test_export_unexpected_error_prints_only_secret_name_and_type(tmp_path, capsys, caplog):
    caplog.set_level(logging.DEBUG)
    instance, kube, pub, _ = _world(tmp_path)

    def exploding_sops(argv, *, stdin="", env=None, cwd=None):
        raise RuntimeError(f"leaked {PLANT_TOKEN} {PLANT_TOKEN_B64} {PLANT_DOCKER_B64}")

    rc = export_secrets(instance, kube, recipient_file=str(pub), sops_run=exploding_sops)
    out, err = capsys.readouterr()
    assert rc == EXIT_DIFF
    assert err.splitlines() == [
        "orbit secrets export: proj-dev/app-config RuntimeError",
        "orbit secrets export: proj-dev/ghcr-pull RuntimeError",
    ]
    assert_no_plants(out, err, caplog.text)


def test_export_missing_recipient_is_usage(tmp_path, capsys):
    instance = tmp_path / "instance"
    instance.mkdir()
    rc = export_secrets(instance, FakeKube(), recipient_file=None, sops_run=FakeSops().run)
    err = capsys.readouterr().err
    assert rc == EXIT_USAGE
    assert "recipient" in err
    assert_no_plants(err)


def test_cmd_export_missing_instance_dir_is_usage(tmp_path, capsys):
    fake = FakeSops()
    rc = cmd_export(_args(tmp_path / "nope", FakeKube(), fake, tmp_path / "x"))
    assert rc == EXIT_USAGE
    assert "not a directory" in capsys.readouterr().err


# ------------------------------------------------------------------ verify


def test_verify_match_on_faithful_file(tmp_path, monkeypatch, capsys, caplog):
    caplog.set_level(logging.DEBUG)
    instance, kube, pub, fake = _world(tmp_path)
    _age_key(tmp_path, monkeypatch)
    assert export_secrets(instance, kube, recipient_file=str(pub), sops_run=fake.run) == EXIT_OK
    capsys.readouterr()
    rc = verify_secrets(instance, kube, sops_run=fake.run)
    out, err = capsys.readouterr()
    assert rc == EXIT_OK
    assert "proj-dev/app-config match" in out
    assert "proj-dev/ghcr-pull match" in out
    assert "mismatch" not in out
    assert "no-file" not in out
    assert_no_plants(out, err, caplog.text)


def test_verify_mismatch_value_missing_extra(tmp_path, monkeypatch, capsys, caplog):
    caplog.set_level(logging.DEBUG)
    instance, kube, pub, fake = _world(tmp_path)
    _age_key(tmp_path, monkeypatch)
    assert export_secrets(instance, kube, recipient_file=str(pub), sops_run=fake.run) == EXIT_OK
    capsys.readouterr()

    live_app = next(
        o for o in kube.objects
        if o.get("kind") == "Secret" and o["metadata"]["name"] == "app-config"
    )
    # value differs
    live_app["data"]["TOKEN"] = PLANT_CHANGED_B64
    rc = verify_secrets(instance, kube, sops_run=fake.run)
    out, err = capsys.readouterr()
    assert rc == EXIT_DIFF
    assert "proj-dev/app-config mismatch" in out
    assert "TOKEN: value differs" in out
    assert_no_plants(out, err, caplog.text)

    # restore token, drop OTHER (file has extra), add NEW (file missing)
    live_app["data"]["TOKEN"] = PLANT_TOKEN_B64
    del live_app["data"]["OTHER"]
    live_app["data"]["NEW"] = _b64("new")
    rc = verify_secrets(instance, kube, sops_run=fake.run)
    out, err = capsys.readouterr()
    assert rc == EXIT_DIFF
    assert "OTHER: extra" in out
    assert "NEW: missing" in out
    assert_no_plants(out, err, caplog.text)


def test_verify_mismatch_when_only_type_differs(tmp_path, monkeypatch, capsys):
    instance, kube, pub, fake = _world(tmp_path)
    _age_key(tmp_path, monkeypatch)
    assert export_secrets(instance, kube, recipient_file=str(pub), sops_run=fake.run) == EXIT_OK
    capsys.readouterr()
    live_pull = next(
        o for o in kube.objects
        if o.get("kind") == "Secret" and o["metadata"]["name"] == "ghcr-pull"
    )
    live_pull["type"] = "Opaque"
    rc = verify_secrets(instance, kube, sops_run=fake.run)
    out, err = capsys.readouterr()
    assert rc == EXIT_DIFF
    assert "proj-dev/ghcr-pull mismatch type differs" in out.splitlines()
    assert "proj-dev/app-config match" in out
    assert_no_plants(out, err)


def test_export_and_verify_leave_only_ciphertext_and_sops_yaml(tmp_path, monkeypatch, capsys):
    instance, kube, pub, fake = _world(tmp_path)
    _age_key(tmp_path, monkeypatch)
    assert export_secrets(instance, kube, recipient_file=str(pub), sops_run=fake.run) == EXIT_OK
    assert verify_secrets(instance, kube, sops_run=fake.run) == EXIT_OK
    capsys.readouterr()
    root = instance / "clusters/platform/secrets/proj-dev"
    files = sorted(p for p in instance.rglob("*") if not p.is_dir())
    assert files == sorted([instance / ".sops.yaml", root / "app-config.yaml", root / "ghcr-pull.yaml"])


def _rewrite_plain(fake: FakeSops, path: Path, mutate) -> None:
    doc = _decrypt_file(fake, path)
    mutate(doc)
    proc = fake.run(
        ["sops", "encrypt", "--age", RECIPIENT, "--filename-override", path.name],
        stdin=yaml.safe_dump(doc, sort_keys=False),
    )
    assert proc.returncode == 0
    path.write_text(proc.stdout)


def _extra_template(doc):
    doc["spec"]["secretTemplates"].append({"name": "app-config-2", "data": {"X": _b64("x")}})


def _string_data(doc):
    doc["spec"]["secretTemplates"][0]["stringData"] = {"S": "plain"}


def _spec_string_data(doc):
    doc["spec"]["stringData"] = {"S": "plain"}


def _other_name(doc):
    doc["metadata"]["name"] = "elsewhere"


def _other_template_name(doc):
    doc["spec"]["secretTemplates"][0]["name"] = "elsewhere"


def _other_namespace(doc):
    doc["metadata"]["namespace"] = "proj-prod"


def _all_as_string_data(doc):
    """The shape the onboarding documents' own sealing command writes: plain values."""
    import base64
    template = doc["spec"]["secretTemplates"][0]
    template["stringData"] = {k: base64.b64decode(v).decode() for k, v in template.pop("data").items()}


def _string_data_not_strings(doc):
    doc["spec"]["secretTemplates"][0]["stringData"] = {"S": 12}


def test_verify_reads_a_file_sealed_with_plain_values(tmp_path, monkeypatch, capsys):
    """QA of v1.0.0, F34: three files on the test box, sealed by the documented command
    with `stringData:`, read "mismatch unsupported shape" for good. Their content is
    compared like any other: equal is a match."""
    instance, kube, pub, fake = _world(tmp_path)
    _age_key(tmp_path, monkeypatch)
    assert export_secrets(instance, kube, recipient_file=str(pub), sops_run=fake.run) == EXIT_OK
    capsys.readouterr()
    app = instance / "clusters/platform/secrets/proj-dev/app-config.yaml"
    _rewrite_plain(fake, app, _all_as_string_data)
    rc = verify_secrets(instance, kube, sops_run=fake.run)
    out, err = capsys.readouterr()
    assert rc == EXIT_OK, out + err
    assert "proj-dev/app-config match" in out.splitlines()
    assert_no_plants(out, err)


def test_verify_compares_plain_values_too_and_names_only_the_key(tmp_path, monkeypatch, capsys):
    """A key that exists only as stringData, and one whose plain value differs from the
    live Secret, are differences like any other: named by key, never by value."""
    instance, kube, pub, fake = _world(tmp_path)
    _age_key(tmp_path, monkeypatch)
    assert export_secrets(instance, kube, recipient_file=str(pub), sops_run=fake.run) == EXIT_OK
    capsys.readouterr()
    app = instance / "clusters/platform/secrets/proj-dev/app-config.yaml"

    def changed(doc):
        _all_as_string_data(doc)
        doc["spec"]["secretTemplates"][0]["stringData"]["TOKEN"] = "another-value-entirely"
        doc["spec"]["secretTemplates"][0]["stringData"]["S"] = "plain"

    _rewrite_plain(fake, app, changed)
    rc = verify_secrets(instance, kube, sops_run=fake.run)
    out, err = capsys.readouterr()
    assert rc == EXIT_DIFF
    assert "proj-dev/app-config mismatch S: extra; TOKEN: value differs" in out.splitlines()
    assert "another-value-entirely" not in out + err
    assert_no_plants(out, err)


@pytest.mark.parametrize("mutate", [
    _extra_template, _string_data_not_strings, _spec_string_data,
    _other_name, _other_template_name, _other_namespace,
])
def test_verify_refuses_unsupported_shape(tmp_path, monkeypatch, capsys, mutate):
    instance, kube, pub, fake = _world(tmp_path)
    _age_key(tmp_path, monkeypatch)
    assert export_secrets(instance, kube, recipient_file=str(pub), sops_run=fake.run) == EXIT_OK
    capsys.readouterr()
    app = instance / "clusters/platform/secrets/proj-dev/app-config.yaml"
    _rewrite_plain(fake, app, mutate)
    # the other namespace/name exists live with identical content, so only the shape check can refuse
    kube.add_object(_secret("proj-prod", "app-config", data={"TOKEN": PLANT_TOKEN_B64, "OTHER": _b64("other")}))
    kube.add_object(_secret("proj-dev", "elsewhere", data={"TOKEN": PLANT_TOKEN_B64, "OTHER": _b64("other")}))
    rc = verify_secrets(instance, kube, sops_run=fake.run)
    out, err = capsys.readouterr()
    assert rc == EXIT_DIFF
    lines = out.splitlines()
    assert "proj-dev/app-config mismatch unsupported shape" in lines
    assert "proj-dev/ghcr-pull match" in lines
    assert not any("app-config match" in ln or "no-file" in ln for ln in lines)
    assert_no_plants(out, err)


def test_verify_reports_externalsecret_targets_with_no_file(tmp_path, monkeypatch, capsys):
    instance, kube, pub, fake = _world(tmp_path)
    _age_key(tmp_path, monkeypatch)
    assert export_secrets(instance, kube, recipient_file=str(pub), sops_run=fake.run) == EXIT_OK
    capsys.readouterr()
    kube.add_object(_es("proj-dev", "later"))
    kube.add_object(_secret("proj-dev", "later", data={"X": _b64("x")}))
    rc = verify_secrets(instance, kube, sops_run=fake.run)
    out, err = capsys.readouterr()
    assert rc == EXIT_DIFF
    assert "proj-dev/later no-file" in out
    assert "proj-dev/app-config match" in out
    assert_no_plants(out, err)


def test_verify_requires_age_key_file(tmp_path, monkeypatch, capsys):
    instance, kube, pub, fake = _world(tmp_path)
    assert export_secrets(instance, kube, recipient_file=str(pub), sops_run=fake.run) == EXIT_OK
    capsys.readouterr()
    monkeypatch.delenv("SOPS_AGE_KEY_FILE", raising=False)
    rc = verify_secrets(instance, kube, sops_run=fake.run)
    err = capsys.readouterr().err
    assert rc == EXIT_USAGE
    assert "SOPS_AGE_KEY_FILE" in err


def test_fake_sops_refuses_encrypt_without_filename_override_or_recipient():
    fake = FakeSops()
    missing_name = fake.run(["sops", "encrypt", "--age", RECIPIENT], stdin="a: 1\n")
    assert missing_name.returncode != 0
    missing_age = fake.run(
        ["sops", "encrypt", "--filename-override", "x.yaml"], stdin="a: 1\n",
    )
    assert missing_age.returncode != 0
    not_sops = fake.run(["not-sops", "encrypt"], stdin="a: 1\n")
    assert not_sops.returncode != 0


# ------------------------------------------------------------------ cli


@pytest.mark.anyio
async def test_orbit_cli_help_includes_secrets(capsys):
    from orbitd.client import async_main
    assert await async_main(["secrets", "--help"]) == 0
    assert await async_main(["secrets", "export", "--help"]) == 0
    assert await async_main(["secrets", "verify", "--help"]) == 0
    help_text = capsys.readouterr().out
    assert "--kubeconfig" in help_text
    assert "--recipient-file" in help_text


@pytest.mark.anyio
async def test_orbit_cli_dispatches_secrets_export(tmp_path, monkeypatch, capsys):
    from orbitd.client import async_main
    instance, kube, pub, fake = _world(tmp_path)
    monkeypatch.setattr("orbitd.secrets.Kube", lambda kubeconfig=None: kube)
    monkeypatch.setattr("orbitd.secrets.run_sops", fake.run)
    rc = await async_main([
        "secrets", "export", str(instance),
        "--kubeconfig", "/dev/null",
        "--recipient-file", str(pub),
    ])
    out, err = capsys.readouterr()
    assert rc == EXIT_OK
    assert (instance / "clusters/platform/secrets/proj-dev/app-config.yaml").is_file()
    assert "written" in out
    assert_no_plants(out, err)


def test_cmd_verify_via_args(tmp_path, monkeypatch, capsys):
    instance, kube, pub, fake = _world(tmp_path)
    _age_key(tmp_path, monkeypatch)
    assert cmd_export(_args(instance, kube, fake, pub)) == EXIT_OK
    capsys.readouterr()
    rc = cmd_verify(_args(instance, kube, fake))
    out, err = capsys.readouterr()
    assert rc == EXIT_OK
    assert "match" in out
    assert_no_plants(out, err)


def test_main_help_exits_0():
    assert main(["--help"]) == 0
    assert main(["export", "--help"]) == 0


# ------------------------------------------------------------------ real sops


@pytest.mark.skipif(not (SOPS and AGE_KEYGEN), reason="sops and age-keygen not on PATH")
def test_real_sops_round_trip(tmp_path, monkeypatch, capsys, caplog):
    caplog.set_level(logging.DEBUG)
    key = tmp_path / "throwaway.agekey"
    subprocess.run([AGE_KEYGEN, "-o", str(key)], check=True, capture_output=True, text=True)
    pub_text = subprocess.run(
        [AGE_KEYGEN, "-y", str(key)], check=True, capture_output=True, text=True,
    ).stdout.strip()
    pub = tmp_path / "throwaway.pub"
    pub.write_text(pub_text + "\n")
    monkeypatch.setenv("SOPS_AGE_KEY_FILE", str(key))
    instance = tmp_path / "instance"
    instance.mkdir()
    kube = FakeKube()
    kube.add_object(_es("proj-dev", "app-config"))
    kube.add_object(_secret("proj-dev", "app-config", data={"TOKEN": PLANT_TOKEN_B64}))
    rc = export_secrets(instance, kube, recipient_file=str(pub))
    out, err = capsys.readouterr()
    assert rc == EXIT_OK, err
    written = instance / "clusters/platform/secrets/proj-dev/app-config.yaml"
    assert written.is_file()
    ciphertext = written.read_text()
    assert "sops:" in ciphertext
    assert_no_plants(ciphertext, out, err, caplog.text)
    sealed = yaml.safe_load(ciphertext)
    assert sealed["apiVersion"] == "isindir.github.com/v1alpha3"
    assert sealed["kind"] == "SopsSecret"
    assert sealed["metadata"] == {"name": "app-config", "namespace": "proj-dev"}
    template = sealed["spec"]["secretTemplates"][0]
    assert template["name"] == "app-config"
    assert template["type"] == "Opaque"
    assert list(template["data"]) == ["TOKEN"]
    assert all(re.fullmatch(r"ENC\[AES256_GCM,.*\]", v) for v in template["data"].values())
    rc = verify_secrets(instance, kube)
    vout, verr = capsys.readouterr()
    assert rc == EXIT_OK, verr
    assert "proj-dev/app-config match" in vout
    assert_no_plants(vout, verr, caplog.text)
    files = sorted(p for p in instance.rglob("*") if not p.is_dir())
    assert files == sorted([instance / ".sops.yaml", written])
    live = next(o for o in kube.objects if o.get("kind") == "Secret")
    live["data"]["TOKEN"] = PLANT_CHANGED_B64
    rc = verify_secrets(instance, kube)
    mout, merr = capsys.readouterr()
    assert rc == EXIT_DIFF
    assert "TOKEN: value differs" in mout
    assert_no_plants(mout, merr, caplog.text)


RETYPED = {
    "SCI": "1e10", "OCT": "0o17", "BOOL": "true", "NUL": "null",
    "TILDE": "~", "HEX": "0x1F", "BIN": "0b10", "PLUS": "+123",
}


@pytest.mark.skipif(not (SOPS and AGE_KEYGEN), reason="sops and age-keygen not on PATH")
def test_real_sops_keeps_number_like_values_as_strings(tmp_path, monkeypatch, capsys):
    key = tmp_path / "throwaway.agekey"
    subprocess.run([AGE_KEYGEN, "-o", str(key)], check=True, capture_output=True, text=True)
    pub_text = subprocess.run(
        [AGE_KEYGEN, "-y", str(key)], check=True, capture_output=True, text=True,
    ).stdout.strip()
    pub = tmp_path / "throwaway.pub"
    pub.write_text(pub_text + "\n")
    monkeypatch.setenv("SOPS_AGE_KEY_FILE", str(key))
    instance = tmp_path / "instance"
    instance.mkdir()
    kube = FakeKube()
    kube.add_object(_es("proj-dev", "odd-values"))
    kube.add_object(_secret("proj-dev", "odd-values", data=dict(RETYPED)))
    assert export_secrets(instance, kube, recipient_file=str(pub)) == EXIT_OK
    capsys.readouterr()
    written = instance / "clusters/platform/secrets/proj-dev/odd-values.yaml"
    first = written.read_bytes()
    sealed = yaml.safe_load(first)["spec"]["secretTemplates"][0]["data"]
    assert set(sealed) == set(RETYPED)
    assert all(re.fullmatch(r"ENC\[AES256_GCM,.*,type:str\]", v) for v in sealed.values())
    proc = subprocess.run(
        [SOPS, "--config", os.devnull, "decrypt", "--input-type", "yaml", "--output-type", "yaml",
         "--filename-override", "clusters/platform/secrets/proj-dev/odd-values.yaml"],
        input=first.decode(), capture_output=True, text=True,
    )
    assert proc.returncode == 0, proc.stderr
    opened = yaml.safe_load(proc.stdout)["spec"]["secretTemplates"][0]["data"]
    assert opened == RETYPED
    assert all(type(v) is str for v in opened.values())
    rc = verify_secrets(instance, kube)
    out, err = capsys.readouterr()
    assert rc == EXIT_OK, err
    assert out.splitlines() == ["proj-dev/odd-values match"]
    assert export_secrets(instance, kube, recipient_file=str(pub)) == EXIT_OK
    out, _ = capsys.readouterr()
    assert "unchanged" in out and "written" not in out
    assert written.read_bytes() == first


def _other_kind(doc):
    doc["kind"] = "NotASopsSecret"


def _other_api_version(doc):
    doc["apiVersion"] = "example.invalid/v1"


@pytest.mark.parametrize("mutate", [_other_kind, _other_api_version])
def test_verify_refuses_a_document_that_is_not_a_sopssecret(tmp_path, monkeypatch, capsys, mutate):
    """Review r21b (Astra): a file decrypting to another kind must never report match."""
    instance, kube, pub, fake = _world(tmp_path)
    _age_key(tmp_path, monkeypatch)
    assert export_secrets(instance, kube, recipient_file=str(pub), sops_run=fake.run) == EXIT_OK
    capsys.readouterr()
    app = instance / "clusters/platform/secrets/proj-dev/app-config.yaml"
    _rewrite_plain(fake, app, mutate)
    rc = verify_secrets(instance, kube, sops_run=fake.run)
    out, err = capsys.readouterr()
    assert rc == EXIT_DIFF
    lines = out.splitlines()
    assert "proj-dev/app-config mismatch unsupported shape" in lines
    assert "proj-dev/ghcr-pull match" in lines
    assert_no_plants(out, err)


# ------------------------------------------------------------------ review r21b (opus): recipients

OTHER_RECIPIENT = "age1fakeSecondRecipientNotASecret00001"


def _sops_yaml(instance: Path, age: str) -> None:
    (instance / ".sops.yaml").write_text(yaml.safe_dump({"creation_rules": [{
        "path_regex": r"clusters/platform/secrets/.*\.yaml$",
        "encrypted_regex": ENCRYPTED_REGEX, "age": age}]}, sort_keys=False))


def _file_recipients(path: Path) -> list[str]:
    return sorted(r["recipient"] for r in (yaml.safe_load(path.read_text())["sops"]["age"]))


def _encrypt_ages(fake: FakeSops) -> set[str]:
    return {_flag(c["argv"], "--age") for c in fake.calls if "encrypt" in c["argv"]}


def test_export_keeps_every_sops_yaml_recipient(tmp_path, capsys):
    instance, kube, pub, fake = _world(tmp_path)
    _sops_yaml(instance, f"{RECIPIENT},{OTHER_RECIPIENT}")
    assert export_secrets(instance, kube, sops_run=fake.run) == EXIT_OK
    ages = _encrypt_ages(fake)
    assert ages, "export encrypted nothing"
    for age in ages:
        assert sorted(a.strip() for a in age.split(",")) == sorted([RECIPIENT, OTHER_RECIPIENT])


def test_export_rewrites_a_file_encrypted_to_another_recipient(tmp_path, capsys):
    instance, kube, pub, fake = _world(tmp_path)
    assert export_secrets(instance, kube, recipient_file=str(pub), sops_run=fake.run) == EXIT_OK
    app = instance / "clusters/platform/secrets/proj-dev/app-config.yaml"
    assert _file_recipients(app) == [RECIPIENT]
    capsys.readouterr()
    _sops_yaml(instance, OTHER_RECIPIENT)
    assert export_secrets(instance, kube, sops_run=fake.run) == EXIT_OK
    out, _ = capsys.readouterr()
    assert "proj-dev/app-config" in out and "written" in [ln for ln in out.splitlines() if "proj-dev/app-config" in ln][0]
    assert _file_recipients(app) == [OTHER_RECIPIENT]


def test_export_unchanged_needs_the_same_recipients(tmp_path, capsys):
    instance, kube, pub, fake = _world(tmp_path)
    assert export_secrets(instance, kube, recipient_file=str(pub), sops_run=fake.run) == EXIT_OK
    app = instance / "clusters/platform/secrets/proj-dev/app-config.yaml"
    before = app.read_bytes()
    capsys.readouterr()
    assert export_secrets(instance, kube, sops_run=fake.run) == EXIT_OK
    out, _ = capsys.readouterr()
    assert app.read_bytes() == before
    assert [ln for ln in out.splitlines() if "proj-dev/app-config" in ln][0].endswith("unchanged")


def _data_list(doc):
    doc["spec"]["secretTemplates"][0]["data"] = []


def _data_nested(doc):
    doc["spec"]["secretTemplates"][0]["data"]["TOKEN"] = {"nested": "x"}


def _data_number(doc):
    doc["spec"]["secretTemplates"][0]["data"]["TOKEN"] = 12


@pytest.mark.parametrize("mutate", [_data_list, _data_nested, _data_number])
def test_verify_refuses_template_data_that_is_not_a_string_mapping(tmp_path, monkeypatch, capsys, mutate):
    """Review r21b-2 (Astra): data must be a mapping of key to string, never normalised."""
    instance, kube, pub, fake = _world(tmp_path)
    _age_key(tmp_path, monkeypatch)
    assert export_secrets(instance, kube, recipient_file=str(pub), sops_run=fake.run) == EXIT_OK
    capsys.readouterr()
    app = instance / "clusters/platform/secrets/proj-dev/app-config.yaml"
    _rewrite_plain(fake, app, mutate)
    rc = verify_secrets(instance, kube, sops_run=fake.run)
    out, err = capsys.readouterr()
    assert rc == EXIT_DIFF
    assert "proj-dev/app-config mismatch unsupported shape" in out.splitlines()
    assert_no_plants(out, err)
