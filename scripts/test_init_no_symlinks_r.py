#!/usr/bin/env python3
"""
Review round 1 of the symlink refusal (r23c): the walk that looks for symlinks under clusters/
silently skipped a directory it could not list, so a link inside it was still written through.
The guard must fail closed: a directory under clusters/ that init cannot inspect is refused by
name, before anything is read or written, whether or not it hides a link.

Also here: receiver-era text the round-1 text test could not see.
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

_SPEC = importlib.util.spec_from_file_location("test_init_no_symlinks", HERE / "test_init_no_symlinks.py")
_NS = importlib.util.module_from_spec(_SPEC)
_BYTECODE, sys.dont_write_bytecode = sys.dont_write_bytecode, True
_SPEC.loader.exec_module(_NS)
sys.dont_write_bytecode = _BYTECODE
_P8 = _NS._P8


@unittest.skipIf(hasattr(os, "geteuid") and os.geteuid() == 0, "root ignores directory modes")
class UnreadableDirectoryTest(_P8._FixtureCase):
    def initialized(self):
        fx = self.fixture()
        self.run_ok(fx)
        return fx

    def lock(self, d: pathlib.Path) -> None:
        os.chmod(d, 0o311)                      # write and search, no listing
        self.addCleanup(os.chmod, d, 0o755)     # runs before the fixture's rmtree

    def test_a_link_inside_an_unlistable_directory_is_not_written_through(self):
        fx = self.initialized()
        outside = pathlib.Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, outside, ignore_errors=True)
        victim = outside / "victim.yaml"
        victim.write_text("OUTSIDE ORIGINAL\n")
        dst = fx.rendered("platform/apps/kube-prometheus-stack.yaml")
        dst.unlink()
        dst.symlink_to(victim)
        self.lock(dst.parent)
        proc = fx.run()
        text = proc.stdout + proc.stderr
        self.assertNotEqual(proc.returncode, 0, text)
        self.assertNotIn("Traceback", text)
        self.assertIn("platform/apps", text, "the refusal must name the directory it could not list")
        self.assertEqual(victim.read_text(), "OUTSIDE ORIGINAL\n", "init wrote outside the instance")

    def test_an_unlistable_directory_is_refused_even_without_a_link(self):
        fx = self.initialized()
        d = fx.rendered("platform/config")
        self.lock(d)
        proc = fx.run()
        text = proc.stdout + proc.stderr
        self.assertNotEqual(proc.returncode, 0, text)
        self.assertNotIn("Traceback", text)
        self.assertIn("platform/config", text)


class ReceiverTextTest(unittest.TestCase):
    """Phrases, per file, that still describe the deleted CI-event receiver."""
    PHRASES = {
        ".claude/skills/orbit-start/SKILL.md": (
            "finished-build event", "notifies nobody", "written by ci", "only a success run deploys"),
        "docs/DEVELOPMENT.md": ("orbit-hooks", "workflow_run"),
        "template/provisioning/clusters/platform/config/orbitd/deployment.yaml": ("hooks receiver",),
        "template/provisioning/clusters/platform/config/orbitd/externalsecret.yaml": ("hooks receiver",),
    }

    def test_no_file_still_describes_the_receiver(self):
        hits = {}
        for rel, phrases in self.PHRASES.items():
            f = ROOT / rel
            if not f.exists():
                continue
            for n, line in enumerate(f.read_text().splitlines(), 1):
                if any(p in line.lower() for p in phrases):
                    hits.setdefault(rel, []).append(n)
        self.assertEqual(hits, {}, "text still describes the deleted receiver")


if __name__ == "__main__":
    unittest.main()
