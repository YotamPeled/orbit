#!/usr/bin/env python3
"""
Grammar tests for scripts/core-sync.sh — run as `python3 scripts/test_core_sync.py`.

The break this file catches is a `rm -rf` outside the instance repo. core-sync.sh vendors
core into an instance by reading core's OWN core-manifest.txt (the pinned version's, not
the instance's), and the sync arm removes each destination before copying it. Review round
2 (2026-09-07) ran the real script with `source.txt -> ../outside` and watched it delete a
sibling directory and write a file in its place — on every instance that bumps its pin,
from one line somebody adds to a manifest nobody re-reads. The same probe found an empty
source copying the whole core clone including `.git`, an empty destination killing the run
mid-loop under `set -u` instead of reporting, and `a -> b -> c` splitting at the first
arrow for the source and the last for the destination in silence.

So the grammar is a rule with a test, not a convention: a line is `<path>` or
`<path> -> <path>`, both sides non-empty, neither absolute, neither carrying a `..`
segment, and both resolving beneath their own root — the core clone for the source, the
instance repo for the destination. Every case below drives the REAL script over a real
git clone in a throwaway directory, and each asserts the three things a bad line owes the
operator: it is named by content, it does not stop the lines after it, and the run exits
non-zero.
"""
import pathlib
import shutil
import subprocess
import sys
import tempfile
import unittest

# The retired tooling is named in exactly one place — the sops-plane contract, which the
# contract test itself does not scan — and this file reads that list instead of
# repeating it, so the retirement guard cannot itself become a mention.
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
_BYTECODE, sys.dont_write_bytecode = sys.dont_write_bytecode, True
from test_sops_only_tooling import RETIRED_PATHS  # noqa: E402
sys.dont_write_bytecode = _BYTECODE

SYNC = pathlib.Path(__file__).resolve().parent / "core-sync.sh"
GIT = ["git", "-c", "user.email=t@t", "-c", "user.name=t", "-c", "commit.gpgsign=false"]


