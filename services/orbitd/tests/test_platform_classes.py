"""Platform compare classes: generated, ignored hook/admission/stamp, overlay stays."""

from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace

from orbitd.platform import EXIT_DRIFT, cmd_plan, load_applications, render_application
from orbitd.shadow import compare_objects

FIXTURES = Path(__file__).resolve().parent / "fixtures"
CLASSES = FIXTURES / "platform" / "classes"
OTHER = "b3RoZXI="  # base64 "other"


def _plan_args(instance_dir, *, app=None, summary=False, live_from=None):
    return SimpleNamespace(
        instance_dir=str(instance_dir),
        app=app,
        summary=summary,
        kube=None,
        kubeconfig=None,
        render_only=None,
        live_from=live_from,
    )


def _live_with_classes(objects: list[dict]) -> list[dict]:
    """Each class's live mutation on a copy of the rendered set."""
    live = [deepcopy(obj) for obj in objects]
    by_name = {(o.get("kind"), (o.get("metadata") or {}).get("name")): o for o in live}
    tls = by_name[("Secret", "webhook-tls")]
    tls["data"] = {
        "ca.crt": OTHER, "ca.key": OTHER, "tls.crt": OTHER, "tls.key": OTHER,
    }
    workload = by_name[("Deployment", "workload")]
    ann = workload["spec"]["template"]["metadata"]["annotations"]
    ann["checksum/config"] = "live-checksum"
    nested = by_name[("ConfigMap", "nested-yaml")]
    nested["data"]["tempo.yaml"] = "server:\n  http_listen_port: 3200\n"
    pvc = by_name[("PersistentVolumeClaim", "data")]
    pvc["spec"]["storageClassName"] = "local-path"
    plugin = by_name[("Secret", "plugin")]
    plugin["data"]["SIDECAR_IMAGE"] = OTHER
    minted = by_name[("Secret", "minted")]
    minted["data"]["password"] = OTHER
    overlay = by_name[("ConfigMap", "cmd-params")]
    overlay["metadata"]["labels"]["app.kubernetes.io/name"] = "cmd-params"
    return live


def _classes_objects() -> tuple[dict, list[dict]]:
    app = next(a for a in load_applications(CLASSES) if a["metadata"]["name"] == "classes")
    return app, render_application(CLASSES, app)


def test_tls_secret_data_diff_is_generated_not_changed():
    rendered = {
        "apiVersion": "v1", "kind": "Secret",
        "metadata": {"name": "webhook-tls", "namespace": "ns-a"},
        "type": "kubernetes.io/tls",
        "data": {
            "ca.crt": "dGVzdA==", "ca.key": "dGVzdA==",
            "tls.crt": "dGVzdA==", "tls.key": "dGVzdA==",
        },
    }
    live = deepcopy(rendered)
    live["data"] = {
        "ca.crt": OTHER, "ca.key": OTHER, "tls.crt": OTHER, "tls.key": OTHER,
    }
    report = compare_objects([rendered], [live], project="classes")
    assert report.changed == []
    assert report.empty is True
    assert len(report.generated) == 1
    gen = report.generated[0]
    assert gen.kind == "Secret" and gen.name == "webhook-tls"
    assert gen.keys == ["ca.crt", "ca.key", "tls.crt", "tls.key"]


def test_named_generated_secret_is_generated_without_tls_keys():
    rendered = {
        "apiVersion": "v1", "kind": "Secret",
        "metadata": {"name": "minted", "namespace": "ns-a"},
        "data": {"password": "c2VlZA=="},
    }
    live = deepcopy(rendered)
    live["data"]["password"] = OTHER
    report = compare_objects(
        [rendered], [live], project="classes",
        generated_secret_names={"minted"},
    )
    assert report.empty is True
    assert report.changed == []
    assert [g.keys for g in report.generated] == [["password"]]
    unnamed = compare_objects([rendered], [live], project="classes")
    assert unnamed.empty is False
    assert unnamed.generated == []
    assert any("/data/password" in c.paths for c in unnamed.changed)


