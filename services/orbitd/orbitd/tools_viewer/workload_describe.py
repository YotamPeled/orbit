"""orbit_workload_describe — spec, pods, events and usage vs limit."""

from __future__ import annotations

from typing import Annotated, Any

from pydantic import BaseModel, Field

from orbitd.roles import Listing, Registry, Role
from orbitd.state import AppState

from .util import Ident
from .common import (
    relayed, require_env,
    EVENTS_CAP, WORKLOAD_CAP, as_dict, component_names, find_deployment, find_project,
    guarded, list_pods, not_found, ns, paginate, pick, pod_row, rfc3339_now, selector,
)


class ProbeSummary(BaseModel):
    type: str
    path: str | None = None
    port: Any = None
    delay_seconds: int | None = None


class SpecSummary(BaseModel):
    image: str | None = None
    requests: dict[str, Any] = Field(default_factory=dict)
    limits: dict[str, Any] = Field(default_factory=dict)
    probes: list[ProbeSummary] = Field(default_factory=list)
    replicas: int | None = None


class WorkloadEvent(BaseModel):
    name: str | None = None
    reason: str | None = None
    message: str | None = None
    involved: str | None = None


class UsageVsLimit(BaseModel):
    pod: str | None = None
    cpu: str | None = None
    memory: str | None = None
    cpu_limit: str | None = None
    memory_limit: str | None = None


class WorkloadDescribe(BaseModel):
    project: str
    env: str
    component: str
    spec: SpecSummary
    pods: Listing
    events: list[WorkloadEvent]
    usage_vs_limit: list[UsageVsLimit] | None = None
    now: str = Field(description="RFC 3339 timestamp of the server clock at the call")


def register(registry: Registry, state: AppState) -> None:

    @registry.tool(role=Role.viewer, read_only=True, idempotent=True)
    def orbit_workload_describe(
        project: Annotated[Ident, Field(description="Project id from orbit_project_list.")],
        env: Annotated[Ident, Field(description="Environment name, e.g. prod or dev.")],
        component: Annotated[Ident, Field(description="Component name from orbit_project_list.")],
    ) -> WorkloadDescribe:
        """Describe one component: its deployment spec (image, requests, limits, probes),
        its pods (`age` is seconds since the pod was created), the events the cluster
        still holds on those pods or the deployment (they expire, typically within an
        hour; an empty list on an older pod means expired, not "nothing happened"),
        and usage vs limit from metrics.k8s.io when that API answers.
        usage_vs_limit lists only this component's pods (the ones in pods[]);
        memory_limit is set on every listed row. `now` is the server clock as RFC 3339.
        `pod` names match orbit_workload_list.
        Example: orbit_workload_describe(project="demo", env="prod", component="web")."""
        return guarded(lambda: _describe(state, project, env, component))


def _describe(state: AppState, project: str, env: str, component: str) -> WorkloadDescribe:
    entry = find_project(state, project)
    if entry is None:
        return not_found(f"project {project!r} is not in the catalog", project=project)
    if (missing := require_env(entry, project, env)) is not None:
        return missing
    if component not in component_names(entry):
        return not_found(
            f"component {component!r} is not in project {project!r}",
            project=project, component=component,
        )
    kube = state.backends.kube
    namespace = ns(project, env)
    dep = find_deployment(kube, namespace, project, component)
    pods = list_pods(kube, namespace, selector(project, component))
    pod_rows = [pod_row(p) for p in pods]
    pod_names = [r["pod"] for r in pod_rows if r.get("pod")]
    dep_name = pick(as_dict(as_dict(dep).get("metadata") if dep is not None else {}), "name")
    return WorkloadDescribe(
        project=project, env=env, component=component,
        spec=_spec(dep, pods),
        pods=paginate(pod_rows, cursor=None, limit=WORKLOAD_CAP, cap=WORKLOAD_CAP),
        events=_events(kube, namespace, {n for n in pod_names + [dep_name] if n}),
        usage_vs_limit=_usage(kube, namespace, pods),
        now=rfc3339_now(),
    )


