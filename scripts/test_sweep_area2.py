"""Area-2 sweep, round 1 (2026-09-14): one test per repaired root cause.

Each test names the break it catches. Fixtures are borrowed from test_ship.py and
test_assert_deployed.py rather than rebuilt.
"""
from __future__ import annotations

import os
import pathlib
import shutil
import subprocess
import sys
import tempfile
import unittest

import yaml

import test_assert_deployed as ad
import test_ship
from test_ship import GIT, ROOT

SHIP = ROOT / "scripts" / "ship.sh"
MONITOR = ROOT / "scripts" / "monitor-commit.sh"
PITR = ROOT / "scripts" / "pitr-drill.sh"
BOOTSTRAP = ROOT / "scripts" / "bootstrap-node.sh"
NO_CREDENTIALS = ROOT / "scripts" / "no-credentials.py"

MARKER = "PLANTED9d3f"          # any diagnostic that repeats an input carries this


def _remote_tags(remote) -> dict:
    return yaml.safe_load(subprocess.run(
        ["git", "--git-dir", str(remote), "show", "main:catalog/tags.yaml"],
        check=True, capture_output=True, text=True).stdout)["tags"] or {}


def _remote_head(remote) -> str:
    return subprocess.run(["git", "--git-dir", str(remote), "rev-parse", "main"],
                          check=True, capture_output=True, text=True).stdout.strip()


