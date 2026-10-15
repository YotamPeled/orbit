"""The applier: orbitd writes the rendered tenant set to the cluster (PHASE5-DESIGN §2).

`plan` renders (render.py), reads live (shadow.py) and orders every rendered object
by kind in Helm's install order; `apply` server-side-applies each one with field
manager `orbit` and force on conflicts, and after a successful real run writes the
inventory ConfigMap (this run's rendered refs plus previous entries this run did
not delete); `prune` deletes exactly `plan.prune`, the previous inventory
minus the render, after the hard rules below. Nothing here runs on a schedule and
nothing in `apply` deletes.

The hard switch is `ORBITD_APPLY` (`Config.apply_mode`): `off` (default) refuses
apply, prune and the inventory write outright, `dry-run` forces every request to
`dryRun=All`, `on` does what the caller asked. Every write in this module goes through
`_effective_dry_run`; there is no second path.

Prune never trusts what it read at plan time: before each DELETE it re-reads the
object, re-runs the hard rules on that fresh copy, and deletes with `resourceVersion`
and `uid` preconditions from it. An object that changed hands in between is reported
`withheld`, never deleted and never retried.

The applier's own grant, the `orbit-applier-configmaps` Role and RoleBinding the
generator writes into every tenant namespace, is never applied or pruned here: RBAC
escalation prevention means orbitd cannot create a Role granting rights it does not
yet hold in that namespace, so the bootstrap path owns those two objects. `plan`
lists them under `skipped`; the hard rule is "never an RBAC object whose name
starts with `orbit-`".
"""

# History: the bootstrap path above was ArgoCD's app-of-apps before phase 5
# retired that plane and deleted its namespace. Its Application documents stay in
# the instance tree as the platform declaration the desk reads.

from __future__ import annotations

import re
import json
import logging
from copy import deepcopy
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from pydantic import BaseModel, Field

from .backends import KUBE_TIMEOUT
from .deploy import BUDGET_SPENT, budget_spent, guard_namespace, scrub
from .render import render as render_desired
from .roles import CheckoutSource
from .shadow import (
    LABEL_ENV, LABEL_INSTANCE, LABEL_PROJECT, MONITORING_NS, RENDER_STAMP_ANNOTATIONS, _as_dict,
    _get_live, _instance_values, _is_not_found, compare_objects, read_live,
)
from .source import read_source
from .state import AppState
from .tools_viewer.common import ns as tenant_namespace

log = logging.getLogger("orbitd.applier")

FIELD_MANAGER = "orbit"
DRY_RUN_ALL = "All"
INVENTORY_NS = "orbit"
INVENTORY_PREFIX = "orbit-inventory-"
MODE_OFF, MODE_DRY_RUN, MODE_ON = "off", "dry-run", "on"
TLS_SECRET_TYPE = "kubernetes.io/tls"

# Every kind this module may create, patch or delete, listed once: the generator's
# WRITABLE_KINDS (scripts/catalog-sync.py) minus Application, which is ArgoCD's own
# declaration and never applied by orbitd, and minus Role and RoleBinding (the
# applier's own grant, see the module docstring: no RBAC kinds), plus every kind
# charts/service renders. The ClusterRole orbitd-applier in
# template/provisioning/clusters/platform/config/orbitd/rbac.yaml is this table minus
# configmaps, whose verbs come only from namespaced Roles (orbit, monitoring, and the
# generator's orbit-applier-configmaps in every tenant namespace); a test holds the
# two together. Rows are (API group, resource, Kind).
APPLIED_KINDS: tuple[tuple[str, str, str], ...] = (
    ("", "namespaces", "Namespace"),
    ("", "serviceaccounts", "ServiceAccount"),
    ("", "configmaps", "ConfigMap"),
    ("", "resourcequotas", "ResourceQuota"),
    ("", "persistentvolumeclaims", "PersistentVolumeClaim"),
    ("", "services", "Service"),
    ("apps", "deployments", "Deployment"),
    ("batch", "jobs", "Job"),
    ("batch", "cronjobs", "CronJob"),
    ("networking.k8s.io", "networkpolicies", "NetworkPolicy"),
    ("networking.k8s.io", "ingresses", "Ingress"),
    ("traefik.io", "middlewares", "Middleware"),
    ("cert-manager.io", "certificates", "Certificate"),
    ("opentelemetry.io", "instrumentations", "Instrumentation"),
    ("postgresql.cnpg.io", "clusters", "Cluster"),
    ("postgresql.cnpg.io", "scheduledbackups", "ScheduledBackup"),
    ("barmancloud.cnpg.io", "objectstores", "ObjectStore"),
    ("monitoring.coreos.com", "servicemonitors", "ServiceMonitor"),
    ("monitoring.coreos.com", "probes", "Probe"),
    ("monitoring.coreos.com", "prometheusrules", "PrometheusRule"),
)
KINDS: frozenset[str] = frozenset(kind for _, _, kind in APPLIED_KINDS)
APPLY_VERBS: tuple[str, ...] = ("create", "patch", "delete")

