"""Monitors as their own reconcile unit: `monitors/*.yaml` applied beside the pairs.

`plan_monitors` renders the monitor files (monitors.py, pure) and names the prune
set against the unit's own inventory (`orbit/orbit-inventory-monitors`);
`apply_monitors` server-side-applies the render through the applier's switch and
writers; `prune_monitors` deletes exactly the plan's prune set and rewrites the
inventory without the deleted. The reconcile loop applies but never prunes.

A monitor whose file still exists but is now invalid stays declared: its previous
objects are neither applied nor pruned, so a bad edit cannot delete what the last
good render made. The prune hard rules only ever let go of objects in the
namespaces monitors render into that still carry the monitors label.
"""

from __future__ import annotations

from copy import deepcopy
import hashlib
import json
import logging
from pathlib import Path
from typing import Any

import yaml

from .applier import (
    RULE_CHANGED,
    ApplyResult,
    ObjectRef,
    ObjectResult,
    Plan,
    PruneResult,
    Withheld,
    _delete_options,
    _effective_dry_run,
    _is_conflict,
    _is_not_found,
    _live_now,
    _prune_order_key,
    _reason,
    _same_as_live,
    _server_side_apply,
    read_inventory,
    write_inventory,
)
from .backends import KUBE_TIMEOUT
from .deploy import BUDGET_SPENT, budget_spent
from .monitors import (
    COMPONENT,
    LABEL_COMPONENT,
    LABEL_MONITOR,
    LOG_CONFIGMAP,
    log_configmap,
    LOG_GROUP,
    LOKI_NS,
    MONITORING_NS,
    render_monitors,
)
from .shadow import _as_dict, _get_live
from .source import read_source
from . import features
from . import orbit_features as rules
from .state import AppState

log = logging.getLogger("orbitd.monitors_apply")

UNIT = "monitors"
INVENTORY_NAME = "orbit-inventory-monitors"
MONITOR_PREFIX = "orbit-monitor-"

RULE_SCOPE = ("not a monitor object: kind must be PrometheusRule or Probe in "
              "monitoring, or ConfigMap orbit-monitors-logs in loki")
RULE_ABSENT = "no live copy"
RULE_LABEL = "live copy is not labelled orbit.local/component: monitors"


