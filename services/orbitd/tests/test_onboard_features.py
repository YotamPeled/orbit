"""The onboarding form chooses the platform's features (docs/V1.md M3): none by default,
object storage and Telegram only when a chosen feature needs them. Every case here goes
the whole offline way an onboarding goes: core's template copied, the form's answers
written into the config by `fill_config`, the node's address written, and `make init`
run on the result. What the scaffolder renders is what the form asked for."""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

from orbitd import checks, onboard_run
from orbitd import orbit_features as rules
from orbitd.onboard_run import FormError, check_features, fill_config, load_form

REPO = Path(__file__).resolve().parents[3]
REGISTRY = rules.load_registry(REPO / "template")
OPTIONAL = [name for name, spec in REGISTRY.items() if not spec["always"]]
TOKEN = "SECRET-TOKEN-0123456789abcdef"
BASE = {
    "domain": "acme.example",
    "github": {"owner": "acme-org"},
    "hetzner": {"token": "HCLOUD-" + TOKEN, "server_type": "cx43", "location": "fsn1", "server_id": 12345, "rebuild": True},
    "cloudflare": {"token": "CF-" + TOKEN, "zone": "acme.example"},
    "acme_email": "ops@acme.example",
}
BUCKET = {"object_storage": {"access_key": "S3KEY-" + TOKEN, "secret_key": "S3SECRET-" + TOKEN, "location": "nbg1"}}
TELEGRAM = {"telegram": {"bot_token": "123456:" + TOKEN, "chat_id": "987654321"}}
PROJECT = {
    "id": "demo", "name": "Demo", "blurb": "A fixture.", "repo": "fixture-org/demo", "hosted": "self",
    "database": {"size": "1Gi", "memory": "256Mi"},
    "environments": {"prod": {"branch": "main"}},
    "components": [{"name": "web", "kind": "web", "class": "interactive", "image": "ghcr.io/fixture-org/demo-web",
                    "port": 8080, "path": "/", "memory": "256Mi", "probePath": "/healthz"}],
}


def form_file(tmp_path: Path, doc: dict) -> Path:
    p = tmp_path / "form.yaml"
    p.write_text(yaml.safe_dump(doc))
    p.chmod(0o600)
    return p


def onboarded(tmp_path: Path, doc: dict, *, expect_init: bool = True):
    """(instance dir, form, `make init`'s result) for a form, the offline way."""
    dest = tmp_path / "inst"
    shutil.copytree(REPO / "template", dest)
    for src, dst in checks.vendored_paths(REPO):
        source, target = REPO / src, dest / dst
        target.parent.mkdir(parents=True, exist_ok=True)
        if source.is_dir():
            shutil.copytree(source, target, dirs_exist_ok=True, ignore=shutil.ignore_patterns("__pycache__"))
        else:
            shutil.copy2(source, target)
    shutil.copy(dest / "instance" / "config.yaml.example", dest / "instance" / "config.yaml")
    form = load_form(form_file(tmp_path, doc))
    fill_config(dest, form, rules.load_registry(dest))
    onboard_run.write_ip(dest, "198.51.100.10")
    proc = subprocess.run([sys.executable, "scripts/instance-init.py"], cwd=dest, capture_output=True, text=True)
    if expect_init:
        assert proc.returncode == 0, proc.stdout + proc.stderr
    return dest, form, proc


def rendered(dest: Path, rel: str) -> Path:
    return dest / "clusters" / rel


def feature_files(name: str) -> list[str]:
    return [p for p in REGISTRY[name]["paths"]]


# ---------------------------------------------------------------- the smallest platform