# Helm's install order by kind (kind_sorter.go), restricted to what this module
# applies: namespace and identity first, then configuration, then the custom
# resources the workloads mount, then workloads, then monitoring. Ties break on
# (namespace, name); a kind not listed sorts after every listed one, by name.
INSTALL_ORDER: tuple[str, ...] = (
    "Namespace", "ServiceAccount", "ConfigMap", "ResourceQuota",
    "NetworkPolicy", "PersistentVolumeClaim",
    "Certificate", "Middleware", "ObjectStore", "Cluster", "ScheduledBackup",
    "Instrumentation",
    "Service", "Deployment", "Job", "CronJob", "Ingress",
    "ServiceMonitor", "Probe", "PrometheusRule",
)
_RANK = {kind: i for i, kind in enumerate(INSTALL_ORDER)}

# Kinds a pair may own in the shared monitoring namespace (the generator's dashboard
# ConfigMap, Probes and PrometheusRules); everything else must sit in the tenant namespace.
MONITORING_KINDS: frozenset[str] = frozenset({"ConfigMap", "Probe", "PrometheusRule"})

RULE_TLS = "never a Secret of type kubernetes.io/tls"
RULE_NAMESPACE_KIND = "never a Namespace"
RULE_PVC = "never a PersistentVolumeClaim"
RULE_KIND = "kind is not one the generator or chart writes"
RULE_NS = "namespace is not one guard_namespace accepts for this pair"
RULE_LABEL = "live copy is not labelled for this project/env pair"
RULE_ABSENT = "no live copy"
RULE_ORBIT_RBAC = "never an RBAC object whose name starts with orbit-"
RULE_CHANGED = "changed since read"
RBAC_KINDS: frozenset[str] = frozenset({"Role", "RoleBinding", "ClusterRole", "ClusterRoleBinding"})
ORBIT_PREFIX = "orbit-"


class ApplyRefused(Exception):
    """ORBITD_APPLY does not permit this call."""


class ObjectRef(BaseModel):
    api_version: str = ""
    kind: str
    namespace: str = ""
    name: str

    @property
    def group(self) -> str:
        return _group_version(self.api_version)[0]

    @property
    def version(self) -> str:
        return _group_version(self.api_version)[1]

    @property
    def line(self) -> str:
        """Inventory line: group/version/Kind/namespace/name (core group is empty)."""
        return "/".join((self.group, self.version, self.kind, self.namespace, self.name))

    @classmethod
    def from_line(cls, line: str) -> ObjectRef:
        parts = line.strip().split("/")
        if len(parts) != 5 or not parts[1] or not parts[2] or not parts[4]:
            raise ValueError(f"inventory line {line!r} is not group/version/Kind/namespace/name")
        group, version, kind, namespace, name = parts
        return cls(api_version=f"{group}/{version}" if group else version,
                   kind=kind, namespace=namespace, name=name)

    @classmethod
    def of(cls, obj: dict[str, Any]) -> ObjectRef:
        meta = obj.get("metadata") or {}
        return cls(api_version=str(obj.get("apiVersion") or ""), kind=str(obj.get("kind") or ""),
                   namespace=str(meta.get("namespace") or ""), name=str(meta.get("name") or ""))

    @property
    def key(self) -> tuple[str, str, str]:
        return (self.kind, self.namespace, self.name)

    def stub(self) -> dict[str, Any]:
        meta: dict[str, Any] = {"name": self.name}
        if self.namespace:
            meta["namespace"] = self.namespace
        return {"apiVersion": self.api_version or "v1", "kind": self.kind, "metadata": meta}


class Withheld(BaseModel):
    """A previous-inventory object the render dropped but a hard rule keeps."""

    ref: ObjectRef
    rule: str


class ObjectResult(BaseModel):
    ref: ObjectRef
    result: str  # apply: applied | unchanged | failed; prune: deleted | absent | withheld | failed
    reason: str | None = None


class ApplyResult(BaseModel):
    dry_run: bool
    ok: bool
    results: list[ObjectResult] = Field(default_factory=list)
    inventory: ObjectRef | None = None  # the ConfigMap written (real runs only)


class PruneResult(BaseModel):
    dry_run: bool
    ok: bool
    results: list[ObjectResult] = Field(default_factory=list)


@dataclass
class Plan:
    project: str
    env: str
    namespace: str
    objects: list[dict[str, Any]]  # rendered, in apply order
    refs: list[ObjectRef]  # same order
    prune: list[ObjectRef]  # previous inventory minus rendered, after the hard rules
    withheld: list[Withheld]  # the rest of that difference, each with the rule that kept it
    previous: list[ObjectRef]  # the previous inventory as read
    inventory_found: bool
    render_sha: str
    source: CheckoutSource
    live: dict[tuple[str, str, str], dict[str, Any]] = field(default_factory=dict)
    skipped: list[Withheld] = field(default_factory=list)  # rendered, never applied (orbit-* RBAC)


# ------------------------------------------------------------------ the switch

def apply_mode(state: AppState) -> str:
    mode = str(getattr(state.cfg, "apply_mode", MODE_OFF) or MODE_OFF)
    return mode if mode in (MODE_OFF, MODE_DRY_RUN, MODE_ON) else MODE_OFF


PAIR_NOT_ENABLED_DETAIL = "pair not in ORBITD_APPLY_PAIRS: this orbitd does not apply it"
PAIR_NOT_ENABLED_MODE = "off (pair not enabled)"


def apply_pairs(state: AppState) -> tuple[str, ...]:
    """ORBITD_APPLY_PAIRS as project/env strings; empty means every catalog pair."""
    raw = getattr(state.cfg, "apply_pairs", ()) or ()
    return tuple(str(p).strip() for p in raw if str(p).strip())


