"""orbit platform apply: gate, CRD-first order, generated Secrets, dry-run, prune."""

from __future__ import annotations

import io
import json
import shutil
import subprocess
import sys
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from kubernetes.client.exceptions import ApiException

from orbitd.applier import ObjectRef, inventory_name
from orbitd.client import async_main
from orbitd.platform import EXIT_RENDER, main
from orbitd.platform_apply import (
    EXIT_FAILED,
    EXIT_GATE,
    EXIT_OK,
    RULE_CRD,
    RULE_GENERATED,
    RULE_KUBE_SYSTEM,
    TWO_APPLIERS,
    cmd_apply,
    inventory_ref_for_app,
    order_objects,
    platform_prune_rule,
    strip_checksum_annotations,
    strip_render_stamp_annotations,
)
from orbitd.shadow import RENDER_STAMP_ANNOTATIONS, _IGNORE, _unescape
from orbitd.testing import FakeDynamicResource, FakeKube

REPO = Path(__file__).resolve().parents[3]


def _obj(api_version: str, kind: str, name: str, namespace: str | None = None, **body) -> dict:
    meta: dict = {"name": name}
    if namespace:
        meta["namespace"] = namespace
    return {"apiVersion": api_version, "kind": kind, "metadata": meta, **body}


def _application(name: str, path: str, dest: str = "proj-a", *,
                 automated=None, generated: str | None = None, create_ns: bool = True) -> dict:
    meta: dict = {"name": name, "namespace": "argocd"}
    if generated:
        meta["annotations"] = {"orbit.local/generated-secrets": generated}
    spec: dict = {
        "source": {
            "repoURL": "https://git.example.invalid/repo.git",
            "path": path,
            "directory": {"recurse": True},
        },
        "destination": {"namespace": dest},
        "syncPolicy": {},
    }
    if create_ns:
        spec["syncPolicy"]["syncOptions"] = ["CreateNamespace=true"]
    if automated is not None:
        spec["syncPolicy"]["automated"] = automated
    return {
        "apiVersion": "argoproj.io/v1alpha1",
        "kind": "Application",
        "metadata": meta,
        "spec": spec,
    }


def _instance(root, name: str, objects: list[dict], **app_kw):
    rel = f"clusters/platform/config/{name}"
    cfg = root / rel
    cfg.mkdir(parents=True)
    for i, obj in enumerate(objects):
        (cfg / f"{i}.yaml").write_text(yaml.safe_dump(obj))
    apps = root / "clusters" / "platform" / "apps"
    apps.mkdir(parents=True, exist_ok=True)
    (apps / f"{name}.yaml").write_text(yaml.safe_dump(_application(name, rel, **app_kw)))
    return root


def _live_app(name: str, *, automated=None) -> dict:
    spec: dict = {"destination": {"namespace": "proj-a"}, "syncPolicy": {}}
    if automated is not None:
        spec["syncPolicy"]["automated"] = automated
    return {
        "apiVersion": "argoproj.io/v1alpha1",
        "kind": "Application",
        "metadata": {
            "name": name, "namespace": "argocd",
            "uid": "app-uid", "resourceVersion": "1",
        },
        "spec": spec,
    }


def _apply_args(instance_dir, *, app=None, kube=None, kubeconfig=None,
                dry_run=False, prune=False):
    return SimpleNamespace(
        instance_dir=str(instance_dir),
        app=app,
        kube=kube,
        kubeconfig=kubeconfig,
        dry_run=dry_run,
        prune=prune,
    )


def _add_live(kube: FakeKube, obj: dict, *, uid: str = "uid-live", rv: str = "3") -> dict:
    live = deepcopy(obj)
    live.setdefault("metadata", {}).setdefault("uid", uid)
    live["metadata"].setdefault("resourceVersion", rv)
    return kube.add_object(live)


def _kinds_applied(kube: FakeKube) -> list[str]:
    return [
        c["kind"] for c in kube.apply_calls
        if not (c["kind"] == "ConfigMap" and c.get("namespace") == "orbit")
    ]


def _inventory(kube: FakeKube, app: str) -> list[dict]:
    name = f"orbit-platform-inventory-{app}"
    return [
        o for o in kube.objects
        if o.get("kind") == "ConfigMap"
        and (o.get("metadata") or {}).get("name") == name
        and (o.get("metadata") or {}).get("namespace") == "orbit"
    ]


def _cmd(args, stdin: str = "") -> int:
    return cmd_apply(args, stdin=io.StringIO(stdin))


# ------------------------------------------------------------------ helpers

def test_order_objects_crds_then_namespaces_then_helm_rest():
    objects = [
        _obj("v1", "ConfigMap", "keep", "proj-a"),
        _obj("v1", "Namespace", "proj-a"),
        _obj("apiextensions.k8s.io/v1", "CustomResourceDefinition", "widgets.example.io"),
        _obj("apps/v1", "Deployment", "web", "proj-a"),
        _obj("v1", "Service", "web", "proj-a"),
    ]
    ordered = order_objects(objects)
    kinds = [o["kind"] for o in ordered]
    assert kinds[0] == "CustomResourceDefinition"
    assert kinds[1] == "Namespace"
    assert kinds[2:] == ["ConfigMap", "Service", "Deployment"]


