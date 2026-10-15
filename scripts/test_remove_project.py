"""scripts/remove-project.sh (V1 §8, S17): the supported path for a project dropped from
the catalog. Driven against stub kubectl/orbit binaries that record every call, so the
refusals and the order "verified backup before any delete" are checked offline."""
from __future__ import annotations

import os
import pathlib
import shutil
import subprocess
import tempfile
import textwrap
import unittest

REPO = pathlib.Path(__file__).resolve().parent.parent
SCRIPT = REPO / "scripts" / "remove-project.sh"
GIT = ["git", "-c", "user.email=t@t", "-c", "user.name=t", "-c", "commit.gpgsign=false"]

import test_db_backup

# kubectl for the removal's own questions (what is labelled, what is deleted); everything
# about a database (dump, scratch restore, counts) is test_db_backup's stub, which this
# one hands over to.
KUBECTL = textwrap.dedent(r'''
    #!/usr/bin/env bash
    deleted(){ grep -q "^kubectl delete ns" "$FAKE_STATE/calls.log" 2>/dev/null; }
    case "$*" in
      "get ns -l "*) echo "kubectl $*" >> "$FAKE_STATE/calls.log"; deleted || { [ -z "$FAKE_NS" ] || printf '%s\n' $FAKE_NS; } ;;
      "-n monitoring get "*) echo "kubectl $*" >> "$FAKE_STATE/calls.log"
        grep -q "^kubectl -n monitoring delete" "$FAKE_STATE/calls.log" || { [ -z "$FAKE_MON" ] || printf '%s\n' $FAKE_MON; } ;;
      *" get deploy,cronjob,statefulset,pvc "*) echo "kubectl $*" >> "$FAKE_STATE/calls.log"; echo deployment.apps/api ;;
      *" get clusters.postgresql.cnpg.io -o "*) echo "kubectl $*" >> "$FAKE_STATE/calls.log"; echo app-db ;;
      "delete "*|"-n monitoring delete "*) echo "kubectl $*" >> "$FAKE_STATE/calls.log" ;;
      *) exec "$(dirname "$0")/kubectl-database" "$@" ;;
    esac
''').lstrip()

ORBIT = textwrap.dedent(r'''
    #!/usr/bin/env bash
    echo "orbit $*" >> "$FAKE_STATE/calls.log"
    case "$*" in
      "platform apply "*) cat >/dev/null ;;
      "platform plan "*) echo "services empty=true rendered=1 matched=1 changed=0" >&2 ;;
    esac
''').lstrip()


