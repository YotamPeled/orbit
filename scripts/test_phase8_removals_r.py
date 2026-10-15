#!/usr/bin/env python3
"""
Phase 8 removals, review round 1 (fnd-e7w34r7). Four defects, one class each:

- InitPathSafetyTest: init must never write or delete through a symlink. The retired-file
  cleanup deleted files in a directory outside the instance through a symlinked retired
  directory, and the gate-closed removal loop had the same pattern. This is the second time
  init followed a symlink (the secrets folder was the first), so the rule is uniform: before
  rendering, init refuses any symlink on the path of anything it will write or delete under
  clusters/, names the path, exits nonzero, and leaves both trees untouched. A retired
  destination that is a directory is refused the same way, never a traceback.
- McpHostTest: orbitd's Ingress routes mcp.<primary>, so orbitd's host and origin guard must
  allow it; otherwise every MCP request on that host is a 421.
- VendoredRetiredTest: files an instance vendored from template/clusters for the retired
  components go on the next init too, so the status page is never half-removed.
- UserFacingReferencesTest: the orbit-start skill and the app-repo build workflow no longer
  send anyone to the retired endpoints or the deleted source.
"""
import importlib.util
import pathlib
import shutil
import sys
import tempfile
import unittest

import yaml

HERE = pathlib.Path(__file__).resolve().parent
ROOT = HERE.parent

_SPEC = importlib.util.spec_from_file_location("test_phase8_removals", HERE / "test_phase8_removals.py")
_P8 = importlib.util.module_from_spec(_SPEC)
_BYTECODE, sys.dont_write_bytecode = sys.dont_write_bytecode, True
_SPEC.loader.exec_module(_P8)
_CSPEC = importlib.util.spec_from_file_location(
    "orbitd_config", ROOT / "services" / "orbitd" / "orbitd" / "config.py")
_CFG = importlib.util.module_from_spec(_CSPEC)
sys.modules["orbitd_config"] = _CFG  # dataclasses resolve annotations through sys.modules
_CSPEC.loader.exec_module(_CFG)
sys.dont_write_bytecode = _BYTECODE

InitFixture, BASE_CONFIG = _P8.InitFixture, _P8.BASE_CONFIG


def _tree(path: pathlib.Path) -> dict:
    return {str(p.relative_to(path)): (p.read_bytes() if p.is_file() else None)
            for p in sorted(path.rglob("*"))}


class _Case(_P8._FixtureCase):
    def outside(self, files: dict) -> pathlib.Path:
        d = pathlib.Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, d, ignore_errors=True)
        for name, body in files.items():
            (d / name).write_text(body)
        return d

    def assertRefused(self, proc, named: str):
        out = proc.stdout + proc.stderr
        self.assertNotEqual(proc.returncode, 0, out)
        self.assertNotIn("Traceback", out)
        self.assertIn(named, out, "the refusal must name the offending path")


class InitPathSafetyTest(_Case):
    def _link(self, fx, rel: str, target: pathlib.Path):
        p = fx.rendered(rel)
        p.parent.mkdir(parents=True, exist_ok=True)
        if p.is_dir() and not p.is_symlink():      # the static template artifacts live there (round 24)
            shutil.rmtree(p)
        p.symlink_to(target, target_is_directory=target.is_dir())

    def test_a_symlinked_retired_directory_is_refused(self):
        fx = self.fixture()
        out = self.outside({"deployment.yaml": "a: 1\n", "pvc.yaml": "b: 2\n", "mine.txt": "keep\n"})
        before = _tree(out)
        self._link(fx, "platform/config/orbit-hooks", out)
        self.assertRefused(fx.run(), "orbit-hooks")
        self.assertEqual(_tree(out), before, "init changed a directory outside the instance")

    def test_a_symlinked_retired_directory_holding_one_file_is_refused(self):
        fx = self.fixture()
        out = self.outside({"deployment.yaml": "a: 1\n"})
        before = _tree(out)
        self._link(fx, "platform/config/orbit-hooks", out)
        self.assertRefused(fx.run(), "orbit-hooks")
        self.assertEqual(_tree(out), before)

    def test_a_symlinked_directory_a_closed_gate_would_clean_is_refused(self):
        """No bucket closes the bucket gate, which removes the bucket-gated renders;
        the directory holding them must not be followed."""
        fx = self.fixture({"objectStorage": None})
        out = self.outside({"loki.yaml": "c: 3\n", "tempo.yaml": "d: 4\n"})
        before = _tree(out)
        self._link(fx, "platform/apps", out)
        self.assertRefused(fx.run(), "platform/apps")
        self.assertEqual(_tree(out), before)

    def test_a_symlinked_directory_init_renders_into_is_refused(self):
        fx = self.fixture()
        out = self.outside({"unrelated.yaml": "e: 5\n"})
        before = _tree(out)
        self._link(fx, "platform/apps", out)
        self.assertRefused(fx.run(), "platform/apps")
        self.assertEqual(_tree(out), before, "init rendered into a directory outside the instance")

    def test_a_retired_destination_that_is_a_directory_is_refused(self):
        fx = self.fixture()
        d = fx.rendered("platform/config/orbit-hooks/deployment.yaml")
        d.mkdir(parents=True)
        (d / "note.txt").write_text("hand\n")
        self.assertRefused(fx.run(), "orbit-hooks/deployment.yaml")
        self.assertTrue((d / "note.txt").exists())

    def test_a_symlinked_retired_file_never_touches_its_target(self):
        fx = self.fixture()
        out = self.outside({"target.yaml": "f: 6\n"})
        before = _tree(out)
        self._link(fx, "platform/config/orbit-hooks/deployment.yaml", out / "target.yaml")
        fx.run()
        self.assertEqual(_tree(out), before)

    def test_a_clean_instance_still_initializes_twice(self):
        fx = self.fixture()
        self.run_ok(fx)
        snap = fx.snapshot()
        self.run_ok(fx)
        self.assertEqual(fx.snapshot(), snap)