class ManifestGrammarTest(unittest.TestCase):

    def setUp(self):
        self.tmp = pathlib.Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        # A sibling of the instance repo, with something in it worth not losing. This is
        # the thing `../outside` reaches.
        self.outside = self.tmp / "outside"
        self.outside.mkdir()
        (self.outside / "sentinel.txt").write_text("do not delete me\n")

    def core(self, manifest: str) -> pathlib.Path:
        """A core clone whose manifest is the line under test."""
        core = self.tmp / "core"
        fresh = not core.exists()
        core.mkdir(exist_ok=True)
        (core / "a").write_text("core a\n")
        (core / "x").write_text("core x\n")
        (core / "dir").mkdir(exist_ok=True)
        (core / "dir" / "with space.txt").write_text("core spaced\n")
        (core / "core-manifest.txt").write_text(manifest)
        if fresh:
            subprocess.run(GIT + ["init", "-q", "-b", "vendor-test"], cwd=core, check=True)
        subprocess.run(GIT + ["add", "-A"], cwd=core, check=True, capture_output=True)
        subprocess.run(GIT + ["commit", "-q", "--allow-empty", "-m",
                              "manifest under test"], cwd=core, check=True,
                       capture_output=True)
        return core

    def run_sync(self, manifest: str, *args) -> subprocess.CompletedProcess:
        core = self.core(manifest)
        inst = self.tmp / "instance"
        inst.mkdir(exist_ok=True)
        (inst / "orbit-core.lock").write_text(f"url: {core}\nref: vendor-test\n")
        return subprocess.run(["bash", str(SYNC), *args], cwd=inst,
                              capture_output=True, text=True)

    def assert_refused(self, manifest: str, quoted: str):
        """A bad line is named by CONTENT, counted, and stepped over — `a` still lands."""
        proc = self.run_sync(manifest + "a\n")
        out = proc.stdout + proc.stderr
        self.assertNotEqual(proc.returncode, 0, "the bad line did not fail the run:\n" + out)
        self.assertIn(quoted, out, "the refusal does not quote the offending line:\n" + out)
        landed = self.tmp / "instance" / "a"
        self.assertTrue(landed.is_file(),
                        "one bad line stopped the manifest instead of being stepped "
                        "over:\n" + out)
        self.assertEqual(landed.read_text(), "core a\n")
        return proc

    def test_a_mapped_destination_cannot_escape_the_instance_repo(self):
        """THE one. `rm -rf` runs on the destination, so `../` is a delete outside the repo."""
        self.assert_refused("a -> ../outside\n", "a -> ../outside")
        self.assertTrue((self.outside / "sentinel.txt").is_file(),
                        "core-sync.sh deleted a directory outside the instance repo")
        self.assertEqual((self.outside / "sentinel.txt").read_text(), "do not delete me\n")

    def test_an_absolute_destination_is_refused(self):
        self.assert_refused("a -> /abs\n", "a -> /abs")
        self.assertFalse(pathlib.Path("/abs").exists(),
                         "core-sync.sh wrote to an absolute path outside the instance")

    def test_an_empty_destination_reports_instead_of_killing_the_run(self):
        """`rm -rf "${dst:?}"` under `set -euo pipefail` used to abort the loop here, so
        the lines after it were never processed and RC never accumulated."""
        self.assert_refused("a -> \n", "a -> ")

    def test_an_empty_source_is_refused(self):
        """An empty source resolved to the core clone ITSELF and copied it whole, `.git`
        and all, into the destination."""
        proc = self.assert_refused(" -> b\n", " -> b")
        self.assertFalse((self.tmp / "instance" / "b").exists(),
                         "an empty source copied something:\n" + proc.stdout)

    def test_a_source_with_a_dot_dot_segment_is_refused(self):
        self.assert_refused("../../etc/hosts -> hosts\n", "../../etc/hosts -> hosts")
        self.assertFalse((self.tmp / "instance" / "hosts").exists())

    def test_more_than_one_separator_is_refused(self):
        """`x -> y -> z` split at the FIRST arrow for the source and the LAST for the
        destination — `x` into `z`, with `y` silently ignored."""
        proc = self.assert_refused("x -> dir/y -> z\n", "x -> dir/y -> z")
        self.assertFalse((self.tmp / "instance" / "z").exists(),
                         "a three-field line copied something:\n" + proc.stdout)

    def test_a_path_with_a_space_still_works_on_both_sides(self):
        """The grammar is a validator, not a tokenizer: the separator is ` -> `, so a
        space INSIDE either path is ordinary and must survive sync and --check."""
        manifest = "dir/with space.txt -> vendor/with space.txt\n"
        proc = self.run_sync(manifest)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        landed = self.tmp / "instance" / "vendor" / "with space.txt"
        self.assertTrue(landed.is_file(), "a spaced path did not vendor:\n" + proc.stdout)
        self.assertEqual(landed.read_text(), "core spaced\n")
        check = self.run_sync(manifest, "--check")
        self.assertEqual(check.returncode, 0, check.stdout + check.stderr)
        landed.write_text("local edit\n")
        drift = self.run_sync(manifest, "--check")
        self.assertNotEqual(drift.returncode, 0, drift.stdout)
        self.assertIn("DRIFT: vendor/with space.txt", drift.stdout)

    def test_every_bad_line_is_reported_not_just_the_first(self):
        """One run, one list. An operator who fixes the manifest a line at a time because
        the script stops at the first fault is an operator running this five times."""
        proc = self.run_sync("a -> ../outside\n a -> /abs\nx -> y -> z\na\n")
        out = proc.stdout + proc.stderr
        self.assertNotEqual(proc.returncode, 0, out)
        for line in ("a -> ../outside", "a -> /abs", "x -> y -> z"):
            self.assertIn(line, out, f"the run did not report {line!r}:\n" + out)
        self.assertTrue((self.tmp / "instance" / "a").is_file(),
                        "the good line after three bad ones never ran:\n" + out)