class RemoveProjectTest(unittest.TestCase):
    def setUp(self):
        self.tmp = pathlib.Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.bin = self.tmp / "bin"
        self.bin.mkdir()
        for name, body in (("kubectl", KUBECTL), ("kubectl-database", test_db_backup.KUBECTL), ("orbit", ORBIT)):
            p = self.bin / name
            p.write_text(body, newline="\n")
            p.chmod(0o755)
        self.inst = self.tmp / "instance"
        (self.inst / "instance").mkdir(parents=True)
        (self.inst / "catalog").mkdir()
        (self.inst / "instance" / "config.yaml").write_text("name: t\n")
        self.catalog("projects:\n  - id: other\n")
        self.state = self.tmp / "state"
        self.state.mkdir()
        (self.state / "live.txt").write_text("public.games 3\npublic.winners 1\n")
        self.log = self.state / "calls.log"

    def catalog(self, text):
        (self.inst / "catalog" / "projects.yaml").write_text(text)
        if not (self.inst / ".git").exists():
            subprocess.run(GIT + ["init", "-q", "-b", "main"], cwd=self.inst, check=True)
        subprocess.run(GIT + ["add", "-A"], cwd=self.inst, check=True)
        subprocess.run(GIT + ["commit", "-q", "-m", "c", "--allow-empty"], cwd=self.inst, check=True)

    def run_it(self, *args, **env):
        e = dict(os.environ, PATH=f"{self.bin}{os.pathsep}{os.environ['PATH']}",
                 FAKE_STATE=str(self.state), ORBIT_CONFIG_DIR=str(self.tmp / "conf"),
                 FAKE_NS="gone-dev gone-prod", FAKE_MON="servicemonitor.monitoring.coreos.com/gone")
        e.update(env)
        return subprocess.run(["bash", str(SCRIPT), *args], cwd=self.inst, env=e,
                              capture_output=True, text=True)

    def calls(self):
        return self.log.read_text().splitlines() if self.log.exists() else []

    def test_a_project_still_in_the_catalog_is_refused(self):
        self.catalog("projects:\n  - id: gone\n")
        proc = self.run_it("gone", "--yes")
        self.assertEqual(proc.returncode, 2, proc.stdout + proc.stderr)
        self.assertIn("REFUSED: gone is still in catalog/projects.yaml", proc.stdout)
        self.assertEqual(self.calls(), [])

    def test_an_uncommitted_checkout_is_refused(self):
        (self.inst / "catalog" / "projects.yaml").write_text("projects: []\n")
        proc = self.run_it("gone", "--yes")
        self.assertEqual(proc.returncode, 2, proc.stdout + proc.stderr)
        self.assertIn("REFUSED: the instance checkout has uncommitted changes", proc.stdout)
        self.assertEqual(self.calls(), [])

    def test_without_yes_it_lists_and_deletes_nothing(self):
        proc = self.run_it("gone")
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("namespace gone-dev:", proc.stdout)
        self.assertIn("database app-db (CloudNativePG)", proc.stdout)
        self.assertIn("dry run: nothing changed", proc.stdout)
        self.assertFalse([c for c in self.calls() if " delete " in f" {c} " or "env PGDATABASE=" in c
                          or c.startswith("orbit platform apply")], self.calls())

    def test_a_dump_that_does_not_match_the_live_counts_stops_before_any_delete(self):
        proc = self.run_it("gone", "--yes", FAKE_DUMP_GAMES="2")
        self.assertEqual(proc.returncode, 2, proc.stdout + proc.stderr)
        self.assertIn("BACKUP REFUSED: the dump of gone-dev/app-db restores with different row counts", proc.stdout)
        self.assertIn("REFUSED: gone-dev/app-db has no verified backup — nothing deleted", proc.stdout)
        self.assertFalse([c for c in self.calls() if " delete " in f" {c} "
                          or c.startswith("orbit platform apply")], self.calls())

    def test_a_dump_that_does_not_restore_stops_before_any_delete(self):
        """The check is a real restore: a dump whose rows the server refuses is no backup."""
        proc = self.run_it("gone", "--yes", FAKE_RESTORE_FAILS="1")
        self.assertEqual(proc.returncode, 2, proc.stdout + proc.stderr)
        self.assertIn("BACKUP REFUSED: the dump of gone-dev/app-db does not restore", proc.stdout)
        self.assertFalse([c for c in self.calls() if " delete " in f" {c} "
                          or c.startswith("orbit platform apply")], self.calls())

    def test_with_yes_it_backs_up_verifies_then_removes_and_proves_it(self):
        proc = self.run_it("gone", "--yes")
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("REMOVED: gone", proc.stdout)
        calls = self.calls()
        dumps = [i for i, c in enumerate(calls) if "env PGDATABASE=app" in c]
        restores = [i for i, c in enumerate(calls) if "-d orbit_verify_" in c and "-At" not in c]
        deletes = [i for i, c in enumerate(calls) if " delete " in f" {c} "]
        self.assertEqual(len(dumps), 2, calls)          # one per namespace's database
        self.assertEqual(len(restores), 2, "each dump is restored into a scratch database before it counts")
        self.assertTrue(deletes and max(dumps + restores) < min(deletes), calls)
        self.assertIn("kubectl delete ns gone-dev --wait=true --timeout=600s", calls)
        self.assertIn("kubectl delete ns gone-prod --wait=true --timeout=600s", calls)
        backups = sorted((self.tmp / "conf" / "backups").glob("*/*.sql.gz"))
        self.assertEqual([b.parent.name for b in backups], ["gone-dev", "gone-prod"])
        self.assertIn("  public.games 3 3", proc.stdout)
        self.assertIn("orbit platform apply " + str(self.inst) + " --app services --prune", calls,
                      "the services app is pruned from the directory the script runs in")

    def test_nothing_left_is_already_removed(self):
        proc = self.run_it("gone", "--yes", FAKE_NS="", FAKE_MON="")
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("REMOVED: gone", proc.stdout)


if __name__ == "__main__":
    unittest.main(verbosity=2)
