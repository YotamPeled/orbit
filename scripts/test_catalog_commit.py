#!/usr/bin/env python3
"""Tests for scripts/catalog-commit.sh: replace or remove one project's catalog entry on
the instance repo's main as the GitHub App (docs/V1.md M2c, the orbit_catalog_set and
orbit_project_remove tools). The instance is a real one, scaffolded from the template; the
remote is a local bare repo and GitHub is test_ship's loopback fake."""
from __future__ import annotations

import json
import pathlib
import subprocess
import unittest

import yaml

import test_ship
from test_ship import GIT, ROOT
from test_verify_platform import build_scaffold

SCRIPT = ROOT / "scripts" / "catalog-commit.sh"
REPO_URL = "https://github.com/fixture-org/fixture-instance.git"
CATALOG = """version: 3

projects:
  # widgets: the one that gets edited
  - id: widgets
    name: Widgets
    hosted: self
    environments:
      prod: {}
      dev:
        enabled: true
    components:
      - name: web
        kind: web
        image: example/web
        port: 8080
        path: /

  # bare: must never move
  - id: bare
    name: Bare
    hosted: self
    environments:
      prod: {}
    components:
      - name: web
        kind: web
        image: example/web
        port: 8080
        path: /
"""


class CatalogCommitTest(unittest.TestCase):
    setUp = test_ship.ShipScriptTest.setUp
    ship_env = test_ship.ShipScriptTest.ship_env
    assert_credentials_absent = test_ship.ShipScriptTest.assert_credentials_absent

    def git(self, *args, cwd=None, check=True):
        return subprocess.run(GIT + list(args), cwd=cwd, capture_output=True, text=True, check=check)

    def make_remote(self):
        remote, seed = self.tmp / "remote.git", self.tmp / "desk"
        self.git("init", "--bare", "--initial-branch=main", str(remote))
        build_scaffold(seed)
        (seed / "catalog" / "projects.yaml").write_text(CATALOG, encoding="utf-8")
        sync = subprocess.run(["python3", "scripts/catalog-sync.py"], cwd=seed, capture_output=True, text=True)
        self.assertEqual(sync.returncode, 0, sync.stdout + sync.stderr)
        with open(self.tmp / ".gitconfig", "a") as fh:
            fh.write(f'[url "{remote}"]\n\tinsteadOf = {REPO_URL}\n')
        self.git("init", "--initial-branch=main", cwd=seed)
        self.git("add", "-A", cwd=seed)
        self.git("-c", "user.name=t", "-c", "user.email=t@invalid", "commit", "-q", "-m", "base", cwd=seed)
        self.git("remote", "add", "origin", str(remote), cwd=seed)
        self.git("push", "-q", "origin", "main", cwd=seed)
        return remote, seed

    def fake_check(self, *, passes: bool, sentence: str = "") -> pathlib.Path:
        """A stand-in for `orbit` that records its arguments and answers as the gate's
        chain would: silent and 0, or `orbit check: <name>: <sentence>` on stderr and 1."""
        path, log = self.tmp / "fake-orbit", self.tmp / "fake-orbit.log"
        path.write_text(
            "#!/usr/bin/env bash\n"
            f"printf '%s\\n' \"$*\" >> '{log}'\n"
            "[ -f \"$3/catalog/projects.yaml\" ] || { echo 'not a checkout' >&2; exit 2; }\n"
            + ("exit 0\n" if passes else f"echo 'FAIL  fit'\necho 'orbit check: fit: {sentence}' >&2\nexit 1\n"),
            encoding="utf-8")
        path.chmod(0o755)
        return path

    def run_script(self, seed, *args, entry=None, role=None, check=None, precheck=False):
        env = self.ship_env(seed)
        if check is not None:
            env["ORBIT_CHECK_BIN"] = str(check)
        if precheck:
            env["ORBIT_CATALOG_PRECHECK"] = "1"
        if entry is not None:
            path = self.tmp / "entry.json"
            path.write_text(entry if isinstance(entry, str) else json.dumps(entry), encoding="utf-8")
            env["CATALOG_ENTRY_FILE"] = str(path)
        if role:
            env["ORBIT_ROLE"] = role
        return subprocess.run(["bash", str(SCRIPT), *args], cwd=seed, capture_output=True, text=True, env=env)

    def head(self, remote) -> str:
        return self.git("--git-dir", str(remote), "rev-parse", "main").stdout.strip()

    def count(self, remote) -> int:
        return int(self.git("--git-dir", str(remote), "rev-list", "--count", "main").stdout)

    def show(self, remote, path, rev="main") -> str:
        return self.git("--git-dir", str(remote), "show", f"{rev}:{path}").stdout

    def files(self, remote, rev="main") -> set:
        return set(self.git("--git-dir", str(remote), "ls-tree", "-r", "--name-only", rev).stdout.split())

    def entry(self, remote, project):
        return next(p for p in yaml.safe_load(self.show(remote, "catalog/projects.yaml"))["projects"] if p["id"] == project)

    def test_set_lands_one_commit_with_the_trailers_and_the_regenerated_tree(self):
        remote, seed = self.make_remote()
        base, commits = self.head(remote), self.count(remote)
        new = self.entry(remote, "widgets")
        new["components"][0]["replicas"] = 2
        proc = self.run_script(seed, "set", "widgets", entry=new)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        head = self.head(remote)
        self.assertIn(f"COMMIT {head}", proc.stdout)
        self.assertEqual(self.count(remote), commits + 1, "one change is one commit")
        message = self.git("--git-dir", str(remote), "log", "-1", "--format=%B", "main").stdout
        self.assertTrue(message.startswith("catalog: set widgets"))
        for trailer in ("Orbit-Action: catalog-set", "Orbit-Actor: desk-operator", "Orbit-Role: operator", "Orbit-Project: widgets"):
            self.assertIn(trailer, message)
        self.assertEqual(self.entry(remote, "widgets"), new)
        catalog = self.show(remote, "catalog/projects.yaml")
        self.assertIn("# widgets: the one that gets edited", catalog)
        self.assertIn("# bare: must never move", catalog)
        changed = set(self.git("--git-dir", str(remote), "diff", "--name-only", base, head).stdout.split())
        self.assertIn("catalog/projects.yaml", changed)
        self.assertTrue({"clusters/apps/widgets-prod.yaml", "clusters/apps/widgets-dev.yaml"} <= changed,
                        "the generated manifests follow the catalog in the same commit")
        self.assertFalse([p for p in changed if "bare" in p], "another project's files moved")
        prod = yaml.safe_load(self.show(remote, "clusters/apps/widgets-prod.yaml"))
        self.assertEqual(prod["spec"]["source"]["helm"]["valuesObject"]["components"][0]["replicas"], 2)
        self.assert_credentials_absent(proc.stdout + proc.stderr)

    def test_an_entry_that_changes_nothing_makes_no_commit(self):
        remote, seed = self.make_remote()
        head = self.head(remote)
        proc = self.run_script(seed, "set", "widgets", entry=self.entry(remote, "widgets"), role="admin")
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("UNCHANGED widgets", proc.stdout)
        self.assertIn(f"COMMIT {head}", proc.stdout)
        self.assertEqual(self.head(remote), head)

    def test_an_entry_the_generator_refuses_is_refused_in_its_sentence_and_nothing_is_pushed(self):
        remote, seed = self.make_remote()
        head = self.head(remote)
        bad = self.entry(remote, "widgets")
        bad["components"][0]["kind"] = "daemon"
        proc = self.run_script(seed, "set", "widgets", entry=bad)
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("STEP render FAIL", proc.stdout)
        self.assertIn("kind", proc.stdout)
        self.assertEqual(self.head(remote), head)

    def test_what_the_gate_would_refuse_is_refused_before_the_push(self):
        """QA of v1.0.0, F26: an entry the generator accepts and the gate refuses used to
        land on main, where it was refused, and a refused main blocks every deploy."""
        remote, seed = self.make_remote()
        head = self.head(remote)
        new = self.entry(remote, "widgets")
        new["components"][0]["memory"] = "64Gi"
        check = self.fake_check(passes=False, sentence="the catalog reserves 65536 Mi; the node has 15000 Mi to give")
        proc = self.run_script(seed, "set", "widgets", entry=new, check=check, precheck=True)
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("STEP check FAIL", proc.stdout)
        self.assertIn("fit: the catalog reserves 65536 Mi; the node has 15000 Mi to give", proc.stdout)
        self.assertNotIn("orbit check: ", proc.stdout.splitlines()[-1])
        self.assertNotIn("COMMIT", proc.stdout)
        self.assertEqual(self.head(remote), head, "a commit the gate would refuse was pushed")
        called = (self.tmp / "fake-orbit.log").read_text().split()
        self.assertEqual(called[:2], ["check", "all"])

    def test_what_the_gate_accepts_lands_after_its_checks_ran_on_the_edited_tree(self):
        remote, seed = self.make_remote()
        commits = self.count(remote)
        new = self.entry(remote, "widgets")
        new["components"][0]["replicas"] = 2
        proc = self.run_script(seed, "set", "widgets", entry=new, check=self.fake_check(passes=True), precheck=True)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("STEP check OK", proc.stdout)
        self.assertIn(f"COMMIT {self.head(remote)}", proc.stdout)
        self.assertEqual(self.count(remote), commits + 1)
        self.assertEqual(self.entry(remote, "widgets"), new)

    def test_the_checks_are_asked_for_and_cannot_run_so_nothing_is_pushed(self):
        remote, seed = self.make_remote()
        head = self.head(remote)
        new = self.entry(remote, "widgets")
        new["components"][0]["replicas"] = 2
        proc = self.run_script(seed, "set", "widgets", entry=new, check=self.tmp / "no-such-orbit", precheck=True)
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("STEP check FAIL", proc.stdout)
        self.assertEqual(self.head(remote), head)

    def test_check_runs_the_edit_the_generator_and_the_gate_and_pushes_nothing(self):
        """QA of v1.0.0, F22: a dry run that validates. `check` is `set` without the commit."""
        remote, seed = self.make_remote()
        head = self.head(remote)
        new = self.entry(remote, "widgets")
        new["components"][0]["replicas"] = 2
        proc = self.run_script(seed, "check", "widgets", entry=new, check=self.fake_check(passes=True))
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("CHECKED widgets", proc.stdout)
        self.assertNotIn("COMMIT", proc.stdout)
        self.assertEqual(self.head(remote), head)
        # the generator's refusal, and the gate's, each in its own step; still nothing pushed
        bad = self.entry(remote, "widgets")
        bad["components"][0]["kind"] = "daemon"
        proc = self.run_script(seed, "check", "widgets", entry=bad, check=self.fake_check(passes=True))
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("STEP render FAIL", proc.stdout)
        proc = self.run_script(seed, "check", "widgets", entry=new,
                               check=self.fake_check(passes=False, sentence="two components claim one path"))
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("STEP check FAIL", proc.stdout)
        self.assertIn("two components claim one path", proc.stdout)
        # an entry equal to main's is said to be so
        proc = self.run_script(seed, "check", "widgets", entry=self.entry(remote, "widgets"),
                               check=self.fake_check(passes=True))
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("UNCHANGED widgets", proc.stdout)
        self.assertEqual(self.head(remote), head)

    def test_an_entry_for_another_project_or_no_entry_at_all_is_refused_before_a_token_is_minted(self):
        remote, seed = self.make_remote()
        head = self.head(remote)
        other = self.run_script(seed, "set", "widgets", entry=dict(self.entry(remote, "bare")))
        self.assertNotEqual(other.returncode, 0)
        self.assertIn("STEP edit FAIL", other.stdout)
        self.assertIn("an entry's id cannot change", other.stdout)
        missing = self.run_script(seed, "set", "widgets")
        self.assertNotEqual(missing.returncode, 0)
        self.assertIn("STEP args FAIL", missing.stdout)
        self.assertIn("CATALOG_ENTRY_FILE is not set", missing.stdout)
        for args in (("set", "Widgets; id"), ("drop", "widgets"), ("set",)):
            proc = self.run_script(seed, *args, entry={"id": "x"})
            self.assertNotEqual(proc.returncode, 0)
            self.assertIn("STEP args FAIL", proc.stdout)
            self.assertNotIn("Widgets; id", proc.stdout + proc.stderr)
        self.assertEqual(self.head(remote), head)

    def test_remove_takes_the_entry_and_everything_generated_for_it(self):
        remote, seed = self.make_remote()
        base = self.head(remote)
        proc = self.run_script(seed, "remove", "widgets", role="admin")
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        ids = [p["id"] for p in yaml.safe_load(self.show(remote, "catalog/projects.yaml"))["projects"]]
        self.assertEqual(ids, ["bare"])
        message = self.git("--git-dir", str(remote), "log", "-1", "--format=%B", "main").stdout
        for trailer in ("Orbit-Action: catalog-remove", "Orbit-Role: admin", "Orbit-Project: widgets"):
            self.assertIn(trailer, message)
        before, after = self.files(remote, base), self.files(remote)
        gone = before - after
        self.assertTrue({"clusters/apps/widgets-prod.yaml", "clusters/apps/widgets-dev.yaml"} <= gone, gone)
        self.assertTrue(all("widgets" in p or p.startswith("clusters/apps/tenant-probe-") for p in gone), gone)
        self.assertIn("clusters/apps/bare-prod.yaml", after)
        self.assertEqual(after - before, set())
        namespaces = self.show(remote, "clusters/apps/namespaces.yaml")
        self.assertNotIn("widgets-prod", namespaces)
        self.assertIn("bare-prod", namespaces)

    def test_remove_takes_the_projects_sealed_secrets_and_no_one_elses(self):
        """Sealed files are written by hand, so the generator leaves them: after a removal
        `make verify` asked for Secrets of namespaces that were gone (docs/V1.md §8). The
        directories of the removed project's own pairs go in the same commit; a project
        whose name only begins the same way keeps its own."""
        remote, seed = self.make_remote()
        for ns in ("widgets-prod", "widgets-dev", "bare-prod", "widgets-extra-prod"):
            d = seed / "clusters" / "platform" / "secrets" / ns
            d.mkdir(parents=True, exist_ok=True)
            (d / "ghcr-pull.yaml").write_text("apiVersion: isindir.github.com/v1alpha3\nkind: SopsSecret\n", encoding="utf-8")
        self.git("add", "-A", cwd=seed)
        self.git("-c", "user.name=t", "-c", "user.email=t@invalid", "commit", "-q", "-m", "sealed files", cwd=seed)
        self.git("push", "-q", "origin", "main", cwd=seed)
        proc = self.run_script(seed, "remove", "widgets", role="admin")
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        after = self.files(remote)
        self.assertNotIn("clusters/platform/secrets/widgets-prod/ghcr-pull.yaml", after)
        self.assertNotIn("clusters/platform/secrets/widgets-dev/ghcr-pull.yaml", after)
        self.assertIn("clusters/platform/secrets/bare-prod/ghcr-pull.yaml", after)
        self.assertIn("clusters/platform/secrets/widgets-extra-prod/ghcr-pull.yaml", after)

    def test_remove_takes_the_projects_monitor_files_and_no_one_elses(self):
        """QA of v1.0.0, F40: a removed project's monitors stayed in monitors/, files the
        monitors unit then refuses for ever, with their rules still alerting. A monitor
        file names its project; those of the removed project go in the same commit."""
        remote, seed = self.make_remote()
        monitors = seed / "monitors"
        monitors.mkdir(exist_ok=True)
        for name, project in (("widgets-errors", "widgets"), ("widgets-door", "widgets"), ("bare-door", "bare")):
            (monitors / f"{name}.yaml").write_text(
                f"name: {name}\nproject: {project}\nenv: prod\nkind: metric\nquery: up\n", encoding="utf-8")
        (monitors / "broken.yaml").write_text("{not: [yaml", encoding="utf-8")
        self.git("add", "-A", cwd=seed)
        self.git("-c", "user.name=t", "-c", "user.email=t@invalid", "commit", "-q", "-m", "monitors", cwd=seed)
        self.git("push", "-q", "origin", "main", cwd=seed)
        commits = self.count(remote)
        proc = self.run_script(seed, "remove", "widgets", role="admin")
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertEqual(self.count(remote), commits + 1, "the monitor files go in the removal's one commit")
        after = self.files(remote)
        self.assertNotIn("monitors/widgets-errors.yaml", after)
        self.assertNotIn("monitors/widgets-door.yaml", after)
        self.assertIn("monitors/bare-door.yaml", after)
        self.assertIn("monitors/broken.yaml", after, "a file that names no project is nobody's to delete")
        self.assertIn("MONITOR-REMOVED widgets-door", proc.stdout)
        self.assertIn("MONITOR-REMOVED widgets-errors", proc.stdout)
        self.assertNotIn("MONITOR-REMOVED bare-door", proc.stdout)
        # a `set` never touches monitors/
        new = self.entry(remote, "bare")
        new["components"][0]["replicas"] = 2
        proc = self.run_script(seed, "set", "bare", entry=new)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("monitors/bare-door.yaml", self.files(remote))

    def test_removing_a_project_that_is_not_there_is_refused(self):
        remote, seed = self.make_remote()
        head = self.head(remote)
        proc = self.run_script(seed, "remove", "ghost")
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("project ghost is not in the catalog", proc.stdout)
        self.assertEqual(self.head(remote), head)


if __name__ == "__main__":
    unittest.main()
