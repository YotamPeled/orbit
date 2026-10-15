"""orbit_job_status — inspect one in-process job by id."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Annotated, Any

from pydantic import BaseModel, Field

from orbitd.jobs import Job, TERMINAL
from orbitd.roles import Registry, Role
from orbitd.state import AppState

from .common import guarded, jobs_of, not_found


class JobRecord(BaseModel):
    job_id: str
    status: str
    created_at: str
    result: Any = None
    error: str | None = None
    poll_after_ms: int | None = None


def to_record(job: Job) -> JobRecord:
    created = datetime.fromtimestamp(job.created_at, tz=timezone.utc).isoformat().replace("+00:00", "Z")
    return JobRecord(
        job_id=job.id,
        status=job.status,
        created_at=created,
        result=job.result,
        error=job.error,
        poll_after_ms=None if job.status in TERMINAL else 1000,
    )


def register(registry: Registry, state: AppState) -> None:

    @registry.tool(role=Role.viewer, read_only=True, idempotent=True)
    def orbit_job_status(
        job_id: Annotated[str, Field(
            min_length=8, max_length=64,
            description="Job id returned by a mutating tool (or created in tests).",
        )],
    ) -> JobRecord:
        """Return the current record for an in-process job. Jobs live at most 24 hours
        and the table keeps 100 records; an unknown or expired id fails `not_found`.
        No viewer tool creates jobs yet — mutating tools will, and tests create them
        directly. The same `job_id` is what orbit_job_wait accepts.
        Example: orbit_job_status(job_id="a1b2c3d4e5f6a7b8")."""
        return guarded(lambda: _status(state, job_id))


def _status(state: AppState, job_id: str) -> JobRecord:
    job = jobs_of(state).get(job_id)
    if job is None:
        return not_found(f"job {job_id!r} is unknown or expired", job_id=job_id)
    return to_record(job)