def test_a_form_with_no_features_no_bucket_and_no_telegram_onboards_core_alone(tmp_path):
    dest, form, _ = onboarded(tmp_path, BASE)
    config = yaml.safe_load((dest / "instance" / "config.yaml").read_text())
    assert config["features"] == {name: False for name in OPTIONAL}
    assert "objectStorage" not in config, "a platform with no bucket has no objectStorage block"
    assert config["contacts"]["telegram"] is False and "telegramChatId" not in config["contacts"]
    for name in OPTIONAL:
        for rel in feature_files(name):
            assert not rendered(dest, rel).exists(), f"{rel} is rendered although {name} is off"
    for rel in REGISTRY["core"]["paths"]:
        if rel == "platform/config/orbit-intake":
            continue                # the agent plane's door: rendered only once it is wired (intakeIngress)
        assert rendered(dest, rel).exists(), f"core's {rel} is missing"
    stack = rendered(dest, "platform/apps/kube-prometheus-stack.yaml").read_text()
    assert "telegram_configs" not in stack and "enabled: false" in stack
    assert TOKEN not in (dest / "instance" / "config.yaml").read_text()


def test_the_config_keeps_its_comments_and_a_second_run_writes_nothing(tmp_path):
    dest, form, _ = onboarded(tmp_path, BASE)
    text = (dest / "instance" / "config.yaml").read_text()
    assert "# short id; prefixes global names" in text
    assert fill_config(dest, form, rules.load_registry(dest)) == []
    assert (dest / "instance" / "config.yaml").read_text() == text


# ---------------------------------------------------------------- each feature, on and off

@pytest.mark.parametrize("feature", OPTIONAL)
def test_each_feature_on_renders_its_files_and_off_renders_none(tmp_path, feature):
    needs = {feature}
    grew = True
    while grew:                                   # a feature comes with what it requires
        grew = False
        for name in list(needs):
            for req in REGISTRY[name]["requires"]:
                if req != "bucket" and req not in needs:
                    needs.add(req)
                    grew = True
    bucket = any("bucket" in REGISTRY[name]["requires"] for name in needs)
    doc = {**BASE, **(BUCKET if bucket else {}), **(TELEGRAM if "alerts-telegram" in needs else {}),
           "features": {name: True for name in needs}}
    dest, form, _ = onboarded(tmp_path, doc)
    config = yaml.safe_load((dest / "instance" / "config.yaml").read_text())
    assert config["features"] == {name: name in needs for name in OPTIONAL}
    assert ("objectStorage" in config) == bucket
    for name in OPTIONAL:
        for rel in feature_files(name):
            assert rendered(dest, rel).exists() == (name in needs), (
                f"{rel}: {name} is {'on' if name in needs else 'off'} in the form")
    stack = rendered(dest, "platform/apps/kube-prometheus-stack.yaml").read_text()
    assert ("telegram_configs" in stack) == ("alerts-telegram" in needs)
    if "alerts-telegram" in needs:
        assert "chat_id: 987654321" in stack


def test_everything_on_is_the_whole_platform(tmp_path):
    doc = {**BASE, **BUCKET, **TELEGRAM, "features": {name: True for name in OPTIONAL}}
    dest, _, _ = onboarded(tmp_path, doc)
    for name in OPTIONAL:
        for rel in feature_files(name):
            assert rendered(dest, rel).exists(), rel
    config = yaml.safe_load((dest / "instance" / "config.yaml").read_text())
    assert config["objectStorage"]["buckets"]["platform"] == "acme-orbit-platform"
    assert config["objectStorage"]["endpoint"] == "https://nbg1.your-objectstorage.com"


# ---------------------------------------------------------------- refused, naming the field

@pytest.mark.parametrize("feature", [name for name in OPTIONAL if "bucket" in REGISTRY[name]["requires"]])
def test_a_feature_that_needs_a_bucket_without_object_storage_is_refused_naming_the_field(tmp_path, feature):
    on = {feature: True, **{req: True for req in REGISTRY[feature]["requires"] if req != "bucket"}}
    form = load_form(form_file(tmp_path, {**BASE, "features": on}))
    with pytest.raises(FormError) as e:
        check_features(form, REGISTRY)
    assert f"`features.{feature}`" in str(e.value) and "`object_storage`" in str(e.value)
    assert TOKEN not in str(e.value)


