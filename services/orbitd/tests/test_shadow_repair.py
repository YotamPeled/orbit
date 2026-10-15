"""Repair tests: each defect from the shadow-diff review, reproduced then fixed."""

from __future__ import annotations

import os
import tempfile
import threading
import time
from copy import deepcopy
from pathlib import Path

import pytest
import yaml
from test_shadow import HELM, seed_instance

from orbitd.render import render
from orbitd.shadow import (
    LIVE_GVKS, PLATFORM_LIVE_GVKS, ShadowReport, _DEFAULTS, _IGNORE, _QUANTITY,
    _live_index, _normalise_live, _pointer_segments, _write_report, diff,
)
from orbitd.shadow import _changed_paths  # noqa: PLC2701

REPO = Path(__file__).resolve().parents[3]
RBAC = REPO / "template" / "provisioning" / "clusters" / "platform" / "config" / "orbitd" / "rbac.yaml"
_IRREGULAR_PLURALS = {
    "NetworkPolicy": "networkpolicies",
    "Ingress": "ingresses",
}


def _paths(rendered: dict, live: dict, *, kind: str = "",
           api_version: str = "") -> list[str]:
    if not kind:
        kind = str(rendered.get("kind") or "")
    if not api_version:
        api_version = str(rendered.get("apiVersion") or "")
    return _changed_paths(
        _normalise_live(rendered), _normalise_live(live),
        kind=kind, api_version=api_version,
    )


# ------------------------------------------------------------------ 1. live-only fields


def test_injected_configmap_key_is_a_change():
    rendered = {
        "apiVersion": "v1", "kind": "ConfigMap",
        "metadata": {"name": "cm", "namespace": "ns"},
        "data": {"k": "v"},
    }
    live = deepcopy(rendered)
    live["data"]["injected"] = "surprise"
    paths = _paths(rendered, live)
    assert any("injected" in p for p in paths), paths


def test_privileged_container_is_a_change():
    rendered = {
        "apiVersion": "apps/v1", "kind": "Deployment",
        "metadata": {"name": "web", "namespace": "ns"},
        "spec": {"template": {"spec": {"containers": [{"name": "c", "image": "x"}]}}},
    }
    live = deepcopy(rendered)
    live["spec"]["template"]["spec"]["containers"][0]["securityContext"] = {
        "privileged": True,
    }
    paths = _paths(rendered, live, kind="Deployment")
    assert any("privileged" in p or "securityContext" in p for p in paths), paths


def test_defaulted_container_field_is_not_a_change():
    rendered = {
        "apiVersion": "apps/v1", "kind": "Deployment",
        "metadata": {"name": "web", "namespace": "ns"},
        "spec": {"template": {"spec": {"containers": [{"name": "c", "image": "x"}]}}},
    }
    live = deepcopy(rendered)
    container = live["spec"]["template"]["spec"]["containers"][0]
    container["terminationMessagePath"] = "/dev/termination-log"
    container["terminationMessagePolicy"] = "File"
    container["imagePullPolicy"] = "IfNotPresent"
    live["spec"]["template"]["spec"]["dnsPolicy"] = "ClusterFirst"
    live["spec"]["template"]["spec"]["restartPolicy"] = "Always"
    live["spec"]["template"]["spec"]["schedulerName"] = "default-scheduler"
    live["spec"]["progressDeadlineSeconds"] = 600
    live["spec"]["revisionHistoryLimit"] = 10
    live["spec"]["strategy"] = {
        "type": "RollingUpdate",
        "rollingUpdate": {"maxUnavailable": "25%", "maxSurge": "25%"},
    }
    assert _paths(rendered, live, kind="Deployment") == []


# ------------------------------------------------------------------ 2. ObjectStore GVK


def test_live_gvks_objectstore_is_barmancloud():
    assert ("barmancloud.cnpg.io/v1", "ObjectStore") in LIVE_GVKS
    assert ("postgresql.cnpg.io/v1", "ObjectStore") not in LIVE_GVKS


def _gvk_plural(kind: str) -> str:
    return _IRREGULAR_PLURALS.get(kind, kind.lower() + "s")


def _gvk_group(api_version: str) -> str:
    return api_version.rsplit("/", 1)[0] if "/" in api_version else ""


