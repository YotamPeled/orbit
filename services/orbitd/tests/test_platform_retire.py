"""orbit platform retire: delete what a platform app applied once the instance no longer
renders it.

`apply --prune` only walks apps the instance renders, so an app whose Application file is
gone (Infisical, ESO, Zitadel, OBI) leaves its stored inventory and every object in it
behind. `retire --app NAME` reads that inventory and deletes what orbit applied, under the
prune refusals plus three of its own: a Namespace another stored inventory still uses, a
CustomResourceDefinition while any object of its kind exists, and any run that would
garbage-collect a Secret outside the namespaces it deletes."""

from __future__ import annotations

import io
import json

import test_platform_apply as tpa
from orbitd.applier import ObjectRef
from orbitd.platform import main
from orbitd.platform_apply import EXIT_FAILED, EXIT_OK
from orbitd.testing import FakeKube

APP = "old"


def owned(obj: dict) -> dict:
    return tpa._orbit_applied(obj)


def ns(name: str) -> dict:
    return tpa._obj("v1", "Namespace", name)


def cm(name: str, namespace: str) -> dict:
    return tpa._obj("v1", "ConfigMap", name, namespace, data={"k": "v"})


def crd(group: str, kind: str, plural: str) -> dict:
    return tpa._obj("apiextensions.k8s.io/v1", "CustomResourceDefinition", f"{plural}.{group}",
                    spec={"group": group, "scope": "Namespaced",
                          "names": {"kind": kind, "plural": plural},
                          "versions": [{"name": "v1", "served": True, "storage": True}]})


def rendered_instance(tmp_path, *apps: str):
    """An instance that renders `apps` (and never APP unless named)."""
    for a in apps:
        tpa._instance(tmp_path, a, [cm("keep", "proj-a")], create_ns=False)
    (tmp_path / "clusters" / "platform" / "apps").mkdir(parents=True, exist_ok=True)
    return tmp_path


def seed(kube: FakeKube, app: str, objs: list[dict], *, live=True) -> None:
    for i, o in enumerate(objs):
        if live:
            tpa._add_live(kube, owned(o), uid=f"uid-{app}-{i}")
    tpa._seed_inventory(kube, app, [ObjectRef.of(o) for o in objs])


def run(monkeypatch, kube, tmp_path, *args, stdin="") -> int:
    monkeypatch.setattr("orbitd.platform_apply.Kube", lambda kubeconfig=None: kube)
    monkeypatch.setattr("sys.stdin", io.StringIO(stdin))
    return main(["retire", str(tmp_path), *args])


def names(kube, kind):
    return {(o["metadata"].get("namespace", ""), o["metadata"]["name"])
            for o in kube.objects if o.get("kind") == kind}


def inventory_left(kube, app=APP):
    return tpa._inventory(kube, app)


def rows(out: str) -> dict:
    body = json.loads(out)
    return {r["ref"]["name"]: r for app in body["apps"] for r in app["results"]}


def test_a_retired_app_is_deleted_after_typed_confirmation(tmp_path, capsys, monkeypatch):
    rendered_instance(tmp_path, "live-app")
    kube = FakeKube()
    seed(kube, APP, [ns("old-ns"), cm("a", "old-ns"), cm("b", "old-ns")])
    assert run(monkeypatch, kube, tmp_path, "--app", APP, stdin="retire old\n") == EXIT_OK
    assert names(kube, "ConfigMap") & {("old-ns", "a"), ("old-ns", "b")} == set()
    assert ("", "old-ns") not in names(kube, "Namespace")
    assert inventory_left(kube) == [], "a fully retired app's inventory is deleted"


def test_without_the_typed_confirmation_nothing_is_deleted(tmp_path, capsys, monkeypatch):
    rendered_instance(tmp_path)
    kube = FakeKube()
    seed(kube, APP, [ns("old-ns"), cm("a", "old-ns")])
    assert run(monkeypatch, kube, tmp_path, "--app", APP, stdin="yes\n") == EXIT_FAILED
    assert kube.delete_calls == []
    assert "retire old" in capsys.readouterr().err


def test_dry_run_deletes_nothing_and_names_every_object(tmp_path, capsys, monkeypatch):
    rendered_instance(tmp_path)
    kube = FakeKube()
    seed(kube, APP, [ns("old-ns"), cm("a", "old-ns")])
    assert run(monkeypatch, kube, tmp_path, "--app", APP, "--dry-run") == EXIT_OK
    assert [c for c in kube.delete_calls if not c.get("dry_run") and not (c.get("body") or {}).get("dryRun")] == []
    got = rows(capsys.readouterr().out)
    assert set(got) >= {"old-ns", "a"}


