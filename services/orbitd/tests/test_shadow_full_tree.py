"""Shadow render of the generated tenant tree: namespaces, dashboard, probes."""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from copy import deepcopy
from pathlib import Path

import pytest
import yaml
from conftest import mint, sdk_client
from test_shadow import HELM, _application

from orbitd.render import render
from orbitd.roles import Role
from orbitd.shadow import _live_index, compare_objects, diff

REPO = Path(__file__).resolve().parents[3]
CHART = REPO / "charts" / "service"
FIXTURES = Path(__file__).resolve().parent / "fixtures"

PROJ_A = "proj-a"
PROJ_B = "proj-b"
ENV_X = "env-x"
ENV_Y = "env-y"
ENV_Z = "env-z"
NS_AX = f"{PROJ_A}-{ENV_X}"
NS_AY = f"{PROJ_A}-{ENV_Y}"
NS_BX = f"{PROJ_B}-{ENV_X}"
MONITORING = "monitoring"

PAIR_VALUES = {
    "project": PROJ_A,
    "env": ENV_X,
    "hostnames": ["proj-a.example.org"],
    "components": [{
        "name": "web",
        "kind": "web",
        "image": "example/web",
        "port": 8080,
        "path": "/",
        "probePath": "/healthz",
        "replicas": 1,
    }],
}

EXTRA_CM = {
    "apiVersion": "v1",
    "kind": "ConfigMap",
    "metadata": {"name": "extra-cm", "namespace": NS_AX},
    "data": {"k": "v"},
}

TENANT_KEYS = {
    ("Namespace", "", NS_AX),
    ("ResourceQuota", NS_AX, "orbit-quota"),
    ("Instrumentation", NS_AX, "orbit"),
    ("NetworkPolicy", NS_AX, "default-deny-ingress"),
    ("ConfigMap", MONITORING, "grafana-dashboard-red-proj-a-env-x"),
    ("Probe", MONITORING, "other-check-ax"),
}

OTHER_KEYS = {
    ("Namespace", "", NS_AY),
    ("ResourceQuota", NS_AY, "orbit-quota"),
    ("Instrumentation", NS_AY, "orbit"),
    ("NetworkPolicy", NS_AY, "default-deny-ingress"),
    ("ConfigMap", MONITORING, "grafana-dashboard-red-proj-a-env-y"),
    ("Probe", MONITORING, "other-check-ay"),
    ("Probe", MONITORING, "tenant-probe-wrong-env"),
    ("Namespace", "", NS_BX),
    ("ResourceQuota", NS_BX, "orbit-quota"),
}

HELM_KIND_COUNT = 5  # ConfigMap extra-cm, Deployment, Ingress, Service, ServiceAccount


def _obj_key(obj: dict) -> tuple[str, str, str]:
    meta = obj.get("metadata") or {}
    return (
        str(obj.get("kind") or ""),
        str(meta.get("namespace") or ""),
        str(meta.get("name") or ""),
    )


def _namespace(name: str, project: str, env: str) -> dict:
    return {
        "apiVersion": "v1",
        "kind": "Namespace",
        "metadata": {
            "name": name,
            "labels": {"orbit.local/project": project, "orbit.local/env": env},
        },
    }


def _quota(ns: str) -> dict:
    return {
        "apiVersion": "v1",
        "kind": "ResourceQuota",
        "metadata": {"name": "orbit-quota", "namespace": ns},
        "spec": {"hard": {"pods": "6"}},
    }


def _instrumentation(ns: str) -> dict:
    return {
        "apiVersion": "opentelemetry.io/v1alpha1",
        "kind": "Instrumentation",
        "metadata": {"name": "orbit", "namespace": ns},
        "spec": {"exporter": {"endpoint": "http://tempo.tempo.svc:4318"}},
    }


def _netpol(ns: str) -> dict:
    return {
        "apiVersion": "networking.k8s.io/v1",
        "kind": "NetworkPolicy",
        "metadata": {"name": "default-deny-ingress", "namespace": ns},
        "spec": {"podSelector": {}, "policyTypes": ["Ingress"]},
    }


def _ns_bundle(name: str, project: str, env: str) -> list[dict]:
    return [
        _namespace(name, project, env),
        _quota(name),
        _instrumentation(name),
        _netpol(name),
    ]


def _dashboard(project: str, env: str) -> dict:
    return {
        "apiVersion": "v1",
        "kind": "ConfigMap",
        "metadata": {
            "name": f"grafana-dashboard-red-{project}-{env}",
            "namespace": MONITORING,
            "labels": {"grafana_dashboard": "1"},
        },
        "data": {f"red-{project}-{env}.json": "{}"},
    }