def pair_enabled(state: AppState, project: str, env: str) -> bool:
    """False when ORBITD_APPLY_PAIRS is non-empty and does not list this pair."""
    pairs = apply_pairs(state)
    if not pairs:
        return True
    return f"{project}/{env}" in pairs


def _effective_dry_run(state: AppState, dry_run: bool, what: str) -> bool:
    mode = apply_mode(state)
    if mode == MODE_OFF:
        raise ApplyRefused(
            f"ORBITD_APPLY=off: {what} is disabled on this orbitd; "
            f"set ORBITD_APPLY=dry-run or ORBITD_APPLY=on")
    if mode == MODE_DRY_RUN:
        return True
    return bool(dry_run)


# ------------------------------------------------------------------ plan

def plan(state: AppState, project: str, env: str) -> Plan:
    """Render, read live and the previous inventory, order, and name the prune set."""
    objects, sha = render_desired(state, project, env)
    ordered = sorted((deepcopy(o) for o in objects), key=_order_key)
    skipped = [Withheld(ref=ObjectRef.of(o), rule=RULE_ORBIT_RBAC)
               for o in ordered if is_orbit_rbac(ObjectRef.of(o))]
    ordered = [o for o in ordered if not is_orbit_rbac(ObjectRef.of(o))]
    refs = [ObjectRef.of(o) for o in ordered]
    live_index = {ObjectRef.of(o).key: o for o in read_live(state, ordered, project, env)}
    previous, found = read_inventory(state, project, env)
    rendered_keys = {r.key for r in refs}
    instance_vals = _instance_values(ordered)
    instance_vals.add(project)
    prune: list[ObjectRef] = []
    withheld: list[Withheld] = []
    dyn = state.backends.kube.dynamic
    for ref in previous:
        if ref.key in rendered_keys:
            continue
        live = live_index.get(ref.key)
        if live is None:
            got = _get_live(dyn, ref.stub())
            live = _as_dict(got) if got is not None else None
        rule = prune_rule(project, env, ref, live, instance_vals=instance_vals)
        if rule is None:
            prune.append(ref)
        else:
            withheld.append(Withheld(ref=ref, rule=rule))
    prune.sort(key=_prune_order_key)
    return Plan(
        project=project, env=env, namespace=tenant_namespace(project, env),
        objects=ordered, refs=refs, prune=prune, withheld=withheld,
        previous=previous, inventory_found=found, render_sha=sha,
        source=read_source(state), live=live_index, skipped=skipped,
    )


def _order_key(obj: dict[str, Any]) -> tuple[int, str, str, str]:
    ref = ObjectRef.of(obj)
    return (_RANK.get(ref.kind, len(INSTALL_ORDER)), ref.kind, ref.namespace, ref.name)


def _prune_order_key(ref: ObjectRef) -> tuple[int, str, str, str]:
    """Reverse of the install order: workloads go before what they mount."""
    return (-_RANK.get(ref.kind, len(INSTALL_ORDER)), ref.kind, ref.namespace, ref.name)


def prune_rule(project: str, env: str, ref: ObjectRef, live: dict[str, Any] | None, *,
               instance_vals: set[str] | None = None) -> str | None:
    """The first hard rule that forbids deleting `ref`, or None when it may go."""
    if is_orbit_rbac(ref):
        return RULE_ORBIT_RBAC
    if ref.kind == "Secret" and live is not None and str(live.get("type") or "") == TLS_SECRET_TYPE:
        return RULE_TLS
    if ref.kind == "Namespace":
        return RULE_NAMESPACE_KIND
    if ref.kind == "PersistentVolumeClaim":
        return RULE_PVC
    if ref.kind not in KINDS:
        return RULE_KIND
    if not namespace_allowed(project, env, ref):
        return RULE_NS
    if live is None:
        return RULE_ABSENT
    vals = set(instance_vals or ())
    vals.add(project)
    if not labelled_for_pair(ref, live, instance_vals=vals, project=project, env=env):
        return RULE_LABEL
    return None


def is_orbit_rbac(ref: ObjectRef) -> bool:
    """The applier's own grant (and any other `orbit-*` RBAC object): never written, never pruned."""
    return ref.kind in RBAC_KINDS and ref.name.startswith(ORBIT_PREFIX)


def labelled_for_pair(ref: ObjectRef, live: dict[str, Any], *, instance_vals: set[str],
                      project: str, env: str) -> bool:
    """shadow's extras rule, keyed on the ref's namespace: `orbit.local/project`+`env`
    in monitoring, `app.kubernetes.io/instance` in the tenant namespace."""
    labels = (live.get("metadata") or {}).get("labels") or {}
    if ref.namespace == MONITORING_NS:
        return (str(labels.get(LABEL_PROJECT) or "") == project
                and str(labels.get(LABEL_ENV) or "") == env)
    inst = labels.get(LABEL_INSTANCE)
    return inst is not None and str(inst) in instance_vals


def namespace_allowed(project: str, env: str, ref: ObjectRef) -> bool:
    """The tenant namespace guard_namespace derives, or monitoring for its few kinds."""
    if ref.kind == "Namespace":
        return guard_namespace(project, env, ref.name) is None
    if ref.namespace == MONITORING_NS:
        return ref.kind in MONITORING_KINDS
    return bool(ref.namespace) and guard_namespace(project, env, ref.namespace) is None


