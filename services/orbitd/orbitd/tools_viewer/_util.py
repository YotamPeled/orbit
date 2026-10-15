"""Shared helpers for viewer tools: memory quantities, kube accessors, backend errors."""

from __future__ import annotations

import re
from typing import Any

from kubernetes.client.exceptions import ApiException

from ..backends import BackendError, BackendUnconfigured

MI = 1024 * 1024
ID_RE = re.compile(r"^[a-z0-9]([a-z0-9-]{0,38}[a-z0-9])?$")

# Binary suffixes first so "Mi" is not parsed as "M" + "i". "m" is millibytes
# (metrics.k8s.io), not mega.
_MEMORY_SUFFIXES: tuple[tuple[str, int], ...] = (
    ("Ei", 1024 ** 6), ("Pi", 1024 ** 5), ("Ti", 1024 ** 4),
    ("Gi", 1024 ** 3), ("Mi", 1024 ** 2), ("Ki", 1024),
    ("E", 1000 ** 6), ("P", 1000 ** 5), ("T", 1000 ** 4),
    ("G", 1000 ** 3), ("M", 1000 ** 2), ("K", 1000), ("k", 1000),
)

METRICS_GROUP, METRICS_VERSION, METRICS_PLURAL = "metrics.k8s.io", "v1beta1", "pods"
CERT_GROUP, CERT_VERSION, CERT_PLURAL = "cert-manager.io", "v1", "certificates"


def memory_bytes(value: Any) -> int:
    if value is None or value == "":
        return 0
    if isinstance(value, bool):
        return 0
    if isinstance(value, (int, float)):
        return int(value)
    text = str(value).strip()
    if not text:
        return 0
    if text.endswith("m") and not text.endswith("Mi") and not text.endswith("M"):
        try:
            return int(float(text[:-1]) / 1000.0)
        except ValueError:
            return 0
    for suffix, mul in _MEMORY_SUFFIXES:
        if text.endswith(suffix):
            try:
                return int(float(text[: -len(suffix)]) * mul)
            except ValueError:
                return 0
    try:
        return int(float(text))
    except ValueError:
        return 0


def memory_mi(value: Any) -> int:
    return memory_bytes(value) // MI


def as_map(obj: Any) -> dict[str, Any]:
    if obj is None:
        return {}
    if isinstance(obj, dict):
        return obj
    if hasattr(obj, "to_dict"):
        data = obj.to_dict()
        if isinstance(data, dict):
            return data
    data = getattr(obj, "_data", None)
    return data if isinstance(data, dict) else {}


def get(obj: Any, *names: str, default: Any = None) -> Any:
    """First present dict key or attribute among `names` (camelCase and snake_case)."""
    if obj is None:
        return default
    mapping = obj if isinstance(obj, dict) else None
    if mapping is not None:
        for name in names:
            if name in mapping:
                return mapping[name]
        return default
    for name in names:
        if hasattr(obj, name):
            value = getattr(obj, name)
            if value is not None:
                return value
    data = getattr(obj, "_data", None)
    if isinstance(data, dict):
        for name in names:
            if name in data:
                return data[name]
    return default


def items_of(listing: Any) -> list[Any]:
    if listing is None:
        return []
    if isinstance(listing, dict):
        return list(listing.get("items") or [])
    found = getattr(listing, "items", None)
    return list(found) if found else []


def meta_name(obj: Any) -> str:
    return str(get(get(obj, "metadata"), "name") or "")


def meta_namespace(obj: Any) -> str:
    return str(get(get(obj, "metadata"), "namespace") or "")


def quota_memory_mi(quota: Any, which: str) -> int | None:
    """`which` is 'hard' or 'used'. Prefers requests.memory, then memory."""
    status = get(quota, "status")
    spec = get(quota, "spec")
    mapping = as_map(get(status, which)) or (as_map(get(spec, which)) if which == "hard" else {})
    for key in ("requests.memory", "memory"):
        if key in mapping and mapping[key] not in (None, ""):
            return memory_mi(mapping[key])
    return None


