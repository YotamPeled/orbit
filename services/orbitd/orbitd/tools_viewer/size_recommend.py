"""orbit_size_recommend — sizing advice per component, from what Prometheus recorded.

This module only asks Prometheus and maps the answers; every decision is in
`orbitd.sizing` (pure). It reads the catalog for the component names and writes nothing.
"""

from __future__ import annotations

import math
from typing import Annotated, Any

from pydantic import BaseModel, Field

from .. import sizing
from ..backends import BackendError
from ..roles import Registry, Role
from ..state import AppState
from .common import component_names, find_project, guarded, not_found, ns, require_env
from .util import Ident

WINDOW_DEFAULT = 7
WINDOW_MAX = 14
# The resolution the window is walked at: history, gaps and the CPU rate all use it.
STEP_S = 300


class MemorySuggestion(BaseModel):
    request_mi: int
    limit_mi: int


class CpuSuggestion(BaseModel):
    request_m: int
    limit_m: int | None = Field(default=None, description="null unless a CPU limit is set today")


class MemorySizing(BaseModel):
    request_mi: int | None = None
    limit_mi: int | None = None
    peak_mi: int | None = Field(default=None, description="highest working set in the window, rounded up")
    median_mi: int | None = None
    verdict: str
    reason: str
    suggestion: MemorySuggestion | None = None


class CpuSizing(BaseModel):
    request_m: int | None = None
    limit_m: int | None = None
    p95_m: float | None = None
    mean_m: float | None = None
    peak_m: float | None = Field(default=None, description="highest 5-minute rate in the window")
    verdict: str
    reason: str
    suggestion: CpuSuggestion | None = None


class ComponentSizing(BaseModel):
    component: str
    containers: int = Field(description="containers of this component running now")
    history_days: float = Field(description="days of samples actually found, at most window_days")
    oom_kills: int = Field(description="out-of-memory kills in the window: the restarts that followed an exit that was one")
    restarts: int
    memory: MemorySizing
    cpu: CpuSizing


class SizeRecommend(BaseModel):
    """Listing envelope plus the pair and the window the advice was computed over."""

    project: str
    env: str
    namespace: str
    window_days: int
    items: list[ComponentSizing]
    total: int | None = None
    returned: int = 0
    truncated: bool = False
    next_cursor: str | None = None


KILL_STEP = "1m"       # the resolution out-of-memory kills are counted at (see `oom_kills`)


def queries(namespace: str, window_days: int, *, window: str | None = None) -> dict[str, str]:
    """Every PromQL expression one call sends, by the quantity it answers. Each returns
    one sample per `container` label; cAdvisor keeps a series per container instance and
    a component may run several pods, so each is aggregated to the component.
    `window` replaces `<window_days>d` with another PromQL duration (the tests ask the
    minute-by-minute kill count over hours: promtool's evaluator holds 10 000 samples)."""
    w = window or f"{window_days}d"
    step = f"{STEP_S // 60}m"
    sel = f'namespace="{namespace}",container!="",container!="POD"'
    ksm = f'namespace="{namespace}"'
    mem = f"container_memory_working_set_bytes{{{sel}}}"
    cpu = f"rate(container_cpu_usage_seconds_total{{{sel}}}[{step}])"
    restarts = f"increase(kube_pod_container_status_restarts_total{{{ksm}}}[{w}])"
    return {
        "running": f"count by (container) ({mem})",
        "memory_request": f'max by (container) (kube_pod_container_resource_requests{{{ksm},resource="memory"}})',
        "memory_limit": f'max by (container) (kube_pod_container_resource_limits{{{ksm},resource="memory"}})',
        "cpu_request": f'max by (container) (kube_pod_container_resource_requests{{{ksm},resource="cpu"}})',
        "cpu_limit": f'max by (container) (kube_pod_container_resource_limits{{{ksm},resource="cpu"}})',
        "memory_peak": f"max by (container) (max_over_time({mem}[{w}]))",
        "memory_median": f"max by (container) (quantile_over_time(0.5, {mem}[{w}]))",
        "cpu_p95": f"max by (container) (quantile_over_time(0.95, {cpu}[{w}:{step}]))",
        "cpu_mean": f"max by (container) (avg_over_time({cpu}[{w}:{step}]))",
        "cpu_peak": f"max by (container) (max_over_time({cpu}[{w}:{step}]))",
        "samples": f"count_over_time((max by (container) ({mem}))[{w}:{step}])",
        "history_s": f"time() - min_over_time(timestamp(max by (container) ({mem}))[{w}:{step}])",
        "restarts": f"sum by (container) ({restarts})",
        # the share of the window the component's containers were ready, over every pod it
        # had: what tells a crash loop from a few restarts of builds long since replaced
        "ready_fraction": (
            f"sum by (container) (sum_over_time(kube_pod_container_status_ready{{{ksm}}}[{w}])) / "
            f"sum by (container) (count_over_time(kube_pod_container_status_ready{{{ksm}}}[{w}]))"
        ),
        # A k3s kubelet reports an out-of-memory kill as reason="Error" with exit code 137
        # (template rules.yaml, PlatformContainerRestarted), so both signatures count.
        # Counted restart by restart: minute by minute, the restarts of that minute, when
        # the container's last exit AT THAT MINUTE was a kill. Asked once over the whole
        # window ("restarts in the window of a container whose last exit is a kill"), four
        # clean restarts followed by one kill read as five kills (QA of v1.0.0, F21). The
        # minute is on purpose finer than the 5-minute step the other queries use: on
        # the test box three restarts inside five minutes, the last one a kill, read as
        # three kills at that step (v1.0.1-rc1). kube-state-metrics is scraped every 30 s,
        # so a minute holds one restart and its reason together. The restarts of a
        # minute are the counter less itself a minute earlier, or the counter itself for
        # a pod younger than that (it starts at 0); `increase` would extrapolate a single
        # restart to more than one.
        "oom_kills": (
            f"sum by (container) (sum_over_time((("
            f"(kube_pod_container_status_restarts_total{{{ksm}}} "
            f"- kube_pod_container_status_restarts_total{{{ksm}}} offset {KILL_STEP}) "
            f"or kube_pod_container_status_restarts_total{{{ksm}}}) "
            f"and on (namespace, pod, container) "
            f'(kube_pod_container_status_last_terminated_reason{{{ksm},reason="OOMKilled"}} == 1 '
            f"or kube_pod_container_status_last_terminated_exitcode{{{ksm}}} == 137))[{w}:{KILL_STEP}]))"
        ),
    }