def test_an_app_the_instance_still_renders_is_refused(tmp_path, capsys, monkeypatch):
    rendered_instance(tmp_path, APP)
    kube = FakeKube()
    seed(kube, APP, [cm("a", "proj-a")])
    assert run(monkeypatch, kube, tmp_path, "--app", APP, stdin="retire old\n") == EXIT_FAILED
    assert kube.delete_calls == []
    assert "apply --prune" in capsys.readouterr().err


def test_an_app_without_a_stored_inventory_is_refused(tmp_path, capsys, monkeypatch):
    rendered_instance(tmp_path)
    kube = FakeKube()
    assert run(monkeypatch, kube, tmp_path, "--app", "never", stdin="retire never\n") == EXIT_FAILED
    assert "inventory" in capsys.readouterr().err


def test_an_object_orbit_did_not_apply_is_withheld(tmp_path, capsys, monkeypatch):
    rendered_instance(tmp_path)
    kube = FakeKube()
    mine, foreign = cm("mine", "old-ns"), cm("foreign", "old-ns")
    tpa._add_live(kube, owned(mine), uid="uid-mine")
    tpa._add_live(kube, foreign, uid="uid-foreign")          # no orbit managedFields
    tpa._seed_inventory(kube, APP, [ObjectRef.of(mine), ObjectRef.of(foreign)])
    assert run(monkeypatch, kube, tmp_path, "--app", APP, stdin="retire old\n") == EXIT_OK
    got = rows(capsys.readouterr().out)
    assert got["mine"]["result"] == "deleted"
    assert got["foreign"]["result"] == "withheld"
    assert ("old-ns", "foreign") in names(kube, "ConfigMap")
    (left,) = inventory_left(kube)
    assert "foreign" in left["data"]["objects"] and "mine" not in left["data"]["objects"], \
        "a withheld entry stays in the inventory so a later run can finish it"


def test_a_namespace_another_inventory_uses_is_withheld(tmp_path, capsys, monkeypatch):
    rendered_instance(tmp_path)
    kube = FakeKube()
    seed(kube, APP, [ns("shared"), cm("a", "shared")])
    tpa._seed_inventory(kube, "other", [ObjectRef.of(cm("b", "shared"))])
    tpa._add_live(kube, owned(cm("b", "shared")), uid="uid-b")
    assert run(monkeypatch, kube, tmp_path, "--app", APP, stdin="retire old\n") == EXIT_OK
    got = rows(capsys.readouterr().out)
    assert got["shared"]["result"] == "withheld"
    assert ("", "shared") in names(kube, "Namespace")
    assert got["a"]["result"] == "deleted"


def test_a_crd_is_deleted_only_when_no_object_of_its_kind_is_left(tmp_path, capsys, monkeypatch):
    rendered_instance(tmp_path)
    kube = FakeKube()
    widgets = crd("example.io", "Widget", "widgets")
    gadgets = crd("example.io", "Gadget", "gadgets")
    seed(kube, APP, [widgets, gadgets])
    tpa._add_live(kube, tpa._obj("example.io/v1", "Widget", "w1", "tenant-a"), uid="uid-w1")
    assert run(monkeypatch, kube, tmp_path, "--app", APP, stdin="retire old\n") == EXIT_OK
    got = rows(capsys.readouterr().out)
    assert got["widgets.example.io"]["result"] == "withheld"
    assert got["gadgets.example.io"]["result"] == "deleted"
    assert ("", "widgets.example.io") in names(kube, "CustomResourceDefinition")


def test_a_run_that_would_collect_a_secret_outside_its_namespaces_deletes_nothing(tmp_path, capsys, monkeypatch):
    """The ExternalSecret case: deleting the owner garbage-collects the Secret it made."""
    rendered_instance(tmp_path)
    kube = FakeKube()
    es = tpa._obj("external-secrets.io/v1", "ExternalSecret", "creds", "tenant-a")
    seed(kube, APP, [ns("old-ns"), es])
    secret = tpa._obj("v1", "Secret", "creds", "tenant-a", data={"k": "eA=="})
    secret["metadata"]["ownerReferences"] = [{"apiVersion": "external-secrets.io/v1",
                                              "kind": "ExternalSecret", "name": "creds",
                                              "uid": "uid-old-1"}]
    tpa._add_live(kube, secret, uid="uid-secret")
    assert run(monkeypatch, kube, tmp_path, "--app", APP, stdin="retire old\n") == EXIT_FAILED
    assert kube.delete_calls == []
    err = capsys.readouterr().err
    assert "tenant-a/creds" in err
    assert "eA==" not in err, "never print a Secret's data"


