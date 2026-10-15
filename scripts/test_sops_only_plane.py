#!/usr/bin/env python3
"""
Phase 6 decision 6 (DESIGN-v2 §8): sops is the only secret plane. Infisical, its data plane
and the External Secrets Operator leave core the way ArgoCD did.

- CoreShipsNoneTest: the template, the service chart and the two generators carry no live
  line that names Infisical, ESO, an ExternalSecret or a ClusterSecretStore, and the
  retired sources are gone.
- ProviderTest: `secrets.provider: sops` (or no provider) initializes; the two Infisical
  values are refused by name, and nothing is written.
- RenderedTreeTest: a fresh init renders no ExternalSecret and no ClusterSecretStore.
- ExistingInstanceTest: an instance that rendered the retired files loses them on its next
  init, the removals are reported by path, and a hand-written file beside them survives.
- ChartTest: a service whose Secret carries no `from:` renders no ExternalSecret.

Self-contained on purpose: the init fixture below does not borrow test_instance_init.py's,
which this change rewrites. Run as `python3 scripts/test_sops_only_plane.py` or under pytest.
"""
import json
import pathlib
import re
import shutil
import subprocess
import sys
import tempfile
import unittest

import yaml

HERE = pathlib.Path(__file__).resolve().parent
ROOT = HERE.parent
SCRIPT = HERE / "instance-init.py"
TEMPLATE = ROOT / "template"
CHART = ROOT / "charts" / "service"

RETIRED = re.compile(r"infisical|external-?secrets?|clustersecretstore|secretstoreref", re.I)

RETIRED_CORE_PATHS = (
    "template/provisioning/clusters/platform/apps/infisical.yaml",
    "template/provisioning/clusters/platform/apps/infisical-data.yaml",
    "template/provisioning/clusters/platform/apps/external-secrets.yaml",
    "template/provisioning/clusters/platform/config/infisical",
    "template/provisioning/clusters/platform/config/external-secrets",
    "template/provisioning/clusters/platform/config/orbitd/externalsecret.yaml",
    "template/provisioning/clusters/platform/config/loki/s3-credentials.yaml",
    "template/provisioning/clusters/platform/config/tempo/s3-credentials.yaml",
    "template/clusters/platform/config/external-secrets",
    "charts/service/templates/externalsecrets.yaml",
)

# What an Infisical-era instance rendered or generated under clusters/.
RETIRED_RENDERS = (
    "platform/apps/infisical.yaml",
    "platform/apps/infisical-data.yaml",
    "platform/apps/external-secrets.yaml",
    "platform/config/infisical/db-and-cache.yaml",
    "platform/config/infisical/netpol.yaml",
    "platform/config/infisical/token-reviewer.yaml",
    "platform/config/external-secrets/stores.yaml",
    "platform/config/external-secrets/sentinel.yaml",
    "platform/config/external-secrets/eso-identity.yaml",
    "platform/config/external-secrets/dns-credentials.yaml",
    "platform/config/external-secrets/netpol.yaml",
    "platform/config/orbitd/externalsecret.yaml",
    "platform/config/loki/s3-credentials.yaml",
    "platform/config/tempo/s3-credentials.yaml",
)

CONFIG = {
    "instance": {"name": "fixtureinst"},
    "git": {"repoUrl": "git@github.com:fixture-org/fixture-instance.git",
            "branch": "main", "github_org": "fixture-org"},
    "domains": {"primary": "fixture.org", "dev": "dev.fixture.org",
                "dnsZones": ["fixture.org"]},
    "registry": "ghcr.io/fixture-org",
    "coreRegistry": {"owner": "ghcr.io/fixture-core", "orbitdTag": "main-1111111"},
    "nodes": [{"name": "fixture-node-1", "ip": "198.51.100.10", "role": "control-plane"}],
    "secrets": {"provider": "sops", "environments": ["dev", "prod"]},
    "objectStorage": {"provider": "hetzner", "bucket": "fixture-bucket",
                      "endpoint": "https://fsn1.your-objectstorage.com", "region": "fsn1"},
    "contacts": {"telegram": True, "telegramChatId": "1", "email": "ops@fixture.org",
                 "adminFirstName": "Fixture", "adminLastName": "Operator"},
}


