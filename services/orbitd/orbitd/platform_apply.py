"""`orbit platform apply`: the desk writes the platform layer through the applier library.

Runs on the desk with a kubeconfig; never talks to orbitd. Safety gate first: a live
ArgoCD Application with `spec.syncPolicy.automated` means two appliers would force the
same objects. Then `platform.plan` for rendered objects and classes, then server-side
apply with field manager `orbit` and force, in global phases over every selected app: all
CRDs (wait Established), then all Namespaces, then app by app in Helm install order.
`generated` Secrets are created (POST) and never overwritten; `checksum/*` pod-template
annotations and render-time stamps (`RENDER_STAMP_ANNOTATIONS`) are stripped before
apply. Inventory is `orbit-platform-inventory-<app>` in `orbit`, a name no tenant
project/env pair can produce.
"""

from __future__ import annotations

import json
import sys
import time
from collections.abc import Callable, Mapping
from copy import deepcopy
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any, TextIO

from kubernetes.client.exceptions import ApiException

from .applier import (
    DRY_RUN_ALL,
    FIELD_MANAGER,
    INVENTORY_NS,
    INVENTORY_PREFIX,
    ObjectRef,
    ObjectResult,
    RULE_ABSENT,
    RULE_NAMESPACE_KIND,
    RULE_ORBIT_RBAC,
    RULE_PVC,
    RULE_TLS,
    TLS_SECRET_TYPE,
    _delete_options,
    _is_conflict,
    _live_now,
    _reason,
    _server_side_apply,
    is_orbit_rbac,
)
from .backends import KUBE_TIMEOUT, Kube
from .platform import (
    EXIT_RENDER,
    RenderError,
    RenderReadError,
    _generated_secret_names,
    load_applications,
    plan as platform_plan,
    render_application,
    resolve_kubeconfig,
)
from .shadow import RENDER_STAMP_ANNOTATIONS, _CLUSTER_SCOPED, _as_dict, _get_live, _is_not_found

EXIT_OK = 0
EXIT_FAILED = 1
EXIT_GATE = 3

KIND_CRD = "CustomResourceDefinition"
KIND_NS = "Namespace"
KIND_SECRET = "Secret"
KIND_PVC = "PersistentVolumeClaim"
ARGOCD_NS = "argocd"
APPLICATION_GVK = ("argoproj.io/v1alpha1", "Application")
KUBE_SYSTEM = "kube-system"
CONFIRM_PREFIX = "prune "
RETIRE_CONFIRM_PREFIX = "retire "
TWO_APPLIERS = "two appliers forcing the same objects is the failure this prevents"

RULE_CRD = "never a CustomResourceDefinition"
RULE_NS_SHARED = "namespace is still listed by another app's stored inventory"
RULE_CRD_INSTANCES = "objects of this kind still exist"
RULE_KUBE_SYSTEM = "never anything in kube-system"
RULE_GENERATED = "never a generated Secret"
RULE_NOT_INVENTORIED = "not in this app's stored inventory"
RULE_NO_MANAGED_FIELDS = "live copy carries no metadata.managedFields; ownership cannot be proven"
RULE_NOT_OWNED = f"live copy was not applied by field manager {FIELD_MANAGER}"
GENERATED_PRESENT = "generated Secret already present"
RULE_NS_CASCADE = "a Namespace this run deletes would garbage-collect this object"
NOT_ATTEMPTED = "not-attempted"
PLATFORM_INVENTORY_PREFIX = "orbit-platform-inventory-"

Key = tuple[str, str, str]  # ObjectRef.key: kind, namespace, name

# Helm install order (kind_sorter.go) with Namespace and CRD pulled out: those
# two kinds are applied first, in that named sequence, across the selected app.
HELM_INSTALL_ORDER: tuple[str, ...] = (
    "NetworkPolicy", "ResourceQuota", "LimitRange", "PodSecurityPolicy",
    "PodDisruptionBudget", "ServiceAccount", "Secret", "SecretList",
    "ConfigMap", "StorageClass", "PersistentVolume", "PersistentVolumeClaim",
    "ClusterRole", "ClusterRoleList", "ClusterRoleBinding", "ClusterRoleBindingList",
    "Role", "RoleList", "RoleBinding", "RoleBindingList",
    "Service", "DaemonSet", "Pod", "ReplicationController", "ReplicaSet",
    "Deployment", "HorizontalPodAutoscaler", "StatefulSet", "Job", "CronJob",
    "IngressClass", "Ingress", "APIService",
    "Certificate", "ClusterIssuer", "Middleware", "ObjectStore", "Cluster",
    "ScheduledBackup", "Instrumentation", "ExternalSecret",
    "ServiceMonitor", "Probe", "PrometheusRule",
    "ValidatingWebhookConfiguration", "MutatingWebhookConfiguration",
    "PriorityClass",
)
_RANK = {kind: i for i, kind in enumerate(HELM_INSTALL_ORDER)}

CRD_ESTABLISHED_TIMEOUT = 60.0
CRD_ESTABLISHED_POLL = 0.5

_TLS_KEY_PAIRS = (
    frozenset({"tls.crt", "tls.key"}),
    frozenset({"ca.crt", "ca.key"}),
)


def inventory_ref_for_app(app: str) -> ObjectRef:
    """ConfigMap orbit-platform-inventory-<app> in orbit. Tenant inventories are
    orbit-inventory-<project>-<env>, so the two never share a name."""
    return ObjectRef(
        api_version="v1", kind="ConfigMap", namespace=INVENTORY_NS,
        name=f"{PLATFORM_INVENTORY_PREFIX}{app}",
    )


def _desk_state(kube: Any) -> Any:
    """The desk is always allowed to write; ORBITD_APPLY is a daemon switch."""
    return SimpleNamespace(
        cfg=SimpleNamespace(apply_mode="on"),
        backends=SimpleNamespace(kube=kube),
    )


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def automated_sync(application: dict[str, Any] | None) -> bool:
    """True when spec.syncPolicy.automated is set (including `{}`)."""
    if not application:
        return False
    policy = (application.get("spec") or {}).get("syncPolicy") or {}
    auto = policy.get("automated", None)
    return auto is not None and auto is not False


def read_live_application(kube: Any, name: str) -> dict[str, Any] | None:
    stub = {
        "apiVersion": APPLICATION_GVK[0],
        "kind": APPLICATION_GVK[1],
        "metadata": {"name": name, "namespace": ARGOCD_NS},
    }
    got = _get_live(kube.dynamic, stub)
    return _as_dict(got) if got is not None else None


def gate_message(app: str, *, dry_run: bool) -> str:
    where = f"{APPLICATION_GVK[1].lower()}s.argoproj.io/{app} in {ARGOCD_NS}"
    head = (
        f"orbit platform apply: app {app}: {where} has spec.syncPolicy.automated set"
        if dry_run
        else f"orbit platform apply: refusing {app}: {where} has spec.syncPolicy.automated set"
    )
    extra = " (dry-run: not enforced)" if dry_run else ""
    return f"{head}{extra}; {TWO_APPLIERS}"


def check_gate(kube: Any, names: list[str], *, dry_run: bool,
               errors: dict[str, str] | None = None) -> list[str]:
    """Return app names whose live Application has automated sync. An app whose
    Application cannot be read goes into `errors` (when given) instead of raising."""
    blocked: list[str] = []
    for name in names:
        try:
            live = read_live_application(kube, name)
        except Exception as e:
            if errors is None:
                raise
            errors[name] = f"gate: {_reason(e)}"
            continue
        if automated_sync(live):
            blocked.append(name)
    return blocked


def _secret_data_keys(obj: dict[str, Any]) -> set[str]:
    keys: set[str] = set()
    for field in ("data", "stringData"):
        body = obj.get(field) or {}
        if isinstance(body, dict):
            keys.update(str(k) for k in body)
    return keys


def is_generated_secret(obj: dict[str, Any], named: frozenset[str]) -> bool:
    if str(obj.get("kind") or "") != KIND_SECRET:
        return False
    name = str((obj.get("metadata") or {}).get("name") or "")
    if name in named:
        return True
    if str(obj.get("type") or "") == TLS_SECRET_TYPE:
        return True
    keys = _secret_data_keys(obj)
    return any(pair <= keys for pair in _TLS_KEY_PAIRS)


