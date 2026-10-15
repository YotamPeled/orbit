"""Area 3b of the whole-app sweep (shadow diff and the appliers): one test per repair.

Each test fails with its repair reverted: a Secret's values never reach a diff; catalog
ids are validated before they build a report path; an API error's body stays out of a
reason; a retire plan fails closed."""
from __future__ import annotations

import json

import pytest
import test_platform_retire as base
from kubernetes.client.exceptions import ApiException

from orbitd import shadow
from orbitd.applier import _reason
from orbitd.platform_apply import EXIT_FAILED, plan_retire
from orbitd.shadow import _report_dir, catalog_pairs, compare_objects
from orbitd.testing import FakeKube

MARK = "PLANTEDsecretVALUE7q"
MARK_B64 = "UExBTlRFRHNlY3JldFZBTFVFN3E="


def secret(value: str, **extra) -> dict:
    return {"apiVersion": "v1", "kind": "Secret", "type": "Opaque",
            "metadata": {"name": "app-cfg", "namespace": "fixture-ns"},
            "data": {"password": value}, **extra}


def dump(report) -> str:
    return json.dumps(report.model_dump(mode="json"), default=str)


def test_a_secret_value_never_reaches_a_diff():
    report = compare_objects([secret("cmVuZGVyZWQ=")], [secret(MARK_B64)], project="fixture")
    assert [c.paths for c in report.changed] == [["/data/password"]]
    text = dump(report)
    assert MARK_B64 not in text and MARK not in text and "cmVuZGVyZWQ=" not in text
    diff = report.changed[0].diffs[0]
    assert diff.rendered != diff.live and diff.live.startswith("sha256:")


def test_a_secret_whose_type_and_string_data_differ_hides_every_value():
    rendered = secret("cmVuZGVyZWQ=")
    live = secret(MARK_B64, stringData={"other": MARK})
    live["type"] = "kubernetes.io/basic-auth"
    report = compare_objects([rendered], [live], project="fixture")
    text = dump(report)
    assert report.changed and MARK_B64 not in text and MARK not in text


def test_catalog_pairs_skip_ids_that_are_not_catalog_ids(monkeypatch):
    entries = [{"id": "../../../evil", "environments": {"prod": {}}},
               {"id": "good", "environments": {"prod": {}, "a=b,c": {}}},
               {"id": "also/bad", "environments": {"prod": {}}}]
    monkeypatch.setattr(shadow, "project_entries", lambda state: entries)
    monkeypatch.setattr(shadow, "env_names", lambda entry: list(entry["environments"]))
    assert catalog_pairs(state=None) == [("good", "prod")]
    with pytest.raises(ValueError):
        _report_dir(None, "../../../evil", "prod")


def test_an_api_errors_body_stays_out_of_the_reason():
    class Resp:
        status = 403
        reason = "Forbidden"
        data = f'{{"message": "webhook says {MARK}"}}'
        def getheaders(self):
            return {"X-Trace": MARK}
    exc = ApiException(http_resp=Resp())
    text = _reason(exc)
    assert MARK not in text
    assert "HTTP 403" in text and "Forbidden" in text


def test_plan_retire_fails_closed_when_a_live_read_raises(monkeypatch):
    kube = FakeKube()
    objs = [base.cm("a", "old-ns"), base.cm("b", "old-ns"), base.ns("old-ns")]
    base.seed(kube, base.APP, objs)
    from orbitd import testing as _t
    real_get = _t.FakeDynamicResource.get

    def flaky(self, name=None, namespace=None, *args, **kwargs):
        if name == "b":
            raise RuntimeError(f"500 from the apiserver {MARK}")
        return real_get(self, name, namespace, *args, **kwargs)

    monkeypatch.setattr(_t.FakeDynamicResource, "get", flaky)
    from orbitd.applier import ObjectRef
    previous = [ObjectRef(kind=o["kind"], api_version=o["apiVersion"],
                          namespace=o["metadata"].get("namespace", ""), name=o["metadata"]["name"])
                for o in objs]
    deletes, pending, _, _ = plan_retire(kube, base.APP, previous, frozenset())
    assert all(r.name != "b" for r in deletes), "an unreadable object joined the delete set"
    failed = [p for p in pending if p.result == "failed"]
    assert [p.ref.name for p in failed] == ["b"]
    assert failed[0].reason and "RuntimeError" in failed[0].reason