def plan_monitors(state: AppState) -> Plan:
    """Render the monitor files and name the prune set for the monitors unit.

    The catalog comes from `instance.projects()`, the ruler wiring from the
    checkout's `instance/config.yaml` (absent file or key means not wired; only
    the value `wired` counts). A file `render_monitors` refuses is logged at
    WARNING naming the file and never blocks the other monitors; its previous
    objects stay declared, so they are neither applied nor pruned.
    """
    root = state.backends.instance.require()
    catalog = state.backends.instance.projects() or {}
    files, unreadable = _read_monitor_files(root)
    result = render_monitors(files, catalog=catalog,
                             loki_ruler_wired=_loki_ruler_wired(root),
                             uptime_on=_feature_on(root, "uptime"),
                             telegram_on=_feature_on(root, "alerts-telegram"))
    invalid: set[str] = set(unreadable)
    # Still-declared log monitors whose file is invalid: an unreadable file's kind
    # is unknown, so its stem stays a candidate, exactly as it keeps the shared
    # ConfigMap prune-withheld below. A file whose body is not a mapping is
    # treated the same way: its kind is equally unknown.
    invalid_log: set[str] = set(unreadable)
    log_invalid = bool(unreadable)
    for filename, body in files.items():
        if not isinstance(body, dict):
            stem = _stem(filename)
            invalid.add(stem)
            invalid_log.add(stem)
            log_invalid = True
    for filename, message in result.errors.items():
        log.warning("monitors %s: %s (skipped, the other monitors still apply)",
                    filename, message)
        stem = _stem(filename)
        invalid.add(stem)
        body = files.get(filename)
        if isinstance(body, dict):
            name = body.get("name")
            if isinstance(name, str) and name:
                invalid.add(name)
            if body.get("kind") == "log":
                log_invalid = True
                invalid_log.add(stem)
                if isinstance(name, str) and name:
                    invalid_log.add(name)
    objects = result.objects
    dyn = state.backends.kube.dynamic
    # The last log monitor's rule. Every log monitor's rule lives in one shared ConfigMap,
    # which is rendered only while a log monitor exists. When the last one went (deleted,
    # or removed with its project) nothing rendered the ConfigMap any more, so it stayed
    # as it was, and nothing here deletes what is merely no longer rendered: the rule of
    # a monitor that no longer existed went on being evaluated by the Loki ruler
    # (v1.0.1-rc2 on the test box, after a project's removal). While the ConfigMap is on
    # the cluster and no log monitor is declared, it is rendered with no rule in it, and
    # the next apply empties it.
    if not log_invalid and not any(_is_log_configmap(ObjectRef.of(o)) for o in objects):
        stale = _get_live(dyn, log_configmap([]))
        if stale is not None and _loki_rules_in_configmap(_as_dict(stale)):
            objects.append(log_configmap([]))
            objects.sort(key=lambda o: (o["kind"], o["metadata"]["namespace"], o["metadata"]["name"]))
    refs = [ObjectRef.of(o) for o in objects]
    live_index: dict[tuple[str, str, str], dict[str, Any]] = {}
    for obj in objects:
        got = _get_live(dyn, obj)
        if got is not None:
            body = _as_dict(got)
            live_index[ObjectRef.of(body).key] = body
    _carry_forward_invalid_log_rules(objects, invalid_log, live_index)
    previous, found = read_inventory(state, UNIT, "", name=INVENTORY_NAME)
    rendered_keys = {r.key for r in refs}
    prune: list[ObjectRef] = []
    withheld: list[Withheld] = []
    for ref in previous:
        if ref.key in rendered_keys:
            continue
        if _still_declared_invalid(ref, invalid, log_invalid):
            continue
        live = live_index.get(ref.key)
        if live is None:
            got = _get_live(dyn, ref.stub())
            live = _as_dict(got) if got is not None else None
        rule = monitors_prune_rule(ref, live)
        if rule is None:
            prune.append(ref)
        else:
            withheld.append(Withheld(ref=ref, rule=rule))
    prune.sort(key=_prune_order_key)
    return Plan(
        project=UNIT, env="", namespace="", objects=objects, refs=refs,
        prune=prune, withheld=withheld, previous=previous,
        inventory_found=found, render_sha=_render_sha(objects),
        source=read_source(state), live=live_index, skipped=[],
    )


def _is_log_configmap(ref: ObjectRef) -> bool:
    return ref.kind == "ConfigMap" and ref.namespace == LOKI_NS and ref.name == LOG_CONFIGMAP


def monitors_prune_rule(ref: ObjectRef, live: dict[str, Any] | None) -> str | None:
    """The first hard rule that forbids deleting `ref`, or None when it may go."""
    if ref.kind in ("PrometheusRule", "Probe"):
        if ref.namespace != MONITORING_NS:
            return RULE_SCOPE
    elif not (ref.kind == "ConfigMap" and ref.namespace == LOKI_NS
              and ref.name == LOG_CONFIGMAP):
        return RULE_SCOPE
    if live is None:
        return RULE_ABSENT
    labels = (live.get("metadata") or {}).get("labels") or {}
    if labels.get(LABEL_COMPONENT) != COMPONENT:
        return RULE_LABEL
    return None


def apply_monitors(state: AppState, plan: Plan, *, dry_run: bool) -> ApplyResult:
    """Server-side apply every planned monitor object in order; stop at the first
    failure. A real, fully successful run writes the unit's inventory ConfigMap."""
    dry = _effective_dry_run(state, dry_run, "apply")
    dyn = state.backends.kube.dynamic
    results: list[ObjectResult] = []
    for obj, ref in zip(plan.objects, plan.refs):
        if not dry and budget_spent(state, plan.project, plan.env):
            results.append(ObjectResult(ref=ref, result="failed", reason=BUDGET_SPENT))
            return ApplyResult(dry_run=dry, ok=False, results=results)
        reason = _apply_guard(ref)
        if reason is not None:
            results.append(ObjectResult(ref=ref, result="failed", reason=reason))
            return ApplyResult(dry_run=dry, ok=False, results=results)
        try:
            _server_side_apply(dyn, obj, ref, dry=dry)
        except Exception as e:  # the dynamic client raises its own hierarchy
            results.append(ObjectResult(ref=ref, result="failed", reason=_reason(e)))
            return ApplyResult(dry_run=dry, ok=False, results=results)
        live = plan.live.get(ref.key)
        results.append(ObjectResult(
            ref=ref, result="unchanged" if _same_as_live(obj, live, plan) else "applied"))
    inventory = None
    if not dry:
        inventory = write_inventory(state, plan, name=INVENTORY_NAME)
    return ApplyResult(dry_run=dry, ok=True, results=results, inventory=inventory)