def _probe(name: str, project: str, env: str) -> dict:
    return {
        "apiVersion": "monitoring.coreos.com/v1",
        "kind": "Probe",
        "metadata": {
            "name": name,
            "namespace": MONITORING,
            "labels": {"orbit.local/project": project, "orbit.local/env": env},
        },
        "spec": {"module": "http_2xx"},
    }


def seed_full_tree(state) -> Path:
    root = state.backends.instance_root
    (root / "catalog" / "projects.yaml").write_text(yaml.safe_dump({
        "version": 3,
        "projects": [
            {
                "id": PROJ_A, "name": "Proj A",
                "environments": {
                    ENV_X: {"branch": "main"},
                    ENV_Y: {"branch": "dev", "enabled": True},
                },
                "components": [{"name": "web", "kind": "web", "image": "example/web"}],
            },
            {
                "id": PROJ_B, "name": "Proj B",
                "environments": {ENV_X: {"branch": "main"}},
                "components": [{"name": "web", "kind": "web", "image": "example/web"}],
            },
        ],
    }, sort_keys=False))
    dest_chart = root / "charts" / "service"
    if dest_chart.exists():
        shutil.rmtree(dest_chart)
    shutil.copytree(CHART, dest_chart)
    apps = root / "clusters" / "apps"
    apps.mkdir(parents=True, exist_ok=True)
    docs = [_application(PROJ_A, ENV_X, PAIR_VALUES), EXTRA_CM]
    (apps / f"{PROJ_A}-{ENV_X}.yaml").write_text(
        yaml.safe_dump_all(docs, sort_keys=False))
    ns_docs = (
        _ns_bundle(NS_AX, PROJ_A, ENV_X)
        + _ns_bundle(NS_AY, PROJ_A, ENV_Y)
        + _ns_bundle(NS_BX, PROJ_B, ENV_X)
    )
    (apps / "namespaces.yaml").write_text(yaml.safe_dump_all(ns_docs, sort_keys=False))
    (apps / f"dashboard-{PROJ_A}-{ENV_X}.yaml").write_text(
        yaml.safe_dump(_dashboard(PROJ_A, ENV_X), sort_keys=False))
    (apps / f"dashboard-{PROJ_A}-{ENV_Y}.yaml").write_text(
        yaml.safe_dump(_dashboard(PROJ_A, ENV_Y), sort_keys=False))
    # Filename is not the selector: matching labels in a non-tenant-probe-* file count.
    (apps / "other-check.yaml").write_text(yaml.safe_dump_all([
        _probe("other-check-ax", PROJ_A, ENV_X),
        _probe("other-check-ay", PROJ_A, ENV_Y),
    ], sort_keys=False))
    (apps / "tenant-probe-deadbeef.yaml").write_text(
        yaml.safe_dump(_probe("tenant-probe-wrong-env", PROJ_A, ENV_Z), sort_keys=False))
    return root


@pytest.fixture
def full_world(fake_backends):
    seed_full_tree(fake_backends)
    return fake_backends


def _load_fixture(name: str) -> list[dict]:
    return json.loads((FIXTURES / name).read_text())


@pytest.mark.skipif(not HELM, reason="helm not on PATH")
def test_render_fixture_tree_returns_only_that_pair(full_world):
    objects, sha = render(full_world, PROJ_A, ENV_X)
    keys = {_obj_key(o) for o in objects}
    assert TENANT_KEYS <= keys
    assert keys.isdisjoint(OTHER_KEYS)
    assert len(objects) == HELM_KIND_COUNT + len(TENANT_KEYS)
    assert len(sha) == 64
    dash = next(
        o for o in objects
        if o.get("kind") == "ConfigMap"
        and (o.get("metadata") or {}).get("name") == "grafana-dashboard-red-proj-a-env-x"
    )
    assert (dash.get("metadata") or {}).get("namespace") == MONITORING
    assert ((dash.get("metadata") or {}).get("labels") or {}).get("grafana_dashboard") == "1"
    assert "orbit.local/project" not in ((dash.get("metadata") or {}).get("labels") or {})


@pytest.mark.skipif(not HELM, reason="helm not on PATH")
def test_render_probe_with_right_project_wrong_env_is_excluded(full_world):
    objects, _ = render(full_world, PROJ_A, ENV_X)
    names = {
        (o.get("metadata") or {}).get("name")
        for o in objects if o.get("kind") == "Probe"
    }
    assert names == {"other-check-ax"}
    assert "tenant-probe-wrong-env" not in names
    assert "other-check-ay" not in names