def test_server_managed_fields_are_ignored_for_every_kind():
    rendered = {"apiVersion": "external-secrets.io/v1", "kind": "ExternalSecret",
                "metadata": {"name": "app", "namespace": "fixture-ns"},
                "spec": {"secretStoreRef": {"name": "store", "kind": "ClusterSecretStore"}}}
    live = json.loads(json.dumps(rendered))
    live["metadata"].update({"uid": "u", "resourceVersion": "7", "generation": 1,
                             "managedFields": [{"manager": "external-secrets"}]})
    live["status"] = {"refreshTime": "2026-09-14T06:00:00Z",
                      "conditions": [{"type": "Ready", "status": "True", "reason": "SecretSynced",
                                      "message": f"secret synced {MARK}"}]}
    report = compare_objects([rendered], [live], project="fixture")
    assert report.empty is True, [c.paths for c in report.changed]
    assert MARK not in dump(report)


def test_a_deeply_nested_live_configmap_value_is_compared_as_text():
    depth = 3000
    nested = "".join(f"{'  ' * i}k{i}:\n" for i in range(depth)) + "  " * depth + "v: 1\n"
    rendered = {"apiVersion": "v1", "kind": "ConfigMap", "metadata": {"name": "c", "namespace": "fixture-ns"},
                "data": {"app.yaml": "a: 1\nb: 2\n"}}
    live = json.loads(json.dumps(rendered))
    live["data"]["app.yaml"] = nested
    report = compare_objects([rendered], [live], project="fixture")
    assert report.empty is False


def test_a_project_with_one_bad_env_still_diffs_its_good_envs(monkeypatch):
    from orbitd.tools_viewer import platform_diff as pd
    entry = {"id": "demo", "environments": {"prod": {}, "e2e_test": {}, "a=b,c": {}}}
    monkeypatch.setattr(pd, "find_project", lambda state, project: entry)
    monkeypatch.setattr(pd, "env_names", lambda e: list(e["environments"]))
    assert pd._pairs(None, "demo", None) == [("demo", "prod")]
    with pytest.raises(ValueError):
        shadow.diff(None, "demo", "a=b,c")


def _retire_world(monkeypatch, failing_name):
    kube = FakeKube()
    objs = [base.cm("a", "old-ns"), base.cm("b", "old-ns"), base.ns("old-ns")]
    base.seed(kube, base.APP, objs)
    from orbitd import testing as _t
    real_get = _t.FakeDynamicResource.get

    def flaky(self, name=None, namespace=None, *args, **kwargs):
        if name == failing_name:
            raise RuntimeError("503 from the apiserver")
        return real_get(self, name, namespace, *args, **kwargs)

    monkeypatch.setattr(_t.FakeDynamicResource, "get", flaky)
    return kube


def test_a_plan_time_failure_refuses_the_retire_even_with_its_namespace_doomed(tmp_path, capsys, monkeypatch):
    kube = _retire_world(monkeypatch, "b")
    instance = base.rendered_instance(tmp_path)
    for args in (("--dry-run",), ()):
        code = base.run(monkeypatch, kube, instance, "--app", base.APP, *args, stdin="retire old\n")
        assert code == EXIT_FAILED, args
        got = base.rows(capsys.readouterr().out)
        assert got["b"]["result"] == "failed"
        assert "deleted" not in {r["result"] for r in got.values()}
    assert ("", "old-ns") in base.names(kube, "Namespace")
    assert base.inventory_left(kube) is not None


def test_a_rule_check_that_raises_keeps_the_object_out_of_the_delete_set(monkeypatch):
    kube = FakeKube()
    objs = [base.cm("a", "old-ns"), base.ns("old-ns")]
    base.seed(kube, base.APP, objs)
    from orbitd import platform_apply as pa
    from orbitd.applier import ObjectRef
    monkeypatch.setattr(pa, "platform_retire_rule", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("rule")))
    previous = [ObjectRef(kind=o["kind"], api_version=o["apiVersion"],
                          namespace=o["metadata"].get("namespace", ""), name=o["metadata"]["name"]) for o in objs]
    deletes, pending, _, _ = plan_retire(kube, base.APP, previous, frozenset())
    assert deletes == []
    assert {p.result for p in pending} == {"failed"}


def test_a_transport_failures_cause_survives_the_reason():
    tail = "x" * 300 + " Failed to establish a new connection: [Errno 111] Connection refused"
    assert "Connection refused" in _reason(RuntimeError(tail))
