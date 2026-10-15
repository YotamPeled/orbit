"""orbit platform retire, round 1 repairs.

The review found six ways the verb deletes more than it says, or says less than it did.
Every one of them is the same failure at heart: the REPORT and the STORED INVENTORY must
describe what actually happened to the cluster, and a refusal must be a refusal rather than
a thing that happens to hold only when nothing goes wrong.

  * A Namespace's deletion garbage-collects what is inside it — including objects this same
    run refused to delete. Withholding them and then deleting their Namespace is not
    withholding; it is deleting them without saying so.
  * A render that could not be READ is not "the instance no longer renders this app". The
    refusal that stops a live app being retired must not be switched off by an I/O error.
  * A discovery call that fails is not "this kind is not served". The same swallow sits in
    the path `secrets export` uses, where it silently writes nothing.
  * A live automated sync will put the objects back, so the gate must stop the run rather
    than be reported as ok.
  * A tenant's inventory keeps a Namespace in use just as a platform one does.
  * An object the run never reached is not an object that survived by choice.
"""

from __future__ import annotations

import io
import json

import pytest
import test_platform_apply as tpa
import test_platform_retire as base
from orbitd.applier import ObjectRef
from orbitd.platform_apply import EXIT_FAILED, EXIT_OK
from orbitd.testing import FakeKube

APP = base.APP


def secret(name: str, namespace: str, **meta) -> dict:
    obj = tpa._obj("v1", "Secret", name, namespace, type="Opaque")
    obj["metadata"].update(meta)
    return obj


class TestNamespaceCascade:
    """Deleting a Namespace deletes everything inside it. The verb cannot pretend otherwise,
    and it cannot refuse on everything either: Kubernetes puts a `default` ServiceAccount and
    a `kube-root-ca.crt` ConfigMap in EVERY Namespace, so a rule of "refuse if this Namespace
    holds anything orbit did not apply" refuses every Namespace on a real cluster — which is
    exactly the case the verb exists for.

    So: the cluster's own furniture is never a reason to do anything; an object another
    inventory owns still refuses (that rule lives below); and anything else the Namespace takes
    is NAMED in the confirmation and RECORDED as deleted, so the stored inventory never claims
    an object survived that the cascade destroyed."""

    def furnish(self, kube, namespace):
        """What Kubernetes itself puts in every Namespace."""
        tpa._add_live(kube, tpa._obj("v1", "ServiceAccount", "default", namespace),
                      uid=f"uid-sa-{namespace}")
        tpa._add_live(kube, tpa._obj("v1", "ConfigMap", "kube-root-ca.crt", namespace,
                                     data={"ca.crt": "x"}), uid=f"uid-ca-{namespace}")

    def test_the_clusters_own_furniture_never_blocks_the_run(self, tmp_path, capsys, monkeypatch):
        base.rendered_instance(tmp_path)
        kube = FakeKube()
        base.seed(kube, APP, [base.ns("old-ns"), base.cm("a", "old-ns")])
        self.furnish(kube, "old-ns")
        code = base.run(monkeypatch, kube, tmp_path, "--app", APP, stdin="retire old\n")
        assert code == EXIT_OK, ("a Namespace holding only Kubernetes' own default objects "
                                 "could not be retired:\n" + capsys.readouterr().err)
        assert ("", "old-ns") not in base.names(kube, "Namespace")

    def test_the_furniture_is_not_reported_as_collateral(self, tmp_path, capsys, monkeypatch):
        base.rendered_instance(tmp_path)
        kube = FakeKube()
        base.seed(kube, APP, [base.ns("old-ns"), base.cm("a", "old-ns")])
        self.furnish(kube, "old-ns")
        base.run(monkeypatch, kube, tmp_path, "--app", APP, "--dry-run")
        out = capsys.readouterr().out
        assert "kube-root-ca.crt" not in out, "the cluster's own furniture is noise, not news"

    def test_a_stranger_the_namespace_takes_is_named_before_the_delete(self, tmp_path, capsys, monkeypatch):
        base.rendered_instance(tmp_path)
        kube = FakeKube()
        base.seed(kube, APP, [base.ns("old-ns"), base.cm("a", "old-ns")])
        self.furnish(kube, "old-ns")
        tpa._add_live(kube, base.cm("stranger", "old-ns"), uid="uid-stranger")
        base.run(monkeypatch, kube, tmp_path, "--app", APP, "--dry-run")
        out = capsys.readouterr().out
        assert "stranger" in out, ("an object orbit never applied is destroyed with the "
                                   "Namespace and the operator is never shown it")

    def test_a_stranger_is_recorded_as_deleted_not_as_withheld(self, tmp_path, capsys, monkeypatch):
        base.rendered_instance(tmp_path)
        kube = FakeKube()
        base.seed(kube, APP, [base.ns("old-ns"), base.cm("a", "old-ns")])
        tpa._add_live(kube, secret("tls", "old-ns", annotations={"cert-manager.io/issuer": "x"}),
                      uid="uid-tls")
        assert base.run(monkeypatch, kube, tmp_path, "--app", APP, stdin="retire old\n") == EXIT_OK
        got = base.rows(capsys.readouterr().out)
        assert "tls" in got, "the generated Secret the Namespace destroyed appears in no row"
        assert got["tls"]["result"] == "deleted", (
            "the report says a Secret survived that the Namespace delete destroyed: "
            + json.dumps(got["tls"]))