def _readonly_grants() -> dict[tuple[str, str], set[str]]:
    docs = [d for d in yaml.safe_load_all(RBAC.read_text()) if d]
    roles = [
        d for d in docs
        if d.get("kind") == "ClusterRole" and d["metadata"]["name"] == "orbitd-readonly"
    ]
    assert len(roles) == 1, [d.get("metadata") for d in docs if d.get("kind") == "ClusterRole"]
    grants: dict[tuple[str, str], set[str]] = {}
    for rule in roles[0].get("rules") or []:
        verbs = set(rule.get("verbs") or [])
        for group in rule.get("apiGroups") or []:
            for resource in rule.get("resources") or []:
                grants.setdefault((group, resource), set()).update(verbs)
    return grants


def test_tenant_live_gvks_have_orbitd_readonly_get_list():
    grants = _readonly_grants()
    missing: list[tuple[str, str, str]] = []
    for api_version, kind in LIVE_GVKS:
        group = _gvk_group(api_version)
        resource = _gvk_plural(kind)
        verbs = grants.get((group, resource), set())
        if not {"get", "list"} <= verbs:
            missing.append((group, resource, kind))
    assert missing == [], (
        "orbitd-readonly missing get/list for tenant LIVE_GVKS: "
        + ", ".join(f"{g}/{r} ({k})" for g, r, k in missing)
    )


def test_platform_live_gvks_hold_statefulset_daemonset_pdb():
    extras = (
        ("apps/v1", "StatefulSet"),
        ("apps/v1", "DaemonSet"),
        ("policy/v1", "PodDisruptionBudget"),
    )
    for pair in extras:
        assert pair not in LIVE_GVKS
        assert pair in PLATFORM_LIVE_GVKS
    assert PLATFORM_LIVE_GVKS[: len(LIVE_GVKS)] == LIVE_GVKS


def test_extra_scan_derives_gvk_from_rendered(fake_backends):
    rendered = [{
        "apiVersion": "example.io/v1",
        "kind": "Widget",
        "metadata": {"name": "keep", "namespace": "demo-prod"},
    }]
    kube = fake_backends.backends.kube
    kube.add_object(deepcopy(rendered[0]))
    kube.add_object({
        "apiVersion": "example.io/v1",
        "kind": "Widget",
        "metadata": {
            "name": "stray",
            "namespace": "demo-prod",
            "labels": {"app.kubernetes.io/instance": "demo"},
        },
    })
    found = _live_index(fake_backends, rendered, "demo")
    assert ("Widget", "demo-prod", "stray") in found


# ------------------------------------------------------------------ 3. quantity coercion


def test_configmap_numeric_looking_strings_are_not_coerced():
    rendered = {
        "apiVersion": "v1", "kind": "ConfigMap",
        "metadata": {"name": "cm"},
        "data": {"n": "1"},
    }
    live = deepcopy(rendered)
    live["data"]["n"] = "001"
    paths = _paths(rendered, live)
    assert any(p.endswith("/n") or p.endswith("/data/n") for p in paths), paths


def test_resource_quantities_still_compare_numerically():
    rendered = {
        "apiVersion": "apps/v1", "kind": "Deployment",
        "metadata": {"name": "web"},
        "spec": {"template": {"spec": {"containers": [{
            "name": "c",
            "resources": {"limits": {"memory": "1Gi", "cpu": "100m"}},
        }]}}},
    }
    live = deepcopy(rendered)
    live["spec"]["template"]["spec"]["containers"][0]["resources"]["limits"] = {
        "memory": "1024Mi", "cpu": "0.1",
    }
    assert _paths(rendered, live, kind="Deployment") == []


# ------------------------------------------------------------------ 4. empty maps


def test_empty_annotations_match_absent_annotations(fake_backends, monkeypatch):
    rendered = [{
        "apiVersion": "v1", "kind": "ConfigMap",
        "metadata": {
            "name": "cm", "namespace": "demo-prod",
            "annotations": {}, "labels": {},
        },
        "data": {"k": "v"},
    }]
    monkeypatch.setattr("orbitd.shadow.render_desired", lambda *a, **k: (rendered, "sha"))
    live = deepcopy(rendered[0])
    live["metadata"].pop("annotations")
    live["metadata"].pop("labels")
    fake_backends.backends.kube.add_object(live)
    report = diff(fake_backends, "demo", "prod")
    assert report.changed == []
    assert report.empty is True


# ------------------------------------------------------------------ 5. releaseName