class ShipFanOutTest(unittest.TestCase):
    """R1: ship.sh's named env/component reach the write; R3: bump outcome guards."""

    setUp = test_ship.ShipScriptTest.setUp
    make_remote = test_ship.ShipScriptTest.make_remote
    ship_env = test_ship.ShipScriptTest.ship_env
    run_ship = test_ship.ShipScriptTest.run_ship
    assert_credentials_absent = test_ship.ShipScriptTest.assert_credentials_absent

    def catalog(self, seed) -> dict:
        return yaml.safe_load((seed / "catalog" / "projects.yaml").read_text())

    def write_catalog(self, seed, cat, push=True):
        (seed / "catalog" / "projects.yaml").write_text(yaml.safe_dump(cat))
        if push:
            subprocess.run(GIT + ["commit", "-qam", "catalog"], cwd=seed,
                           check=True, capture_output=True)
            subprocess.run(GIT + ["push", "-q", "origin", "main"], cwd=seed,
                           check=True, capture_output=True)

    def test_ship_to_staging_leaves_a_sibling_env_on_the_same_branch_untouched(self):
        remote, seed = self.make_remote()
        cat = self.catalog(seed)
        cat["projects"][0]["environments"]["staging"] = {"enabled": True, "branch": "main"}
        self.write_catalog(seed, cat)
        result = self.run_ship(seed, "widgets", "staging", "web", "main-abcdef0")
        out = result.stdout + result.stderr
        self.assertEqual(result.returncode, 0, out)
        self.assertEqual(_remote_tags(remote), {"widgets/staging/web": "main-abcdef0"}, out)

    def test_ship_of_one_component_leaves_a_sibling_sharing_the_image_untouched(self):
        remote, seed = self.make_remote()
        cat = self.catalog(seed)
        cat["projects"][0]["components"].append(
            {"name": "worker", "image": "ghcr.io/shipfix-org/widgets-web"})
        self.write_catalog(seed, cat)
        result = self.run_ship(seed, "widgets", "prod", "web", "main-abcdef0")
        out = result.stdout + result.stderr
        self.assertEqual(result.returncode, 0, out)
        self.assertEqual(_remote_tags(remote), {"widgets/prod/web": "main-abcdef0"}, out)

    def test_catalog_skew_between_desk_and_main_fails_the_bump_and_writes_nothing(self):
        # the desk's catalog knows staging on release-9; main does not. The bump runs on
        # the clone of main, so the named target claims nothing there.
        remote, seed = self.make_remote()
        before = _remote_head(remote)
        cat = self.catalog(seed)
        cat["projects"][0]["environments"]["staging"] = {"enabled": True, "branch": "release-9"}
        self.write_catalog(seed, cat, push=False)
        result = self.run_ship(seed, "widgets", "staging", "web", "release-9-abcdef0")
        out = result.stdout + result.stderr
        self.assertNotEqual(result.returncode, 0, out)
        self.assertIn("STEP bump FAIL", result.stdout, out)
        self.assertIn("does not claim this branch in the catalog on main", out)
        self.assertNotIn("COMMIT ", result.stdout, out)
        self.assertEqual(_remote_head(remote), before)
        self.assertEqual(_remote_tags(remote), {})

    def test_reshipping_the_deployed_tag_says_already_deployed_not_deployed_nowhere(self):
        remote, seed = self.make_remote()
        first = self.run_ship(seed, "widgets", "prod", "web", "main-abcdef0")
        self.assertEqual(first.returncode, 0, first.stdout + first.stderr)
        head = _remote_head(remote)
        second = self.run_ship(seed, "widgets", "prod", "web", "main-abcdef0")
        out = second.stdout + second.stderr
        self.assertEqual(second.returncode, 0, out)
        self.assertIn("tag already deployed", out)
        self.assertNotIn("deployed nowhere", out)
        self.assertEqual(_remote_head(remote), head)
        self.assertIn(f"COMMIT {head}", second.stdout.splitlines()[-1])

    def test_a_failing_commit_is_a_bump_failure_not_a_landed_commit(self):
        remote, seed = self.make_remote()
        before = _remote_head(remote)
        hooks = self.tmp / "hooks"
        hooks.mkdir()
        (hooks / "pre-commit").write_text("#!/bin/sh\necho 'hook refused' >&2\nexit 1\n")
        (hooks / "pre-commit").chmod(0o755)
        with open(self.tmp / ".gitconfig", "a") as fh:
            fh.write(f"[core]\n\thooksPath = {hooks}\n")
        result = self.run_ship(seed, "widgets", "prod", "web", "main-abcdef0")
        out = result.stdout + result.stderr
        self.assertNotEqual(result.returncode, 0, out)
        self.assertIn("STEP bump FAIL", result.stdout, out)
        self.assertIn("commit failed", out)
        self.assertNotIn("COMMIT ", result.stdout, out)
        self.assertEqual(_remote_head(remote), before)

    def test_a_commit_that_moved_nothing_is_a_bump_failure_not_a_landed_commit(self):
        # a git whose `commit` exits 0 without committing (a hook that swallowed it): the
        # push of the unchanged HEAD would succeed and read as a deploy
        remote, seed = self.make_remote()
        before = _remote_head(remote)
        real = shutil.which("git")
        (self.bindir / "git").write_text(
            "#!/bin/sh\n[ \"$1\" = commit ] && exit 0\nexec %s \"$@\"\n" % real)
        (self.bindir / "git").chmod(0o755)
        result = self.run_ship(seed, "widgets", "prod", "web", "main-abcdef0")
        out = result.stdout + result.stderr
        self.assertNotEqual(result.returncode, 0, out)
        self.assertIn("STEP bump FAIL", result.stdout, out)
        self.assertIn("commit made no new commit", out)
        self.assertNotIn("COMMIT ", result.stdout, out)
        self.assertEqual(_remote_head(remote), before)

    def test_a_neighbours_catalog_repo_text_never_reaches_the_ship_output(self):
        # round 2: a second project sharing the image is skipped with a note that ship.sh
        # now prints on success; the note names validated ids only, never the repo text
        remote, seed = self.make_remote()
        cat = self.catalog(seed)
        cat["projects"].append({"id": "neighbour", "repo": "\x1b[31m" + MARKER + "-org/evil\x1b[0m",
                                "components": [{"name": "web", "image": "ghcr.io/shipfix-org/widgets-web"}],
                                "environments": {"prod": {"branch": "main"}}})
        cat["projects"].append({"id": "\x1b[31m" + MARKER + "id", "repo": "other-org/thing",
                                "components": [{"name": "\x1b[31m" + MARKER + "\nSTEP bump OK\nweb",
                                                "image": "ghcr.io/shipfix-org/widgets-web"}],
                                "environments": {"prod": {"branch": "main"}}})
        self.write_catalog(seed, cat)
        result = self.run_ship(seed, "widgets", "prod", "web", "main-abcdef0")
        out = result.stdout + result.stderr
        self.assertEqual(result.returncode, 0, out)
        self.assertIn("note: skipped 2 project(s) not owned by the dispatching repo: "
                      "neighbour/web (built from another repo), "
                      "(a project id not of the catalog shape)/(a component name not of the label shape)"
                      " (built from another repo)", out)
        self.assertEqual(out.count("STEP bump OK"), 1, out)
        self.assertNotIn(MARKER, out)
        self.assertNotIn("\x1b", out)
        self.assertEqual(_remote_tags(remote), {"widgets/prod/web": "main-abcdef0"}, out)

    def test_a_component_named_like_a_long_label_is_shippable(self):
        # round 2: a component name is a DNS label (up to 63), not a project id (up to 40)
        remote, seed = self.make_remote()
        name = "frontend-image-proxy-edge-renderer-service"
        cat = self.catalog(seed)
        cat["projects"][0]["components"].append(
            {"name": name, "image": "ghcr.io/shipfix-org/widgets-" + name})
        self.write_catalog(seed, cat)
        result = self.run_ship(seed, "widgets", "prod", name, "main-abcdef0")
        out = result.stdout + result.stderr
        self.assertEqual(result.returncode, 0, out)
        self.assertEqual(_remote_tags(remote), {"widgets/prod/" + name: "main-abcdef0"}, out)

    def test_ship_refuses_a_non_id_env_before_anything_runs(self):
        _, seed = self.make_remote()
        result = self.run_ship(seed, "widgets", MARKER + "_env", "web", "main-abcdef0")
        out = result.stdout + result.stderr
        self.assertNotEqual(result.returncode, 0, out)
        self.assertIn("STEP args FAIL", out)
        self.assertNotIn("STEP config", out)

    def bump_tag(self, seed, **env_extra):
        env = self.ship_env(seed)
        env.update({"IMAGE": "ghcr.io/shipfix-org/widgets-web", "TAG": "main-abcdef0",
                    "BRANCH": "main", "REPO": "shipfix-org/widgets"})
        env.update(env_extra)
        return subprocess.run([sys.executable, str(seed / "scripts" / "bump-tag.py")], cwd=seed,
                              capture_output=True, text=True, env=env)

    def test_bump_tag_refusals_never_repeat_the_refused_value(self):
        _, seed = self.make_remote()
        for field, value in (("TAG", MARKER + "!!"), ("REPO", MARKER),
                             ("BRANCH", MARKER + " x"), ("ORBIT_SHIP_ENV", MARKER),
                             ("ORBIT_SHIP_COMPONENT", MARKER + "-" + "x" * 70)):
            with self.subTest(field=field):
                proc = self.bump_tag(seed, **{field: value})
                out = proc.stdout + proc.stderr
                self.assertNotEqual(proc.returncode, 0, out)
                self.assertIn("refusing", out)
                self.assertNotIn(MARKER, out)

    def test_bump_tag_ownership_refusal_names_project_ids_not_catalog_repo_text(self):
        _, seed = self.make_remote()
        cat = self.catalog(seed)
        cat["projects"][0]["repo"] = "\x1b[31m" + MARKER + "-org/evil"
        (seed / "catalog" / "projects.yaml").write_text(yaml.safe_dump(cat))
        env = {k: "" for k in ("ORBIT_SHIP_PROJECT", "ORBIT_SHIP_ENV", "ORBIT_SHIP_COMPONENT")}
        proc = self.bump_tag(seed, REPO="other-org/thing", **env)
        out = proc.stdout + proc.stderr
        self.assertNotEqual(proc.returncode, 0, out)
        self.assertIn("owns none of the projects using this image: widgets/web (built from another repo)", out)
        self.assertNotIn(MARKER, out)

    def test_bump_tag_reports_already_current_when_the_tag_is_deployed(self):
        _, seed = self.make_remote()
        (seed / "catalog" / "tags.yaml").write_text(
            "tags:\n  widgets/prod/web: main-abcdef0\n")
        proc = self.bump_tag(seed)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("already current: widgets/prod/web", proc.stdout)


