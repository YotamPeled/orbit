"""orbit_job_wait — block until a job is terminal, up to timeout_s."""

from __future__ import annotations

from typing import Annotated

from pydantic import Field

from orbitd.roles import Registry, Role
from orbitd.state import AppState

from .common import guarded, jobs_of, not_found
from .job_status import JobRecord, to_record


def register(registry: Registry, state: AppState) -> None:

    @registry.tool(role=Role.viewer, read_only=True, idempotent=True)
    def orbit_job_wait(
        job_id: Annotated[str, Field(
            min_length=8, max_length=64,
            description="Job id from orbit_job_status or a mutating tool.",
        )],
        timeout_s: Annotated[int, Field(
            default=30, ge=0, le=120,
            description="Seconds to wait. Capped at 120; 0 returns the current record.",
        )] = 30,
    ) -> JobRecord:
        """Wait until the named job reaches a terminal status, or until timeout_s
        (at most 120). Returns the current record either way — pending if the wait
        ran out. Unknown ids fail `not_found`. The same `job_id` is what
        orbit_job_status accepts.
        Example: orbit_job_wait(job_id="a1b2c3d4e5f6a7b8", timeout_s=30)."""
        return guarded(lambda: _wait(state, job_id, timeout_s))


def _wait(state: AppState, job_id: str, timeout_s: int) -> JobRecord:
    job = jobs_of(state).wait(job_id, timeout_s)
    if job is None:
        return not_found(f"job {job_id!r} is unknown or expired", job_id=job_id)
    return to_record(job)
