"""orbit_logs_query — Loki query_range, capped at 200 lines; the node's own pod logs when
the logs feature is off."""

from __future__ import annotations

from typing import Annotated

from pydantic import BaseModel, Field

from .. import node_logs
from ..features import enable_hint
from ..roles import Listing, Registry, Role, failure
from ..state import AppState
from .common import as_dict, find_project, guarded, list_pods, not_found, pick, relayed, require_env, selector
from .util import CURSOR_PATTERN, Duration, Ident, LOGS_CAP, QUERY_PATTERN, duration_seconds, now, ts_int


class LogLine(BaseModel):
    ts: str
    pod: str
    line: str
    container: str | None = None
    previous: bool | None = Field(default=None, description="true for a line from the container's run before its last restart")


class LogListing(Listing):
    """A Listing of log lines, saying where they came from when that is not Loki."""

    note: str | None = None


NODE_NOTE = ("the logs feature is off: these lines come from the node through the Kubernetes API "
             "(each container's current run, and the run before its last restart), not from Loki; "
             "a deleted pod's lines are gone and only line filters are answered")


def register(registry: Registry, state: AppState) -> None:

    @registry.tool(role=Role.viewer, read_only=True, idempotent=True)
    def orbit_logs_query(
        project: Annotated[Ident, Field(description="Project id; namespace is project-env.")],
        env: Annotated[Ident, Field(description="Environment id; namespace is project-env.")],
        component: Annotated[Ident | None, Field(description="Component id; omitted means every pod in the namespace.")] = None,
        query: Annotated[str | None, Field(pattern=QUERY_PATTERN, max_length=2048,
                                           description="LogQL filter appended after the selector, e.g. |= \"error\"; one line, at most 2048 characters.")] = None,
        since: Duration = "15m",
        limit: Annotated[int, Field(ge=1, le=LOGS_CAP, description="Max lines to return; the hard cap is 200.")] = LOGS_CAP,
        cursor: Annotated[str | None, Field(pattern=CURSOR_PATTERN, max_length=32,
                                            description="Last timestamp from the previous page (unix nanos).")] = None,
    ) -> LogListing:
        """Query the log lines of a project environment.

        Builds a LogQL selector from project and env (and component, when given) and
        appends the optional filter. Pages forward from `since`, or from `cursor` when
        paging, and caps the result at 200 lines. With the logs feature off there is no
        Loki: the lines come from the node (the pods that still exist, each container's
        current run and the run before its last restart), `note` says so, and `query`
        may only be a chain of line filters (|= != |~ !~). Example: orbit_logs_query(project="demo",
        env="prod", component="web", query="|= \\"error\\"", since="15m") returns
        {items: [{ts, pod, line}, ...], truncated, next_cursor}."""

        def _go() -> Listing:
            entry = find_project(state, project)
            if entry is None:
                return not_found(f"project {project!r} is not in the catalog", project=project)
            if (missing := require_env(entry, project, env)) is not None:
                return missing
            namespace = f"{project}-{env}"
            if state.features.snapshot().on.get("logs", True) is False:
                return _from_the_node(state, project, namespace, component, query, since, limit, cursor)
            state.backends.loki.require()
            if component:
                pods = list_pods(state.backends.kube, namespace, selector(project, component))
                names = []
                for pod in pods:
                    name = pick(as_dict(as_dict(pod).get("metadata") or {}), "name")
                    if name:
                        names.append(str(name))
                if not names:
                    return LogListing.of([])
                matcher = "|".join(names)
                log_selector = '{namespace="%s", pod=~"%s"}' % (namespace, matcher)
            else:
                log_selector = '{namespace="%s"}' % namespace
            logql = f"{log_selector} {query}" if query else log_selector
            end_s = now()
            start_s = end_s - duration_seconds(since)
            start_ns = str(int(start_s * 1_000_000_000))
            skip = 0
            cursor_ts = None
            if cursor:
                cursor_ts, skip = _parse_cursor(cursor)
                start_ns = cursor_ts
            want = min(limit, LOGS_CAP)
            request_limit = want + skip
            data = state.backends.loki.get("/loki/api/v1/query_range", params={
                "query": logql,
                "start": start_ns,
                "end": str(int(end_s * 1_000_000_000)),
                "limit": request_limit,
                "direction": "forward",
            })
            items = _flatten(data)
            backend_n = len(items)
            if cursor_ts is not None and skip:
                dropped = 0
                kept: list[LogLine] = []
                for row in items:
                    if dropped < skip and row.ts == cursor_ts:
                        dropped += 1
                        continue
                    kept.append(row)
                items = kept
            page = items[:want]
            # Complete only when the backend returned fewer than requested.
            truncated = backend_n >= request_limit
            next_cursor = _next_cursor(page, cursor_ts, skip) if truncated and page else None
            return LogListing.of([row.model_dump(exclude_none=True) for row in page], next_cursor=next_cursor)

        return guarded(_go)  # type: ignore[return-value]


