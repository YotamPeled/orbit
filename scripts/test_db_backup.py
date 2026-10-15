#!/usr/bin/env python3
"""scripts/db-backup.sh and scripts/db-restore.sh, offline: a backup is only a backup once
it was restored and counted, a restore never touches the live database without one, and
every way either can stop leaves the live data alone. Driven against a stub `kubectl` that
plays one CloudNativePG primary: it hands out a dump, takes it back into a scratch
database, and counts what it was really given. The real thing is proven on the test box
(docs/V1.md, scenarios S07, S17 and S17b)."""
from __future__ import annotations

import gzip
import os
import pathlib
import shutil
import subprocess
import sys
import tempfile
import unittest

REPO = pathlib.Path(__file__).resolve().parent.parent
BACKUP = REPO / "scripts" / "db-backup.sh"
RESTORE = REPO / "scripts" / "db-restore.sh"

# A stand-in for kubectl talking to one database pod. State lives in $FAKE_STATE:
#   live.txt       "schema.table rows" lines: what the live database holds
#   scratch.sql    what was last loaded into a scratch database
#   calls.log      every invocation, one line each
KUBECTL = r'''#!/usr/bin/env python3
import gzip, os, pathlib, re, sys
argv = sys.argv[1:]
state = pathlib.Path(os.environ["FAKE_STATE"])
with open(state / "calls.log", "a") as fh:
    fh.write("kubectl " + " ".join(argv) + "\n")
line = " ".join(argv)
env = os.environ.get

def live():
    return [l for l in (state / "live.txt").read_text().splitlines() if l.strip()]

def dump_sql(rows_override=None):
    out = ["SET statement_timeout = 0;"]
    for entry in live():
        table, rows = entry.split()
        n = int(rows_override) if rows_override is not None and table == "public.games" else int(rows)
        out.append("CREATE TABLE %s (id integer);" % table)
        out.append("COPY %s (id) FROM stdin;" % table)
        out += [str(i) for i in range(1, n + 1)]
        out.append("\\.")
    return "\n".join(out) + "\n"

def counts_of(sql):
    found, table, n = [], None, 0
    for l in sql.splitlines():
        m = re.match(r"COPY (\S+) ", l)
        if m:
            table, n = m.group(1), 0
        elif table and l == "\\.":
            found.append("%s %d" % (table, n)); table = None
        elif table:
            n += 1
    return found

def notices(entries):
    for e in entries:
        sys.stderr.write("NOTICE:  ORBIT-COUNT %s\n" % e)

if " get clusters.postgresql.cnpg.io " in line and "-o" not in argv:
    sys.exit(0 if env("FAKE_NO_DATABASE") != "1" else 1)
if " get pod -l " in line:
    if env("FAKE_NO_PRIMARY") != "1":
        print("app-db-1", end="")
    sys.exit(0)
if " get clusters.postgresql.cnpg.io " in line and "initdb.database" in line:
    print("app", end=""); sys.exit(0)
if " get clusters.postgresql.cnpg.io " in line and "initdb.owner" in line:
    print("app", end=""); sys.exit(0)
if " exec " in line:
    tail = argv[argv.index("--") + 1:]
    joined = " ".join(tail)
    if tail[0] == "env":                               # the snapshot dump + the live counts
        sys.stdin.read()
        if env("FAKE_DUMP_FAILS") == "1":
            sys.stderr.write("pg_dump: error: connection failed\n"); sys.exit(0)
        sys.stdout.buffer.write(gzip.compress(dump_sql(env("FAKE_DUMP_GAMES")).encode()))
        sys.stdout.flush()
        notices(live()); sys.stderr.write("ORBIT-DUMP-OK\n"); sys.exit(0)
    if tail[0] == "bash":                              # scratch -> live, one transaction
        if env("FAKE_LOAD_FAILS") == "1":
            sys.stderr.write("ERROR:  could not load\n"); sys.exit(3)
        (state / "live.txt").write_text("\n".join(counts_of((state / "scratch.sql").read_text())) + "\n")
        sys.exit(0)
    if "CREATE DATABASE" in joined or "DROP DATABASE" in joined:
        sys.exit(0)
    database = tail[tail.index("-d") + 1]
    if "-At" in tail and "owner=app" in joined:        # the ownership check
        sys.stdin.read()
        if env("FAKE_WRONG_OWNER") == "1":
            print("public.games (postgres)")
        sys.exit(0)
    if "-At" in tail:                                  # per-table counts of one database
        sys.stdin.read()
        if database.startswith("orbit_"):
            notices(counts_of((state / "scratch.sql").read_text()))
        else:
            notices(live())
        sys.exit(0)
    if database.startswith("orbit_"):                  # a dump being loaded into a scratch database
        sql = sys.stdin.read()
        if env("FAKE_RESTORE_FAILS") == "1":
            sys.stderr.write('psql:<stdin>:4: ERROR:  new row for relation "games" violates check constraint "games_id_check"\n'
                             'DETAIL:  Failing row contains (SECRET-ROW-VALUE).\n')
            sys.exit(3)
        (state / "scratch.sql").write_text(sql)
        sys.exit(0)
sys.stderr.write("stub kubectl: unexpected %s\n" % line)
sys.exit(9)
'''


