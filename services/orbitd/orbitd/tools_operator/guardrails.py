"""orbit_db_backup, orbit_db_restore, orbit_project_remove: the three jobs an agent used
to do from a laptop with a desk script, around orbitd. Each runs that same script in a
desk Job on the cluster (orbitd/desk_job.py), behind the role gate, a confirmation and
the audit log, and each keeps the rule the script was written for: no data is changed or
deleted without a backup that was proven to restore."""

from __future__ import annotations

from dataclasses import replace as dataclass_replace

import logging
import re
from typing import Annotated, Any, Callable

from mcp.server.mcpserver import Context
from mcp_types import InputRequiredResult
from pydantic import Field

from .. import desk_job, features_job
from ..backends import CNPG_GROUP, CNPG_PLURAL, CNPG_VERSION, BackendError, BackendUnconfigured
from ..deploy import actor_of, audit, runtime_of, scrub, start_job
from ..deploy import _mutation  # noqa: PLC2701 - the deploy job's own helper
from ..roles import Mutation, Registry, Role, Step, failure
from ..source import read_source, sourced
from ..state import AppState
from ..tools import confirmation, refused
from ..tools_viewer import db_status
from ..tools_viewer.common import find_project, not_found, ns, require_env
from .catalog import commit_catalog
from ..tools_viewer.util import Ident

BACKUP_TOOL = "orbit_db_backup"
RESTORE_TOOL = "orbit_db_restore"
REMOVE_TOOL = "orbit_project_remove"
PROJECT_LABEL = "orbit.local/project"
BACKUP_NAME = r"^(latest|[a-z0-9][a-z0-9-]{0,62}-db-[0-9]{8}T[0-9]{6}Z\.sql\.gz)$"
INSTANT = r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z$"
BACKUP_S, RESTORE_S, REMOVE_S = 1800, 3600, 3600
OUTPUT_LINES = 60
_BACKUP_OK = re.compile(r"^BACKUP OK: \S*?([^/\s]+\.sql\.gz)$")