def prune_monitors(state: AppState, plan: Plan, *, dry_run: bool) -> PruneResult:
    """Delete exactly `plan.prune`, in reverse install order; stop at the first
    failure. Like the applier's prune, each object is re-read now and the hard
    rules run on that fresh copy. A successful real run rewrites the unit's
    inventory without the deleted; a dry run deletes and stores nothing."""
    dry = _effective_dry_run(state, dry_run, "prune")
    dyn = state.backends.kube.dynamic
    results: list[ObjectResult] = []
    for ref in plan.prune:
        if not dry and budget_spent(state, plan.project, plan.env):
            results.append(ObjectResult(ref=ref, result="failed", reason=BUDGET_SPENT))
            return PruneResult(dry_run=dry, ok=False, results=results)
        fresh = _live_now(dyn, ref)
        if fresh is None:
            results.append(ObjectResult(ref=ref, result="absent"))
            continue
        rule = monitors_prune_rule(ref, fresh)
        if rule is not None:
            results.append(ObjectResult(ref=ref, result="withheld", reason=rule))
            continue
        try:
            resource = dyn.resources.get(api_version=ref.api_version or "v1", kind=ref.kind)
            kwargs: dict[str, Any] = {
                "name": ref.name, "_request_timeout": KUBE_TIMEOUT,
                "body": _delete_options(fresh, dry=dry),
            }
            if ref.namespace:
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
    if not dry:
        deleted = {r.ref.key for r in results if r.result == "deleted"}
        write_inventory(state, plan, deleted=deleted, name=INVENTORY_NAME)
    return PruneResult(dry_run=dry, ok=True, results=results)


def _read_monitor_files(root: Path) -> tuple[dict[str, Any], set[str]]:
    """Filename to parsed body for `monitors/*.yaml`; stems of files that could
    not be read or parsed (still declared, so their objects are never pruned)."""
    directory = root / "monitors"
    try:
        paths = sorted(directory.glob("*.yaml")) if directory.is_dir() else []
    except OSError:
        paths = []
    files: dict[str, Any] = {}
    unreadable: set[str] = set()
    for path in paths:
        try:
            files[path.name] = yaml.safe_load(path.read_text())
        except (OSError, yaml.YAMLError) as e:
            log.warning("monitors %s: cannot read (%s); skipped", path.name, e)
            unreadable.add(path.stem)
    return files, unreadable


def _feature_on(root: Path, name: str) -> bool:
    """Whether a feature a monitor needs (uptime: the prober of a probe monitor;
    alerts-telegram: the channel of `notify: [telegram]`) is on in this checkout. A
    checkout that cannot be read as features (a tree from before the registry, a config
    the reading refuses) gates nothing, as everywhere else."""
    try:
        return features.read(root).on.get(name, True)
    except rules.FeatureError:
        return True


LOKI_APP_REL = "clusters/platform/apps/loki.yaml"


def _loki_ruler_wired(root: Path) -> bool:
    """Whether something evaluates a log monitor's rule in this checkout.

    The declaration `lokiRuler: wired` in `instance/config.yaml` counts, as before. So
    does the wiring itself: the instance's rendered Loki application carrying a ruler
    that reads local rule files, notifies Alertmanager, and mounts the monitors'
    ConfigMap. The template has rendered exactly that for every instance with the logs
    feature on since the wiring moved into it, while the declaration stayed absent by
    default, so a stock instance refused every log monitor, one of the three kinds,
    naming a switch no document explained (QA of v1.0.0, F9). An absent or unparsable
    file means not wired."""
    try:
        doc = yaml.safe_load((root / "instance" / "config.yaml").read_text())
    except (OSError, yaml.YAMLError):
        doc = None
    if isinstance(doc, dict) and doc.get("lokiRuler") == "wired":
        return True
    return _loki_app_wires_ruler(root)