def test_checksum_pod_template_annotation_is_ignored():
    rendered = {
        "apiVersion": "apps/v1", "kind": "Deployment",
        "metadata": {"name": "workload", "namespace": "ns-a"},
        "spec": {"template": {"metadata": {"annotations": {
            "checksum/config": "deadbeef",
            "keep": "x",
        }}, "spec": {"containers": [{"name": "c", "image": "x"}]}}},
    }
    live = deepcopy(rendered)
    live["spec"]["template"]["metadata"]["annotations"]["checksum/config"] = "live"
    report = compare_objects([rendered], [live])
    assert report.empty is True
    assert report.changed == []

    sts = {
        "apiVersion": "apps/v1", "kind": "StatefulSet",
        "metadata": {"name": "tempo", "namespace": "ns-a"},
        "spec": {"template": {"metadata": {"annotations": {
            "checksum/configmap": "aaaa",
        }}, "spec": {"containers": [{"name": "c", "image": "x"}]}}},
    }
    live_sts = deepcopy(sts)
    live_sts["spec"]["template"]["metadata"]["annotations"]["checksum/configmap"] = "bbbb"
    assert compare_objects([sts], [live_sts]).empty is True

    live_keep = deepcopy(rendered)
    live_keep["spec"]["template"]["metadata"]["annotations"]["keep"] = "y"
    keep_report = compare_objects([rendered], [live_keep])
    assert keep_report.empty is False
    assert any(p.endswith("/keep") for p in keep_report.changed[0].paths)


def test_configmap_yaml_null_key_equals_absent():
    rendered = {
        "apiVersion": "v1", "kind": "ConfigMap",
        "metadata": {"name": "nested-yaml", "namespace": "ns-a"},
        "data": {"tempo.yaml": "server:\n  http_listen_port: 3200\nmetrics:\n  wal: null\n"},
    }
    live = deepcopy(rendered)
    live["data"]["tempo.yaml"] = "server:\n  http_listen_port: 3200\n"
    report = compare_objects([rendered], [live])
    assert report.empty is True
    assert report.changed == []

    live_other = deepcopy(rendered)
    live_other["data"]["tempo.yaml"] = "server:\n  http_listen_port: 1\n"
    other = compare_objects([rendered], [live_other])
    assert other.empty is False
    assert any(p.endswith("/tempo.yaml") for p in other.changed[0].paths)

    raw = {
        "apiVersion": "v1", "kind": "ConfigMap",
        "metadata": {"name": "plain", "namespace": "ns-a"},
        "data": {"k": "not: [yaml"},
    }
    live_raw = deepcopy(raw)
    live_raw["data"]["k"] = "other"
    raw_report = compare_objects([raw], [live_raw])
    assert raw_report.empty is False
    assert any(p.endswith("/k") for p in raw_report.changed[0].paths)

    numeric = {
        "apiVersion": "v1", "kind": "ConfigMap",
        "metadata": {"name": "n", "namespace": "ns-a"},
        "data": {"n": "1"},
    }
    live_numeric = deepcopy(numeric)
    live_numeric["data"]["n"] = "001"
    numeric_report = compare_objects([numeric], [live_numeric])
    assert numeric_report.empty is False
    assert any(p.endswith("/n") for p in numeric_report.changed[0].paths)


def test_pvc_omitted_storage_class_accepts_any_live_value():
    rendered = {
        "apiVersion": "v1", "kind": "PersistentVolumeClaim",
        "metadata": {"name": "data", "namespace": "ns-a"},
        "spec": {"accessModes": ["ReadWriteOnce"],
                 "resources": {"requests": {"storage": "1Gi"}}},
    }
    for cls in ("local-path", "gp2", "anything"):
        live = deepcopy(rendered)
        live["spec"]["storageClassName"] = cls
        report = compare_objects([rendered], [live])
        assert report.empty is True, cls
        assert report.changed == []

    pinned = deepcopy(rendered)
    pinned["spec"]["storageClassName"] = "local-path"
    live_other = deepcopy(pinned)
    live_other["spec"]["storageClassName"] = "gp2"
    pinned_report = compare_objects([pinned], [live_other])
    assert pinned_report.empty is False
    assert any(p.endswith("/storageClassName") for p in pinned_report.changed[0].paths)