def register(registry: Registry, state: AppState) -> None:

    @registry.tool(role=Role.operator, read_only=False, destructive=False)
    def orbit_db_backup(
        project: Annotated[Ident, Field(description="Project id from orbit_project_list.")],
        env: Annotated[Ident, Field(description="Environment name, e.g. prod or dev.")],
        ctx: Context = None,  # type: ignore[assignment]
    ) -> Mutation | InputRequiredResult:
        """Back up the database of one project environment now, and prove the backup
        restores. Fails `not_found` when the catalog entry declares no database.
        Confirms, then starts a job with steps `confirm, backup`: a Kubernetes Job in
        namespace `orbit` dumps the database (pg_dump, with the per-table row counts
        read in the same snapshot), restores the dump into a scratch database on the
        same server, compares every table's row count, drops the scratch database,
        keeps the file on the platform's backups volume and, when the platform has a
        bucket, copies it there and reads it back (sha256). A dump that does not
        restore, or restores with other counts, is not a backup: the step fails with
        the script's `BACKUP REFUSED:` sentence. `evidence.job_id` is the job; wait
        with orbit_job_wait. On success `evidence.backup` is the file's name (what
        orbit_db_restore takes), `evidence.verified` the tally line and
        `evidence.output` the last lines the script printed. The database stays in
        service throughout; the scratch copy needs room for a second copy of the data
        while the check runs. Operator or admin.
        Example: orbit_db_backup(project="demo", env="prod")."""
        return _backup(state, project, env, ctx)

    @registry.tool(role=Role.admin, read_only=False, destructive=True)
    def orbit_db_restore(
        project: Annotated[Ident, Field(description="Project id from orbit_project_list.")],
        env: Annotated[Ident, Field(description="Environment name, e.g. prod or dev.")],
        backup: Annotated[str | None, Field(
            pattern=BACKUP_NAME,
            description="A backup's file name as orbit_db_backup returned it in evidence.backup, or `latest`. "
                        "Give this or `to`, not both.")] = None,
        to: Annotated[str | None, Field(
            pattern=INSTANT,
            description="A moment to restore to, RFC3339 in UTC to the second (2026-10-04T15:00:00Z). "
                        "prod only, and only while the backups feature is on.")] = None,
        dry_run: Annotated[bool, Field(
            description="Load that state into a scratch copy and report each table's row count beside the live "
                        "one; change nothing on the live database.")] = False,
        ctx: Context = None,  # type: ignore[assignment]
    ) -> Mutation | InputRequiredResult:
        """Replace the data of one project environment's database with an earlier
        state: a backup file (`backup`), or a point in time (`to`, a prod database
        whose write-ahead log is archived to the bucket). Everything written after
        that state is lost. A backup file that is not on the platform's backups volume
        (the node was rebuilt, the volume is new) is fetched from the bucket, and
        `latest` is the newest of the volume's and the bucket's. `refused` before anything runs when neither or both of
        `backup` and `to` are given, when `to` is asked of an env other than prod or
        while the backups feature is off, and when `to` is in the future or before the
        database's first base backup (orbit_db_status: `first_recoverability_point`,
        the earliest moment there is). `dry_run` asks for no confirmation and
        starts a job with the one step `compare`: the state is loaded into a scratch
        copy (for `to`, a throwaway namespace restored from the bucket, which takes
        minutes), `evidence.output` carries a line per table with the live row count
        beside that state's, and the job ends `dry_run` with the live database
        untouched. Read it before restoring: a table the loss emptied must show its
        rows on the restored side. Without `dry_run`: confirms, then starts a job with
        steps `confirm, backup, restore`: `backup` first backs up the live database as it
        is now, exactly as orbit_db_backup does; if that backup cannot be made and
        proven, `restore` is `skipped` and the live database is not touched. `restore`
        loads the chosen state into a scratch copy and replaces the live data in one
        transaction, then checks that every table holds the restored row count and
        belongs to the database's owner. `evidence.job_id` is the job; wait with
        orbit_job_wait. `evidence.pre_restore_backup` names the backup of the state
        before the restore (restore it to undo), `evidence.output` the last lines the
        script printed, among them the per-table counts. The application is not
        restarted. Admin only.
        Example: orbit_db_restore(project="demo", env="prod", backup="latest")."""
        return _restore(state, project, env, backup, to, dry_run, ctx)

    @registry.tool(role=Role.admin, read_only=False, destructive=True)
    def orbit_project_remove(
        project: Annotated[Ident, Field(description="Project id from orbit_project_list, or the id of a project "
                                                    "already deleted from the catalog whose namespaces are still there.")],
        dry_run: Annotated[bool, Field(
            description="Report the namespaces and databases that would go; do not confirm or delete.")] = False,
        ctx: Context = None,  # type: ignore[assignment]
    ) -> Mutation | InputRequiredResult:
        """Remove a project completely: its catalog entry, its namespaces with
        everything in them, its databases, and its monitoring objects. Fails
        `not_found` when the project is neither in the catalog nor on the cluster.
        `dry_run` returns `evidence.in_catalog`, `evidence.namespaces` and
        `evidence.databases` with no confirmation. Otherwise confirms, naming them,
        then starts a job under the project's lock with steps `confirm, commit,
        checkout, monitors, backup, remove`. `commit` deletes the entry from
        catalog/projects.yaml (`catalog-commit.sh remove <project>`: one commit with
        the trailers `Orbit-Action: catalog-remove`, `Orbit-Actor`, `Orbit-Role`,
        `Orbit-Project`; the generated manifests, the project's sealed secret files and
        the files of its monitors go in the same commit) and `checkout` waits for this
        orbitd to check it out and for its commit gate to accept it; both are `skipped`
        for a project already gone from the catalog. `monitors` deletes the rules and
        probes of the project's monitors (their files went with the commit), before
        anything they watch is deleted; if it fails, `backup` and `remove` are
        `skipped` and calling the tool again resumes. `backup` backs up every database
        in the project's namespaces as orbit_db_backup does (dumped, restored into a
        scratch database, row counts equal); if any backup is refused, `remove` is
        `skipped` and nothing on the cluster is deleted (the catalog commit stays, and
        calling the tool again resumes from `backup`). `remove` prunes the platform's
        `services` app, deletes the monitoring objects and the namespaces, and proves
        nothing labelled with the project is left. `evidence.job_id` is the job; wait
        with orbit_job_wait. `evidence.backups` names the backup files, which stay on
        the platform's backups volume (and in the bucket when there is one) after the
        namespaces are gone. Admin only.
        Example: orbit_project_remove(project="demo", dry_run=true)."""
        return _remove(state, project, dry_run, ctx)