# ------------------------------------------------------------------ apply

def apply(state: AppState, plan: Plan, *, dry_run: bool) -> ApplyResult:
    """Server-side apply every planned object in order; stop at the first failure.

    Nothing is rolled back: server-side apply is idempotent and the next run
    re-applies. A real, fully successful run writes the inventory ConfigMap.
    """
    dry = _effective_dry_run(state, dry_run, "apply")
    dyn = state.backends.kube.dynamic
    results: list[ObjectResult] = []
    settled = workloads_settled(plan)
    applied_before = _applied_here_before(state, plan)
    for obj, ref in zip(plan.objects, plan.refs):
        if ref.kind == "ResourceQuota":
            obj = quota_for_now(obj, plan.live.get(ref.key), settled=settled)
        if not dry and budget_spent(state, plan.project, plan.env):
            results.append(ObjectResult(ref=ref, result="failed", reason=BUDGET_SPENT))
            return ApplyResult(dry_run=dry, ok=False, results=results)
        reason = _apply_guard(plan, ref)
        if reason is not None:
            results.append(ObjectResult(ref=ref, result="failed", reason=reason))
            return ApplyResult(dry_run=dry, ok=False, results=results)
        try:
            after = _server_side_apply(dyn, obj, ref, dry=dry)
            reverted = _revert_hand_edits(dyn, ref, _as_dict(after), dry=dry)
        except Exception as e:  # the dynamic client raises its own hierarchy
            results.append(ObjectResult(ref=ref, result="failed", reason=_reason(e)))
            return ApplyResult(dry_run=dry, ok=False, results=results)
        live = plan.live.get(ref.key)
        if reverted:
            reason = (("would revert" if dry else "reverted") + " a hand edit: " + "; ".join(reverted))[:1000]
            # in orbitd's log too, so the reconcile loop's silent fix stays findable in Loki
            log.warning("%s/%s %s %s/%s: %s", plan.project, plan.env, ref.kind, ref.namespace, ref.name, reason)
            results.append(ObjectResult(ref=ref, result="applied", reason=reason))
            continue
        # The other hand edits: a field orbit owns that a hand tool changed (a scale, an
        # image), which the apply itself just put back, and an object orbit had applied
        # that is gone from the cluster. Both were undone in silence: on the test box a
        # scale to three, a deleted Service and a deleted NetworkPolicy came back after
        # 4.6 minutes with the dev site down meanwhile, and nothing anywhere said what
        # had happened (QA of v1.0.0, F33).
        undone = _undone_by_apply(obj, live, plan, deleted=live is None and ref.key in applied_before)
        if undone:
            reason = (("would revert" if dry else "reverted") + " a hand edit: " + undone)[:1000]
            log.warning("%s/%s %s %s/%s: %s", plan.project, plan.env, ref.kind, ref.namespace, ref.name, reason)
            results.append(ObjectResult(ref=ref, result="applied", reason=reason))
            continue
        results.append(ObjectResult(
            ref=ref, result="unchanged" if _same_as_live(obj, live, plan) else "applied"))
    inventory = None
    if not dry:
        inventory = write_inventory(state, plan)
    return ApplyResult(dry_run=dry, ok=True, results=results, inventory=inventory)


# ------------------------------------------------------------------ the quota, lowered late
#
# The quota is derived from the catalog, so a component given LESS memory lowers it in the
# same commit. Applied at once (it sorts before the workloads), the lower quota met the old,
# larger pod still running: the rollout's new pod was refused ("exceeded quota"), the old
# pod was therefore never replaced, and the namespace stayed that way for good, refusing
# every later ship too. Seen on the rebuilt test box, 2026-10-09, when a component went
# from 1Gi back to 256Mi; three scenarios failed behind it (docs/V1.md §8). So a quota is
# RAISED at once and LOWERED only when the pair's Deployments are what was asked for and
# done rolling. Until then each key keeps the higher of the live and the wanted value,
# which is the room the old pods were admitted under; the reconcile loop lowers it on its
# next pass after the rollout.

_QTY = re.compile(r"^([0-9]+(?:\.[0-9]+)?)(Ki|Mi|Gi|Ti|k|K|M|G|T|m)?$")
_QTY_UNIT = {None: 1.0, "Ki": 1024.0, "Mi": 1024.0 ** 2, "Gi": 1024.0 ** 3, "Ti": 1024.0 ** 4,
             "k": 1e3, "K": 1e3, "M": 1e6, "G": 1e9, "T": 1e12, "m": 1e-3}


def _qty(value: Any) -> float | None:
    m = _QTY.match(str(value).strip()) if value is not None else None
    return float(m.group(1)) * _QTY_UNIT[m.group(2)] if m else None


def deployment_settled(live: dict[str, Any] | None) -> bool:
    """A live Deployment whose rollout is over: the controller has seen the newest spec,
    every pod is of it, and none of an older one is left."""
    if live is None:
        return False
    spec, status = live.get("spec") or {}, live.get("status") or {}
    want = spec.get("replicas", 1)
    if (status.get("observedGeneration") or 0) < ((live.get("metadata") or {}).get("generation") or 0):
        return False
    return (status.get("replicas") or 0) == want and (status.get("updatedReplicas") or 0) == want \
        and (status.get("availableReplicas") or 0) == want


