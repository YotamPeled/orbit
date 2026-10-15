"""orbit_traces_find — Tempo /api/search."""

from __future__ import annotations

from typing import Annotated, Any

from pydantic import BaseModel, Field

from ..roles import Listing, Registry, Role
from ..state import AppState
import re

from ..roles import failure
from .common import project_entries, relayed
from .util import Duration, TRACES_FIND_CAP, duration_seconds, now, run

# a TraceQL attribute name: dotted identifiers, never quotes, spaces or operators
_ATTR_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_./:-]{0,127}")
# The paths the kubelet and Prometheus call every few seconds. Every one is traced, so a
# search with no tag came back as twenty `GET /readyz` (QA of v1.0.0, F11). Beside these,
# every probePath, readinessPath and metrics path the catalog declares.
PROBE_PATHS = frozenset({"/healthz", "/readyz", "/livez", "/health", "/ready", "/live", "/metrics", "/ping"})
PROBE_FETCH_FACTOR, PROBE_FETCH_MAX = 6, 120


class TraceHit(BaseModel):
    trace_id: str
    root_service: str
    root_name: str
    duration_ms: int
    start: str


def register(registry: Registry, state: AppState) -> None:

    @registry.tool(role=Role.viewer, read_only=True, idempotent=True)
    def orbit_traces_find(
        service: Annotated[str | None, Field(description="resource.service.name to search for.")] = None,
        tags: Annotated[dict[str, str] | None, Field(description="Additional search tags, e.g. {\"http.status_code\": \"500\"}.")] = None,
        since: Duration = "1h",
        limit: Annotated[int, Field(ge=1, le=TRACES_FIND_CAP, description="Max traces to return; the hard cap is 20.")] = TRACES_FIND_CAP,
        probes: Annotated[bool, Field(
            description="Include the traces of health checks and metrics scrapes (GET /healthz, /readyz, "
                        "/metrics and the like). Left out by default: there is one every few seconds.")] = False,
    ) -> Listing:
        """Search Tempo for traces matching a service and optional tags.

        Looks back `since` (default 1h) via GET /api/search. Each hit's trace_id is
        what orbit_traces_get accepts. Traces whose root is a health check or a metrics
        scrape (the kubelet's and Prometheus's calls: /healthz, /readyz, /metrics, and
        every probe path the catalog declares) are left out unless `probes` is true;
        more traces are asked of Tempo to make up for them. Example:
        orbit_traces_find(service="web", tags={"http.method": "GET"}, since="1h",
        limit=20) returns
        {items: [{trace_id, root_service, root_name, duration_ms, start}, ...]}."""

        def _go() -> Listing:
            state.backends.tempo.require()
            bad = [k for k in (tags or {}) if not _ATTR_RE.fullmatch(str(k))]
            if bad or len(tags or {}) > 32:
                return failure("invalid_argument",
                               "a tag key must be a dotted attribute name (letters, digits, _ . / : -), "
                               "at most 32 tags", retryable=False, tag_keys=len(tags or {}))
            end_s = int(now())
            start_s = int(end_s - duration_seconds(since))
            want = min(limit, TRACES_FIND_CAP)
            asked = want if probes else min(want * PROBE_FETCH_FACTOR, PROBE_FETCH_MAX)
            params: dict[str, Any] = {"limit": asked, "start": start_s, "end": end_s}
            traceql = _traceql(service, tags)
            if traceql:
                params["q"] = traceql
            data = state.backends.tempo.get("/api/search", params=params)
            found = _hits(data)
            hits = found if probes else [h for h in found if not _is_probe(h.root_name, _probe_paths(state))]
            truncated = len(hits) > want
            page = hits[:want]
            if not truncated:
                # Tempo gave as many as were asked for: there may be more behind them
                truncated = len(found) >= asked
            return Listing(items=[h.model_dump() for h in page], total=len(hits),
                           returned=len(page), truncated=truncated, next_cursor=None)

        return run(_go)  # type: ignore[return-value]


def _probe_paths(state: AppState) -> frozenset[str]:
    paths = set(PROBE_PATHS)
    try:
        for entry in project_entries(state):
            for comp in entry.get("components") or []:
                if not isinstance(comp, dict):
                    continue
                metrics = comp.get("metrics") if isinstance(comp.get("metrics"), dict) else {}
                for value in (comp.get("probePath"), comp.get("readinessPath"), metrics.get("path")):
                    if isinstance(value, str) and value.startswith("/") and len(value) > 1:
                        paths.add(value.rstrip("/") or value)
    except Exception:
        pass
    return frozenset(paths)


def _is_probe(root_name: str, paths: frozenset[str]) -> bool:
    """`GET /readyz`, `HTTP GET /healthz`, `/metrics`: a root span named for one of the
    probe paths, whatever method word the instrumentation puts in front."""
    words = str(root_name or "").split()
    if not words:
        return False
    path = words[-1].split("?", 1)[0]
    return path.startswith("/") and (path.rstrip("/") or path) in paths


def _escape_traceql(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"')


def _attr(key: str) -> str:
    if key.startswith("resource.") or key.startswith("span."):
        return key
    if key in {"service.name", "resource.service.name"}:
        return "resource.service.name"
    return f"span.{key}"


def attr_ok(key: str) -> bool:
    return _ATTR_RE.fullmatch(key) is not None


def _traceql(service: str | None, tags: dict[str, str] | None) -> str | None:
    parts: list[str] = []
    seen: set[str] = set()
    if service:
        parts.append(f'resource.service.name="{_escape_traceql(service)}"')
        seen.add("resource.service.name")
    for key, value in (tags or {}).items():
        attr = _attr(str(key))
        if attr in seen:
            continue
        seen.add(attr)
        parts.append(f'{attr}="{_escape_traceql(str(value))}"')
    if not parts:
        return None
    return "{" + " && ".join(parts) + "}"


def _hits(data: object) -> list[TraceHit]:
    payload = data if isinstance(data, dict) else {}
    traces = payload.get("traces") or []
    out: list[TraceHit] = []
    for row in traces:
        if not isinstance(row, dict):
            continue
        trace_id = str(row.get("traceID") or row.get("trace_id") or row.get("traceId") or "")
        if not trace_id:
            continue
        start = row.get("startTimeUnixNano") or row.get("start") or ""
        duration = row.get("durationMs") or row.get("duration_ms") or 0
        try:
            duration_ms = int(duration)
        except (TypeError, ValueError):
            duration_ms = 0
        out.append(TraceHit(
            trace_id=trace_id,
            root_service=relayed(row.get("rootServiceName") or row.get("root_service") or "",
                                 limit=256),
            root_name=relayed(row.get("rootTraceName") or row.get("root_name") or "", limit=256),
            duration_ms=duration_ms,
            start=str(start),
        ))
    return out
