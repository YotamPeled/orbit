#!/usr/bin/env python3
"""
Review round 2 of the symlink refusal (r23d). Two things, and a change of method for the second:

- A missing clusters/ is not an unreadable one. Failing closed means refusing what cannot be
  inspected; a tree that does not exist yet is simply rendered, as before the refusal existed.
- Receiver-era text was found in both review rounds because each round's test matched phrases one
  line at a time in a named list of files. This test sweeps every file a reader is sent to for how
  a build becomes a deploy, with comment lines joined so a phrase split across a line break is seen.
"""
import importlib.util
import pathlib
import re
import shutil
import sys
import unittest

HERE = pathlib.Path(__file__).resolve().parent
ROOT = HERE.parent

_SPEC = importlib.util.spec_from_file_location("test_init_no_symlinks", HERE / "test_init_no_symlinks.py")
_NS = importlib.util.module_from_spec(_SPEC)
_BYTECODE, sys.dont_write_bytecode = sys.dont_write_bytecode, True
_SPEC.loader.exec_module(_NS)
sys.dont_write_bytecode = _BYTECODE
_P8 = _NS._P8


class MissingClustersTest(_P8._FixtureCase):
    def test_a_missing_clusters_directory_is_rendered_not_refused(self):
        fx = self.fixture()
        self.run_ok(fx)
        shutil.rmtree(fx.root / "clusters")
        proc = fx.run()
        text = proc.stdout + proc.stderr
        self.assertEqual(proc.returncode, 0, text)
        self.assertNotIn("cannot be listed", text)
        self.assertTrue((fx.root / "clusters" / "platform" / "apps").is_dir())


# How a build became a deploy before phase 8: CI's workflow_run event went to the orbit-hooks
# receiver, which bumped the tag, and Argo CD reconciled it. None of that exists now.
STALE = re.compile(r"hooks receiver|workflow_run|orbit-hooks|argo ?cd reconciles|"
                   r"conclusion: success`? deploys|the receiver (re-derives|drops|refuses|reads)", re.I)

SWEPT = [".claude/skills", "docs/ONBOARDING.md", "docs/DEVELOPMENT.md",
         "template/provisioning/clusters/platform/config/orbitd"]


def _joined(text: str) -> str:
    """The file as one line of prose: comment markers stripped, line breaks collapsed."""
    return " ".join(re.sub(r"^\s*(#|//|\*|>)\s?", "", l).strip() for l in text.splitlines())


class ReceiverSweepTest(unittest.TestCase):
    def test_no_user_facing_file_describes_the_old_deploy_path(self):
        files = []
        for rel in SWEPT:
            p = ROOT / rel
            files += [p] if p.is_file() else [f for f in p.rglob("*") if f.is_file()]
        hits = {}
        for f in files:
            try:
                text = f.read_text()
            except UnicodeDecodeError:
                continue
            found = sorted({m.group(0) for m in STALE.finditer(_joined(text))})
            if found:
                hits[str(f.relative_to(ROOT))] = found
        self.assertEqual(hits, {}, "text still describes the receiver/Argo CD deploy path")


if __name__ == "__main__":
    unittest.main()