def test_compare_new_kinds_empty_when_live_has_listed_defaults():
    rendered = _load_fixture("proj-a-env-x-rendered.json")
    live = _load_fixture("proj-a-env-x-live.json")
    ns_live = next(o for o in live if o.get("kind") == "Namespace")
    ns_labels = (ns_live.get("metadata") or {}).get("labels") or {}
    assert ns_labels.get("kubernetes.io/metadata.name") == NS_AX
    assert (ns_live.get("metadata") or {}).get("managedFields")
    assert (ns_live.get("metadata") or {}).get("annotations", {}).get(
        "argocd.argoproj.io/tracking-id",
    )
    report = compare_objects(rendered, live, project=PROJ_A, env=ENV_X)
    assert report.empty is True
    assert report.missing == []
    assert report.extra == []
    assert report.changed == []
    rendered_keys = {(r.kind, r.namespace, r.name) for r in report.rendered}
    matched_keys = {(r.kind, r.namespace, r.name) for r in report.live_matched}
    assert rendered_keys == matched_keys
    assert ("Namespace", "", NS_AX) in rendered_keys
    assert ("Probe", MONITORING, "tenant-probe-ax") in rendered_keys
    assert ("ConfigMap", MONITORING, "grafana-dashboard-red-proj-a-env-x") in rendered_keys
    assert ("ResourceQuota", NS_AX, "orbit-quota") in rendered_keys
    assert ("Instrumentation", NS_AX, "orbit") in rendered_keys


def test_namespace_name_label_is_ignored_only_on_namespace():
    rendered = {
        "apiVersion": "v1", "kind": "ConfigMap",
        "metadata": {"name": "cm", "namespace": NS_AX},
        "data": {"k": "v"},
    }
    live = deepcopy(rendered)
    live.setdefault("metadata", {}).setdefault("labels", {})[
        "kubernetes.io/metadata.name"
    ] = "not-the-name"
    report = compare_objects([rendered], [live], project=PROJ_A, env=ENV_X)
    assert report.empty is False
    assert report.changed
    assert "/metadata/labels/kubernetes.io~1metadata.name" in report.changed[0].paths

    ns_rendered = {
        "apiVersion": "v1", "kind": "Namespace",
        "metadata": {"name": NS_AX, "labels": {"orbit.local/project": PROJ_A}},
    }
    ns_live = deepcopy(ns_rendered)
    ns_live["metadata"]["labels"]["kubernetes.io/metadata.name"] = "other-value"
    ns_live["spec"] = {"finalizers": ["kubernetes"]}
    ns_report = compare_objects(
        [ns_rendered], [ns_live], project=PROJ_A, env=ENV_X,
    )
    assert ns_report.empty is True
    assert ns_report.changed == []


def test_shadow_offline_new_kinds_stays_empty():
    proc = subprocess.run(
        [
            sys.executable, "-m", "orbitd.shadow_offline",
            str(FIXTURES / "proj-a-env-x-rendered.json"),
            str(FIXTURES / "proj-a-env-x-live.json"),
        ],
        capture_output=True, text=True,
        cwd=str(Path(__file__).resolve().parents[1]),
    )
    assert proc.returncode == 0, proc.stderr
    body = json.loads(proc.stdout)
    assert body["empty"] is True
    assert body["missing"] == []
    assert body["extra"] == []
    assert body["changed"] == []
    assert "rendered" in body and "live_matched" in body
    assert len(body["rendered"]) == len(body["live_matched"]) == 6


def test_monitoring_extras_scan_uses_project_env_selector(fake_backends):
    rendered = [
        {
            "apiVersion": "v1", "kind": "ConfigMap",
            "metadata": {
                "name": "keep", "namespace": NS_AX,
                "labels": {"app.kubernetes.io/instance": PROJ_A},
            },
            "data": {"k": "v"},
        },
        {
            "apiVersion": "monitoring.coreos.com/v1", "kind": "Probe",
            "metadata": {
                "name": "keep-probe", "namespace": MONITORING,
                "labels": {"orbit.local/project": PROJ_A, "orbit.local/env": ENV_X},
            },
        },
    ]
    kube = fake_backends.backends.kube
    for obj in rendered:
        kube.add_object(deepcopy(obj))
    kube.add_object({
        "apiVersion": "monitoring.coreos.com/v1", "kind": "Probe",
        "metadata": {
            "name": "stray-probe", "namespace": MONITORING,
            "labels": {"orbit.local/project": PROJ_A, "orbit.local/env": ENV_X},
        },
    })
    kube.add_object({
        "apiVersion": "monitoring.coreos.com/v1", "kind": "Probe",
        "metadata": {
            "name": "instance-only", "namespace": MONITORING,
            "labels": {"app.kubernetes.io/instance": PROJ_A},
        },
    })
    kube.add_object({
        "apiVersion": "v1", "kind": "Service",
        "metadata": {
            "name": "stray-svc", "namespace": NS_AX,
            "labels": {"app.kubernetes.io/instance": PROJ_A},
        },
        "spec": {"ports": [{"port": 80}]},
    })
    found = _live_index(fake_backends, rendered, PROJ_A, ENV_X)
    assert ("Probe", MONITORING, "stray-probe") in found
    assert ("Probe", MONITORING, "instance-only") not in found
    assert ("Service", NS_AX, "stray-svc") in found
    list_calls = [
        c for c in kube.dynamic_calls
        if c.get("name") is None and c.get("label_selector")
    ]
    selectors = {(c["namespace"], c["label_selector"]) for c in list_calls}
    assert (NS_AX, f"app.kubernetes.io/instance={PROJ_A}") in selectors
    assert (
        MONITORING,
        f"orbit.local/project={PROJ_A},orbit.local/env={ENV_X}",
    ) in selectors


