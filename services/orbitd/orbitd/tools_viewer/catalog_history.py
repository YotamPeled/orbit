"""orbit_catalog_history — the changes made to one project's catalog entry.

The ledger (orbit_ledger_get, orbit_deploy_history) is the history of image tags. A
change of configuration is not in it: a setting that made a scheduled job delete its
data was found on the test box only by reading the application's own log, where the
restore skill says to "find the destructive deploy in the ledger" (QA of v1.0.0, F31).
This is the other history: each commit of catalog/projects.yaml that changed the
project's entry, when, by whom, and which fields."""

from __future__ import annotations

from typing import Annotated, Any

import yaml
from pydantic import Field

from ..backends import BackendError, BackendUnconfigured
from ..roles import Listing, Registry, Role, failure
from ..state import AppState
from .common import find_project, not_found
from .util import Ident

CATALOG_FILE = "catalog/projects.yaml"
HISTORY_CAP = 50
SCAN_CAP = 400           # commits of the file looked at, at most
PATHS_CAP = 30
FIELD_SEP = "\x1f"


def _entry_at(instance: Any, rev: str, project: str) -> dict[str, Any] | None:
    """The project's entry in the catalog as of `rev`; None when it is not there (or the
    file did not exist, or did not parse, at that commit)."""
    text = instance.show(f"{rev}:{CATALOG_FILE}", missing_ok=True)
    if not text:
        return None
    try:
        doc = yaml.safe_load(text) or {}
    except yaml.YAMLError:
        return None
    for entry in (doc.get("projects") if isinstance(doc, dict) else None) or []:
        if isinstance(entry, dict) and entry.get("id") == project:
            return entry
    return None


def collect(state: AppState, project: str, *, limit: int, cursor: str | None) -> Listing:
    from ..tools_operator.catalog import changed
    inst = state.backends.instance
    inst.require()
    log = inst.run_git(["log", f"--max-count={SCAN_CAP}",
                        f"--format=%H{FIELD_SEP}%aI{FIELD_SEP}%s{FIELD_SEP}"
                        "%(trailers:key=Orbit-Actor,valueonly,separator=%x20)"
                        f"{FIELD_SEP}%(trailers:key=Orbit-Action,valueonly,separator=%x20)",
                        "--", CATALOG_FILE])
    commits = [line.split(FIELD_SEP) for line in log.splitlines() if line.strip()]
    start = 0
    if cursor:
        start = int(cursor)
        if start < 0:
            raise ValueError("cursor")
    rows: list[dict[str, Any]] = []
    newer: dict[str, Any] | None = None
    have_newer = False
    # Newest first. Each commit's entry is compared with the one before it in time: the
    # next line of the log, read once and carried over.
    for index, parts in enumerate(commits):
        sha, at, subject, actor, action = (parts + ["", "", "", "", ""])[:5]
        now = newer if have_newer else _entry_at(inst, sha, project)
        older = _entry_at(inst, commits[index + 1][0], project) if index + 1 < len(commits) else (
            _entry_at(inst, f"{sha}^", project))
        newer, have_newer = older, True
        if now == older:
            continue
        if older is None:
            what, paths = "added", []
        elif now is None:
            what, paths = "removed", []
        else:
            what, paths = "changed", changed(older, now)
        row: dict[str, Any] = {"commit": sha, "at": at, "subject": subject.strip()[:200], "what": what,
                               "changed": paths[:PATHS_CAP]}
        if len(paths) > PATHS_CAP:
            row["changed_more"] = len(paths) - PATHS_CAP
        if actor.strip():
            row["actor"] = actor.strip()[:100]
        if action.strip():
            row["action"] = action.strip()[:60]
        rows.append(row)
    window = rows[start:start + limit]
    next_cursor = str(start + limit) if start + limit < len(rows) else None
    return Listing.of(window, total=len(rows), next_cursor=next_cursor)


def register(registry: Registry, state: AppState) -> None:

    @registry.tool(role=Role.viewer, read_only=True, idempotent=True)
    def orbit_catalog_history(
        project: Annotated[Ident, Field(description="Project id from orbit_project_list.")],
        limit: Annotated[int, Field(ge=1, le=HISTORY_CAP, description="Page size; capped at 50.")] = 20,
        cursor: Annotated[str | None, Field(description="Opaque page cursor from a previous next_cursor.")] = None,
    ) -> Listing:
        """The changes made to one project's catalog entry, newest first: every commit
        of catalog/projects.yaml in which the entry differs from the commit before.
        Each item is {commit, at, subject, what, changed, actor, action}: `what` is
        `changed`, `added` or `removed`; `changed` the paths that differ, as
        orbit_catalog_set names them (`components[web].memory`,
        `environments.dev.env.KEEP_DAYS (added)`), never a value; `actor` and `action`
        the commit's `Orbit-Actor` and `Orbit-Action` trailers when it was made through
        orbitd (a change merged by pull request carries none). This is where a change
        of configuration is found: orbit_ledger_get and orbit_deploy_history list image
        tags only. `at` is when the commit was made; orbitd applies it a few minutes
        later (orbit_deploy_wait). The last 400 commits of the file are read. Fails
        `not_found` for a project that is not in the catalog now and never appears in
        those commits.
        Example: orbit_catalog_history(project="demo", limit=5) -> {items: [{commit:
        "abc...", at: "2026-10-11T22:58:40+00:00", what: "changed", changed:
        ["components[janitor].env.KEEP_DAYS"], actor: "ada", action: "catalog-set"}]}."""
        try:
            listing = collect(state, project, limit=min(limit, HISTORY_CAP), cursor=cursor)
            if not listing.items and not listing.total and find_project(state, project) is None:
                return not_found(f"project {project!r} is not in the catalog and no commit of it is in "
                                 f"the catalog's recent history", project=project)  # type: ignore[return-value]
            return listing
        except BackendUnconfigured as e:
            return e.as_failure()  # type: ignore[return-value]
        except BackendError as e:
            return e.as_failure()  # type: ignore[return-value]
        except ValueError:
            return failure("invalid_cursor", "cursor must be a non-negative integer from a previous next_cursor",
                           next_action="omit cursor or pass the next_cursor from the last page",
                           cursor=cursor)  # type: ignore[return-value]
