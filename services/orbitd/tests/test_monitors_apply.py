"""The reconcile loop applies declared monitors as one more unit beside the pairs: its own
inventory (orbit/orbit-inventory-monitors), its own env lock, never a prune from the loop.
A confirmed prune deletes exactly the monitors whose files are gone, and only objects that
are marked as monitor objects in the namespaces monitors render into."""

from __future__ import annotations

import logging
from dataclasses import replace

import pytest
import yaml

from orbitd import monitors_apply
from orbitd.applier import ObjectRef
from orbitd.deploy import runtime_of
from orbitd.monitors_apply import (
    INVENTORY_NAME, UNIT, apply_monitors, plan_monitors, prune_monitors,
)
from orbitd.reconcile import ReconcileRunner
from orbitd.state import AppState
from orbitd.testing import FakeBackends

from test_monitors import CATALOG, log as log_monitor, metric

INV_NS = "orbit"


def world(cfg, tmp_path, monitors, *, mode="on", loki_ruler=False) -> AppState:
    backends = FakeBackends.build(tmp_path, projects=CATALOG, monitors=monitors)
    if loki_ruler:
        (backends.instance_root / "instance").mkdir(exist_ok=True)
        (backends.instance_root / "instance" / "config.yaml").write_text(
            yaml.safe_dump({"lokiRuler": "wired"}))
    state = AppState.build(cfg, backends=backends)
    # No catalog pair is enabled: these tests are about the monitors unit alone.
    state.cfg = replace(state.cfg, apply_mode=mode, apply_pairs=("none/none",))
    return state


def live(state, kind, namespace, name):
    for o in state.backends.kube.objects:
        m = o.get("metadata") or {}
        if o.get("kind") == kind and m.get("namespace") == namespace and m.get("name") == name:
            return o
    return None


def inventory_lines(state) -> list[str]:
    cm = live(state, "ConfigMap", INV_NS, INVENTORY_NAME)
    assert cm is not None, "no monitors inventory"
    return [l for l in cm["data"]["objects"].splitlines() if l.strip()]


def rule_ref(name: str) -> ObjectRef:
    return ObjectRef(api_version="monitoring.coreos.com/v1", kind="PrometheusRule",
                     namespace="monitoring", name=f"orbit-monitor-{name}")


def remove_file(state, name: str) -> None:
    (state.backends.instance_root / "monitors" / f"{name}.yaml").unlink()


def test_inventory_is_named_for_the_unit():
    assert UNIT == "monitors"
    assert INVENTORY_NAME == "orbit-inventory-monitors"


def test_loop_applies_declared_monitors_and_records_their_inventory(cfg, tmp_path):
    state = world(cfg, tmp_path, {"queue-backlog": metric()})
    ReconcileRunner(state, dry_run=False).run_once()
    rule = live(state, "PrometheusRule", "monitoring", "orbit-monitor-queue-backlog")
    assert rule is not None
    assert rule["spec"]["groups"][0]["rules"][0]["alert"] == "queue-backlog"
    assert rule_ref("queue-backlog").line in inventory_lines(state)


def test_the_monitors_tick_is_recorded_under_the_unit(cfg, tmp_path):
    state = world(cfg, tmp_path, {"queue-backlog": metric()})
    runner = ReconcileRunner(state, dry_run=False)
    runner.run_once()
    tick = runner.latest(UNIT, "")
    assert tick is not None and tick.mode == "on"
    assert tick.applied == 1 and tick.failed == 0


def test_dry_run_mode_stores_nothing(cfg, tmp_path):
    state = world(cfg, tmp_path, {"queue-backlog": metric()}, mode="dry-run")
    ReconcileRunner(state, dry_run=True).run_once()
    calls = [c for c in state.backends.kube.apply_calls if c["kind"] == "PrometheusRule"]
    assert calls and all(c["dry_run"] == "All" for c in calls)
    assert live(state, "PrometheusRule", "monitoring", "orbit-monitor-queue-backlog") is None
    assert live(state, "ConfigMap", INV_NS, INVENTORY_NAME) is None


def test_a_removed_monitor_is_reported_never_deleted_by_the_loop(cfg, tmp_path):
    state = world(cfg, tmp_path, {"queue-backlog": metric(), "keep": metric(name="keep")})
    runner = ReconcileRunner(state, dry_run=False)
    runner.run_once()
    remove_file(state, "queue-backlog")
    runner.run_once()
    assert live(state, "PrometheusRule", "monitoring", "orbit-monitor-queue-backlog") is not None
    assert runner.latest(UNIT, "").would_prune == [rule_ref("queue-backlog").line]
    assert state.backends.kube.delete_calls == []