def _live_lines(text: str):
    """Lines that are not YAML/shell/Helm comments: a comment may record history."""
    in_helm_comment = False
    for n, line in enumerate(text.splitlines(), 1):
        s = line.strip()
        if in_helm_comment:
            if "*/" in s:
                in_helm_comment = False
            continue
        if s.startswith("{{- /*") or s.startswith("{{/*"):
            in_helm_comment = "*/" not in s
            continue
        if s and not s.startswith("#"):
            yield n, line


def _hits(text: str):
    return [(n, l.strip()) for n, l in _live_lines(text) if RETIRED.search(l)]


class Fixture:
    def __init__(self, case: unittest.TestCase, config: dict):
        tmp = tempfile.mkdtemp()
        case.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        self.root = pathlib.Path(tmp)
        (self.root / "scripts").mkdir()
        shutil.copy(SCRIPT, self.root / "scripts" / SCRIPT.name)
        shutil.copytree(TEMPLATE / "provisioning", self.root / "provisioning")
        (self.root / "instance").mkdir()
        (self.root / "instance" / "config.yaml").write_text(yaml.safe_dump(config))
        (self.root / "clusters").mkdir()

    def run(self):
        return subprocess.run([sys.executable, "scripts/" + SCRIPT.name], cwd=self.root,
                              capture_output=True, text=True)

    def rendered(self, rel: str) -> pathlib.Path:
        return self.root / "clusters" / rel

    def files(self):
        return [f for f in sorted((self.root / "clusters").rglob("*")) if f.is_file()]


def _config(**secrets) -> dict:
    cfg = json.loads(json.dumps(CONFIG))
    if secrets.get("drop"):
        cfg["secrets"].pop("provider")
    elif "provider" in secrets:
        cfg["secrets"]["provider"] = secrets["provider"]
    return cfg


class CoreShipsNoneTest(unittest.TestCase):
    def test_no_retired_source_is_left(self):
        present = [p for p in RETIRED_CORE_PATHS if (ROOT / p).exists()]
        self.assertEqual(present, [], "retired secret-plane sources still in core")

    def test_no_live_line_in_the_template_chart_or_generators_names_the_old_plane(self):
        files = [p for p in TEMPLATE.rglob("*") if p.is_file()]
        files += [p for p in CHART.rglob("*") if p.is_file()]
        files += [HERE / "catalog-sync.py", HERE / "instance-init.py"]
        hits = {}
        for f in files:
            try:
                found = _hits(f.read_text())
            except UnicodeDecodeError:
                continue
            if found:
                hits[str(f.relative_to(ROOT))] = found[:3]
        self.assertEqual(hits, {}, "live lines still name Infisical or ESO")


class ProviderTest(unittest.TestCase):
    def test_sops_initializes(self):
        fx = Fixture(self, _config())
        proc = fx.run()
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)

    def test_no_provider_initializes_as_sops(self):
        fx = Fixture(self, _config(drop=True))
        proc = fx.run()
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)

    def test_the_infisical_values_are_refused_by_name_and_nothing_is_written(self):
        for value in ("infisical", "infisical-cloud", "vault"):
            with self.subTest(value=value):
                fx = Fixture(self, _config(provider=value))
                proc = fx.run()
                out = proc.stdout + proc.stderr
                self.assertNotEqual(proc.returncode, 0, f"{value!r} was accepted")
                self.assertIn("secrets.provider", out)
                self.assertIn("sops", out, "the refusal must name the one plane left")
                self.assertEqual(fx.files(), [], f"{value!r} was refused but init wrote files")