def _by_container(state: AppState, expr: str) -> dict[str, Any]:
    """One instant query: the raw value per `container` label. Values stay as Prometheus
    sent them (strings, "NaN" included); `orbitd.sizing` decides what is usable."""
    payload = state.backends.prom.get("/api/v1/query", params={"query": expr})
    if not isinstance(payload, dict) or payload.get("status") != "success":
        detail = payload.get("error") if isinstance(payload, dict) else None
        raise BackendError(status=None, body=str(detail or "unexpected response to a sizing query"),
                           what_failed="ORBITD_PROMETHEUS_URL did not answer a sizing query",
                           env_var="ORBITD_PROMETHEUS_URL", retryable=False)
    data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
    out: dict[str, Any] = {}
    for entry in data.get("result") or []:
        if not isinstance(entry, dict):
            continue
        name = (entry.get("metric") or {}).get("container")
        pair = entry.get("value")
        if isinstance(name, str) and isinstance(pair, (list, tuple)) and len(pair) >= 2:
            out[name] = pair[1]
    return out


def _whole(value: float | None, *, up: bool = False) -> int | None:
    if value is None:
        return None
    return int(math.ceil(value - 1e-9)) if up else int(round(value))


def _tenth(value: float | None) -> float | None:
    return None if value is None else round(value, 1)


def _component(name: str, got: dict[str, dict[str, Any]], window_days: int) -> ComponentSizing:
    def raw(key: str) -> Any:
        return got[key].get(name)

    history = sizing.number(raw("history_s"))
    if history is not None:
        history = min(history, window_days * 86400.0)
    gaps = sizing.gap_fraction(raw("samples"), history, STEP_S)
    kills, restarts = sizing.count(raw("oom_kills")), sizing.count(raw("restarts"))

    m_request, m_limit = sizing.mi(raw("memory_request")), sizing.mi(raw("memory_limit"))
    m_peak, m_median = sizing.mi(raw("memory_peak")), sizing.mi(raw("memory_median"))
    memory = sizing.recommend_memory(
        request_mi=m_request, limit_mi=m_limit, peak_mi=m_peak, median_mi=m_median,
        history_s=history, gap_fraction=gaps, oom_kills=kills, restarts=restarts,
        ready_fraction=raw("ready_fraction"))

    c_request, c_limit = sizing.millicores(raw("cpu_request")), sizing.millicores(raw("cpu_limit"))
    c_p95, c_mean = sizing.millicores(raw("cpu_p95")), sizing.millicores(raw("cpu_mean"))
    c_peak = sizing.millicores(raw("cpu_peak"))
    cpu = sizing.recommend_cpu(
        request_m=c_request, limit_m=c_limit, p95_m=c_p95, peak_m=c_peak,
        history_s=history, gap_fraction=gaps, restarts=restarts,
        ready_fraction=raw("ready_fraction"))

    return ComponentSizing(
        component=name,
        containers=sizing.count(raw("running")),
        history_days=round((history or 0.0) / 86400.0, 1),
        oom_kills=kills, restarts=restarts,
        memory=MemorySizing(
            request_mi=_whole(m_request), limit_mi=_whole(m_limit),
            peak_mi=_whole(m_peak, up=True), median_mi=_whole(m_median),
            verdict=memory.verdict, reason=memory.reason,
            suggestion=(None if memory.request is None or memory.limit is None
                        else MemorySuggestion(request_mi=memory.request, limit_mi=memory.limit)),
        ),
        cpu=CpuSizing(
            request_m=_whole(c_request), limit_m=_whole(c_limit),
            p95_m=_tenth(c_p95), mean_m=_tenth(c_mean), peak_m=_tenth(c_peak),
            verdict=cpu.verdict, reason=cpu.reason,
            suggestion=(None if cpu.request is None
                        else CpuSuggestion(request_m=cpu.request, limit_m=cpu.limit)),
        ),
    )


