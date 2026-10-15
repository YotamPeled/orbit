"""orbit_metrics_query — Prometheus instant or range query, capped at 11 000 points."""

from __future__ import annotations

from typing import Annotated, Any

from pydantic import BaseModel, Field

from ..roles import Listing, Registry, Role
from ..state import AppState
from .common import relayed
from .util import EPOCH_OR_RFC3339, METRICS_CAP, PROMQL_PATTERN, RANGE_MAX_S, duration_seconds, now, run
from ..roles import failure


class MetricSeries(BaseModel):
    metric: dict[str, str]
    values: list[list[Any]]


def register(registry: Registry, state: AppState) -> None:

    @registry.tool(role=Role.viewer, read_only=True, idempotent=True)
    def orbit_metrics_query(
        query: Annotated[str, Field(min_length=1, max_length=4096, pattern=PROMQL_PATTERN,
                                    description="PromQL expression, e.g. up; one line, at most 4096 characters.")],
        start: Annotated[str | None, Field(pattern=EPOCH_OR_RFC3339, description="Range start (unix seconds or RFC3339). Omit with end for an instant query.")] = None,
        end: Annotated[str | None, Field(pattern=EPOCH_OR_RFC3339, description="Range end (unix seconds or RFC3339); the range runs forward and spans at most 30 days.")] = None,
        step: Annotated[str | None, Field(pattern=r"^[0-9]{1,6}(ms|s|m|h|d|w)$", description="Range step, e.g. 15s. Ignored for an instant query.")] = None,
    ) -> Listing:
        """Evaluate a PromQL expression against Prometheus.

        With neither start nor end this is an instant query; with either, a range
        query. Points across every series are capped at 11 000 and `truncated` is set
        when the cap cuts the result. A range whose `step` would give more than 11 000
        points for one series is `invalid_argument` before Prometheus is asked, naming a
        step that fits (a day at the default 15s is 5 761 points; a week needs 1m). Example: orbit_metrics_query(query="up",
        start="1710000000", end="1710003600", step="15s") returns
        {items: [{metric, values: [[ts, v], ...]}, ...], truncated}."""

        def _go() -> Listing:
            state.backends.prom.require()
            ranged = start is not None or end is not None
            if ranged:
                try:
                    start_s = _epoch(start) if start is not None else int(now()) - 3600
                    end_s = _epoch(end) if end is not None else int(now())
                except ValueError:
                    return failure("invalid_argument", "start and end are unix seconds or RFC 3339",
                                   retryable=False)
                if end_s < start_s or end_s - start_s > RANGE_MAX_S:
                    return failure("invalid_argument",
                                   f"the range must run forward and span at most {RANGE_MAX_S // 86400} days",
                                   retryable=False, start=str(start), end=str(end))
                # Prometheus refuses a range of more than 11 000 points per series, and
                # its refusal reached the caller as "check the query syntax" for a query
                # that was fine (QA of v1.0.0, F19). The arithmetic is done here, and the
                # refusal names the step that fits.
                step_s = duration_seconds(step or "15s")
                points = int((end_s - start_s) // step_s) + 1 if step_s > 0 else METRICS_CAP + 1
                if points > METRICS_CAP:
                    fits = _step_that_fits(end_s - start_s)
                    return failure(
                        "invalid_argument",
                        f"this range at step {step or '15s'} is {points} points per series; Prometheus "
                        f"answers at most {METRICS_CAP}. The query itself may be fine",
                        next_action=f"pass a larger step (step=\"{fits}\" fits this range) or a shorter range",
                        retryable=False, points=points, cap=METRICS_CAP, step=step or "15s", step_that_fits=fits)
                params: dict[str, Any] = {
                    "query": query,
                    "start": start or str(start_s),
                    "end": end or str(end_s),
                    "step": step or "15s",
                }
                data = state.backends.prom.get("/api/v1/query_range", params=params)
            else:
                data = state.backends.prom.get("/api/v1/query", params={"query": query})
            series, truncated = _cap(_series(data), METRICS_CAP)
            return Listing(items=[s.model_dump() for s in series], total=len(series),
                           returned=len(series), truncated=truncated, next_cursor=None)

        return run(_go)  # type: ignore[return-value]


def _step_that_fits(span_s: float) -> str:
    """The smallest of the usual steps at which `span_s` is within the cap."""
    for text in ("15s", "30s", "1m", "2m", "5m", "10m", "15m", "30m", "1h", "2h", "6h", "1d"):
        if span_s // duration_seconds(text) + 1 <= METRICS_CAP:
            return text
    return "1d"


def _epoch(value: str) -> float:
    """Unix seconds from a value already of epoch or RFC 3339 shape."""
    try:
        return float(value)
    except ValueError:
        from datetime import datetime
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()


def _series(data: object) -> list[MetricSeries]:
    payload = data if isinstance(data, dict) else {}
    data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
    result = data.get("result") or []
    out: list[MetricSeries] = []
    if data.get("resultType") in ("scalar", "string"):
        # A scalar answer (`time()`, `scalar(...)`) is one [timestamp, value] pair, not a
        # list of series: one series with no labels, so the clock the operator skill
        # promises is really returned.
        if isinstance(result, (list, tuple)) and len(result) >= 2:
            out.append(MetricSeries(metric={}, values=[[result[0], str(result[1])]]))
        return out
    for entry in result:
        if not isinstance(entry, dict):
            continue
        metric = {relayed(k, limit=256): relayed(v, limit=1024)
                  for k, v in (entry.get("metric") or {}).items()}
        values: list[list[Any]] = []
        if "values" in entry:
            for pair in entry.get("values") or []:
                if isinstance(pair, (list, tuple)) and len(pair) >= 2:
                    values.append([pair[0], str(pair[1])])
        elif "value" in entry:
            pair = entry.get("value")
            if isinstance(pair, (list, tuple)) and len(pair) >= 2:
                values.append([pair[0], str(pair[1])])
        out.append(MetricSeries(metric=metric, values=values))
    return out


def _cap(series: list[MetricSeries], cap: int) -> tuple[list[MetricSeries], bool]:
    out: list[MetricSeries] = []
    used = 0
    truncated = False
    for item in series:
        remaining = cap - used
        if remaining <= 0:
            truncated = True
            break
        values = item.values
        if len(values) > remaining:
            values = values[:remaining]
            truncated = True
        out.append(MetricSeries(metric=item.metric, values=values))
        used += len(values)
    return out, truncated
