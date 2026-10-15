#!/usr/bin/env python3
"""`make check` (template/Makefile): the generated files are what the catalog generates.

QA of v1.0.0, F3. The target was `sync` followed by `git diff --exit-code clusters/apps`,
so it failed on every change that was not committed yet: the documented order is sync,
check, commit, and check always failed there with "generated files are stale, run make
sync and commit" right after a sync. It also could not see a generated file that is new,
because git does not diff a file it does not track.

The REAL Makefile runs here over a stand-in generator (one file per catalog line), with
no git repository at all: the question is about the tree, not about what is committed.
"""
import pathlib
import shutil
import subprocess
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parent.parent
MAKEFILE = ROOT / "template" / "Makefile"

GENERATOR = '''
import pathlib
root = pathlib.Path(__file__).resolve().parent.parent
apps = root / "clusters" / "apps"
apps.mkdir(parents=True, exist_ok=True)
want = {}
for line in (root / "catalog" / "projects.yaml").read_text().split():
    if line == "REFUSE":
        raise SystemExit("the generator refuses this catalog")
    want[f"{line}.yaml"] = f"generated for {line}\\n"
for path in apps.glob("*.yaml"):
    if path.name not in want:
        path.unlink()
        print(f"pruned clusters/apps/{path.name}")
for name, text in want.items():
    if not (apps / name).is_file() or (apps / name).read_text() != text:
        (apps / name).write_text(text)
        print(f"wrote clusters/apps/{name}")
'''


@unittest.skipUnless(shutil.which("make"), "make is not installed")
class MakeCheckTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.tmp.name)
        shutil.copy2(MAKEFILE, self.root / "Makefile")
        (self.root / "scripts").mkdir()
        (self.root / "scripts" / "catalog-sync.py").write_text(GENERATOR)
        (self.root / "catalog").mkdir()
        self.catalog("alpha beta")
        self.make("sync")

    def tearDown(self):
        self.tmp.cleanup()

    def catalog(self, text):
        (self.root / "catalog" / "projects.yaml").write_text(text + "\n")

    def make(self, target):
        return subprocess.run(["make", "-s", target], cwd=self.root, capture_output=True, text=True, timeout=120)

    def apps(self):
        return {p.name: p.read_text() for p in (self.root / "clusters" / "apps").glob("*.yaml")}

    def test_a_synced_tree_passes_committed_or_not(self):
        """Sync, check, commit: the documented order. Nothing here is committed (there is
        no repository at all) and the tree is exactly what the generator writes."""
        proc = self.make("check")
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("catalog and manifests agree", proc.stdout)
        # and again after a catalog change that was synced and not committed
        self.catalog("alpha beta gamma")
        self.assertEqual(self.make("sync").returncode, 0)
        proc = self.make("check")
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)

    def test_a_generated_file_edited_by_hand_fails_and_is_named(self):
        (self.root / "clusters" / "apps" / "alpha.yaml").write_text("edited by hand\n")
        proc = self.make("check")
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("alpha.yaml", proc.stdout + proc.stderr)
        self.assertIn("were stale", proc.stdout + proc.stderr)
        self.assertEqual(self.apps()["alpha.yaml"], "generated for alpha\n", "check leaves the tree regenerated")
        self.assertEqual(self.make("check").returncode, 0, "a second check on the regenerated tree passes")

    def test_a_catalog_change_that_was_not_synced_fails_also_when_its_file_is_new(self):
        """The old `git diff` could not see this one: the file it should have found
        missing is one git has never tracked."""
        self.catalog("alpha beta gamma")
        proc = self.make("check")
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("gamma.yaml", proc.stdout + proc.stderr)

    def test_a_generated_file_the_catalog_no_longer_has_fails(self):
        self.catalog("alpha")
        proc = self.make("check")
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("beta.yaml", proc.stdout + proc.stderr)
        self.assertNotIn("beta.yaml", self.apps())

    def test_a_catalog_the_generator_refuses_fails_with_its_sentence(self):
        self.catalog("alpha REFUSE")
        proc = self.make("check")
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("the generator refuses this catalog", proc.stdout + proc.stderr)
        self.assertNotIn("catalog and manifests agree", proc.stdout)


if __name__ == "__main__":
    unittest.main()
