"""orbit_workload_list — pods in a project environment."""

from __future__ import annotations

from typing import Annotated

from pydantic import Field

from orbitd.roles import Registry, Role
from orbitd.state import AppState

from .util import Ident
from .common import (
    require_env,
    WORKLOAD_CAP, ClockedListing, clocked, find_project, guarded, list_pods, not_found,
    ns, paginate, pod_row,
)


def register(registry: Registry, state: AppState) -> None:

    @registry.tool(role=Role.viewer, read_only=True, idempotent=True)
    def orbit_workload_list(
        project: Annotated[Ident, Field(description="Project id from orbit_project_list.")],
        env: Annotated[Ident, Field(description="Environment name, e.g. prod or dev.")],
        limit: Annotated[int, Field(
            default=WORKLOAD_CAP, ge=1, le=WORKLOAD_CAP,
            description="Page size. Capped at 100.",
        )] = WORKLOAD_CAP,
        cursor: Annotated[str | None, Field(
            default=None,
            description="Pagination cursor from a previous next_cursor.",
        )] = None,
    ) -> ClockedListing:
        """List pods in namespace `<project>-<env>`. Each item is {pod, component,
        phase, ready, restarts, last_terminated_reason, image, age, node}; `pod` is
        what orbit_workload_describe's events refer to.
        `age` is seconds since the pod was created. `now` is the server clock as RFC 3339.
        The list is capped at 100 items; pass next_cursor to continue.
        Example: orbit_workload_list(project="demo", env="prod")."""
        return guarded(lambda: _list(state, project, env, limit, cursor))


def _list(state: AppState, project: str, env: str, limit: int, cursor: str | None) -> ClockedListing:
    entry = find_project(state, project)
    if entry is None:
        return not_found(f"project {project!r} is not in the catalog", project=project)
    if (missing := require_env(entry, project, env)) is not None:
        return missing
    pods = [pod_row(p) for p in list_pods(state.backends.kube, ns(project, env))]
    return clocked(paginate(pods, cursor=cursor, limit=limit, cap=WORKLOAD_CAP))
