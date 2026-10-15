"""orbit platform release-owners: drop a retired field manager's ownership from live
platform objects, once, before orbit applies them.

ArgoCD applied every platform object before orbit took over, and its `argocd-controller`
Apply entries stay in managedFields after ArgoCD is gone. Server-side apply removes a field
only when no other manager owns it, so a field orbit stops setting (the Loki rules volume
changing from configMap to projected) survives under the retired owner and the object
becomes invalid. Releasing those entries makes orbit the owner of what it applies."""

from __future__ import annotations

from copy import deepcopy

from kubernetes.client.exceptions import ApiException

import test_platform_apply as tpa
from orbitd.platform import main
from orbitd.platform_apply import EXIT_FAILED, EXIT_OK, RETIRED_MANAGERS
from orbitd.testing import FakeDynamicResource, FakeKube

ARGO = {"manager": "argocd-controller", "operation": "Apply", "apiVersion": "v1",
        "fieldsType": "FieldsV1", "fieldsV1": {"f:data": {"f:k": {}}}}
ORBIT = {"manager": "orbit", "operation": "Apply", "apiVersion": "v1",
         "fieldsType": "FieldsV1", "fieldsV1": {"f:data": {"f:k": {}}}}
K3S = {"manager": "k3s", "operation": "Update", "apiVersion": "v1",
       "fieldsType": "FieldsV1", "fieldsV1": {"f:metadata": {}}}


def world(tmp_path, managed_by_name: dict[str, list[dict]], *, absent=()) -> FakeKube:
    names = [*managed_by_name, *absent]
    objs = [tpa._obj("v1", "ConfigMap", n, "proj-a", data={"k": "v"}) for n in names]
    tpa._instance(tmp_path, "chart-a", objs, create_ns=False)
    kube = FakeKube()
    for name, managed in managed_by_name.items():
        live = tpa._obj("v1", "ConfigMap", name, "proj-a", data={"k": "old"})
        live["metadata"]["managedFields"] = deepcopy(managed)
        tpa._add_live(kube, live, rv="7")
    return kube


def run(monkeypatch, kube, *args) -> int:
    monkeypatch.setattr("orbitd.platform_apply.Kube", lambda kubeconfig=None: kube)
    return main(["release-owners", *args])


def patches(kube) -> list[dict]:
    return [m for m in kube.mutations if m["verb"] == "patch"]


def managers(entries) -> list[str]:
    return [e.get("manager") for e in entries]


def live(kube, name):
    return tpa._live(kube, "ConfigMap", "proj-a", name)


def test_only_argocd_is_retired():
    assert RETIRED_MANAGERS == ("argocd-controller",)


def test_retired_entries_are_dropped_and_every_other_owner_kept(tmp_path, capsys, monkeypatch):
    kube = world(tmp_path, {"both": [ARGO, ORBIT, K3S], "clean": [ORBIT, K3S]})
    assert run(monkeypatch, kube, str(tmp_path), "--app", "chart-a") == EXIT_OK
    (p,) = patches(kube)
    assert p["name"] == "both" and p["namespace"] == "proj-a"
    assert p["content_type"] == "application/merge-patch+json"
    # guarded: a concurrent writer makes this a conflict, not a lost update
    assert p["body"]["metadata"]["resourceVersion"] == "7"
    assert managers(p["body"]["metadata"]["managedFields"]) == ["orbit", "k3s"]
    assert managers(live(kube, "both")["metadata"]["managedFields"]) == ["orbit", "k3s"]
    assert managers(live(kube, "clean")["metadata"]["managedFields"]) == ["orbit", "k3s"]
    assert live(kube, "both")["data"] == {"k": "old"}, "release changes ownership, not content"
    out = capsys.readouterr().out
    assert "ConfigMap proj-a/both: released argocd-controller" in out
    assert "clean" not in out


def test_an_object_only_the_retired_manager_owned_is_reset(tmp_path, capsys, monkeypatch):
    """An empty managedFields list in a patch is ignored by the API server; [{}] is the
    documented way to clear it."""
    kube = world(tmp_path, {"argo-only": [ARGO]})
    assert run(monkeypatch, kube, str(tmp_path), "--app", "chart-a") == EXIT_OK
    (p,) = patches(kube)
    assert p["body"]["metadata"]["managedFields"] == [{}]


def test_dry_run_names_what_it_would_release_and_patches_nothing(tmp_path, capsys, monkeypatch):
    kube = world(tmp_path, {"both": [ARGO, ORBIT]})
    assert run(monkeypatch, kube, str(tmp_path), "--app", "chart-a", "--dry-run") == EXIT_OK
    assert patches(kube) == []
    assert managers(live(kube, "both")["metadata"]["managedFields"]) == ["argocd-controller", "orbit"]
    assert "ConfigMap proj-a/both: would release argocd-controller" in capsys.readouterr().out


def test_objects_not_in_the_cluster_are_skipped(tmp_path, capsys, monkeypatch):
    kube = world(tmp_path, {"both": [ARGO, ORBIT]}, absent=("not-yet",))
    assert run(monkeypatch, kube, str(tmp_path), "--app", "chart-a") == EXIT_OK
    assert [p["name"] for p in patches(kube)] == ["both"]


def test_a_conflict_is_reported_and_the_rest_still_released(tmp_path, capsys, monkeypatch):
    kube = world(tmp_path, {"a": [ARGO, ORBIT], "b": [ARGO, ORBIT]})
    real = FakeDynamicResource.patch

    def patch(self, name=None, body=None, namespace=None, content_type=None, **kw):
        if name == "a":
            raise ApiException(status=409, reason="Conflict: the object has been modified")
        return real(self, name=name, body=body, namespace=namespace,
                    content_type=content_type, **kw)

    monkeypatch.setattr(FakeDynamicResource, "patch", patch)
    assert run(monkeypatch, kube, str(tmp_path), "--app", "chart-a") == EXIT_FAILED
    assert managers(live(kube, "b")["metadata"]["managedFields"]) == ["orbit"]
    captured = capsys.readouterr()
    assert "proj-a/a" in captured.out + captured.err


def test_a_failure_reports_status_and_reason_never_the_response_body(tmp_path, capsys, monkeypatch):
    """An admission webhook's rejection can quote the object's own fields back; the
    command names the object, the status and the reason, and nothing from the body."""
    kube = world(tmp_path, {"a": [ARGO, ORBIT]})

    def patch(self, name=None, body=None, namespace=None, content_type=None, **kw):
        e = ApiException(status=422, reason="Unprocessable Entity")
        e.body = ('{"kind":"Status","message":"admission webhook denied: '
                  'data.customer=private-customer-value is not allowed"}')
        raise e

    monkeypatch.setattr(FakeDynamicResource, "patch", patch)
    assert run(monkeypatch, kube, str(tmp_path), "--app", "chart-a") == EXIT_FAILED
    captured = capsys.readouterr()
    text = captured.out + captured.err
    assert "private-customer-value" not in text
    assert "proj-a/a" in text and "422" in text