@pytest.mark.skipif(not HELM, reason="helm not on PATH")
def test_helm_honours_release_name(fake_backends, monkeypatch):
    seed_instance(fake_backends)
    apps = fake_backends.backends.instance_root / "clusters" / "apps" / "demo-prod.yaml"
    docs = list(yaml.safe_load_all(apps.read_text()))
    for doc in docs:
        if isinstance(doc, dict) and doc.get("kind") == "Application":
            helm = ((doc.get("spec") or {}).get("source") or {}).setdefault("helm", {})
            helm["releaseName"] = "from-spec"
    apps.write_text(yaml.safe_dump_all(docs, sort_keys=False))
    captured: list[list[str]] = []
    from orbitd.helm import run_helm as real_run

    def spy(argv, **kwargs):
        captured.append(list(argv))
        return real_run(argv, **kwargs)

    # helm is launched through the bounded runner; the argv is what the spy sees
    monkeypatch.setattr("orbitd.render.run_helm", spy)
    render(fake_backends, "demo", "prod")
    helm_calls = [a for a in captured if a and a[0] == "helm"]
    assert helm_calls, captured
    argv = helm_calls[0]
    assert "template" in argv
    assert argv[argv.index("template") + 1] == "from-spec"


# ------------------------------------------------------------------ 6. race: lock + unique tempfile


def test_overlapping_diff_returns_in_progress_result(fake_backends, monkeypatch):
    seed_instance(fake_backends)
    objects = [{
        "apiVersion": "v1", "kind": "ConfigMap",
        "metadata": {"name": "cm", "namespace": "demo-prod"},
        "data": {"k": "v"},
    }]
    entered = threading.Event()
    release = threading.Event()
    calls = {"n": 0}

    def fake_render(state, project, env):
        calls["n"] += 1
        entered.set()
        assert release.wait(timeout=5)
        return objects, "abc"

    monkeypatch.setattr("orbitd.shadow.render_desired", fake_render)
    results: list[ShadowReport] = []
    errors: list[BaseException] = []

    def run():
        try:
            results.append(diff(fake_backends, "demo", "prod"))
        except BaseException as e:  # noqa: BLE001
            errors.append(e)

    t1 = threading.Thread(target=run)
    t1.start()
    assert entered.wait(timeout=5)
    t2 = threading.Thread(target=run)
    t2.start()
    time.sleep(0.2)
    assert calls["n"] == 1
    release.set()
    t1.join(timeout=5)
    t2.join(timeout=5)
    assert errors == []
    assert len(results) == 2
    assert calls["n"] == 1
    assert results[0].render_sha == results[1].render_sha == "abc"


def test_write_report_uses_unique_tempfile_and_replace(fake_backends, monkeypatch):
    created: list[str] = []
    real_ntf = tempfile.NamedTemporaryFile

    def spy(*args, **kwargs):
        assert kwargs.get("delete") is False
        handle = real_ntf(*args, **kwargs)
        created.append(handle.name)
        return handle

    monkeypatch.setattr("orbitd.shadow.tempfile.NamedTemporaryFile", spy)
    replaced: list[tuple[str, str | os.PathLike]] = []
    real_replace = os.replace

    def spy_replace(src, dst):
        replaced.append((src, dst))
        return real_replace(src, dst)

    monkeypatch.setattr("orbitd.shadow.os.replace", spy_replace)
    report = ShadowReport(project="demo", env="prod", empty=True)
    path = _write_report(fake_backends, report, clock=None)
    assert created, "NamedTemporaryFile was not used"
    assert replaced, "os.replace was not used"
    assert replaced[0][0] == created[0]
    assert Path(replaced[0][1]) == Path(path)
    assert Path(path).is_file()


# ------------------------------------------------------------------ 7. kind+path tables (round 2)


def test_configmap_data_image_pull_policy_live_only_is_a_change():
    rendered = {
        "apiVersion": "v1", "kind": "ConfigMap",
        "metadata": {"name": "cm"},
        "data": {"k": "v"},
    }
    live = deepcopy(rendered)
    live["data"]["imagePullPolicy"] = "Never"
    paths = _paths(rendered, live)
    assert any("imagePullPolicy" in p for p in paths), paths


