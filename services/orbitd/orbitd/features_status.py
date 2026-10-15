"""The state of every feature, in one shape: what the checkout declares, what the cluster
has, what each feature needs and whether it is there, the tools it carries, what it costs
right now, and what a converge would do next.

Three readers print this and nothing else: the orbit_features_list tool, `orbit features
status` on the desk, and `orbit onboard status`. One collector, so they cannot disagree.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import yaml

from . import features as features_module
from . import features_converge as converge
from . import orbit_features as rules

SOPS_GROUP, SOPS_VERSION, SOPS_PLURAL = "isindir.github.com", "v1alpha3", "sopssecrets"
SECRETS_REL = "clusters/platform/secrets"
STAGED_APPS_REL = "provisioning/clusters/platform/apps"
TOKEN_RE = re.compile(r"\{\{[A-Za-z0-9_.]+\}\}")
WORKLOAD_KINDS = ("Deployment", "StatefulSet", "DaemonSet")


def app_namespaces(root: Path, registry: dict[str, Any]) -> dict[str, str]:
    """{Application: destination namespace}, read from the staged tree (which holds every
    feature's Applications whether or not the feature is on)."""
    out: dict[str, str] = {}
    all_on = {name: True for name in registry}
    directory = root / STAGED_APPS_REL
    if not directory.is_dir():
        return out
    for f in sorted(directory.glob("*.yaml")):
        try:
            text = TOKEN_RE.sub("x", rules.apply_blocks(f.read_text(encoding="utf-8"), all_on))
            docs = list(yaml.safe_load_all(text))
        except (OSError, yaml.YAMLError, rules.FeatureError):
            continue
        for doc in docs:
            if isinstance(doc, dict) and doc.get("kind") == "Application":
                name = str((doc.get("metadata") or {}).get("name") or "")
                ns = str(((doc.get("spec") or {}).get("destination") or {}).get("namespace") or "")
                if name and ns:
                    out[name] = ns
    return out


def _secret_row(root: Path, kube: Any, ref: str) -> dict[str, Any]:
    """A sealed secret a feature needs: sealed in git, and applied on the cluster."""
    namespace, _, name = ref.partition("/")
    in_git = (root / SECRETS_REL / namespace / f"{name}.yaml").is_file()
    on_cluster: bool | None
    try:
        kube.dynamic.resources.get(api_version=f"{SOPS_GROUP}/{SOPS_VERSION}", kind="SopsSecret").get(
            name=name, namespace=namespace)
        on_cluster = True
    except Exception as e:
        on_cluster = False if getattr(e, "status", None) == 404 else None
    met = in_git and on_cluster is not False
    if not in_git:
        detail = f"the sealed secret {ref} is not in the instance repo ({SECRETS_REL}/{namespace}/{name}.yaml)"
    elif on_cluster is False:
        detail = f"the sealed secret {ref} is in the instance repo but not applied on the cluster"
    elif on_cluster is None:
        detail = f"the sealed secret {ref} is in the instance repo; the cluster could not be asked"
    else:
        detail = f"the sealed secret {ref} is sealed and applied"
    return {"name": f"secret {ref}", "met": met, "detail": detail}


def _requirement_rows(root: Path, kube: Any, config: dict[str, Any], registry: dict[str, Any],
                      on: dict[str, bool], name: str) -> list[dict[str, Any]]:
    rows = []
    for req in registry[name]["requires"]:
        if req == "bucket":
            met = rules.has_bucket(config)
            rows.append({"name": "bucket", "met": met,
                         "detail": ("objectStorage.buckets.platform is declared" if met else
                                    "no objectStorage.buckets.platform in instance/config.yaml")})
        else:
            met = bool(on.get(req))
            rows.append({"name": f"feature {req}", "met": met,
                         "detail": f"the {req} feature is {'on' if met else 'off'}"})
    for ref in registry[name].get("secrets") or []:
        rows.append(_secret_row(root, kube, str(ref)))
    return rows


def _not_ready(ops: Any, kube: Any, app: str) -> list[str]:
    """The workloads of an applied app that are not ready, as `Kind/namespace/name`."""
    from .platform_apply import read_app_inventory
    try:
        refs, _ = read_app_inventory(kube, app)
    except Exception:
        return [f"{app}: its inventory could not be read"]
    waiting = []
    for r in refs:
        if r.kind not in WORKLOAD_KINDS:
            continue
        try:
            ready = ops._ready(r)
        except Exception as e:      # a workload that cannot be read is not known to be ready
            waiting.append(f"{r.kind}/{r.namespace}/{r.name} (could not be read: {e.__class__.__name__})")
            continue
        if not ready:
            waiting.append(f"{r.kind}/{r.namespace}/{r.name}")
        elif r.kind != "DaemonSet" and getattr(ops, "wanted", lambda _r: None)(r) == 0:
            # zero wanted, zero ready: nothing of it runs. No workload of a platform
            # feature is meant to run no pod, so this is a feature that is not there.
            waiting.append(f"{r.kind}/{r.namespace}/{r.name} (scaled to 0)")
    return waiting


_WORKLOAD_NAME = re.compile(r"[a-z0-9]([a-z0-9.-]{0,251}[a-z0-9])?")


def _workloads(kube: Any, apps: list[str]) -> list[tuple[str, str]] | None:
    """(namespace, name) of every workload the feature's own applications applied; None
    when an inventory cannot be read."""
    from .platform_apply import read_app_inventory
    out: list[tuple[str, str]] = []
    for app in apps:
        try:
            refs, _ = read_app_inventory(kube, app)
        except Exception:
            return None
        out += [(str(r.namespace), str(r.name)) for r in refs if r.kind in WORKLOAD_KINDS and r.namespace and r.name]
    return out


def _memory_mib(prom: Any, workloads: list[tuple[str, str]] | None) -> float | None:
    """Resident memory of a feature's OWN workloads right now, MiB; None when it cannot be
    asked or the feature has no workload of its own.

    It was the memory of every container in the namespaces the feature's applications
    live in. The uptime feature is one small exporter in `monitoring`, where Prometheus,
    Alertmanager and Grafana also run: it read 708 MiB (QA of v1.0.0, F39). A workload's
    pods are named after it (`<name>-...`), in its namespace."""
    if not workloads:
        return None
    by_namespace: dict[str, set[str]] = {}
    for namespace, name in workloads:
        by_namespace.setdefault(namespace, set()).add(name)
    # A workload's name is a DNS name: letters, digits, `-` and `.`. Only the dot means
    # something in a regular expression, and inside a PromQL string its backslash is
    # doubled. (`re.escape` also escapes the hyphen, and `\-` is not an escape PromQL
    # knows: the query for `blackbox-exporter` was a parse error and the number null.)
    selectors = " or ".join(
        'container_memory_rss{container!="",namespace="%s",pod=~"(%s)-.+"}'
        % (namespace, "|".join(n.replace(".", "\\\\.") for n in sorted(names) if _WORKLOAD_NAME.fullmatch(n)))
        for namespace, names in sorted(by_namespace.items()) if _WORKLOAD_NAME.fullmatch(namespace))
    if not selectors:
        return None
    try:
        data = prom.get("/api/v1/query", params={"query": f"sum({selectors})"})
        result = ((data or {}).get("data") or {}).get("result") or []
        if not result:
            return 0.0
        return round(float(result[0]["value"][1]) / 1048576, 1)
    except Exception:
        return None


def collect(state: Any) -> dict[str, Any]:
    """{features: [row...], explicit, problem, converge_job, record}. Raises nothing: what
    cannot be read is said in the row or in `problem`."""
    out: dict[str, Any] = {"features": [], "explicit": False, "problem": None,
                           "converge_job": None, "record": None}
    root_dir = state.backends.instance.root
    if root_dir is None:
        out["problem"] = "ORBITD_INSTANCE_DIR is not set: there is no instance checkout to read"
        return out
    root = Path(root_dir)
    try:
        snap = features_module.read(root)
    except rules.RegistryMissing:
        out["problem"] = ("this instance's core predates features (no provisioning/features.yaml): "
                          "everything it renders runs, and nothing can be switched")
        return out
    except rules.FeatureError as e:
        out["problem"] = str(e)
        return out
    try:
        config = yaml.safe_load((root / features_module.CONFIG_REL).read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError):
        config = {}
    kube = state.backends.kube
    ops = converge.ClusterOps(root, kube)
    out["explicit"] = snap.explicit
    try:
        plan = converge.make_plan(snap.registry, snap.on, ops, assumed=converge.assumed_states(root, snap.registry))
        out["record"] = ops.read_record()
    except Exception as e:
        out["problem"] = f"the cluster could not be asked what is applied ({e.__class__.__name__})"
        plan = None
    try:
        from . import features_job
        running = features_job.active(kube)
        out["converge_job"] = ((running or {}).get("metadata") or {}).get("name")
    except Exception:
        out["converge_job"] = None
    for name, spec in snap.registry.items():
        row: dict[str, Any] = {
            "feature": name, "summary": " ".join(str(spec.get("summary") or "").split()),
            "always": spec["always"], "declared": bool(snap.on.get(name)),
            "requires": _requirement_rows(root, kube, config, snap.registry, snap.on, name),
            "tools": list(spec["tools"]), "apps": {}, "applied": None, "ready": None,
            "not_ready": [], "pending": None, "memory_mib": None,
        }
        if spec["always"]:
            row["applied"] = True
            out["features"].append(row)
            continue
        if plan is not None:
            apps = {app: ops.applied(app) for app in spec["apps"]}
            row["apps"] = apps
            row["applied"] = all(apps.values()) if apps else plan.was.get(name)
            row["pending"] = ("enable" if name in plan.enable else "disable" if name in plan.disable else None)
            for app, applied in apps.items():
                if applied:
                    row["not_ready"] += _not_ready(ops, kube, app)
            row["ready"] = bool(row["applied"]) and not row["not_ready"] if row["declared"] else None
            if any(apps.values()):
                row["memory_mib"] = _memory_mib(state.backends.prom,
                                                _workloads(kube, [a for a, applied in apps.items() if applied]))
        out["features"].append(row)
    return out


def sentence(row: dict[str, Any]) -> str:
    """One line for a person: the feature, its state, and what is wrong if anything is."""
    if row["always"]:
        return f"{row['feature']}: always on"
    state = "on" if row["declared"] else "off"
    parts = [f"{row['feature']}: {state}"]
    unmet = [r["detail"] for r in row["requires"] if not r["met"]]
    if row["declared"]:
        if row["applied"] is False:
            parts.append("NOT APPLIED")
        elif row["not_ready"]:
            parts.append("NOT READY: " + ", ".join(row["not_ready"]))
        elif row["applied"]:
            parts.append("applied and ready")
        if unmet:
            parts.append("MISSING: " + "; ".join(unmet))
    elif row["applied"]:
        parts.append("STILL APPLIED")
    if row["pending"]:
        parts.append(f"a converge would {row['pending']} it")
    if row["memory_mib"] is not None:
        parts.append(f"{row['memory_mib']} MiB resident")
    return ", ".join(parts)
