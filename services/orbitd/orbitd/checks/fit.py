"""What the commit reserves fits the node.

Every component and database reserves memory (its request). The scheduler places a pod
only where its request fits, so a catalog that reserves more than the node has leaves
some pod Pending forever, and which one depends on restart order: it can be a prod
component that was running fine. Summed here from the rendered manifests, beside what
the platform itself reserves, against the node's allocatable memory.
"""

from __future__ import annotations

import re
from typing import Any

from . import Context
from .render import rendered_pairs

NAME = "fit"
WHAT = "the memory every project reserves, plus the platform's, fits the node"
MI = 1024 * 1024
_QUANTITY = re.compile(r"^([0-9]+(?:\.[0-9]+)?)(Ki|Mi|Gi|Ti|k|K|M|G|T|m)?$")
_UNIT = {None: 1, "Ki": 1024, "Mi": 1024 ** 2, "Gi": 1024 ** 3, "Ti": 1024 ** 4,
         "k": 1000, "K": 1000, "M": 1000 ** 2, "G": 1000 ** 3, "T": 1000 ** 4, "m": 0.001}


def quantity_bytes(value: object) -> int:
    m = _QUANTITY.match(str(value).strip()) if value is not None else None
    return int(float(m.group(1)) * _UNIT[m.group(2)]) if m else 0


def pod_request(spec: dict[str, Any]) -> int:
    """A pod's memory reservation: the sum of its containers, or its largest init container."""
    def req(container: Any) -> int:
        return quantity_bytes((((container or {}).get("resources") or {}).get("requests") or {}).get("memory"))
    app = sum(req(c) for c in spec.get("containers") or [])
    init = max((req(c) for c in spec.get("initContainers") or []), default=0)
    return max(app, init)


def reserved_bytes(objects: list[dict[str, Any]]) -> int:
    """Memory the workloads among `objects` reserve while running normally (a CronJob's
    pod comes and goes and is not counted)."""
    total = 0
    for obj in objects:
        kind, spec = obj.get("kind"), obj.get("spec") or {}
        if kind in ("Deployment", "StatefulSet"):
            replicas = spec.get("replicas")
            replicas = 1 if replicas is None else int(replicas)
            total += replicas * pod_request(((spec.get("template") or {}).get("spec")) or {})
        elif kind == "Cluster" and str(obj.get("apiVersion", "")).startswith("postgresql.cnpg.io/"):
            instances = int(spec.get("instances") or 1)
            total += instances * quantity_bytes(((spec.get("resources") or {}).get("requests") or {}).get("memory"))
    return total


def check(ctx: Context) -> str | None:
    pairs, failure = rendered_pairs(ctx)
    if failure:
        return f"what the commit reserves cannot be summed: {failure}"
    projects = sum(reserved_bytes(objects) for _, objects in pairs)
    node = ctx.facts.node_allocatable_bytes()
    platform = ctx.facts.platform_requests_bytes()
    if projects + platform <= node:
        return None
    return (f"the catalog reserves {projects // MI} Mi for projects and the platform holds {platform // MI} Mi, "
            f"{(projects + platform) // MI} Mi in all; the node has {node // MI} Mi to give: a pod would never "
            f"be scheduled. Lower a component's memory, remove a project, or use a larger node")