def test_container_image_pull_policy_never_live_only_is_a_change():
    rendered = {
        "apiVersion": "apps/v1", "kind": "Deployment",
        "metadata": {"name": "web"},
        "spec": {"template": {"spec": {"containers": [{"name": "c", "image": "x"}]}}},
    }
    live = deepcopy(rendered)
    live["spec"]["template"]["spec"]["containers"][0]["imagePullPolicy"] = "Never"
    paths = _paths(rendered, live)
    assert any(p.endswith("/imagePullPolicy") for p in paths), paths


def test_container_image_pull_policy_if_not_present_live_only_is_equal():
    rendered = {
        "apiVersion": "apps/v1", "kind": "Deployment",
        "metadata": {"name": "web"},
        "spec": {"template": {"spec": {"containers": [{"name": "c", "image": "x"}]}}},
    }
    live = deepcopy(rendered)
    live["spec"]["template"]["spec"]["containers"][0]["imagePullPolicy"] = "IfNotPresent"
    assert _paths(rendered, live) == []


def test_container_termination_message_path_other_is_a_change():
    rendered = {
        "apiVersion": "apps/v1", "kind": "Deployment",
        "metadata": {"name": "web"},
        "spec": {"template": {"spec": {"containers": [{"name": "c", "image": "x"}]}}},
    }
    live = deepcopy(rendered)
    live["spec"]["template"]["spec"]["containers"][0]["terminationMessagePath"] = (
        "/tmp/other"
    )
    paths = _paths(rendered, live)
    assert any(p.endswith("/terminationMessagePath") for p in paths), paths


def test_configmap_quantity_named_keys_compare_as_strings():
    rendered = {
        "apiVersion": "v1", "kind": "ConfigMap",
        "metadata": {"name": "cm"},
        "data": {"resources": "1", "storage": "1", "hard": "1", "used": "1"},
    }
    live = deepcopy(rendered)
    for key in ("resources", "storage", "hard", "used"):
        live["data"][key] = "001"
    paths = _paths(rendered, live)
    for key in ("resources", "storage", "hard", "used"):
        assert any(p.endswith(f"/{key}") for p in paths), (key, paths)


def test_deployment_cpu_milli_equals_fraction():
    rendered = {
        "apiVersion": "apps/v1", "kind": "Deployment",
        "metadata": {"name": "web"},
        "spec": {"template": {"spec": {"containers": [{
            "name": "c",
            "resources": {"requests": {"cpu": "500m"}},
        }]}}},
    }
    live = deepcopy(rendered)
    live["spec"]["template"]["spec"]["containers"][0]["resources"]["requests"]["cpu"] = (
        "0.5"
    )
    assert _paths(rendered, live) == []


def test_init_container_image_pull_policy_if_not_present_live_only_is_equal():
    rendered = {
        "apiVersion": "apps/v1", "kind": "Deployment",
        "metadata": {"name": "web"},
        "spec": {"template": {"spec": {
            "containers": [{"name": "c", "image": "x"}],
            "initContainers": [
                {"name": "i0", "image": "x"},
                {"name": "i1", "image": "x"},
            ],
        }}},
    }
    live = deepcopy(rendered)
    live["spec"]["template"]["spec"]["initContainers"][1]["imagePullPolicy"] = (
        "IfNotPresent"
    )
    assert _paths(rendered, live) == []


def test_init_container_image_pull_policy_never_live_only_is_a_change():
    rendered = {
        "apiVersion": "apps/v1", "kind": "Deployment",
        "metadata": {"name": "web"},
        "spec": {"template": {"spec": {
            "containers": [{"name": "c", "image": "x"}],
            "initContainers": [{"name": "i0", "image": "x"}],
        }}},
    }
    live = deepcopy(rendered)
    live["spec"]["template"]["spec"]["initContainers"][0]["imagePullPolicy"] = (
        "Never"
    )
    paths = _paths(rendered, live)
    assert any(p.endswith("/imagePullPolicy") for p in paths), paths


def test_init_container_cpu_milli_equals_fraction():
    rendered = {
        "apiVersion": "apps/v1", "kind": "Deployment",
        "metadata": {"name": "web"},
        "spec": {"template": {"spec": {
            "containers": [{"name": "c", "image": "x"}],
            "initContainers": [{
                "name": "i0",
                "resources": {"requests": {"cpu": "500m"}},
            }],
        }}},
    }
    live = deepcopy(rendered)
    live["spec"]["template"]["spec"]["initContainers"][0]["resources"]["requests"][
        "cpu"
    ] = "0.5"
    assert _paths(rendered, live) == []


