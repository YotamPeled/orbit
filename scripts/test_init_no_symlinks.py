#!/usr/bin/env python3
"""
Init never writes or deletes through a symlink — as one invariant, not per step.

Three reviews in a row found a different init step following a symlink under clusters/: the
secrets folder, the retired-file cleanup, then the token fill (which rewrote a file outside the
instance through a symlinked vendored leaf) and the empty-directory sweep (a traceback after
rendering). Checking each step's destinations missed the next step each time. The rule is now:
before init reads config or renders anything, it walks clusters/ and refuses if ANY entry is a
symlink — file, directory or dangling, pointing inside or outside the instance. The refusal names
every offending path, exits nonzero with no traceback, and leaves the instance and anything outside
it byte-identical. An instance tree has no legitimate use for a symlink under clusters/: git
stores the rendered manifests as plain files.

Also here: user-facing text stops describing the deploy receiver phase 8 deleted.
"""
import importlib.util
import os
import pathlib
import shutil
import sys
import tempfile
import unittest

HERE = pathlib.Path(__file__).resolve().parent
ROOT = HERE.parent
_SPEC = importlib.util.spec_from_file_location("test_phase8_removals", HERE / "test_phase8_removals.py")
_P8 = importlib.util.module_from_spec(_SPEC)
_BYTECODE, sys.dont_write_bytecode = sys.dont_write_bytecode, True
_SPEC.loader.exec_module(_P8)
sys.dont_write_bytecode = _BYTECODE
InitFixture = _P8.InitFixture


def _state(path: pathlib.Path) -> dict:
    """Every entry under path, without following links: a link is recorded as its target."""
    out = {}
    for dirpath, dirnames, filenames in os.walk(path, followlinks=False):
        for name in dirnames + filenames:
            p = pathlib.Path(dirpath) / name
            rel = str(p.relative_to(path))
            if p.is_symlink():
                out[rel] = ("link", os.readlink(p))
            elif p.is_file():
                out[rel] = ("file", p.read_bytes())
            else:
                out[rel] = ("dir",)
    return out


class NoSymlinkUnderClustersTest(_P8._FixtureCase):
    def outside(self, files: dict) -> pathlib.Path:
        d = pathlib.Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, d, ignore_errors=True)
        for name, body in files.items():
            (d / name).write_text(body)
        return d

    def initialized(self) -> InitFixture:
        fx = self.fixture()
        self.run_ok(fx)
        return fx

    def assertRefusedUntouched(self, fx, outside, named: str):
        inst_before, out_before = _state(fx.root), (_state(outside) if outside else None)
        proc = fx.run()
        text = proc.stdout + proc.stderr
        self.assertNotEqual(proc.returncode, 0, text)
        self.assertNotIn("Traceback", text)
        self.assertIn(named, text, "the refusal must name the symlink")
        self.assertEqual(_state(fx.root), inst_before, "init changed the instance while refusing")
        if outside is not None:
            self.assertEqual(_state(outside), out_before, "init changed a directory outside the instance")

    def test_a_symlinked_vendored_file_holding_a_token_is_refused(self):
        fx = self.initialized()
        out = self.outside({"v.yaml": "host: {{domains.primary}}\n"})
        link = fx.rendered("platform/apps/vendored.yaml")
        link.symlink_to(out / "v.yaml")
        self.assertRefusedUntouched(fx, out, "platform/apps/vendored.yaml")

    def test_a_symlinked_empty_directory_off_every_destination_is_refused(self):
        fx = self.initialized()
        out = self.outside({})
        fx.rendered("platform/extra").symlink_to(out, target_is_directory=True)
        self.assertRefusedUntouched(fx, out, "platform/extra")

    def test_a_symlink_pointing_inside_the_instance_is_refused(self):
        fx = self.initialized()
        fx.rendered("platform/alias").symlink_to(fx.rendered("platform/apps"), target_is_directory=True)
        self.assertRefusedUntouched(fx, None, "platform/alias")

    def test_a_dangling_symlink_is_refused(self):
        fx = self.initialized()
        fx.rendered("platform/apps/gone.yaml").symlink_to(fx.root.parent / "no-such-file.yaml")
        self.assertRefusedUntouched(fx, None, "platform/apps/gone.yaml")

    def test_clusters_itself_as_a_symlink_is_refused_on_first_init(self):
        fx = self.fixture()
        out = self.outside({"keep.yaml": "k: 1\n"})
        shutil.rmtree(fx.root / "clusters")
        (fx.root / "clusters").symlink_to(out, target_is_directory=True)
        self.assertRefusedUntouched(fx, out, "clusters")

    def test_every_symlink_is_named_not_just_the_first(self):
        fx = self.initialized()
        out = self.outside({"a.yaml": "a: 1\n"})
        fx.rendered("platform/apps/one.yaml").symlink_to(out / "a.yaml")
        fx.rendered("platform/config/two").symlink_to(out, target_is_directory=True)
        proc = fx.run()
        text = proc.stdout + proc.stderr
        self.assertIn("platform/apps/one.yaml", text)
        self.assertIn("platform/config/two", text)

    def test_a_clean_instance_initializes_repeatedly_and_identically(self):
        fx = self.initialized()
        before = _state(fx.root / "clusters")
        self.run_ok(fx)
        self.assertEqual(_state(fx.root / "clusters"), before)


class NoDeployReceiverTextTest(unittest.TestCase):
    """Phase 8 deleted the receiver that turned a finished CI build into a deploy; nothing in
    services/ handles that event now. Text must not tell a reader that path exists."""

    FILES = (".claude/skills/orbit-start/SKILL.md", "services/intake/app.py")
    PHRASES = ("deploy receiver", "hooks receiver", "receiver refuses", "workflow_run event")

    def test_no_file_describes_the_deleted_receiver(self):
        hits = {}
        for rel in self.FILES:
            f = ROOT / rel
            if not f.exists():
                continue
            for n, line in enumerate(f.read_text().splitlines(), 1):
                if any(p in line.lower() for p in self.PHRASES):
                    hits.setdefault(rel, []).append(n)
        self.assertEqual(hits, {}, "text still describes the deleted deploy receiver")


if __name__ == "__main__":
    unittest.main()
