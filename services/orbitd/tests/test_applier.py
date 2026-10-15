"""The applier over the fake dynamic client: plan order, inventory, the prune rules,
the ORBITD_APPLY switch, first-failure stop, the reconcile tool, the offline fixture,
and the RBAC template held to the module's kind list."""

from __future__ import annotations

import json
from copy import deepcopy
from dataclasses import replace
from pathlib import Path

import pytest
import yaml
from conftest import MCP_URL, mint, sdk_client
from kubernetes.client.exceptions import ApiException
from mcp import Client
from mcp.client.streamable_http import streamable_http_client
from mcp_types import ElicitResult

from orbitd import applier
from orbitd.applier import (
    APPLIED_KINDS, INSTALL_ORDER, ApplyRefused, ObjectRef, apply, plan, prune,
    prune_rule, read_inventory, write_inventory,
)
from orbitd.reconcile import ReconcileRunner
from orbitd.roles import Role

REPO = Path(__file__).resolve().parents[3]
RBAC = REPO / "template" / "provisioning" / "clusters" / "platform" / "config" / "orbitd" / "rbac.yaml"
FIXTURES = Path(__file__).resolve().parent / "fixtures"

PROJECT = "demo"
ENV = "dev"
NS = f"{PROJECT}-{ENV}"
MON = "monitoring"
SHA = "f" * 64
CATALOG = {
    "id": PROJECT,
    "environments": {"prod": {"branch": "main"}, "dev": {"branch": "dev", "enabled": True}},
    "components": [{"name": "web", "kind": "web", "image": "example/web"}],
}
INSTANCE = {"app.kubernetes.io/instance": PROJECT}
PAIR = {"orbit.local/project": PROJECT, "orbit.local/env": ENV}


def obj(api_version: str, kind: str, name: str, namespace: str | None = NS,
        labels: dict | None = None, **body) -> dict:
    meta: dict = {"name": name}
    if namespace:
        meta["namespace"] = namespace
    if labels is not None:
        meta["labels"] = labels
    return {"apiVersion": api_version, "kind": kind, "metadata": meta, **body}


def rendered_set() -> list[dict]:
    """A tenant render out of order, so `plan` has something to sort."""
    return [
        obj("apps/v1", "Deployment", "demo-web", labels=INSTANCE,
            spec={"replicas": 1, "template": {"spec": {"containers": [{"name": "web", "image": "example/web:1"}]}}}),
        obj("monitoring.coreos.com/v1", "Probe", "tenant-probe", namespace=MON, labels=PAIR,
            spec={"targets": {"staticConfig": {"static": ["https://demo.example.org"]}}}),
        obj("v1", "Service", "demo-web", labels=INSTANCE, spec={"ports": [{"port": 80}]}),
        obj("v1", "ConfigMap", "grafana-dashboard-red-demo-dev", namespace=MON, labels=PAIR,
            data={"red.json": "{}"}),
        obj("v1", "ResourceQuota", "orbit-quota", spec={"hard": {"limits.memory": "1Gi"}}),
        obj("v1", "ServiceAccount", "demo", labels=INSTANCE),
        obj("networking.k8s.io/v1", "NetworkPolicy", "default-deny-ingress", spec={"podSelector": {}}),
        obj("v1", "ConfigMap", "app-config", labels=INSTANCE, data={"k": "v"}),
        obj("v1", "Namespace", NS, namespace=None, labels=PAIR),
    ]


def kinds_of(refs) -> list[str]:
    return [r.kind for r in refs]


def set_mode(state, mode: str) -> None:
    state.cfg = replace(state.cfg, apply_mode=mode)


def use_render(monkeypatch, objects: list[dict], sha: str = SHA) -> None:
    monkeypatch.setattr(applier, "render_desired", lambda state, p, e: (deepcopy(objects), sha))


def seed_live(kube, objects: list[dict]) -> None:
    for i, o in enumerate(objects):
        live = deepcopy(o)
        live["metadata"]["uid"] = f"uid-{i}"
        live["metadata"]["resourceVersion"] = "7"
        add_live(kube, live)


def add_live(kube, o: dict, *, uid: str = "uid-live", rv: str = "3") -> dict:
    """Store `o` as the API server would hand it back: with a uid and a resourceVersion."""
    o["metadata"].setdefault("uid", uid)
    o["metadata"].setdefault("resourceVersion", rv)
    return kube.add_object(o)


def seed_inventory(kube, refs: list[ObjectRef]) -> dict:
    return kube.add_object({
        "apiVersion": "v1", "kind": "ConfigMap",
        "metadata": {"name": f"orbit-inventory-{PROJECT}-{ENV}", "namespace": "orbit"},
        "data": {"objects": "\n".join(sorted(r.line for r in refs)), "render_sha": "old",
                 "commit": "old", "applied_at": "2026-01-01T00:00:00Z"},
    })


def inventory_lines(kube) -> list[str]:
    stored = [o for o in kube.objects if o["kind"] == "ConfigMap"
              and o["metadata"].get("namespace") == "orbit"
              and o["metadata"]["name"] == f"orbit-inventory-{PROJECT}-{ENV}"]
    assert len(stored) == 1, [o["metadata"] for o in stored]
    return str((stored[0].get("data") or {}).get("objects") or "").splitlines()


def _body_dry_run(call: dict) -> bool:
    """A DELETE is a dry run only when DeleteOptions.dryRun is in the body."""
    body = call.get("body")
    return isinstance(body, dict) and body.get("dryRun") == ["All"]


def dropped_component(objects: list[dict]) -> tuple[list[dict], list[dict]]:
    """The Deployment and Service of demo-web: a component leaving the render."""
    gone, kept = [], []
    for o in objects:
        if o["kind"] in ("Deployment", "Service") and o["metadata"]["name"] == "demo-web":
            gone.append(o)
        else:
            kept.append(o)
    return gone, kept


DROPPED_LINES = [
    f"apps/v1/Deployment/{NS}/demo-web",
    f"/v1/Service/{NS}/demo-web",
]


@pytest.fixture
def world(fake_backends):
    root = fake_backends.backends.instance_root
    (root / "catalog" / "projects.yaml").write_text(
        yaml.safe_dump({"version": 3, "projects": [CATALOG]}, sort_keys=False))
    return fake_backends


# ------------------------------------------------------------------ plan

def test_plan_orders_by_helm_install_order_then_namespace_and_name(world, monkeypatch):
    use_render(monkeypatch, rendered_set())
    p = plan(world, PROJECT, ENV)
    assert kinds_of(p.refs) == [
        "Namespace", "ServiceAccount", "ConfigMap", "ConfigMap",
        "ResourceQuota", "NetworkPolicy", "Service", "Deployment", "Probe",
    ]
    # Within a kind: (namespace, name); the tenant ConfigMap sorts before monitoring's.
    cms = [r for r in p.refs if r.kind == "ConfigMap"]
    assert [(r.namespace, r.name) for r in cms] == [
        (NS, "app-config"), (MON, "grafana-dashboard-red-demo-dev")]
    assert [ObjectRef.of(o) for o in p.objects] == p.refs
    assert p.render_sha == SHA and p.namespace == NS
    assert p.prune == [] and p.withheld == [] and p.inventory_found is False
    assert [r.kind for r in sorted(p.refs, key=lambda r: INSTALL_ORDER.index(r.kind))] == kinds_of(p.refs)


def test_unknown_kind_sorts_after_every_listed_kind(world, monkeypatch):
    objects = [obj("acme.io/v1", "Widget", "w"), *rendered_set()]
    use_render(monkeypatch, objects)
    p = plan(world, PROJECT, ENV)
    assert p.refs[-1].kind == "Widget"


# ------------------------------------------------------------------ inventory