class MonitorNameTest(unittest.TestCase):
    """R2: monitor-commit's two name refusals name the shape, not the value."""

    setUp = test_ship.ShipScriptTest.setUp
    make_remote = test_ship.ShipScriptTest.make_remote
    ship_env = test_ship.ShipScriptTest.ship_env

    def run_monitor(self, seed, *args, body=None):
        env = self.ship_env(seed)
        env["ORBIT_ACTION"] = "monitor-upsert"
        if body is not None:
            path = self.tmp / "monitor.yaml"
            path.write_text(yaml.safe_dump(body, sort_keys=False))
            env["MONITOR_FILE"] = str(path)
        return subprocess.run(["bash", str(MONITOR), *args], cwd=seed,
                              capture_output=True, text=True, env=env)

    def test_a_non_label_monitor_name_is_refused_without_being_repeated(self):
        _, seed = self.make_remote()
        proc = self.run_monitor(seed, "upsert", "\x1b[31m" + MARKER)
        out = proc.stdout + proc.stderr
        self.assertNotEqual(proc.returncode, 0, out)
        self.assertIn("monitor name is not a DNS label", out)
        self.assertNotIn(MARKER, out)

    def test_a_body_whose_name_differs_is_refused_without_repeating_the_body_name(self):
        _, seed = self.make_remote()
        body = {"name": "\x1b[31m" + MARKER + "A1" * 48, "kind": "loki", "query": "x"}
        proc = self.run_monitor(seed, "upsert", "queue-backlog", body=body)
        out = proc.stdout + proc.stderr
        self.assertNotEqual(proc.returncode, 0, out)
        self.assertIn("monitor file's top-level name is not 'queue-backlog'", out)
        self.assertNotIn(MARKER, out)


