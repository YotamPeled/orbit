#!/usr/bin/env python3
"""Tests for scripts/features-commit.sh: switch one feature on the instance repo's main as
the GitHub App (docs/V1.md M2c). The instance is a real one, scaffolded from the template
the way ONBOARDING does, so the commit under test is what the scaffolder and the generator
really render; the remote is a local bare repo and GitHub is test_ship's loopback fake."""
from __future__ import annotations

import pathlib
import subprocess
import unittest

import yaml

import test_ship
from test_ship import GIT, ROOT
from test_verify_platform import build_scaffold

SCRIPT = ROOT / "scripts" / "features-commit.sh"
REPO_URL = "https://github.com/fixture-org/fixture-instance.git"


class FeaturesCommitTest(unittest.TestCase):
    setUp = test_ship.ShipScriptTest.setUp
    ship_env = test_ship.ShipScriptTest.ship_env
    assert_credentials_absent = test_ship.ShipScriptTest.assert_credentials_absent

    def git(self, *args, cwd=None, check=True):
        return subprocess.run(GIT + list(args), cwd=cwd, capture_output=True, text=True, check=check)

    def make_remote(self):
        """A scaffolded instance (every feature on: it has a bucket and no features block),
        in sync, committed and pushed to a bare remote git reaches by the instance's URL."""
        remote, seed = self.tmp / "remote.git", self.tmp / "desk"
        self.git("init", "--bare", "--initial-branch=main", str(remote))
        build_scaffold(seed)
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

    def run_script(self, seed, *args, xtrace=False):
        argv = ["bash", "-x", str(SCRIPT), *args] if xtrace else ["bash", str(SCRIPT), *args]
        return subprocess.run(argv, cwd=seed, capture_output=True, text=True, env=self.ship_env(seed))

    def head(self, remote) -> str:
        return self.git("--git-dir", str(remote), "rev-parse", "main").stdout.strip()

    def count(self, remote) -> int:
        return int(self.git("--git-dir", str(remote), "rev-list", "--count", "main").stdout)

    def show(self, remote, path, rev="main"):
        return self.git("--git-dir", str(remote), "show", f"{rev}:{path}", check=False)

    def files(self, remote, rev="main") -> set:
        return set(self.git("--git-dir", str(remote), "ls-tree", "-r", "--name-only", rev).stdout.split())

    def test_off_lands_one_commit_with_the_ledger_trailers_and_the_tree_without_the_feature(self):
        remote, seed = self.make_remote()
        base, commits = self.head(remote), self.count(remote)
        proc = self.run_script(seed, "traces", "off")
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        head = self.head(remote)
        self.assertIn(f"COMMIT {head}", proc.stdout)
        self.assertEqual(self.count(remote), commits + 1, "one switch is one commit")
        message = self.git("--git-dir", str(remote), "log", "-1", "--format=%B", "main").stdout
        for trailer in ("Orbit-Action: features-set", "Orbit-Actor: desk-operator", "Orbit-Role: admin",
                        "Orbit-Feature: traces=off"):
            self.assertIn(trailer, message)
        config = yaml.safe_load(self.show(remote, "instance/config.yaml").stdout)
        import orbit_features
        optional = [n for n, s in orbit_features.load_registry(ROOT / "template").items() if not s["always"]]
        self.assertEqual(config["features"], {name: name != "traces" for name in optional},
                         "the first switch names every feature at its current state, and changes one")
        self.assertIn("FEATURES " + " ".join(f"{n}={'off' if n == 'traces' else 'on'}" for n in optional), proc.stdout)
        before, after = self.files(remote, base), self.files(remote)
        gone = before - after
        self.assertEqual(gone, {"clusters/platform/apps/tempo.yaml",
                                "clusters/platform/apps/opentelemetry-operator.yaml",
                                "clusters/platform/config/tempo/netpol.yaml"})
        self.assertEqual(after - before, set())
        self.assertIn("clusters/platform/apps/loki.yaml", after)
        rules = self.show(remote, "clusters/platform/monitoring/rules.yaml").stdout
        self.assertNotIn("tempo_ingester_failed_flushes_total", rules)
        self.assertIn("loki_distributor_lines_received_total", rules)
        changed = set(self.git("--git-dir", str(remote), "diff", "--name-only", base, head).stdout.split())
        self.assertTrue(all(p == "instance/config.yaml" or p.startswith("clusters/") for p in changed), changed)
        self.assert_credentials_absent(proc.stdout + proc.stderr)

    def test_on_again_restores_the_rendered_tree_exactly(self):
        remote, seed = self.make_remote()
        base = self.head(remote)
        self.assertEqual(self.run_script(seed, "traces", "off").returncode, 0)
        proc = self.run_script(seed, "traces", "on")
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        changed = self.git("--git-dir", str(remote), "diff", "--name-only", base, self.head(remote)).stdout.split()
        self.assertEqual(changed, ["instance/config.yaml"],
                         "off then on must leave the tree as it was; only the config now names its features")

    def test_a_switch_that_changes_nothing_makes_no_commit(self):
        remote, seed = self.make_remote()
        self.assertEqual(self.run_script(seed, "traces", "off").returncode, 0)
        head = self.head(remote)
        again = self.run_script(seed, "traces", "off")
        self.assertEqual(again.returncode, 0, again.stdout + again.stderr)
        self.assertEqual(self.head(remote), head)
        self.assertIn(f"COMMIT {head}", again.stdout)

    def test_a_switch_the_config_cannot_take_is_refused_in_a_sentence_and_nothing_is_pushed(self):
        remote, seed = self.make_remote()
        head = self.head(remote)
        proc = self.run_script(seed, "nope", "on")
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("STEP edit FAIL", proc.stdout)
        self.assertIn("nope is not a feature this core has", proc.stdout)
        self.assertEqual(self.head(remote), head)
        proc = self.run_script(seed, "core", "off")
        self.assertIn("always on", proc.stdout)
        self.assertEqual(self.head(remote), head)

    def test_bad_arguments_fail_before_any_token_is_minted(self):
        remote, seed = self.make_remote()
        head = self.head(remote)
        for args in (("traces",), ("traces", "maybe"), ("Traces", "on"), ("../x", "on"), ("", "on"),
                     ("traces", "on", "extra")):
            with self.subTest(args=args):
                self.assertNotEqual(self.run_script(seed, *args).returncode, 0)
        self.assertEqual(self.httpd.token_posts, 0)
        self.assertEqual(self.head(remote), head)

    # ---- whose scripts run -----------------------------------------------------------------

    def core_env(self, seed, version):
        env = self.ship_env(seed)
        env.update({"ORBIT_CORE_DIR": str(ROOT), "ORBITD_VERSION": version})
        return env

    def pin(self, seed, remote, ref, *, tamper=None):
        """Commit a core pin (and optionally a tampered scaffolder) to the remote's main."""
        (seed / "orbit-core.lock").write_text(f"url: https://example.invalid/orbit-core.git\nref: {ref}\n")
        if tamper is not None:
            (seed / "scripts" / "instance-init.py").write_text(tamper)
        self.git("add", "-A", cwd=seed)
        self.git("-c", "user.name=t", "-c", "user.email=t@invalid", "commit", "-q", "-m", "pin", cwd=seed)
        self.git("push", "-q", "origin", "main", cwd=seed)

    def test_with_a_core_of_its_own_the_build_runs_its_scripts_not_the_commits(self):
        """The instance repository carries a scaffolder that would leave a mark. The switch
        still renders the right tree, and the mark is never made."""
        remote, seed = self.make_remote()
        mark = self.tmp / "ran-the-commits-script"
        self.pin(seed, remote, "v9.9.9", tamper=f"import pathlib\npathlib.Path({str(mark)!r}).write_text('x')\n")
        proc = subprocess.run(["bash", str(SCRIPT), "traces", "off"], cwd=seed, capture_output=True, text=True,
                              env=self.core_env(seed, "v9.9.9"))
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertFalse(mark.exists(), "the commit's own scaffolder ran inside the switch")
        after = self.files(remote)
        self.assertNotIn("clusters/platform/apps/tempo.yaml", after)
        self.assertIn("clusters/platform/apps/loki.yaml", after)
        changed = set(self.git("--git-dir", str(remote), "diff", "--name-only", "HEAD~1", "HEAD").stdout.split())
        self.assertTrue(all(p == "instance/config.yaml" or p.startswith("clusters/") for p in changed),
                        f"the switch committed more than the config and its render: {sorted(changed)}")

    def test_an_instance_pinned_to_another_core_is_refused_and_nothing_is_pushed(self):
        remote, seed = self.make_remote()
        self.pin(seed, remote, "v9.9.8")
        head = self.head(remote)
        proc = subprocess.run(["bash", str(SCRIPT), "traces", "off"], cwd=seed, capture_output=True, text=True,
                              env=self.core_env(seed, "v9.9.9"))
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("pins orbit-core v9.9.8 and this build is v9.9.9", proc.stdout)
        self.assertEqual(self.head(remote), head)

    def test_xtrace_never_prints_the_token(self):
        remote, seed = self.make_remote()
        proc = self.run_script(seed, "traces", "off", xtrace=True)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assert_credentials_absent(proc.stdout + proc.stderr)


if __name__ == "__main__":
    unittest.main()
