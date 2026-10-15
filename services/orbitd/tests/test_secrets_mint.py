"""orbit secrets mint: a new random value, sealed as SopsSecret files, one per namespace.

Used for credentials the platform makes for itself (the Alertmanager webhook bearer is
the first: the same token in `orbit` and `monitoring`). The value exists only in this
process and on sops's stdin; it never reaches argv, the environment, output or a log."""

from __future__ import annotations

import base64
import logging

import pytest
import yaml

import orbitd.secrets as secrets_mod
import test_secrets as ts
from orbitd.secrets import EXIT_OK, EXIT_USAGE, main, mint_secret

PLANT = "plantval_minted_ff66uu"
PLANT_B64 = base64.b64encode(PLANT.encode()).decode()
NAMESPACES = ["orbit", "monitoring"]


def world(tmp_path):
    instance = tmp_path / "instance"
    instance.mkdir()
    pub = tmp_path / "recipient.pub"
    pub.write_text(ts.RECIPIENT + "\n")
    return instance, pub, ts.FakeSops()


def path_of(instance, ns, name="orbit-alert-webhook"):
    return instance / "clusters/platform/secrets" / ns / f"{name}.yaml"


def opened(fake, path):
    proc = fake.run(["sops", "decrypt", "--filename-override", path.name], stdin=path.read_text())
    assert proc.returncode == 0
    return yaml.safe_load(proc.stdout)


def mint(instance, pub, fake, *, value=PLANT, rotate=False, namespaces=NAMESPACES,
         name="orbit-alert-webhook", key="token"):
    return mint_secret(instance, name=name, namespaces=list(namespaces), key=key,
                       recipient_file=str(pub), rotate=rotate, sops_run=fake.run,
                       value_factory=lambda: value)


def test_mint_writes_one_sopssecret_per_namespace_with_the_same_value(tmp_path, capsys):
    instance, pub, fake = world(tmp_path)
    assert mint(instance, pub, fake) == EXIT_OK
    for ns in NAMESPACES:
        doc = opened(fake, path_of(instance, ns))
        assert doc["apiVersion"] == "isindir.github.com/v1alpha3"
        assert doc["kind"] == "SopsSecret"
        assert doc["metadata"] == {"name": "orbit-alert-webhook", "namespace": ns}
        assert doc["spec"]["secretTemplates"] == [
            {"name": "orbit-alert-webhook", "type": "Opaque", "data": {"token": PLANT_B64}}]
    assert (instance / ".sops.yaml").is_file()
    out = capsys.readouterr().out
    assert "orbit/orbit-alert-webhook token minted" in out
    assert "monitoring/orbit-alert-webhook token minted" in out


def test_the_value_reaches_sops_only_on_stdin(tmp_path, capsys, caplog):
    caplog.set_level(logging.DEBUG)
    instance, pub, fake = world(tmp_path)
    assert mint(instance, pub, fake) == EXIT_OK
    encrypts = [c for c in fake.calls if "encrypt" in c["argv"]]
    assert len(encrypts) == len(NAMESPACES)
    for call in encrypts:
        assert PLANT not in " ".join(call["argv"])
        assert all(PLANT not in str(v) and PLANT_B64 not in str(v) for v in call["env"].values())
        assert PLANT_B64 in call["stdin"]
    out, err = capsys.readouterr()
    for text in (out, err, caplog.text):
        assert PLANT not in text and PLANT_B64 not in text
    for ns in NAMESPACES:
        raw = path_of(instance, ns).read_text()
        assert PLANT not in raw and PLANT_B64 not in raw


def test_when_every_file_exists_mint_changes_nothing(tmp_path):
    instance, pub, fake = world(tmp_path)
    assert mint(instance, pub, fake) == EXIT_OK
    before = {ns: path_of(instance, ns).read_text() for ns in NAMESPACES}
    fake.calls.clear()
    assert mint(instance, pub, fake, value="plantval_second_gg77tt") == EXIT_OK
    assert fake.calls == []
    assert {ns: path_of(instance, ns).read_text() for ns in NAMESPACES} == before


