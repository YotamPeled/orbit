"""orbit_platform_health — node reservation, quota headroom, certs, readiness."""

from __future__ import annotations

from datetime import datetime, timezone

from pydantic import BaseModel

from mcp.server.mcpserver import Context

from ..backends import BackendError, BackendUnconfigured
from ..roles import Registry, Role
from ..state import AppState
from . import _util as u


class NodeReservation(BaseModel):
    name: str
    allocatable_mi: int
    requested_mi: int


class QuotaHeadroom(BaseModel):
    namespace: str
    hard_mi: int
    used_mi: int
    headroom_mi: int
    largest_component_mi: int
    surge_fits: bool


class CertificateExpiry(BaseModel):
    namespace: str
    name: str
    not_after: str | None = None
    days_left: int | None = None


class UnreadyDeployment(BaseModel):
    namespace: str
    name: str
    replicas: int
    ready: int
    unavailable: int


class PlatformHealth(BaseModel):
    nodes: list[NodeReservation]
    quotas: list[QuotaHeadroom]
    certificates: list[CertificateExpiry]
    readiness: list[UnreadyDeployment]


def _pod_requests_on_node(pods: list, node_name: str) -> int:
    total = 0
    for pod in pods:
        if not u.pod_is_active(pod):
            continue
        spec = u.get(pod, "spec")
        bound = u.get(spec, "nodeName", "node_name")
        if bound != node_name:
            continue
        total += u.pod_memory_mi(pod, "requests")
    return total


def _all_pods(kube) -> list:
    pods: list = []
    for ns in u.namespace_names(kube):
        pods.extend(u.list_pods(kube, ns))
    return pods


def _largest_component_mi(state: AppState, kube, namespace: str) -> int:
    best = 0
    for pod in u.list_pods(kube, namespace):
        if not u.pod_is_active(pod):
            continue
        best = max(best, u.pod_memory_mi(pod, "requests"))
    for dep in u.list_deployments(kube, namespace):
        template = u.get(u.get(dep, "spec"), "template")
        spec = u.get(template, "spec")
        total = 0
        for container in u.get(spec, "containers") or []:
            total += u.container_memory_mi(container, "requests") or u.container_memory_mi(container, "limits")
        best = max(best, total)
    try:
        catalog = state.backends.instance.projects()
    except (BackendUnconfigured, BackendError):
        catalog = None
    if isinstance(catalog, dict):
        for proj in catalog.get("projects") or []:
            pid = str(proj.get("id") or "")
            for env in (proj.get("environments") or {}):
                if f"{pid}-{env}" != namespace:
                    continue
                for comp in proj.get("components") or []:
                    best = max(best, u.memory_mi(comp.get("memory")))
    return best


def _parse_not_after(value: str | None) -> datetime | None:
    if not value:
        return None
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def collect_health(state: AppState) -> PlatformHealth:
    kube = state.backends.kube
    pods = _all_pods(kube)
    nodes: list[NodeReservation] = []
    for node in u.list_nodes(kube):
        name = u.meta_name(node)
        allocatable = u.as_map(u.get(u.get(node, "status"), "allocatable"))
        alloc = u.memory_mi(allocatable.get("memory"))
        nodes.append(NodeReservation(
            name=name, allocatable_mi=alloc, requested_mi=_pod_requests_on_node(pods, name),
        ))

    quotas: list[QuotaHeadroom] = []
    for ns in u.namespace_names(kube):
        hard = used = None
        for quota in u.list_quotas(kube, ns):
            h = u.quota_memory_mi(quota, "hard")
            w = u.quota_memory_mi(quota, "used")
            if h is not None:
                hard = (hard or 0) + h
            if w is not None:
                used = (used or 0) + w
        if hard is None:
            continue
        used_mi = used or 0
        headroom = hard - used_mi
        largest = _largest_component_mi(state, kube, ns)
        quotas.append(QuotaHeadroom(
            namespace=ns, hard_mi=hard, used_mi=used_mi, headroom_mi=headroom,
            largest_component_mi=largest, surge_fits=headroom >= largest,
        ))

    now = datetime.now(timezone.utc)
    certificates: list[CertificateExpiry] = []
    certs = u.list_custom(kube, u.CERT_GROUP, u.CERT_VERSION, u.CERT_PLURAL) or []
    for cert in certs:
        status = u.as_map(u.get(cert, "status"))
        not_after = status.get("notAfter") or status.get("not_after")
        if not isinstance(not_after, str):
            not_after = None
        expiry = _parse_not_after(not_after)
        days_left = None if expiry is None else int((expiry - now).total_seconds() // 86400)
        certificates.append(CertificateExpiry(
            namespace=u.meta_namespace(cert), name=u.meta_name(cert),
            not_after=not_after, days_left=days_left,
        ))

    readiness: list[UnreadyDeployment] = []
    for ns in u.namespace_names(kube):
        for dep in u.list_deployments(kube, ns):
            spec = u.get(dep, "spec")
            status = u.get(dep, "status")
            replicas = int(u.get(spec, "replicas") or u.get(status, "replicas") or 0)
            ready = int(u.get(status, "availableReplicas", "available_replicas", "readyReplicas",
                              "ready_replicas") or 0)
            unavailable = int(u.get(status, "unavailableReplicas", "unavailable_replicas") or 0)
            if unavailable == 0:
                unavailable = max(replicas - ready, 0)
            if unavailable <= 0:
                continue
            readiness.append(UnreadyDeployment(
                namespace=ns, name=u.meta_name(dep),
                replicas=replicas, ready=ready, unavailable=unavailable,
            ))

    return PlatformHealth(nodes=nodes, quotas=quotas, certificates=certificates, readiness=readiness)


def register(registry: Registry, state: AppState) -> None:

    @registry.tool(role=Role.viewer, read_only=True, idempotent=True)
    def orbit_platform_health(ctx: Context) -> PlatformHealth:
        """Cluster health: node allocatable versus requested, quota headroom per
        namespace (hard minus used, and whether one surge pod of the largest
        component still fits), cert-manager Certificate notAfter/days_left, and
        deployments with unavailable replicas. Example: orbit_platform_health() ->
        {nodes: [{name: "k3s-1", allocatable_mi: 14131, requested_mi: 4096}],
        quotas: [{namespace: "demo-prod", headroom_mi: 1024, surge_fits: true}],
        certificates: [{name: "wildcard", days_left: 40}], readiness: []}."""
        try:
            return collect_health(state)
        except (BackendUnconfigured, BackendError) as e:
            return e.as_failure()  # type: ignore[return-value]
