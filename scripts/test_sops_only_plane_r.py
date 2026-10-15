#!/usr/bin/env python3
"""
The sops-only plane, round 1 repairs — run as
`python3 scripts/test_sops_only_plane_r.py`. No cluster, no credentials, no network.

WHY. The commit that made sops the only secret plane passed its own contract and was then
reviewed. Four things it left, plus one the landing merge exposed:

  * The sealed-tree scan rejects the placeholder file that init itself writes. A fresh
    instance keeps `clusters/platform/secrets/` in git with a `.gitkeep`, because git keeps
    no empty folder and the platform plan refuses to render when the folder is missing — and
    the stage then reports that placeholder as a sealed file holding no SopsSecret. A fresh
    org is red on its first verify.
  * The operator skills still present the retired plane as the default and give a rotation
    procedure whose tooling is deleted.
  * A catalog written against the old `from:` value is silently rewritten rather than
    refused, so an org's declared source changes under it without a word.
  * The instance config example still documents a token and a file that are gone.
  * An instance that ran on the hosted vault keeps a sealed machine-identity file pointing at
    a namespace phase 8 deleted; init leaves it there for ever.
"""
import json
import pathlib
import re
import shutil
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
_BYTECODE, sys.dont_write_bytecode = sys.dont_write_bytecode, True
from test_verify_secrets import build_repo, run_stage  # noqa: E402
from test_instance_init import BASE_CONFIG, InitFixture  # noqa: E402
sys.dont_write_bytecode = _BYTECODE

ROOT = pathlib.Path(__file__).resolve().parent.parent
SKILLS = ROOT / ".claude" / "skills"
EXAMPLE = ROOT / "template" / "instance" / "config.yaml.example"
GENERATOR = ROOT / "scripts" / "catalog-sync.py"
RETIRED = re.compile(r"infisical|provision-vault|root-ceremony|external-secrets|\bESO\b", re.I)