def _owned_by_the_app(kube, name: str) -> None:
    secret = tpa._obj("v1", "Secret", name, "tenant-a", data={"k": "eA=="})
    secret["metadata"]["ownerReferences"] = [{"apiVersion": "external-secrets.io/v1",
                                              "kind": "ExternalSecret", "name": "creds",
                                              "uid": "uid-old-1"}]
    tpa._add_live(kube, secret, uid=f"uid-{name}")


def _registry(tmp_path, generated: str) -> None:
    (tmp_path / "provisioning").mkdir(exist_ok=True)
    (tmp_path / "provisioning" / "features.yaml").write_text(
        "features:\n  core:\n    always: true\n    summary: core\n"
        f"  things:\n    summary: things\n    apps: [{APP}]\n    generated: [{generated}]\n", encoding="utf-8")


def test_a_secret_the_registry_says_the_app_made_for_itself_goes_with_it(tmp_path, capsys, monkeypatch):
    """The database operator's certificates are owned by its Deployment: every retire of it
    was refused and the databases feature could never be turned off (docs/V1.md §8). A
    Secret the registry names under the feature's `generated` is let go, and said."""
    rendered_instance(tmp_path)
    _registry(tmp_path, "tenant-a/creds")
    kube = FakeKube()
    seed(kube, APP, [ns("old-ns"), tpa._obj("external-secrets.io/v1", "ExternalSecret", "creds", "tenant-a")])
    _owned_by_the_app(kube, "creds")
    assert run(monkeypatch, kube, tmp_path, "--app", APP, stdin="retire old\n") == EXIT_OK
    err = capsys.readouterr().err
    assert "also going, made by the app for itself: Secret tenant-a/creds" in err
    assert "eA==" not in err


def test_only_the_named_secret_is_let_go(tmp_path, capsys, monkeypatch):
    """A second Secret the same owner would collect, not named in the registry, still
    refuses the whole run: nothing is deleted."""
    rendered_instance(tmp_path)
    _registry(tmp_path, "tenant-a/creds")
    kube = FakeKube()
    seed(kube, APP, [ns("old-ns"), tpa._obj("external-secrets.io/v1", "ExternalSecret", "creds", "tenant-a")])
    _owned_by_the_app(kube, "creds")
    _owned_by_the_app(kube, "someone-elses")
    assert run(monkeypatch, kube, tmp_path, "--app", APP, stdin="retire old\n") == EXIT_FAILED
    assert kube.delete_calls == []
    err = capsys.readouterr().err
    assert "garbage-collect Secret tenant-a/someone-elses" in err


def test_another_apps_generated_secret_is_not_this_apps_to_let_go(tmp_path, capsys, monkeypatch):
    rendered_instance(tmp_path)
    (tmp_path / "provisioning").mkdir(exist_ok=True)
    (tmp_path / "provisioning" / "features.yaml").write_text(
        "features:\n  core:\n    always: true\n    summary: core\n"
        "  things:\n    summary: things\n    apps: [some-other-app]\n    generated: [tenant-a/creds]\n", encoding="utf-8")
    kube = FakeKube()
    seed(kube, APP, [ns("old-ns"), tpa._obj("external-secrets.io/v1", "ExternalSecret", "creds", "tenant-a")])
    _owned_by_the_app(kube, "creds")
    assert run(monkeypatch, kube, tmp_path, "--app", APP, stdin="retire old\n") == EXIT_FAILED
    assert kube.delete_calls == []


def test_kube_system_is_never_touched(tmp_path, capsys, monkeypatch):
    rendered_instance(tmp_path)
    kube = FakeKube()
    seed(kube, APP, [cm("coredns-extra", "kube-system"), cm("a", "old-ns")])
    assert run(monkeypatch, kube, tmp_path, "--app", APP, stdin="retire old\n") == EXIT_OK
    got = rows(capsys.readouterr().out)
    assert got["coredns-extra"]["result"] == "withheld"
    assert ("kube-system", "coredns-extra") in names(kube, "ConfigMap")


def test_namespaced_objects_go_before_their_namespace_and_crds_go_last(tmp_path, capsys, monkeypatch):
    rendered_instance(tmp_path)
    kube = FakeKube()
    seed(kube, APP, [crd("example.io", "Gadget", "gadgets"), ns("old-ns"), cm("a", "old-ns")])
    assert run(monkeypatch, kube, tmp_path, "--app", APP, stdin="retire old\n") == EXIT_OK
    order = [c["kind"] for c in kube.delete_calls if c["kind"] != "ConfigMap" or c["name"] == "a"]
    assert order.index("ConfigMap") < order.index("Namespace") < order.index("CustomResourceDefinition")
