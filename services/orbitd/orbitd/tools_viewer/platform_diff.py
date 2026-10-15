"""orbit_platform_diff — admin read-only shadow render-and-diff against live."""

from __future__ import annotations

from typing import Annotated, Any

from pydantic import Field

from mcp.server.mcpserver import Context

from ..roles import Listing, Registry, Role
from ..source import read_source
from ..state import AppState
from ._util import ID_RE
from .common import env_names, find_project, guarded, not_found


def register(registry: Registry, state: AppState) -> None:

    @registry.tool(role=Role.admin, read_only=True, idempotent=True)
    def orbit_platform_diff(
        project: Annotated[str | None, Field(
            default=None, pattern=ID_RE.pattern,
            description="Catalog project id. Omit with env to diff every catalog pair.",
        )] = None,
        env: Annotated[str | None, Field(
            default=None, pattern=ID_RE.pattern,
            description="Environment id (prod, dev, ...). Omit with project to diff every env of that project.",
        )] = None,
        ctx: Context = None,  # type: ignore[assignment]
    ) -> Listing:
        """Render the desired objects for a project/environment (or every catalog pair)
        and diff them against live cluster state. Read-only: nothing is applied.
        Each item is a shadow report: `empty` is true when missing, extra and changed
        are all empty; `owned` lists live objects whose owner is a rendered object;
        `changed[].diffs` is `{path, rendered, live}` with JSON-encoded values cut at
        200 characters; `render_sha` is the sha256 of the rendered list; `streak_days`
        is consecutive daily empty reports ending today; `path`/`report_path` is the
        stored JSON under ORBITD_STATE_DIR/shadow/<project>-<env>/<UTC date>.json and
        `date` is that UTC date. Phase 5 waits on seven consecutive empty days.
        The listing's `source` is {commit, age_s, lag_note} for the checkout, once per call.
        Example: orbit_platform_diff(project="demo", env="prod")."""
        return guarded(lambda: _run(state, project, env))


def _run(state: AppState, project: str | None, env: str | None) -> Listing | Any:
    from ..shadow import diff

    pairs = _pairs(state, project, env)
    if not isinstance(pairs, list):
        return pairs
    source = read_source(state)
    reports = [diff(state, pid, name).model_dump(mode="json") for pid, name in pairs]
    return Listing.of(reports, source=source)


def _pairs(state: AppState, project: str | None, env: str | None) -> list[tuple[str, str]] | Any:
    from ..shadow import catalog_pairs

    if project:
        entry = find_project(state, project)
        if entry is None:
            return not_found(f"project {project!r} is not in the catalog", project=project)
        if env and env not in env_names(entry):
            return not_found(
                f"env {env!r} is not in project {project!r}",
                project=project, env=env,
            )
        if env:
            return [(project, env)]
        # an env whose name is not a catalog id is skipped, not diffed: shadow.diff
        # refuses it, and one such env must not fail the project's other envs
        from ..shadow import pair_ok
        return [(project, name) for name in env_names(entry) if pair_ok(project, name)]
    all_pairs = catalog_pairs(state)
    if env:
        selected = [(pid, name) for pid, name in all_pairs if name == env]
        if not selected:
            return not_found(f"env {env!r} is not in the catalog", env=env)
        return selected
    return all_pairs