def test_container_table_rows_are_mirrored_for_init_containers():
    defaults = {(av, kind, pointer): value for av, kind, pointer, value in _DEFAULTS}
    for av, kind, pointer, value in _DEFAULTS:
        if "/containers/" not in pointer:
            continue
        mirrored = pointer.replace("/containers/", "/initContainers/", 1)
        assert (av, kind, mirrored) in defaults, (av, kind, mirrored)
        assert defaults[(av, kind, mirrored)] == value
    quantities = {(av, kind, pointer) for av, kind, pointer in _QUANTITY}
    for av, kind, pointer in _QUANTITY:
        if "/containers/" not in pointer:
            continue
        mirrored = pointer.replace("/containers/", "/initContainers/", 1)
        assert (av, kind, mirrored) in quantities, (av, kind, mirrored)


def _shape_supports(obj: object, pointer: str) -> bool:
    """True when `pointer` is a field of this object: every non-final segment exists.

    A missing leaf is allowed (server-defaulted fields are omitted from a render).
    `*` is one list item or one map value. A pointer that leaves the kind's shape
    (Service `/spec/template/...`) returns False.
    """
    segs = _pointer_segments(pointer)
    nodes: list[object] = [obj]
    for i, seg in enumerate(segs):
        last = i == len(segs) - 1
        nxt: list[object] = []
        progressed = False
        for node in nodes:
            if seg == "*":
                if isinstance(node, list) and node:
                    nxt.extend(node)
                    progressed = True
                elif isinstance(node, dict) and node:
                    nxt.extend(node.values())
                    progressed = True
                continue
            if not isinstance(node, dict):
                continue
            if seg not in node:
                if last:
                    progressed = True
                continue
            nxt.append(node[seg])
            progressed = True
        if last:
            return progressed
        if not nxt:
            return False
        nodes = nxt
    return True


def _shape_container() -> dict:
    return {
        "name": "c",
        "image": "example/c",
        "ports": [{"containerPort": 1}],
        "env": [{"name": "POD_NS", "valueFrom": {"fieldRef": {"fieldPath": "metadata.namespace"}}}],
        "resources": {
            "limits": {"memory": "1Mi"},
            "requests": {"cpu": "1m", "memory": "1Mi"},
        },
        "livenessProbe": {"httpGet": {"path": "/"}},
        "readinessProbe": {"httpGet": {"path": "/"}},
        "startupProbe": {"httpGet": {"path": "/"}},
    }


def _shape_pod() -> dict:
    return {
        "containers": [_shape_container()],
        "initContainers": [_shape_container()],
        "volumes": [
            {"name": "cm", "configMap": {"name": "x"}},
            {"name": "sec", "secret": {"secretName": "x"}},
            {"name": "proj", "projected": {"sources": []}},
            {"name": "hp", "hostPath": {"path": "/var"}},
        ],
    }