def test_a_confirmed_prune_deletes_exactly_the_removed_monitor(cfg, tmp_path):
    state = world(cfg, tmp_path, {"queue-backlog": metric(), "keep": metric(name="keep")})
    ReconcileRunner(state, dry_run=False).run_once()
    remove_file(state, "queue-backlog")
    plan = plan_monitors(state)
    assert [r.line for r in plan.prune] == [rule_ref("queue-backlog").line]
    prune_monitors(state, plan, dry_run=False)
    assert live(state, "PrometheusRule", "monitoring", "orbit-monitor-queue-backlog") is None
    assert live(state, "PrometheusRule", "monitoring", "orbit-monitor-keep") is not None
    lines = inventory_lines(state)
    assert rule_ref("queue-backlog").line not in lines
    assert rule_ref("keep").line in lines


def test_a_dry_run_prune_deletes_nothing(cfg, tmp_path):
    state = world(cfg, tmp_path, {"queue-backlog": metric()})
    ReconcileRunner(state, dry_run=False).run_once()
    remove_file(state, "queue-backlog")
    prune_monitors(state, plan_monitors(state), dry_run=True)
    assert live(state, "PrometheusRule", "monitoring", "orbit-monitor-queue-backlog") is not None
    assert rule_ref("queue-backlog").line in inventory_lines(state)


def _foreign(state, kind, namespace, name, labels):
    state.backends.kube.add_object({
        "apiVersion": "monitoring.coreos.com/v1" if kind == "PrometheusRule" else "apps/v1",
        "kind": kind,
        "metadata": {"name": name, "namespace": namespace, "labels": labels,
                     "uid": f"uid-{name}", "resourceVersion": "1"},
        "spec": {}})
    return ObjectRef(api_version=state.backends.kube.objects[-1]["apiVersion"], kind=kind,
                     namespace=namespace, name=name)


def test_prune_withholds_anything_not_marked_as_a_monitor_object(cfg, tmp_path):
    state = world(cfg, tmp_path, {"keep": metric(name="keep")})
    ReconcileRunner(state, dry_run=False).run_once()
    unmarked = _foreign(state, "PrometheusRule", "monitoring", "orbit-monitor-hand", {})
    wrong_kind = _foreign(state, "Deployment", "monitoring", "orbit-monitor-dep",
                          {"orbit.local/component": "monitors"})
    wrong_ns = _foreign(state, "PrometheusRule", "demo-prod", "orbit-monitor-ns",
                        {"orbit.local/component": "monitors"})
    cm = live(state, "ConfigMap", INV_NS, INVENTORY_NAME)
    cm["data"]["objects"] += "\n" + "\n".join(r.line for r in (unmarked, wrong_kind, wrong_ns))
    plan = plan_monitors(state)
    assert plan.prune == []
    assert {w.ref.line for w in plan.withheld} == {unmarked.line, wrong_kind.line, wrong_ns.line}
    prune_monitors(state, plan, dry_run=False)
    for ref in (unmarked, wrong_kind, wrong_ns):
        assert live(state, ref.kind, ref.namespace, ref.name) is not None


def test_an_invalid_monitor_file_does_not_block_the_others(cfg, tmp_path, caplog):
    state = world(cfg, tmp_path, {"good": metric(name="good"),
                                  "bad": metric(name="bad", severity="critical")})
    with caplog.at_level(logging.WARNING):
        ReconcileRunner(state, dry_run=False).run_once()
    assert live(state, "PrometheusRule", "monitoring", "orbit-monitor-good") is not None
    assert live(state, "PrometheusRule", "monitoring", "orbit-monitor-bad") is None
    assert any("bad.yaml" in r.getMessage() for r in caplog.records)


def test_an_invalid_file_never_prunes_what_it_rendered_before(cfg, tmp_path):
    """A monitor that turns invalid is still declared: the loop keeps its objects and a
    confirmed prune does not treat it as removed."""
    state = world(cfg, tmp_path, {"queue-backlog": metric()})
    ReconcileRunner(state, dry_run=False).run_once()
    path = state.backends.instance_root / "monitors" / "queue-backlog.yaml"
    path.write_text(yaml.safe_dump(metric(severity="critical")))
    plan = plan_monitors(state)
    assert plan.prune == []
    prune_monitors(state, plan, dry_run=False)
    assert live(state, "PrometheusRule", "monitoring", "orbit-monitor-queue-backlog") is not None