def test_strip_checksum_on_pod_template_only():
    dep = _obj(
        "apps/v1", "Deployment", "web", "proj-a",
        spec={
            "template": {
                "metadata": {
                    "annotations": {
                        "checksum/config": "abc",
                        "keep": "yes",
                    },
                },
            },
        },
    )
    dep["metadata"]["annotations"] = {"checksum/not-template": "stay"}
    out = strip_checksum_annotations(dep)
    assert "checksum/config" not in out["spec"]["template"]["metadata"]["annotations"]
    assert out["spec"]["template"]["metadata"]["annotations"]["keep"] == "yes"
    assert out["metadata"]["annotations"]["checksum/not-template"] == "stay"


STAMP_VALUE = "2026-09-10T12:00:00+03:00"
_WORKLOAD_KINDS = ("Deployment", "StatefulSet", "DaemonSet")


def _stamped_workload(kind: str) -> dict:
    obj = _obj(
        "apps/v1", kind, "web",
        spec={
            "selector": {"matchLabels": {"app": "web"}},
            "template": {
                "metadata": {
                    "labels": {"app": "web"},
                    "annotations": {
                        "updatedAt": STAMP_VALUE,
                        "kubectl.kubernetes.io/restartedAt": STAMP_VALUE,
                        "keep": "1",
                    },
                },
                "spec": {"containers": [{"name": "web", "image": "example/web:1"}]},
            },
        },
    )
    obj["metadata"]["annotations"] = {
        "updatedAt": STAMP_VALUE,
        "kubectl.kubernetes.io/restartedAt": STAMP_VALUE,
        "keep": "1",
    }
    return obj


def _assert_stamps_omitted(body: dict) -> None:
    meta_ann = (body.get("metadata") or {}).get("annotations") or {}
    tmpl = ((body.get("spec") or {}).get("template") or {}).get("metadata") or {}
    tmpl_ann = tmpl.get("annotations") or {}
    for stamp in RENDER_STAMP_ANNOTATIONS:
        assert stamp not in meta_ann, stamp
        assert stamp not in tmpl_ann, stamp
    assert meta_ann.get("keep") == "1"
    assert tmpl_ann.get("keep") == "1"


def test_strip_render_stamps_on_metadata_and_template():
    for kind in _WORKLOAD_KINDS:
        out = strip_render_stamp_annotations(_stamped_workload(kind))
        _assert_stamps_omitted(out)


# ------------------------------------------------------------------ gate

def test_gate_refuses_automated_application(tmp_path, capsys):
    _instance(tmp_path, "chart-a", [_obj("v1", "ConfigMap", "keep")])
    kube = FakeKube()
    kube.add_object(_live_app("chart-a", automated={"prune": True, "selfHeal": True}))
    rc = _cmd(_apply_args(tmp_path, kube=kube, app="chart-a"))
    err = capsys.readouterr().err
    assert rc == EXIT_GATE
    assert "chart-a" in err
    assert TWO_APPLIERS in err
    assert kube.apply_calls == []
    assert kube.mutations == [] or all(m.get("verb") != "server_side_apply" for m in kube.mutations)


def test_gate_passes_when_application_absent(tmp_path, capsys):
    _instance(tmp_path, "chart-a", [_obj("v1", "ConfigMap", "keep")])
    kube = FakeKube()
    rc = _cmd(_apply_args(tmp_path, kube=kube, app="chart-a"))
    capsys.readouterr()
    assert rc == EXIT_OK
    assert "ConfigMap" in _kinds_applied(kube)
    assert all(c["field_manager"] == "orbit" and c["force_conflicts"] is True for c in kube.apply_calls)


def test_gate_passes_when_application_is_manual(tmp_path, capsys):
    _instance(tmp_path, "chart-a", [_obj("v1", "ConfigMap", "keep")])
    kube = FakeKube()
    kube.add_object(_live_app("chart-a", automated=None))
    rc = _cmd(_apply_args(tmp_path, kube=kube, app="chart-a"))
    capsys.readouterr()
    assert rc == EXIT_OK
    assert _kinds_applied(kube)


def test_gate_dry_run_reports_automated_but_does_not_enforce(tmp_path, capsys):
    _instance(tmp_path, "chart-a", [_obj("v1", "ConfigMap", "keep")])
    kube = FakeKube()
    kube.add_object(_live_app("chart-a", automated={}))
    rc = _cmd(_apply_args(tmp_path, kube=kube, app="chart-a", dry_run=True))
    out, err = capsys.readouterr()
    assert rc == EXIT_OK
    assert TWO_APPLIERS in err
    assert "not enforced" in err
    body = json.loads(out)
    assert body["apps"][0]["gate"] == "reported"
    assert kube.apply_calls
    assert all(c["dry_run"] == "All" for c in kube.apply_calls)
    assert not _inventory(kube, "chart-a")


# ------------------------------------------------------------------ CRD order and Established wait

def test_crd_applied_first_then_namespace_then_rest(tmp_path, capsys):
    crd = _obj("apiextensions.k8s.io/v1", "CustomResourceDefinition", "widgets.example.io",
               spec={"group": "example.io"})
    cm = _obj("v1", "ConfigMap", "keep")
    _instance(tmp_path, "dir-a", [cm, crd])
    kube = FakeKube()
    rc = _cmd(_apply_args(tmp_path, kube=kube, app="dir-a"))
    capsys.readouterr()
    assert rc == EXIT_OK
    kinds = _kinds_applied(kube)
    assert kinds[0] == "CustomResourceDefinition"
    assert kinds[1] == "Namespace"
    assert "ConfigMap" in kinds
    crd_gets = [
        c for c in kube.dynamic_calls
        if c.get("kind") == "CustomResourceDefinition" and c.get("name") == "widgets.example.io"
    ]
    assert crd_gets, "Established wait must GET the CRD"