# ------------------------------------------------------------------------------ shared

desk_log = logging.getLogger("orbitd.desk")
FAILED_JOB_LINES = 40

def _fail(tool: str, actor: str, project: str, env: str, code: str, what: str, **kw: Any) -> Any:
    audit(actor=actor, tool=tool, project=project, env=env, component="-", tag="-", verdict="failed")
    return failure(code, what, project=project, **({"env": env} if env else {}), **kw)


def _prepared(state: AppState, tool: str, actor: str, project: str, env: str) -> Any:
    """(settings, commit), or the failure that says why a desk Job cannot run."""
    settings = desk_job.DeskSettings.from_env()
    if settings is None:
        return _fail(tool, actor, project, env, "backend_unconfigured", desk_job.UNCONFIGURED,
                     next_action="upgrade the platform to a core release whose orbitd deployment names its desk image")
    source = read_source(state)
    commit = source.commit if source else None
    if not commit:
        return _fail(tool, actor, project, env, "backend_error",
                     "this orbitd has no instance commit checked out yet", retryable=True)
    return settings, commit


def _lines(log: str) -> list[str]:
    return [line.rstrip() for line in scrub(log or "").splitlines() if line.strip()]


def _said(lines: list[str], *prefixes: str) -> str | None:
    """The last line the script printed that starts with one of `prefixes`."""
    for line in reversed(lines):
        if line.startswith(prefixes):
            return line[:600]
    return None


def _backups(lines: list[str]) -> list[str]:
    return [m.group(1) for line in lines if (m := _BACKUP_OK.match(line))]


def _run_desk(state: AppState, *, settings: desk_job.DeskSettings, commit: str, kind: str, script: str,
              args: list[str], deadline_s: int) -> tuple[str, list[str], str | None]:
    """(outcome, the script's lines, the Job's name). outcome `unavailable` carries one
    line saying why the Job was not started."""
    runtime = runtime_of(state)
    kube = state.backends.kube
    try:
        name = desk_job.start(kube, settings, commit, kind=kind, script=script, args=args, deadline_s=deadline_s)
        outcome, log = desk_job.wait(kube, name, clock=runtime.clock, sleep=runtime.sleeper,
                                     timeout_s=float(deadline_s) + 120.0)
    except desk_job.DeskJobUnavailable as e:
        return "unavailable", [str(e)], None
    except Exception as e:
        return "unavailable", [scrub(f"{e.__class__.__name__}: {e}")[:600]], None
    lines = _lines(log)
    if outcome == "failed":
        _read_then_gone(kube, name, kind, lines)
    return outcome, lines, name


def _read_then_gone(kube: Any, name: str, kind: str, lines: list[str]) -> None:
    """A desk Job that failed has been read: its caller gets the outcome and the script's
    sentence, orbitd's own log keeps its last lines, and the Job is deleted.

    Measured on the test box (docs/V1.md §8): a removal refused for want of a provable
    backup, the tool working as it should, left a failed Job that kept `KubeJobFailed`
    firing for the day the Job is kept. An alert is for what nobody has been told. A Job
    orbitd never got to read (it restarted, or stopped waiting) stays and still alerts."""
    for line in lines[-FAILED_JOB_LINES:]:
        desk_log.warning("desk job %s (%s) failed: %s", name, kind, line)
    try:
        features_job.delete(kube, name)
    except Exception as e:
        desk_log.warning("desk job %s (%s) failed and could not be deleted: %s", name, kind,
                         scrub(f"{e.__class__.__name__}: {e}")[:300])