@pytest.mark.parametrize("wired", [True, False])
def test_log_monitors_follow_the_instance_loki_ruler_flag(cfg, tmp_path, wired):
    state = world(cfg, tmp_path, {"error-burst": log_monitor()}, loki_ruler=wired)
    ReconcileRunner(state, dry_run=False).run_once()
    cm = live(state, "ConfigMap", "loki", "orbit-monitors-logs")
    assert (cm is not None) == wired


def test_the_loop_skips_monitors_while_a_job_holds_their_lock(cfg, tmp_path):
    state = world(cfg, tmp_path, {"queue-backlog": metric()})
    locks = runtime_of(state).locks
    assert locks.try_acquire(UNIT, "", "job-upsert") is None
    ReconcileRunner(state, dry_run=False).run_once()
    assert live(state, "PrometheusRule", "monitoring", "orbit-monitor-queue-backlog") is None
    locks.release(UNIT, "", "job-upsert")
    ReconcileRunner(state, dry_run=False).run_once()
    assert live(state, "PrometheusRule", "monitoring", "orbit-monitor-queue-backlog") is not None


def test_apply_monitors_is_callable_without_the_loop(cfg, tmp_path):
    state = world(cfg, tmp_path, {"queue-backlog": metric()})
    result = apply_monitors(state, plan_monitors(state), dry_run=False)
    assert result.ok
    assert live(state, "PrometheusRule", "monitoring", "orbit-monitor-queue-backlog") is not None


def _log_alerts(state) -> list[str]:
    cm = live(state, "ConfigMap", "loki", "orbit-monitors-logs")
    assert cm is not None
    (text,) = cm["data"].values()
    return [r["alert"] for g in yaml.safe_load(text)["groups"] for r in g["rules"]]


def test_an_invalid_log_monitor_keeps_its_live_rule_in_the_shared_configmap(cfg, tmp_path):
    """The Loki rules for every log monitor share one ConfigMap: a still-declared monitor
    that turns invalid must keep its last applied rule there, not vanish from it."""
    second = log_monitor(name="second", summary="second v1")
    state = world(cfg, tmp_path, {"error-burst": log_monitor(), "second": second},
                  loki_ruler=True)
    runner = ReconcileRunner(state, dry_run=False)
    runner.run_once()
    assert _log_alerts(state) == ["error-burst", "second"]
    root = state.backends.instance_root / "monitors"
    (root / "error-burst.yaml").write_text(yaml.safe_dump(log_monitor(severity="critical")))
    (root / "second.yaml").write_text(yaml.safe_dump(log_monitor(name="second", summary="second v2")))
    runner.run_once()
    assert _log_alerts(state) == ["error-burst", "second"]
    cm = live(state, "ConfigMap", "loki", "orbit-monitors-logs")
    (text,) = cm["data"].values()
    rules = {r["alert"]: r for g in yaml.safe_load(text)["groups"] for r in g["rules"]}
    assert rules["second"]["annotations"]["summary"] == "second v2", "valid changes still apply"
    assert rules["error-burst"]["labels"]["severity"] == "info", "the last good rule is kept"


def test_a_removed_log_monitor_leaves_the_shared_configmap(cfg, tmp_path):
    state = world(cfg, tmp_path, {"error-burst": log_monitor(),
                                  "second": log_monitor(name="second")}, loki_ruler=True)
    runner = ReconcileRunner(state, dry_run=False)
    runner.run_once()
    remove_file(state, "error-burst")
    runner.run_once()
    assert _log_alerts(state) == ["second"]


@pytest.mark.parametrize("text", ["null\n", "- not\n- a mapping\n", "just a string\n"])
def test_a_log_monitor_file_that_stops_being_a_mapping_keeps_its_rule(cfg, tmp_path, text):
    """Still declared (the file is there) but no longer a monitor body at all: its last good
    rule stays in the shared ConfigMap, like any other invalid log monitor's."""
    state = world(cfg, tmp_path, {"error-burst": log_monitor(),
                                  "second": log_monitor(name="second")}, loki_ruler=True)
    runner = ReconcileRunner(state, dry_run=False)
    runner.run_once()
    assert _log_alerts(state) == ["error-burst", "second"]
    (state.backends.instance_root / "monitors" / "error-burst.yaml").write_text(text)
    runner.run_once()
    assert _log_alerts(state) == ["error-burst", "second"]


# ---------------------------------------------------------------- is the ruler wired?