def generated_names(application: dict[str, Any], plan_row: dict[str, Any] | None = None) -> frozenset[str]:
    names = set(_generated_secret_names(application))
    for item in (plan_row or {}).get("generated") or []:
        if isinstance(item, dict) and item.get("kind") == KIND_SECRET and item.get("name"):
            names.add(str(item["name"]))
    return frozenset(names)


def _strip_checksums_in(node: Any) -> None:
    if isinstance(node, dict):
        tmpl = node.get("template")
        if isinstance(tmpl, dict):
            meta = tmpl.get("metadata")
            if isinstance(meta, dict):
                ann = meta.get("annotations")
                if isinstance(ann, dict):
                    for key in [k for k in ann if isinstance(k, str) and k.startswith("checksum/")]:
                        del ann[key]
        for value in node.values():
            _strip_checksums_in(value)
    elif isinstance(node, list):
        for item in node:
            _strip_checksums_in(item)


def strip_checksum_annotations(obj: dict[str, Any]) -> dict[str, Any]:
    """Drop Helm checksum/* keys on pod-template annotations before apply."""
    out = deepcopy(obj)
    _strip_checksums_in(out)
    return out


def _strip_render_stamps_in(node: Any) -> None:
    """Drop RENDER_STAMP_ANNOTATIONS from every metadata.annotations map.

    Top-level and spec.template (and any nested workload template) so a
    re-render cannot change a field the compare already ignores. Omitting the
    key in server-side apply leaves whatever another manager set.
    """
    stamps = RENDER_STAMP_ANNOTATIONS
    if isinstance(node, dict):
        meta = node.get("metadata")
        if isinstance(meta, dict):
            ann = meta.get("annotations")
            if isinstance(ann, dict):
                for key in [k for k in ann if k in stamps]:
                    del ann[key]
        for value in node.values():
            _strip_render_stamps_in(value)
    elif isinstance(node, list):
        for item in node:
            _strip_render_stamps_in(item)


def strip_render_stamp_annotations(obj: dict[str, Any]) -> dict[str, Any]:
    """Drop render-time / restart stamp annotations before apply."""
    out = deepcopy(obj)
    _strip_render_stamps_in(out)
    return out


def _order_key(obj: dict[str, Any]) -> tuple[int, str, str, str]:
    ref = ObjectRef.of(obj)
    return (_RANK.get(ref.kind, len(HELM_INSTALL_ORDER)), ref.kind, ref.namespace, ref.name)