class Box:
    """A temp directory with the stub on PATH, a live database, and a backups directory."""

    def __init__(self, test: unittest.TestCase, live=("public.games 3", "public.winners 1")):
        self.tmp = pathlib.Path(tempfile.mkdtemp())
        test.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        (self.tmp / "bin").mkdir()
        stub = self.tmp / "bin" / "kubectl"
        stub.write_text(KUBECTL, newline="\n")
        stub.chmod(0o755)
        self.state = self.tmp / "state"
        self.state.mkdir()
        (self.state / "live.txt").write_text("\n".join(live) + "\n")
        self.backups = self.tmp / "backups"

    def env(self, **extra) -> dict:
        e = {"PATH": f"{self.tmp / 'bin'}{os.pathsep}{os.environ['PATH']}", "HOME": str(self.tmp),
             "FAKE_STATE": str(self.state), "ORBIT_BACKUP_DIR": str(self.backups)}
        e.update(extra)
        return e

    def run(self, script, *args, **extra):
        return subprocess.run(["bash", str(script), *args], cwd=self.tmp, env=self.env(**extra),
                              capture_output=True, text=True)

    def calls(self) -> list[str]:
        log = self.state / "calls.log"
        return log.read_text().splitlines() if log.exists() else []

    def files(self, namespace="demo-prod") -> list[str]:
        d = self.backups / namespace
        return sorted(p.name for p in d.iterdir()) if d.is_dir() else []

    def live(self) -> list[str]:
        return (self.state / "live.txt").read_text().split("\n")[:-1]

    def touched_live(self) -> bool:
        """Whether anything was loaded into the live database."""
        return any("--single-transaction" in c for c in self.calls())