def _pod_sizes(obj: dict[str, Any] | None) -> dict[tuple[str, str, str], float | None] | None:
    """What a Deployment's pods ask for: (container, requests|limits, resource) -> quantity."""
    try:
        spec = obj["spec"]["template"]["spec"]  # type: ignore[index]
    except (KeyError, TypeError):
        return None
    out: dict[tuple[str, str, str], float | None] = {}
    for c in list(spec.get("initContainers") or []) + list(spec.get("containers") or []):
        for side in ("requests", "limits"):
            for resource, value in (((c.get("resources") or {}).get(side)) or {}).items():
                out[(str(c.get("name")), side, str(resource))] = _qty(value)
    return out


def workloads_settled(plan: Plan) -> bool:
    """Every Deployment of the plan runs pods of the SIZES rendered and is done rolling.

    Sizes, not the whole object: what a quota counts is what the pods ask for. Until
    v0.34.34 this asked for a Deployment identical to its render, and one that had ever
    been restarted through orbitd carries the restart's stamp for good (a difference the
    drift compare reports and nobody removes), so its namespace's quota was never lowered
    again: on the rebuilt box a quota raised for a size that was then taken back stayed
    64Mi up, tick after tick (2026-10-11, docs/V1.md §8)."""
    for obj, ref in zip(plan.objects, plan.refs):
        if ref.kind != "Deployment":
            continue
        live = plan.live.get(ref.key)
        want = _pod_sizes(obj)
        if want is None or _pod_sizes(live) != want or not deployment_settled(live):
            return False
    return True


def quota_for_now(obj: dict[str, Any], live: dict[str, Any] | None, *, settled: bool) -> dict[str, Any]:
    """The ResourceQuota to apply now: as rendered when the workloads are settled or there
    is no live quota; otherwise with every key the render would LOWER kept at its live
    value. A key the render raises, adds or leaves alone is as rendered; a value that is
    not a quantity is as rendered."""
    if settled or live is None:
        return obj
    want = ((obj.get("spec") or {}).get("hard") or {})
    have = ((live.get("spec") or {}).get("hard") or {})
    kept = {k: have[k] for k, v in want.items()
            if k in have and _qty(v) is not None and _qty(have[k]) is not None and _qty(have[k]) > _qty(v)}
    if not kept:
        return obj
    out = deepcopy(obj)
    out["spec"]["hard"] = {**want, **kept}
    return out


def _apply_guard(plan: Plan, ref: ObjectRef) -> str | None:
    if is_orbit_rbac(ref):
        return RULE_ORBIT_RBAC
    if ref.kind not in KINDS:
        return f"kind {ref.kind} is not one this applier writes"
    if not ref.name:
        return "object has no metadata.name"
    if not namespace_allowed(plan.project, plan.env, ref):
        where = ref.name if ref.kind == "Namespace" else ref.namespace or "(none)"
        return f"namespace {where!r} is not one guard_namespace accepts for {plan.project}/{plan.env}"
    return None


def _server_side_apply(dyn: Any, obj: dict[str, Any], ref: ObjectRef, *, dry: bool) -> Any:
    resource = dyn.resources.get(api_version=ref.api_version or "v1", kind=ref.kind)
    kwargs: dict[str, Any] = {
        "body": obj, "name": ref.name, "field_manager": FIELD_MANAGER,
        "force_conflicts": True, "_request_timeout": KUBE_TIMEOUT,
    }
    if ref.kind != "Namespace" and ref.namespace:
        kwargs["namespace"] = ref.namespace
    if dry:
        kwargs["dry_run"] = DRY_RUN_ALL
    return resource.server_side_apply(**kwargs)


# ------------------------------------------------------------------ hand edits
#
# Server-side apply removes a field only when its applier owned it and stopped sending
# it. A field a person added by hand (`kubectl set env`, `kubectl edit`) is owned by
# THAT field manager, so orbit's apply never removes it: the drift compare saw the extra
# env var on every run and the reconcile loop left it in place for ever (S14, docs/V1.md
# §8). After each apply the fields a hand-edit manager owns ALONE — no orbit entry and no
# controller co-owns them — are removed with one JSON patch, guarded by the object's
# resourceVersion. Only hand tools are matched (kubectl and the usual consoles), so a
# controller's own writes are never touched, and a restart stamp is not a hand edit.

HAND_EDIT_MANAGERS: tuple[str, ...] = ("kubectl", "k9s", "lens", "openlens", "headlamp", "mozilla")
_HAND_EDIT_ROOTS = ("f:spec", "f:data", "f:binaryData", "f:metadata")
_HAND_EDIT_EXEMPT = frozenset(f"f:{a}" for a in RENDER_STAMP_ANNOTATIONS)


def _is_hand_manager(entry: Any) -> bool:
    if not isinstance(entry, dict) or entry.get("subresource"):
        return False
    return str(entry.get("manager") or "").lower().startswith(HAND_EDIT_MANAGERS)


def _merge_fields(into: dict[str, Any], tree: Any) -> None:
    if not isinstance(tree, dict):
        return
    for k, v in tree.items():
        if isinstance(v, dict):
            _merge_fields(into.setdefault(k, {}), v)
        else:
            into.setdefault(k, {})