def test_established_wait_fails_the_app_when_status_never_arrives(tmp_path, capsys, monkeypatch):
    crd = _obj("apiextensions.k8s.io/v1", "CustomResourceDefinition", "widgets.example.io",
               spec={"group": "example.io"})
    cm = _obj("v1", "ConfigMap", "keep")
    _instance(tmp_path, "dir-a", [cm, crd], create_ns=False)
    kube = FakeKube()
    kube.stamp_crd_established = False
    monkeypatch.setattr("orbitd.platform_apply.CRD_ESTABLISHED_TIMEOUT", 0)
    monkeypatch.setattr("orbitd.platform_apply.CRD_ESTABLISHED_POLL", 0)
    rc = _cmd(_apply_args(tmp_path, kube=kube, app="dir-a"))
    capsys.readouterr()
    assert rc == EXIT_FAILED
    assert _kinds_applied(kube) == ["CustomResourceDefinition"]
    assert not _inventory(kube, "dir-a")


# ------------------------------------------------------------------ generated Secrets and checksums

def test_generated_secret_skipped_when_present_created_when_missing(tmp_path, capsys):
    minted = _obj("v1", "Secret", "minted", data={"password": "cA=="})
    keep = _obj("v1", "ConfigMap", "keep", data={"k": "v"})
    _instance(tmp_path, "chart-a", [minted, keep], generated="minted", create_ns=False)
    kube = FakeKube()
    _add_live(kube, _obj("v1", "Secret", "minted", "proj-a", data={"password": "cA=="}))
    rc = _cmd(_apply_args(tmp_path, kube=kube, app="chart-a"))
    out = capsys.readouterr().out
    assert rc == EXIT_OK
    body = json.loads(out)
    results = {(r["ref"]["kind"], r["ref"]["name"], r["result"]) for r in body["apps"][0]["results"]}
    assert ("Secret", "minted", "skipped") in results
    assert ("ConfigMap", "keep", "applied") in results
    secret_applies = [c for c in kube.apply_calls if c["kind"] == "Secret" and c["name"] == "minted"]
    assert secret_applies == []

    kube2 = FakeKube()
    rc = _cmd(_apply_args(tmp_path, kube=kube2, app="chart-a"))
    capsys.readouterr()
    assert rc == EXIT_OK
    creates = [m for m in kube2.mutations if m["verb"] == "create" and m["kind"] == "Secret"]
    assert len(creates) == 1 and creates[0]["field_manager"] == "orbit"
    assert not any(c["kind"] == "Secret" for c in kube2.apply_calls)
    assert any(o["kind"] == "Secret" and o["metadata"]["name"] == "minted" for o in kube2.objects)


def test_tls_generated_secret_skipped_when_present(tmp_path, capsys):
    tls = _obj(
        "v1", "Secret", "webhook-tls",
        type="kubernetes.io/tls",
        data={"tls.crt": "YQ==", "tls.key": "YQ=="},
    )
    _instance(tmp_path, "chart-a", [tls], create_ns=False)
    kube = FakeKube()
    _add_live(kube, _obj(
        "v1", "Secret", "webhook-tls", "proj-a",
        type="kubernetes.io/tls",
        data={"tls.crt": "YQ==", "tls.key": "YQ=="},
    ))
    rc = _cmd(_apply_args(tmp_path, kube=kube, app="chart-a"))
    out = capsys.readouterr().out
    assert rc == EXIT_OK
    body = json.loads(out)
    assert body["apps"][0]["results"][0]["result"] == "skipped"
    assert not any(c["kind"] == "Secret" for c in kube.apply_calls)


def test_checksum_annotations_stripped_before_apply(tmp_path, capsys):
    dep = _obj(
        "apps/v1", "Deployment", "web",
        spec={
            "template": {
                "metadata": {"annotations": {"checksum/config": "deadbeef", "keep": "1"}},
                "spec": {"containers": [{"name": "web", "image": "example/web:1"}]},
            },
        },
    )
    _instance(tmp_path, "chart-a", [dep], create_ns=False)
    kube = FakeKube()
    rc = _cmd(_apply_args(tmp_path, kube=kube, app="chart-a"))
    capsys.readouterr()
    assert rc == EXIT_OK
    bodies = [m["body"] for m in kube.mutations if m.get("verb") == "server_side_apply"]
    dep_bodies = [b for b in bodies if b.get("kind") == "Deployment"]
    assert dep_bodies
    ann = dep_bodies[0]["spec"]["template"]["metadata"]["annotations"]
    assert "checksum/config" not in ann
    assert ann["keep"] == "1"


