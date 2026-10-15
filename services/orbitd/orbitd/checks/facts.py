"""What the checks may know about the cluster, read from the Kubernetes API.

Two numbers, both memory in bytes: what the node can give to pods, and what the platform
itself (every namespace that is not a project's) already reserves. Each is read when a
check first asks and kept for the run; a cluster that does not answer raises
FactsUnavailable, which a check reports as its own kind of failure.
"""

from __future__ import annotations

from typing import Any

from . import Facts, FactsUnavailable

PROJECT_LABEL = "orbit.local/project"


def from_kube(kube: Any) -> Facts:
    from ..tools_viewer import _util as u
    memo: dict[str, int] = {}

    def asked(key: str, read: Any) -> int:
        if key not in memo:
            try:
                memo[key] = int(read())
            except FactsUnavailable:
                raise
            except Exception as e:
                raise FactsUnavailable(f"the cluster did not answer ({e.__class__.__name__})") from e
        return memo[key]

    def node() -> int:
        total = 0
        for item in u.items_of(u.kube_call(kube.core.list_node)):
            allocatable = u.as_map(u.get(u.get(item, "status"), "allocatable"))
            total += u.memory_bytes(allocatable.get("memory"))
        if total <= 0:
            raise FactsUnavailable("no node reports allocatable memory")
        return total

    def platform() -> int:
        total = 0
        for ns in u.items_of(u.kube_call(kube.core.list_namespace)):
            labels = u.as_map(u.get(u.get(ns, "metadata"), "labels"))
            if labels.get(PROJECT_LABEL):
                continue                       # a project's namespace: the catalog's, summed from the commit
            name = u.meta_name(ns)
            if not name:
                continue
            for pod in u.list_pods(kube, name):
                if u.pod_is_active(pod):
                    total += u.pod_memory_mi(pod, "requests") * 1024 * 1024
        return total

    return Facts(node_allocatable_bytes=lambda: asked("node", node),
                 platform_requests_bytes=lambda: asked("platform", platform))


def fixed(node_mi: int, platform_mi: int) -> Facts:
    """Facts given on the command line (a fixture, a CI run with no cluster)."""
    return Facts(node_allocatable_bytes=lambda: node_mi * 1024 * 1024,
                 platform_requests_bytes=lambda: platform_mi * 1024 * 1024)
