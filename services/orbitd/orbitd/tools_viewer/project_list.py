"""orbit_project_list — catalog projects, environments and components."""

from __future__ import annotations

from orbitd.roles import Listing, Registry, Role
from orbitd.state import AppState

from .common import component_names, env_names, guarded, project_entries


def register(registry: Registry, state: AppState) -> None:

    @registry.tool(role=Role.viewer, read_only=True, idempotent=True)
    def orbit_project_list() -> Listing:
        """List every project in the instance catalog with its environments and
        components. Reads catalog/projects.yaml through the instance backend; an unset
        instance dir is the actionable failure `backend_unconfigured`. The `id` values
        are what orbit_catalog_get, orbit_deploy_status and orbit_workload_list accept.
        Example: orbit_project_list() -> {items: [{id: "demo", environments: ["prod"], ...}]."""
        return guarded(lambda: _list(state))


def _list(state: AppState) -> Listing:
    items = []
    for entry in project_entries(state):
        pid = entry.get("id")
        if not pid:
            continue
        items.append({
            "id": pid,
            "name": entry.get("name") or pid,
            "environments": env_names(entry),
            "components": component_names(entry),
        })
    return Listing.of(items)