def test_inventory_round_trip_then_prune_names_exactly_the_dropped_object(world, monkeypatch):
    set_mode(world, "on")
    kube = world.backends.kube
    objects = rendered_set()
    use_render(monkeypatch, objects)
    seed_live(kube, objects)
    first = plan(world, PROJECT, ENV)
    result = apply(world, first, dry_run=False)
    assert result.ok and result.dry_run is False
    assert result.inventory == ObjectRef(api_version="v1", kind="ConfigMap", namespace="orbit",
                                         name=f"orbit-inventory-{PROJECT}-{ENV}")
    stored = [o for o in kube.objects if o["kind"] == "ConfigMap"
              and o["metadata"]["namespace"] == "orbit"]
    assert len(stored) == 1
    data = stored[0]["data"]
    assert data["render_sha"] == SHA
    assert data["objects"].splitlines() == sorted(r.line for r in first.refs)
    assert "/v1/Namespace//demo-dev" in data["objects"].splitlines()
    assert "apps/v1/Deployment/demo-dev/demo-web" in data["objects"].splitlines()
    assert data["applied_at"].endswith("Z") and "commit" in data
    previous, found = read_inventory(world, PROJECT, ENV)
    assert found is True and sorted(r.line for r in previous) == sorted(r.line for r in first.refs)

    dropped = [o for o in objects if o["kind"] == "ConfigMap" and o["metadata"]["name"] == "app-config"]
    use_render(monkeypatch, [o for o in objects if o not in dropped])
    second = plan(world, PROJECT, ENV)
    assert second.inventory_found is True
    assert second.prune == [ObjectRef(api_version="v1", kind="ConfigMap", namespace=NS, name="app-config")]
    assert second.withheld == []


def test_inventory_line_round_trips_core_and_grouped_and_cluster_scoped():
    for line in ("/v1/Namespace//demo-dev", "apps/v1/Deployment/demo-dev/demo-web",
                 "monitoring.coreos.com/v1/Probe/monitoring/tenant-probe"):
        assert ObjectRef.from_line(line).line == line
    assert ObjectRef.from_line("/v1/Namespace//demo-dev").api_version == "v1"
    with pytest.raises(ValueError):
        ObjectRef.from_line("Deployment/demo-dev/demo-web")


def test_missing_inventory_means_no_prune_even_with_labelled_extras(world, monkeypatch):
    kube = world.backends.kube
    objects = rendered_set()
    use_render(monkeypatch, objects)
    seed_live(kube, objects)
    kube.add_object(obj("v1", "ConfigMap", "orphan", labels=INSTANCE, data={}))
    p = plan(world, PROJECT, ENV)
    assert p.inventory_found is False and p.previous == [] and p.prune == []


# ------------------------------------------------------------------ prune rules, one each

def _previous_with(kube, extra_ref: ObjectRef, live: dict | None) -> None:
    refs = [ObjectRef.of(o) for o in rendered_set()] + [extra_ref]
    seed_inventory(kube, refs)
    if live is not None:
        add_live(kube, live)


def _plan_with_extra(world, monkeypatch, extra: dict, *, live: dict | None):
    kube = world.backends.kube
    objects = rendered_set()
    use_render(monkeypatch, objects)
    seed_live(kube, objects)
    _previous_with(kube, ObjectRef.of(extra), live)
    return plan(world, PROJECT, ENV)


def test_prune_never_deletes_a_tls_secret(world, monkeypatch):
    secret = obj("v1", "Secret", "demo-tls", labels=INSTANCE, type="kubernetes.io/tls")
    p = _plan_with_extra(world, monkeypatch, secret, live=deepcopy(secret))
    assert p.prune == []
    assert [(w.ref.name, w.rule) for w in p.withheld] == [("demo-tls", applier.RULE_TLS)]


def test_prune_never_deletes_a_namespace(world, monkeypatch):
    old_ns = obj("v1", "Namespace", "demo-old", namespace=None, labels=PAIR)
    p = _plan_with_extra(world, monkeypatch, old_ns, live=deepcopy(old_ns))
    assert p.prune == []
    assert [(w.ref.name, w.rule) for w in p.withheld] == [("demo-old", applier.RULE_NAMESPACE_KIND)]


def test_prune_never_deletes_a_persistent_volume_claim(world, monkeypatch):
    pvc = obj("v1", "PersistentVolumeClaim", "demo-data", labels=INSTANCE, spec={})
    p = _plan_with_extra(world, monkeypatch, pvc, live=deepcopy(pvc))
    assert p.prune == []
    assert [(w.ref.name, w.rule) for w in p.withheld] == [("demo-data", applier.RULE_PVC)]


def test_prune_only_kinds_the_generator_or_chart_writes(world, monkeypatch):
    role = obj("rbac.authorization.k8s.io/v1", "Role", "demo-runner", labels=INSTANCE, rules=[])
    p = _plan_with_extra(world, monkeypatch, role, live=deepcopy(role))
    assert p.prune == []
    assert [(w.ref.name, w.rule) for w in p.withheld] == [("demo-runner", applier.RULE_KIND)]
    assert prune_rule(PROJECT, ENV, ObjectRef(api_version="v1", kind="Secret", namespace=NS, name="s"),
                      {"metadata": {"labels": INSTANCE}, "type": "Opaque"}) == applier.RULE_KIND


def test_prune_only_namespaces_guard_namespace_accepts(world, monkeypatch):
    foreign = obj("v1", "ConfigMap", "stray", namespace="kube-system", labels=INSTANCE, data={})
    p = _plan_with_extra(world, monkeypatch, foreign, live=deepcopy(foreign))
    assert p.prune == []
    assert [(w.ref.namespace, w.rule) for w in p.withheld] == [("kube-system", applier.RULE_NS)]
    # monitoring admits only the generator's kinds there
    dep = ObjectRef(api_version="apps/v1", kind="Deployment", namespace=MON, name="demo-web")
    assert prune_rule(PROJECT, ENV, dep, {"metadata": {"labels": INSTANCE}}) == applier.RULE_NS
    probe = ObjectRef(api_version="monitoring.coreos.com/v1", kind="Probe", namespace=MON, name="p")
    assert prune_rule(PROJECT, ENV, probe, {"metadata": {"labels": PAIR}}) is None


def test_prune_never_deletes_an_object_not_labelled_for_the_pair(world, monkeypatch):
    unlabelled = obj("v1", "ConfigMap", "someone-elses", labels={"app.kubernetes.io/instance": "other"}, data={})
    p = _plan_with_extra(world, monkeypatch, unlabelled, live=deepcopy(unlabelled))
    assert p.prune == []
    assert [(w.ref.name, w.rule) for w in p.withheld] == [("someone-elses", applier.RULE_LABEL)]
    other_env = ObjectRef(api_version="monitoring.coreos.com/v1", kind="Probe", namespace=MON, name="p")
    live = {"metadata": {"labels": {"orbit.local/project": PROJECT, "orbit.local/env": "prod"}}}
    assert prune_rule(PROJECT, ENV, other_env, live) == applier.RULE_LABEL
    bare = ObjectRef(api_version="v1", kind="ConfigMap", namespace=NS, name="bare")
    assert prune_rule(PROJECT, ENV, bare, {"metadata": {}}) == applier.RULE_LABEL


def test_prune_reads_live_for_an_inventory_object_the_extras_scan_missed(world, monkeypatch):
    """A previous object with no live copy is withheld as absent, never deleted blind."""
    gone = obj("v1", "ConfigMap", "gone", labels=INSTANCE, data={})
    p = _plan_with_extra(world, monkeypatch, gone, live=None)
    assert p.prune == []
    assert [(w.ref.name, w.rule) for w in p.withheld] == [("gone", applier.RULE_ABSENT)]


# ------------------------------------------------------------------ the switch