class BackingUp(unittest.TestCase):
    def test_a_backup_is_dumped_restored_counted_and_only_then_called_one(self):
        box = Box(self)
        proc = box.run(BACKUP, "demo-prod", "demo-db")
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        (name,) = box.files()
        self.assertRegex(name, r"^demo-db-\d{8}T\d{6}Z\.sql\.gz$")
        self.assertEqual(proc.stdout.strip().splitlines()[-1], f"BACKUP OK: {box.backups / 'demo-prod' / name}")
        self.assertIn("  public.games 3 3", proc.stdout)
        self.assertIn("  public.winners 1 1", proc.stdout)
        self.assertIn("verified: 2 table(s), 4 row(s)", proc.stdout)
        path = box.backups / "demo-prod" / name
        self.assertEqual(oct(path.stat().st_mode & 0o777), "0o600")
        self.assertIn("COPY public.games", gzip.decompress(path.read_bytes()).decode())
        calls = box.calls()
        order = [next(i for i, c in enumerate(calls) if needle in c) for needle in
                 ("env PGDATABASE=app", "CREATE DATABASE", "-d orbit_verify_", "DROP DATABASE")]
        self.assertEqual(order, sorted(order), "dump, scratch database, restore, drop")
        self.assertTrue(any("--snapshot" in c or "env PGDATABASE=app" in c for c in calls))
        self.assertFalse(box.touched_live())

    def test_the_dump_and_the_counts_are_one_snapshot(self):
        """The SQL sent with the dump opens one transaction, exports its snapshot, hands it
        to pg_dump and counts in that same transaction."""
        text = BACKUP.read_text(encoding="utf-8")
        a, b = text.index("BEGIN ISOLATION LEVEL REPEATABLE READ;"), text.index("COMMIT;")
        block = text[a:b]
        self.assertIn("pg_export_snapshot()", block)
        self.assertIn('--snapshot="\\$ORBIT_SNAPSHOT"', block)
        self.assertIn("$COUNT_SQL", block)

    def test_a_dump_that_restores_with_other_counts_is_not_a_backup(self):
        box = Box(self)
        proc = box.run(BACKUP, "demo-prod", "demo-db", FAKE_DUMP_GAMES="2")
        self.assertEqual(proc.returncode, 2, proc.stdout + proc.stderr)
        self.assertIn("BACKUP REFUSED: the dump of demo-prod/demo-db restores with different row counts", proc.stdout)
        self.assertIn("  public.games 3 2", proc.stdout)
        self.assertNotIn("BACKUP OK", proc.stdout)
        (name,) = box.files()
        self.assertTrue(name.endswith(".sql.gz.refused"), "kept for inspection, under a name nothing takes for a backup")
        self.assertTrue(any("DROP DATABASE" in c for c in box.calls()), "the scratch database is dropped on refusal too")

    def test_a_dump_that_does_not_restore_is_not_a_backup_and_no_row_is_printed(self):
        box = Box(self)
        proc = box.run(BACKUP, "demo-prod", "demo-db", FAKE_RESTORE_FAILS="1")
        self.assertEqual(proc.returncode, 2, proc.stdout + proc.stderr)
        self.assertIn("BACKUP REFUSED: the dump of demo-prod/demo-db does not restore", proc.stdout)
        self.assertIn('violates check constraint "games_id_check"', proc.stdout, "the server's own reason is given")
        self.assertNotIn("SECRET-ROW-VALUE", proc.stdout + proc.stderr, "a failing row's data is never printed")
        self.assertTrue(box.files()[0].endswith(".refused"))
        self.assertTrue(any("DROP DATABASE" in c for c in box.calls()))

    def test_a_dump_that_did_not_finish_is_not_a_backup(self):
        box = Box(self)
        proc = box.run(BACKUP, "demo-prod", "demo-db", FAKE_DUMP_FAILS="1")
        self.assertEqual(proc.returncode, 2, proc.stdout + proc.stderr)
        self.assertIn("BACKUP REFUSED: pg_dump of demo-prod/demo-db failed or wrote a broken file", proc.stdout)
        self.assertFalse(any("CREATE DATABASE" in c for c in box.calls()), "nothing is restored from a broken dump")

    def test_no_database_or_no_primary_is_refused_before_anything_runs(self):
        for extra, says in (({"FAKE_NO_DATABASE": "1"}, "demo-prod has no database demo-db"),
                            ({"FAKE_NO_PRIMARY": "1"}, "has no running primary")):
            with self.subTest(case=says):
                box = Box(self)
                proc = box.run(BACKUP, "demo-prod", "demo-db", **extra)
                self.assertEqual(proc.returncode, 2)
                self.assertIn("BACKUP REFUSED: ", proc.stdout)
                self.assertIn(says, proc.stdout)
                self.assertEqual(box.files(), [])

    def test_arguments_that_are_not_names_never_reach_kubectl(self):
        for args in (("demo-prod",), ("demo prod", "demo-db"), ("-n", "demo-db"), ("demo-prod", "demo-db;id"), ()):
            with self.subTest(args=args):
                box = Box(self)
                proc = box.run(BACKUP, *args)
                self.assertEqual(proc.returncode, 2)
                self.assertIn("usage: db-backup.sh", proc.stdout)
                self.assertEqual(box.calls(), [])

    def test_without_bucket_keys_the_bucket_copy_is_said_to_be_skipped(self):
        box = Box(self)
        (box.tmp / "instance").mkdir()
        (box.tmp / "instance" / "config.yaml").write_text(
            "objectStorage:\n  endpoint: https://fsn1.example.invalid\n  buckets:\n    platform: fixture-platform\n")
        proc = box.run(BACKUP, "demo-prod", "demo-db")
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("bucket copy: skipped", proc.stdout)

    def test_the_bucket_keys_reach_curl_on_stdin_and_a_copy_that_differs_is_refused(self):
        """curl is stubbed: it records its argv and stdin, stores an upload and serves it
        back (or serves something else, to prove the comparison bites)."""
        curl = r'''#!/usr/bin/env python3
import os, pathlib, sys
state = pathlib.Path(os.environ["FAKE_STATE"])
argv = sys.argv[1:]
with open(state / "curl.log", "a") as fh:
    fh.write(" ".join(argv) + "\n")
(state / "curl.stdin").write_text(sys.stdin.read())
if "-T" in argv:
    (state / "bucket.obj").write_bytes(pathlib.Path(argv[argv.index("-T") + 1]).read_bytes())
elif "-o" in argv:
    data = (state / "bucket.obj").read_bytes()
    if os.environ.get("FAKE_BUCKET_CORRUPTS") == "1":
        data = data[:-1] + b"X"
    pathlib.Path(argv[argv.index("-o") + 1]).write_bytes(data)
'''
        for corrupt in ("0", "1"):
            with self.subTest(corrupt=corrupt):
                box = Box(self)
                stub = box.tmp / "bin" / "curl"
                stub.write_text(curl, newline="\n")
                stub.chmod(0o755)
                (box.tmp / "instance").mkdir()
                (box.tmp / "instance" / "config.yaml").write_text(
                    "objectStorage:\n  endpoint: https://fsn1.example.invalid\n  buckets:\n    platform: fixture-platform\n")
                proc = box.run(BACKUP, "demo-prod", "demo-db", ORBIT_S3_ACCESS_KEY="AKIAFIXTURE",
                               ORBIT_S3_SECRET_KEY="fixture-secret-value", FAKE_BUCKET_CORRUPTS=corrupt)
                log = (box.state / "curl.log").read_text()
                self.assertNotIn("AKIAFIXTURE", log)
                self.assertNotIn("fixture-secret-value", log, "a key on curl's command line is a key in the process list")
                self.assertIn('user = "AKIAFIXTURE:fixture-secret-value"', (box.state / "curl.stdin").read_text())
                self.assertIn("--aws-sigv4 aws:amz:fsn1:s3", log)
                self.assertIn("https://fsn1.example.invalid/fixture-platform/orbit-backups/demo-prod/demo-db-", log)
                self.assertNotIn("fixture-secret-value", proc.stdout + proc.stderr)
                if corrupt == "0":
                    self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
                    self.assertIn("bucket copy: s3://fixture-platform/orbit-backups/demo-prod/", proc.stdout)
                    self.assertIn("sha256 equal", proc.stdout)
                else:
                    self.assertEqual(proc.returncode, 2)
                    self.assertIn("BACKUP REFUSED: the bucket copy differs from the dump", proc.stdout)