class TestRenderFailure:
    """A render that could not be read is not an absent app."""

    def test_an_unreadable_application_file_refuses(self, tmp_path, capsys, monkeypatch):
        tpa._instance(tmp_path, APP, [base.cm("keep", "proj-a")], create_ns=False)
        app_file = next((tmp_path / "clusters" / "platform" / "apps").glob("*.yaml"))
        app_file.chmod(0o000)
        kube = FakeKube()
        base.seed(kube, APP, [base.cm("a", "old-ns")])
        try:
            code = base.run(monkeypatch, kube, tmp_path, "--app", APP, stdin="retire old\n")
        finally:
            app_file.chmod(0o644)
        assert code == EXIT_FAILED, "an unreadable render was read as 'no longer rendered'"
        assert kube.delete_calls == []

    def test_a_missing_apps_directory_refuses(self, tmp_path, capsys, monkeypatch):
        kube = FakeKube()
        base.seed(kube, APP, [base.cm("a", "old-ns")])
        code = base.run(monkeypatch, kube, tmp_path, "--app", APP, stdin="retire old\n")
        assert code == EXIT_FAILED, ("an instance directory with no rendered apps was read as "
                                     "'this app is not rendered'")
        assert kube.delete_calls == []


class TestSyncGate:
    """A live automated sync puts everything back."""

    def test_an_automated_sync_stops_the_run(self, tmp_path, capsys, monkeypatch):
        base.rendered_instance(tmp_path)
        kube = FakeKube()
        base.seed(kube, APP, [base.cm("a", "old-ns")])
        tpa._add_live(kube, tpa._obj("argoproj.io/v1alpha1", "Application", APP, "argocd",
                                     spec={"syncPolicy": {"automated": {}}}), uid="uid-app")
        code = base.run(monkeypatch, kube, tmp_path, "--app", APP, stdin="retire old\n")
        assert code == EXIT_FAILED, "the deploy tool will re-create everything this run deleted"
        assert kube.delete_calls == []
        assert base.inventory_left(kube), "the inventory was destroyed by a run that changed nothing"


class TestSharedNamespace:
    """A tenant's objects keep a Namespace alive too."""

    def test_a_namespace_a_tenant_inventory_uses_is_withheld(self, tmp_path, capsys, monkeypatch):
        base.rendered_instance(tmp_path)
        kube = FakeKube()
        base.seed(kube, APP, [base.ns("proj-dev"), base.cm("a", "proj-dev")])
        kube.add_object({
            "apiVersion": "v1", "kind": "ConfigMap",
            "metadata": {"name": "orbit-inventory-proj-dev", "namespace": "orbit",
                         "uid": "inv-tenant", "resourceVersion": "1"},
            "data": {"objects": ObjectRef.of(base.cm("tenant", "proj-dev")).line,
                     "generated": "", "applied_at": "2026-01-01T00:00:00Z"},
        })
        tpa._add_live(kube, base.cm("tenant", "proj-dev"), uid="uid-tenant")
        base.run(monkeypatch, kube, tmp_path, "--app", APP, stdin="retire old\n")
        assert ("", "proj-dev") in base.names(kube, "Namespace"), (
            "a Namespace a tenant still occupies was deleted, taking the tenant's objects")


class TestPartialFailure:
    """A report that omits what was never attempted reads as a clean run."""

    def test_every_object_appears_in_the_report(self, tmp_path, capsys, monkeypatch):
        base.rendered_instance(tmp_path)
        kube = FakeKube()
        objs = [base.cm("a", "old-ns"), base.cm("b", "old-ns"),
                base.cm("c", "old-ns"), base.ns("old-ns")]
        base.seed(kube, APP, objs)
        calls = {"n": 0}
        from orbitd import testing as _t
        real_delete = _t.FakeDynamicResource.delete

        def flaky(self, *args, **kwargs):
            calls["n"] += 1
            if calls["n"] == 2:
                raise RuntimeError("500 from the apiserver")
            return real_delete(self, *args, **kwargs)

        monkeypatch.setattr(_t.FakeDynamicResource, "delete", flaky)
        code = base.run(monkeypatch, kube, tmp_path, "--app", APP, stdin="retire old\n")
        assert code == EXIT_FAILED
        got = base.rows(capsys.readouterr().out)
        assert set(got) == {"a", "b", "c", "old-ns"}, (
            "objects the run never reached appear in no row: " + json.dumps(sorted(got)))
        untouched = [r for n, r in got.items() if r["result"] not in ("deleted", "failed")]
        assert untouched, "nothing says which objects were never attempted"