def _alone(hand: dict[str, Any], others: dict[str, Any] | None, segs: list[str],
           out: list[list[str]]) -> None:
    """Every node of `hand` that it owns (a leaf, or an item marked '.') and no other
    manager's tree reaches; a node the others co-own is descended into instead."""
    for key, sub in hand.items():
        if key == "." or not isinstance(sub, dict) or key in _HAND_EDIT_EXEMPT:
            continue
        theirs = (others or {}).get(key)
        if (not sub or "." in sub) and theirs is None:
            out.append(segs + [key])
            continue
        _alone(sub, theirs if isinstance(theirs, dict) else None, segs + [key], out)


def _resolve(obj: Any, segs: list[str]) -> tuple[list[str | int], str] | None:
    """A fieldsV1 path as JSON-pointer steps against the live object, plus a shown path
    (field names and list keys only — never a value). None when the field is gone."""
    steps: list[str | int] = []
    shown = ""
    cur = obj
    for seg in segs:
        kind, _, rest = seg.partition(":")
        if kind == "f" and isinstance(cur, dict) and rest in cur:
            steps.append(rest)
            shown += ("." if shown else "") + rest
            cur = cur[rest]
        elif kind in ("k", "v") and isinstance(cur, list):
            try:
                want = json.loads(rest)
            except ValueError:
                return None
            idx = next((i for i, item in enumerate(cur)
                        if (kind == "v" and item == want)
                        or (kind == "k" and isinstance(item, dict) and isinstance(want, dict)
                            and all(item.get(a) == b for a, b in want.items()))), None)
            if idx is None:
                return None
            steps.append(idx)
            label = ",".join(f"{a}={b}" for a, b in want.items()) if kind == "k" and isinstance(want, dict) else "value"
            shown += f"[{label}]"
            cur = cur[idx]
        else:
            return None
    return steps, shown


def hand_edits(live: dict[str, Any]) -> list[tuple[list[str | int], str, str]]:
    """(pointer steps, shown path, manager) for each field a hand-edit manager owns alone."""
    entries = (live.get("metadata") or {}).get("managedFields") or []
    others: dict[str, Any] = {}
    for e in entries:
        if isinstance(e, dict) and not _is_hand_manager(e):
            _merge_fields(others, e.get("fieldsV1"))
    found: list[tuple[list[str | int], str, str]] = []
    for e in entries:
        if not _is_hand_manager(e):
            continue
        tree = e.get("fieldsV1") or {}
        paths: list[list[str]] = []
        for root in _HAND_EDIT_ROOTS:
            if isinstance(tree.get(root), dict):
                if root == "f:metadata":
                    for sub in ("f:labels", "f:annotations"):
                        if isinstance(tree[root].get(sub), dict):
                            _alone(tree[root][sub], ((others.get(root) or {}).get(sub)), [root, sub], paths)
                else:
                    _alone(tree[root], others.get(root), [root], paths)
        for segs in paths:
            got = _resolve(live, segs)
            if got is not None:
                found.append((got[0], got[1], str(e.get("manager"))[:64]))
    return found


def _pointer(steps: list[str | int]) -> str:
    return "".join("/" + str(s).replace("~", "~0").replace("/", "~1") for s in steps)


def _revert_hand_edits(dyn: Any, ref: ObjectRef, live: dict[str, Any], *, dry: bool) -> list[str]:
    """Remove the fields hand edits own alone; the shown paths, or [] when there are none."""
    edits = hand_edits(live) if live else []
    if not edits:
        return []
    # later list items first, so an earlier removal never shifts a later index
    edits.sort(key=lambda e: [(1, s) if isinstance(s, int) else (0, s) for s in e[0]], reverse=True)
    ops: list[dict[str, Any]] = [{"op": "test", "path": "/metadata/resourceVersion",
                                  "value": str((live.get("metadata") or {}).get("resourceVersion") or "")}]
    ops += [{"op": "remove", "path": _pointer(steps)} for steps, _, _ in edits]
    resource = dyn.resources.get(api_version=ref.api_version or "v1", kind=ref.kind)
    kwargs: dict[str, Any] = {"body": ops, "name": ref.name, "field_manager": FIELD_MANAGER,
                              "content_type": "application/json-patch+json",
                              "_request_timeout": KUBE_TIMEOUT}
    if ref.kind != "Namespace" and ref.namespace:
        kwargs["namespace"] = ref.namespace
    if dry:
        kwargs["dry_run"] = DRY_RUN_ALL
    resource.patch(**kwargs)
    return [f"{shown} (by {manager})" for _, shown, manager in sorted(edits, key=lambda e: e[1])]