def _start(state: AppState, *, tool: str, actor: str, project: str, env: str, tag: str,
           run: Callable[[list[Step]], Mutation], deadline_s: int, last_step: str,
           confirmed: bool = True) -> Any:
    opening = [Step(name="confirm", status="ok")] if confirmed else []
    return sourced(state, start_job(
        state, project=project, env=env, fn=lambda: run(list(opening)), actor=actor, tool=tool,
        component="-", tag=tag, opening=opening, budget_s=float(deadline_s) + 300.0,
        timeout_step=(last_step, "TIMED OUT")))


def _done(state: AppState, status: str, steps: list[Step], evidence: dict[str, Any]) -> Mutation:
    return _mutation(status=status, steps=steps, commit=None, evidence=evidence, source=read_source(state))


# ------------------------------------------------------------------------------ backup

def _database(state: AppState, project: str, env: str) -> Any:
    """None when the pair has a database in the catalog, else the failure."""
    entry = find_project(state, project)
    if entry is None:
        return not_found(f"project {project!r} is not in the catalog", project=project)
    if (missing := require_env(entry, project, env)) is not None:
        return missing
    if not entry.get("database"):
        return not_found(f"project {project!r} declares no database", project=project, env=env)
    return None


def _backup(state: AppState, project: str, env: str, ctx: Context | None) -> Any:
    actor = actor_of()
    try:
        if (missing := _database(state, project, env)) is not None:
            return missing
    except (BackendError, BackendUnconfigured) as e:
        return e.as_failure()
    ready = _prepared(state, BACKUP_TOOL, actor, project, env)
    if not isinstance(ready, tuple):
        return ready
    settings, commit = ready
    verdict = confirmation(ctx, (
        f"Back up the database of {project}/{env} now? It is dumped, the dump is restored into a scratch "
        f"database on the same server to prove it (room for a second copy of the data is needed while "
        f"that runs), and the file is kept on the platform's backups volume and in the bucket when there "
        f"is one. The database stays in service.")) if ctx is not None else True
    if isinstance(verdict, InputRequiredResult):
        return verdict
    if not verdict:
        audit(actor=actor, tool=BACKUP_TOOL, project=project, env=env, component="-", tag="-", verdict="refused")
        return refused(f"the backup of {project}/{env} was not confirmed")

    def run(steps: list[Step]) -> Mutation:
        outcome, lines, job = _run_desk(state, settings=settings, commit=commit, kind="db-backup",
                                        script="db-backup.sh", args=[ns(project, env), f"{project}-db"],
                                        deadline_s=BACKUP_S)
        evidence: dict[str, Any] = {"project": project, "env": env, "desk_job": job,
                                    "output": lines[-OUTPUT_LINES:]}
        made = _backups(lines)
        if outcome == "succeeded" and made:
            db_status.record_file_backup(state, ns(project, env), f"{project}-db", made[-1])
            evidence["backup"] = made[-1]
            evidence["verified"] = _said(lines, "verified: ")
            evidence["bucket"] = _said(lines, "bucket copy: ")
            steps.append(Step(name="backup", status="ok", detail=made[-1]))
            return _done(state, "applied", steps, evidence)
        detail = _said(lines, "BACKUP REFUSED: ") or (lines[-1] if lines else f"the backup Job {outcome}")
        steps.append(Step(name="backup", status="failed", detail=detail[:600]))
        return _done(state, "failed", steps, evidence)

    return _start(state, tool=BACKUP_TOOL, actor=actor, project=project, env=env, tag="backup",
                  run=run, deadline_s=BACKUP_S, last_step="backup")


# ------------------------------------------------------------------------------ restore

FUTURE_SLACK_S = 60.0


def _epoch(instant: str | None) -> float | None:
    """Seconds since the epoch for an RFC 3339 instant in UTC; None when it is not one."""
    from datetime import datetime, timezone
    try:
        text = str(instant).strip().replace("Z", "+00:00")
        moment = datetime.fromisoformat(text)
    except (TypeError, ValueError):
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.timestamp()


