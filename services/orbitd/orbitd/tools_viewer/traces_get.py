"""orbit_traces_get — Tempo /api/traces/{id}, summarised and capped at 500 spans."""

from __future__ import annotations

from typing import Annotated, Any

from pydantic import BaseModel, Field

from ..backends import BackendError
from ..roles import Listing, Registry, Role, failure
from ..state import AppState
from .util import TRACES_SPAN_CAP, run


class Span(BaseModel):
    span_id: str
    parent: str | None = None
    service: str
    name: str
    duration_ms: int
    status: str


def register(registry: Registry, state: AppState) -> None:

    @registry.tool(role=Role.viewer, read_only=True, idempotent=True)
    def orbit_traces_get(
        trace_id: Annotated[str, Field(min_length=8, max_length=32, pattern=r"^[0-9a-fA-F]+$",
                                       description="Trace id as returned by orbit_traces_find.")],
    ) -> Listing:
        """Fetch one trace from Tempo and summarise its spans.

        GET /api/traces/{trace_id}. Each span is {span_id, parent, service, name,
        duration_ms, status}. The list is capped at 500 spans and truncated when cut.
        Example: orbit_traces_get(trace_id="4bf92f3577b34da6a3ce929d0e0e4736") returns
        {items: [{span_id, parent, service, name, duration_ms, status}, ...], truncated}."""

        def _go() -> Listing:
            state.backends.tempo.require()
            try:
                data = state.backends.tempo.get(f"/api/traces/{trace_id}")
            except BackendError as e:
                if e.status == 404:
                    return failure(
                        "not_found",
                        f"trace {trace_id} was not found",
                        next_action="call orbit_traces_find to list traces",
                        trace_id=trace_id,
                    )  # type: ignore[return-value]
                raise
            spans = _spans(data)
            if not spans:
                return failure(
                    "not_found",
                    f"trace {trace_id} was not found",
                    next_action="call orbit_traces_find to list traces",
                    trace_id=trace_id,
                )  # type: ignore[return-value]
            truncated = len(spans) > TRACES_SPAN_CAP
            page = spans[:TRACES_SPAN_CAP]
            return Listing(items=[s.model_dump() for s in page], total=len(spans),
                           returned=len(page), truncated=truncated, next_cursor=None)

        return run(_go)  # type: ignore[return-value]


def _spans(data: object) -> list[Span]:
    payload = data if isinstance(data, dict) else {}
    batches = payload.get("batches") or payload.get("resourceSpans") or []
    if not batches and isinstance(payload.get("data"), list):
        return _jaeger(payload["data"])
    out: list[Span] = []
    for batch in batches:
        if not isinstance(batch, dict):
            continue
        service = _service(batch.get("resource") or {})
        span_groups = (batch.get("scopeSpans") or batch.get("instrumentationLibrarySpans") or [])
        if not span_groups and "spans" in batch:
            span_groups = [batch]
        for group in span_groups:
            if not isinstance(group, dict):
                continue
            for raw in group.get("spans") or []:
                if not isinstance(raw, dict):
                    continue
                out.append(_span(raw, service))
    return out


def _jaeger(traces: list[Any]) -> list[Span]:
    out: list[Span] = []
    for trace in traces:
        if not isinstance(trace, dict):
            continue
        processes = trace.get("processes") or {}
        for raw in trace.get("spans") or []:
            if not isinstance(raw, dict):
                continue
            proc = processes.get(raw.get("processID") or "") or {}
            service = str(proc.get("serviceName") or "")
            duration = raw.get("duration") or 0
            try:
                # Jaeger duration is microseconds.
                duration_ms = int(duration) // 1000
            except (TypeError, ValueError):
                duration_ms = 0
            refs = raw.get("references") or []
            parent = None
            for ref in refs:
                if isinstance(ref, dict) and str(ref.get("refType") or "").upper() == "CHILD_OF":
                    parent = str(ref.get("spanID") or "") or None
                    break
            out.append(Span(
                span_id=str(raw.get("spanID") or raw.get("span_id") or ""),
                parent=parent,
                service=service,
                name=str(raw.get("operationName") or raw.get("name") or ""),
                duration_ms=duration_ms,
                status=_status(raw.get("status") or raw.get("tags")),
            ))
    return out


def _span(raw: dict[str, Any], service: str) -> Span:
    span_id = str(raw.get("spanId") or raw.get("spanID") or raw.get("span_id") or "")
    parent = raw.get("parentSpanId") or raw.get("parentSpanID") or raw.get("parent") or None
    parent_s = str(parent) if parent else None
    start = _nano(raw.get("startTimeUnixNano") or raw.get("startTime"))
    end = _nano(raw.get("endTimeUnixNano") or raw.get("endTime"))
    duration_ns = raw.get("durationNanos") or raw.get("duration") or 0
    try:
        duration_ns_i = int(duration_ns)
    except (TypeError, ValueError):
        duration_ns_i = 0
    if duration_ns_i <= 0 and end > start:
        duration_ns_i = end - start
    return Span(
        span_id=span_id,
        parent=parent_s or None,
        service=service,
        name=str(raw.get("name") or raw.get("operationName") or ""),
        duration_ms=duration_ns_i // 1_000_000,
        status=_status(raw.get("status")),
    )


def _service(resource: dict[str, Any]) -> str:
    if resource.get("serviceName"):
        return str(resource["serviceName"])
    for attr in resource.get("attributes") or []:
        if isinstance(attr, dict) and attr.get("key") == "service.name":
            value = attr.get("value") or {}
            if isinstance(value, dict):
                return str(value.get("stringValue") or value.get("string_value") or "")
            return str(value)
    return ""


def _nano(value: object) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _status(value: object) -> str:
    if isinstance(value, dict):
        code = value.get("code")
        if code in (2, "2", "STATUS_CODE_ERROR", "ERROR", "error"):
            return "error"
        if code in (1, "1", "STATUS_CODE_OK", "OK", "ok"):
            return "ok"
        if code in (0, "0", "STATUS_CODE_UNSET", "UNSET", None):
            return "unset"
        return str(code).lower()
    if isinstance(value, list):
        for tag in value:
            if isinstance(tag, dict) and tag.get("key") in ("error", "otel.status_code"):
                val = str(tag.get("value") or "").lower()
                if val in ("true", "error"):
                    return "error"
    return "unset"