class RenderedTreeTest(unittest.TestCase):
    def test_a_fresh_init_renders_no_old_plane_object(self):
        fx = Fixture(self, _config())
        proc = fx.run()
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        kinds, hits = {}, {}
        for f in fx.files():
            text = f.read_text()
            found = _hits(text)
            if found:
                hits[str(f.relative_to(fx.root))] = found[:3]
            if f.suffix in (".yaml", ".yml"):
                try:
                    docs = [d for d in yaml.safe_load_all(text) if isinstance(d, dict)]
                except yaml.YAMLError:
                    continue
                for d in docs:
                    if d.get("kind") in ("ExternalSecret", "ClusterSecretStore", "SecretStore"):
                        kinds.setdefault(str(f.relative_to(fx.root)), []).append(d["kind"])
        self.assertEqual(kinds, {}, "init rendered an ESO object")
        self.assertEqual(hits, {}, "init rendered a live line naming Infisical or ESO")

    def test_the_secrets_app_still_renders(self):
        fx = Fixture(self, _config())
        self.assertEqual(fx.run().returncode, 0)
        self.assertTrue(fx.rendered("platform/apps/secrets.yaml").exists(),
                        "the sops secrets app is the plane now and must render")


class ExistingInstanceTest(unittest.TestCase):
    def test_the_next_init_removes_what_the_old_plane_rendered(self):
        fx = Fixture(self, _config())
        for rel in RETIRED_RENDERS:
            p = fx.rendered(rel)
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text("apiVersion: v1\nkind: ConfigMap\nmetadata: {name: left-over}\n")
        hand = fx.rendered("platform/config/team-notes/keep.yaml")
        hand.parent.mkdir(parents=True, exist_ok=True)
        hand.write_text("apiVersion: v1\nkind: ConfigMap\nmetadata: {name: keep}\n")
        proc = fx.run()
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        left = [rel for rel in RETIRED_RENDERS if fx.rendered(rel).exists()]
        self.assertEqual(left, [], "init kept files the old plane rendered")
        for d in ("infisical", "external-secrets"):
            self.assertFalse(fx.rendered(f"platform/config/{d}").exists(),
                             f"empty platform/config/{d} left behind")
        self.assertTrue(hand.exists(), "init deleted a hand-written file")
        self.assertIn("external-secrets/stores.yaml", proc.stdout + proc.stderr,
                      "a removal must be reported by path")

    def test_a_hand_written_file_in_a_retired_directory_survives(self):
        fx = Fixture(self, _config())
        mine = fx.rendered("platform/config/external-secrets/mine.yaml")
        mine.parent.mkdir(parents=True, exist_ok=True)
        mine.write_text("apiVersion: v1\nkind: ConfigMap\nmetadata: {name: mine}\n")
        proc = fx.run()
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertTrue(mine.exists(), "init deleted a file it never rendered")


@unittest.skipUnless(shutil.which("helm"), "helm not installed")
class ChartTest(unittest.TestCase):
    def _render(self, secrets: list) -> list:
        values = {"project": "fixture", "env": "dev", "secrets": secrets}
        with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False) as fh:
            yaml.safe_dump(values, fh)
        self.addCleanup(pathlib.Path(fh.name).unlink)
        proc = subprocess.run(["helm", "template", "t", str(CHART), "-f", fh.name],
                              capture_output=True, text=True)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        return [d for d in yaml.safe_load_all(proc.stdout) if isinstance(d, dict)]

    def test_a_secret_without_from_renders_no_external_secret(self):
        docs = self._render([{"name": "app-creds", "keys": ["TOKEN"]}])
        self.assertEqual([d for d in docs if d.get("kind") == "ExternalSecret"], [])

    def test_from_infisical_is_no_longer_a_value(self):
        schema = json.loads((CHART / "values.schema.json").read_text())
        self.assertNotIn("infisical", json.dumps(schema).lower())


if __name__ == "__main__":
    unittest.main()