def _instant(epoch: float) -> str:
    import time as _time
    return _time.strftime("%Y-%m-%dT%H:%M:%SZ", _time.gmtime(epoch))


def _restore(state: AppState, project: str, env: str, backup: str | None, to: str | None,
             dry_run: bool, ctx: Context | None) -> Any:
    actor = actor_of()
    try:
        if (missing := _database(state, project, env)) is not None:
            return missing
    except (BackendError, BackendUnconfigured) as e:
        return e.as_failure()
    if bool(backup) == bool(to):
        return _fail(RESTORE_TOOL, actor, project, env, "refused",
                     "give exactly one of `backup` (a backup's file name, or `latest`) and `to` (a moment in UTC)",
                     next_action="orbit_db_backup returns a backup's name in evidence.backup")
    if to:
        if env != "prod":
            return _fail(RESTORE_TOOL, actor, project, env, "refused",
                         f"a point in time exists only for a prod database (its write-ahead log is archived "
                         f"to the bucket); restore {project}/{env} from a backup file",
                         next_action="pass `backup` instead of `to`")
        if not state.features.snapshot().on.get("backups", True):
            return _fail(RESTORE_TOOL, actor, project, env, "refused",
                         "a point in time needs the backups feature, which is off on this platform: no "
                         "write-ahead log is archived",
                         next_action="restore from a backup file (`backup`), or an admin turns the feature on: "
                                     "`orbit features enable backups`")
        # A moment the archive cannot give is refused here, in a second. Asked anyway,
        # each ran a restore into a scratch namespace for ten minutes and failed with
        # "did not become healthy" and raw pod logs (QA of v1.0.0, F32).
        moment = _epoch(to)
        import time as _time
        now = _time.time()            # the wall clock: `to` is a wall-clock moment
        if moment is not None and moment > now + FUTURE_SLACK_S:
            return _fail(RESTORE_TOOL, actor, project, env, "refused",
                         f"{to} is in the future (it is {_instant(now)} now): there is nothing to restore to yet",
                         next_action="pass a moment in the past, in UTC")
        try:
            first, _ = db_status.recovery_window(state.backends.kube, ns(project, env), f"{project}-db")
        except Exception:
            first = None
        earliest = _epoch(first) if first else None
        if moment is not None and earliest is not None and moment < earliest:
            return _fail(RESTORE_TOOL, actor, project, env, "refused",
                         f"{to} is before the earliest moment this database can be restored to, {first} (its "
                         f"first base backup in the bucket): no archive reaches that far back",
                         next_action=f"pass a moment at or after {first} (orbit_db_status shows it as "
                                     f"first_recoverability_point), or restore a backup file with `backup`",
                         first_recoverability_point=first)
    ready = _prepared(state, RESTORE_TOOL, actor, project, env)
    if not isinstance(ready, tuple):
        return ready
    settings, commit = ready
    chosen = ["--to", to] if to else ["--from", str(backup)]
    if dry_run:
        def compare(steps: list[Step]) -> Mutation:
            outcome, lines, job = _run_desk(state, settings=settings, commit=commit, kind="db-compare",
                                            script="db-restore.sh", args=[project, env, *chosen],
                                            deadline_s=RESTORE_S)
            evidence: dict[str, Any] = {"project": project, "env": env, "desk_job": job,
                                        "compared": to or backup, "output": lines[-OUTPUT_LINES:]}
            if outcome == "succeeded" and _said(lines, "nothing changed on the live database"):
                steps.append(Step(name="compare", status="ok", detail=f"{project}/{env} beside {to or backup}"))
                return _done(state, "dry_run", steps, evidence)
            detail = (_said(lines, "RESTORE REFUSED: ", "FAIL: ")
                      or (lines[-1] if lines else f"the comparison Job {outcome}"))
            steps.append(Step(name="compare", status="failed", detail=detail[:600]))
            return _done(state, "failed", steps, evidence)

        return _start(state, tool=RESTORE_TOOL, actor=actor, project=project, env=env, tag="compare",
                      run=compare, deadline_s=RESTORE_S, last_step="compare", confirmed=False)
    what = f"its state at {to}" if to else ("its latest backup" if backup == "latest" else f"the backup {backup}")
    verdict = confirmation(ctx, (
        f"Replace the data of {project}/{env}'s database with {what}? Everything written after that state "
        f"is LOST. The live database is backed up first, and if that backup cannot be proven to restore, "
        f"nothing is changed.")) if ctx is not None else True
    if isinstance(verdict, InputRequiredResult):
        return verdict
    if not verdict:
        audit(actor=actor, tool=RESTORE_TOOL, project=project, env=env, component="-", tag="-", verdict="refused")
        return refused(f"the restore of {project}/{env} was not confirmed")
    args = [project, env, *chosen, "--yes"]

    def run(steps: list[Step]) -> Mutation:
        outcome, lines, job = _run_desk(state, settings=settings, commit=commit, kind="db-restore",
                                        script="db-restore.sh", args=args, deadline_s=RESTORE_S)
        evidence: dict[str, Any] = {"project": project, "env": env, "desk_job": job,
                                    "restored": to or backup, "output": lines[-OUTPUT_LINES:]}
        made = _backups(lines)
        if not made:
            # no proven backup of the live state: the script stopped before touching it
            detail = (_said(lines, "BACKUP REFUSED: ", "RESTORE REFUSED: ")
                      or (lines[-1] if lines else f"the restore Job {outcome}"))
            steps.append(Step(name="backup", status="failed", detail=detail[:600]))
            steps.append(Step(name="restore", status="skipped",
                              detail="the live database was not backed up, so it was not changed"))
            return _done(state, "failed", steps, evidence)
        evidence["pre_restore_backup"] = made[0]
        db_status.record_file_backup(state, ns(project, env), f"{project}-db", made[0])
        steps.append(Step(name="backup", status="ok", detail=made[0]))
        said = _said(lines, "RESTORED: ")
        if outcome == "succeeded" and said:
            steps.append(Step(name="restore", status="ok", detail=said))
            return _done(state, "applied", steps, evidence)
        detail = (_said(lines, "RESTORE REFUSED: ", "RESTORE NOT PROVEN: ", "LOADED, BUT NOT OWNED", "FAIL: ")
                  or (lines[-1] if lines else f"the restore Job {outcome}"))
        steps.append(Step(name="restore", status="failed", detail=detail[:600]))
        return _done(state, "failed", steps, evidence)

    return _start(state, tool=RESTORE_TOOL, actor=actor, project=project, env=env, tag="restore",
                  run=run, deadline_s=RESTORE_S, last_step="restore")