def _epoch(stamp: Any) -> float | None:
    if stamp is None:
        return None
    if hasattr(stamp, "timestamp"):
        return float(stamp.timestamp())
    try:
        return datetime.fromisoformat(str(stamp).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _applied_here_before(state: AppState, plan: Plan) -> set[tuple[str, str, str]]:
    """The objects the inventory says orbit applied to this pair before, when that
    inventory is this namespace's own: written after the namespace was made.

    An object in it that is gone from the cluster was deleted by a hand. But an
    inventory can be older than the namespace: a project made again under the name of a
    removed one found the removed one's inventory still in namespace `orbit`, and its
    first tick called every object "deleted from the cluster and restored" (v1.0.1-rc1 on
    the test box). An inventory that predates the namespace, a namespace this apply is
    creating, or a date that cannot be read: nothing is claimed."""
    if not plan.previous:
        return set()
    namespace = next((plan.live.get(r.key) for r in plan.refs if r.kind == "Namespace"), None)
    if not isinstance(namespace, dict):
        return set()
    created = _epoch((namespace.get("metadata") or {}).get("creationTimestamp"))
    try:
        got = _get_live(state.backends.kube.dynamic, inventory_ref(plan.project, plan.env).stub())
    except Exception:
        return set()
    written = _epoch(((_as_dict(got).get("data") or {}) if got is not None else {}).get("applied_at"))
    if created is None or written is None or written < created:
        return set()
    return {r.key for r in plan.previous}


def _owns(tree: Any, path: str) -> bool:
    """Whether a managedFields `fieldsV1` tree owns the field at `path` (the comparison's
    `/spec/replicas` form). A list item is matched through any of the tree's keyed items;
    owning a parent whole (an empty node) owns what is under it."""
    node = tree
    for seg in [p for p in path.split("/") if p]:
        if not isinstance(node, dict):
            return False
        if not node:
            return True
        keyed = [v for k, v in node.items() if str(k).startswith(("k:", "v:", "i:"))]
        if keyed and (seg.isdigit() or f"f:{seg}" not in node):
            merged: dict[str, Any] = {}
            for item in keyed:
                _merge_fields(merged, item)
            node = merged
            continue
        if f"f:{seg}" not in node:
            return False
        node = node[f"f:{seg}"]
    return isinstance(node, dict)


def _undone_by_apply(obj: dict[str, Any], live: dict[str, Any] | None, plan: Plan, *, deleted: bool) -> str | None:
    """What a hand did to an object that this apply put back, as one phrase; None when
    the apply changed it for another reason (a new commit) or not at all. Paths and the
    tool's name only, never a value."""
    if deleted:
        return "the object had been deleted from the cluster and is restored"
    if live is None:
        return None
    hands = [e for e in (live.get("metadata") or {}).get("managedFields") or []
             if isinstance(e, dict) and str(e.get("manager") or "").lower().startswith(HAND_EDIT_MANAGERS)]
    if not hands:
        return None
    report = compare_objects([obj], [live], project=plan.project, env=plan.env)
    if not report.changed:
        return None
    found: list[str] = []
    for path in report.changed[0].paths:
        by = next((str(e.get("manager"))[:64] for e in hands if _owns(e.get("fieldsV1") or {}, path)), None)
        if by:
            found.append(f"{path.strip('/').replace('/', '.')} (by {by})")
    return "; ".join(sorted(found)[:8]) or None


def _same_as_live(obj: dict[str, Any], live: dict[str, Any] | None, plan: Plan) -> bool:
    if live is None:
        return False
    report = compare_objects([obj], [live], project=plan.project, env=plan.env)
    return not report.changed and not report.missing


def _reason(exc: BaseException) -> str:
    """Class plus status and reason phrase for an API error — never the response body or
    headers: an admission webhook's message quotes the object's own fields back, and a
    Secret field would then reach a tool result, an audit step and stderr."""
    status = getattr(exc, "status", None)
    if status is not None:
        phrase = getattr(exc, "reason", None) or ""
        return f"{exc.__class__.__name__}: HTTP {status} {phrase}".rstrip()[:200]
    text = scrub(str(exc) or exc.__class__.__name__)
    return f"{exc.__class__.__name__}: {text}"[:1000]


# ------------------------------------------------------------------ inventory

def inventory_name(project: str, env: str) -> str:
    return f"{INVENTORY_PREFIX}{project}-{env}"


def inventory_ref(project: str, env: str, *, name: str | None = None) -> ObjectRef:
    """The inventory ConfigMap ref: the pair's, or `name` for a unit with its own."""
    return ObjectRef(api_version="v1", kind="ConfigMap", namespace=INVENTORY_NS,
                     name=name or inventory_name(project, env))


def read_inventory(state: AppState, project: str, env: str, *,
                   name: str | None = None) -> tuple[list[ObjectRef], bool]:
    """Previous applied set and whether the ConfigMap exists. Missing = empty = no prune."""
    ref = inventory_ref(project, env, name=name)
    got = _get_live(state.backends.kube.dynamic, ref.stub())
    if got is None:
        return [], False
    data = _as_dict(got).get("data") or {}
    refs: list[ObjectRef] = []
    for line in str(data.get("objects") or "").splitlines():
        if not line.strip():
            continue
        try:
            refs.append(ObjectRef.from_line(line))
        except ValueError as e:
            log.warning("inventory %s/%s: %s (line skipped, never pruned)", INVENTORY_NS, ref.name, e)
    return refs, True


def inventory_entries(rendered: list[ObjectRef], previous: list[ObjectRef],
                      deleted: set[tuple[str, str, str]]) -> list[ObjectRef]:
    """This run's rendered refs plus every previous entry this run did not delete."""
    by_key = {r.key: r for r in previous if r.key not in deleted}
    by_key.update({r.key: r for r in rendered})
    return list(by_key.values())


def inventory_body(plan: Plan, *, applied_at: str | None = None,
                   deleted: set[tuple[str, str, str]] | None = None,
                   name: str | None = None) -> dict[str, Any]:
    ref = inventory_ref(plan.project, plan.env, name=name)
    entries = inventory_entries(plan.refs, plan.previous, deleted or set())
    return {
        "apiVersion": "v1", "kind": "ConfigMap",
        "metadata": {
            "name": ref.name, "namespace": ref.namespace,
            "labels": {LABEL_PROJECT: plan.project, LABEL_ENV: plan.env,
                       "app.kubernetes.io/managed-by": FIELD_MANAGER},
        },
        "data": {
            "objects": "\n".join(sorted(r.line for r in entries)),
            "render_sha": plan.render_sha,
            "commit": plan.source.commit,
            "applied_at": applied_at or _now(),
        },
    }


def write_inventory(state: AppState, plan: Plan, *, dry_run: bool = False,
                    deleted: set[tuple[str, str, str]] | None = None,
                    name: str | None = None) -> ObjectRef:
    """Server-side apply the inventory ConfigMap, through the same switch as every
    other write: `off` refuses, `dry-run` sends dryRun=All and stores nothing.

    The stored set is this run's rendered refs union the previous entries this
    run did not delete.
    """
    dry = _effective_dry_run(state, dry_run, "write_inventory")
    body = inventory_body(plan, deleted=deleted, name=name)
    ref = ObjectRef.of(body)
    _server_side_apply(state.backends.kube.dynamic, body, ref, dry=dry)
    return ref


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


# ------------------------------------------------------------------ prune

def prune(state: AppState, plan: Plan, *, dry_run: bool) -> PruneResult:
    """Delete exactly `plan.prune`, in reverse install order; stop at the first failure.

    `plan.live` is never consulted: each object is re-read now, the hard rules run on
    that copy, and the DELETE carries its `resourceVersion` and `uid` as preconditions.
    A rule that now forbids it, or a precondition the server refuses (409), reports the
    object `withheld` and moves on; only an unexpected error stops the run.
    """
    dry = _effective_dry_run(state, dry_run, "prune")
    dyn = state.backends.kube.dynamic
    results: list[ObjectResult] = []
    instance_vals = _instance_values(plan.objects)
    for ref in plan.prune:
        if not dry and budget_spent(state, plan.project, plan.env):
            results.append(ObjectResult(ref=ref, result="failed", reason=BUDGET_SPENT))
            return PruneResult(dry_run=dry, ok=False, results=results)
        fresh = _live_now(dyn, ref)
        rule = prune_rule(plan.project, plan.env, ref, fresh, instance_vals=instance_vals)
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
                "body": _delete_options(fresh, dry=dry),
            }
            if ref.kind != "Namespace" and ref.namespace:
                kwargs["namespace"] = ref.namespace
            resource.delete(**kwargs)
        except Exception as e:
            if _is_not_found(e):
                results.append(ObjectResult(ref=ref, result="absent"))
                continue
            if _is_conflict(e):
                results.append(ObjectResult(ref=ref, result="withheld", reason=RULE_CHANGED))
                continue
            results.append(ObjectResult(ref=ref, result="failed", reason=_reason(e)))
            return PruneResult(dry_run=dry, ok=False, results=results)
        results.append(ObjectResult(ref=ref, result="deleted"))
    return PruneResult(dry_run=dry, ok=True, results=results)