class Restoring(unittest.TestCase):
    def backed_up(self, box) -> str:
        proc = box.run(BACKUP, "demo-prod", "demo-db")
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        (name,) = box.files()
        return name

    def lose_the_games(self, box):
        (box.state / "live.txt").write_text("public.games 0\npublic.winners 1\n")
        (box.state / "calls.log").unlink()

    def test_without_yes_it_compares_and_changes_nothing(self):
        box = Box(self)
        name = self.backed_up(box)
        self.lose_the_games(box)
        proc = box.run(RESTORE, "demo", "prod", "--from", name)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("  public.games 0 3", proc.stdout, "live now beside the backup")
        self.assertIn("nothing changed on the live database", proc.stdout)
        self.assertFalse(box.touched_live())
        self.assertEqual(box.live(), ["public.games 0", "public.winners 1"])
        self.assertEqual(box.files(), [name], "a comparison takes no backup")

    def test_with_yes_it_backs_up_the_live_state_first_then_restores_and_proves_it(self):
        box = Box(self)
        name = self.backed_up(box)
        self.lose_the_games(box)
        import time
        time.sleep(1.1)          # the pre-restore backup's name carries the second
        proc = box.run(RESTORE, "demo", "prod", "--from", "latest", "--yes")
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertEqual(proc.stdout.strip().splitlines()[-1], f"RESTORED: demo-prod/demo-db holds the data of {name}",
                         "`latest` is the newest backup that existed BEFORE this restore took its own")
        self.assertEqual(sorted(box.live()), ["public.games 3", "public.winners 1"])
        files = box.files()
        self.assertEqual(len(files), 2)
        pre = next(f for f in files if f != name)
        self.assertIn(f"the state before the restore is kept as {pre}", proc.stdout)
        self.assertIn("COPY public.games (id) FROM stdin;\n\\.", gzip.decompress((box.backups / "demo-prod" / pre).read_bytes()).decode(),
                      "the pre-restore backup holds the state that was live: no games")
        calls = box.calls()
        first_backup = next(i for i, c in enumerate(calls) if "env PGDATABASE=app" in c)
        load = next(i for i, c in enumerate(calls) if "--single-transaction" in c)
        self.assertLess(first_backup, load, "the backup comes before the live database is touched")
        self.assertIn("every relation is owned by app", proc.stdout)

    def test_a_backup_that_is_refused_refuses_the_restore(self):
        box = Box(self)
        name = self.backed_up(box)
        self.lose_the_games(box)
        proc = box.run(RESTORE, "demo", "prod", "--from", name, "--yes", FAKE_RESTORE_FAILS="1")
        self.assertEqual(proc.returncode, 2, proc.stdout + proc.stderr)
        self.assertIn("RESTORE REFUSED: the live database could not be backed up first, so it is not changed", proc.stdout)
        self.assertFalse(box.touched_live())
        self.assertEqual(box.live(), ["public.games 0", "public.winners 1"])

    def test_a_load_that_fails_says_the_data_is_as_it_was_and_names_the_backup(self):
        box = Box(self)
        name = self.backed_up(box)
        self.lose_the_games(box)
        proc = box.run(RESTORE, "demo", "prod", "--from", name, "--yes", FAKE_LOAD_FAILS="1")
        self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)
        self.assertIn("the transaction was rolled back and the live data is as it was", proc.stdout)
        self.assertNotIn("RESTORED:", proc.stdout)
        self.assertEqual(box.live(), ["public.games 0", "public.winners 1"])

    def test_tables_not_owned_by_the_owner_are_reported_not_called_restored(self):
        box = Box(self)
        name = self.backed_up(box)
        self.lose_the_games(box)
        proc = box.run(RESTORE, "demo", "prod", "--from", name, "--yes", FAKE_WRONG_OWNER="1")
        self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)
        self.assertIn("LOADED, BUT NOT OWNED BY app", proc.stdout)
        self.assertNotIn("RESTORED:", proc.stdout)

    BUCKET_CURL = r"""#!/usr/bin/env python3
# a bucket: -T stores an object under its URL's path, -o serves it, ?list-type=2 lists keys
import os, pathlib, re, sys, urllib.parse
state = pathlib.Path(os.environ["FAKE_STATE"]); store = state / "bucket"; store.mkdir(exist_ok=True)
argv = sys.argv[1:]
with open(state / "curl.log", "a") as fh:
    fh.write(" ".join(argv) + "\n")
(state / "curl.stdin").write_text(sys.stdin.read())
url = next(a for a in argv if a.startswith("https://"))
u = urllib.parse.urlsplit(url)
name = urllib.parse.quote(u.path, safe="")
if "-T" in argv:
    (store / name).write_bytes(pathlib.Path(argv[argv.index("-T") + 1]).read_bytes())
elif "list-type=2" in u.query:
    prefix = urllib.parse.parse_qs(u.query)["prefix"][0]
    bucket = u.path.strip("/")
    keys = sorted(urllib.parse.unquote(f.name)[len("/" + bucket + "/"):] for f in store.iterdir())
    print("<ListBucketResult>" + "".join(f"<Contents><Key>{k}</Key></Contents>" for k in keys if k.startswith(prefix)) + "</ListBucketResult>")
elif "-o" in argv:
    obj = store / name
    if not obj.exists():
        sys.exit(22)
    data = obj.read_bytes()
    if os.environ.get("FAKE_BUCKET_CORRUPTS") == "1":
        data = b"not a gzip file"
    pathlib.Path(argv[argv.index("-o") + 1]).write_bytes(data)
"""
    KEYS = {"ORBIT_S3_ACCESS_KEY": "AKIAFIXTURE", "ORBIT_S3_SECRET_KEY": "fixture-secret-value"}

    def with_bucket(self, box):
        stub = box.tmp / "bin" / "curl"
        stub.write_text(self.BUCKET_CURL, newline="\n")
        stub.chmod(0o755)
        (box.tmp / "instance").mkdir()
        (box.tmp / "instance" / "config.yaml").write_text(
            "objectStorage:\n  endpoint: https://fsn1.example.invalid\n  buckets:\n    platform: fixture-platform\n")
        proc = box.run(BACKUP, "demo-prod", "demo-db", **self.KEYS)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        (name,) = box.files()
        return name

    def test_a_backup_never_takes_the_name_of_one_that_exists(self):
        """Two backups in one second had one name: the second overwrote the first. Ten in a
        row here are ten files (each waits out the second of the one before)."""
        box = Box(self)
        for _ in range(3):
            proc = box.run(BACKUP, "demo-prod", "demo-db")
            self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertEqual(len(box.files()), 3)

    def test_a_rebuilt_platform_restores_from_the_bucket_by_name_and_by_latest(self):
        """The volume the backups lie on is the node's: after a rebuild it is empty, and the
        copy in the bucket is the only one. Until 2026-10-08 the answer then was "there is no
        backup on this platform" (docs/V1.md §8)."""
        for how in ("name", "latest"):
            with self.subTest(how=how):
                box = Box(self)
                name = self.with_bucket(box)
                shutil.rmtree(box.backups)                    # the node was rebuilt
                self.lose_the_games(box)
                (box.state / "curl.log").unlink()
                proc = box.run(RESTORE, "demo", "prod", "--from", name if how == "name" else "latest", "--yes", **self.KEYS)
                self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
                self.assertIn(f"== {name} is not on this platform's volume: fetching s3://fixture-platform/orbit-backups/demo-prod/{name}", proc.stdout)
                self.assertIn("fetched from the bucket: s3://fixture-platform/orbit-backups/demo-prod/", proc.stdout)
                self.assertIn(f"RESTORED: demo-prod/demo-db holds the data of {name}", proc.stdout)
                self.assertEqual(sorted(box.live()), ["public.games 3", "public.winners 1"])
                log = (box.state / "curl.log").read_text()
                self.assertNotIn("fixture-secret-value", log + proc.stdout + proc.stderr)
                self.assertIn('user = "AKIAFIXTURE:fixture-secret-value"', (box.state / "curl.stdin").read_text())
                self.assertIn(name, box.files(), "the fetched file is kept on the volume")

    def test_a_backup_on_the_volume_is_used_without_asking_the_bucket_for_it(self):
        box = Box(self)
        name = self.with_bucket(box)
        self.lose_the_games(box)
        (box.state / "curl.log").unlink()
        proc = box.run(RESTORE, "demo", "prod", "--from", name, **self.KEYS)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertNotIn("fetching", proc.stdout)
        self.assertFalse((box.state / "curl.log").exists(), "a file that is here is not downloaded")

    def test_what_is_in_neither_place_or_is_not_a_backup_file_is_refused_and_nothing_is_touched(self):
        box = Box(self)
        name = self.with_bucket(box)
        shutil.rmtree(box.backups)
        self.lose_the_games(box)
        proc = box.run(RESTORE, "demo", "prod", "--from", "demo-db-20000101T000000Z.sql.gz", "--yes", **self.KEYS)
        self.assertEqual(proc.returncode, 2, proc.stdout + proc.stderr)
        self.assertIn("RESTORE REFUSED: there is no backup demo-db-20000101T000000Z.sql.gz of demo-prod/demo-db on this platform's volume or in its bucket", proc.stdout)
        proc = box.run(RESTORE, "demo", "prod", "--from", name, "--yes", FAKE_BUCKET_CORRUPTS="1", **self.KEYS)
        self.assertEqual(proc.returncode, 2, proc.stdout + proc.stderr)
        self.assertIn(f"RESTORE REFUSED: the bucket's copy of {name} is not a readable gzip file", proc.stdout)
        self.assertEqual(box.files(), [], "a copy that is not a backup is not kept")
        # without keys the bucket cannot be asked: the old answer
        proc = box.run(RESTORE, "demo", "prod", "--from", name, "--yes")
        self.assertEqual(proc.returncode, 2)
        self.assertIn(f"RESTORE REFUSED: there is no backup {name} of demo-prod/demo-db on this platform", proc.stdout)
        self.assertFalse(box.touched_live())
        self.assertEqual(box.live(), ["public.games 0", "public.winners 1"])

    def test_only_this_databases_own_backups_are_taken_by_name(self):
        box = Box(self)
        name = self.backed_up(box)
        (box.backups / "demo-prod" / "other-db-20261001T000000Z.sql.gz").write_bytes(b"x")
        for bad, says in (("other-db-20261001T000000Z.sql.gz", "that is not the name of a backup of demo-prod/demo-db"),
                          ("../demo-prod/" + name, "that is not the name of a backup"),
                          ("demo-db-20000101T000000Z.sql.gz", "there is no backup demo-db-20000101T000000Z.sql.gz"),
                          (name + ".refused", "that is not the name of a backup")):
            with self.subTest(name=bad):
                proc = box.run(RESTORE, "demo", "prod", "--from", bad, "--yes")
                self.assertEqual(proc.returncode, 2, proc.stdout + proc.stderr)
                self.assertIn("RESTORE REFUSED: ", proc.stdout)
                self.assertIn(says, proc.stdout)
                self.assertFalse(box.touched_live())

    def test_latest_with_no_backup_and_a_point_in_time_outside_prod_are_refused(self):
        box = Box(self)
        proc = box.run(RESTORE, "demo", "prod", "--from", "latest", "--yes")
        self.assertEqual(proc.returncode, 2)
        self.assertIn("there is no backup of demo-prod/demo-db to restore", proc.stdout)
        proc = box.run(RESTORE, "demo", "dev", "--to", "2026-10-04T15:00:00Z", "--yes")
        self.assertEqual(proc.returncode, 2)
        self.assertIn("a point in time exists only for a prod database", proc.stdout)
        proc = box.run(RESTORE, "demo", "prod", "--to", "yesterday", "--yes")
        self.assertEqual(proc.returncode, 2)
        self.assertIn("--to must be an RFC3339 UTC instant", proc.stdout)
        for args in (("demo", "prod"), ("demo", "prod", "--from", "latest", "--to", "2026-10-04T15:00:00Z"), ("demo",)):
            proc = box.run(RESTORE, *args)
            self.assertEqual(proc.returncode, 2)
            self.assertIn("usage: db-restore.sh", proc.stdout)
        self.assertFalse(box.touched_live())


if __name__ == "__main__":
    unittest.main()