# ------------------------------------------------------------------------------ remove

def _left_behind(state: AppState, project: str) -> tuple[list[str], list[str]]:
    """(namespaces, databases as namespace/name) labelled with the project."""
    kube = state.backends.kube
    listed = kube.core.list_namespace(label_selector=f"{PROJECT_LABEL}={project}")
    namespaces = []
    for item in getattr(listed, "items", None) or []:
        meta = item.to_dict().get("metadata") if hasattr(item, "to_dict") else (item.get("metadata") or {})
        labels = (meta or {}).get("labels") or {}
        if labels.get(PROJECT_LABEL) == project and (meta or {}).get("name"):
            namespaces.append(str(meta["name"]))
    databases = []
    for namespace in sorted(namespaces):
        found = kube.custom.list_namespaced_custom_object(CNPG_GROUP, CNPG_VERSION, namespace, CNPG_PLURAL)
        for cluster in (found or {}).get("items") or []:
            name = ((cluster or {}).get("metadata") or {}).get("name")
            if name:
                databases.append(f"{namespace}/{name}")
    return sorted(namespaces), databases


MONITORS_LOCK_WAIT_S = 90.0


def _remove_monitors(state: AppState, project: str) -> Step:
    """The `monitors` step of a removal: delete the rules and probes of the project's
    monitors, whose files the removal commit took out of the repository.

    Before the namespaces go, so that a monitor watching the project does not fire at
    its removal: on the test box the removed project's uptime monitor paged the chat two
    minutes after the project was deleted, and went on existing where no tool listed it
    (QA of v1.0.0, F40). Under the monitors lock, like a monitor delete."""
    from .. import monitors_apply
    from ..applier import MODE_ON, apply_mode
    mode = apply_mode(state)
    if mode != MODE_ON:
        return Step(name="monitors", status="skipped",
                    detail=f"ORBITD_APPLY={mode}: this orbitd deletes nothing; a monitor of the project keeps its rule")
    runtime = runtime_of(state)
    holder = f"remove-{project}"
    deadline = runtime.clock() + MONITORS_LOCK_WAIT_S
    while runtime.locks.try_acquire(monitors_apply.UNIT, "", holder) is not None:
        if runtime.clock() >= deadline:
            return Step(name="monitors", status="failed",
                        detail="another monitor change held the monitors lock for 90 s; call the tool again")
        runtime.sleeper(3.0)
    try:
        plan = monitors_apply.plan_monitors(state)
        # the monitors that remain, applied now: a log monitor of the project has no
        # object of its own, its rule leaves the shared ConfigMap when that is rendered
        # without it
        applied = monitors_apply.apply_monitors(state, plan, dry_run=False)
        if not applied.ok:
            bad = next((r for r in applied.results if r.result == "failed"), None)
            where = f"{bad.ref.kind} {bad.ref.namespace}/{bad.ref.name}: {bad.reason}" if bad else "apply failed"
            return Step(name="monitors", status="failed", detail=f"the monitors that remain could not be applied ({where})"[:600])
        rewrote = any(r.result == "applied" and r.ref.kind == "ConfigMap" and r.ref.name == monitors_apply.LOG_CONFIGMAP
                      for r in applied.results)
        logs = "; the log monitors' rules were rewritten without the project's" if rewrote else ""
        refs = monitors_apply.prune_refs_for_project(state, plan, project)
        if not refs:
            return Step(name="monitors", status="ok", detail="no monitor of this project is on the cluster" + logs
                        if not rewrote else "the log monitors' rules were rewritten without the project's")
        pruned = monitors_apply.prune_monitors(state, dataclass_replace(plan, prune=refs), dry_run=False)
    except Exception as e:
        return Step(name="monitors", status="failed", detail=scrub(f"{e.__class__.__name__}: {e}")[:600])
    finally:
        runtime.locks.release(monitors_apply.UNIT, "", holder)
    bad = next((r for r in pruned.results if r.result in ("failed", "withheld")), None)
    if not pruned.ok or bad is not None:
        where = f"{bad.ref.kind} {bad.ref.namespace}/{bad.ref.name}: {bad.reason}" if bad else "the delete failed"
        return Step(name="monitors", status="failed", detail=where[:600])
    names = sorted({r.ref.name[len(monitors_apply.MONITOR_PREFIX):] if r.ref.name.startswith(monitors_apply.MONITOR_PREFIX)
                    else r.ref.name for r in pruned.results})
    return Step(name="monitors", status="ok",
                detail=f"{len(pruned.results)} object(s) of monitor(s) {', '.join(names)} deleted" + logs)