def _instance_with_loki(tmp_path, edit=None):
    """An instance root holding the TEMPLATE's own Loki application, its scaffold tokens
    filled the way `make init` fills them, optionally edited."""
    import re
    from pathlib import Path
    template = Path(__file__).resolve().parents[3] / "template/provisioning/clusters/platform/apps/loki.yaml"
    text = re.sub(r"\{\{[A-Za-z.]+\}\}", "fixture", template.read_text(encoding="utf-8"))
    doc = yaml.safe_load(text)
    if edit:
        edit(doc["spec"]["source"]["helm"]["valuesObject"])
    target = tmp_path / "clusters/platform/apps/loki.yaml"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(yaml.safe_dump(doc))
    return tmp_path


def test_the_stock_loki_application_is_a_wired_ruler(tmp_path):
    """QA of v1.0.0, F9: the template wires the ruler for every instance with logs on,
    and a stock instance still refused every log monitor because `lokiRuler: wired`
    was not written in its config. The wiring itself is what is read now."""
    assert monitors_apply._loki_ruler_wired(_instance_with_loki(tmp_path)) is True


def test_a_loki_application_missing_any_part_of_the_wiring_is_not_a_wired_ruler(tmp_path):
    def no_alertmanager(v): v["loki"]["rulerConfig"].pop("alertmanager_url")
    def bucket_rules(v): v["loki"]["rulerConfig"]["storage"] = {"type": "s3"}
    def no_mount(v): v["singleBinary"]["extraVolumeMounts"] = []
    def other_configmap(v): v["singleBinary"]["extraVolumes"][0]["projected"]["sources"] = [
        {"configMap": {"name": "something-else"}}]
    def mounted_elsewhere(v): v["singleBinary"]["extraVolumeMounts"][0]["mountPath"] = "/srv/rules"
    for i, edit in enumerate((no_alertmanager, bucket_rules, no_mount, other_configmap, mounted_elsewhere)):
        root = tmp_path / str(i)
        root.mkdir()
        assert monitors_apply._loki_ruler_wired(_instance_with_loki(root, edit)) is False, edit.__name__
    # no Loki application at all (the logs feature off), and one that is not YAML
    assert monitors_apply._loki_ruler_wired(tmp_path / "nothing") is False
    broken = tmp_path / "broken" / "clusters/platform/apps"
    broken.mkdir(parents=True)
    (broken / "loki.yaml").write_text("{{not: yaml")
    assert monitors_apply._loki_ruler_wired(tmp_path / "broken") is False


def test_the_declaration_still_counts_where_loki_is_wired_by_hand(tmp_path):
    (tmp_path / "instance").mkdir()
    (tmp_path / "instance" / "config.yaml").write_text(yaml.safe_dump({"lokiRuler": "wired"}))
    assert monitors_apply._loki_ruler_wired(tmp_path) is True


# ---------------------------------------------------------------- a removed project's monitors

def test_removing_a_project_deletes_its_monitors_objects_and_no_one_elses(cfg, tmp_path):
    """QA of v1.0.0, F40: orbit_project_remove left the project's monitors, files and
    rules; two minutes after a removal the removed project's uptime monitor paged. The
    removal commit takes the files; this is the step that takes the objects."""
    from test_monitors import probe
    from orbitd.tools_operator import guardrails
    other = dict(CATALOG, projects=CATALOG["projects"] + [{"id": "other", "environments": {"prod": {}}}])
    state = world(cfg, tmp_path, {"queue-backlog": metric(), "api-door": probe(),
                                  "theirs": metric(name="theirs", project="other")})
    (state.backends.instance_root / "catalog" / "projects.yaml").write_text(yaml.safe_dump(other))
    ReconcileRunner(state, dry_run=False).run_once()
    for kind, name in (("PrometheusRule", "queue-backlog"), ("PrometheusRule", "api-door"),
                       ("Probe", "api-door"), ("PrometheusRule", "theirs")):
        assert live(state, kind, "monitoring", f"orbit-monitor-{name}") is not None
    # nothing of a project whose monitor files are still there is touched
    step = guardrails._remove_monitors(state, "demo")
    assert (step.name, step.status) == ("monitors", "ok") and "no monitor of this project" in step.detail
    assert live(state, "Probe", "monitoring", "orbit-monitor-api-door") is not None
    # the removal commit: the project's monitor files are gone from the repository
    remove_file(state, "queue-backlog")
    remove_file(state, "api-door")
    step = guardrails._remove_monitors(state, "demo")
    assert (step.name, step.status) == ("monitors", "ok"), step
    assert "api-door" in step.detail and "queue-backlog" in step.detail and "3 object(s)" in step.detail
    for kind, name in (("PrometheusRule", "queue-backlog"), ("PrometheusRule", "api-door"), ("Probe", "api-door")):
        assert live(state, kind, "monitoring", f"orbit-monitor-{name}") is None, f"{kind} {name} outlived its project"
    assert live(state, "PrometheusRule", "monitoring", "orbit-monitor-theirs") is not None
    lines = inventory_lines(state)
    assert rule_ref("theirs").line in lines and rule_ref("queue-backlog").line not in lines
    # the monitors lock is given back, and a second call finds nothing left
    assert runtime_of(state).locks.try_acquire(UNIT, "", "probe") is None
    runtime_of(state).locks.release(UNIT, "", "probe")
    assert "no monitor of this project" in guardrails._remove_monitors(state, "demo").detail


