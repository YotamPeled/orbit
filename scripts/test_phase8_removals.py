#!/usr/bin/env python3
"""
Phase 8 (DESIGN-v2 §2, §8): agentgateway, the status page and orbit-hooks are deleted.

agentgateway is an MCP door in front of a third-party Grafana MCP server; orbitd is the MCP
server now. The status page is a product feature, not platform. orbit-hooks receives CI
events, and the desk ships without CI. Deleting them has three halves, and each class below
holds one:

- CoreShipsNoneTest: core itself no longer carries their sources, image or workflow.
- RenderedTreeTest: a fresh init renders nothing that names them, and orbitd's
  Ingress answers on mcp.<primary> (DESIGN-v2 §9: the record agentgateway held moves
  to orbitd).
- ExistingInstanceTest: an instance that rendered them before keeps none of those files
  after the next init, a hand-written file beside them survives, and a config that still
  carries the retired coreRegistry.hooksTag pin initializes.

Run as `python3 scripts/test_phase8_removals.py` or under pytest.
"""
import importlib.util
import json
import pathlib
import re
import shutil
import sys
import tempfile
import unittest

import yaml

HERE = pathlib.Path(__file__).resolve().parent
ROOT = HERE.parent

_SPEC = importlib.util.spec_from_file_location("test_instance_init", HERE / "test_instance_init.py")
_TII = importlib.util.module_from_spec(_SPEC)
_BYTECODE, sys.dont_write_bytecode = sys.dont_write_bytecode, True
_SPEC.loader.exec_module(_TII)
sys.dont_write_bytecode = _BYTECODE
InitFixture, BASE_CONFIG = _TII.InitFixture, _TII.BASE_CONFIG

RETIRED_NAMES = ("agentgateway", "orbit-hooks", "status-page")

# Every core path that carried one of the three.
RETIRED_CORE_PATHS = (
    "template/clusters/platform/apps/agentgateway.yaml",
    "template/clusters/platform/config/agentgateway",
    "template/provisioning/clusters/platform/config/agentgateway",
    "template/clusters/platform/config/status-page",
    "template/provisioning/clusters/platform/config/status-page",
    "template/provisioning/clusters/platform/config/orbit-hooks",
    "services/hooks",
    ".github/workflows/hooks-image.yml",
    # 2026-10-06, the owner's rule that nothing runs in GitHub Actions: the optional build
    # pipeline the template shipped to instance repos, and its caller stub.
    "template/.github/workflows/build-service.yml",
    "template/.github/app-repo-build.example.yml",
    "template/.github",
)

# What an instance rendered or vendored from them before this change.
RETIRED_RENDERS = (
    "platform/config/agentgateway/jwt-door.yaml",
    "platform/config/agentgateway/mcp-grafana.yaml",
    "platform/config/agentgateway/gateway.yaml",
    "platform/config/status-page/content.yaml",
    "platform/config/status-page/ingress.yaml",
    "platform/config/status-page/namespace.yaml",
    "platform/config/orbit-hooks/deployment.yaml",
    "platform/config/orbit-hooks/externalsecret.yaml",
    "platform/config/orbit-hooks/namespace.yaml",
    "platform/config/orbit-hooks/pvc.yaml",
    "platform/config/orbit-hooks/service.yaml",
)


def _live_lines(text: str):
    """Lines that are not YAML/shell comments: a comment may record history, a live line
    may not name a deleted component."""
    for n, line in enumerate(text.splitlines(), 1):
        stripped = line.strip()
        if stripped and not stripped.startswith("#"):
            yield n, line


def _names_retired(text: str):
    return [(n, line.strip()) for n, line in _live_lines(text)
            if any(name in line for name in RETIRED_NAMES)]


def _merged(extra: dict = None) -> dict:
    cfg = json.loads(json.dumps(BASE_CONFIG))
    for k, v in (extra or {}).items():
        if isinstance(v, dict) and isinstance(cfg.get(k), dict):
            cfg[k].update(v)
        else:
            cfg[k] = v
    return cfg


class _FixtureCase(unittest.TestCase):
    def fixture(self, extra: dict = None) -> InitFixture:
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        return InitFixture(pathlib.Path(tmp), _merged(extra))

    def run_ok(self, fx: InitFixture):
        proc = fx.run()
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        return proc