def container_memory_mi(container: Any, kind: str) -> int:
    resources = get(container, "resources")
    return memory_mi(as_map(get(resources, kind)).get("memory"))


def _seq(value: Any) -> list[Any]:
    if not value:
        return []
    return list(value) if not isinstance(value, list) else value


TERMINAL_PHASES = {"Succeeded", "Failed"}


def pod_is_active(pod: Any) -> bool:
    """False for Succeeded/Failed and for pods with a deletionTimestamp."""
    meta = get(pod, "metadata")
    if get(meta, "deletionTimestamp", "deletion_timestamp"):
        return False
    phase = get(get(pod, "status"), "phase")
    return phase not in TERMINAL_PHASES


def pod_memory_mi(pod: Any, kind: str) -> int:
    """Effective pod reservation: max(sum(app containers), max(init containers))."""
    spec = get(pod, "spec")
    app = 0
    for container in _seq(get(spec, "containers")):
        app += container_memory_mi(container, kind)
    inits = [
        container_memory_mi(container, kind)
        for container in _seq(get(spec, "initContainers") or get(spec, "init_containers"))
    ]
    return max(app, max(inits) if inits else 0)


def kube_call(fn, *args, **kwargs):
    try:
        return fn(*args, **kwargs)
    except (BackendUnconfigured, BackendError):
        raise
    except ApiException as e:
        raise BackendError(
            status=getattr(e, "status", None),
            body=str(e),
            what_failed="kubernetes request failed",
            env_var="ORBITD_KUBECONFIG",
        ) from e
    except Exception as e:
        raise BackendError(
            status=None, body=str(e),
            what_failed="kubernetes request failed",
            env_var="ORBITD_KUBECONFIG",
        ) from e


def namespace_names(kube: Any) -> list[str]:
    names: set[str] = set()
    for ns in items_of(kube_call(kube.core.list_namespace)):
        name = meta_name(ns)
        phase = get(get(ns, "status"), "phase")
        if name and phase != "Terminating":
            names.add(name)
    for pod in getattr(kube, "pods", []) or []:
        ns = (pod.get("metadata") or {}).get("namespace") if isinstance(pod, dict) else meta_namespace(pod)
        if ns:
            names.add(ns)
    for dep in getattr(kube, "deployments", []) or []:
        ns = (dep.get("metadata") or {}).get("namespace") if isinstance(dep, dict) else meta_namespace(dep)
        if ns:
            names.add(ns)
    for quota in getattr(kube, "quotas", []) or []:
        ns = (quota.get("metadata") or {}).get("namespace") if isinstance(quota, dict) else meta_namespace(quota)
        if ns:
            names.add(ns)
    if names:
        return sorted(names)
    # Real cluster: list_namespace already populated `names`. Empty means none.
    return []


def list_custom(kube: Any, group: str, version: str, plural: str) -> list[Any] | None:
    """All-namespaces custom objects. None means the API is absent (404)."""
    custom = kube.custom
    if hasattr(custom, "list_custom_object_for_all_namespaces"):
        try:
            body = kube_call(custom.list_custom_object_for_all_namespaces, group, version, plural)
            return items_of(body)
        except BackendError as e:
            if e.status in (404, 403):
                return None
            raise
    collected: list[Any] = []
    seen_api = False
    for ns in namespace_names(kube):
        try:
            body = kube_call(custom.list_namespaced_custom_object, group, version, ns, plural)
        except BackendError as e:
            if e.status in (404, 403):
                continue
            raise
        seen_api = True
        collected.extend(items_of(body))
    if not seen_api and not collected:
        return None
    return collected


def list_pods(kube: Any, namespace: str) -> list[Any]:
    return items_of(kube_call(kube.core.list_namespaced_pod, namespace))


def list_quotas(kube: Any, namespace: str) -> list[Any]:
    return items_of(kube_call(kube.core.list_namespaced_resource_quota, namespace))


def list_deployments(kube: Any, namespace: str) -> list[Any]:
    return items_of(kube_call(kube.apps.list_namespaced_deployment, namespace))


def list_nodes(kube: Any) -> list[Any]:
    return items_of(kube_call(kube.core.list_node))
