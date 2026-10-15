"""scripts/wait-applied.sh (docs/V1.md §8, S09/S10): the wait an agent cannot do by
sleeping. Driven against a stub `orbit` whose deploy_status answers come from a file the
test rewrites, and a real git repository for the ancestry question."""
from __future__ import annotations

import json
import os
import pathlib
import shutil
import subprocess
import tempfile
import unittest

SCRIPT = pathlib.Path(__file__).resolve().parent / "wait-applied.sh"
GIT = ["git", "-c", "user.email=t@t", "-c", "user.name=t", "-c", "commit.gpgsign=false"]
ORBIT = """#!/usr/bin/env bash
cat "$FAKE_STATUS"
"""


class WaitAppliedTest(unittest.TestCase):
    def setUp(self):
        self.tmp = pathlib.Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.repo = self.tmp / "repo"
        self.repo.mkdir()
        subprocess.run(GIT + ["init", "-q", "-b", "main"], cwd=self.repo, check=True)
        for n in ("one", "two"):
            (self.repo / n).write_text(n)
            subprocess.run(GIT + ["add", "-A"], cwd=self.repo, check=True)
            subprocess.run(GIT + ["commit", "-qm", n], cwd=self.repo, check=True)
        self.first = subprocess.run(["git", "rev-parse", "HEAD~1"], cwd=self.repo, capture_output=True, text=True).stdout.strip()
        self.head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=self.repo, capture_output=True, text=True).stdout.strip()
        bindir = self.tmp / "bin"; bindir.mkdir()
        (bindir / "orbit").write_text(ORBIT, newline="\n"); (bindir / "orbit").chmod(0o755)
        (bindir / "sleep").write_text("#!/bin/sh\nexit 0\n", newline="\n"); (bindir / "sleep").chmod(0o755)
        self.status = self.tmp / "status.json"
        self.env = dict(os.environ, PATH=f"{bindir}{os.pathsep}{os.environ['PATH']}", FAKE_STATUS=str(self.status))

    def answer(self, reconcile):
        self.status.write_text(json.dumps({"project": "p", "env": "dev", "reconcile": reconcile}))

    def run_it(self, *args):
        return subprocess.run(["bash", str(SCRIPT), "p", "dev", *args], cwd=self.repo, env=self.env,
                              capture_output=True, text=True, timeout=60)

    def tick(self, commit, failed=0):
        return {"last_run": "2026-10-05T00:00:00Z", "applied": 1, "failed": failed, "mode": "on",
                "source": {"commit": commit}}

    def test_a_tick_on_a_checkout_containing_head_is_applied(self):
        self.answer(self.tick(self.head))
        proc = self.run_it()
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn(f"APPLIED: p/dev carries {self.head[:12]}", proc.stdout)

    def test_a_tick_on_an_older_checkout_is_not_and_times_out(self):
        self.answer(self.tick(self.first))
        proc = self.run_it("--timeout", "1")
        self.assertEqual(proc.returncode, 1, proc.stdout)
        self.assertIn("not yet on", proc.stdout)
        self.assertIn("TIMEOUT", proc.stdout)

    def test_an_older_commit_asked_for_is_carried_by_a_newer_checkout(self):
        self.answer(self.tick(self.head))
        proc = self.run_it(self.first)
        self.assertEqual(proc.returncode, 0, proc.stdout)

    def test_a_failed_tick_is_reported_not_waited_out(self):
        self.answer(self.tick(self.head, failed=2))
        proc = self.run_it()
        self.assertEqual(proc.returncode, 1, proc.stdout)
        self.assertIn("FAILED on 2 object(s)", proc.stdout)

    def test_a_loop_that_is_off_says_so(self):
        self.answer(None)
        proc = self.run_it()
        self.assertEqual(proc.returncode, 2, proc.stdout)
        self.assertIn("reconcile loop is off", proc.stdout)
        self.answer({**self.tick(self.head), "mode": "off (pair not enabled)"})
        proc = self.run_it()
        self.assertEqual(proc.returncode, 2, proc.stdout)
        self.assertIn("not in ORBITD_APPLY_PAIRS", proc.stdout)


if __name__ == "__main__":
    unittest.main(verbosity=2)