def test_switch_off_refuses_apply_and_prune_without_a_request(world, monkeypatch):
    kube = world.backends.kube
    objects = rendered_set()
    use_render(monkeypatch, objects)
    p = plan(world, PROJECT, ENV)
    assert applier.apply_mode(world) == "off"
    with pytest.raises(ApplyRefused, match="ORBITD_APPLY=off"):
        apply(world, p, dry_run=True)
    with pytest.raises(ApplyRefused, match="ORBITD_APPLY=off"):
        prune(world, p, dry_run=True)
    assert kube.apply_calls == [] and kube.delete_calls == []


def test_dry_run_mode_never_sends_a_non_dry_request(world, monkeypatch):
    set_mode(world, "dry-run")
    kube = world.backends.kube
    objects = rendered_set()
    use_render(monkeypatch, objects)
    before = deepcopy(kube.objects)
    p = plan(world, PROJECT, ENV)
    result = apply(world, p, dry_run=False)
    assert result.ok and result.dry_run is True and result.inventory is None
    assert len(kube.apply_calls) == len(p.refs) > 0
    assert all(c["dry_run"] == "All" for c in kube.apply_calls)
    assert all(c["field_manager"] == "orbit" and c["force_conflicts"] is True for c in kube.apply_calls)
    assert kube.objects == before

    stray = obj("v1", "ConfigMap", "stray", labels=INSTANCE, data={})
    add_live(kube, stray)
    p.prune = [ObjectRef.of(stray)]
    p.live[ObjectRef.of(stray).key] = stray
    pruned = prune(world, p, dry_run=False)
    assert pruned.ok and pruned.dry_run is True
    assert [r.result for r in pruned.results] == ["deleted"]
    assert kube.delete_calls and all(_body_dry_run(c) for c in kube.delete_calls)
    assert stray in kube.objects


def test_mode_on_honours_the_callers_dry_run(world, monkeypatch):
    set_mode(world, "on")
    kube = world.backends.kube
    use_render(monkeypatch, rendered_set())
    p = plan(world, PROJECT, ENV)
    result = apply(world, p, dry_run=True)
    assert result.dry_run is True and result.inventory is None
    assert all(c["dry_run"] == "All" for c in kube.apply_calls)
    assert not any(o["kind"] == "ConfigMap" and o["metadata"]["namespace"] == "orbit" for o in kube.objects)


# ------------------------------------------------------------------ apply

def test_apply_stops_at_first_failure_and_writes_no_inventory(world, monkeypatch):
    set_mode(world, "on")
    kube = world.backends.kube
    use_render(monkeypatch, rendered_set())
    p = plan(world, PROJECT, ENV)
    second = p.refs[1]
    kube.apply_errors[(second.kind, second.namespace, second.name)] = RuntimeError("Forbidden: nope")
    result = apply(world, p, dry_run=False)
    assert result.ok is False and result.inventory is None
    assert [r.result for r in result.results] == ["applied", "failed"]
    assert result.results[1].ref == second
    assert "Forbidden: nope" in (result.results[1].reason or "")
    assert len(kube.apply_calls) == 2
    assert not any(o["kind"] == "ConfigMap" and o["metadata"]["namespace"] == "orbit" for o in kube.objects)
    # the first object landed and stays: nothing is rolled back
    assert any(o["kind"] == p.refs[0].kind and o["metadata"]["name"] == p.refs[0].name for o in kube.objects)


def test_apply_classifies_unchanged_versus_applied(world, monkeypatch):
    set_mode(world, "on")
    kube = world.backends.kube
    objects = rendered_set()
    use_render(monkeypatch, objects)
    seed_live(kube, [o for o in objects if o["kind"] != "Service"])
    p = plan(world, PROJECT, ENV)
    result = apply(world, p, dry_run=False)
    assert result.ok
    by_kind = {r.ref.kind: r.result for r in result.results}
    assert by_kind["Service"] == "applied"
    assert by_kind["Deployment"] == "unchanged" and by_kind["Namespace"] == "unchanged"
    assert len(kube.apply_calls) == len(p.refs) + 1  # every object, plus the inventory


def test_apply_refuses_a_rendered_object_outside_the_pairs_namespaces(world, monkeypatch):
    set_mode(world, "on")
    kube = world.backends.kube
    objects = rendered_set() + [obj("v1", "ConfigMap", "escape", namespace="kube-system", data={})]
    use_render(monkeypatch, objects)
    p = plan(world, PROJECT, ENV)
    result = apply(world, p, dry_run=False)
    assert result.ok is False
    last = result.results[-1]
    assert last.result == "failed" and last.ref.namespace == "kube-system"
    assert "guard_namespace" in (last.reason or "")
    assert not any(c["namespace"] == "kube-system" for c in kube.apply_calls)
    assert not any(o["kind"] == "ConfigMap" and o["metadata"]["namespace"] == "orbit" for o in kube.objects)


def test_apply_refuses_a_kind_it_does_not_write_before_sending(world, monkeypatch):
    set_mode(world, "on")
    kube = world.backends.kube
    role = obj("rbac.authorization.k8s.io/v1", "Role", "demo-runner", rules=[])
    use_render(monkeypatch, rendered_set() + [role])
    p = plan(world, PROJECT, ENV)
    result = apply(world, p, dry_run=False)
    assert result.ok is False and result.results[-1].ref.kind == "Role"
    assert not any(c["kind"] == "Role" for c in kube.apply_calls)


def test_prune_deletes_exactly_the_plan_in_reverse_install_order(world, monkeypatch):
    set_mode(world, "on")
    kube = world.backends.kube
    objects = rendered_set()
    use_render(monkeypatch, objects)
    seed_live(kube, objects)
    old_cm = obj("v1", "ConfigMap", "old-cm", labels=INSTANCE, data={})
    old_dep = obj("apps/v1", "Deployment", "old-dep", labels=INSTANCE, spec={})
    add_live(kube, old_cm)
    add_live(kube, old_dep)
    seed_inventory(kube, [ObjectRef.of(o) for o in objects] + [ObjectRef.of(old_cm), ObjectRef.of(old_dep)])
    p = plan(world, PROJECT, ENV)
    assert [r.name for r in p.prune] == ["old-dep", "old-cm"]
    result = prune(world, p, dry_run=False)
    assert result.ok and [r.result for r in result.results] == ["deleted", "deleted"]
    assert [c["name"] for c in kube.delete_calls] == ["old-dep", "old-cm"]
    assert old_cm not in kube.objects and old_dep not in kube.objects
    assert len([o for o in kube.objects if o["metadata"].get("namespace") == NS]) == len(
        [o for o in objects if o["metadata"].get("namespace") == NS])


# ------------------------------------------------------------------ the tool

pytestmark_async = pytest.mark.anyio


