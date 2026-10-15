"""In-process job registry. Long operations return a handle; status/wait reclaim it.

Bounded to 100 records and 24 hours (DESIGN-v2 §3.4). No viewer tool creates jobs yet;
mutating tools will, and tests create them directly.
"""

from __future__ import annotations

import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

MAX_JOBS = 100
JOB_TTL_SECONDS = 24 * 3600
TERMINAL = frozenset({"completed", "failed", "cancelled"})
STATUSES = frozenset({"pending", "running", "completed", "failed", "cancelled"})


@dataclass
class Job:
    id: str
    status: str
    created_at: float
    result: Any = None
    error: str | None = None
    event: threading.Event = field(default_factory=threading.Event)
    # what the job is, for the tools that act on a job by id (orbit_job_cancel):
    # {project, env, tool, component, tag, actor}; empty for a job made without them
    meta: dict[str, Any] = field(default_factory=dict)


class Jobs:
    """Process-local job table. create / get / wait; the oldest terminal records fall off
    the cap. A running job is never evicted by the cap: its handle is what a waiter holds
    and what its own completion writes to. The table can therefore stand above the cap
    by the number of jobs running at once, which the env locks bound to one per pair."""

    def __init__(self) -> None:
        self._jobs: dict[str, Job] = {}
        self._lock = threading.Lock()

    def create(self, *, status: str = "pending", result: Any = None,
               error: str | None = None, job_id: str | None = None) -> Job:
        if status not in STATUSES:
            raise ValueError(f"unknown job status {status!r}")
        job = Job(
            id=job_id or uuid.uuid4().hex,
            status=status,
            created_at=time.time(),
            result=result,
            error=error,
        )
        if status in TERMINAL:
            job.event.set()
        with self._lock:
            self._gc_locked()
            while len(self._jobs) >= MAX_JOBS:
                terminal = [j for j in self._jobs.values() if j.status in TERMINAL]
                if not terminal:
                    break
                oldest = min(terminal, key=lambda j: j.created_at)
                self._jobs.pop(oldest.id, None)
            self._jobs[job.id] = job
        return job

    def get(self, job_id: str) -> Job | None:
        with self._lock:
            self._gc_locked()
            return self._jobs.get(job_id)

    def wait(self, job_id: str, timeout_s: float) -> Job | None:
        job = self.get(job_id)
        if job is None:
            return None
        if job.status in TERMINAL or timeout_s <= 0:
            return job
        job.event.wait(timeout=timeout_s)
        return self.get(job_id) or job

    def complete(self, job_id: str, *, status: str = "completed",
                 result: Any = None, error: str | None = None) -> Job | None:
        if status not in TERMINAL:
            raise ValueError(f"{status!r} is not a terminal job status")
        with self._lock:
            self._gc_locked()
            job = self._jobs.get(job_id)
            if job is None:
                return None
            job.status = status
            job.result = result
            job.error = error
            job.event.set()
            return job

    def _gc_locked(self) -> None:
        cutoff = time.time() - JOB_TTL_SECONDS
        expired = [jid for jid, job in self._jobs.items() if job.created_at < cutoff]
        for jid in expired:
            self._jobs.pop(jid, None)


__all__ = ["Job", "Jobs", "MAX_JOBS", "JOB_TTL_SECONDS", "TERMINAL"]