class CoreShipsNoneTest(unittest.TestCase):
    def test_no_core_path_carries_a_retired_component(self):
        present = [p for p in RETIRED_CORE_PATHS if (ROOT / p).exists()]
        self.assertEqual(present, [], "retired component sources still in core")

    def test_the_vendoring_manifest_names_none(self):
        manifest = (ROOT / "template" / "core-manifest.txt")
        if not manifest.exists():
            manifest = ROOT / "core-manifest.txt"
        hits = _names_retired(manifest.read_text())
        self.assertEqual(hits, [], f"{manifest.name} still vendors a retired component")

    def test_no_template_or_generator_line_names_one(self):
        """Comments may keep history; no live line in the template, the generators or
        the verify stages may name a deleted component."""
        files = [p for p in (ROOT / "template").rglob("*") if p.is_file()]
        files += [ROOT / "scripts" / "catalog-sync.py", ROOT / "scripts" / "instance-init.py"]
        files += sorted((ROOT / "scripts" / "verify").glob("*.sh"))
        files += sorted((ROOT / "scripts" / "testdata").rglob("*.yaml"))
        hits = {}
        for f in files:
            try:
                text = f.read_text()
            except UnicodeDecodeError:
                continue
            found = _names_retired(text)
            if found:
                hits[str(f.relative_to(ROOT))] = found[:3]
        self.assertEqual(hits, {}, "live lines still name a retired component")

    def test_tenant_namespaces_no_longer_admit_the_gateway(self):
        src = (ROOT / "scripts" / "catalog-sync.py").read_text()
        self.assertEqual([l for _, l in _live_lines(src) if "agentgateway-system" in l], [])

    def test_platform_restart_alert_no_longer_watches_the_gateway(self):
        rules = (ROOT / "template" / "clusters" / "platform" / "monitoring" / "rules.yaml")
        if not rules.exists():
            rules = ROOT / "template" / "provisioning" / "clusters" / "platform" / "monitoring" / "rules.yaml"
        self.assertNotIn("agentgateway-system", rules.read_text())


class RenderedTreeTest(_FixtureCase):
    def _assert_clean(self, fx: InitFixture):
        hits = {}
        for f in sorted((fx.root / "clusters").rglob("*")):
            if f.is_file():
                found = _names_retired(f.read_text())
                if found:
                    hits[str(f.relative_to(fx.root))] = found[:3]
        self.assertEqual(hits, {}, "init rendered a retired component")

    def test_a_fresh_init_renders_none(self):
        fx = self.fixture()
        self.run_ok(fx)
        self._assert_clean(fx)
        for rel in RETIRED_RENDERS:
            self.assertFalse(fx.rendered(rel).exists(), rel)

    def test_orbitd_answers_on_the_mcp_host(self):
        fx = self.fixture()
        self.run_ok(fx)
        docs = [d for d in yaml.safe_load_all(
            fx.rendered("platform/config/orbitd/service.yaml").read_text()) if d]
        ingresses = [d for d in docs if d.get("kind") == "Ingress"]
        self.assertTrue(ingresses, "orbitd has no Ingress")
        hosts = {r.get("host") for d in ingresses for r in d["spec"].get("rules", [])}
        primary = BASE_CONFIG["domains"]["primary"]
        self.assertIn(f"mcp.{primary}", hosts)
        self.assertIn(f"orbit.{primary}", hosts, "the existing orbit host must stay")


class ExistingInstanceTest(_FixtureCase):
    def test_the_next_init_removes_what_they_rendered(self):
        fx = self.fixture()
        for rel in RETIRED_RENDERS:
            p = fx.rendered(rel)
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text("apiVersion: v1\nkind: ConfigMap\nmetadata: {name: left-over}\n")
        hand = fx.rendered("platform/config/team-notes/keep.yaml")
        hand.parent.mkdir(parents=True, exist_ok=True)
        hand.write_text("apiVersion: v1\nkind: ConfigMap\nmetadata: {name: keep}\n")
        proc = self.run_ok(fx)
        left = [rel for rel in RETIRED_RENDERS if fx.rendered(rel).exists()]
        self.assertEqual(left, [], "init kept files a deleted component rendered")
        for d in ("agentgateway", "status-page", "orbit-hooks"):
            self.assertFalse(fx.rendered(f"platform/config/{d}").exists(),
                             f"empty platform/config/{d} left behind")
        self.assertTrue(hand.exists(), "init deleted a hand-written file")
        out = proc.stdout + proc.stderr
        self.assertIn("orbit-hooks/deployment.yaml", out, "a removal must be reported by path")

    def test_a_config_still_carrying_the_hooks_pin_initializes(self):
        fx = self.fixture()
        self.assertIn("hooksTag", BASE_CONFIG["coreRegistry"])
        self.run_ok(fx)

    def test_a_config_without_the_hooks_pin_initializes(self):
        cfg = _merged()
        cfg["coreRegistry"].pop("hooksTag", None)
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        fx = InitFixture(pathlib.Path(tmp), cfg)
        self.run_ok(fx)

    def test_the_example_config_no_longer_asks_for_the_hooks_pin(self):
        example = (ROOT / "template" / "instance" / "config.yaml.example").read_text()
        self.assertEqual([l for _, l in _live_lines(example) if "hooksTag" in l], [])


if __name__ == "__main__":
    unittest.main()