@pytest.mark.anyio
async def test_reconcile_tool_payload_shape_on_dry_run(world, http, monkeypatch):
    set_mode(world, "dry-run")
    kube = world.backends.kube
    objects = rendered_set()
    use_render(monkeypatch, objects)
    seed_live(kube, objects)
    old = obj("v1", "ConfigMap", "old-cm", labels=INSTANCE, data={})
    add_live(kube, old)
    pvc = obj("v1", "PersistentVolumeClaim", "keep", labels=INSTANCE, spec={})
    add_live(kube, pvc)
    seed_inventory(kube, [ObjectRef.of(o) for o in objects] + [ObjectRef.of(old), ObjectRef.of(pvc)])
    async with sdk_client(http, mint(world, Role.admin)) as client:
        r = await client.call_tool("orbit_deploy_reconcile", {"project": PROJECT, "env": ENV})
    assert not r.is_error, r.content
    body = r.structured_content
    assert body["status"] == "dry_run"
    assert [s["name"] for s in body["steps"]] == ["catalog", "plan", "apply", "prune"]
    assert all(s["status"] == "ok" for s in body["steps"])
    ev = body["evidence"]
    assert ev["project"] == PROJECT and ev["env"] == ENV and ev["namespace"] == NS
    assert ev["mode"] == "dry-run" and ev["render_sha"] == SHA
    assert ev["counts"] == {"objects": len(objects), "prune": 1, "withheld": 1,
                            "previous": len(objects) + 2, "skipped": 0}
    assert ev["skipped"] == []
    assert [o["kind"] for o in ev["objects"]][:3] == ["Namespace", "ServiceAccount", "ConfigMap"]
    assert ev["prune"] == [{"api_version": "v1", "kind": "ConfigMap", "namespace": NS, "name": "old-cm"}]
    assert ev["withheld"] == [{"ref": {"api_version": "v1", "kind": "PersistentVolumeClaim",
                                       "namespace": NS, "name": "keep"},
                               "rule": applier.RULE_PVC}]
    assert ev["apply"]["dry_run"] is True and ev["apply"]["ok"] is True
    assert [x["result"] for x in ev["apply"]["results"]] == ["unchanged"] * len(objects)
    assert ev["prune_run"]["dry_run"] is True
    assert [x["result"] for x in ev["prune_run"]["results"]] == ["deleted"]
    assert ev["inventory"] == {"name": f"orbit-inventory-{PROJECT}-{ENV}", "namespace": "orbit",
                               "found": True, "written": False}
    assert body["source"]["commit"] and "lag_note" in body["source"]
    assert old in kube.objects
    assert all(c["dry_run"] == "All" for c in kube.apply_calls)
    assert all(_body_dry_run(c) for c in kube.delete_calls)


@pytest.mark.anyio
async def test_reconcile_tool_is_refused_when_the_switch_is_off_and_hidden_below_admin(world, http, monkeypatch):
    kube = world.backends.kube
    use_render(monkeypatch, rendered_set())
    async with sdk_client(http, mint(world, Role.admin)) as client:
        r = await client.call_tool("orbit_deploy_reconcile", {"project": PROJECT, "env": ENV, "dry_run": False})
    assert not r.is_error, r.content
    body = r.structured_content
    assert body["status"] == "refused"
    assert body["evidence"]["mode"] == "off" and body["evidence"]["counts"]["objects"] == 9
    apply_step = [s for s in body["steps"] if s["name"] == "apply"][0]
    assert apply_step["status"] == "skipped" and "ORBITD_APPLY=off" in apply_step["detail"]
    assert kube.apply_calls == [] and kube.delete_calls == []
    async with sdk_client(http, mint(world, Role.operator)) as client:
        listed = await client.list_tools()
    assert "orbit_deploy_reconcile" not in {t.name for t in listed.tools}


@pytest.mark.anyio
async def test_reconcile_tool_refuses_non_dry_run_when_pair_not_enabled(world, http, monkeypatch):
    """Catches: a real apply of a pair this orbitd must not apply, or a dry-run also refused."""
    set_mode(world, "on")
    world.cfg = replace(world.cfg, apply_pairs=("demo/prod",))
    kube = world.backends.kube
    use_render(monkeypatch, rendered_set())
    async with sdk_client(http, mint(world, Role.admin)) as client:
        refused = await client.call_tool(
            "orbit_deploy_reconcile", {"project": PROJECT, "env": ENV, "dry_run": False})
        assert kube.apply_calls == [] and kube.delete_calls == []
        dry = await client.call_tool(
            "orbit_deploy_reconcile", {"project": PROJECT, "env": ENV, "dry_run": True})
    assert not refused.is_error, refused.content
    body = refused.structured_content
    assert body["status"] == "refused"
    apply_step = [s for s in body["steps"] if s["name"] == "apply"][0]
    assert apply_step["status"] == "skipped"
    assert apply_step["detail"] == applier.PAIR_NOT_ENABLED_DETAIL
    assert not dry.is_error, dry.content
    assert dry.structured_content["status"] == "dry_run"


@pytest.mark.anyio
async def test_reconcile_tool_applies_then_prunes_after_confirmation(world, http, monkeypatch):
    set_mode(world, "on")
    kube = world.backends.kube
    objects = rendered_set()
    use_render(monkeypatch, objects)
    seed_live(kube, objects[:5])
    old = obj("v1", "ConfigMap", "old-cm", labels=INSTANCE, data={})
    add_live(kube, old)
    seed_inventory(kube, [ObjectRef.of(o) for o in objects] + [ObjectRef.of(old)])
    async with sdk_client(http, mint(world, Role.admin), confirm=True) as client:
        r = await client.call_tool("orbit_deploy_reconcile", {"project": PROJECT, "env": ENV, "dry_run": False})
    assert not r.is_error, r.content
    body = r.structured_content
    assert body["status"] == "applied", body
    assert [s["name"] for s in body["steps"]] == ["catalog", "plan", "apply", "inventory", "prune"]
    assert body["evidence"]["inventory"]["written"] is True
    assert body["evidence"]["prune_run"]["results"] == [
        {"ref": {"api_version": "v1", "kind": "ConfigMap", "namespace": NS, "name": "old-cm"},
         "result": "deleted", "reason": None}]
    assert old not in kube.objects
    inventory = [o for o in kube.objects if o["metadata"]["name"] == f"orbit-inventory-{PROJECT}-{ENV}"]
    assert len(inventory) == 1 and inventory[0]["data"]["render_sha"] == SHA
    assert not any(c["dry_run"] for c in kube.apply_calls + kube.delete_calls)


@pytest.mark.anyio
async def test_reconcile_tool_unknown_pair_is_not_found(world, http):
    async with sdk_client(http, mint(world, Role.admin)) as client:
        r = await client.call_tool("orbit_deploy_reconcile", {"project": PROJECT, "env": "qa"})
    assert r.is_error and r.structured_content["code"] == "not_found"


# ------------------------------------------------------------------ offline fixture

def test_offline_fixture_dry_run_plan_applies_every_object_and_prunes_nothing(world, monkeypatch):
    rendered = json.loads((FIXTURES / "proj-a-env-x-rendered.json").read_text())
    live = json.loads((FIXTURES / "proj-a-env-x-live.json").read_text())
    root = world.backends.instance_root
    (root / "catalog" / "projects.yaml").write_text(yaml.safe_dump({"version": 3, "projects": [
        {"id": "proj-a", "environments": {"env-x": {"branch": "main", "enabled": True}},
         "components": [{"name": "web", "kind": "web", "image": "example/web"}]}]}))
    set_mode(world, "dry-run")
    kube = world.backends.kube
    for o in live:
        kube.add_object(o)
    use_render(monkeypatch, rendered)
    p = plan(world, "proj-a", "env-x")
    assert len(p.refs) == len(rendered) == 6
    assert kinds_of(p.refs) == ["Namespace", "ConfigMap", "ResourceQuota",
                                "NetworkPolicy", "Instrumentation", "Probe"]
    assert p.prune == [] and p.withheld == []
    result = apply(world, p, dry_run=True)
    assert result.ok and result.dry_run is True
    assert len(result.results) == 6 and not any(r.result == "failed" for r in result.results)
    assert len(kube.apply_calls) == 6 and all(c["dry_run"] == "All" for c in kube.apply_calls)
    pruned = prune(world, p, dry_run=True)
    assert pruned.ok and pruned.results == [] and kube.delete_calls == []


# ------------------------------------------------------------------ RBAC template

WRITE_VERBS = {"create", "update", "patch", "delete", "deletecollection", "*"}
ORBITD_SUBJECT = {"kind": "ServiceAccount", "name": "orbitd", "namespace": "orbit"}


