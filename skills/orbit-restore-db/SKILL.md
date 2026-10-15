---
name: orbit-restore-db
description: "Bring a database's data back to a moment before something destroyed it (a bad migration, a wrong DELETE): find the moment with the ledger and the logs, make sure the destroyer no longer runs, compare first with a dry run, then restore with orbit_db_restore. Needs the admin role. Read orbit-operator first."
role: admin
---

# Restore a database

Input: "the data in project P's production database was destroyed — bring it back". Output:
the live database holding the data as of a stated moment, with the per-table row counts
before and after, or a precise stop.

`orbit_db_restore` does it, and it needs the `admin` role: with a lower one the tool is not
in your list, and the answer is that the job needs an admin token (steps 1 and 2 still tell
the requester what happened and when). It restores one of two things:

- `to`: a moment. Only a `prod` database has one, and only while the `backups` feature is
  on: its write-ahead log is archived to the bucket, so it can be rebuilt as of any second.
- `backup`: a backup file that `orbit_db_backup` made earlier (`orbit-change-service`), by
  name or `latest`. This is all a dev database has.

Both tools exist while the `databases` feature is on.

## 1. When did it happen

- `orbit_ledger_get(project=P, env="prod", limit=10)`: the deploys, newest first, each with
  its commit time. A migration runs when a new tag starts, so the destructive deploy is the
  one whose time matches the loss.
- `orbit_catalog_history(project=P, limit=10)`: the changes of configuration, which the
  ledger does not hold. A setting can destroy data as well as a ship can (a retention
  of zero days, a job pointed at the wrong database): each item names the commit, its
  time, who made it and the paths that changed. orbitd applies a catalog commit a few
  minutes after `at`, and a scheduled job acts on it at its next run, so the loss is
  later than the commit.
- `orbit_logs_query` on the prod namespace around that time: the application usually logs
  its migration (a line naming it). The last line before it is a good upper bound.
- `orbit_db_status(project=P, env="prod")`: `first_recoverability_point` is the earliest
  moment there is to restore to (the database's first base backup in the bucket). A `to`
  before it, or in the future, is refused at once with that moment in the message.
- Choose `to` as an RFC3339 UTC instant **before** the destructive event and after the
  last write you want to keep — one minute before the bad deploy is the usual answer. Writes made after `to` are lost by the restore; say which window that is.

## 2. Make sure the destroyer no longer runs

`orbit_deploy_status(project=P, env="prod")`. If the tag that destroyed the data still
runs, it may destroy it again at its next restart: roll it back first with `orbit-rollback`
(to the tag before it in the ledger) and wait for that to land. If it was a setting, put
the setting back first (`orbit-change-service`) and wait for it to be live. Do not restore
under a running destroyer.

`orbit_db_status(project=P, env="prod")`: `state` must be `ready` before anything is
restored into it (`not ready` and `hibernated` are a database that is down; `phase` is the
database controller's own word and can still read healthy then).

## 3. Compare first: `orbit_db_restore(project, env, to=T, dry_run=true)`

No confirmation; nothing on the live database changes. It returns `status: running` with
`evidence.job_id`: wait with `orbit_job_wait(job_id, timeout_s=120)` and call it again
while the job runs (a point in time is rebuilt from the bucket in a throwaway namespace,
which takes several minutes). The job has one step, `compare`, and ends `dry_run`.
`evidence.output` carries a line per table: the table, its rows live now, its rows in the
state you asked for. Read it: the tables the loss emptied must show their rows on the
restored side. If the restored side is empty too, `to` is after the loss: choose an
earlier moment and compare again. If `compare` failed, quote its `detail` and stop.

## 4. Restore: `orbit_db_restore(project, env, to=T)`

Production needs a human go, as a ship does: show step 3's table and the window of writes
that will be lost, and ask, unless the requester already said in so many words to restore
prod. The tool then asks for its own confirmation. Wait with `orbit_job_wait` as in step
3. The job's steps are `confirm, backup, restore`:

- `backup` first backs up the live database as it is now, and proves that backup
  restores. If it fails, `restore` is `skipped` and the live database was not touched:
  quote the `backup` step's `detail` and stop.
- `restore` replaces the live data in one transaction, then checks every table holds the
  restored row count and belongs to the database's owner.
- `applied`: done. `evidence.pre_restore_backup` names the backup of the state before the
  restore; restoring that file (`backup=<its name>`) undoes this restore.
- `failed` at `restore`: quote its `detail`. The transaction was rolled back or the check
  after it failed; `evidence.pre_restore_backup` still names the way back.

The application is not restarted. If it cached what it read, restart it (`orbit-rollback`,
"restart").

## The answer

The moment you restored to and why (the deploy and log line it is based on), step 3's
table, the final step's `detail` and the counts in `evidence.output`, the window of writes
lost, the name of the pre-restore backup, and whether the destructive tag was rolled back
first.