@pytest.mark.parametrize("kind", list(_WORKLOAD_KINDS))
@pytest.mark.parametrize("dry_run", [False, True], ids=["apply", "dry-run"])
def test_apply_omits_render_stamps_on_workload(kind, dry_run, tmp_path, capsys):
    _instance(tmp_path, "chart-a", [_stamped_workload(kind)], create_ns=False)
    kube = FakeKube()
    rc = _cmd(_apply_args(tmp_path, kube=kube, app="chart-a", dry_run=dry_run))
    capsys.readouterr()
    assert rc == EXIT_OK
    bodies = [
        m["body"] for m in kube.mutations
        if m.get("verb") == "server_side_apply" and m["body"].get("kind") == kind
    ]
    assert bodies, kind
    _assert_stamps_omitted(bodies[0])
    if dry_run:
        assert all(c["dry_run"] == "All" for c in kube.apply_calls)


def test_render_stamp_annotations_are_ignored_by_compare():
    """Adding a stamp to the tuple without an ignore row, or an ignore-table
    stamp path whose key is not in the tuple, turns this red."""
    stamp_set = set(RENDER_STAMP_ANNOTATIONS)
    assert stamp_set

    ignored_annotation_keys: set[str] = set()
    for _av, _kind, pointer in _IGNORE:
        if "/annotations/" in pointer:
            ignored_annotation_keys.add(_unescape(pointer.rsplit("/", 1)[-1]))
    missing = stamp_set - ignored_annotation_keys
    assert not missing, missing

    prefixes = (
        "/metadata/annotations/",
        "/spec/template/metadata/annotations/",
    )
    found: set[str] = set()
    for av, kind, pointer in _IGNORE:
        if av != "apps/v1" or kind not in _WORKLOAD_KINDS:
            continue
        for prefix in prefixes:
            if not pointer.startswith(prefix):
                continue
            rest = pointer[len(prefix):]
            if rest and "/" not in rest:
                key = _unescape(rest)
                if key != "deprecated.daemonset.template.generation":
                    found.add(key)
    assert found == stamp_set, (found, stamp_set)

    from orbitd.shadow import _changed_paths, _normalise_live

    for stamp in RENDER_STAMP_ANNOTATIONS:
        rendered = _stamped_workload("Deployment")
        live = deepcopy(rendered)
        rendered["spec"]["template"]["metadata"]["annotations"][stamp] = "render-time"
        live["spec"]["template"]["metadata"]["annotations"][stamp] = "live-time"
        paths = _changed_paths(
            _normalise_live(rendered), _normalise_live(live),
            kind="Deployment", api_version="apps/v1",
        )
        assert paths == [], (stamp, paths)


# ------------------------------------------------------------------ dry-run

def test_dry_run_sends_only_dry_requests_and_writes_no_inventory(tmp_path, capsys):
    _instance(tmp_path, "chart-a", [_obj("v1", "ConfigMap", "keep")])
    kube = FakeKube()
    before = list(kube.objects)
    rc = _cmd(_apply_args(tmp_path, kube=kube, app="chart-a", dry_run=True))
    capsys.readouterr()
    assert rc == EXIT_OK
    assert kube.apply_calls
    assert all(c["dry_run"] == "All" for c in kube.apply_calls)
    assert kube.objects == before
    assert not _inventory(kube, "chart-a")
    assert kube.delete_calls == []


# ------------------------------------------------------------------ prune

ORBIT_APPLIED = [{"manager": "orbit", "operation": "Apply", "apiVersion": "v1"}]


def _orbit_applied(obj: dict) -> dict:
    """A live copy whose managedFields carry field manager orbit's Apply entry."""
    out = deepcopy(obj)
    out["metadata"]["managedFields"] = deepcopy(ORBIT_APPLIED)
    return out


def _seed_inventory(kube: FakeKube, app: str, refs: list[ObjectRef],
                    generated: list[ObjectRef] = ()) -> None:
    kube.add_object({
        "apiVersion": "v1",
        "kind": "ConfigMap",
        "metadata": {
            "name": f"orbit-platform-inventory-{app}",
            "namespace": "orbit",
            "uid": "inv",
            "resourceVersion": "1",
        },
        "data": {
            "objects": "\n".join(sorted(r.line for r in refs)),
            "generated": "\n".join(sorted(r.line for r in generated)),
            "applied_at": "2026-01-01T00:00:00Z",
        },
    })