def test_rbac_template_grants_exactly_the_modules_kinds():
    """The ClusterRole is APPLIED_KINDS minus configmaps; every ConfigMap write verb in
    the template sits in a namespaced Role (orbit: inventory, monitoring: dashboards)."""
    docs = [d for d in yaml.safe_load_all(RBAC.read_text()) if d]
    roles = [d for d in docs if d.get("kind") == "ClusterRole" and d["metadata"]["name"] == "orbitd-applier"]
    assert len(roles) == 1
    granted: set[tuple[str, str]] = set()
    for rule in roles[0]["rules"]:
        assert rule["verbs"] == list(applier.APPLY_VERBS)
        assert len(rule["apiGroups"]) == 1
        for resource in rule["resources"]:
            granted.add((rule["apiGroups"][0], resource))
    assert granted == {(group, resource) for group, resource, _ in APPLIED_KINDS} - {("", "configmaps")}
    assert ("", "configmaps") not in granted
    assert "ConfigMap" in applier.KINDS  # the module still writes them: through the Roles
    assert "Role" not in applier.KINDS and "RoleBinding" not in applier.KINDS
    assert "Secret" not in applier.KINDS and "Application" not in applier.KINDS
    bindings = [d for d in docs if d.get("kind") == "ClusterRoleBinding" and d["metadata"]["name"] == "orbitd-applier"]
    assert bindings[0]["subjects"] == [ORBITD_SUBJECT]

    # no ClusterRole in the file grants a ConfigMap write verb
    for d in docs:
        if d.get("kind") != "ClusterRole":
            continue
        for rule in d.get("rules") or []:
            if "configmaps" in (rule.get("resources") or []):
                assert not WRITE_VERBS & set(rule["verbs"]), (d["metadata"]["name"], rule)

    # the three namespaced Roles, bound to orbit/orbitd, configmaps only
    by_ns = {d["metadata"]["namespace"]: d for d in docs if d.get("kind") == "Role"}
    assert set(by_ns) == {"orbit", "monitoring", "loki"}
    assert by_ns["orbit"]["rules"] == [{"apiGroups": [""], "resources": ["configmaps"],
                                        "verbs": ["get", "list", "create", "patch"]}]
    assert by_ns["monitoring"]["rules"] == [{"apiGroups": [""], "resources": ["configmaps"],
                                             "verbs": ["create", "patch", "delete"]}]
    # the monitors' shared Loki ruler ConfigMap
    assert by_ns["loki"]["rules"] == [{"apiGroups": [""], "resources": ["configmaps"],
                                       "verbs": ["create", "patch", "delete"]}]
    role_bindings = {d["metadata"]["namespace"]: d for d in docs if d.get("kind") == "RoleBinding"}
    assert set(role_bindings) == {"orbit", "monitoring", "loki"}
    for ns, rb in role_bindings.items():
        assert rb["roleRef"] == {"apiGroup": "rbac.authorization.k8s.io", "kind": "Role",
                                 "name": by_ns[ns]["metadata"]["name"]}
        assert rb["subjects"] == [ORBITD_SUBJECT]
        assert rb["metadata"]["name"].startswith("orbit")


# ------------------------------------------------------------------ review repairs

def _no_non_dry_call(kube) -> None:
    for c in kube.apply_calls:
        assert c["dry_run"] == "All", c
    for c in kube.delete_calls:
        assert _body_dry_run(c), c


@pytest.mark.anyio
@pytest.mark.parametrize("mode", ["off", "dry-run"])
async def test_switch_off_or_dry_run_records_no_non_dry_mutating_call_from_any_writer(
        world, http, monkeypatch, mode):
    """P1-1: every public writer (apply, prune, write_inventory, the tool) goes through
    the one gate. `off` raises before any request; `dry-run` sends only dryRun=All."""
    set_mode(world, mode)
    kube = world.backends.kube
    objects = rendered_set()
    use_render(monkeypatch, objects)
    seed_live(kube, objects[:4])
    old = obj("v1", "ConfigMap", "old-cm", labels=INSTANCE, data={})
    add_live(kube, old)
    seed_inventory(kube, [ObjectRef.of(o) for o in objects] + [ObjectRef.of(old)])
    before = deepcopy(kube.objects)
    p = plan(world, PROJECT, ENV)
    assert p.prune == [ObjectRef.of(old)]
    writers = {
        "apply": lambda: apply(world, p, dry_run=False),
        "prune": lambda: prune(world, p, dry_run=False),
        "write_inventory": lambda: write_inventory(world, p),
    }
    for name, call in writers.items():
        if mode == "off":
            with pytest.raises(ApplyRefused, match=f"ORBITD_APPLY=off: {name}"):
                call()
        else:
            result = call()
            assert not isinstance(result, (applier.ApplyResult, applier.PruneResult)) or result.dry_run is True
    async with sdk_client(http, mint(world, Role.admin), confirm=True) as client:
        r = await client.call_tool("orbit_deploy_reconcile", {"project": PROJECT, "env": ENV, "dry_run": False})
    assert not r.is_error, r.content
    assert r.structured_content["status"] == ("refused" if mode == "off" else "dry_run")
    if mode == "off":
        assert kube.apply_calls == [] and kube.delete_calls == []
    else:
        assert kube.apply_calls and kube.delete_calls
        _no_non_dry_call(kube)
    assert kube.objects == before
    assert old in kube.objects
    inventory = [o for o in kube.objects if o["metadata"]["name"] == f"orbit-inventory-{PROJECT}-{ENV}"]
    assert len(inventory) == 1 and inventory[0]["data"]["render_sha"] == "old"


def test_write_inventory_honours_the_callers_dry_run_when_the_switch_is_on(world, monkeypatch):
    set_mode(world, "on")
    kube = world.backends.kube
    use_render(monkeypatch, rendered_set())
    p = plan(world, PROJECT, ENV)
    ref = write_inventory(world, p, dry_run=True)
    assert ref.name == f"orbit-inventory-{PROJECT}-{ENV}"
    assert [c["dry_run"] for c in kube.apply_calls] == ["All"]
    assert not any(o["metadata"]["name"] == ref.name for o in kube.objects)


def _plan_with_old_cm(world, monkeypatch, kube):
    objects = rendered_set()
    use_render(monkeypatch, objects)
    seed_live(kube, objects)
    old = obj("v1", "ConfigMap", "old-cm", labels=dict(INSTANCE), data={})
    add_live(kube, old, uid="uid-old", rv="41")
    seed_inventory(kube, [ObjectRef.of(o) for o in objects] + [ObjectRef.of(old)])
    p = plan(world, PROJECT, ENV)
    assert p.prune == [ObjectRef.of(old)]
    return p, old


def test_prune_rereads_ownership_and_withholds_an_object_that_changed_hands(world, monkeypatch):
    """P1-2: the review's reproduction. Labels replaced between plan and prune: no DELETE."""
    set_mode(world, "on")
    kube = world.backends.kube
    p, old = _plan_with_old_cm(world, monkeypatch, kube)
    # plan's cached copy still says ours; the cluster now says someone else's
    assert p.live[ObjectRef.of(old).key]["metadata"]["labels"] == INSTANCE
    old["metadata"]["labels"] = {"app.kubernetes.io/instance": "someone-else"}
    old["metadata"]["resourceVersion"] = "42"
    result = prune(world, p, dry_run=False)
    assert result.ok is True
    assert [(r.result, r.reason) for r in result.results] == [("withheld", applier.RULE_LABEL)]
    assert kube.delete_calls == []
    assert old in kube.objects


