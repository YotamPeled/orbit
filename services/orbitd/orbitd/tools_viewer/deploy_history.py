"""orbit_deploy_history — tag-file commits for a project/env(/component)."""

from __future__ import annotations

from typing import Annotated, Any

import yaml
from pydantic import Field

from orbitd.roles import Listing, Registry, Role
from orbitd.state import AppState

from .ledger_get import changes
from .util import Ident
from .common import (
    require_env,
    HISTORY_CAP, HISTORY_DEFAULT, find_project, guarded, not_found, paginate, tag_key,
)


def register(registry: Registry, state: AppState) -> None:

    @registry.tool(role=Role.viewer, read_only=True, idempotent=True)
    def orbit_deploy_history(
        project: Annotated[Ident, Field(description="Project id from orbit_project_list.")],
        env: Annotated[Ident, Field(description="Environment name, e.g. prod or dev.")],
        component: Annotated[Ident | None, Field(
            default=None,
            description="Component name. Omit to include every component in the env.",
        )] = None,
        limit: Annotated[int, Field(
            default=HISTORY_DEFAULT, ge=1, le=HISTORY_CAP,
            description="Page size. Capped at 100.",
        )] = HISTORY_DEFAULT,
        cursor: Annotated[str | None, Field(
            default=None,
            description="Pagination cursor from a previous next_cursor.",
        )] = None,
    ) -> Listing:
        """List tag changes for a project and environment from git history of
        catalog/tags.yaml. Each item is {commit, at, subject, tag?}; `commit` is what
        another tool would accept as a ship target. Pass the returned next_cursor to
        page; the list is capped at 100 items.
        Example: orbit_deploy_history(project="demo", env="prod", component="web", limit=20)."""
        return guarded(lambda: _history(state, project, env, component, limit, cursor))


def _history(state: AppState, project: str, env: str, component: str | None,
             limit: int, cursor: str | None) -> Listing:
    entry = find_project(state, project)
    if entry is None:
        return not_found(f"project {project!r} is not in the catalog", project=project)
    if (missing := require_env(entry, project, env)) is not None:
        return missing
    instance = state.backends.instance

    def value_at(commit: str) -> Any:
        value = _tag_at(instance, commit, project, env, component)
        return None if value is _ABSENT else value

    items = [
        {"commit": row.get("commit") or "", "at": row.get("authored"), "subject": row.get("subject"), "tag": value}
        for row, value in changes(instance, value_at)
    ]
    return paginate(items, cursor=cursor, limit=limit, cap=HISTORY_CAP)


_ABSENT = object()


def _tag_at(instance: Any, commit: str, project: str, env: str, component: str | None) -> Any:
    if not commit:
        return _ABSENT
    text = instance.show(f"{commit}:catalog/tags.yaml", missing_ok=True)
    if not text:
        return _ABSENT
    try:
        doc = yaml.safe_load(text) or {}
    except yaml.YAMLError:
        return _ABSENT
    tree = doc.get("tags", doc) if isinstance(doc, dict) else {}
    if not isinstance(tree, dict):
        return _ABSENT
    prefix = f"{project}/{env}/"
    matching = {k: v for k, v in tree.items() if str(k).startswith(prefix)}
    if not matching:
        return _ABSENT
    if component is None:
        return matching
    key = tag_key(project, env, component)
    if key not in tree:
        return _ABSENT
    value = tree.get(key)
    return None if value is None else str(value)