class PlaceholderTest(unittest.TestCase):
    """The folder init creates must pass the stage init's own tree produces."""

    def setUp(self):
        self.tmp = pathlib.Path(tempfile.mkdtemp(prefix="orbit-j24ar-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.tree = self.tmp / "scaffold"
        build_repo(self.tree)

    def commit(self):
        env = dict(GIT_AUTHOR_NAME="f", GIT_AUTHOR_EMAIL="f@f",
                   GIT_COMMITTER_NAME="f", GIT_COMMITTER_EMAIL="f@f", PATH="/usr/bin:/bin")
        subprocess.run(["git", "add", "-A"], cwd=self.tree, env=env, capture_output=True)
        subprocess.run(["git", "commit", "-qm", "placeholder"], cwd=self.tree, env=env,
                       capture_output=True)

    def test_the_placeholder_is_not_read_as_a_sealed_file(self):
        (self.tree / "clusters" / "platform" / "secrets" / ".gitkeep").write_text("")
        self.commit()
        proc = run_stage(self.tree)
        self.assertEqual(proc.returncode, 0,
                         "the placeholder init writes is reported as a broken sealed file:\n"
                         + proc.stdout[-2500:])
        self.assertNotIn(".gitkeep", proc.stdout)

    def test_a_fresh_instance_with_only_the_placeholder_is_green(self):
        for f in (self.tree / "clusters" / "platform" / "secrets").rglob("*.yaml"):
            f.unlink()
        (self.tree / "clusters" / "platform" / "secrets" / ".gitkeep").write_text("")
        self.commit()
        proc = run_stage(self.tree)
        self.assertEqual(proc.returncode, 0,
                         "a fresh org with no sealed file yet is red on its first verify:\n"
                         + proc.stdout[-2500:])

    def test_a_real_file_that_is_not_a_sealed_secret_is_still_red(self):
        """The guard is the placeholder, not 'ignore anything unparsable'."""
        (self.tree / "clusters" / "platform" / "secrets" / "notes.yaml").write_text("a: 1\n")
        self.commit()
        proc = run_stage(self.tree)
        self.assertNotEqual(proc.returncode, 0,
                            "a stray YAML file in the sealed tree passed:\n" + proc.stdout[-2500:])


class OperatorTextTest(unittest.TestCase):
    """What an operator is told to do must be what exists."""

    def skills(self):
        return sorted(SKILLS.rglob("SKILL.md"))

    def test_no_skill_presents_the_retired_plane(self):
        hits = []
        for p in self.skills():
            for n, line in enumerate(p.read_text().splitlines(), 1):
                if RETIRED.search(line):
                    hits.append(f"{p.relative_to(ROOT)}:{n}: {line.strip()[:100]}")
        self.assertEqual(hits, [], "an operator skill still names the retired plane:\n"
                                   + "\n".join(hits))

    def test_the_rotation_procedure_is_the_one_that_exists(self):
        text = "\n".join(p.read_text() for p in self.skills())
        self.assertRegex(text, r"orbit secrets (mint|adopt|export|verify)",
                         "no skill tells an operator how a value is rotated on the sealed plane")

    def test_the_config_example_documents_nothing_deleted(self):
        hits = [f"{n}: {l.strip()[:100]}" for n, l in enumerate(EXAMPLE.read_text().splitlines(), 1)
                if RETIRED.search(l)]
        self.assertEqual(hits, [], "the instance config example still documents the retired "
                                   "plane:\n" + "\n".join(hits))


class CatalogRefusalTest(unittest.TestCase):
    """An org's declared source is not rewritten under it."""

    def sandbox(self, source: str):
        sys.path.insert(0, str(ROOT / "scripts"))
        _b, sys.dont_write_bytecode = sys.dont_write_bytecode, True
        from test_catalog_quota import Sandbox  # noqa: E402
        sys.dont_write_bytecode = _b
        box = Sandbox()
        self.addCleanup(box.close)
        cat = box.root / "catalog" / "projects.yaml"
        text = cat.read_text()
        text = text.replace("    components:", "    secrets:\n"
                            f"      - name: widgets-app\n        from: {source}\n"
                            "        keys: [WIDGETS_API_TOKEN]\n    components:", 1)
        cat.write_text(text)
        return box

    def test_the_retired_source_is_refused(self):
        box = self.sandbox("infisical")
        proc = box.generate()
        self.assertNotEqual(proc.returncode, 0,
                            "a catalog declaring the retired source generated silently:\n"
                            + proc.stdout[-1500:])
        self.assertIn("infisical", proc.stdout + proc.stderr,
                      "the refusal does not name the value it refused")

    def test_the_two_supported_sources_still_generate(self):
        for source in ("sops", "external"):
            box = self.sandbox(source)
            proc = box.generate()
            self.assertEqual(proc.returncode, 0, f"{source}: " + proc.stdout[-1500:])


class RetiredFileTest(unittest.TestCase):
    """What an existing instance carries from the old plane leaves on the next init."""

    REL = "platform/config/external-secrets/infisical-machine-identity.sops.yaml"

    def fixture(self):
        tmp = pathlib.Path(tempfile.mkdtemp(prefix="orbit-j24ar-init-"))
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        return InitFixture(tmp, json.loads(json.dumps(BASE_CONFIG)))

    def test_the_machine_identity_file_is_removed(self):
        fx = self.fixture()
        for rel in (self.REL, self.REL + ".example"):
            p = fx.rendered(rel)
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text("apiVersion: isindir.github.com/v1alpha3\nkind: SopsSecret\n"
                         "metadata: {name: infisical-machine-identity, "
                         "namespace: external-secrets}\n")
        proc = fx.run()
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        left = [rel for rel in (self.REL, self.REL + ".example") if fx.rendered(rel).exists()]
        self.assertEqual(left, [], "a sealed file for the retired plane survives init, and it "
                                   "targets a namespace phase 8 deleted")

    def test_a_sealed_file_of_the_current_plane_is_untouched(self):
        fx = self.fixture()
        keep = fx.rendered("platform/secrets/monitoring/alertmanager-intake.yaml")
        keep.parent.mkdir(parents=True, exist_ok=True)
        keep.write_text("sealed: ENC[AES256_GCM,data:fixture]\n")
        proc = fx.run()
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertEqual(keep.read_text(), "sealed: ENC[AES256_GCM,data:fixture]\n")


if __name__ == "__main__":
    unittest.main(verbosity=2)
