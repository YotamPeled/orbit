#!/usr/bin/env python3
"""Whose scripts a ship runs (docs/V1.md §8, 2026-10-06). orbitd ships with the GitHub App
key, the registry token and the token signing key in reach, on a fresh clone of the
instance repository. With a core of its own (ORBIT_CORE_DIR, as in the orbitd image) it
runs this build's bump script and nothing out of the clone unless the clone's vendored
core is byte for byte this build's. The instance here is a real scaffold; the remote is a
local bare repository and GitHub is test_ship's loopback fake."""
from __future__ import annotations

import pathlib
import subprocess
import unittest

import yaml

import test_ship
from test_ship import GIT, ROOT, SHIP
from test_verify_platform import build_scaffold

REPO_URL = "https://github.com/fixture-org/fixture-instance.git"
VERSION = "v9.9.9"
CATALOG = {"version": 3, "projects": [{
    "id": "widgets", "name": "Widgets", "repo": "fixture-org/widgets", "hosted": "self",
    "environments": {"prod": {"branch": "main"}},
    "components": [{"name": "web", "kind": "web", "image": "ghcr.io/fixture-org/widgets-web", "port": 8080, "path": "/"}],
}]}


class ShipRunsItsOwnScripts(unittest.TestCase):
    setUp = test_ship.ShipScriptTest.setUp
    ship_env = test_ship.ShipScriptTest.ship_env
    assert_credentials_absent = test_ship.ShipScriptTest.assert_credentials_absent

    def git(self, *args, cwd=None, check=True):
        return subprocess.run(GIT + list(args), cwd=cwd, capture_output=True, text=True, check=check)

    def make_remote(self, pin=VERSION):
        remote, seed = self.tmp / "remote.git", self.tmp / "desk"
        self.git("init", "--bare", "--initial-branch=main", str(remote))
        build_scaffold(seed)
        (seed / "catalog" / "projects.yaml").write_text(yaml.safe_dump(CATALOG, sort_keys=False))
        (seed / "orbit-core.lock").write_text(f"url: https://example.invalid/orbit-core.git\nref: {pin}\n")
        sync = subprocess.run(["python3", "scripts/catalog-sync.py"], cwd=seed, capture_output=True, text=True)
        self.assertEqual(sync.returncode, 0, sync.stdout + sync.stderr)
        with open(self.tmp / ".gitconfig", "a") as fh:
            fh.write(f'[url "{remote}"]\n\tinsteadOf = {REPO_URL}\n')
        self.git("init", "--initial-branch=main", cwd=seed)
        (seed / ".gitignore").write_text("__pycache__/\n")
        self.land(seed, "base")
        self.git("remote", "add", "origin", str(remote), cwd=seed)
        self.git("push", "-q", "origin", "main", cwd=seed)
        return remote, seed

    def land(self, seed, message, push=False):
        self.git("add", "-A", cwd=seed)
        self.git("-c", "user.name=t", "-c", "user.email=t@invalid", "commit", "-q", "-m", message, cwd=seed)
        if push:
            self.git("push", "-q", "origin", "main", cwd=seed)

    def ship(self, seed, core=True):
        env = self.ship_env(seed)
        if core:
            env.update({"ORBIT_CORE_DIR": str(ROOT), "ORBITD_VERSION": VERSION})
        return subprocess.run(["bash", str(SHIP), "widgets", "prod", "web", "main-abcdef0"], cwd=seed,
                              capture_output=True, text=True, env=env)

    def head(self, remote) -> str:
        return self.git("--git-dir", str(remote), "rev-parse", "main").stdout.strip()

    def tags(self, remote) -> dict:
        return yaml.safe_load(self.git("--git-dir", str(remote), "show", "main:catalog/tags.yaml").stdout).get("tags") or {}

    def test_a_clone_that_carries_this_builds_core_ships_as_ever(self):
        remote, seed = self.make_remote()
        base = self.head(remote)
        proc = self.ship(seed)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertEqual(self.tags(remote).get("widgets/prod/web"), "main-abcdef0")
        self.assertEqual(proc.stdout.splitlines()[-1], f"COMMIT {self.head(remote)}")
        changed = self.git("--git-dir", str(remote), "diff", "--name-only", base, "main").stdout.split()
        self.assertTrue(changed and all(p == "catalog/tags.yaml" or p.startswith("clusters/") for p in changed), changed)
        message = self.git("--git-dir", str(remote), "log", "-1", "--format=%B", "main").stdout
        self.assertIn("Orbit-Action: ship", message)
        self.assert_credentials_absent(proc.stdout + proc.stderr)

    def test_a_script_a_commit_brought_is_never_run(self):
        """Each of the three files a ship would run out of the clone, replaced by one that
        leaves a mark: the ship is refused naming the file, the mark is never made, and
        nothing is pushed."""
        for rel in ("scripts/bump-and-push.sh", "scripts/bump-tag.py", "scripts/catalog-sync.py", "scripts/lib-instance.sh"):
            with self.subTest(script=rel):
                self.setUp()
                remote, seed = self.make_remote()
                mark = self.tmp / "ran-the-commits-script"
                body = (f"#!/usr/bin/env bash\ntouch {mark}\n" if rel.endswith(".sh")
                        else f"import pathlib\npathlib.Path({str(mark)!r}).write_text('x')\n")
                (seed / rel).write_text(body)
                self.land(seed, "a commit that brings its own script", push=True)
                head = self.head(remote)
                proc = self.ship(seed)
                self.assertNotEqual(proc.returncode, 0, proc.stdout + proc.stderr)
                self.assertIn("STEP bump FAIL", proc.stdout)
                self.assertIn(f"is not this build's: {rel} differs", proc.stdout + proc.stderr)
                self.assertFalse(mark.exists(), f"the commit's own {rel} ran inside the ship")
                self.assertEqual(self.head(remote), head, "a refused ship pushed something")
                self.assertEqual(self.tags(remote), {})

    def test_a_module_planted_beside_cores_scripts_is_never_imported(self):
        """`python3 scripts/bump-tag.py` looks in scripts/ first: a yaml.py there would be
        the yaml it imports. The ship is refused before anything runs."""
        remote, seed = self.make_remote()
        mark = self.tmp / "imported-the-commits-module"
        (seed / "scripts" / "yaml.py").write_text(f"import pathlib\npathlib.Path({str(mark)!r}).write_text('x')\n")
        self.land(seed, "a module beside core's scripts", push=True)
        head = self.head(remote)
        proc = self.ship(seed)
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("scripts/yaml.py is not a file of this core", proc.stdout + proc.stderr)
        self.assertFalse(mark.exists(), "the planted module was imported")
        self.assertEqual(self.head(remote), head)

    def test_an_instances_own_shell_script_beside_cores_is_its_own_business(self):
        remote, seed = self.make_remote()
        (seed / "scripts" / "our-own.sh").write_text("#!/bin/sh\n")
        self.land(seed, "the instance's own helper", push=True)
        proc = self.ship(seed)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)

    def test_an_instance_pinned_to_another_core_is_refused_with_what_to_do(self):
        remote, seed = self.make_remote(pin="v9.9.8")
        head = self.head(remote)
        proc = self.ship(seed)
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("the instance pins orbit-core v9.9.8 and this build is v9.9.9", proc.stdout)
        self.assertIn("orbit platform apply --app platform-config", proc.stdout)
        self.assertEqual(self.head(remote), head)

    def test_without_a_core_of_its_own_the_clones_script_runs_as_before(self):
        """A bare checkout of core (no ORBIT_CORE_DIR): unchanged, so an instance's own CI
        and the older tests keep their meaning."""
        remote, seed = self.make_remote()
        proc = self.ship(seed, core=False)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertEqual(self.tags(remote).get("widgets/prod/web"), "main-abcdef0")


class CoreSame(unittest.TestCase):
    def test_this_core_is_the_same_as_itself_and_says_one_line_for_a_difference(self):
        import shutil
        import tempfile
        tmp = pathlib.Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        build_scaffold(tmp / "inst")
        run = lambda: subprocess.run(["python3", str(ROOT / "scripts" / "core-same.py"), str(ROOT), str(tmp / "inst")],  # noqa: E731
                                     capture_output=True, text=True)
        ok = run()
        self.assertEqual((ok.returncode, ok.stdout), (0, ""), ok.stdout + ok.stderr)
        target = tmp / "inst" / "scripts" / "ship.sh"
        original = target.read_bytes()
        target.write_bytes(original + b"# x\n")
        self.assertEqual(run().stdout.strip(), "scripts/ship.sh differs")
        target.unlink()
        self.assertEqual(run().stdout.strip(), "scripts/ship.sh is missing")
        target.symlink_to(ROOT / "scripts" / "ship.sh")
        self.assertEqual(run().stdout.strip(), "scripts/ship.sh is a symlink")


if __name__ == "__main__":
    unittest.main()