def test_an_orbitd_that_applies_nothing_deletes_no_monitor(cfg, tmp_path):
    from orbitd.tools_operator import guardrails
    state = world(cfg, tmp_path, {"queue-backlog": metric()}, mode="off")
    step = guardrails._remove_monitors(state, "demo")
    assert (step.name, step.status) == ("monitors", "skipped")


# ---------------------------------------------------------------- the last log monitor's rule

def log_rules(state) -> list[str]:
    cm = live(state, "ConfigMap", "loki", "orbit-monitors-logs")
    if cm is None:
        return []
    doc = yaml.safe_load(cm["data"]["rules.yaml"]) or {}
    return [r["alert"] for g in doc.get("groups") or [] for r in g.get("rules") or []]


def test_the_last_log_monitors_rule_leaves_the_ruler_when_its_file_is_gone(cfg, tmp_path):
    """On the test box (v1.0.1-rc2) a removed project's log monitor stayed in the Loki
    ruler: every log monitor's rule lives in one shared ConfigMap, rendered only while a
    log monitor exists, and nothing deletes what is merely no longer rendered. With no
    log monitor declared the ConfigMap is rendered with no rule in it."""
    state = world(cfg, tmp_path, {"error-burst": log_monitor(), "queue-backlog": metric()}, loki_ruler=True)
    runner = ReconcileRunner(state, dry_run=False)
    runner.run_once()
    assert log_rules(state) == ["error-burst"]
    remove_file(state, "error-burst")
    runner.run_once()
    assert log_rules(state) == [], "the rule of a monitor that no longer exists is still evaluated"
    assert live(state, "PrometheusRule", "monitoring", "orbit-monitor-queue-backlog") is not None
    # and it settles: the empty ConfigMap is not rewritten on every tick
    before = len([m for m in state.backends.kube.mutations if m.get("kind") == "ConfigMap" and m.get("name") == "orbit-monitors-logs"])
    plan = monitors_apply.plan_monitors(state)
    assert not any(r.kind == "ConfigMap" and r.namespace == "loki" for r in plan.refs)
    runner.run_once()
    after = len([m for m in state.backends.kube.mutations if m.get("kind") == "ConfigMap" and m.get("name") == "orbit-monitors-logs"])
    assert after == before


def test_one_of_two_log_monitors_gone_leaves_the_other_ones_rule(cfg, tmp_path):
    state = world(cfg, tmp_path, {"error-burst": log_monitor(), "auth-fail": log_monitor(name="auth-fail")},
                  loki_ruler=True)
    runner = ReconcileRunner(state, dry_run=False)
    runner.run_once()
    assert log_rules(state) == ["auth-fail", "error-burst"]
    remove_file(state, "error-burst")
    runner.run_once()
    assert log_rules(state) == ["auth-fail"]


def test_a_log_monitor_whose_file_is_invalid_keeps_its_rule(cfg, tmp_path):
    """An invalid file is still a declared monitor: its rule is carried, never emptied."""
    state = world(cfg, tmp_path, {"error-burst": log_monitor()}, loki_ruler=True)
    runner = ReconcileRunner(state, dry_run=False)
    runner.run_once()
    (state.backends.instance_root / "monitors" / "error-burst.yaml").write_text(
        yaml.safe_dump(dict(log_monitor(), severity="loud")))
    runner.run_once()
    assert log_rules(state) == ["error-burst"]


def test_removing_a_project_takes_its_log_monitors_rule_too(cfg, tmp_path):
    from orbitd.tools_operator import guardrails
    state = world(cfg, tmp_path, {"error-burst": log_monitor(), "queue-backlog": metric()}, loki_ruler=True)
    ReconcileRunner(state, dry_run=False).run_once()
    remove_file(state, "error-burst")
    remove_file(state, "queue-backlog")
    step = guardrails._remove_monitors(state, "demo")
    assert (step.name, step.status) == ("monitors", "ok"), step
    assert "queue-backlog" in step.detail and "log monitors' rules were rewritten" in step.detail
    assert log_rules(state) == []
    assert live(state, "PrometheusRule", "monitoring", "orbit-monitor-queue-backlog") is None
