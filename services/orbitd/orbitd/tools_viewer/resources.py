"""orbit:// resources. Every one is also reachable by a tool (DESIGN-v2 §3.5)."""

from __future__ import annotations

from typing import Any

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ResourceError, ResourceNotFoundError

from ..backends import BackendError, BackendUnconfigured
from ..roles import Registry
from ..state import AppState
from .catalog_schema import SCHEMA_REL, read_schema
from .ledger_get import LEDGER_CAP, collect_ledger
from .platform_budget import collect_budget
from .render_get import RenderConflict, read_render, render_path

# Each resource is also reachable by a tool; the role gate uses this map.
RESOURCE_TOOLS: tuple[tuple[str, str], ...] = (
    ("orbit://catalog/projects", "orbit_catalog_get"),
    ("orbit://catalog/schema", "orbit_catalog_schema"),
    ("orbit://state/tags", "orbit_deploy_status"),
    ("orbit://monitors", "orbit_monitor_list"),
    ("orbit://budget", "orbit_platform_budget"),
    ("orbit://render/", "orbit_render_get"),
    ("orbit://ledger/", "orbit_ledger_get"),
)


def bind(registry: Registry) -> None:
    for uri, tool in RESOURCE_TOOLS:
        registry.bind_resource(uri, tool)


def _instance_payload(state: AppState, reader) -> Any:
    try:
        return reader()
    except BackendUnconfigured as e:
        raise ResourceError(f"{e.variable} is not set") from e
    except BackendError as e:
        raise ResourceError(e.what_failed) from e


def mount(server: MCPServer, state: AppState) -> None:

    @server.resource("orbit://catalog/projects", mime_type="application/json",
                     description="The authored catalog (catalog/projects.yaml).")
    def catalog_projects() -> Any:
        return _instance_payload(state, state.backends.instance.projects)

    @server.resource("orbit://catalog/schema", mime_type="application/json",
                     description="JSON Schema for catalog values, from the vendored chart.")
    def catalog_schema() -> str:
        try:
            return read_schema(state)
        except BackendUnconfigured as e:
            raise ResourceError(f"{e.variable} is not set") from e
        except BackendError as e:
            raise ResourceNotFoundError(f"{SCHEMA_REL} is not in the instance dir") from e

    @server.resource("orbit://state/tags", mime_type="application/json",
                     description="Current catalog/tags.yaml (desired image tags).")
    def state_tags() -> Any:
        return _instance_payload(state, state.backends.instance.tags)

    @server.resource("orbit://monitors", mime_type="application/json",
                     description="Authored monitors/*.yaml documents.")
    def monitors() -> Any:
        return _instance_payload(state, state.backends.instance.monitors)

    @server.resource("orbit://budget", mime_type="application/json",
                     description="Live platform memory budget (same payload as orbit_platform_budget).")
    def budget() -> Any:
        try:
            return collect_budget(state).model_dump()
        except (BackendUnconfigured, BackendError) as e:
            raise ResourceError(e.what_failed) from e

    @server.resource("orbit://render/{project}/{env}", mime_type="text/yaml",
                     description="Generated Application YAML for one project/env pair.")
    def render(project: str, env: str) -> str:
        try:
            return read_render(state, project, env)
        except BackendUnconfigured as e:
            raise ResourceError(f"{e.variable} is not set") from e
        except RenderConflict as e:
            raise ResourceError(str(e)) from e
        except BackendError as e:
            raise ResourceNotFoundError(render_path(project, env) + " is not in the instance dir") from e

    @server.resource("orbit://ledger/{project}/{env}", mime_type="application/json",
                     description="tags.yaml history for one project/env pair.")
    def ledger(project: str, env: str) -> Any:
        try:
            return collect_ledger(state, project, env, limit=LEDGER_CAP, cursor=None).model_dump()
        except BackendUnconfigured as e:
            raise ResourceError(f"{e.variable} is not set") from e
        except BackendError as e:
            raise ResourceError(e.what_failed) from e