def test_prune_refuses_crd_kube_system_generated_and_requires_typed_confirmation(tmp_path, capsys):
    keep = _obj("v1", "ConfigMap", "keep")
    _instance(tmp_path, "chart-a", [keep], generated="minted", create_ns=False)
    kube = FakeKube()
    leftover = _obj("v1", "ConfigMap", "old-cm", "proj-a", data={"k": "v"})
    crd = _obj("apiextensions.k8s.io/v1", "CustomResourceDefinition", "oldwidgets.example.io")
    sys_cm = _obj("v1", "ConfigMap", "coredns", "kube-system", data={})
    minted = _obj("v1", "Secret", "minted", "proj-a", data={"password": "cA=="})
    stored_leftover = _add_live(kube, _orbit_applied(leftover))
    _add_live(kube, crd)
    _add_live(kube, sys_cm)
    _add_live(kube, minted)
    rendered_ref = ObjectRef.of(_obj("v1", "ConfigMap", "keep", "proj-a"))
    _seed_inventory(kube, "chart-a", [
        rendered_ref,
        ObjectRef.of(leftover),
        ObjectRef.of(crd),
        ObjectRef.of(sys_cm),
        ObjectRef.of(minted),
    ])

    rc = _cmd(_apply_args(tmp_path, kube=kube, app="chart-a", prune=True), stdin="nope\n")
    out, err = capsys.readouterr()
    assert rc == EXIT_FAILED
    assert "prune chart-a" in err
    assert kube.delete_calls == []
    assert stored_leftover in kube.objects

    kube2 = FakeKube()
    _add_live(kube2, _orbit_applied(leftover))
    _add_live(kube2, crd)
    _add_live(kube2, sys_cm)
    _add_live(kube2, minted)
    _seed_inventory(kube2, "chart-a", [
        rendered_ref,
        ObjectRef.of(leftover),
        ObjectRef.of(crd),
        ObjectRef.of(sys_cm),
        ObjectRef.of(minted),
    ])
    rc = _cmd(_apply_args(tmp_path, kube=kube2, app="chart-a", prune=True), stdin="prune chart-a\n")
    out, err = capsys.readouterr()
    assert rc == EXIT_OK
    body = json.loads(out)
    prune_rows = body["apps"][0]["prune"]
    by_name = {r["ref"]["name"]: r for r in prune_rows}
    assert by_name["oldwidgets.example.io"]["result"] == "withheld"
    assert by_name["oldwidgets.example.io"]["reason"] == RULE_CRD
    assert by_name["coredns"]["result"] == "withheld"
    assert by_name["coredns"]["reason"] == RULE_KUBE_SYSTEM
    assert by_name["minted"]["result"] == "withheld"
    assert by_name["minted"]["reason"] == RULE_GENERATED
    assert by_name["old-cm"]["result"] == "deleted"
    assert not any((o.get("metadata") or {}).get("name") == "old-cm" for o in kube2.objects)
    assert any(o.get("metadata", {}).get("name") == "oldwidgets.example.io" for o in kube2.objects)
    assert any(o.get("metadata", {}).get("name") == "coredns" for o in kube2.objects)
    assert any(o.get("metadata", {}).get("name") == "minted" for o in kube2.objects)
    assert "type 'prune chart-a' to delete" in err


def test_prune_dry_run_leaves_the_object_in_the_cluster(tmp_path, capsys):
    keep = _obj("v1", "ConfigMap", "keep")
    _instance(tmp_path, "chart-a", [keep], create_ns=False)
    leftover = _obj("v1", "ConfigMap", "old-cm", "proj-a", data={})
    kube = FakeKube()
    stored_leftover = _add_live(kube, _orbit_applied(leftover))
    _seed_inventory(kube, "chart-a", [
        ObjectRef.of(_obj("v1", "ConfigMap", "keep", "proj-a")),
        ObjectRef.of(leftover),
    ])
    rc = _cmd(
        _apply_args(tmp_path, kube=kube, app="chart-a", dry_run=True, prune=True),
        stdin="prune chart-a\n",
    )
    out = capsys.readouterr().out
    assert rc == EXIT_OK
    assert stored_leftover in kube.objects
    assert kube.delete_calls
    for c in kube.delete_calls:
        assert isinstance(c.get("body"), dict) and c["body"].get("dryRun") == ["All"]
    body = json.loads(out)
    assert [r["result"] for r in body["apps"][0]["prune"]] == ["deleted"]


def test_prune_dry_run_still_requires_confirmation(tmp_path, capsys):
    keep = _obj("v1", "ConfigMap", "keep")
    _instance(tmp_path, "chart-a", [keep], create_ns=False)
    leftover = _obj("v1", "ConfigMap", "old-cm", "proj-a", data={})
    kube = FakeKube()
    stored_leftover = _add_live(kube, _orbit_applied(leftover))
    _seed_inventory(kube, "chart-a", [
        ObjectRef.of(_obj("v1", "ConfigMap", "keep", "proj-a")),
        ObjectRef.of(leftover),
    ])
    rc = _cmd(
        _apply_args(tmp_path, kube=kube, app="chart-a", dry_run=True, prune=True),
        stdin="",
    )
    capsys.readouterr()
    assert rc == EXIT_FAILED
    assert kube.delete_calls == []
    assert stored_leftover in kube.objects


def test_platform_prune_rule_names_the_three_extra_refusals():
    crd = ObjectRef(api_version="apiextensions.k8s.io/v1", kind="CustomResourceDefinition",
                    namespace="", name="widgets.example.io")
    assert platform_prune_rule("chart-a", crd, {"kind": "CustomResourceDefinition"},
                               named_generated=frozenset()) == RULE_CRD
    sys_cm = ObjectRef(api_version="v1", kind="ConfigMap", namespace="kube-system", name="coredns")
    assert platform_prune_rule("chart-a", sys_cm, {"metadata": {"namespace": "kube-system"}},
                               named_generated=frozenset()) == RULE_KUBE_SYSTEM
    secret = ObjectRef(api_version="v1", kind="Secret", namespace="proj-a", name="minted")
    assert platform_prune_rule("chart-a", secret, {"kind": "Secret", "metadata": {"name": "minted"}},
                               named_generated=frozenset({"minted"})) == RULE_GENERATED


# ------------------------------------------------------------------ exit codes and per-app continue

def test_missing_instance_dir_exits_2(tmp_path, capsys):
    kube = FakeKube()
    rc = _cmd(_apply_args(tmp_path / "no-such", kube=kube))
    err = capsys.readouterr().err
    assert rc == EXIT_RENDER
    assert "not a directory" in err
    assert kube.apply_calls == []