class AssertDeployedTest(unittest.TestCase):
    """R2 (catalog read), R5 (EXPECTED from the catalog image), R6 (curl escaping)."""

    setUp = ad.AssertDeployedTest.setUp
    _catalog = ad.AssertDeployedTest._catalog
    _cluster = ad.AssertDeployedTest._cluster
    _run = ad.AssertDeployedTest._run
    _lines = ad.AssertDeployedTest._lines
    _step = ad.AssertDeployedTest._step

    def test_a_catalog_parse_error_names_the_exception_type_not_the_text(self):
        self._catalog("note: !%s value\n" % MARKER)
        proc = self._run()
        out = proc.stdout + proc.stderr
        self.assertNotEqual(proc.returncode, 0, out)
        self.assertIn("READERR ConstructorError", out)
        self.assertNotIn(MARKER, out)

    def test_expected_image_is_the_catalog_image_not_the_naming_convention(self):
        image = "ghcr.io/fixture-org/portal-frontend"
        self._catalog(ad.WEB_CATALOG.replace("ghcr.io/fixture-org/portal-web", image))
        self._cluster(pod=ad._pod(ad.POD, image + ":" + ad.TAG))
        proc = self._run()
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertRegex(self._step(proc, "image"), r"^STEP image OK ")

    def test_a_catalog_without_an_image_falls_back_to_the_convention(self):
        self._catalog(ad.WEB_CATALOG.replace("        image: ghcr.io/fixture-org/portal-web\n", ""))
        proc = self._run()
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertRegex(self._step(proc, "image"), r"^STEP image OK ")

    def test_web_auth_credential_is_escaped_for_curls_config_grammar(self):
        cred = 'authuser:pa\\ss"word'
        auth = self.tmp / "web.auth"
        ad._write(auth, cred + "\r\n")
        proc = self._run(env_extra={"ORBIT_WEB_AUTH_FILE": str(auth)})
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertRegex(self._step(proc, "log"), r"^STEP log OK ")
        stdin = (self.tmp / "curl.stdin").read_text()
        self.assertIn('user = "authuser:pa\\\\ss\\"word"\n', stdin)
        self.assertNotIn("\r", stdin)


class NoCredentialsPointerTest(unittest.TestCase):
    """R4: a credential pasted into a {secret, key} pointer is still a finding."""

    def run_check(self, pointer: dict):
        root = pathlib.Path(tempfile.mkdtemp(prefix="no-credentials-"))
        self.addCleanup(shutil.rmtree, root, True)
        (root / "scripts" / "verify").mkdir(parents=True)
        (root / "clusters" / "apps").mkdir(parents=True)
        shutil.copy(NO_CREDENTIALS, root / "scripts" / "no-credentials.py")
        shutil.copy(ROOT / "scripts" / "verify" / "shown.py", root / "scripts" / "verify" / "shown.py")
        (root / "clusters" / "apps" / "widgets.yaml").write_text(yaml.safe_dump(
            {"kind": "Deployment", "spec": {"env": {"API_TOKEN": pointer}}}))
        return subprocess.run([sys.executable, "scripts/no-credentials.py"], cwd=root,
                              capture_output=True, text=True, timeout=60)

    def test_a_token_inside_a_secret_pointer_is_a_finding(self):
        proc = self.run_check({"secret": "ghp_" + MARKER + "credential0123456789abcd", "key": "API_TOKEN"})
        self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)
        self.assertIn("credential in a generated manifest", proc.stdout)
        self.assertNotIn(MARKER, proc.stdout + proc.stderr)

    def test_a_clean_secret_pointer_is_not_a_finding(self):
        proc = self.run_check({"secret": "widgets-secrets", "key": "API_TOKEN"})
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)