def _spec(dep: Any, pods: list[Any]) -> SpecSummary:
    container: dict[str, Any] = {}
    replicas = None
    if dep is not None:
        data = as_dict(dep)
        spec = as_dict(pick(data, "spec") or {})
        replicas = pick(spec, "replicas")
        template = as_dict(pick(spec, "template") or {})
        pod_spec = as_dict(pick(template, "spec") or {})
        containers = pick(pod_spec, "containers") or []
        if containers:
            container = as_dict(containers[0])
    if not container and pods:
        pod_spec = as_dict(pick(as_dict(pods[0]), "spec") or {})
        containers = pick(pod_spec, "containers") or []
        if containers:
            container = as_dict(containers[0])
    resources = as_dict(pick(container, "resources") or {})
    probes = []
    for key, snake, label in (
        ("livenessProbe", "liveness_probe", "liveness"),
        ("readinessProbe", "readiness_probe", "readiness"),
        ("startupProbe", "startup_probe", "startup"),
    ):
        raw = pick(container, key, snake)
        if raw:
            probes.append(_probe(label, as_dict(raw)))
    return SpecSummary(
        image=pick(container, "image"),
        requests=as_dict(pick(resources, "requests") or {}),
        limits=as_dict(pick(resources, "limits") or {}),
        probes=probes,
        replicas=int(replicas) if replicas is not None else None,
    )


def _probe(kind: str, raw: dict[str, Any]) -> ProbeSummary:
    http = as_dict(pick(raw, "httpGet", "http_get") or {})
    return ProbeSummary(
        type=kind,
        path=pick(http, "path"),
        port=pick(http, "port"),
        delay_seconds=_as_int(pick(raw, "initialDelaySeconds", "initial_delay_seconds")),
    )


def _events(kube: Any, namespace: str, names: set[str]) -> list[WorkloadEvent]:
    if not names:
        # nothing of this component exists to hold an event: an empty set is not
        # "every event in the namespace"
        return []
    listed = kube.core.list_namespaced_event(namespace)
    out: list[WorkloadEvent] = []
    for raw in list(getattr(listed, "items", None) or []):
        data = as_dict(raw)
        involved = as_dict(pick(data, "involvedObject", "involved_object") or {})
        involved_name = pick(involved, "name")
        if names and involved_name not in names:
            continue
        meta = as_dict(pick(data, "metadata") or {})
        out.append(WorkloadEvent(
            name=pick(meta, "name"),
            reason=relayed(pick(data, "reason"), limit=200),
            message=relayed(pick(data, "message")),
            involved=involved_name,
        ))
    return out[-EVENTS_CAP:]


def _usage(kube: Any, namespace: str, pods: list[Any]) -> list[UsageVsLimit] | None:
    try:
        payload = kube.custom.list_namespaced_custom_object(
            "metrics.k8s.io", "v1beta1", namespace, "pods",
        )
    except Exception:
        return None
    items = (payload or {}).get("items") if isinstance(payload, dict) else None
    if not items:
        return None
    limits_by_pod: dict[str, dict[str, Any]] = {}
    component_pods: set[str] = set()
    for pod in pods:
        row = pod_row(pod)
        name = row.get("pod")
        if not name:
            continue
        component_pods.add(name)
        data = as_dict(pod)
        spec = as_dict(pick(data, "spec") or {})
        containers = pick(spec, "containers") or []
        first = as_dict(containers[0] if containers else {})
        resources = as_dict(pick(first, "resources") or {})
        limits_by_pod[name] = as_dict(pick(resources, "limits") or {})
    out: list[UsageVsLimit] = []
    for item in items:
        data = as_dict(item)
        meta = as_dict(pick(data, "metadata") or {})
        name = pick(meta, "name")
        if name not in component_pods:
            continue
        containers = pick(data, "containers") or []
        usage = as_dict(pick(as_dict(containers[0] if containers else {}), "usage") or {})
        limits = limits_by_pod.get(name) or {}
        memory_limit = pick(limits, "memory")
        if memory_limit is None:
            continue
        out.append(UsageVsLimit(
            pod=name,
            cpu=pick(usage, "cpu"),
            memory=pick(usage, "memory"),
            cpu_limit=pick(limits, "cpu"),
            memory_limit=memory_limit,
        ))
    return out


def _as_int(value: Any) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None