def test_sidecar_image_stamp_is_ignored_on_both_sides():
    rendered = {
        "apiVersion": "v1", "kind": "Secret",
        "metadata": {"name": "plugin", "namespace": "ns-a"},
        "data": {"SIDECAR_IMAGE": "cGxhY2Vob2xkZXI=", "keep": "dGVzdA=="},
    }
    live = deepcopy(rendered)
    live["data"]["SIDECAR_IMAGE"] = OTHER
    report = compare_objects([rendered], [live])
    assert report.empty is True
    assert report.changed == []
    assert report.generated == []

    live_keep = deepcopy(rendered)
    live_keep["data"]["keep"] = OTHER
    keep_report = compare_objects([rendered], [live_keep])
    assert keep_report.empty is False
    assert any("/data/keep" in c.paths for c in keep_report.changed)


def test_label_overlay_stays_changed():
    rendered = {
        "apiVersion": "v1", "kind": "ConfigMap",
        "metadata": {
            "name": "cmd-params", "namespace": "ns-a",
            "labels": {"app.kubernetes.io/instance": "classes"},
        },
        "data": {"param": "on"},
    }
    live = deepcopy(rendered)
    live["metadata"]["labels"]["app.kubernetes.io/name"] = "cmd-params"
    report = compare_objects([rendered], [live], project="classes")
    assert report.empty is False
    assert report.generated == []
    assert len(report.changed) == 1
    assert "/metadata/labels/app.kubernetes.io~1name" in report.changed[0].paths


def test_empty_ignores_generated_and_stays_false_when_changed():
    tls = {
        "apiVersion": "v1", "kind": "Secret",
        "metadata": {"name": "webhook-tls", "namespace": "ns-a"},
        "data": {"tls.crt": "dGVzdA==", "tls.key": "dGVzdA=="},
    }
    overlay = {
        "apiVersion": "v1", "kind": "ConfigMap",
        "metadata": {"name": "cmd-params", "namespace": "ns-a"},
        "data": {"param": "on"},
    }
    live_tls = deepcopy(tls)
    live_tls["data"]["tls.crt"] = OTHER
    live_tls["data"]["tls.key"] = OTHER
    only_gen = compare_objects([tls], [live_tls])
    assert only_gen.generated and only_gen.changed == []
    assert only_gen.empty is True

    live_overlay = deepcopy(overlay)
    live_overlay["metadata"] = {
        "name": "cmd-params", "namespace": "ns-a",
        "labels": {"app.kubernetes.io/name": "cmd-params"},
    }
    both = compare_objects(
        [tls, overlay], [live_tls, live_overlay], project="classes",
    )
    assert both.generated
    assert both.changed
    assert both.empty is False


def test_live_from_placeholder_plan_reports_classes(tmp_path, monkeypatch, capsys):
    def boom(*_a, **_k):
        raise AssertionError("Kube must not be constructed for --live-from")
    monkeypatch.setattr("orbitd.platform.Kube", boom)
    app, objects = _classes_objects()
    assert app["metadata"]["annotations"]["orbit.local/generated-secrets"] == "minted"
    live = _live_with_classes(objects)
    plan_path = tmp_path / "plan.json"
    plan_path.write_text(json.dumps({"apps": [{"name": "classes", "live": live}]}))
    rc = cmd_plan(_plan_args(CLASSES, app="classes", live_from=str(plan_path), summary=True))
    out, err = capsys.readouterr()
    assert rc == EXIT_DRIFT
    body = json.loads(out)
    assert body["empty"] is False
    row = body["apps"][0]
    assert row["name"] == "classes"
    assert "generated" in row and "hooks" in row and "owned" in row
    gen = {(g["kind"], g["name"]): g["keys"] for g in row["generated"]}
    assert gen[("Secret", "webhook-tls")] == ["ca.crt", "ca.key", "tls.crt", "tls.key"]
    assert gen[("Secret", "minted")] == ["password"]
    changed = {(c["kind"], c["name"]): c["paths"] for c in row["changed"]}
    assert list(changed) == [("ConfigMap", "cmd-params")]
    assert "/metadata/labels/app.kubernetes.io~1name" in changed[("ConfigMap", "cmd-params")]
    for kind, name in (
        ("Deployment", "workload"),
        ("ConfigMap", "nested-yaml"),
        ("PersistentVolumeClaim", "data"),
        ("Secret", "plugin"),
        ("Secret", "webhook-tls"),
        ("Secret", "minted"),
    ):
        assert (kind, name) not in changed, (kind, name, changed)
    assert "classes empty=false" in err
    assert "generated=2" in err
