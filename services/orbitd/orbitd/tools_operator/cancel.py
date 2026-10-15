"""orbit_job_cancel — operator: stop a ship, rollback or restart that is waiting.

A ship waits for its rollout for up to ten minutes and holds the pair's lock while it
does. When the new version will never become ready (its readiness check does not pass),
that wait is ten minutes in which a rollback, a restart and another ship are all
refused `busy`, and there was nothing to do but watch (QA of v1.0.0, F18). Cancelling
tells the job to stop waiting: it writes nothing more, gives its verdict, and frees the
pair. It undoes nothing: a tag bump that landed stays landed."""

from __future__ import annotations

from typing import Annotated, Any

from pydantic import Field

from ..deploy import actor_of, audit, runtime_of
from ..jobs import TERMINAL
from ..roles import Mutation, Registry, Role, Step, failure
from ..source import sourced
from ..state import AppState
from ..tools_viewer.common import jobs_of, not_found

TOOL = "orbit_job_cancel"
# The jobs whose wait is only a wait: stopping it leaves nothing half done. A database
# restore, a removal, a feature switch or a catalog change runs steps that must finish.
CANCELLABLE = frozenset({"orbit_deploy_ship", "orbit_deploy_rollback", "orbit_workload_restart"})
SETTLE_WAIT_S = 20.0


def register(registry: Registry, state: AppState) -> None:

    @registry.tool(role=Role.operator, read_only=False, destructive=False)
    def orbit_job_cancel(
        job_id: Annotated[str, Field(min_length=8, max_length=64,
                                     description="The running job's id: `evidence.job_id` of the ship, "
                                                 "rollback or restart, or `job_id` in a `busy` refusal.")],
    ) -> Mutation:
        """Stop a running ship, rollback or restart: the job stops waiting for its
        rollout, writes nothing more, and frees its project/environment, so a rollback
        or another ship can start at once instead of after the ten-minute rollout
        timeout. It undoes nothing: a tag bump the job already committed stays in the
        repository and stays applied (the old pods keep serving while the new ones are
        not ready), so follow with orbit_deploy_rollback or a ship of a good tag.
        Fails `not_found` for an unknown or expired id, `not_running` for a job that
        already ended, `not_cancellable` for any other kind of job (a database restore,
        a removal, a catalog or feature change must run to its end). No confirmation.
        Returns `applied` once the job has ended, its record `cancelled` (read it with
        orbit_job_status: its last step is `cancel`, `evidence.cancelled_by` the
        caller); `evidence.pending` true when it had not ended after 20 seconds (a
        commit script in flight finishes first). Operator or admin.
        Example: orbit_job_cancel(job_id="a1b2c3d4e5f6a7b8")."""
        return _cancel(state, job_id)


def _cancel(state: AppState, job_id: str) -> Any:
    actor = actor_of()
    jobs = jobs_of(state)
    job = jobs.get(job_id)
    if job is None:
        return not_found(f"job {job_id!r} is unknown or expired", job_id=job_id)
    meta = dict(getattr(job, "meta", None) or {})
    project, env, tool = str(meta.get("project") or ""), str(meta.get("env") or ""), str(meta.get("tool") or "")

    def said(verdict: str) -> None:
        audit(actor=actor, tool=TOOL, project=project or "-", env=env, component=str(meta.get("component") or "-"),
              tag=str(meta.get("tag") or "-"), verdict=verdict)

    if job.status in TERMINAL:
        said("failed")
        return failure("not_running", f"job {job_id} already ended ({job.status}); there is nothing to cancel",
                       next_action="read its result with orbit_job_status", job_id=job_id, status=job.status)
    if tool not in CANCELLABLE:
        said("failed")
        return failure(
            "not_cancellable",
            f"job {job_id} is {tool or 'not a ship, rollback or restart'}: only a ship, a rollback or a restart "
            f"can be cancelled (their wait is only a wait; this job's steps must run to their end)",
            next_action="wait for it with orbit_job_wait", job_id=job_id, tool=tool or None)
    runtime = runtime_of(state)
    if not runtime.locks.cancel(project, env, job_id, actor):
        said("failed")
        return failure("not_running", f"job {job_id} no longer holds {project}/{env}; it is ending",
                       next_action="read its result with orbit_job_status", retryable=True, job_id=job_id)
    ended = jobs.wait(job_id, SETTLE_WAIT_S)
    done = ended is not None and ended.status in TERMINAL
    said("applied")
    evidence: dict[str, Any] = {"job_id": job_id, "project": project, "env": env, "tool": tool,
                                "job_status": ended.status if ended is not None else "unknown"}
    if not done:
        evidence["pending"] = True
    return sourced(state, Mutation(
        status="applied",
        steps=[Step(name="cancel", status="ok",
                    detail=(f"{tool} {job_id[:8]} on {project}/{env} was told to stop and has ended; the pair is free"
                            if done else
                            f"{tool} {job_id[:8]} on {project}/{env} was told to stop; it had not ended after "
                            f"{int(SETTLE_WAIT_S)} s (a commit in flight finishes first): wait with orbit_job_wait"))],
        evidence=evidence))


__all__ = ["register", "CANCELLABLE"]