def order_objects(objects: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """CRDs, then Namespaces, then the rest in Helm install order."""
    crds, namespaces, rest = [], [], []
    for obj in objects:
        kind = str(obj.get("kind") or "")
        if kind == KIND_CRD:
            crds.append(obj)
        elif kind == KIND_NS:
            namespaces.append(obj)
        else:
            rest.append(obj)
    crds.sort(key=_order_key)
    namespaces.sort(key=_order_key)
    rest.sort(key=_order_key)
    return crds + namespaces + rest


def _crd_established(obj: dict[str, Any]) -> bool:
    for cond in (obj.get("status") or {}).get("conditions") or []:
        if not isinstance(cond, dict):
            continue
        if str(cond.get("type") or "") == "Established" and str(cond.get("status") or "") == "True":
            return True
    return False


def wait_crd_established(dyn: Any, ref: ObjectRef, *,
                         timeout: float | None = None, poll: float | None = None) -> None:
    deadline = time.monotonic() + (CRD_ESTABLISHED_TIMEOUT if timeout is None else timeout)
    interval = CRD_ESTABLISHED_POLL if poll is None else poll
    while True:
        got = _get_live(dyn, ref.stub())
        live = _as_dict(got) if got is not None else {}
        if _crd_established(live):
            return
        if time.monotonic() >= deadline:
            raise TimeoutError(f"CRD {ref.name} did not report Established")
        time.sleep(max(interval, 0.0))


def _applied_by_orbit(live: dict[str, Any]) -> bool | None:
    """True when metadata.managedFields holds an Apply entry by field manager orbit;
    None when the live copy carries no managedFields at all."""
    entries = (live.get("metadata") or {}).get("managedFields")
    if not isinstance(entries, list) or not entries:
        return None
    return any(
        isinstance(e, dict) and e.get("manager") == FIELD_MANAGER and e.get("operation") == "Apply"
        for e in entries
    )


def platform_prune_rule(app: str, ref: ObjectRef, live: dict[str, Any] | None, *,
                        named_generated: frozenset[str],
                        inventory: Mapping[Key, bool] | None = None) -> str | None:
    """Kind refusals, then positive ownership: the ref is in this app's stored inventory
    (`inventory` maps each stored entry to its generated mark) and the live copy was
    applied by field manager orbit."""
    if ref.kind == KIND_CRD:
        return RULE_CRD
    if ref.namespace == KUBE_SYSTEM or (ref.kind == KIND_NS and ref.name == KUBE_SYSTEM):
        return RULE_KUBE_SYSTEM
    if is_orbit_rbac(ref):
        return RULE_ORBIT_RBAC
    if ref.kind == KIND_NS:
        return RULE_NAMESPACE_KIND
    if ref.kind == KIND_PVC:
        return RULE_PVC
    stored = inventory or {}
    if ref.kind == KIND_SECRET:
        if live is not None and str(live.get("type") or "") == TLS_SECRET_TYPE:
            return RULE_TLS
        if (stored.get(ref.key) or ref.name in named_generated
                or (live is not None and is_generated_secret(live, named_generated))):
            return RULE_GENERATED
    if ref.key not in stored:
        return RULE_NOT_INVENTORIED
    if live is None:
        return RULE_ABSENT
    owned = _applied_by_orbit(live)
    if owned is None:
        return RULE_NO_MANAGED_FIELDS
    if not owned:
        return RULE_NOT_OWNED
    return None


def _prune_order_key(ref: ObjectRef) -> tuple[int, str, str, str]:
    return (-_RANK.get(ref.kind, len(HELM_INSTALL_ORDER)), ref.kind, ref.namespace, ref.name)


def confirm_prune(app: str, refs: list[ObjectRef], stdin: TextIO, stderr: TextIO) -> bool:
    print(f"prune set for {app}:", file=stderr)
    for ref in refs:
        ns = ref.namespace or "(cluster)"
        print(f"  {ref.kind} {ns}/{ref.name}", file=stderr)
    print(f"type '{CONFIRM_PREFIX}{app}' to delete:", file=stderr, flush=True)
    line = stdin.readline()
    return line.strip() == f"{CONFIRM_PREFIX}{app}"


def _result_dump(item: ObjectResult) -> dict[str, Any]:
    return item.model_dump(mode="json")


def _create_only(dyn: Any, obj: dict[str, Any], ref: ObjectRef, *, dry: bool) -> Any:
    """POST: the server refuses an existing object (409), so nothing is ever overwritten."""
    resource = dyn.resources.get(api_version=ref.api_version or "v1", kind=ref.kind)
    kwargs: dict[str, Any] = {
        "body": obj, "field_manager": FIELD_MANAGER, "_request_timeout": KUBE_TIMEOUT,
    }
    if ref.namespace:
        kwargs["namespace"] = ref.namespace
    if dry:
        kwargs["dry_run"] = DRY_RUN_ALL
    return resource.create(**kwargs)


def apply_objects(
    kube: Any,
    objects: list[dict[str, Any]],
    *,
    dry_run: bool,
    named_generated: frozenset[str],
) -> list[ObjectResult]:
    """Server-side apply via applier._server_side_apply (field manager orbit, force);
    generated Secrets are created, and one already present is left as it is."""
    dyn = kube.dynamic
    results: list[ObjectResult] = []
    ordered = order_objects(objects)
    for obj in ordered:
        ref = ObjectRef.of(obj)
        if not ref.name:
            results.append(ObjectResult(ref=ref, result="failed", reason="object has no metadata.name"))
            return results
        body = strip_render_stamp_annotations(strip_checksum_annotations(obj))
        try:
            if is_generated_secret(obj, named_generated):
                try:
                    _create_only(dyn, body, ref, dry=dry_run)
                except Exception as e:
                    if not _is_conflict(e):
                        raise
                    results.append(ObjectResult(ref=ref, result="skipped", reason=GENERATED_PRESENT))
                    continue
            else:
                _server_side_apply(dyn, body, ref, dry=dry_run)
        except Exception as e:
            results.append(ObjectResult(ref=ref, result="failed", reason=_reason(e)))
            return results
        if ref.kind == KIND_CRD and not dry_run:
            try:
                wait_crd_established(dyn, ref)
            except Exception as e:
                results.append(ObjectResult(ref=ref, result="failed", reason=_reason(e)))
                return results
        results.append(ObjectResult(ref=ref, result="applied"))
    return results


def _inventory_lines(text: Any) -> list[ObjectRef]:
    refs: list[ObjectRef] = []
    for line in str(text or "").splitlines():
        if not line.strip():
            continue
        try:
            refs.append(ObjectRef.from_line(line))
        except ValueError:
            continue  # unreadable entry: never pruned
    return refs


def read_app_inventory(kube: Any, app: str) -> tuple[list[ObjectRef], frozenset[Key]]:
    """Stored entries and the keys of those marked generated. Missing = empty = no prune."""
    got = _get_live(kube.dynamic, inventory_ref_for_app(app).stub())
    if got is None:
        return [], frozenset()
    data = _as_dict(got).get("data") or {}
    generated = frozenset(r.key for r in _inventory_lines(data.get("generated")))
    return _inventory_lines(data.get("objects")), generated


def inventory_entries(rendered: list[ObjectRef], previous: list[ObjectRef],
                      deleted: set[Key]) -> list[ObjectRef]:
    """This run's rendered refs plus every previous entry this run did not delete."""
    by_key = {r.key: r for r in previous if r.key not in deleted}
    by_key.update({r.key: r for r in rendered})
    return list(by_key.values())


def write_app_inventory(kube: Any, app: str, refs: list[ObjectRef],
                        generated: frozenset[Key] = frozenset()) -> ObjectRef:
    ref = inventory_ref_for_app(app)
    body = {
        "apiVersion": "v1",
        "kind": "ConfigMap",
        "metadata": {
            "name": ref.name,
            "namespace": ref.namespace,
            "labels": {
                "app.kubernetes.io/managed-by": FIELD_MANAGER,
                "orbit.local/app": app,
            },
        },
        "data": {
            "objects": "\n".join(sorted(r.line for r in refs)),
            "generated": "\n".join(sorted(r.line for r in refs if r.key in generated)),
            "applied_at": _now(),
        },
    }
    _server_side_apply(kube.dynamic, body, ref, dry=False)
    return ref


def prune_from_previous(
    kube: Any, app: str, previous: list[ObjectRef], rendered: list[ObjectRef], *,
    named_generated: frozenset[str], inventory: Mapping[Key, bool] | None = None,
) -> tuple[list[ObjectRef], list[ObjectResult]]:
    rendered_keys = {r.key for r in rendered}
    dyn = kube.dynamic
    prune: list[ObjectRef] = []
    withheld: list[ObjectResult] = []
    for ref in previous:
        if ref.key in rendered_keys:
            continue
        live = _live_now(dyn, ref)
        rule = platform_prune_rule(app, ref, live, named_generated=named_generated,
                                   inventory=inventory)
        if rule is None:
            prune.append(ref)
        else:
            withheld.append(ObjectResult(ref=ref, result="withheld", reason=rule))
    prune.sort(key=_prune_order_key)
    return prune, withheld


def prune_objects(kube: Any, refs: list[ObjectRef], *, dry_run: bool,
                  app: str, named_generated: frozenset[str],
                  inventory: Mapping[Key, bool] | None = None) -> list[ObjectResult]:
    """Delete the prune set; re-read and re-run the hard rules like applier.prune."""
    dyn = kube.dynamic
    results: list[ObjectResult] = []
    for ref in refs:
        fresh = _live_now(dyn, ref)
        rule = platform_prune_rule(app, ref, fresh, named_generated=named_generated,
                                   inventory=inventory)
        if rule == RULE_ABSENT or fresh is None:
            results.append(ObjectResult(ref=ref, result="absent"))
            continue
        if rule is not None:
            results.append(ObjectResult(ref=ref, result="withheld", reason=rule))
            continue
        try:
            resource = dyn.resources.get(api_version=ref.api_version or "v1", kind=ref.kind)
            kwargs: dict[str, Any] = {
                "name": ref.name, "_request_timeout": KUBE_TIMEOUT,
                "body": _delete_options(fresh, dry=dry_run),
            }
            if ref.kind != KIND_NS and ref.namespace:
                kwargs["namespace"] = ref.namespace
            resource.delete(**kwargs)
        except Exception as e:
            if _is_not_found(e):
                results.append(ObjectResult(ref=ref, result="absent"))
                continue
            results.append(ObjectResult(ref=ref, result="failed", reason=_reason(e)))
            return results
        results.append(ObjectResult(ref=ref, result="deleted"))
    return results


# ------------------------------------------------------------------ retire

def _retire_order_key(ref: ObjectRef) -> tuple[int, int, str, str, str]:
    """Reverse install order for the rest, then Namespaces, then CRDs last."""
    if ref.kind == KIND_CRD:
        group = 2
    elif ref.kind == KIND_NS:
        group = 1
    else:
        group = 0
    return (group, -_RANK.get(ref.kind, len(HELM_INSTALL_ORDER)),
            ref.kind, ref.namespace, ref.name)


def confirm_retire(app: str, refs: list[ObjectRef], stdin: TextIO, stderr: TextIO,
                   collateral: list[ObjectRef] | None = None) -> bool:
    print(f"retire set for {app}:", file=stderr)
    for ref in refs:
        ns = ref.namespace or "(cluster)"
        print(f"  {ref.kind} {ns}/{ref.name}", file=stderr)
    extra = list(collateral or [])
    if extra:
        print(f"a Namespace delete removes everything in it, listed or not; this run takes:", file=stderr)
        for ref in extra:
            ns = ref.namespace or "(cluster)"
            print(f"  {ref.kind} {ns}/{ref.name}", file=stderr)
    print(f"type '{RETIRE_CONFIRM_PREFIX}{app}' to delete:", file=stderr, flush=True)
    line = stdin.readline()
    return line.strip() == f"{RETIRE_CONFIRM_PREFIX}{app}"


def _other_inventory_refs(dyn: Any, app: str) -> list[ObjectRef]:
    """Every stored object ref from every inventory but this app's own: platform
    (`orbit-platform-inventory-*`) and tenant (`orbit-inventory-*`) alike. A
    tenant's objects keep a Namespace alive just as a platform app's do."""
    try:
        resource = dyn.resources.get(api_version="v1", kind="ConfigMap")
        listed = resource.get(namespace=INVENTORY_NS, _request_timeout=KUBE_TIMEOUT)
    except Exception as e:
        if _is_not_found(e):
            return []
        raise
    items = listed.get("items") if isinstance(listed, dict) else getattr(listed, "items", None)
    refs: list[ObjectRef] = []
    for item in items or []:
        doc = _as_dict(item)
        meta = doc.get("metadata") or {}
        name = str(meta.get("name") or "")
        if name == f"{PLATFORM_INVENTORY_PREFIX}{app}":
            continue
        if not (name.startswith(PLATFORM_INVENTORY_PREFIX) or name.startswith(INVENTORY_PREFIX)):
            continue
        data = doc.get("data") or {}
        refs.extend(_inventory_lines(data.get("objects")))
    return refs


def _other_inventory_namespaces(dyn: Any, app: str) -> set[str]:
    """Namespaces any other app's stored inventory still lists an object in."""
    return {ref.namespace for ref in _other_inventory_refs(dyn, app) if ref.namespace}


def _crd_has_live_instances(dyn: Any, live: dict[str, Any]) -> bool:
    """True when any object of the CRD's kind exists. A kind that cannot be
    determined counts as present: deleting the CRD then cannot be proven safe."""
    spec = live.get("spec")
    if not isinstance(spec, dict):
        return True
    group = str(spec.get("group") or "")
    names = spec.get("names")
    kind = str(names.get("kind") or "") if isinstance(names, dict) else ""
    if not group or not kind:
        return True
    versions: list[str] = []
    raw_versions = spec.get("versions")
    if isinstance(raw_versions, list):
        for entry in raw_versions:
            if isinstance(entry, dict) and entry.get("name"):
                text = str(entry["name"])
                if text not in versions:
                    versions.append(text)
    single = spec.get("version")
    if isinstance(single, str) and single and single not in versions:
        versions.append(single)
    if not versions:
        return True
    for version in versions:
        try:
            resource = dyn.resources.get(api_version=f"{group}/{version}", kind=kind)
            listed = resource.get(_request_timeout=KUBE_TIMEOUT)
        except Exception as e:
            if _is_not_found(e):
                continue
            raise
        items = listed.get("items") if isinstance(listed, dict) else getattr(listed, "items", None)
        if items:
            return True
    return False


def platform_retire_rule(
    app: str, ref: ObjectRef, live: dict[str, Any] | None, dyn: Any, *,
    named_generated: frozenset[str] = frozenset(),
    inventory: Mapping[Key, bool] | None = None,
    shared_namespaces: frozenset[str] | set[str] = frozenset(),
) -> str | None:
    """Retire refusals: the prune kind rules minus Namespace, CRD and PVC (a retired
    app may delete those), plus a Namespace another stored inventory still uses and
    a CRD while any object of its kind exists. PVCs go with their Namespace."""
    if ref.namespace == KUBE_SYSTEM or (ref.kind == KIND_NS and ref.name == KUBE_SYSTEM):
        return RULE_KUBE_SYSTEM
    if is_orbit_rbac(ref):
        return RULE_ORBIT_RBAC
    stored = inventory or {}
    if ref.kind == KIND_SECRET:
        if live is not None and str(live.get("type") or "") == TLS_SECRET_TYPE:
            return RULE_TLS
        if (stored.get(ref.key) or ref.name in named_generated
                or (live is not None and is_generated_secret(live, named_generated))):
            return RULE_GENERATED
    if ref.key not in stored:
        return RULE_NOT_INVENTORIED
    if live is None:
        return RULE_ABSENT
    owned = _applied_by_orbit(live)
    if owned is None:
        return RULE_NO_MANAGED_FIELDS
    if not owned:
        return RULE_NOT_OWNED
    if ref.kind == KIND_NS and ref.name in shared_namespaces:
        return RULE_NS_SHARED
    if ref.kind == KIND_CRD and _crd_has_live_instances(dyn, live):
        return RULE_CRD_INSTANCES
    return None


def plan_retire(
    kube: Any, app: str, previous: list[ObjectRef], generated: frozenset[Key],
) -> tuple[list[ObjectRef], list[ObjectResult], dict[Key, bool], set[str]]:
    """Split the stored inventory into the ordered delete set and the rows for
    everything else (withheld with its rule, absent for what is already gone)."""
    dyn = kube.dynamic
    stored = {r.key: r.key in generated for r in previous}
    shared = _other_inventory_namespaces(dyn, app)
    deletes: list[ObjectRef] = []
    pending: list[ObjectResult] = []
    for ref in previous:
        # an object whose live read or rule check cannot be completed is never deleted
        # on the strength of the failure: it gets a failed row and stays out of the set
        try:
            live = _live_now(dyn, ref)
        except Exception as e:
            pending.append(ObjectResult(ref=ref, result="failed", reason=_reason(e)))
            continue
        try:
            rule = platform_retire_rule(
                app, ref, live, dyn, named_generated=frozenset(),
                inventory=stored, shared_namespaces=shared,
            )
        except Exception as e:
            pending.append(ObjectResult(ref=ref, result="failed", reason=_reason(e)))
            continue
        if rule is None:
            deletes.append(ref)
        elif rule == RULE_ABSENT:
            pending.append(ObjectResult(ref=ref, result="absent"))
        else:
            pending.append(ObjectResult(ref=ref, result="withheld", reason=rule))
    deletes.sort(key=_retire_order_key)
    return deletes, pending, stored, shared


def check_retire_collectors(kube: Any, deletes: list[ObjectRef]) -> list[str]:
    """namespace/name of every live Secret outside the Namespaces this run deletes
    that carries an ownerReference to an object this run deletes. Names only:
    a Secret's data never reaches the message. Secrets this run deletes itself
    are accounted for in its own rows, not collected here."""
    dyn = kube.dynamic
    delete_namespaces = {r.name for r in deletes if r.kind == KIND_NS}
    delete_set = {(r.kind, r.namespace, r.name) for r in deletes}
    namespaced_keys = {(r.kind, r.namespace, r.name) for r in deletes
                       if r.kind not in _CLUSTER_SCOPED}
    cluster_keys = {(r.kind, r.name) for r in deletes if r.kind in _CLUSTER_SCOPED}
    delete_uids: set[str] = set()
    for ref in deletes:
        try:
            live = _live_now(dyn, ref)
        except Exception:
            continue
        if live is None:
            continue
        uid = str((live.get("metadata") or {}).get("uid") or "")
        if uid:
            delete_uids.add(uid)
    try:
        resource = dyn.resources.get(api_version="v1", kind=KIND_SECRET)
        listed = resource.get(_request_timeout=KUBE_TIMEOUT)
    except Exception as e:
        if _is_not_found(e):
            return []
        raise
    items = listed.get("items") if isinstance(listed, dict) else getattr(listed, "items", None)
    collected: list[str] = []
    for item in items or []:
        doc = _as_dict(item)
        meta = doc.get("metadata") or {}
        namespace, name = str(meta.get("namespace") or ""), str(meta.get("name") or "")
        if not name or namespace in delete_namespaces:
            continue
        if (KIND_SECRET, namespace, name) in delete_set:
            continue
        refs = meta.get("ownerReferences")
        if not isinstance(refs, list):
            continue
        for owner in refs:
            if not isinstance(owner, dict):
                continue
            uid = str(owner.get("uid") or "")
            if uid and uid in delete_uids:
                collected.append(f"{namespace}/{name}")
                break
            kind, oname = str(owner.get("kind") or ""), str(owner.get("name") or "")
            if not kind or not oname:
                continue
            if (kind, namespace, oname) in namespaced_keys or (kind, oname) in cluster_keys:
                collected.append(f"{namespace}/{name}")
                break
    return sorted(collected)


# Namespaced kinds a stranger object plausibly is: anything orbit ever applied
# (taken from the stored inventories at the call site) plus these. Deleting a
# Namespace garbage-collects every one of them, whatever the retire rules said
# about the rows this run planned.
_CASCADE_CORE_KINDS: tuple[tuple[str, str], ...] = (
    ("v1", "ConfigMap"),
    ("v1", "Secret"),
    ("v1", "Service"),
    ("v1", "ServiceAccount"),
    ("v1", "Endpoints"),
    ("v1", "PersistentVolumeClaim"),
    ("apps/v1", "Deployment"),
    ("apps/v1", "StatefulSet"),
    ("apps/v1", "DaemonSet"),
    ("apps/v1", "ReplicaSet"),
    ("batch/v1", "Job"),
    ("batch/v1", "CronJob"),
    ("networking.k8s.io/v1", "Ingress"),
    ("networking.k8s.io/v1", "NetworkPolicy"),
    ("rbac.authorization.k8s.io/v1", "Role"),
    ("rbac.authorization.k8s.io/v1", "RoleBinding"),
)


def _cascade_candidate_kinds(refs: list[ObjectRef]) -> list[tuple[str, str]]:
    """(api_version, kind) pairs to look for inside a Namespace this run deletes:
    every kind any stored inventory ever listed, plus the core kinds."""
    kinds: dict[tuple[str, str], None] = {}
    for ref in refs:
        if not ref.kind or ref.kind == KIND_NS or ref.kind in _CLUSTER_SCOPED:
            continue
        kinds.setdefault((ref.api_version or "v1", ref.kind), None)
    for pair in _CASCADE_CORE_KINDS:
        kinds.setdefault(pair, None)
    return sorted(kinds)


def _is_cluster_furniture(ref: ObjectRef, live: dict[str, Any] | None) -> bool:
    """Kubernetes' own objects in every Namespace: never a reason to refuse,
    never reported. The `default` ServiceAccount, the `kube-root-ca.crt`
    ConfigMap, and the token Secrets the cluster mints for them."""
    if ref.kind == "ServiceAccount" and ref.name == "default":
        return True
    if ref.kind == "ConfigMap" and ref.name == "kube-root-ca.crt":
        return True
    if ref.kind == KIND_SECRET:
        doc = live or {}
        if str(doc.get("type") or "") == "kubernetes.io/service-account-token":
            return True
        meta = doc.get("metadata") if isinstance(doc.get("metadata"), dict) else {}
        ann = meta.get("annotations") if isinstance(meta, dict) else {}
        if isinstance(ann, dict) and ann.get("kubernetes.io/service-account.name") == "default":
            return True
        if ref.name.startswith("default-token-"):
            return True
    return False


def _namespace_cascade(
    kube: Any, app: str, deletes: list[ObjectRef], known: list[ObjectRef], *,
    other_keys: set[Key] | frozenset[Key] | None = None,
) -> tuple[list[ObjectRef], list[ObjectRef], str | None]:
    """What a Namespace delete takes with it.

    Returns (blocking, collateral, list_error). `blocking` is every live
    object inside a Namespace this run deletes that another stored inventory
    (platform or tenant) still lists: the run must refuse. `collateral` is
    everything else the Namespace delete removes that this run does not
    itself delete — a stranger orbit never applied, or this app's own object
    the rules withheld: the operator sees it in the confirmation and the
    report records it as deleted, so the stored inventory never claims it
    survived. The cluster's own furniture (`default` ServiceAccount,
    `kube-root-ca.crt` ConfigMap, ServiceAccount token Secrets) is noise,
    not news: never blocking, never reported.

    A fixed list of kinds cannot prove a Namespace holds nothing else, and
    this function does not claim to: a Namespace delete removes everything
    in it, listed or not. `list_error` names a kind that could not be
    listed: without that list the run cannot say what the Namespace takes,
    so the caller must refuse. The retiring app's own inventory is managed
    explicitly and never counts."""
    dyn = kube.dynamic
    doomed = sorted({r.name for r in deletes if r.kind == KIND_NS})
    if not doomed:
        return [], [], None
    delete_keys = {r.key for r in deletes}
    other = set(other_keys) if other_keys is not None else set()
    own_inventory = inventory_ref_for_app(app).name
    blocking: list[ObjectRef] = []
    collateral: list[ObjectRef] = []
    for namespace in doomed:
        for api_version, kind in _cascade_candidate_kinds(known):
            try:
                resource = dyn.resources.get(api_version=api_version, kind=kind)
                listed = resource.get(namespace=namespace, _request_timeout=KUBE_TIMEOUT)
            except Exception as e:
                if _is_not_found(e):
                    continue
                return blocking, collateral, f"{kind}.{api_version} in {namespace}: {_reason(e)}"
            items = listed.get("items") if isinstance(listed, dict) else getattr(listed, "items", None)
            for item in items or []:
                doc = _as_dict(item)
                ref = ObjectRef.of(doc)
                if not ref.name or ref.namespace != namespace or ref.key in delete_keys:
                    continue
                if ref.kind == "ConfigMap" and ref.namespace == INVENTORY_NS and ref.name == own_inventory:
                    continue
                if _is_cluster_furniture(ref, doc):
                    continue
                if ref.key in other:
                    blocking.append(ref)
                else:
                    collateral.append(ref)
    blocking.sort(key=lambda r: (r.namespace, r.kind, r.name))
    collateral.sort(key=lambda r: (r.namespace, r.kind, r.name))
    return blocking, collateral, None


def retire_objects(
    kube: Any, refs: list[ObjectRef], *, dry_run: bool, app: str,
    inventory: Mapping[Key, bool] | None = None,
    shared_namespaces: frozenset[str] | set[str] = frozenset(),
) -> list[ObjectResult]:
    """Delete the retire set; re-read and re-run the hard rules like prune_objects.
    An object already gone is reported absent, not an error. The first failed
    delete stops the run, and every object the run then never reaches gets a
    not-attempted row: the report names the whole set or it names nothing."""
    dyn = kube.dynamic
    results: list[ObjectResult] = []

    def _fail_and_stop(failed_ref: ObjectRef, reason: str, at: int) -> list[ObjectResult]:
        results.append(ObjectResult(ref=failed_ref, result="failed", reason=reason))
        where = failed_ref.namespace or "(cluster)"
        for rest in refs[at + 1:]:
            results.append(ObjectResult(
                ref=rest, result=NOT_ATTEMPTED,
                reason=f"not attempted: {failed_ref.kind} {where}/{failed_ref.name} failed first",
            ))
        return results

    for index, ref in enumerate(refs):
        try:
            fresh = _live_now(dyn, ref)
        except Exception as e:
            return _fail_and_stop(ref, _reason(e), index)
        try:
            rule = platform_retire_rule(
                app, ref, fresh, dyn, named_generated=frozenset(),
                inventory=inventory, shared_namespaces=shared_namespaces,
            )
        except Exception as e:
            return _fail_and_stop(ref, _reason(e), index)
        if rule == RULE_ABSENT or fresh is None:
            results.append(ObjectResult(ref=ref, result="absent"))
            continue
        if rule is not None:
            results.append(ObjectResult(ref=ref, result="withheld", reason=rule))
            continue
        try:
            resource = dyn.resources.get(api_version=ref.api_version or "v1", kind=ref.kind)
            kwargs: dict[str, Any] = {
                "name": ref.name, "_request_timeout": KUBE_TIMEOUT,
                "body": _delete_options(fresh, dry=dry_run),
            }
            if ref.kind != KIND_NS and ref.namespace:
                kwargs["namespace"] = ref.namespace
            resource.delete(**kwargs)
        except Exception as e:
            if _is_not_found(e):
                results.append(ObjectResult(ref=ref, result="absent"))
                continue
            results.append(ObjectResult(ref=ref, result="failed", reason=_reason(e)))
            where = ref.namespace or "(cluster)"
            for rest in refs[index + 1:]:
                results.append(ObjectResult(
                    ref=rest, result=NOT_ATTEMPTED,
                    reason=f"not attempted: {ref.kind} {where}/{ref.name} failed first",
                ))
            return results
        results.append(ObjectResult(ref=ref, result="deleted"))
    return results


def _delete_app_inventory(kube: Any, app: str) -> None:
    ref = inventory_ref_for_app(app)
    live = _get_live(kube.dynamic, ref.stub())
    if live is None:
        return
    try:
        resource = kube.dynamic.resources.get(api_version="v1", kind="ConfigMap")
        resource.delete(
            name=ref.name, namespace=ref.namespace,
            body=_delete_options(_as_dict(live), dry=False),
            _request_timeout=KUBE_TIMEOUT,
        )
    except Exception as e:
        if _is_not_found(e):
            return
        raise


def finish_retire_inventory(
    kube: Any, app: str, previous: list[ObjectRef], generated: frozenset[Key],
    results: list[ObjectResult],
) -> str | None:
    """A fully retired app loses its inventory ConfigMap; otherwise the inventory
    is rewritten to hold only what is left, so a later run can finish it."""
    gone = {r.ref.key for r in results if r.result in ("deleted", "absent")}
    left = [r for r in previous if r.key not in gone]
    if not left:
        _delete_app_inventory(kube, app)
        return None
    kept_generated = frozenset(k for k in generated if k in {r.key for r in left})
    return write_app_inventory(kube, app, left, kept_generated).line


def _generated_with(instance_dir: Path, app: str) -> frozenset[str]:
    """The registry's `generated` Secrets of the features that own `app`; none when the
    instance has no registry or it cannot be read (then nothing is let through)."""
    try:
        from . import orbit_features
        return orbit_features.generated_with(app, orbit_features.load_registry(instance_dir))
    except Exception:
        return frozenset()


def run_retire(
    instance_dir: Path,
    kube: Any,
    *,
    app: str,
    dry_run: bool = False,
    stdin: TextIO | None = None,
    stderr: TextIO | None = None,
) -> tuple[int, dict[str, Any] | None]:
    """Delete what a platform app applied once the instance no longer renders it.
    Returns (exit, report); whole-run refusals return no report, only a stderr
    line — except a dry run that finds a Namespace cascade, which returns the
    report so the operator sees every object the Namespace would take."""
    in_stream = sys.stdin if stdin is None else stdin
    err = sys.stderr if stderr is None else stderr
    try:
        rendered = load_applications(instance_dir, app)
    except RenderReadError as e:
        print(
            f"orbit platform retire: refusing {app}: cannot read the rendered apps "
            f"({e.app}: {e.message}); cannot prove {app} is no longer rendered",
            file=err,
        )
        return EXIT_FAILED, None
    except RenderError:
        rendered = []
    if rendered:
        print(
            f"orbit platform retire: refusing {app}: instance still renders app {app}; "
            f"use `apply --prune` to delete what it no longer renders",
            file=err,
        )
        return EXIT_FAILED, None
    gate_errors: dict[str, str] = {}
    blocked = check_gate(kube, [app], dry_run=dry_run, errors=gate_errors)
    if gate_errors:
        detail = gate_errors.get(app) or "; ".join(f"{name}: {why}" for name, why in gate_errors.items())
        print(
            f"orbit platform retire: refusing {app}: the deploy gate could not be "
            f"read: {detail}",
            file=err,
        )
        return EXIT_FAILED, None
    gate_reported = False
    if blocked:
        if not dry_run:
            print(gate_message(app, dry_run=False), file=err)
            return EXIT_FAILED, None
        print(gate_message(app, dry_run=True), file=err)
        gate_reported = True
    previous, previous_generated = read_app_inventory(kube, app)
    if not previous:
        print(
            f"orbit platform retire: refusing {app}: no stored inventory for app {app}",
            file=err,
        )
        return EXIT_FAILED, None
    try:
        deletes, pending, stored, shared = plan_retire(kube, app, previous, previous_generated)
    except Exception as e:
        print(
            f"orbit platform retire: refusing {app}: could not plan the retire: {_reason(e)}",
            file=err,
        )
        return EXIT_FAILED, None
    plan_failed = [p for p in pending if p.result == "failed"]
    if plan_failed:
        # an object the plan could not read or judge stops the run here: past this
        # point the Namespace cascade would fold its row into the doomed set and
        # report it deleted
        row = _new_row(app, dry_run)
        row["results"] = [_result_dump(r) for r in pending] + [
            _result_dump(ObjectResult(ref=r, result=NOT_ATTEMPTED,
                                      reason="not attempted: the plan could not read every object"))
            for r in deletes
        ]
        row["error"] = plan_failed[0].reason or "retire plan failed"
        row["ok"] = False
        print(
            f"orbit platform retire: refusing {app}: {len(plan_failed)} object(s) could not be "
            f"read or judged ({plan_failed[0].reason})",
            file=err,
        )
        return EXIT_FAILED, {"ok": False, "dry_run": dry_run, "apps": [row]}
    try:
        collected = check_retire_collectors(kube, deletes)
    except Exception as e:
        print(
            f"orbit platform retire: refusing {app}: could not check what this run would collect: {_reason(e)}",
            file=err,
        )
        return EXIT_FAILED, None
    # What the app's own software made for itself goes with it, when the registry names it
    # (a feature's `generated`). The database operator's two certificates are owned by its
    # Deployment, so every retire of it was refused, and the databases feature could not be
    # turned off at all (docs/V1.md §8). Named Secrets only; any other is still a refusal.
    theirs = _generated_with(instance_dir, app)
    going = [ref for ref in collected if ref in theirs]
    collected = [ref for ref in collected if ref not in theirs]
    if going:
        print(f"orbit platform retire: {app}: also going, made by the app for itself: "
              + ", ".join(f"Secret {ref}" for ref in going), file=err)
    if collected:
        names = ", ".join(f"Secret {ref}" for ref in collected)
        print(
            f"orbit platform retire: refusing {app}: deleting this set would "
            f"garbage-collect {names} outside the Namespaces this run deletes",
            file=err,
        )
        return EXIT_FAILED, None
    try:
        other_refs = _other_inventory_refs(kube.dynamic, app)
    except Exception as e:
        print(
            f"orbit platform retire: refusing {app}: could not read other inventories: {_reason(e)}",
            file=err,
        )
        return EXIT_FAILED, None
    other_keys = {r.key for r in other_refs}
    blocking, collateral, list_error = _namespace_cascade(
        kube, app, deletes, previous + other_refs, other_keys=other_keys,
    )
    if list_error is not None:
        print(
            f"orbit platform retire: refusing {app}: cannot list {list_error}; "
            f"cannot say what a deleted Namespace would take with it; "
            f"a Namespace delete removes everything in it, listed or not",
            file=err,
        )
        return EXIT_FAILED, None
    if blocking:
        taken = ", ".join(f"{r.kind} {r.namespace}/{r.name}" for r in blocking)
        message = (
            f"orbit platform retire: refusing {app}: deleting this set would "
            f"garbage-collect {taken} this run does not delete"
        )
        print(message, file=err)
        if not dry_run:
            return EXIT_FAILED, None
        row = _new_row(app, dry_run)
        if gate_reported:
            row["gate"] = "reported"
        done = retire_objects(
            kube, deletes, dry_run=True, app=app,
            inventory=stored, shared_namespaces=shared,
        )
        pending_keys = {p.ref.key for p in pending}
        extra = [r for r in collateral if r.key not in pending_keys]
        row["results"] = [_result_dump(r) for r in done + pending] + [
            _result_dump(ObjectResult(ref=r, result="withheld", reason=RULE_NS_CASCADE))
            for r in blocking + extra
        ]
        row["error"] = message
        row["ok"] = False
        return EXIT_FAILED, {"ok": False, "dry_run": dry_run, "apps": [row]}
    collateral_keys = {r.key for r in collateral}
    pending_by_key = {p.ref.key: p for p in pending}
    stranger_collateral = [r for r in collateral if r.key not in pending_by_key]
    row = _new_row(app, dry_run)
    if gate_reported:
        row["gate"] = "reported"
    if deletes and not dry_run:
        if not confirm_retire(app, deletes, in_stream, err, collateral=collateral):
            row["results"] = [_result_dump(r) for r in pending] + [
                _result_dump(ObjectResult(ref=r, result="withheld", reason="confirmation required"))
                for r in deletes
            ] + [
                _result_dump(ObjectResult(ref=r, result="withheld", reason="confirmation required"))
                for r in stranger_collateral
            ]
            row["error"] = f"retire {app}: confirmation was not '{RETIRE_CONFIRM_PREFIX}{app}'"
            return EXIT_FAILED, {"ok": False, "dry_run": dry_run, "apps": [row]}
    done = retire_objects(
        kube, deletes, dry_run=dry_run, app=app,
        inventory=stored, shared_namespaces=shared,
    )
    done_by_key = {r.ref.key: r for r in done}
    doomed_ns_result: dict[str, str] = {}
    for ref in deletes:
        if ref.kind == KIND_NS:
            got = done_by_key.get(ref.key)
            if got is not None:
                doomed_ns_result[ref.name] = got.result
    if dry_run:
        pending_kept = [p for p in pending if p.ref.key not in collateral_keys]
        collateral_rows = [ObjectResult(ref=r, result="deleted") for r in collateral]
        combined = done + pending_kept + collateral_rows
        row["results"] = [_result_dump(r) for r in combined]
        failed = next((r for r in combined if r.result == "failed"), None)
    else:
        pending_kept = list(pending)
        collateral_rows: list[ObjectResult] = []
        collateral_taken_keys: set[Key] = set()
        for ref in collateral:
            ns_result = doomed_ns_result.get(ref.namespace)
            if ns_result in ("deleted", "absent"):
                collateral_rows.append(ObjectResult(ref=ref, result="deleted"))
                collateral_taken_keys.add(ref.key)
            elif ns_result in ("failed", NOT_ATTEMPTED, "withheld"):
                if ref.key not in pending_by_key:
                    collateral_rows.append(ObjectResult(
                        ref=ref, result=NOT_ATTEMPTED,
                        reason=f"not attempted: Namespace {ref.namespace} was not deleted",
                    ))
        pending_kept = [p for p in pending if p.ref.key not in collateral_taken_keys]
        combined = done + pending_kept + collateral_rows
        row["results"] = [_result_dump(r) for r in combined]
        failed = next((r for r in combined if r.result == "failed"), None)
    if failed is not None:
        row["error"] = failed.reason or "retire failed"
    ok = failed is None
    if not dry_run:
        try:
            row["inventory"] = finish_retire_inventory(
                kube, app, previous, previous_generated, combined,
            )
        except Exception as e:
            row["error"] = f"inventory: {_reason(e)}"
            ok = False
    row["ok"] = ok
    code = EXIT_OK if ok else EXIT_FAILED
    return code, {"ok": ok, "dry_run": dry_run, "apps": [row]}


def cmd_retire(args: Any, *, stdin: TextIO | None = None) -> int:
    instance_dir = Path(args.instance_dir)
    if not instance_dir.is_dir():
        print(f"orbit platform retire: {instance_dir} is not a directory", file=sys.stderr)
        return EXIT_RENDER
    kube = (
        args.kube if getattr(args, "kube", None) is not None
        else Kube(resolve_kubeconfig(getattr(args, "kubeconfig", None)))
    )
    code, body = run_retire(
        instance_dir, kube,
        app=args.app,
        dry_run=bool(getattr(args, "dry_run", False)),
        stdin=stdin,
    )
    if body is not None:
        from .client import write_json_document
        write_json_document(body, sort_keys=True)
    return code


def add_retire_arguments(parser: Any) -> None:
    parser.add_argument("instance_dir", help="instance checkout containing clusters/platform/apps")
    parser.add_argument("--app", metavar="NAME", required=True, help="retire this Application metadata.name")
    parser.add_argument(
        "--kubeconfig",
        help="kubeconfig path; ORBITD_KUBECONFIG, then KUBECONFIG, then in-cluster",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="print what would be deleted; delete nothing, no confirmation",
    )


@dataclass
class AppRun:
    """One selected app across the global phases; `row` is its report entry."""

    application: dict[str, Any]
    name: str
    row: dict[str, Any]
    objects: list[dict[str, Any]] = field(default_factory=list)
    refs: list[ObjectRef] = field(default_factory=list)
    named: frozenset[str] = frozenset()
    generated: frozenset[Key] = frozenset()
    previous: list[ObjectRef] = field(default_factory=list)
    deleted: set[Key] = field(default_factory=set)
    failed: bool = False

    def fail(self, error: str) -> None:
        self.failed = True
        prior = self.row.get("error")
        self.row["error"] = f"{prior}; {error}" if prior else error


def _new_row(name: str, dry_run: bool) -> dict[str, Any]:
    return {
        "name": name, "ok": False, "dry_run": dry_run, "gate": "ok",
        "results": [], "prune": [], "inventory": None, "error": None,
    }


def _step(app: AppRun, operation: str, fn: Callable[..., None], *args: Any,
          after_failure: bool = False, **kwargs: Any) -> None:
    """The per-app exception boundary: an error is recorded on that app's row, the
    results already collected stay, and the run moves on to the next app."""
    if app.failed and not after_failure:
        return
    try:
        fn(*args, **kwargs)
    except Exception as e:
        app.fail(f"{operation}: {_reason(e)}")


def _prepare(instance_dir: Path, kube: Any, app: AppRun) -> None:
    try:
        app.objects = render_application(instance_dir, app.application)
        _, body = platform_plan(instance_dir, kube, app=app.name)
    except RenderError as e:
        app.fail(f"render failed: {e.message}")
        return
    plan_row = next((a for a in body.get("apps") or [] if a.get("name") == app.name), {})
    app.named = generated_names(app.application, plan_row)
    app.refs = [ObjectRef.of(o) for o in app.objects]


def _read_previous(kube: Any, app: AppRun) -> None:
    app.previous, previous_generated = read_app_inventory(kube, app.name)
    rendered_generated = {
        ObjectRef.of(o).key for o in app.objects if is_generated_secret(o, app.named)
    }
    app.generated = frozenset(rendered_generated) | previous_generated


def _apply_phase(kube: Any, app: AppRun, objects: list[dict[str, Any]], *, dry_run: bool) -> None:
    results = apply_objects(kube, objects, dry_run=dry_run, named_generated=app.named)
    app.row["results"] += [_result_dump(r) for r in results]
    failed = next((r for r in results if r.result == "failed"), None)
    if failed is not None:
        app.fail(failed.reason or "apply failed")


def _write_inventory(kube: Any, app: AppRun) -> None:
    entries = inventory_entries(app.refs, app.previous, app.deleted)
    app.row["inventory"] = write_app_inventory(kube, app.name, entries, app.generated).line


def _prune(kube: Any, app: AppRun, *, dry_run: bool, stdin: TextIO, stderr: TextIO) -> None:
    stored = {r.key: r.key in app.generated for r in app.previous}
    candidates, withheld = prune_from_previous(
        kube, app.name, app.previous, app.refs, named_generated=app.named, inventory=stored,
    )
    app.row["prune"] = [_result_dump(w) for w in withheld]
    if not candidates:
        return
    if not confirm_prune(app.name, candidates, stdin, stderr):
        app.row["prune"] += [
            _result_dump(ObjectResult(ref=r, result="withheld", reason="confirmation required"))
            for r in candidates
        ]
        app.fail(f"prune {app.name}: confirmation was not '{CONFIRM_PREFIX}{app.name}'")
        return
    pruned = prune_objects(
        kube, candidates, dry_run=dry_run, app=app.name, named_generated=app.named,
        inventory=stored,
    )
    app.row["prune"] += [_result_dump(r) for r in pruned]
    if not dry_run:
        app.deleted = {r.ref.key for r in pruned if r.result == "deleted"}
    failed = next((r for r in pruned if r.result == "failed"), None)
    if failed is not None:
        app.fail(failed.reason or "prune failed")


def _of_kind(objects: list[dict[str, Any]], kind: str) -> list[dict[str, Any]]:
    return [o for o in objects if str(o.get("kind") or "") == kind]


def _rest(objects: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [o for o in objects if str(o.get("kind") or "") not in (KIND_CRD, KIND_NS)]


def run(
    instance_dir: Path,
    kube: Any,
    *,
    app: str | None = None,
    dry_run: bool = False,
    prune: bool = False,
    stdin: TextIO | None = None,
    stderr: TextIO | None = None,
) -> tuple[int, dict[str, Any]]:
    """Global phases over every selected app: all CRDs (each waited Established), then
    all Namespaces, then app by app the rest in Helm install order, its inventory and
    its prune. A failing app is recorded on its row and the others carry on."""
    in_stream = sys.stdin if stdin is None else stdin
    err = sys.stderr if stderr is None else stderr
    applications = load_applications(instance_dir, app)
    names = [str((a.get("metadata") or {}).get("name") or "") for a in applications]
    gate_errors: dict[str, str] = {}
    blocked = check_gate(kube, names, dry_run=dry_run, errors=gate_errors)
    report: dict[str, Any] = {"ok": True, "dry_run": dry_run, "apps": []}
    blocked_set = set(blocked)
    if blocked:
        for name in blocked:
            print(gate_message(name, dry_run=dry_run), file=err)
        if not dry_run:
            report["ok"] = False
            report["apps"] = [
                {
                    "name": name, "ok": False, "dry_run": False, "gate": "refused",
                    "results": [], "prune": [], "inventory": None, "error": TWO_APPLIERS,
                }
                for name in blocked
            ]
            return EXIT_GATE, report
    runs: list[AppRun] = []
    for application, name in zip(applications, names):
        one = AppRun(application=application, name=name, row=_new_row(name, dry_run))
        if name in blocked_set:
            one.row["gate"] = "reported"
        if name in gate_errors:
            one.fail(gate_errors[name])
        runs.append(one)
    for one in runs:
        _step(one, "plan", _prepare, instance_dir, kube, one)
        _step(one, "read inventory", _read_previous, kube, one)
    for kind in (KIND_CRD, KIND_NS):
        for one in runs:
            _step(one, f"apply {kind}", _apply_phase, kube, one, _of_kind(one.objects, kind),
                  dry_run=dry_run)
    for one in runs:
        _step(one, "apply", _apply_phase, kube, one, _rest(one.objects), dry_run=dry_run)
        if not dry_run:
            _step(one, "write inventory", _write_inventory, kube, one)
        if prune:
            _step(one, "prune", _prune, kube, one, dry_run=dry_run, stdin=in_stream, stderr=err)
        if one.deleted:
            _step(one, "write inventory", _write_inventory, kube, one, after_failure=True)
    for one in runs:
        one.row["ok"] = not one.failed
        report["apps"].append(one.row)
        if one.failed:
            report["ok"] = False
    code = EXIT_OK if report["ok"] else EXIT_FAILED
    return code, report


def cmd_apply(args: Any, *, stdin: TextIO | None = None) -> int:
    instance_dir = Path(args.instance_dir)
    if not instance_dir.is_dir():
        print(f"orbit platform apply: {instance_dir} is not a directory", file=sys.stderr)
        return EXIT_RENDER
    kube = (
        args.kube if getattr(args, "kube", None) is not None
        else Kube(resolve_kubeconfig(getattr(args, "kubeconfig", None)))
    )
    try:
        code, body = run(
            instance_dir, kube,
            app=getattr(args, "app", None),
            dry_run=bool(getattr(args, "dry_run", False)),
            prune=bool(getattr(args, "prune", False)),
            stdin=stdin,
        )
    except RenderError as e:
        print(f"orbit platform apply: render failed for {e.app}: {e.message}", file=sys.stderr)
        return EXIT_RENDER
    from .client import write_json_document
    write_json_document(body, sort_keys=True)
    return code


def add_apply_arguments(parser: Any) -> None:
    parser.add_argument("instance_dir", help="instance checkout containing clusters/platform/apps")
    parser.add_argument("--app", metavar="NAME", help="only this Application metadata.name")
    parser.add_argument(
        "--kubeconfig",
        help="kubeconfig path; ORBITD_KUBECONFIG, then KUBECONFIG, then in-cluster",
    )
    parser.add_argument("--dry-run", action="store_true", help="dryRun=All on every request; no inventory")
    parser.add_argument(
        "--prune", action="store_true",
        help="delete previous inventory minus rendered after typing 'prune <app>'",
    )


RETIRED_MANAGERS = ("argocd-controller",)

MERGE_PATCH_JSON = "application/merge-patch+json"


def _release_failure_detail(exc: BaseException) -> str:
    """Status code + reason phrase only. The response body can quote the
    object's own fields back (admission webhooks do), so it never reaches output."""
    if isinstance(exc, ApiException):
        return f"{exc.status} {exc.reason}"
    return type(exc).__name__


def _retired_managers(entries: Any) -> list[str]:
    """Retired managers owning the live copy, in managedFields order, de-duplicated."""
    found: list[str] = []
    for entry in entries or []:
        manager = entry.get("manager") if isinstance(entry, dict) else None
        if manager in RETIRED_MANAGERS and manager not in found:
            found.append(str(manager))
    return found


def release_objects(kube: Any, objects: list[dict[str, Any]], *, dry_run: bool = False) -> int:
    """Drop retired managers' managedFields entries from each rendered object's live
    copy, once, so a later server-side apply owns what it sets. Objects the cluster
    does not have, or whose live copy no retired manager owns, are left alone."""
    dyn = kube.dynamic
    failed = 0
    for obj in objects:
        ref = ObjectRef.of(obj)
        try:
            got = _get_live(dyn, obj)
        except Exception as e:
            print(
                f"orbit platform release-owners: {ref.kind} {ref.namespace}/{ref.name}: "
                f"failed {_release_failure_detail(e)}",
                file=sys.stderr,
            )
            failed += 1
            continue
        if got is None:
            continue
        live = _as_dict(got)
        entries = (live.get("metadata") or {}).get("managedFields") or []
        released = _retired_managers(entries)
        if not released:
            continue
        label = ",".join(released)
        if dry_run:
            print(f"{ref.kind} {ref.namespace}/{ref.name}: would release {label}")
            continue
        remaining = [
            deepcopy(e) for e in entries
            if not (isinstance(e, dict) and e.get("manager") in RETIRED_MANAGERS)
        ]
        body = {
            "metadata": {
                "managedFields": remaining or [{}],
                "resourceVersion": (live.get("metadata") or {}).get("resourceVersion"),
            },
        }
        try:
            resource = dyn.resources.get(api_version=ref.api_version or "v1", kind=ref.kind)
            kwargs: dict[str, Any] = {
                "name": ref.name, "body": body, "content_type": MERGE_PATCH_JSON,
            }
            if ref.kind not in _CLUSTER_SCOPED and ref.namespace:
                kwargs["namespace"] = ref.namespace
            resource.patch(**kwargs)
        except Exception as e:
            print(
                f"orbit platform release-owners: {ref.kind} {ref.namespace}/{ref.name}: "
                f"failed {_release_failure_detail(e)}",
                file=sys.stderr,
            )
            failed += 1
            continue
        print(f"{ref.kind} {ref.namespace}/{ref.name}: released {label}")
    return EXIT_FAILED if failed else EXIT_OK


def cmd_release_owners(args: Any) -> int:
    instance_dir = Path(args.instance_dir)
    if not instance_dir.is_dir():
        print(f"orbit platform release-owners: {instance_dir} is not a directory", file=sys.stderr)
        return EXIT_RENDER
    kube = (
        args.kube if getattr(args, "kube", None) is not None
        else Kube(resolve_kubeconfig(getattr(args, "kubeconfig", None)))
    )
    try:
        applications = load_applications(instance_dir, getattr(args, "app", None))
        objects = [obj for app in applications for obj in render_application(instance_dir, app)]
    except RenderError as e:
        print(f"orbit platform release-owners: render failed for {e.app}: {e.message}", file=sys.stderr)
        return EXIT_RENDER
    return release_objects(kube, objects, dry_run=bool(getattr(args, "dry_run", False)))


def add_release_owners_arguments(parser: Any) -> None:
    parser.add_argument("instance_dir", help="instance checkout containing clusters/platform/apps")
    parser.add_argument("--app", metavar="NAME", help="only this Application metadata.name")
    parser.add_argument(
        "--kubeconfig",
        help="kubeconfig path; ORBITD_KUBECONFIG, then KUBECONFIG, then in-cluster",
    )
    parser.add_argument("--dry-run", action="store_true", help="print what would be released; send no patch")


__all__ = [
    "EXIT_OK", "EXIT_FAILED", "EXIT_GATE", "TWO_APPLIERS",
    "RULE_CRD", "RULE_KUBE_SYSTEM", "RULE_GENERATED", "RULE_NOT_INVENTORIED",
    "RULE_NO_MANAGED_FIELDS", "RULE_NOT_OWNED", "GENERATED_PRESENT",
    "HELM_INSTALL_ORDER", "CRD_ESTABLISHED_TIMEOUT", "CRD_ESTABLISHED_POLL",
    "inventory_ref_for_app", "automated_sync", "read_live_application",
    "check_gate", "gate_message", "is_generated_secret", "generated_names",
    "strip_checksum_annotations", "strip_render_stamp_annotations",
    "order_objects", "wait_crd_established",
    "platform_prune_rule", "confirm_prune", "apply_objects",
    "RETIRE_CONFIRM_PREFIX", "RULE_NS_SHARED", "RULE_CRD_INSTANCES",
    "platform_retire_rule", "plan_retire", "check_retire_collectors",
    "retire_objects", "confirm_retire", "run_retire", "cmd_retire",
    "add_retire_arguments", "finish_retire_inventory",
    "AppRun",
    "read_app_inventory", "inventory_entries", "write_app_inventory", "run", "cmd_apply", "add_apply_arguments",
    "RETIRED_MANAGERS", "release_objects", "cmd_release_owners", "add_release_owners_arguments",
]