def test_per_app_failure_continues_and_exits_1(tmp_path, capsys):
    _instance(tmp_path, "chart-a", [_obj("v1", "ConfigMap", "keep-a")], create_ns=False)
    _instance(tmp_path, "dir-a", [_obj("v1", "ConfigMap", "keep-b")], create_ns=False)
    kube = FakeKube()
    kube.apply_errors[("ConfigMap", "proj-a", "keep-b")] = RuntimeError("Forbidden: nope")
    rc = _cmd(_apply_args(tmp_path, kube=kube))
    out = capsys.readouterr().out
    assert rc == EXIT_FAILED
    body = json.loads(out)
    by_name = {a["name"]: a for a in body["apps"]}
    assert by_name["chart-a"]["ok"] is True
    assert by_name["dir-a"]["ok"] is False
    assert _inventory(kube, "chart-a")
    assert not _inventory(kube, "dir-a")


def test_apply_writes_inventory_named_for_the_app(tmp_path, capsys):
    _instance(tmp_path, "chart-a", [_obj("v1", "ConfigMap", "keep")], create_ns=False)
    kube = FakeKube()
    rc = _cmd(_apply_args(tmp_path, kube=kube, app="chart-a"))
    capsys.readouterr()
    assert rc == EXIT_OK
    stored = _inventory(kube, "chart-a")
    assert len(stored) == 1
    lines = stored[0]["data"]["objects"].splitlines()
    assert "/v1/ConfigMap/proj-a/keep" in lines


def test_unknown_app_exits_2(tmp_path, capsys):
    _instance(tmp_path, "chart-a", [_obj("v1", "ConfigMap", "keep")])
    kube = FakeKube()
    rc = _cmd(_apply_args(tmp_path, kube=kube, app="missing-app"))
    err = capsys.readouterr().err
    assert rc == EXIT_RENDER
    assert "missing-app" in err


# ------------------------------------------------------------------ CLI wiring

def test_platform_main_apply_uses_injected_kube(tmp_path, capsys, monkeypatch):
    _instance(tmp_path, "chart-a", [_obj("v1", "ConfigMap", "keep")], create_ns=False)
    kube = FakeKube()
    monkeypatch.setattr("orbitd.platform_apply.Kube", lambda kubeconfig=None: kube)
    rc = main(["apply", str(tmp_path), "--app", "chart-a"])
    capsys.readouterr()
    assert rc == EXIT_OK
    assert _inventory(kube, "chart-a")


@pytest.mark.anyio
async def test_orbit_cli_platform_apply_needs_no_token(tmp_path, capsys, monkeypatch):
    _instance(tmp_path, "chart-a", [_obj("v1", "ConfigMap", "keep")], create_ns=False)
    kube = FakeKube()
    monkeypatch.setattr("orbitd.platform_apply.Kube", lambda kubeconfig=None: kube)
    rc = await async_main(["platform", "apply", str(tmp_path), "--app", "chart-a", "--dry-run"],
                          stdin=io.StringIO(""))
    capsys.readouterr()
    assert rc == EXIT_OK
    assert all(c["dry_run"] == "All" for c in kube.apply_calls)
    assert not _inventory(kube, "chart-a")


# ------------------------------------------------------------------ review repairs (one test per defect)

def _live(kube: FakeKube, kind: str, namespace: str, name: str) -> dict | None:
    for o in kube.objects:
        meta = o.get("metadata") or {}
        if o.get("kind") == kind and meta.get("name") == name and (meta.get("namespace") or "") == namespace:
            return o
    return None


def test_crds_and_namespaces_of_every_app_land_before_any_dependant(tmp_path, capsys):
    """The break: app a-work's ConfigMap/Deployment/Widget are applied before app
    b-crds' CRD and Namespace because each app is finished before the next starts."""
    _instance(tmp_path, "a-work", [
        _obj("v1", "ConfigMap", "cfg"),
        _obj("apps/v1", "Deployment", "web"),
        _obj("example.io/v1", "Widget", "w1"),
    ], dest="ns-a")
    _instance(tmp_path, "b-crds", [
        _obj("apiextensions.k8s.io/v1", "CustomResourceDefinition", "widgets.example.io",
             spec={"group": "example.io"}),
        _obj("v1", "ConfigMap", "cfg-b"),
    ], dest="ns-b")
    kube = FakeKube()
    rc = _cmd(_apply_args(tmp_path, kube=kube))
    capsys.readouterr()
    assert rc == EXIT_OK
    kinds = _kinds_applied(kube)
    first = ("CustomResourceDefinition", "Namespace")
    last_head = max(i for i, k in enumerate(kinds) if k in first)
    first_rest = min(i for i, k in enumerate(kinds) if k not in first)
    assert kinds.count("CustomResourceDefinition") == 1 and kinds.count("Namespace") == 2
    assert last_head < first_rest, kinds
    assert kinds[0] == "CustomResourceDefinition"