def test_a_partial_set_is_refused_without_rotate(tmp_path, capsys):
    instance, pub, fake = world(tmp_path)
    assert mint(instance, pub, fake) == EXIT_OK
    path_of(instance, "monitoring").unlink()
    before = path_of(instance, "orbit").read_text()
    fake.calls.clear()
    assert mint(instance, pub, fake, value="plantval_second_gg77tt") == EXIT_USAGE
    assert fake.calls == []
    assert path_of(instance, "orbit").read_text() == before
    assert not path_of(instance, "monitoring").exists()
    assert "--rotate" in capsys.readouterr().err


def test_rotate_writes_a_new_value_everywhere(tmp_path):
    instance, pub, fake = world(tmp_path)
    assert mint(instance, pub, fake) == EXIT_OK
    new = "plantval_rotated_hh88ss"
    assert mint(instance, pub, fake, value=new, rotate=True) == EXIT_OK
    want = base64.b64encode(new.encode()).decode()
    for ns in NAMESPACES:
        assert opened(fake, path_of(instance, ns))["spec"]["secretTemplates"][0]["data"] == {"token": want}


@pytest.mark.parametrize("kw", [
    {"name": "Bad_Name"}, {"name": "../x"}, {"namespaces": ["orbit", "../etc"]},
    {"namespaces": []}, {"key": "a/b"}, {"key": ""},
])
def test_bad_names_are_refused_and_nothing_is_written(tmp_path, kw):
    instance, pub, fake = world(tmp_path)
    assert mint(instance, pub, fake, **kw) == EXIT_USAGE
    assert fake.calls == []
    assert not (instance / "clusters").exists()


def test_the_default_value_is_long_and_random(tmp_path):
    instance, pub, fake = world(tmp_path)
    kwargs = dict(name="orbit-alert-webhook", namespaces=["orbit"], key="token",
                  recipient_file=str(pub), sops_run=fake.run)
    assert mint_secret(instance, **kwargs) == EXIT_OK
    first = opened(fake, path_of(instance, "orbit"))["spec"]["secretTemplates"][0]["data"]["token"]
    assert mint_secret(instance, rotate=True, **kwargs) == EXIT_OK
    second = opened(fake, path_of(instance, "orbit"))["spec"]["secretTemplates"][0]["data"]["token"]
    assert first != second
    assert len(base64.b64decode(first)) >= 32


def test_the_cli_mints(tmp_path, monkeypatch, capsys):
    instance, pub, fake = world(tmp_path)
    monkeypatch.setattr(secrets_mod, "run_sops", fake.run)
    rc = main(["mint", str(instance), "--name", "orbit-alert-webhook",
               "--namespace", "orbit", "--namespace", "monitoring",
               "--recipient-file", str(pub)])
    assert rc == EXIT_OK
    for ns in NAMESPACES:
        assert path_of(instance, ns).is_file()
    assert "token minted" in capsys.readouterr().out


# A crafted checkout must not redirect a write: any symlink between the instance's
# secrets folder and the file is refused before any namespace is written.

@pytest.mark.parametrize("link", ["clusters", "clusters/platform/secrets", "clusters/platform/secrets/monitoring"])
def test_a_symlink_on_the_path_is_refused_and_nothing_is_written(tmp_path, link):
    instance, pub, fake = world(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    target = instance / link
    target.parent.mkdir(parents=True, exist_ok=True)
    target.symlink_to(outside, target_is_directory=True)
    assert mint(instance, pub, fake) == EXIT_USAGE
    assert fake.calls == []
    assert list(outside.rglob("*")) == []
    assert not path_of(instance, "orbit").exists() or link != "clusters/platform/secrets/monitoring"


def test_a_symlinked_secret_file_is_refused_on_rotate(tmp_path):
    instance, pub, fake = world(tmp_path)
    assert mint(instance, pub, fake) == EXIT_OK
    outside = tmp_path / "outside.yaml"
    outside.write_text("keep\n")
    p = path_of(instance, "orbit")
    p.unlink()
    p.symlink_to(outside)
    fake.calls.clear()
    assert mint(instance, pub, fake, rotate=True) == EXIT_USAGE
    assert fake.calls == []
    assert outside.read_text() == "keep\n"
