"""orbit_ledger_get — tags.yaml history for one project/env pair."""

from __future__ import annotations

from typing import Annotated, Any

import yaml
from pydantic import Field

from mcp.server.mcpserver import Context

from ..backends import BackendError, BackendUnconfigured
from ..roles import Listing, Registry, Role, failure
from ..state import AppState
from ._util import ID_RE

LEDGER_CAP = 100
HISTORY_SCAN = 500  # commits of the tags file read for one answer
TAGS_FILE = "catalog/tags.yaml"


def _prefix(project: str, env: str) -> str:
    return f"{project}/{env}/"


def _tags_at(instance, commit: str) -> dict[str, Any]:
    text = instance.show(f"{commit}:{TAGS_FILE}", missing_ok=True)
    if not text:
        return {}
    try:
        data = yaml.safe_load(text) or {}
    except yaml.YAMLError:
        return {}  # a tags file that commit could not parse carried no tags
    tags = data.get("tags") if isinstance(data, dict) else None
    return tags if isinstance(tags, dict) else {}


def _pair_tags(tags: dict[str, Any], project: str, env: str) -> dict[str, Any]:
    prefix = _prefix(project, env)
    return {k: v for k, v in tags.items() if str(k).startswith(prefix)}


def changes(instance, value_at, scan: int = HISTORY_SCAN) -> list[tuple[dict[str, str], Any]]:
    """The commits of the tags file that changed what `value_at(commit)` answers, newest
    first, each with the value it left. A commit that left it as it was is another
    project's or environment's deploy: on an instance with two projects it filled the
    first's history (the test box, 2026-10-12: the newest thirty entries of checkers-lab
    dev were guestbook's ships). `value_at` answers None where the commit carries nothing
    for the caller. The oldest commit read is kept only when the log ends there: what
    came before it is not known otherwise."""
    history = instance.tags_history(limit=scan)
    values = [value_at(entry.get("commit") or "") for entry in history]
    out: list[tuple[dict[str, str], Any]] = []
    for i, (entry, value) in enumerate(zip(history, values)):
        if value is None:
            continue
        if i + 1 < len(history):
            if value != values[i + 1]:
                out.append((entry, value))
        elif len(history) < scan:
            out.append((entry, value))
    return out


def collect_ledger(state: AppState, project: str, env: str, *,
                   limit: int, cursor: str | None) -> Listing:
    inst = state.backends.instance
    inst.require()
    rows = [
        {"commit": entry.get("commit") or "", "at": entry.get("authored") or "",
         "subject": entry.get("subject") or "", "tags": pair}
        for entry, pair in changes(inst, lambda sha: _pair_tags(_tags_at(inst, sha), project, env) or None)
    ]
    start = 0
    if cursor:
        try:
            start = int(cursor)
        except ValueError:
            raise ValueError("cursor")
        if start < 0:
            raise ValueError("cursor")
    window = rows[start:start + limit]
    next_cursor = str(start + limit) if start + limit < len(rows) else None
    return Listing.of(window, total=len(rows), next_cursor=next_cursor)


def register(registry: Registry, state: AppState) -> None:

    @registry.tool(role=Role.viewer, read_only=True, idempotent=True)
    def orbit_ledger_get(
        project: Annotated[str, Field(pattern=ID_RE.pattern,
                                      description="Catalog project id; tags keys are project/env/component.")],
        env: Annotated[str, Field(pattern=ID_RE.pattern,
                                  description="Environment id (prod, dev, ...).")],
        limit: Annotated[int, Field(ge=1, le=LEDGER_CAP,
                                    description="Page size; capped at 100.")] = 20,
        cursor: Annotated[str | None, Field(description="Opaque page cursor from a previous next_cursor.")] = None,
        ctx: Context = None,  # type: ignore[assignment]
    ) -> Listing:
        """Tags-file history for one project/environment: each git commit of
        `catalog/tags.yaml` that changed a tag under `project/env/...`, newest
        first, with the pair's tags as that commit left them. `commit` is what orbit_deploy_history also returns. Example:
        orbit_ledger_get(project="demo", env="prod", limit=20) -> {items:
        [{commit: "abc...", at: "2026-09-01T00:00:00+00:00", subject: "...",
        tags: {"demo/prod/web": "main-deadbee"}}], next_cursor: null}."""
        try:
            return collect_ledger(state, project, env, limit=min(limit, LEDGER_CAP), cursor=cursor)
        except BackendUnconfigured as e:
            return e.as_failure()  # type: ignore[return-value]
        except BackendError as e:
            return e.as_failure()  # type: ignore[return-value]
        except ValueError:
            return failure(
                "invalid_cursor",
                "cursor must be a non-negative integer from a previous next_cursor",
                next_action="omit cursor or pass the next_cursor from the last page",
                cursor=cursor,
            )