SHAPE_EXTRAS: list[dict] = [
    {
        "apiVersion": "apps/v1", "kind": "Deployment",
        "metadata": {"name": "shape-deploy", "namespace": "demo-prod",
                     "annotations": {}},
        "spec": {
            "template": {"metadata": {"annotations": {}}, "spec": _shape_pod()},
            "strategy": {"rollingUpdate": {}},
        },
    },
    {
        "apiVersion": "apps/v1", "kind": "StatefulSet",
        "metadata": {"name": "shape-sts", "namespace": "demo-prod"},
        "spec": {
            "template": {"metadata": {"annotations": {}}, "spec": _shape_pod()},
            "updateStrategy": {},
            "persistentVolumeClaimRetentionPolicy": {},
            "volumeClaimTemplates": [{"metadata": {"name": "data"}, "spec": {}}],
        },
    },
    {
        "apiVersion": "apps/v1", "kind": "DaemonSet",
        "metadata": {"name": "shape-ds", "namespace": "demo-prod",
                     "annotations": {}},
        "spec": {
            "template": {"metadata": {"annotations": {}}, "spec": _shape_pod()},
            "updateStrategy": {"rollingUpdate": {}},
        },
    },
    {
        "apiVersion": "v1", "kind": "Secret",
        "metadata": {"name": "shape-secret", "namespace": "demo-prod"},
    },
    {
        "apiVersion": "scheduling.k8s.io/v1", "kind": "PriorityClass",
        "metadata": {"name": "shape-pc"},
        "value": 1,
    },
    {
        "apiVersion": "apiextensions.k8s.io/v1", "kind": "CustomResourceDefinition",
        "metadata": {"name": "widgets.example.io", "annotations": {}},
        "spec": {"conversion": {"webhook": {"clientConfig": {}}}},
    },
    {
        "apiVersion": "admissionregistration.k8s.io/v1",
        "kind": "ValidatingWebhookConfiguration",
        "metadata": {"name": "shape-vwh"},
        "webhooks": [{"name": "h.example.io",
                      "clientConfig": {"service": {"name": "s"}},
                      "rules": [{}]}],
    },
    {
        "apiVersion": "admissionregistration.k8s.io/v1",
        "kind": "MutatingWebhookConfiguration",
        "metadata": {"name": "shape-mwh"},
        "webhooks": [{"name": "m.example.io",
                      "clientConfig": {"service": {"name": "s"}},
                      "rules": [{}]}],
    },
    {
        "apiVersion": "admissionregistration.k8s.io/v1",
        "kind": "ValidatingAdmissionPolicy",
        "metadata": {"name": "shape-vap"},
        "spec": {"matchConstraints": {"resourceRules": [{}]}},
    },
    {
        "apiVersion": "admissionregistration.k8s.io/v1",
        "kind": "ValidatingAdmissionPolicyBinding",
        "metadata": {"name": "shape-vapb"},
        "spec": {"matchResources": {"resourceRules": [{}]}},
    },
    {
        "apiVersion": "external-secrets.io/v1", "kind": "ClusterSecretStore",
        "metadata": {"name": "shape-css", "annotations": {}},
        "spec": {"provider": {"infisical": {"secretsScope": {}}}},
    },
    {
        "apiVersion": "generators.external-secrets.io/v1alpha1", "kind": "Password",
        "metadata": {"name": "shape-pw", "namespace": "demo-prod"},
        "spec": {},
    },
    {
        "apiVersion": "batch/v1", "kind": "CronJob",
        "metadata": {
            "name": "shape-cron", "namespace": "demo-prod",
            "labels": {"app": "x"},
        },
        "spec": {
            "schedule": "* * * * *",
            "jobTemplate": {"spec": {
                "selector": {"matchLabels": {"app": "x"}},
                "template": {
                    "metadata": {"labels": {"app": "x"}},
                    "spec": _shape_pod(),
                },
            }},
        },
    },
    {
        "apiVersion": "batch/v1", "kind": "Job",
        "metadata": {
            "name": "shape-job", "namespace": "demo-prod",
            "labels": {"app": "x"},
        },
        "spec": {
            "selector": {"matchLabels": {"app": "x"}},
            "template": {
                "metadata": {"labels": {"app": "x"}},
                "spec": _shape_pod(),
            },
        },
    },
    {
        "apiVersion": "v1", "kind": "PersistentVolumeClaim",
        "metadata": {
            "name": "shape-pvc", "namespace": "demo-prod",
            "annotations": {},
        },
        "spec": {
            "accessModes": ["ReadWriteOnce"],
            "resources": {"requests": {"storage": "1Gi"}},
        },
    },
    {
        "apiVersion": "external-secrets.io/v1", "kind": "ExternalSecret",
        "metadata": {"name": "shape-es", "namespace": "demo-prod", "annotations": {}},
        "spec": {"refreshInterval": "1h", "target": {"template": {}}},
    },
    {
        "apiVersion": "v1", "kind": "ResourceQuota",
        "metadata": {"name": "shape-quota", "namespace": "demo-prod"},
        "spec": {"hard": {"pods": "10", "requests.cpu": "1"}},
    },
    {
        "apiVersion": "postgresql.cnpg.io/v1", "kind": "Cluster",
        "metadata": {"name": "shape-db", "namespace": "demo-prod"},
        "spec": {
            "storage": {"size": "1Gi"},
            "resources": {
                "requests": {"cpu": "100m", "memory": "256Mi"},
                "limits": {"memory": "256Mi"},
            },
            "bootstrap": {"initdb": {}},
            "affinity": {},
            "monitoring": {},
            "postgresql": {
                "parameters": {"max_connections": "100"},
                "syncReplicaElectionConstraint": {},
            },
            "probes": {"liveness": {"isolationCheck": {}}},
            "replicationSlots": {
                "highAvailability": {},
                "synchronizeReplicas": {},
            },
        },
    },
    {
        "apiVersion": "barmancloud.cnpg.io/v1", "kind": "ObjectStore",
        "metadata": {"name": "shape-store", "namespace": "demo-prod"},
        "spec": {"instanceSidecarConfiguration": {"resources": {
            "requests": {"cpu": "20m", "memory": "192Mi"},
            "limits": {"memory": "192Mi"},
        }}},
    },
    {
        "apiVersion": "opentelemetry.io/v1alpha1", "kind": "Instrumentation",
        "metadata": {"name": "shape-otel", "namespace": "demo-prod", "annotations": {}},
        "spec": {
            "python": {"resourceRequirements": {
                "requests": {"cpu": "1m"}, "limits": {"memory": "1Mi"},
            }},
            "dotnet": {"resourceRequirements": {
                "requests": {"cpu": "1m"}, "limits": {"memory": "1Mi"},
            }},
            "nodejs": {"resourceRequirements": {
                "requests": {"cpu": "1m"}, "limits": {"memory": "1Mi"},
            }},
            "java": {"resources": {
                "requests": {"cpu": "1m"}, "limits": {"memory": "1Mi"},
            }},
        },
    },
    {
        "apiVersion": "networking.k8s.io/v1", "kind": "NetworkPolicy",
        "metadata": {"name": "shape-np", "namespace": "demo-prod"},
        "spec": {
            "podSelector": {},
            "ingress": [{"ports": [{"port": 80}]}],
            "egress": [{"ports": [{"port": 53}]}],
        },
    },
    {
        "apiVersion": "v1", "kind": "Namespace",
        "metadata": {
            "name": "demo-prod",
            "labels": {"orbit.local/project": "demo", "orbit.local/env": "prod"},
        },
        "spec": {},
    },
    {
        "apiVersion": "monitoring.coreos.com/v1", "kind": "Probe",
        "metadata": {
            "name": "shape-probe", "namespace": "monitoring",
            "labels": {"orbit.local/project": "demo", "orbit.local/env": "prod"},
        },
        "spec": {
            "module": "http_2xx",
            "prober": {"url": "blackbox.monitoring.svc:9115"},
        },
    },
]