def collect(state: AppState, project: str, env: str, window_days: int) -> SizeRecommend:
    entry = find_project(state, project)
    if entry is None:
        return not_found(f"project {project!r} is not in the catalog", project=project)
    if (missing := require_env(entry, project, env)) is not None:
        return missing
    state.backends.prom.require()
    namespace = ns(project, env)
    got = {key: _by_container(state, expr) for key, expr in queries(namespace, window_days).items()}
    # A container is a catalog component when it carries the component's name (the chart
    # names each workload's one container after it); sidecars, injected init containers
    # and the database's own pods are not components and are left out.
    items = [_component(name, got, window_days)
             for name in component_names(entry) if name in got["running"]]
    return SizeRecommend(project=project, env=env, namespace=namespace, window_days=window_days,
                         items=items, total=len(items), returned=len(items),
                         truncated=False, next_cursor=None)


def register(registry: Registry, state: AppState) -> None:

    @registry.tool(role=Role.viewer, read_only=True, idempotent=True)
    def orbit_size_recommend(
        project: Annotated[Ident, Field(description="Project id from orbit_project_list.")],
        env: Annotated[Ident, Field(description="Environment name, e.g. prod or dev.")],
        window_days: Annotated[int, Field(
            default=WINDOW_DEFAULT, ge=1, le=WINDOW_MAX,
            description="Days of usage to judge, 1 to 14. A week covers a weekly cycle.",
        )] = WINDOW_DEFAULT,
    ) -> SizeRecommend:
        """Sizing advice for each component of `<project>-<env>` that has a container
        running now: what it asks for, what it used over the last `window_days`, and a
        suggested request and limit. It only advises and changes nothing; apply an answer
        with orbit_catalog_set and ask again a week later.

        Per item: `component`, `containers` (running now), `history_days` (days of samples
        actually found), `restarts` and `oom_kills` in the window, then `memory` and `cpu`,
        judged separately. `memory`: `request_mi`, `limit_mi` (set today), `peak_mi` and
        `median_mi` (working set over the window), `suggestion` {request_mi, limit_mi}.
        `cpu`: `request_m`, `limit_m` (millicores set today, null when unset), `p95_m`,
        `mean_m`, `peak_m` (usage over the window), `suggestion` {request_m, limit_m}; a
        CPU limit is suggested only where one is set today. Each has a `verdict` and a
        one-sentence `reason`:

        - `lower` / `raise`: the suggestion is more than 10% away from what is set today.
        - `leave`: within 10%, change nothing.
        - `no_suggestion`: `suggestion` is null and the reason says why: less than 24 h of
          history, gaps in the data, no samples, or memory that sits at its limit without
          being killed. That last one means memory use does not show the need (a runtime
          that manages its own heap fills what it is given and can fail inside the limit):
          read its error rate and its own heap metrics, and consider raising it.
        A `lower` for memory ends its reason with a caution: a runtime that sizes its heap
        from the limit (.NET, a JVM) can already be failing below the limit, where its use
        reads as room to spare; check its error rate before lowering and after.

        Memory is the peak plus 15%, rounded up to 16 Mi, request equal to limit; after an
        out-of-memory kill it is at least the larger of limit x 1.2 and limit + 100 Mi.
        CPU is the p95 plus 15%, rounded up to 10m. No memory suggestion is ever below the
        peak seen. A component with nothing running now is not listed, so an empty
        `items` means no component of the pair is running, not "all fine".
        Example: orbit_size_recommend(project="demo", env="prod") -> {namespace:
        "demo-prod", window_days: 7, items: [{component: "web", containers: 1,
        history_days: 7.0, restarts: 0, oom_kills: 0, memory: {request_mi: 512, limit_mi:
        512, peak_mi: 100, median_mi: 90, verdict: "lower", reason: "peak 100 Mi over
        7.0 d plus 15% is 128 Mi, below the current 512 Mi", suggestion: {request_mi: 128,
        limit_mi: 128}}, cpu: {request_m: 50, limit_m: null, p95_m: 40.0, mean_m: 12.0,
        peak_m: 60.0, verdict: "leave", reason: "p95 40m over 7.0 d plus 15% is 50m,
        within 10% of the current 50m", suggestion: {request_m: 50, limit_m: null}}}]}."""
        return guarded(lambda: collect(state, project, env, window_days))
