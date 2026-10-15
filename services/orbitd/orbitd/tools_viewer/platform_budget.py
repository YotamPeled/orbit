"""orbit_platform_budget — DESIGN-v2 §2 table, live from the cluster."""

from __future__ import annotations

from pydantic import BaseModel

from mcp.server.mcpserver import Context

from ..backends import BackendError, BackendUnconfigured
from ..roles import Registry, Role
from ..state import AppState
from . import _util as u


class BudgetRow(BaseModel):
    namespace: str
    requests_mi: int
    limits_mi: int
    used_mi: int | None = None
    quota_hard_mi: int | None = None
    quota_used_mi: int | None = None


class PlatformBudget(BaseModel):
    """Listing envelope plus the node allocatable (DESIGN-v2 §3.4)."""

    items: list[BudgetRow]
    total: int | None = None
    returned: int = 0
    truncated: bool = False
    next_cursor: str | None = None
    node_allocatable_mi: int = 0


def _metrics_by_namespace(kube) -> dict[str, int] | None:
    items = u.list_custom(kube, u.METRICS_GROUP, u.METRICS_VERSION, u.METRICS_PLURAL)
    if items is None:
        return None
    used: dict[str, int] = {}
    for pod in items:
        ns = u.meta_namespace(pod)
        if not ns:
            continue
        total = 0
        for container in u.as_map(pod).get("containers") or u.get(pod, "containers") or []:
            usage = u.as_map(u.get(container, "usage"))
            total += u.memory_mi(usage.get("memory"))
        used[ns] = used.get(ns, 0) + total
    return used


def collect_budget(state: AppState) -> PlatformBudget:
    kube = state.backends.kube
    usage = _metrics_by_namespace(kube)
    rows: list[BudgetRow] = []
    sum_req = sum_lim = 0
    sum_used = 0
    used_any = False
    for ns in u.namespace_names(kube):
        pods = [p for p in u.list_pods(kube, ns) if u.pod_is_active(p)]
        requests_mi = sum(u.pod_memory_mi(p, "requests") for p in pods)
        limits_mi = sum(u.pod_memory_mi(p, "limits") for p in pods)
        used_mi = None if usage is None else usage.get(ns, 0)
        hard = used_q = None
        for quota in u.list_quotas(kube, ns):
            h = u.quota_memory_mi(quota, "hard")
            w = u.quota_memory_mi(quota, "used")
            if h is not None:
                hard = (hard or 0) + h
            if w is not None:
                used_q = (used_q or 0) + w
        rows.append(BudgetRow(
            namespace=ns, requests_mi=requests_mi, limits_mi=limits_mi,
            used_mi=used_mi, quota_hard_mi=hard, quota_used_mi=used_q,
        ))
        sum_req += requests_mi
        sum_lim += limits_mi
        if used_mi is not None:
            sum_used += used_mi
            used_any = True
    alloc = 0
    for node in u.list_nodes(kube):
        allocatable = u.as_map(u.get(u.get(node, "status"), "allocatable"))
        alloc += u.memory_mi(allocatable.get("memory"))
    total_row = BudgetRow(
        namespace="_total", requests_mi=sum_req, limits_mi=sum_lim,
        used_mi=sum_used if used_any else None,
    )
    items = [*rows, total_row]
    return PlatformBudget(items=items, total=len(items), returned=len(items),
                          truncated=False, next_cursor=None, node_allocatable_mi=alloc)


def register(registry: Registry, state: AppState) -> None:

    @registry.tool(role=Role.viewer, read_only=True, idempotent=True)
    def orbit_platform_budget(ctx: Context) -> PlatformBudget:
        """Live memory budget per namespace: the sum of pod requests and limits,
        usage from metrics.k8s.io when that API answers, and ResourceQuota hard/used
        when a quota exists. A `_total` row sums the namespaces; `node_allocatable_mi`
        is cluster allocatable. Example: orbit_platform_budget() -> {items:
        [{namespace: "demo-prod", requests_mi: 512, limits_mi: 768, used_mi: 400,
        quota_hard_mi: 4096, quota_used_mi: 512}, {namespace: "_total", ...}],
        node_allocatable_mi: 14131}."""
        try:
            return collect_budget(state)
        except (BackendUnconfigured, BackendError) as e:
            return e.as_failure()  # type: ignore[return-value]