def seed_shape_instance(state) -> None:
    seed_instance(state)
    apps = state.backends.instance_root / "clusters" / "apps" / "demo-prod.yaml"
    docs = [d for d in yaml.safe_load_all(apps.read_text()) if isinstance(d, dict)]
    docs.extend(SHAPE_EXTRAS)
    apps.write_text(yaml.safe_dump_all(docs, sort_keys=False))


def test_self_check_rejects_path_that_does_not_exist_on_the_kind():
    service = {
        "apiVersion": "v1", "kind": "Service",
        "metadata": {"name": "svc"},
        "spec": {"ports": [{"port": 80}]},
    }
    assert _shape_supports(service, "/spec/ports/*/protocol") is True
    assert _shape_supports(
        service, "/spec/template/spec/containers/*/imagePullPolicy",
    ) is False


@pytest.mark.skipif(not HELM, reason="helm not on PATH")
def test_table_paths_exist_on_rendered_kinds(fake_backends):
    seed_shape_instance(fake_backends)
    objects, _ = render(fake_backends, "demo", "prod")
    by_gk: dict[tuple[str, str], list[dict]] = {}
    for obj in objects:
        av = str(obj.get("apiVersion") or "")
        kind = str(obj.get("kind") or "")
        by_gk.setdefault((av, kind), []).append(obj)

    missing_kind: list[tuple[str, str, str]] = []
    bad_path: list[tuple[str, str, str]] = []

    def check(av: str, kind: str, pointer: str, *, required: bool) -> None:
        objs = by_gk.get((av, kind), [])
        if not objs:
            if required:
                missing_kind.append((av, kind, pointer))
            return
        if not any(_shape_supports(o, pointer) for o in objs):
            bad_path.append((av, kind, pointer))

    for av, kind, pointer, _value in _DEFAULTS:
        check(av, kind, pointer, required=True)
    for av, kind, pointer in _QUANTITY:
        check(av, kind, pointer, required=True)
    for av, kind, pointer in _IGNORE:
        check(av, kind, pointer, required=False)

    assert missing_kind == [], missing_kind
    assert bad_path == [], bad_path