def test_compare_flags_monitoring_labelled_extra():
    rendered = [{
        "apiVersion": "monitoring.coreos.com/v1", "kind": "Probe",
        "metadata": {
            "name": "keep-probe", "namespace": MONITORING,
            "labels": {"orbit.local/project": PROJ_A, "orbit.local/env": ENV_X},
        },
    }]
    stray = {
        "apiVersion": "monitoring.coreos.com/v1", "kind": "Probe",
        "metadata": {
            "name": "stray-probe", "namespace": MONITORING,
            "labels": {"orbit.local/project": PROJ_A, "orbit.local/env": ENV_X},
        },
    }
    instance_only = {
        "apiVersion": "v1", "kind": "ConfigMap",
        "metadata": {
            "name": "instance-cm", "namespace": MONITORING,
            "labels": {"app.kubernetes.io/instance": PROJ_A},
        },
        "data": {"k": "v"},
    }
    report = compare_objects(
        rendered, [deepcopy(rendered[0]), stray, instance_only],
        project=PROJ_A, env=ENV_X,
    )
    assert [e.name for e in report.extra] == ["stray-probe"]
    assert report.empty is False


@pytest.mark.skipif(not HELM, reason="helm not on PATH")
def test_diff_lists_added_objects_on_rendered_and_live_matched(full_world):
    objects, _ = render(full_world, PROJ_A, ENV_X)
    kube = full_world.backends.kube
    for obj in objects:
        live = deepcopy(obj)
        if live.get("kind") == "Namespace":
            live.setdefault("spec", {})["finalizers"] = ["kubernetes"]
            live["status"] = {"phase": "Active"}
        elif live.get("kind") in {"ResourceQuota", "Instrumentation"}:
            live["status"] = {"observed": True}
        kube.add_object(live)
    report = diff(full_world, PROJ_A, ENV_X)
    assert report.empty is True
    rendered_keys = {(r.kind, r.namespace, r.name) for r in report.rendered}
    matched_keys = {(r.kind, r.namespace, r.name) for r in report.live_matched}
    assert TENANT_KEYS <= rendered_keys
    assert TENANT_KEYS <= matched_keys
    assert rendered_keys == matched_keys


@pytest.mark.anyio
@pytest.mark.skipif(not HELM, reason="helm not on PATH")
async def test_orbit_platform_diff_payload_lists_added_objects(full_world, http):
    objects, _ = render(full_world, PROJ_A, ENV_X)
    for obj in objects:
        live = deepcopy(obj)
        if live.get("kind") == "Namespace":
            live.setdefault("spec", {})["finalizers"] = ["kubernetes"]
            live["status"] = {"phase": "Active"}
        elif live.get("kind") in {"ResourceQuota", "Instrumentation"}:
            live["status"] = {"observed": True}
        full_world.backends.kube.add_object(live)
    async with sdk_client(http, mint(full_world, Role.admin)) as client:
        result = await client.call_tool(
            "orbit_platform_diff", {"project": PROJ_A, "env": ENV_X},
        )
    assert not result.is_error, result.content
    body = result.structured_content
    assert body["returned"] == 1
    item = body["items"][0]
    assert set(item) >= {
        "empty", "rendered", "live_matched", "missing", "extra", "owned",
        "changed", "render_sha", "streak_days", "path", "report_path", "date",
        "project", "env",
    }
    assert item["empty"] is True
    rendered_keys = {
        (r["kind"], r["namespace"], r["name"]) for r in item["rendered"]
    }
    matched_keys = {
        (r["kind"], r["namespace"], r["name"]) for r in item["live_matched"]
    }
    assert TENANT_KEYS <= rendered_keys
    assert TENANT_KEYS <= matched_keys