def test_dry_run_prune_leaves_objects_and_puts_dryrun_in_the_body(world, monkeypatch):
    set_mode(world, "on")
    kube = world.backends.kube
    p, old = _plan_with_old_cm(world, monkeypatch, kube)
    result = prune(world, p, dry_run=True)
    assert result.ok and result.dry_run is True
    assert [r.result for r in result.results] == ["deleted"]
    assert old in kube.objects
    assert kube.delete_calls
    for c in kube.delete_calls:
        assert _body_dry_run(c)
        assert c["body"]["preconditions"] == {"resourceVersion": "41", "uid": "uid-old"}
        assert c.get("dry_run") is None


def test_prune_deletes_with_preconditions_from_the_fresh_read(world, monkeypatch):
    set_mode(world, "on")
    kube = world.backends.kube
    p, old = _plan_with_old_cm(world, monkeypatch, kube)
    # the object was re-created after plan: same name and labels, new identity
    old["metadata"]["uid"] = "uid-new"
    old["metadata"]["resourceVersion"] = "99"
    result = prune(world, p, dry_run=False)
    assert [r.result for r in result.results] == ["deleted"]
    assert len(kube.delete_calls) == 1
    assert kube.delete_calls[0]["body"] == {"preconditions": {"resourceVersion": "99", "uid": "uid-new"}}
    assert old not in kube.objects


def test_prune_reports_a_precondition_failure_as_withheld_and_does_not_retry(world, monkeypatch):
    set_mode(world, "on")
    kube = world.backends.kube
    p, old = _plan_with_old_cm(world, monkeypatch, kube)
    kube.delete_errors[("ConfigMap", NS, "old-cm")] = ApiException(
        status=409, reason="Conflict: the UID in the precondition does not match")
    result = prune(world, p, dry_run=False)
    assert result.ok is True
    assert [(r.result, r.reason) for r in result.results] == [("withheld", applier.RULE_CHANGED)]
    assert len(kube.delete_calls) == 1
    assert old in kube.objects


def test_fake_delete_refuses_a_stale_precondition_like_the_api_server(world):
    kube = world.backends.kube
    old = add_live(kube, obj("v1", "ConfigMap", "cm", data={}), uid="u1", rv="5")
    res = kube.dynamic.resources.get(api_version="v1", kind="ConfigMap")
    with pytest.raises(ApiException) as e:
        res.delete(name="cm", namespace=NS, body={"preconditions": {"uid": "u0", "resourceVersion": "5"}})
    assert e.value.status == 409
    assert old in kube.objects
    res.delete(name="cm", namespace=NS, body={"preconditions": {"uid": "u1", "resourceVersion": "5"}})
    assert old not in kube.objects


def test_fake_delete_ignores_dry_run_keyword_when_a_body_is_present(world):
    """A body plus dry_run=All is a real delete: the API server reads DeleteOptions
    from the body and drops the query parameter."""
    kube = world.backends.kube
    old = add_live(kube, obj("v1", "ConfigMap", "cm", data={}), uid="u1", rv="5")
    res = kube.dynamic.resources.get(api_version="v1", kind="ConfigMap")
    res.delete(
        name="cm", namespace=NS,
        body={"preconditions": {"uid": "u1", "resourceVersion": "5"}},
        dry_run="All",
    )
    assert old not in kube.objects


def test_prune_never_touches_the_plans_cached_live_copy(world, monkeypatch):
    """An object gone from the cluster since plan is `absent`, whatever plan cached."""
    set_mode(world, "on")
    kube = world.backends.kube
    p, old = _plan_with_old_cm(world, monkeypatch, kube)
    kube.objects.remove(old)
    result = prune(world, p, dry_run=False)
    assert [r.result for r in result.results] == ["absent"]
    assert kube.delete_calls == []


def orbit_rbac_docs(namespace: str = NS) -> list[dict]:
    return [
        obj("rbac.authorization.k8s.io/v1", "Role", "orbit-applier-configmaps", namespace=namespace,
            labels=INSTANCE, rules=[{"apiGroups": [""], "resources": ["configmaps"],
                                     "verbs": ["create", "patch", "delete"]}]),
        obj("rbac.authorization.k8s.io/v1", "RoleBinding", "orbit-applier-configmaps", namespace=namespace,
            labels=INSTANCE, roleRef={"kind": "Role", "name": "orbit-applier-configmaps"},
            subjects=[{"kind": "ServiceAccount", "name": "orbitd", "namespace": "orbit"}]),
    ]


def test_orbit_rbac_grant_is_never_applied_and_never_pruned(world, monkeypatch):
    """P2-1: the generator now writes orbit-applier-configmaps into every tenant
    namespace; the applier skips it on apply and a hard rule keeps it from prune."""
    set_mode(world, "on")
    kube = world.backends.kube
    objects = rendered_set() + orbit_rbac_docs()
    use_render(monkeypatch, objects)
    seed_live(kube, objects)
    seed_inventory(kube, [ObjectRef.of(o) for o in rendered_set()])
    p = plan(world, PROJECT, ENV)
    assert [(w.ref.kind, w.ref.name, w.rule) for w in p.skipped] == [
        ("Role", "orbit-applier-configmaps", applier.RULE_ORBIT_RBAC),
        ("RoleBinding", "orbit-applier-configmaps", applier.RULE_ORBIT_RBAC)]
    assert not any(r.kind in ("Role", "RoleBinding") for r in p.refs)
    result = apply(world, p, dry_run=False)
    assert result.ok
    assert not any(c["kind"] in ("Role", "RoleBinding") for c in kube.apply_calls)
    inventory = [o for o in kube.objects if o["metadata"]["name"] == f"orbit-inventory-{PROJECT}-{ENV}"][0]
    assert "Role" not in inventory["data"]["objects"]

    # in the inventory (a past bug, say), labelled for the pair, live: still withheld
    kube.objects.remove(inventory)
    seed_inventory(kube, [ObjectRef.of(o) for o in rendered_set()] + [ObjectRef.of(o) for o in orbit_rbac_docs()])
    use_render(monkeypatch, rendered_set())
    p2 = plan(world, PROJECT, ENV)
    assert p2.prune == []
    assert {(w.ref.kind, w.rule) for w in p2.withheld} == {
        ("Role", applier.RULE_ORBIT_RBAC), ("RoleBinding", applier.RULE_ORBIT_RBAC)}
    # and the rule outranks the kind rule, for every RBAC kind, by name prefix alone
    for kind in ("Role", "RoleBinding", "ClusterRole", "ClusterRoleBinding"):
        ref = ObjectRef(api_version="rbac.authorization.k8s.io/v1", kind=kind, namespace=NS, name="orbit-anything")
        assert prune_rule(PROJECT, ENV, ref, {"metadata": {"labels": INSTANCE}}) == applier.RULE_ORBIT_RBAC
    other = ObjectRef(api_version="rbac.authorization.k8s.io/v1", kind="Role", namespace=NS, name="demo-runner")
    assert prune_rule(PROJECT, ENV, other, {"metadata": {"labels": INSTANCE}}) == applier.RULE_KIND
    assert not applier.is_orbit_rbac(ObjectRef(api_version="v1", kind="ConfigMap", namespace=NS, name="orbit-x"))