class VaultModuleVendoringTest(unittest.TestCase):
    """The `tofu` tree holds instance-owned state core knows nothing about.
    The sync arm does `rm -rf "$dst"` before it copies, so a manifest line naming
    the DIRECTORY would delete that state on every `make init` (which runs core-sync
    first) and on every pin bump — and `--check` would report permanent DRIFT for
    files core does not ship.

    That is why tofu/*.tf is enumerated file by file today. These tests are the
    guard: the reasoning lives in a comment nobody re-reads, the assertion does not.

    The vault module itself left core with the sops plane, so the two tests that
    pinned its vendoring are retired below with it: their expectations are wrong
    on purpose now, and the retirement assertions that replace them fail if the
    tooling comes back."""

    ROOT = pathlib.Path(__file__).resolve().parent.parent

    def manifest_lines(self) -> list:
        return [ln.strip() for ln in (self.ROOT / "core-manifest.txt").read_text().splitlines()
                if ln.strip() and not ln.strip().startswith("#")]

    def test_no_manifest_line_covers_a_tofu_module_directory(self):
        """`tofu` or anything it sits under. Any of them would take the
        state file with it."""
        forbidden = {"tofu", ".", "/"}
        named = {ln.split(" -> ")[0] for ln in self.manifest_lines()}
        self.assertEqual(named & forbidden, set(),
                         "core-manifest.txt names a tofu module DIRECTORY. core-sync.sh "
                         "does `rm -rf` on a destination before copying, so that line "
                         "deletes the module's terraform.tfstate and its generated "
                         "instance.auto.tfvars on every sync.")

    def test_the_vault_tooling_is_gone_from_core(self):
        """Every retired path is absent from the tree."""
        present = [p for p in RETIRED_PATHS if (self.ROOT / p).exists()]
        self.assertEqual(present, [],
                         "vault bootstrap tooling still in core: " + ", ".join(present))

    def test_the_manifest_vendors_none_of_it(self):
        """No manifest line names retired tooling, whole or in part."""
        named = set(self.manifest_lines())
        hits = [ln for ln in named
                if ln in RETIRED_PATHS
                or any(ln == p or ln.startswith(p + "/") for p in RETIRED_PATHS)]
        self.assertEqual(hits, [],
                         "core-manifest.txt still vendors retired vault tooling")


class ProvisioningTreeVendoringTest(unittest.TestCase):
    """`template/provisioning -> provisioning` vendors the UNRENDERED source tree.

    Every core release used to need a hand copy of the staged orbitd files into the
    instance's provisioning/ tree, because nothing vendored it. The manifest now maps
    the whole source tree across, so a pin bump carries it. This drives the REAL
    script over a fixture core carrying that line, both directions: a matching tree
    checks clean, and a touched file under provisioning/ is DRIFT.
    """

    REF = "vendor-test"
    ROOT = pathlib.Path(__file__).resolve().parent.parent

    def setUp(self):
        self.tmp = pathlib.Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def test_the_manifest_maps_the_source_tree(self):
        """The line the fixture below mirrors. core-sync.sh reads the PINNED core's
        own manifest, so without this line no pin bump carries provisioning/ and
        every release is a hand copy again."""
        lines = [ln.strip()
                 for ln in (self.ROOT / "core-manifest.txt").read_text().splitlines()
                 if ln.strip() and not ln.strip().startswith("#")]
        self.assertIn("template/provisioning -> provisioning", lines)

    def fixture(self) -> tuple:
        """A fixture core with a staged orbitd file, and an instance locked to it."""
        core = self.tmp / "core"
        staged = (core / "template" / "provisioning" / "clusters" / "platform"
                  / "config" / "orbitd")
        staged.mkdir(parents=True)
        (staged / "namespace.yaml").write_text(
            "kind: Namespace\nmetadata:\n  name: orbit\n")
        (core / "core-manifest.txt").write_text(
            "template/provisioning -> provisioning\n")
        subprocess.run(GIT + ["init", "-q", "-b", self.REF], cwd=core, check=True)
        subprocess.run(GIT + ["add", "-A"], cwd=core, check=True,
                       capture_output=True)
        subprocess.run(GIT + ["commit", "-q", "--allow-empty", "-m",
                              "provisioning under test"], cwd=core, check=True,
                       capture_output=True)
        inst = self.tmp / "instance"
        inst.mkdir()
        (inst / "orbit-core.lock").write_text(f"url: {core}\nref: {self.REF}\n")
        nested = (inst / "provisioning" / "clusters" / "platform"
                  / "config" / "orbitd" / "namespace.yaml")
        return inst, nested

    def run_sync(self, inst: pathlib.Path, *args) -> subprocess.CompletedProcess:
        return subprocess.run(["bash", str(SYNC), *args], cwd=inst,
                              capture_output=True, text=True)

    def test_the_source_tree_vendors_and_checks_clean(self):
        inst, nested = self.fixture()
        proc = self.run_sync(inst)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertTrue(nested.is_file(),
                        "the staged orbitd file did not vendor:\n" + proc.stdout)
        check = self.run_sync(inst, "--check")
        self.assertEqual(check.returncode, 0, check.stdout + check.stderr)
        self.assertIn(f"vendored core matches {self.REF}",
                      check.stdout.splitlines(),
                      "the clean check did not report its exact match line:\n"
                      + check.stdout)

    def test_a_touched_provisioning_file_is_drift(self):
        inst, nested = self.fixture()
        self.assertEqual(self.run_sync(inst).returncode, 0)
        self.assertEqual(self.run_sync(inst, "--check").returncode, 0)
        nested.write_text(nested.read_text() + "# local edit\n")
        drift = self.run_sync(inst, "--check")
        out = drift.stdout + drift.stderr
        self.assertNotEqual(drift.returncode, 0, out)
        self.assertIn(
            f"DRIFT: provisioning differs from core@{self.REF} "
            f"(template/provisioning)",
            out.splitlines(),
            "the drift is not reported against the instance path:\n" + out)