def _from_the_node(state: AppState, project: str, namespace: str, component: str | None,
                   query: str | None, since: str, limit: int, cursor: str | None) -> Listing:
    """The same question answered without Loki: the Kubernetes API's pod logs."""
    try:
        filters = node_logs.line_filters(query)
    except node_logs.UnsupportedQuery:
        return failure(  # type: ignore[return-value]
            "FEATURE_OFF",
            "the logs feature is off, so only a chain of line filters can be answered "
            '(|= "text", != "text", |~ "regex", !~ "regex")',
            next_action="narrow the query to line filters, or " + enable_hint("logs"),
            feature="logs")
    pods = list_pods(state.backends.kube, namespace, selector(project, component) if component else None)
    rows: list[LogLine] = []
    for pod in pods:
        for row in node_logs.read_pod(state.backends.kube, namespace, pod,
                                      since_seconds=int(duration_seconds(since))):
            if node_logs.keeps(row["line"], filters):
                rows.append(LogLine(ts=row["ts"], pod=relayed(row["pod"], limit=253), line=relayed(row["line"]),
                                    container=row.get("container"), previous=row.get("previous")))
    rows.sort(key=lambda row: (ts_int(row.ts), row.pod))
    floor = int((now() - duration_seconds(since)) * 1_000_000_000)
    rows = [row for row in rows if ts_int(row.ts) >= floor]
    skip, cursor_ts = 0, None
    if cursor:
        cursor_ts, skip = _parse_cursor(cursor)
        rows = [row for row in rows if ts_int(row.ts) >= ts_int(cursor_ts)]
        kept, dropped = [], 0
        for row in rows:
            if dropped < skip and row.ts == cursor_ts:
                dropped += 1
                continue
            kept.append(row)
        rows = kept
    want = min(limit, LOGS_CAP)
    page = rows[:want]
    next_cursor = _next_cursor(page, cursor_ts, skip) if len(rows) > want and page else None
    listing = LogListing.of([row.model_dump(exclude_none=True) for row in page], next_cursor=next_cursor)
    listing.note = NODE_NOTE
    return listing


def _parse_cursor(cursor: str) -> tuple[str, int]:
    """Cursor is `{ts}:{n}` (skip n lines at ts) or `{ts}` (`>` semantics)."""
    if ":" in cursor:
        ts, _, rest = cursor.rpartition(":")
        if ts and rest.isdigit():
            return ts, int(rest)
    if cursor.isdigit():
        return str(int(cursor) + 1), 0
    return cursor, 0


def _next_cursor(page: list[LogLine], cursor_ts: str | None = None, skip: int = 0) -> str:
    last_ts = page[-1].ts
    n = 0
    for row in reversed(page):
        if row.ts != last_ts:
            break
        n += 1
    if n == len(page) and cursor_ts == last_ts:
        # the whole page shares the cursor's stamp: carry the lines already skipped
        # forward, or the next page would be this page again
        n += skip
    return f"{last_ts}:{n}"


def _flatten(data: object) -> list[LogLine]:
    payload = data if isinstance(data, dict) else {}
    rows: list[LogLine] = []
    streams = ((payload.get("data") or {}).get("result") or []) if isinstance(payload.get("data"), dict) else []
    for stream in streams:
        labels = (stream.get("stream") or {}) if isinstance(stream, dict) else {}
        pod = relayed(labels.get("pod") or labels.get("pod_name") or "", limit=253)
        for pair in (stream.get("values") or []) if isinstance(stream, dict) else []:
            if not isinstance(pair, (list, tuple)) or len(pair) < 2:
                continue
            rows.append(LogLine(ts=str(pair[0]), pod=pod, line=relayed(pair[1])))
    rows.sort(key=lambda row: (ts_int(row.ts), row.pod))
    return rows