@pytest.mark.anyio
async def test_reconcile_confirmation_names_every_prune_object_before_any_delete(world, http, monkeypatch):
    """P2-2: the confirmation text carries apiVersion, kind, namespace and name of each
    object to delete; nothing is deleted until it is answered; the payload lists the
    same set under `prune` before any result."""
    set_mode(world, "on")
    kube = world.backends.kube
    objects = rendered_set()
    use_render(monkeypatch, objects)
    seed_live(kube, objects)
    old_cm = obj("v1", "ConfigMap", "old-cm", labels=INSTANCE, data={})
    old_probe = obj("monitoring.coreos.com/v1", "Probe", "old-probe", namespace=MON, labels=PAIR, spec={})
    add_live(kube, old_cm, uid="u-cm")
    add_live(kube, old_probe, uid="u-probe")
    seed_inventory(kube, [ObjectRef.of(o) for o in objects] + [ObjectRef.of(old_cm), ObjectRef.of(old_probe)])
    seen: list[tuple[str, int]] = []

    def recording(answer: bool):
        async def callback(_context, params) -> ElicitResult:
            seen.append((params.message, len(kube.delete_calls)))
            return ElicitResult(action="accept", content={"confirm": answer})
        return callback

    async def call(answer: bool):
        async with http(mint(world, Role.admin)) as h:
            transport = streamable_http_client(MCP_URL, http_client=h)
            async with Client(transport, elicitation_callback=recording(answer)) as client:
                return await client.call_tool("orbit_deploy_reconcile",
                                              {"project": PROJECT, "env": ENV, "dry_run": False})

    r = await call(False)
    assert not r.is_error and r.structured_content["status"] == "refused"
    assert len(seen) == 1
    message, deletes_at_prompt = seen[0]
    assert deletes_at_prompt == 0 and kube.delete_calls == []
    assert "delete 2" in message
    assert f"monitoring.coreos.com/v1 Probe {MON}/old-probe" in message
    assert f"v1 ConfigMap {NS}/old-cm" in message
    assert old_cm in kube.objects and old_probe in kube.objects

    r = await call(True)
    assert not r.is_error, r.content
    body = r.structured_content
    assert body["status"] == "applied", body
    assert seen[1][1] == 0  # still no delete when the second prompt was answered
    ev = body["evidence"]
    keys = list(ev)
    assert keys.index("prune") < keys.index("apply") < keys.index("prune_run")
    named = [(o["api_version"], o["kind"], o["namespace"], o["name"]) for o in ev["prune"]]
    assert named == [("monitoring.coreos.com/v1", "Probe", MON, "old-probe"), ("v1", "ConfigMap", NS, "old-cm")]
    assert [tuple(x["ref"][k] for k in ("api_version", "kind", "namespace", "name"))
            for x in ev["prune_run"]["results"]] == named
    assert [c["name"] for c in kube.delete_calls] == ["old-probe", "old-cm"]
    assert all(c["body"]["preconditions"]["uid"] for c in kube.delete_calls)


# ------------------------------------------------------------------ inventory retention

def _tick_after_component_left(world, monkeypatch):
    """A loop tick whose render no longer includes demo-web. Returns (kube, tick, gone)."""
    set_mode(world, "on")
    kube = world.backends.kube
    objects = rendered_set()
    gone, kept = dropped_component(objects)
    use_render(monkeypatch, kept)
    seed_live(kube, objects)
    seed_inventory(kube, [ObjectRef.of(o) for o in objects])
    tick = ReconcileRunner(world, dry_run=False).tick(PROJECT, ENV)
    return kube, tick, gone


def _is_live(kube, ref: ObjectRef) -> bool:
    return any(ObjectRef.of(o).key == ref.key for o in kube.objects)


def test_loop_tick_keeps_dropped_entries_and_reports_would_prune(world, monkeypatch):
    """The break: the loop rewrites the inventory to this tick's render, so the
    objects it would prune can never be named again."""
    kube, tick, gone = _tick_after_component_left(world, monkeypatch)
    assert tick is not None
    assert kube.delete_calls == []
    for o in gone:
        assert _is_live(kube, ObjectRef.of(o))
    lines = inventory_lines(kube)
    for line in DROPPED_LINES:
        assert line in lines
    assert tick.would_prune == DROPPED_LINES
    data = [o for o in kube.objects if o["metadata"]["name"] == f"orbit-inventory-{PROJECT}-{ENV}"][0]["data"]
    assert data["render_sha"] == SHA
    assert "commit" in data and data["applied_at"].endswith("Z")


def test_second_loop_tick_does_not_duplicate_retained_entries(world, monkeypatch):
    kube, tick, gone = _tick_after_component_left(world, monkeypatch)
    assert tick is not None
    second = ReconcileRunner(world, dry_run=False).tick(PROJECT, ENV)
    assert second is not None
    assert second.would_prune == DROPPED_LINES
    lines = inventory_lines(kube)
    for line in DROPPED_LINES:
        assert lines.count(line) == 1
    assert len(lines) == len(set(lines))
    for o in gone:
        assert _is_live(kube, ObjectRef.of(o))
    assert kube.delete_calls == []


@pytest.mark.anyio
async def test_explicit_reconcile_after_a_tick_prunes_the_retained_entries(
        world, http, monkeypatch):
    """The break: after a tick dropped the entries from the inventory, the explicit
    job's prune set (previous minus render) is empty and the objects stay forever."""
    kube, tick, gone = _tick_after_component_left(world, monkeypatch)
    assert tick is not None and tick.would_prune == DROPPED_LINES
    async with sdk_client(http, mint(world, Role.admin), confirm=True) as client:
        r = await client.call_tool(
            "orbit_deploy_reconcile", {"project": PROJECT, "env": ENV, "dry_run": False})
    assert not r.is_error, r.content
    body = r.structured_content
    assert body["status"] == "applied", body
    named = [(o["api_version"], o["kind"], o["namespace"], o["name"]) for o in body["evidence"]["prune"]]
    assert named == [
        ("apps/v1", "Deployment", NS, "demo-web"),
        ("v1", "Service", NS, "demo-web"),
    ]
    assert [x["result"] for x in body["evidence"]["prune_run"]["results"]] == ["deleted", "deleted"]
    for o in gone:
        assert not _is_live(kube, ObjectRef.of(o))
    lines = inventory_lines(kube)
    for line in DROPPED_LINES:
        assert line not in lines
    gone_keys = {ObjectRef.of(o).key for o in gone}
    for o in rendered_set():
        ref = ObjectRef.of(o)
        if ref.key in gone_keys:
            continue
        assert ref.line in lines


@pytest.mark.anyio
async def test_dry_run_reconcile_leaves_the_prune_set_until_confirmed(world, http, monkeypatch):
    """A dry-run prune reports the set as would-delete and leaves every object;
    the confirmed run then deletes exactly those objects."""
    set_mode(world, "on")
    kube = world.backends.kube
    objects = rendered_set()
    gone, kept = dropped_component(objects)
    use_render(monkeypatch, kept)
    seed_live(kube, objects)
    seed_inventory(kube, [ObjectRef.of(o) for o in objects])
    async with sdk_client(http, mint(world, Role.admin)) as client:
        r = await client.call_tool(
            "orbit_deploy_reconcile", {"project": PROJECT, "env": ENV, "dry_run": True})
    assert not r.is_error, r.content
    body = r.structured_content
    assert body["status"] == "dry_run"
    named = [(o["api_version"], o["kind"], o["namespace"], o["name"]) for o in body["evidence"]["prune"]]
    assert named == [
        ("apps/v1", "Deployment", NS, "demo-web"),
        ("v1", "Service", NS, "demo-web"),
    ]
    assert [x["result"] for x in body["evidence"]["prune_run"]["results"]] == ["deleted", "deleted"]
    assert body["evidence"]["prune_run"]["dry_run"] is True
    for o in gone:
        assert _is_live(kube, ObjectRef.of(o))
    assert kube.delete_calls
    for c in kube.delete_calls:
        assert _body_dry_run(c)
    dry_names = [(c["kind"], c["name"]) for c in kube.delete_calls]

    kube.delete_calls.clear()
    async with sdk_client(http, mint(world, Role.admin), confirm=True) as client:
        r = await client.call_tool(
            "orbit_deploy_reconcile", {"project": PROJECT, "env": ENV, "dry_run": False})
    assert not r.is_error, r.content
    body = r.structured_content
    assert body["status"] == "applied", body
    assert [x["result"] for x in body["evidence"]["prune_run"]["results"]] == ["deleted", "deleted"]
    for o in gone:
        assert not _is_live(kube, ObjectRef.of(o))
    assert [(c["kind"], c["name"]) for c in kube.delete_calls] == dry_names
    for c in kube.delete_calls:
        assert not _body_dry_run(c)