def _preconditions(live: dict[str, Any]) -> dict[str, str]:
    """`metadata.resourceVersion` and `metadata.uid` of the copy just read, both required:
    a DELETE without them would take whatever is there now."""
    meta = live.get("metadata") or {}
    rv, uid = str(meta.get("resourceVersion") or ""), str(meta.get("uid") or "")
    if not rv or not uid:
        raise ValueError("live object carries no resourceVersion/uid; refusing to delete blind")
    return {"resourceVersion": rv, "uid": uid}


def _delete_options(live: dict[str, Any], *, dry: bool) -> dict[str, Any]:
    """DeleteOptions for every delete orbitd sends: preconditions from the fresh copy,
    plus dryRun=["All"] in this body when the call is a dry run. The API server reads
    DeleteOptions from a present body and ignores query parameters, so a dry_run
    keyword on delete is never enough and is never passed."""
    body: dict[str, Any] = {"preconditions": _preconditions(live)}
    if dry:
        body["dryRun"] = [DRY_RUN_ALL]
    return body


def _is_conflict(exc: BaseException) -> bool:
    if exc.__class__.__name__ == "ConflictError":
        return True
    return getattr(exc, "status", None) == 409


def _live_now(dyn: Any, ref: ObjectRef) -> dict[str, Any] | None:
    got = _get_live(dyn, ref.stub())
    return _as_dict(got) if got is not None else None


def _group_version(api_version: str) -> tuple[str, str]:
    if "/" in api_version:
        group, _, version = api_version.partition("/")
        return group, version
    return "", api_version


__all__ = [
    "APPLIED_KINDS", "APPLY_VERBS", "KINDS", "INSTALL_ORDER", "MONITORING_KINDS",
    "FIELD_MANAGER", "INVENTORY_NS", "MODE_OFF", "MODE_DRY_RUN", "MODE_ON",
    "ApplyRefused", "ObjectRef", "Withheld", "ObjectResult", "ApplyResult",
    "PruneResult", "Plan", "apply_mode", "apply_pairs", "pair_enabled",
    "PAIR_NOT_ENABLED_DETAIL", "PAIR_NOT_ENABLED_MODE",
    "plan", "prune_rule", "namespace_allowed",
    "labelled_for_pair", "is_orbit_rbac", "apply", "prune", "read_inventory", "write_inventory",
    "inventory_entries", "inventory_body", "inventory_name", "inventory_ref", "LABEL_INSTANCE",
    "RULE_ORBIT_RBAC", "RULE_CHANGED", "RBAC_KINDS",
]