def test_telegram_alerts_without_telegram_is_refused_naming_the_field(tmp_path):
    form = load_form(form_file(tmp_path, {**BASE, "features": {"alerts-telegram": True}}))
    with pytest.raises(FormError, match="`features.alerts-telegram`.*`telegram`"):
        check_features(form, REGISTRY)


def test_backups_without_databases_and_unknown_features_are_refused(tmp_path):
    form = load_form(form_file(tmp_path, {**BASE, **BUCKET, "features": {"backups": True}}))
    with pytest.raises(FormError, match="databases"):
        check_features(form, REGISTRY)
    form = load_form(form_file(tmp_path, {**BASE, "features": {"teleportation": True}}))
    with pytest.raises(FormError, match="`features.teleportation` is not a feature of this core"):
        check_features(form, REGISTRY)
    for bad in ({"logs": "yes"}, ["logs"], {"Not A Name " + TOKEN: True}):
        with pytest.raises(FormError) as e:
            load_form(form_file(tmp_path, {**BASE, **BUCKET, "features": bad}))
        assert TOKEN not in str(e.value)


def test_a_half_filled_block_is_still_a_bad_form(tmp_path):
    doc = {**BASE, "object_storage": {"access_key": "S3KEY-" + TOKEN, "location": "nbg1"}}
    with pytest.raises(FormError, match="object_storage.secret_key"):
        load_form(form_file(tmp_path, doc))
    with pytest.raises(FormError, match="telegram.chat_id"):
        load_form(form_file(tmp_path, {**BASE, "telegram": {"bot_token": "123456:" + TOKEN}}))


def test_the_run_refuses_a_form_whose_features_cannot_be_before_its_first_step(tmp_path):
    dest = tmp_path / "inst"
    shutil.copytree(REPO / "template", dest)
    shutil.copy(dest / "instance" / "config.yaml.example", dest / "instance" / "config.yaml")
    before = (dest / "instance" / "config.yaml").read_text()
    lines: list[str] = []
    with pytest.raises(FormError, match="`object_storage`"):
        onboard_run.run(dest, form_file(tmp_path, {**BASE, "features": {"logs": True}}),
                        config_dir=tmp_path / "creds", out=lines.append)
    assert lines == [], "no step ran"
    assert (dest / "instance" / "config.yaml").read_text() == before
    assert not (tmp_path / "creds").exists()


# ---------------------------------------------------------------- the bucket-less path

def test_without_a_bucket_a_database_is_backed_up_by_the_nightly_dump(tmp_path):
    """databases on, no bucket: the project's database renders with the chart's pg_dump
    CronJob and its volume, and no bucket archive."""
    if not shutil.which("helm"):
        pytest.skip("needs helm")
    dest, _, _ = onboarded(tmp_path, {**BASE, "features": {"databases": True}})
    (dest / "catalog" / "projects.yaml").write_text(yaml.safe_dump({"version": 3, "projects": [PROJECT]}, sort_keys=False))
    (dest / "catalog" / "tags.yaml").write_text(yaml.safe_dump({"tags": {"demo/prod/web": "main-abc1234"}}))
    sync = subprocess.run([sys.executable, "scripts/catalog-sync.py"], cwd=dest, capture_output=True, text=True)
    assert sync.returncode == 0, sync.stdout + sync.stderr
    app = next(d for d in yaml.safe_load_all((dest / "clusters/apps/demo-prod.yaml").read_text())
               if isinstance(d, dict) and d.get("kind") == "Application")
    values = app["spec"]["source"]["helm"]["valuesObject"]
    assert "s3" not in values and values["database"]
    (tmp_path / "values.yaml").write_text(yaml.safe_dump(values))
    out = subprocess.run(["helm", "template", "demo", str(REPO / "charts" / "service"), "-f", str(tmp_path / "values.yaml")],
                         capture_output=True, text=True)
    assert out.returncode == 0, out.stderr
    kinds = [(d["kind"], d["metadata"]["name"]) for d in yaml.safe_load_all(out.stdout) if d]
    assert ("CronJob", "demo-db-backup") in kinds and ("PersistentVolumeClaim", "demo-db-backup") in kinds
    assert not [k for k in kinds if k[0] in ("ObjectStore", "ScheduledBackup")]
    assert "pg_dump" in out.stdout