def test_generated_secret_created_between_plan_and_apply_keeps_its_data(tmp_path, capsys, monkeypatch):
    """The break: a read-then-forced-apply overwrites a Secret another writer created
    after the read, replacing its password."""
    _instance(tmp_path, "chart-a", [_obj("v1", "Secret", "minted", data={"password": "QQ=="})],
              generated="minted", create_ns=False)
    kube = FakeKube()
    theirs = _obj("v1", "Secret", "minted", "proj-a", data={"password": "Qg=="})

    def race(original):
        def write(self, *args, **kwargs):
            if self.kind == "Secret" and _live(kube, "Secret", "proj-a", "minted") is None:
                _add_live(kube, theirs, uid="uid-theirs", rv="7")
            return original(self, *args, **kwargs)
        return write

    monkeypatch.setattr(FakeDynamicResource, "server_side_apply",
                        race(FakeDynamicResource.server_side_apply))
    monkeypatch.setattr(FakeDynamicResource, "create", race(FakeDynamicResource.create))
    rc = _cmd(_apply_args(tmp_path, kube=kube, app="chart-a"))
    out = capsys.readouterr().out
    assert rc == EXIT_OK
    live = _live(kube, "Secret", "proj-a", "minted")
    assert live["data"] == {"password": "Qg=="}
    assert live["metadata"]["uid"] == "uid-theirs"
    row = json.loads(out)["apps"][0]["results"]
    assert [(r["ref"]["name"], r["result"], r["reason"]) for r in row] == [
        ("minted", "skipped", "generated Secret already present")]


def test_refused_prune_keeps_previous_entries_and_a_later_run_deletes_them(tmp_path, capsys):
    """The break: the inventory is rewritten to this run's render only, so after a
    refused confirmation the dropped object can never be pruned again."""
    one = _instance(tmp_path / "one", "chart-a",
                    [_obj("v1", "ConfigMap", "keep"), _obj("v1", "ConfigMap", "old")], create_ns=False)
    two = _instance(tmp_path / "two", "chart-a", [_obj("v1", "ConfigMap", "keep")], create_ns=False)
    kube = FakeKube()
    assert _cmd(_apply_args(one, kube=kube, app="chart-a")) == EXIT_OK

    rc = _cmd(_apply_args(two, kube=kube, app="chart-a", prune=True), stdin="no\n")
    capsys.readouterr()
    assert rc == EXIT_FAILED
    assert _live(kube, "ConfigMap", "proj-a", "old") is not None
    lines = _inventory(kube, "chart-a")[0]["data"]["objects"].splitlines()
    assert "/v1/ConfigMap/proj-a/old" in lines and "/v1/ConfigMap/proj-a/keep" in lines

    rc = _cmd(_apply_args(two, kube=kube, app="chart-a", prune=True), stdin="prune chart-a\n")
    out = capsys.readouterr().out
    assert rc == EXIT_OK
    assert _live(kube, "ConfigMap", "proj-a", "old") is None
    prune_rows = json.loads(out)["apps"][0]["prune"]
    assert [(r["ref"]["name"], r["result"]) for r in prune_rows] == [("old", "deleted")]
    assert _inventory(kube, "chart-a")[0]["data"]["objects"].splitlines() == ["/v1/ConfigMap/proj-a/keep"]


def test_api_error_on_second_of_three_apps_keeps_first_tries_third_exits_1(tmp_path, capsys, monkeypatch):
    """The break: an API error outside the object applies escapes the run, so the
    first app's results are lost, the third app never runs and no report prints."""
    for app, cm in (("app-a", "keep-a"), ("app-b", "keep-b"), ("app-c", "keep-c")):
        _instance(tmp_path, app, [_obj("v1", "ConfigMap", cm)], create_ns=False)
    kube = FakeKube()
    real_get = FakeDynamicResource.get

    def get(self, name=None, namespace=None, *args, **kwargs):
        if (self.kind, namespace, name) == ("ConfigMap", "proj-a", "keep-b"):
            raise ApiException(status=500, reason="Internal Server Error: etcd timeout")
        return real_get(self, name, namespace, *args, **kwargs)

    monkeypatch.setattr(FakeDynamicResource, "get", get)
    rc = _cmd(_apply_args(tmp_path, kube=kube))
    out = capsys.readouterr().out
    assert rc == EXIT_FAILED
    body = json.loads(out)
    assert body["ok"] is False
    by_name = {a["name"]: a for a in body["apps"]}
    assert list(by_name) == ["app-a", "app-b", "app-c"]
    assert by_name["app-a"]["ok"] is True
    assert [(r["ref"]["name"], r["result"]) for r in by_name["app-a"]["results"]] == [("keep-a", "applied")]
    assert by_name["app-b"]["ok"] is False
    assert "etcd timeout" in by_name["app-b"]["error"]
    assert by_name["app-c"]["ok"] is True
    assert [(r["ref"]["name"], r["result"]) for r in by_name["app-c"]["results"]] == [("keep-c", "applied")]
    assert _inventory(kube, "app-a") and _inventory(kube, "app-c")
    assert not _inventory(kube, "app-b")


