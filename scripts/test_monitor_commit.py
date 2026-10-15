#!/usr/bin/env python3
"""Tests for scripts/monitor-commit.sh: write or remove monitors/<name>.yaml on the instance
repo's main as the GitHub App, the way ship.sh lands a tag. Same fixtures as test_ship.py:
a loopback fake GitHub for the token, a local bare repo as the instance remote."""
from __future__ import annotations

import pathlib
import subprocess
import unittest

import yaml

import test_ship
from test_ship import GIT, ROOT


SCRIPT = ROOT / "scripts" / "monitor-commit.sh"
BODY = {"name": "queue-backlog", "project": "widgets", "env": "prod", "kind": "metric",
        "query": "max(q)", "condition": {"comparison": ">", "threshold": 1, "for": "5m"},
        "severity": "warn", "notify": [], "summary": "Queue backed up"}


class MonitorCommitTest(unittest.TestCase):
    # test_ship's fixture, borrowed method by method through the module: a TestCase class
    # bound in this namespace, under any name, would be collected and run again here.
    setUp = test_ship.ShipScriptTest.setUp
    make_remote = test_ship.ShipScriptTest.make_remote
    ship_env = test_ship.ShipScriptTest.ship_env
    assert_credentials_absent = test_ship.ShipScriptTest.assert_credentials_absent

    def run_script(self, seed, *args, extra=None, xtrace=False):
        env = self.ship_env(seed)
        env.update(extra or {})
        argv = ["bash", "-x", str(SCRIPT), *args] if xtrace else ["bash", str(SCRIPT), *args]
        return subprocess.run(argv, cwd=seed, capture_output=True, text=True, env=env)

    def body_file(self, body=None) -> pathlib.Path:
        path = self.tmp / "monitor.yaml"
        path.write_text(yaml.safe_dump(body or BODY, sort_keys=False))
        return path

    def remote_head(self, remote) -> str:
        return subprocess.run(GIT + ["--git-dir", str(remote), "rev-parse", "main"],
                              capture_output=True, text=True, check=True).stdout.strip()

    def remote_show(self, remote, spec) -> subprocess.CompletedProcess:
        return subprocess.run(GIT + ["--git-dir", str(remote), "show", spec],
                              capture_output=True, text=True)

    def upsert(self, seed, body=None, **kw):
        return self.run_script(seed, "upsert", "queue-backlog",
                               extra={"MONITOR_FILE": str(self.body_file(body)),
                                      "ORBIT_ACTION": "monitor-upsert"}, **kw)

    def test_upsert_lands_the_file_on_main_and_prints_the_commit(self):
        remote, seed = self.make_remote()
        proc = self.upsert(seed)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        head = self.remote_head(remote)
        self.assertIn(f"COMMIT {head}", proc.stdout)
        shown = self.remote_show(remote, "main:monitors/queue-backlog.yaml")
        self.assertEqual(yaml.safe_load(shown.stdout), BODY)
        log = subprocess.run(GIT + ["--git-dir", str(remote), "log", "-1", "--format=%B", "main"],
                             capture_output=True, text=True, check=True).stdout
        self.assertIn("Orbit-Action: monitor-upsert", log)
        self.assertIn("Orbit-Actor: desk-operator", log)
        self.assertIn("Orbit-Role: operator", log)
        self.assert_credentials_absent(proc.stdout + proc.stderr)

    def test_upsert_of_identical_content_makes_no_commit(self):
        remote, seed = self.make_remote()
        self.assertEqual(self.upsert(seed).returncode, 0)
        head = self.remote_head(remote)
        again = self.upsert(seed)
        self.assertEqual(again.returncode, 0, again.stdout + again.stderr)
        self.assertEqual(self.remote_head(remote), head)
        self.assertIn(f"COMMIT {head}", again.stdout)

    def test_delete_removes_the_file(self):
        remote, seed = self.make_remote()
        self.assertEqual(self.upsert(seed).returncode, 0)
        proc = self.run_script(seed, "delete", "queue-backlog",
                               extra={"ORBIT_ACTION": "monitor-delete"})
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn(f"COMMIT {self.remote_head(remote)}", proc.stdout)
        self.assertNotEqual(self.remote_show(remote, "main:monitors/queue-backlog.yaml").returncode, 0)
        log = subprocess.run(GIT + ["--git-dir", str(remote), "log", "-1", "--format=%B", "main"],
                             capture_output=True, text=True, check=True).stdout
        self.assertIn("Orbit-Action: monitor-delete", log)

    def test_delete_of_a_missing_monitor_fails_without_a_commit(self):
        remote, seed = self.make_remote()
        head = self.remote_head(remote)
        proc = self.run_script(seed, "delete", "ghost")
        self.assertNotEqual(proc.returncode, 0)
        self.assertEqual(self.remote_head(remote), head)

    def test_bad_arguments_fail_before_any_token_is_minted(self):
        remote, seed = self.make_remote()
        head = self.remote_head(remote)
        good_file = {"MONITOR_FILE": str(self.body_file())}
        cases = [
            (("upsert", "../escape"), good_file),
            (("upsert", "Bad_Name"), good_file),
            (("upsert", ""), good_file),
            (("rename", "queue-backlog"), good_file),
            (("upsert",), good_file),
            (("upsert", "queue-backlog"), {}),
            (("upsert", "queue-backlog"), {"MONITOR_FILE": str(self.tmp / "missing.yaml")}),
        ]
        for args, extra in cases:
            with self.subTest(args=args, extra=extra):
                proc = self.run_script(seed, *args, extra=extra)
                self.assertNotEqual(proc.returncode, 0)
        self.assertEqual(self.httpd.token_posts, 0)
        self.assertEqual(self.remote_head(remote), head)

    def test_the_monitor_name_must_match_the_body(self):
        remote, seed = self.make_remote()
        head = self.remote_head(remote)
        proc = self.upsert(seed, body={**BODY, "name": "something-else"})
        self.assertNotEqual(proc.returncode, 0)
        self.assertEqual(self.httpd.token_posts, 0)
        self.assertEqual(self.remote_head(remote), head)

    def test_xtrace_never_prints_the_token(self):
        remote, seed = self.make_remote()
        proc = self.upsert(seed, xtrace=True)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assert_credentials_absent(proc.stdout + proc.stderr)
        self.assert_credentials_absent(self.argv_log.read_text() if self.argv_log.exists() else "")

    def test_a_concurrent_commit_on_main_is_kept(self):
        """Main moves between the script's clone and its push: the monitor lands on top and
        the other commit survives."""
        remote, seed = self.make_remote()
        hook = self.tmp / "race-once"
        hook.write_text("")
        other = self.tmp / "other"
        subprocess.run(GIT + ["clone", "-q", str(remote), str(other)], check=True,
                       capture_output=True)
        (other / "catalog" / "tags.yaml").write_text("tags: {widgets/prod/web: t1}\n")
        subprocess.run(GIT + ["commit", "-qam", "concurrent"], cwd=other, check=True,
                       capture_output=True)
        wrapper = self.bindir / "git"
        wrapper.write_text(wrapper.read_text().replace(
            "os.execv(real",
            "race = os.environ.get('RACE_ONCE')\n"
            "if race and os.path.exists(race) and 'push' in sys.argv[1:]:\n"
            "    os.remove(race)\n"
            "    import subprocess as sp\n"
            "    sp.run([real, '-C', os.environ['RACE_REPO'], 'push', '-q', 'origin', 'main'], check=True)\n"
            "os.execv(real", 1))
        proc = self.run_script(seed, "upsert", "queue-backlog", extra={
                "MONITOR_FILE": str(self.body_file()), "RACE_ONCE": str(hook),
                "RACE_REPO": str(other)})
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertFalse(hook.exists(), "the race hook never fired")
        tags = self.remote_show(remote, "main:catalog/tags.yaml").stdout
        self.assertIn("t1", tags)
        self.assertEqual(self.remote_show(remote, "main:monitors/queue-backlog.yaml").returncode, 0)
        self.assertIn(f"COMMIT {self.remote_head(remote)}", proc.stdout)

    # ---------------------------------------------------------------- review round 1

    def seed_commit(self, seed, message):
        subprocess.run(GIT + ["add", "-A"], cwd=seed, check=True, capture_output=True)
        subprocess.run(GIT + ["commit", "-q", "-m", message], cwd=seed, check=True,
                       capture_output=True)
        subprocess.run(GIT + ["push", "-q", "origin", "HEAD:main"], cwd=seed, check=True,
                       capture_output=True)

    def test_a_symlinked_monitor_file_is_refused_and_its_target_untouched(self):
        remote, seed = self.make_remote()
        outside = self.tmp / "outside.yaml"
        outside.write_text("keep: me\n")
        (seed / "monitors").mkdir()
        (seed / "monitors" / "queue-backlog.yaml").symlink_to(outside)
        self.seed_commit(seed, "tracked symlink")
        head = self.remote_head(remote)
        proc = self.upsert(seed)
        self.assertNotEqual(proc.returncode, 0, proc.stdout)
        self.assertEqual(outside.read_text(), "keep: me\n")
        self.assertEqual(self.remote_head(remote), head)

    def test_a_symlinked_monitors_directory_is_refused(self):
        remote, seed = self.make_remote()
        elsewhere = self.tmp / "elsewhere"
        elsewhere.mkdir()
        (seed / "monitors").symlink_to(elsewhere)
        self.seed_commit(seed, "tracked dir symlink")
        head = self.remote_head(remote)
        proc = self.upsert(seed)
        self.assertNotEqual(proc.returncode, 0, proc.stdout)
        self.assertEqual(list(elsewhere.iterdir()), [])
        self.assertEqual(self.remote_head(remote), head)

    def test_a_file_with_keys_outside_the_monitor_schema_is_refused_before_any_token(self):
        remote, seed = self.make_remote()
        head = self.remote_head(remote)
        proc = self.upsert(seed, body={**BODY, "password": "FAKE_SECRET"})
        self.assertNotEqual(proc.returncode, 0)
        self.assertEqual(self.httpd.token_posts, 0)
        self.assertEqual(self.remote_head(remote), head)
        self.assertNotIn("FAKE_SECRET", proc.stdout + proc.stderr)

    def test_a_symlinked_monitor_file_source_is_refused(self):
        remote, seed = self.make_remote()
        real = self.body_file()
        link = self.tmp / "link.yaml"
        link.symlink_to(real)
        proc = self.run_script(seed, "upsert", "queue-backlog", extra={"MONITOR_FILE": str(link)})
        self.assertNotEqual(proc.returncode, 0)
        self.assertEqual(self.httpd.token_posts, 0)

    def test_the_edit_lands_on_main_when_the_remote_default_branch_is_elsewhere(self):
        remote, seed = self.make_remote()
        subprocess.run(GIT + ["checkout", "-q", "-b", "other"], cwd=seed, check=True, capture_output=True)
        (seed / "monitors").mkdir()
        (seed / "monitors" / "queue-backlog.yaml").write_text(self.body_file().read_text())
        subprocess.run(GIT + ["add", "-A"], cwd=seed, check=True, capture_output=True)
        subprocess.run(GIT + ["commit", "-q", "-m", "other branch"], cwd=seed, check=True, capture_output=True)
        subprocess.run(GIT + ["push", "-q", "origin", "other"], cwd=seed, check=True, capture_output=True)
        subprocess.run(GIT + ["--git-dir", str(remote), "symbolic-ref", "HEAD", "refs/heads/other"],
                       check=True, capture_output=True)
        subprocess.run(GIT + ["checkout", "-q", "main"], cwd=seed, check=True, capture_output=True)
        proc = self.upsert(seed)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        main = self.remote_head(remote)
        self.assertIn(f"COMMIT {main}", proc.stdout)
        self.assertEqual(self.remote_show(remote, "main:monitors/queue-backlog.yaml").returncode, 0)


    def test_a_main_override_in_the_environment_is_ignored(self):
        remote, seed = self.make_remote()
        subprocess.run(GIT + ["checkout", "-q", "-b", "other"], cwd=seed, check=True, capture_output=True)
        subprocess.run(GIT + ["push", "-q", "origin", "other"], cwd=seed, check=True, capture_output=True)
        subprocess.run(GIT + ["checkout", "-q", "main"], cwd=seed, check=True, capture_output=True)
        other_before = subprocess.run(GIT + ["--git-dir", str(remote), "rev-parse", "other"],
                                      capture_output=True, text=True, check=True).stdout.strip()
        proc = self.run_script(seed, "upsert", "queue-backlog",
                               extra={"MONITOR_FILE": str(self.body_file()),
                                      "ORBIT_ACTION": "monitor-upsert", "MAIN": "other"})
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertEqual(self.remote_show(remote, "main:monitors/queue-backlog.yaml").returncode, 0)
        other_after = subprocess.run(GIT + ["--git-dir", str(remote), "rev-parse", "other"],
                                     capture_output=True, text=True, check=True).stdout.strip()
        self.assertEqual(other_after, other_before)

    def test_a_directory_at_the_monitor_path_is_refused(self):
        remote, seed = self.make_remote()
        d = seed / "monitors" / "queue-backlog.yaml"
        d.mkdir(parents=True)
        (d / "keep.txt").write_text("keep\n")
        subprocess.run(GIT + ["add", "-A"], cwd=seed, check=True, capture_output=True)
        subprocess.run(GIT + ["commit", "-q", "-m", "a directory where the file goes"], cwd=seed,
                       check=True, capture_output=True)
        subprocess.run(GIT + ["push", "-q", "origin", "main"], cwd=seed, check=True, capture_output=True)
        before = self.remote_head(remote)
        proc = self.upsert(seed)
        self.assertNotEqual(proc.returncode, 0, proc.stdout)
        self.assertNotIn("COMMIT", proc.stdout)
        self.assertEqual(self.remote_head(remote), before)

if __name__ == "__main__":
    unittest.main()