def test_without_the_databases_feature_a_project_database_is_refused(tmp_path):
    dest, _, _ = onboarded(tmp_path, BASE)
    (dest / "catalog" / "projects.yaml").write_text(yaml.safe_dump({"version": 3, "projects": [PROJECT]}, sort_keys=False))
    sync = subprocess.run([sys.executable, "scripts/catalog-sync.py"], cwd=dest, capture_output=True, text=True)
    assert sync.returncode != 0
    assert "declares a database and the databases feature is off" in sync.stdout + sync.stderr


# ---------------------------------------------------------------- the sealed set follows the form

def test_the_secrets_sealed_are_the_ones_the_chosen_features_need(tmp_path, monkeypatch):
    sealed: list[str] = []
    monkeypatch.setattr(onboard_run, "seal", lambda inst, ns, name, data, **kw: sealed.append(f"{ns}/{name}") or True)
    core = {"cert-manager/cloudflare-api-token", "monitoring/orbit-alert-webhook", "monitoring/alertmanager-intake",
            "orbit/orbit-alert-webhook", "orbit/ghcr-pull", "orbit/dev-basic-auth", "orbit/orbitd-registry"}
    cases = [
        (BASE, core),
        ({**BASE, "features": {"dashboards": True}}, core | {"monitoring/grafana-admin", "monitoring/grafana-image-renderer-token"}),
        ({**BASE, **BUCKET, "features": {"logs": True}}, core | {"loki/orbit-s3", "orbit/orbit-s3"}),
        ({**BASE, **BUCKET, "features": {"traces": True}}, core | {"tempo/orbit-s3", "orbit/orbit-s3"}),
        ({**BASE, **TELEGRAM, "features": {"alerts-telegram": True}}, core | {"monitoring/alertmanager-telegram"}),
        ({**BASE, **TELEGRAM}, core),            # Telegram given, feature off: nothing of it is sealed
    ]
    for i, (doc, want) in enumerate(cases):
        sealed.clear()
        creds = tmp_path / f"creds{i}"
        creds.mkdir()
        form = load_form(form_file(tmp_path, doc))
        onboard_run.seal_platform_secrets(tmp_path, creds, form, ghcr_token="ghp_" + TOKEN, app=None)
        assert set(sealed) == want, (doc.get("features"), sorted(set(sealed) ^ want))
        kept = {p.name for p in creds.iterdir()}
        assert ("grafana-admin-password" in kept) == bool(form.wants("dashboards")), "a password is made only for a feature that is on"
        kit = onboard_run.write_recovery_kit(creds, form).read_text()
        assert ("Grafana admin password" in kit) == bool(form.wants("dashboards"))


def test_every_secret_a_feature_names_is_sealed_when_the_feature_is_on(tmp_path, monkeypatch):
    """The registry's `secrets` and the onboarding plan cannot drift apart."""
    sealed: list[str] = []
    monkeypatch.setattr(onboard_run, "seal", lambda inst, ns, name, data, **kw: sealed.append(f"{ns}/{name}") or True)
    form = load_form(form_file(tmp_path, {**BASE, **BUCKET, **TELEGRAM, "features": {name: True for name in OPTIONAL}}))
    (tmp_path / "creds").mkdir()
    onboard_run.seal_platform_secrets(tmp_path, tmp_path / "creds", form, ghcr_token="ghp_" + TOKEN, app=None)
    for name in OPTIONAL:
        for secret in REGISTRY[name]["secrets"]:
            assert secret in sealed, f"{name} needs {secret} and onboarding does not seal it"