class McpHostTest(_Case):
    def test_orbitd_accepts_both_hosts_its_ingress_routes(self):
        fx = self.fixture()
        self.run_ok(fx)
        docs = [d for d in yaml.safe_load_all(
            fx.rendered("platform/config/orbitd/deployment.yaml").read_text()) if d]
        dep = next(d for d in docs if d.get("kind") == "Deployment")
        env = {"ORBITD_SIGNING_KEY": "/k", "ORBITD_PRINCIPALS": "/p", "ORBITD_REVOCATIONS": "/r"}
        for c in dep["spec"]["template"]["spec"]["containers"]:
            for e in c.get("env") or []:
                if "value" in e:
                    env[e["name"]] = str(e["value"])
        cfg = _CFG.from_env(env)
        primary = BASE_CONFIG["domains"]["primary"]
        for host in (f"orbit.{primary}", f"mcp.{primary}"):
            self.assertIn(host, cfg.allowed_hosts, "orbitd would answer 421 on " + host)
            self.assertIn(f"https://{host}", cfg.allowed_origins)
        self.assertEqual(cfg.mcp_url, f"https://orbit.{primary}/mcp",
                         "the canonical MCP URL and token audience stay on orbit.<primary>")


class VendoredRetiredTest(_Case):
    VENDORED = (
        "platform/apps/agentgateway.yaml",
        "platform/config/agentgateway/netpol.yaml",
        "platform/config/status-page/deployment.yaml",
        "platform/config/status-page/service.yaml",
    )

    def test_vendored_retired_files_go_with_the_rendered_ones(self):
        fx = self.fixture()
        for rel in self.VENDORED + ("platform/config/status-page/namespace.yaml",):
            p = fx.rendered(rel)
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text("apiVersion: v1\nkind: ConfigMap\nmetadata: {name: left-over}\n")
        proc = self.run_ok(fx)
        left = [rel for rel in self.VENDORED if fx.rendered(rel).exists()]
        self.assertEqual(left, [])
        self.assertFalse(fx.rendered("platform/config/status-page").exists())
        self.assertIn("status-page/deployment.yaml", proc.stdout + proc.stderr)


class UserFacingReferencesTest(unittest.TestCase):
    RETIRED = ("orbit-hooks", "status.$ORBIT_DOMAIN", "services/hooks", "agentgateway", "status-page")

    def test_skills_workflows_and_docstrings_name_none(self):
        files = sorted((ROOT / ".claude").rglob("*.md"))
        files += [ROOT / "services" / "intake" / "app.py", ROOT / "services" / "notifier" / "app.py",
                  ROOT / "scripts" / "github-app-token.py"]
        hits = {}
        for f in files:
            if not f.exists():
                continue
            for n, line in enumerate(f.read_text().splitlines(), 1):
                if any(r in line for r in self.RETIRED):
                    hits.setdefault(str(f.relative_to(ROOT)), []).append(n)
        self.assertEqual(hits, {}, "still sends a reader to a retired component")


if __name__ == "__main__":
    unittest.main()