PITR_KUBECTL = r"""#!/usr/bin/env bash
# args decide the answer; the hostile values come from the fixture's files
args="$*"
case "$args" in
  *"get ns "*) exit 1 ;;
  *"get pod "*jsonpath*) echo -n "widgets-db-1" ;;
  *"jsonpath={.spec.bootstrap.initdb.database}"*) cat "$STUB_DIR/dbname" ;;
  *"select relname"*) cat "$STUB_DIR/relname" ;;
  *"pg_logical_emit_message"*|*"select count"*) echo "$STUB_DIR" >> "$STUB_DIR/reached-sql"; echo "1|0/0" ;;
esac
exit 0
"""


class PitrDrillIdentifierTest(unittest.TestCase):
    """R8 (A1): a live table or database name that is not a plain identifier never reaches
    psql as SQL, and is never repeated."""

    def setUp(self):
        self.tmp = pathlib.Path(tempfile.mkdtemp(prefix="pitr-drill-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        ad._write(self.tmp / "stubs" / "kubectl", PITR_KUBECTL, executable=True)
        ad._write(self.tmp / "instance" / "config.yaml", yaml.safe_dump(
            {"objectStorage": {"buckets": {"platform": "fixture-bucket"},
                               "endpoint": "https://s3.fixture.test"}}))
        ad._write(self.tmp / "dbname", "appdb")
        ad._write(self.tmp / "relname", "orders")

    def run_drill(self):
        env = dict(os.environ)
        env["PATH"] = str(self.tmp / "stubs") + os.pathsep + env.get("PATH", "")
        env["STUB_DIR"] = str(self.tmp)
        return subprocess.run(["bash", str(PITR), "widgets"], cwd=str(self.tmp),
                              capture_output=True, text=True, env=env, timeout=60)

    def test_a_hostile_table_name_stops_the_drill_before_any_further_sql(self):
        ad._write(self.tmp / "relname", 'a"; drop table orders; --' + MARKER)
        proc = self.run_drill()
        out = proc.stdout + proc.stderr
        self.assertEqual(proc.returncode, 1, out)
        self.assertIn("not a plain identifier", out)
        self.assertNotIn(MARKER, out)
        self.assertFalse((self.tmp / "reached-sql").exists(), out)

    def test_a_hostile_database_name_is_refused_without_being_repeated(self):
        ad._write(self.tmp / "dbname", MARKER + '"')
        proc = self.run_drill()
        out = proc.stdout + proc.stderr
        self.assertEqual(proc.returncode, 1, out)
        self.assertIn("not a catalog id", out)
        self.assertNotIn(MARKER, out)
        self.assertFalse((self.tmp / "reached-sql").exists(), out)

    def test_a_hyphenated_database_name_is_the_platforms_own_and_passes(self):
        # round 2: the chart names the database after the project id, which may carry hyphens
        ad._write(self.tmp / "dbname", "widgets-api")
        proc = self.run_drill()
        out = proc.stdout + proc.stderr
        self.assertNotIn("refused", out)
        self.assertTrue((self.tmp / "reached-sql").exists(), out)

    def test_a_plain_table_name_reaches_the_pinning_statement(self):
        proc = self.run_drill()
        # the drill goes on to apply manifests the stub cannot answer; what this test
        # pins is that a plain identifier passed the gate and the next SQL ran
        self.assertTrue((self.tmp / "reached-sql").exists(), proc.stdout + proc.stderr)


class BootstrapMetadataBlockTest(unittest.TestCase):
    """R7 (A3): a missing metadata DROP rule is fatal, not a warning."""

    def test_missing_metadata_block_is_fatal(self):
        text = BOOTSTRAP.read_text()
        start = text.index("iptables -t raw -C PREROUTING -d 169.254.169.254 -j DROP")
        block = text[start:start + 900]
        self.assertIn("FATAL: metadata block not applied", block)
        self.assertRegex(block, r"FATAL: metadata block not applied[^\n]*\n[^\n]*\n\s*exit 1")
        self.assertNotIn("WARNING: metadata", text)

    def test_the_unit_start_keeps_its_stderr(self):
        text = BOOTSTRAP.read_text()
        self.assertIn("systemctl start orbit-block-metadata.service >/dev/null\n", text)
        self.assertNotIn("systemctl start orbit-block-metadata.service >/dev/null 2>&1", text)


if __name__ == "__main__":
    unittest.main(verbosity=2)