def test_prune_refuses_objects_it_cannot_prove_orbit_applied(tmp_path, capsys):
    """The break: the tenant namespace/label refusals were turned into permission, so an
    inventoried object in another namespace, owned by another writer, was deleted."""
    _instance(tmp_path, "chart-a", [_obj("v1", "ConfigMap", "keep")], create_ns=False)
    foreign = _obj("v1", "ConfigMap", "foreign", "other-team", data={})
    foreign["metadata"]["managedFields"] = [{"manager": "kubectl", "operation": "Apply"}]
    bare = _obj("v1", "ConfigMap", "bare", "other-team", data={})
    ours = _orbit_applied(_obj("v1", "ConfigMap", "ours", "other-team", data={}))
    kube = FakeKube()
    for o in (foreign, bare, ours):
        _add_live(kube, o)
    _seed_inventory(kube, "chart-a", [ObjectRef.of(_obj("v1", "ConfigMap", "keep", "proj-a")),
                                      ObjectRef.of(foreign), ObjectRef.of(bare), ObjectRef.of(ours)])
    rc = _cmd(_apply_args(tmp_path, kube=kube, app="chart-a", prune=True), stdin="prune chart-a\n")
    out = capsys.readouterr().out
    assert rc == EXIT_OK
    by_name = {r["ref"]["name"]: r for r in json.loads(out)["apps"][0]["prune"]}
    assert by_name["foreign"]["result"] == "withheld"
    assert "not applied by field manager orbit" in by_name["foreign"]["reason"]
    assert by_name["bare"]["result"] == "withheld"
    assert "managedFields" in by_name["bare"]["reason"]
    assert by_name["ours"]["result"] == "deleted"
    assert [c["name"] for c in kube.delete_calls] == ["ours"]
    assert _live(kube, "ConfigMap", "other-team", "foreign") is not None
    assert _live(kube, "ConfigMap", "other-team", "bare") is not None

    # Ownership without an inventory entry is not enough.
    stray = ObjectRef(api_version="v1", kind="ConfigMap", namespace="other-team", name="ours")
    assert platform_prune_rule("chart-a", stray, _orbit_applied(ours), named_generated=frozenset(),
                               inventory={}) is not None


def test_entry_marked_generated_survives_its_removal_from_the_render(tmp_path, capsys):
    """The break: the generated classification came only from the current render, so a
    password Secret dropped from the chart became prunable."""
    one = _instance(tmp_path / "one", "chart-a",
                    [_obj("v1", "ConfigMap", "keep"), _obj("v1", "Secret", "minted", data={"password": "QQ=="})],
                    generated="minted", create_ns=False)
    two = _instance(tmp_path / "two", "chart-a", [_obj("v1", "ConfigMap", "keep")], create_ns=False)
    kube = FakeKube()
    assert _cmd(_apply_args(one, kube=kube, app="chart-a")) == EXIT_OK
    capsys.readouterr()
    minted = _live(kube, "Secret", "proj-a", "minted")
    assert minted is not None
    # Even with orbit's Apply entry on it (an earlier applier wrote it that way).
    minted["metadata"]["managedFields"] = deepcopy(ORBIT_APPLIED)

    rc = _cmd(_apply_args(two, kube=kube, app="chart-a", prune=True), stdin="prune chart-a\n")
    out = capsys.readouterr().out
    assert rc == EXIT_OK
    rows = json.loads(out)["apps"][0]["prune"]
    assert [(r["ref"]["name"], r["result"], r["reason"]) for r in rows] == [
        ("minted", "withheld", RULE_GENERATED)]
    assert _live(kube, "Secret", "proj-a", "minted") is not None
    assert kube.delete_calls == []


def test_platform_inventory_name_cannot_collide_with_a_tenant_inventory():
    """The break: the platform inventory for app X is the tenant inventory of a project
    called `platform` with env X."""
    ref = inventory_ref_for_app("monitoring")
    assert (ref.namespace, ref.name) == ("orbit", "orbit-platform-inventory-monitoring")
    assert ref.name != inventory_name("platform", "monitoring")


def _catalog_sandbox(root: Path, project_id: str) -> subprocess.CompletedProcess:
    testdata = REPO / "scripts" / "testdata" / "catalog-apply-pairs"
    (root / "scripts").mkdir(parents=True)
    shutil.copy2(REPO / "scripts" / "catalog-sync.py", root / "scripts" / "catalog-sync.py")
    # the generator imports the shared display rule from beside itself, as vendored
    (root / "scripts" / "verify").mkdir()
    shutil.copy2(REPO / "scripts" / "verify" / "shown.py", root / "scripts" / "verify" / "shown.py")
    shutil.copy2(REPO / "scripts" / "verify" / "orbit_features.py", root / "scripts" / "verify" / "orbit_features.py")
    shutil.copytree(REPO / "charts" / "service", root / "charts" / "service")
    shutil.copytree(testdata / "catalog", root / "catalog")
    shutil.copytree(testdata / "instance", root / "instance")
    (root / "clusters" / "apps").mkdir(parents=True)
    ns = Path("template/provisioning/clusters/platform/config/orbitd/namespace.yaml")
    (root / ns).parent.mkdir(parents=True)
    shutil.copy2(REPO / ns, root / ns)
    catalog = yaml.safe_load((root / "catalog" / "projects.yaml").read_text())
    catalog["projects"][0]["id"] = project_id
    (root / "catalog" / "projects.yaml").write_text(yaml.safe_dump(catalog, sort_keys=False))
    return subprocess.run([sys.executable, "scripts/catalog-sync.py"], cwd=root,
                          capture_output=True, text=True)


def test_catalog_sync_rejects_a_project_named_platform(tmp_path):
    """The break: catalog-sync accepts a tenant project whose id is the platform layer's."""
    control = _catalog_sandbox(tmp_path / "control", "alpha")
    assert control.returncode == 0, control.stdout + control.stderr
    proc = _catalog_sandbox(tmp_path / "platform", "platform")
    assert proc.returncode != 0
    assert "reserved for the platform layer" in proc.stderr