def _loki_app_wires_ruler(root: Path) -> bool:
    try:
        app = yaml.safe_load((root / LOKI_APP_REL).read_text())
        values = app["spec"]["source"]["helm"]["valuesObject"]
        ruler = values["loki"]["rulerConfig"]
        binary = values["singleBinary"]
    except (OSError, yaml.YAMLError, KeyError, TypeError):
        return False
    if not isinstance(ruler, dict) or not isinstance(binary, dict):
        return False
    storage = ruler.get("storage") if isinstance(ruler.get("storage"), dict) else {}
    if not ruler.get("alertmanager_url") or storage.get("type") != "local":
        return False
    directory = str(((storage.get("local") or {}) if isinstance(storage.get("local"), dict) else {}).get("directory") or "")
    if not directory:
        return False
    carrying = set()
    for volume in binary.get("extraVolumes") or []:
        if not isinstance(volume, dict):
            continue
        sources = [volume] + list(((volume.get("projected") or {}).get("sources")) or [])
        if any(isinstance(src, dict) and (src.get("configMap") or {}).get("name") == LOG_CONFIGMAP for src in sources):
            carrying.add(volume.get("name"))
    return any(isinstance(m, dict) and m.get("name") in carrying
               and str(m.get("mountPath") or "").startswith(directory.rstrip("/") + "/")
               for m in binary.get("extraVolumeMounts") or [])


def _still_declared_invalid(ref: ObjectRef, invalid: set[str], log_invalid: bool) -> bool:
    """Whether `ref` belongs to a monitor whose file still exists but is invalid:
    the shared Loki ruler ConfigMap belongs to every invalid log monitor, and a
    rule or probe belongs to the monitor its name carries."""
    if (ref.kind == "ConfigMap" and ref.namespace == LOKI_NS
            and ref.name == LOG_CONFIGMAP):
        return log_invalid
    if ref.kind in ("PrometheusRule", "Probe") and ref.name.startswith(MONITOR_PREFIX):
        return ref.name[len(MONITOR_PREFIX):] in invalid
    return False


def _carry_forward_invalid_log_rules(
    objects: list[dict[str, Any]],
    invalid_log: set[str],
    live_index: dict[tuple[str, str, str], dict[str, Any]],
) -> None:
    """Keep each still-declared invalid log monitor's rule in the shared ConfigMap.

    Every valid log monitor renders into the one `orbit-monitors-logs` ConfigMap,
    so re-rendering from the valid monitors only would silently drop the alert of
    a monitor whose file still exists but is invalid. Each such monitor's rule
    (the one whose `alert` equals the monitor name) is carried from the live
    ConfigMap into the rendered one, keeping the renderer's sort by alert. A
    monitor with no live rule, or a missing or unparsable live ConfigMap, stays
    absent; a removed file names nothing here, so its rule is dropped. A valid
    monitor's rule always wins over a carried one with the same alert.
    """
    if not invalid_log:
        return
    rendered = next(
        (o for o in objects
         if o.get("kind") == "ConfigMap"
         and (o.get("metadata") or {}).get("namespace") == LOKI_NS
         and (o.get("metadata") or {}).get("name") == LOG_CONFIGMAP),
        None,
    )
    if rendered is None:
        return
    live_rules = _loki_rules_in_configmap(
        live_index.get(("ConfigMap", LOKI_NS, LOG_CONFIGMAP)))
    if not live_rules:
        return
    rendered_rules = _loki_rules_in_configmap(rendered)
    carried = False
    for name in sorted(invalid_log):
        if name in rendered_rules:
            continue
        rule = live_rules.get(name)
        if rule is None:
            continue
        rendered_rules[name] = deepcopy(rule)
        log.warning("monitors %s.yaml: still declared but invalid, "
                    "keeping its last applied Loki rule", name)
        carried = True
    if not carried:
        return
    data = rendered.get("data")
    if not isinstance(data, dict) or "rules.yaml" not in data:
        return
    ordered = [rendered_rules[alert] for alert in sorted(rendered_rules)]
    data["rules.yaml"] = yaml.safe_dump(
        {"groups": [{"name": LOG_GROUP, "rules": ordered}]}, sort_keys=False)


