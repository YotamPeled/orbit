#!/usr/bin/env python3
"""
Phase 8c (DESIGN-v2 §8): Zitadel is retired. Grafana signs in with its local admin, whose
password is the sops-sealed Secret grafana-admin; nothing in core provisions an identity
provider, an OIDC client or the `auth:` block that carried its ids.

- CoreShipsNoneTest: no Zitadel sources, no Grafana OIDC, no live line naming either.
- GrafanaAdminTest: Grafana's admin credentials come from Secret grafana-admin.
- AuthBlockTest: a config without `auth:` initializes; one that still carries it is refused
  by name before anything is written.
- ExistingInstanceTest: an instance that rendered Zitadel loses those files on its next init.

Self-contained fixture, as in test_sops_only_plane.py. Run as
`python3 scripts/test_zitadel_retired.py` or under pytest.
"""
import json
import pathlib
import re
import shutil
import subprocess
import sys
import tempfile
import unittest

import sys
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
_B, sys.dont_write_bytecode = sys.dont_write_bytecode, True
from test_instance_init import BASE_CONFIG, InitFixture  # noqa: E402
sys.dont_write_bytecode = _B

import yaml

HERE = pathlib.Path(__file__).resolve().parent
ROOT = HERE.parent
SCRIPT = HERE / "instance-init.py"
TEMPLATE = ROOT / "template"
KPS = TEMPLATE / "provisioning" / "clusters" / "platform" / "apps" / "kube-prometheus-stack.yaml"

RETIRED = re.compile(r"zitadel|generic_oauth|grafana-oidc|oidc|mcpProjectId|grafanaClientId", re.I)

RETIRED_CORE_PATHS = (
    "template/provisioning/clusters/platform/apps/zitadel.yaml",
    "template/provisioning/clusters/platform/config/zitadel",
    "template/provisioning/clusters/platform/config/monitoring/grafana-oidc.yaml",
    "template/clusters/platform/config/zitadel",
)