class TemplateArgocdRemovedTest(unittest.TestCase):
    """ArgoCD's own pieces are gone from the core template and from the vendor list.

    The platform layer is applied from the desk; an instance that still carried
    the retired applier's config tree or root Application would re-vendor them
    on every pin bump.
    """

    ROOT = pathlib.Path(__file__).resolve().parent.parent

    def test_core_template_ships_no_argocd_config_or_root_application(self):
        lines = [ln.strip()
                 for ln in (self.ROOT / "core-manifest.txt").read_text().splitlines()
                 if ln.strip() and not ln.strip().startswith("#")]
        hits = [ln for ln in lines if "argocd" in ln]
        self.assertEqual(hits, [],
                         "core-manifest.txt still names an argocd path: "
                         + ", ".join(hits))
        template = self.ROOT / "template"
        argocd_dirs = [p for p in template.rglob("*")
                       if p.is_dir() and p.name == "argocd"
                       and p.parent.name == "config"]
        self.assertEqual(
            argocd_dirs, [],
            "template still has the retired config tree: "
            + ", ".join(str(p.relative_to(template)) for p in argocd_dirs))
        roots = [p for p in template.rglob("root.yaml")
                 if p.parent.name == "clusters"]
        self.assertEqual(
            roots, [],
            "template still has the retired root Application: "
            + ", ".join(str(p.relative_to(template)) for p in roots))


def _git_core(path: pathlib.Path, manifest: str, files: dict) -> None:
    path.mkdir(parents=True)
    for rel, body in files.items():
        p = path / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(body)
    (path / "core-manifest.txt").write_text(manifest)
    subprocess.run(GIT + ["init", "-q", "-b", "vendor-test"], cwd=path, check=True)
    subprocess.run(GIT + ["add", "-A"], cwd=path, check=True, capture_output=True)
    subprocess.run(GIT + ["commit", "-q", "-m", "core"], cwd=path, check=True,
                   capture_output=True)


def _commit_core(path: pathlib.Path, manifest: str) -> None:
    (path / "core-manifest.txt").write_text(manifest)
    subprocess.run(GIT + ["add", "-A"], cwd=path, check=True, capture_output=True)
    subprocess.run(GIT + ["commit", "-q", "-m", "drop"], cwd=path, check=True,
                   capture_output=True)


def _run_sync(inst: pathlib.Path, *args) -> subprocess.CompletedProcess:
    return subprocess.run(["bash", str(SYNC), *args], cwd=inst,
                          capture_output=True, text=True)