def _loki_rules_in_configmap(cm: dict[str, Any] | None) -> dict[str, dict[str, Any]]:
    """Alert name to rule for every Loki rule-group document in the ConfigMap."""
    rules: dict[str, dict[str, Any]] = {}
    if not isinstance(cm, dict):
        return rules
    data = cm.get("data")
    if not isinstance(data, dict):
        return rules
    for value in data.values():
        if not isinstance(value, str):
            continue
        try:
            doc = yaml.safe_load(value)
        except yaml.YAMLError:
            continue
        if not isinstance(doc, dict):
            continue
        groups = doc.get("groups")
        if not isinstance(groups, list):
            continue
        for group in groups:
            if not isinstance(group, dict):
                continue
            group_rules = group.get("rules")
            if not isinstance(group_rules, list):
                continue
            for rule in group_rules:
                if (isinstance(rule, dict) and isinstance(rule.get("alert"), str)
                        and rule["alert"] not in rules):
                    rules[rule["alert"]] = rule
    return rules


def _apply_guard(ref: ObjectRef) -> str | None:
    if ref.kind in ("PrometheusRule", "Probe"):
        if ref.namespace != MONITORING_NS:
            return (f"namespace {ref.namespace!r} is not {MONITORING_NS!r}: "
                    f"the monitors unit writes {ref.kind} only there")
    elif not (ref.kind == "ConfigMap" and ref.namespace == LOKI_NS
              and ref.name == LOG_CONFIGMAP):
        return f"kind {ref.kind} is not one the monitors unit writes"
    if not ref.name:
        return "object has no metadata.name"
    return None


def _stem(filename: str) -> str:
    return filename[:-len(".yaml")] if filename.endswith(".yaml") else filename


def _render_sha(objects: list[dict[str, Any]]) -> str:
    return hashlib.sha256(
        json.dumps(objects, sort_keys=True, default=str).encode()).hexdigest()


def prune_refs_for_monitor(state: AppState, plan: Plan, name: str) -> list[ObjectRef]:
    """The refs in `plan.prune` that belong to monitor `name`.

    A ref belongs to the monitor whose `orbit.local/monitor` label its live
    object carries; a ref with no live copy falls back to the renderer naming
    (`orbit-monitor-<name>`), so an already-deleted object still reports
    `absent` instead of lingering in the inventory. The shared Loki ruler
    ConfigMap carries no monitor label and is never one monitor's to delete.
    """
    dyn = state.backends.kube.dynamic
    out: list[ObjectRef] = []
    for ref in plan.prune:
        if _ref_monitor(dyn, plan, ref) == name:
            out.append(ref)
    return out


def prune_refs_for_project(state: AppState, plan: Plan, project: str) -> list[ObjectRef]:
    """The refs in `plan.prune` that belong to a monitor of `project`.

    A monitor's file names its project, but a ref in the prune set is one whose file is
    already gone. What still says whose it was is the live PrometheusRule: every rule a
    monitor renders carries the label `project`. The monitors found that way give up
    all their refs (a probe monitor's Probe carries no project of its own)."""
    dyn = state.backends.kube.dynamic
    names: set[str] = set()
    for ref in plan.prune:
        if ref.kind != "PrometheusRule":
            continue
        live = plan.live.get(ref.key)
        if live is None:
            got = _get_live(dyn, ref.stub())
            live = _as_dict(got) if got is not None else None
        if not isinstance(live, dict):
            continue
        for group in ((live.get("spec") or {}).get("groups")) or []:
            for rule in (group or {}).get("rules") or []:
                if isinstance(rule, dict) and (rule.get("labels") or {}).get("project") == project:
                    name = _ref_monitor(dyn, plan, ref)
                    if name:
                        names.add(name)
    return [ref for ref in plan.prune if _ref_monitor(dyn, plan, ref) in names]


def _ref_monitor(dyn: Any, plan: Plan, ref: ObjectRef) -> str | None:
    """The monitor a prune ref belongs to, via its live label, else its name."""
    live = plan.live.get(ref.key)
    if live is None:
        got = _get_live(dyn, ref.stub())
        live = _as_dict(got) if got is not None else None
    if isinstance(live, dict):
        labels = (live.get("metadata") or {}).get("labels") or {}
        if isinstance(labels, dict) and labels.get(LABEL_MONITOR):
            return str(labels[LABEL_MONITOR])
    if ref.kind in ("PrometheusRule", "Probe") and ref.name.startswith(MONITOR_PREFIX):
        return ref.name[len(MONITOR_PREFIX):]
    return None


__all__ = [
    "UNIT", "INVENTORY_NAME", "plan_monitors", "monitors_prune_rule",
    "apply_monitors", "prune_monitors", "prune_refs_for_monitor",
]
