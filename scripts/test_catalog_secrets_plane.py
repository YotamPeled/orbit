#!/usr/bin/env python3
"""The instance's secret plane is sops and only sops.

The generator renders no ExternalSecret anywhere (no per-namespace registry
credential, no basic-auth credential) and marks every declared Secret, the appended
bucket credential included, `from: external`, so the chart declares them for its
render guard and creates none: the Secrets come from SopsSecrets applied by the desk.
Absent `secrets.provider` means sops; anything else is refused. Runs the real
catalog-sync.py in a temporary instance; nothing is written to the checkout and no
network is used.
"""
import pathlib
import shutil
import subprocess
import sys
import tempfile
import unittest

import yaml

ROOT = pathlib.Path(__file__).resolve().parent.parent
GENERATOR = ROOT / "scripts" / "catalog-sync.py"
CHART = ROOT / "charts" / "service"
TESTDATA = ROOT / "scripts" / "testdata" / "catalog-secrets-plane"
ORBITD_NAMESPACE = (ROOT / "template" / "provisioning" / "clusters" / "platform"
                    / "config" / "orbitd" / "namespace.yaml")
SECRETS_APP = (ROOT / "template" / "provisioning" / "clusters" / "platform"
               / "apps" / "secrets.yaml")


class Sandbox:
    def __init__(self, plane="absent"):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.tmp.name)
        (self.root / "scripts").mkdir()
        shutil.copy2(GENERATOR, self.root / "scripts" / "catalog-sync.py")
        (self.root / "charts").mkdir()
        shutil.copytree(CHART, self.root / "charts" / "service")
        shutil.copytree(TESTDATA / "catalog", self.root / "catalog")
        shutil.copytree(TESTDATA / "instance", self.root / "instance")
        if plane != "absent":
            cfg_path = self.root / "instance" / "config.yaml"
            cfg = yaml.safe_load(cfg_path.read_text())
            cfg.setdefault("secrets", {})["provider"] = plane
            cfg_path.write_text(yaml.safe_dump(cfg, sort_keys=False))
        (self.root / "clusters" / "apps").mkdir(parents=True)
        dest = (self.root / "template" / "provisioning" / "clusters" / "platform"
                / "config" / "orbitd" / "namespace.yaml")
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(ORBITD_NAMESPACE, dest)

    def close(self):
        self.tmp.cleanup()

    def generate(self):
        return subprocess.run([sys.executable, "scripts/catalog-sync.py"], cwd=self.root,
                              capture_output=True, text=True)

    def docs(self):
        out = []
        for path in sorted((self.root / "clusters").rglob("*.yaml")):
            out.extend(d for d in yaml.safe_load_all(path.read_text()) if d)
        return out


def _declared_secret_lists(node):
    """Every chart `secrets:` list inside a generated document."""
    if isinstance(node, dict):
        for k, v in node.items():
            if (k == "secrets" and isinstance(v, list) and v
                    and all(isinstance(e, dict) and "name" in e and "keys" in e for e in v)):
                yield v
            else:
                yield from _declared_secret_lists(v)
    elif isinstance(node, list):
        for v in node:
            yield from _declared_secret_lists(v)


def _identity(doc):
    meta = doc.get("metadata") or {}
    return (doc.get("kind"), meta.get("namespace"), meta.get("name"))


class SecretsPlaneTest(unittest.TestCase):
    def render(self, plane="absent"):
        box = Sandbox(plane)
        self.addCleanup(box.close)
        proc = box.generate()
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        return box.docs()

    def test_absent_plane_renders_no_secret_producer_anywhere(self):
        docs = self.render()
        producers = [_identity(d) for d in docs
                     if d.get("kind") in ("ExternalSecret", "ClusterSecretStore",
                                          "SecretStore")]
        self.assertEqual(producers, [])

    def test_explicit_sops_renders_no_secret_producer_anywhere(self):
        docs = self.render("sops")
        producers = [_identity(d) for d in docs
                     if d.get("kind") in ("ExternalSecret", "ClusterSecretStore",
                                          "SecretStore")]
        self.assertEqual(producers, [])

    def test_absent_plane_marks_every_declared_secret_external(self):
        docs = self.render()
        names = []
        for lst in _declared_secret_lists(docs):
            for entry in lst:
                self.assertEqual(entry.get("from"), "external", entry["name"])
                names.append(entry["name"])
        self.assertIn("widgets-app", names)
        self.assertIn("orbit-s3", names, "the prod bucket credential is still declared")

    def test_explicit_sops_renders_what_absent_renders(self):
        self.assertEqual(self.render("sops"), self.render())

    def test_the_retired_providers_are_refused(self):
        """infisical, infisical-cloud and anything else: sops is the only plane."""
        for plane in ("infisical", "infisical-cloud", "vault"):
            with self.subTest(plane=plane):
                box = Sandbox(plane)
                self.addCleanup(box.close)
                proc = box.generate()
                self.assertNotEqual(proc.returncode, 0)
                self.assertIn("secrets.provider", proc.stdout + proc.stderr)

    def test_the_withdrawn_secretsplane_key_is_refused(self):
        """secretsPlane shipped in no release; one key decides the plane: secrets.provider."""
        box = Sandbox()
        self.addCleanup(box.close)
        cfg_path = box.root / "instance" / "config.yaml"
        cfg = yaml.safe_load(cfg_path.read_text()); cfg["secretsPlane"] = "sops"
        cfg_path.write_text(yaml.safe_dump(cfg, sort_keys=False))
        proc = box.generate()
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("secrets.provider", proc.stdout + proc.stderr)

    def test_template_declares_the_secrets_platform_app(self):
        self.assertTrue(SECRETS_APP.is_file(), f"{SECRETS_APP.relative_to(ROOT)} is missing")
        app = yaml.safe_load(SECRETS_APP.read_text())
        self.assertEqual(app["kind"], "Application")
        self.assertEqual(app["metadata"]["name"], "secrets")
        src = app["spec"]["source"]
        self.assertEqual(src["repoURL"], "{{git.repoUrl}}")
        self.assertEqual(src["path"], "clusters/platform/secrets")
        self.assertTrue(src["directory"]["recurse"])


class SopsGoldenTest(unittest.TestCase):
    """The sops render stays byte-identical to the committed golden, file by file."""

    def _check(self, fixture: pathlib.Path, expected: pathlib.Path, plane="absent"):
        box = Sandbox(plane)
        self.addCleanup(box.close)
        if fixture != TESTDATA:
            shutil.rmtree(box.root / "catalog")
            shutil.copytree(fixture / "catalog", box.root / "catalog")
        proc = box.generate()
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        got = {p.relative_to(box.root / "clusters").as_posix(): p.read_bytes()
               for p in (box.root / "clusters").rglob("*.yaml")}
        want = {p.relative_to(expected).as_posix(): p.read_bytes() for p in expected.rglob("*.yaml")}
        self.assertEqual(sorted(got), sorted(want))
        for name in want:
            self.assertEqual(got[name], want[name], f"{name} differs from the sops render")

    def test_absent_plane_is_byte_identical_to_the_golden(self):
        self._check(TESTDATA, TESTDATA / "expected-sops" / "base")

    def test_explicit_sops_is_byte_identical_to_the_golden(self):
        self._check(TESTDATA, TESTDATA / "expected-sops" / "base", "sops")

    def test_aliased_secret_entries_stay_byte_identical_to_the_golden(self):
        self._check(TESTDATA / "aliased", TESTDATA / "expected-sops" / "aliased")


if __name__ == "__main__":
    unittest.main()