def _remove(state: AppState, project: str, dry_run: bool, ctx: Context | None) -> Any:
    actor = actor_of()
    try:
        in_catalog = find_project(state, project) is not None
        namespaces, databases = _left_behind(state, project)
    except (BackendError, BackendUnconfigured) as e:
        return e.as_failure()
    if not in_catalog and not namespaces:
        return not_found(f"project {project!r} is not in the catalog and no namespace carries its label",
                         project=project)
    evidence: dict[str, Any] = {"project": project, "in_catalog": in_catalog,
                                "namespaces": namespaces, "databases": databases}
    if dry_run:
        audit(actor=actor, tool=REMOVE_TOOL, project=project, env="", component="-", tag="-", verdict="dry_run")
        return sourced(state, Mutation(status="dry_run", steps=[], evidence=evidence))
    ready = _prepared(state, REMOVE_TOOL, actor, project, "")
    if not isinstance(ready, tuple):
        return ready
    settings, commit = ready
    parts = [f"Remove project {project} completely?"]
    if in_catalog:
        parts.append("Deletes its entry from the catalog (one commit).")
    parts.append("Deletes the namespace(s) " + ", ".join(namespaces) + " and everything in them."
                 if namespaces else "No namespace carries its label; its monitoring objects are deleted.")
    if databases:
        parts.append("Its database(s) " + ", ".join(databases) + " are DELETED with them. Each is backed up "
                     "first and the backup proven to restore; if one cannot be, nothing is deleted.")
    verdict = confirmation(ctx, " ".join(parts)) if ctx is not None else True
    if isinstance(verdict, InputRequiredResult):
        return verdict
    if not verdict:
        audit(actor=actor, tool=REMOVE_TOOL, project=project, env="", component="-", tag="-", verdict="refused")
        return refused(f"the removal of {project} was not confirmed")

    def run(steps: list[Step]) -> Mutation:
        at = commit
        if in_catalog:
            landed = commit_catalog(state, "remove", project, actor=actor, role="admin", entry=None, steps=steps)
            if landed is None:
                if steps[-1].name == "commit":
                    steps.append(Step(name="checkout", status="skipped", detail="no commit landed"))
                steps.append(Step(name="backup", status="skipped", detail="the catalog entry was not removed"))
                steps.append(Step(name="remove", status="skipped", detail="nothing on the cluster was deleted"))
                return _done(state, "failed", steps, dict(evidence))
            at = landed
        else:
            gone = "the project is already gone from the catalog"
            steps += [Step(name="commit", status="skipped", detail=gone), Step(name="checkout", status="skipped", detail=gone)]
        watched = _remove_monitors(state, project)
        steps.append(watched)
        if watched.status == "failed":
            steps.append(Step(name="backup", status="skipped", detail="the project's monitors were not removed"))
            steps.append(Step(name="remove", status="skipped", detail="nothing in the project's namespaces was deleted"))
            return _done(state, "failed", steps, dict(evidence))
        outcome, lines, job = _run_desk(state, settings=settings, commit=at, kind="remove",
                                        script="remove-project.sh", args=[project, "--yes"],
                                        deadline_s=REMOVE_S)
        out = dict(evidence, desk_job=job, output=lines[-OUTPUT_LINES:])
        if in_catalog:
            out["commit"] = at
        made = _backups(lines)
        out["backups"] = made
        said = _said(lines, f"REMOVED: {project}")
        refusal = _said(lines, "BACKUP REFUSED: ", "REFUSED: ")
        if refusal and not said:
            steps.append(Step(name="backup", status="failed", detail=refusal))
            steps.append(Step(name="remove", status="skipped", detail="no verified backup, so nothing was deleted"))
            return _done(state, "failed", steps, out)
        if not said and len(made) < len(databases):
            # a database with no proven backup and no refusal line: the script did not get
            # that far; whatever it printed last says where it stopped
            detail = lines[-1] if lines else f"the removal Job {outcome}"
            steps.append(Step(name="backup", status="failed", detail=detail[:600]))
            steps.append(Step(name="remove", status="skipped", detail="not every database was backed up, so nothing was deleted"))
            return _done(state, "failed", steps, out)
        steps.append(Step(name="backup", status="ok",
                          detail=", ".join(made) if made else "no database to back up"))
        if outcome == "succeeded" and said:
            db_status.forget_file_backups(state, namespaces)
            steps.append(Step(name="remove", status="ok", detail=said))
            return _done(state, "applied", steps, out)
        detail = _said(lines, "NOT REMOVED: ") or (lines[-1] if lines else f"the removal Job {outcome}")
        steps.append(Step(name="remove", status="failed", detail=detail[:600]))
        return _done(state, "failed", steps, out)

    return _start(state, tool=REMOVE_TOOL, actor=actor, project=project, env="", tag="remove",
                  run=run, deadline_s=REMOVE_S + int(runtime_of(state).runner_timeout_s) + 240, last_step="remove")