def test_a_path_dropped_from_the_manifest_is_deleted(tmp_path):
    """A dest the previous manifest listed and the new one does not is removed.

    --check reports the leftover as drift before the delete; a file the
    previous manifest never named is left alone through both arms.
    """
    core = tmp_path / "core"
    inst = tmp_path / "instance"
    _git_core(core, "core-manifest.txt\nkeep.txt\ngone.txt\n",
              {"keep.txt": "keep\n", "gone.txt": "gone\n"})
    inst.mkdir()
    (inst / "orbit-core.lock").write_text(f"url: {core}\nref: vendor-test\n")
    (inst / "never.txt").write_text("untouched\n")
    proc = _run_sync(inst)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert (inst / "keep.txt").read_text() == "keep\n"
    assert (inst / "gone.txt").read_text() == "gone\n"
    assert (inst / "never.txt").read_text() == "untouched\n"

    _commit_core(core, "core-manifest.txt\nkeep.txt\n")
    check = _run_sync(inst, "--check")
    out = check.stdout + check.stderr
    assert check.returncode != 0, out
    assert "DRIFT: gone.txt leftover (dropped from core@vendor-test)" in out.splitlines(), out
    assert (inst / "gone.txt").is_file(), "check must not delete the leftover"
    assert (inst / "never.txt").read_text() == "untouched\n"

    proc = _run_sync(inst)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert not (inst / "gone.txt").exists(), "dropped dest survived the pin bump"
    assert (inst / "keep.txt").read_text() == "keep\n"
    assert (inst / "never.txt").read_text() == "untouched\n"

    clean = _run_sync(inst, "--check")
    assert clean.returncode == 0, clean.stdout + clean.stderr
    assert (inst / "never.txt").read_text() == "untouched\n"


if __name__ == "__main__":
    unittest.main(verbosity=2)


class VendoredEditsTest(ManifestGrammarTest):
    """A1 (area-1 sweep round 9): `make init` runs core-sync first, which replaced a
    vendored path an operator had edited; uncommitted changes under a destination now
    refuse the sync, by path."""

    def test_uncommitted_changes_under_a_vendored_path_refuse_the_sync(self):
        proc = self.run_sync("a\ndir\n")
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        inst = self.tmp / "instance"
        subprocess.run(GIT + ["init", "-q", "-b", "main"], cwd=inst, check=True)
        subprocess.run(GIT + ["add", "-A"], cwd=inst, check=True, capture_output=True)
        subprocess.run(GIT + ["commit", "-q", "-m", "vendored"], cwd=inst, check=True, capture_output=True)
        (inst / "dir" / "with space.txt").write_text("operator edit PLANTED\n")
        proc = self.run_sync("a\ndir\n")
        self.assertNotEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("REFUSED: dir has uncommitted changes that core-sync would overwrite", proc.stdout)
        self.assertEqual((inst / "dir" / "with space.txt").read_text(), "operator edit PLANTED\n")
        self.assertEqual((inst / "a").read_text(), "core a\n")
        subprocess.run(GIT + ["add", "-A"], cwd=inst, check=True, capture_output=True)
        subprocess.run(GIT + ["commit", "-q", "-m", "edit"], cwd=inst, check=True, capture_output=True)
        proc = self.run_sync("a\ndir\n")
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertEqual((inst / "dir" / "with space.txt").read_text(), "core spaced\n")

    def test_a_rerun_over_its_own_uncommitted_output_is_not_refused(self):
        # V1 §8: `make init` runs core-sync again after an upgrade's `make core-sync`; the
        # vendored output is still uncommitted but identical to core, so nothing is at risk.
        inst = self.tmp / "instance"
        inst.mkdir(exist_ok=True)
        subprocess.run(GIT + ["init", "-q", "-b", "main"], cwd=inst, check=True)
        subprocess.run(GIT + ["commit", "-q", "--allow-empty", "-m", "empty"], cwd=inst, check=True, capture_output=True)
        proc = self.run_sync("a\ndir\n")
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        proc = self.run_sync("a\ndir\n")
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertNotIn("REFUSED", proc.stdout)
        self.assertEqual((inst / "dir" / "with space.txt").read_text(), "core spaced\n")