@pytest.mark.anyio
async def test_a_failed_or_withheld_delete_keeps_its_inventory_entry(world, http, monkeypatch):
    """The break: the post-prune inventory write treats the prune set as deleted, so
    an entry whose delete failed or was withheld is dropped and can never be retried."""
    set_mode(world, "on")
    kube = world.backends.kube
    objects = rendered_set()
    use_render(monkeypatch, objects)
    seed_live(kube, objects)
    withheld = obj("v1", "ConfigMap", "aaa-withheld", labels=INSTANCE, data={})
    dropped = obj("v1", "ConfigMap", "mmm-drop", labels=INSTANCE, data={})
    failed = obj("v1", "ConfigMap", "zzz-failed", labels=INSTANCE, data={})
    add_live(kube, withheld, uid="uid-w", rv="1")
    add_live(kube, dropped, uid="uid-d", rv="1")
    add_live(kube, failed, uid="uid-f", rv="1")
    seed_inventory(kube, [ObjectRef.of(o) for o in objects]
                   + [ObjectRef.of(withheld), ObjectRef.of(dropped), ObjectRef.of(failed)])
    kube.delete_errors[("ConfigMap", NS, "aaa-withheld")] = ApiException(
        status=409, reason="Conflict: the UID in the precondition does not match")
    kube.delete_errors[("ConfigMap", NS, "zzz-failed")] = RuntimeError("apiserver timeout")
    async with sdk_client(http, mint(world, Role.admin), confirm=True) as client:
        r = await client.call_tool(
            "orbit_deploy_reconcile", {"project": PROJECT, "env": ENV, "dry_run": False})
    assert not r.is_error, r.content
    body = r.structured_content
    assert body["status"] == "failed", body
    by_name = {x["ref"]["name"]: x["result"] for x in body["evidence"]["prune_run"]["results"]}
    assert by_name["aaa-withheld"] == "withheld"
    assert by_name["mmm-drop"] == "deleted"
    assert by_name["zzz-failed"] == "failed"
    lines = inventory_lines(kube)
    assert f"/v1/ConfigMap/{NS}/aaa-withheld" in lines
    assert f"/v1/ConfigMap/{NS}/zzz-failed" in lines
    assert f"/v1/ConfigMap/{NS}/mmm-drop" not in lines
    assert withheld in kube.objects and failed in kube.objects
    assert dropped not in kube.objects


# ------------------------------------------------------------------ the quota, lowered late

def _quota(**hard) -> dict:
    return obj("v1", "ResourceQuota", "orbit-quota", spec={"hard": {k.replace("_", "."): v for k, v in hard.items()}})


def _deployment(*, generation=3, observed=3, want=1, replicas=1, updated=1, available=1) -> dict:
    d = obj("apps/v1", "Deployment", "web", spec={"replicas": want})
    d["metadata"]["generation"] = generation
    d["status"] = {"observedGeneration": observed, "replicas": replicas,
                   "updatedReplicas": updated, "availableReplicas": available}
    return d


def test_a_quota_is_not_lowered_while_the_workloads_are_still_rolling():
    """A component given less memory lowers the derived quota in the same commit. Applied
    at once it refused the rollout's new pod while the old, larger one still ran, and the
    namespace stayed jammed (the rebuilt box, 2026-10-09: 1Gi back to 256Mi; docs/V1.md
    §8). Lowered keys keep their live value until the workloads are settled."""
    live = _quota(limits_memory="2112Mi", requests_memory="2112Mi", requests_cpu="1", pods="7")
    want = _quota(limits_memory="1344Mi", requests_memory="1344Mi", requests_cpu="1500m", pods="7")
    now = applier.quota_for_now(want, live, settled=False)
    assert now["spec"]["hard"] == {"limits.memory": "2112Mi", "requests.memory": "2112Mi",
                                   "requests.cpu": "1500m", "pods": "7"}   # lowered kept, raised taken
    assert want["spec"]["hard"]["limits.memory"] == "1344Mi", "the rendered object was changed in place"
    assert applier.quota_for_now(want, live, settled=True) is want
    assert applier.quota_for_now(want, None, settled=False) is want


def test_a_quota_is_raised_at_once_and_units_are_read_as_quantities():
    live = _quota(limits_memory="1Gi", requests_cpu="500m")
    assert applier.quota_for_now(_quota(limits_memory="1024Mi", requests_cpu="0.5"), live, settled=False)["spec"]["hard"] \
        == {"limits.memory": "1024Mi", "requests.cpu": "0.5"}                 # equal: as rendered
    assert applier.quota_for_now(_quota(limits_memory="2Gi", requests_cpu="1"), live, settled=False)["spec"]["hard"] \
        == {"limits.memory": "2Gi", "requests.cpu": "1"}
    assert applier.quota_for_now(_quota(limits_memory="512Mi", requests_cpu="250m"), live, settled=False)["spec"]["hard"] \
        == {"limits.memory": "1Gi", "requests.cpu": "500m"}


def test_a_deployment_is_settled_only_when_its_rollout_is_over():
    assert applier.deployment_settled(_deployment())
    assert not applier.deployment_settled(None)
    assert not applier.deployment_settled(_deployment(generation=4, observed=3))      # not seen yet
    assert not applier.deployment_settled(_deployment(replicas=2, updated=1))         # an old pod is left
    assert not applier.deployment_settled(_deployment(updated=0))                     # the new pod was refused
    assert not applier.deployment_settled(_deployment(available=0))
    assert applier.deployment_settled(_deployment(want=0, replicas=0, updated=0, available=0))


def _sized(memory: str, *, stamp: str | None = None, cpu: str = "250m", **status) -> dict:
    d = _deployment(**status)
    d["spec"]["template"] = {"metadata": {"annotations": {"orbit.dev/restarted-at": stamp} if stamp else {}},
                             "spec": {"containers": [{"name": "web", "resources": {
                                 "requests": {"cpu": cpu, "memory": memory}, "limits": {"memory": memory}}}]}}
    return d


def _plan_of(rendered: dict, live: dict | None):
    ref = ObjectRef.of(rendered)
    return applier.Plan(project=PROJECT, env=ENV, namespace=NS, objects=[rendered], refs=[ref], prune=[], withheld=[],
                        previous=[], inventory_found=True, render_sha=SHA, source=None,
                        live={ref.key: live} if live is not None else {})


def test_a_restart_stamp_does_not_keep_the_workloads_from_being_settled():
    """A Deployment restarted through orbitd keeps the restart's stamp for good, and is
    never identical to its render again. Settled is about what the pods ask for: with the
    whole object compared, that namespace's quota was never lowered after any restart (the
    rebuilt box, 2026-10-11: 64Mi up for good; docs/V1.md §8)."""
    rendered = _sized("256Mi")
    assert applier.workloads_settled(_plan_of(rendered, _sized("256Mi", stamp="2026-10-10T05:21:05Z")))
    assert applier.workloads_settled(_plan_of(_sized("1Gi"), _sized("1024Mi", cpu="0.25")))       # units
    assert not applier.workloads_settled(_plan_of(rendered, _sized("1Gi")))                         # the old size still live
    assert not applier.workloads_settled(_plan_of(rendered, _sized("256Mi", updated=0)))            # rolling
    assert not applier.workloads_settled(_plan_of(rendered, None))                                  # not there yet
    assert applier.workloads_settled(_plan_of(_quota(limits_memory="1Gi"), None))                   # no Deployment at all