RETIRED_RENDERS = (
    "platform/apps/zitadel.yaml",
    "platform/config/zitadel/backup.yaml",
    "platform/config/zitadel/db-and-secrets.yaml",
    "platform/config/monitoring/grafana-oidc.yaml",
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

AUTH = {"issuer": "https://auth.fixture.org", "projectId": "111111111111111111",
        "mcpProjectId": "222222222222222222", "grafanaClientId": "333333333333333333@fixture",
        "argocdClientId": "444444444444444444@fixture", "jwks": "https://auth.fixture.org/oauth/v2/keys"}


def _live_lines(text: str):
    for n, line in enumerate(text.splitlines(), 1):
        s = line.strip()
        if s and not s.startswith("#"):
            yield n, line


def _hits(text: str):
    return [(n, l.strip()[:120]) for n, l in _live_lines(text) if RETIRED.search(l)]


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


def _config(**extra) -> dict:
    cfg = json.loads(json.dumps(CONFIG))
    cfg.update(extra)
    return cfg


class CoreShipsNoneTest(unittest.TestCase):
    def test_no_zitadel_source_is_left(self):
        present = [p for p in RETIRED_CORE_PATHS if (ROOT / p).exists()]
        self.assertEqual(present, [], "Zitadel sources still in core")

    def test_no_live_line_in_the_template_or_generators_names_it(self):
        files = [p for p in TEMPLATE.rglob("*") if p.is_file()]
        files += [HERE / "catalog-sync.py", HERE / "instance-init.py"]
        files += sorted((HERE / "verify").glob("*.sh"))
        files += [p for p in (ROOT / ".claude" / "skills").rglob("*") if p.is_file()]
        hits = {}
        for f in files:
            try:
                found = _hits(f.read_text())
            except UnicodeDecodeError:
                continue
            if found:
                hits[str(f.relative_to(ROOT))] = found[:3]
        self.assertEqual(hits, {}, "live lines still name Zitadel or Grafana OIDC")


class GrafanaAdminTest(unittest.TestCase):
    def _grafana_values(self) -> dict:
        for doc in yaml.safe_load_all(KPS.read_text()):
            if isinstance(doc, dict) and doc.get("kind") == "Application":
                src = doc["spec"].get("source") or (doc["spec"].get("sources") or [{}])[0]
                values = re.sub(r"\{\{[a-zA-Z][a-zA-Z.]*\}\}", "TOKEN", src["helm"]["values"])
                return yaml.safe_load(values)["grafana"]
        raise AssertionError("no Application in kube-prometheus-stack.yaml")

    def test_grafana_admin_comes_from_the_sealed_secret(self):
        admin = self._grafana_values().get("admin") or {}
        self.assertEqual(admin.get("existingSecret"), "grafana-admin")

    def test_grafana_keeps_no_copy_of_its_sign_in(self):
        # A persistent database keeps the admin password the server was first created
        # with, so a rotation of the sealed Secret never reaches it (phase-6 acceptance,
        # 2026-09-12). The value must be stated, not left to the chart's default.
        persistence = self._grafana_values().get("persistence") or {}
        self.assertIs(persistence.get("enabled"), False)

    def test_grafana_stays_reachable_and_still_renders(self):
        # The flag above is one of three `enabled:` lines at the same depth in the
        # dashboard server's block; a blanket edit once turned off the other two, which
        # are the route to the dashboards and the image renderer. Each is held by name.
        values = self._grafana_values()
        self.assertIs((values.get("ingress") or {}).get("enabled"), True)
        self.assertIs((values.get("imageRenderer") or {}).get("enabled"), True)

    def test_grafana_has_no_oauth_environment(self):
        text = json.dumps(self._grafana_values())
        self.assertNotIn("GF_AUTH_GENERIC_OAUTH", text)
        self.assertNotIn("grafana-oidc", text)


class AuthBlockTest(unittest.TestCase):
    def test_a_config_without_auth_initializes_and_renders_no_zitadel(self):
        fx = Fixture(self, _config())
        proc = fx.run()
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        hits = {str(f.relative_to(fx.root)): _hits(f.read_text())[:3]
                for f in fx.files() if _hits(f.read_text())}
        self.assertEqual(hits, {}, "init rendered a live line naming Zitadel or OIDC")
        for rel in RETIRED_RENDERS:
            self.assertFalse(fx.rendered(rel).exists(), rel)

    def test_a_config_that_still_carries_auth_is_refused_by_name(self):
        fx = Fixture(self, _config(auth=dict(AUTH)))
        proc = fx.run()
        out = proc.stdout + proc.stderr
        self.assertNotEqual(proc.returncode, 0, "an auth: block was accepted")
        self.assertIn("auth", out)
        self.assertEqual(fx.files(), [], "a refused config still wrote files")


class ExistingInstanceTest(unittest.TestCase):
    def test_the_next_init_removes_what_zitadel_rendered(self):
        fx = Fixture(self, _config())
        for rel in RETIRED_RENDERS:
            p = fx.rendered(rel)
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text("apiVersion: v1\nkind: ConfigMap\nmetadata: {name: left-over}\n")
        hand = fx.rendered("platform/config/zitadel/mine.yaml")
        hand.write_text("apiVersion: v1\nkind: ConfigMap\nmetadata: {name: mine}\n")
        proc = fx.run()
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertEqual([r for r in RETIRED_RENDERS if fx.rendered(r).exists()], [])
        self.assertTrue(hand.exists(), "init deleted a file it never rendered")
        self.assertIn("zitadel/db-and-secrets.yaml", proc.stdout + proc.stderr)


if __name__ == "__main__":
    unittest.main()


class VerifyStageTest(unittest.TestCase):
    """The platform stage checked the single-sign-on chain for the dashboards and the deploy
    tool. With the identity provider retired there is no chain to check, and a row that
    cannot run must LEAVE — a permanently keyed-off row reads as a check that passes."""

    STAGE = pathlib.Path(__file__).resolve().parent / "verify" / "stage5-platform.sh"

    def setUp(self):
        self.text = self.STAGE.read_text()

    def test_no_row_checks_single_sign_on(self):
        hits = [f"{n}: {l.strip()[:100]}"
                for n, l in enumerate(self.text.splitlines(), 1)
                if any(k in l for k in ("generic_oauth", "code_challenge_method",
                                        "grafana-oidc", "auth/login?return_url"))]
        self.assertEqual(hits, [], "the stage still checks a single-sign-on chain nothing "
                                   "serves:\n" + "\n".join(hits))

    def test_no_gate_arms_on_an_auth_block(self):
        self.assertNotIn("GATE_AUTH", self.text,
                         "the stage still reads a gate the config can no longer carry")

    def test_the_dashboards_are_still_checked(self):
        """Retiring the identity provider does not retire the dashboards: their door and
        their datasource rows stay."""
        self.assertIn("datasource", self.text.lower())


class FourthDestinationTest(unittest.TestCase):
    """Everything the auth: block ever rendered leaves an existing instance.

    The deploy tool's own sign-on config was rendered from the same block and the file left
    core when that tool was retired — so no instance re-renders it, and nothing removes the
    copy an existing instance still carries. A retirement that cleans three of four
    destinations leaves a file pointing at an identity provider that no longer exists."""

    REL = "platform/config/argocd/oidc.yaml"

    def fixture(self):
        tmp = pathlib.Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        return InitFixture(tmp, json.loads(json.dumps(BASE_CONFIG)))

    def test_the_deploy_tools_sign_on_config_is_removed(self):
        fx = self.fixture()
        p = fx.rendered(self.REL)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("apiVersion: v1\nkind: ConfigMap\nmetadata: {name: argocd-cm}\n")
        proc = fx.run()
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertFalse(p.exists(),
                         "the deploy tool's sign-on config survives, pointing at an identity "
                         "provider that no longer exists")

    def test_a_file_the_instance_wrote_itself_survives(self):
        fx = self.fixture()
        mine = fx.rendered("platform/config/argocd/my-own.yaml")
        mine.parent.mkdir(parents=True, exist_ok=True)
        mine.write_text("kind: ConfigMap\n")
        proc = fx.run()
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertTrue(mine.is_file(), "init deleted a file it never rendered")


class RunbookTest(unittest.TestCase):
    """The runbook must not teach an operator to write a block setup now refuses."""

    DOC = pathlib.Path(__file__).resolve().parent.parent / "docs" / "ONBOARDING.md"

    def test_no_step_tells_the_operator_to_declare_an_identity_provider(self):
        text = self.DOC.read_text()
        hits = [f"{n}: {l.strip()[:100]}" for n, l in enumerate(text.splitlines(), 1)
                if re.search(r"^\s*auth:|auth: block|zita", l, re.I)]
        self.assertEqual(hits, [], "the runbook still teaches a block the next init refuses:\n"
                                   + "\n".join(hits))
