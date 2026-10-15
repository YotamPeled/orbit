"""orbit_catalog_get — one catalog entry, or the whole document."""

from __future__ import annotations

from typing import Annotated, Any

from pydantic import BaseModel, Field

from orbitd.roles import Registry, Role
from orbitd.state import AppState

from .common import find_project, guarded, not_found, projects_doc


class CatalogGet(BaseModel):
    """The catalog document, or one project entry when `project` is set."""

    data: dict[str, Any]


def register(registry: Registry, state: AppState) -> None:

    @registry.tool(role=Role.viewer, read_only=True, idempotent=True)
    def orbit_catalog_get(
        project: Annotated[str | None, Field(
            default=None,
            description="Project id from orbit_project_list. Omit to return the whole catalog.",
        )] = None,
    ) -> CatalogGet:
        """Return the instance catalog as the reader stored it. Pass `project` to get
        that one entry; omit it for the full document (version plus projects[]). Unknown
        ids fail `not_found`. The entry's `id`, environment keys and component names are
        the arguments the deploy and workload tools accept.
        Example: orbit_catalog_get(project="demo") -> {data: {id: "demo", ...}}."""
        return guarded(lambda: _get(state, project))


def _get(state: AppState, project: str | None) -> CatalogGet:
    if not project:
        doc = projects_doc(state)
        return CatalogGet(data=doc if isinstance(doc, dict) else {})
    entry = find_project(state, project)
    if entry is None:
        return not_found(f"project {project!r} is not in the catalog", project=project)
    return CatalogGet(data=entry)
