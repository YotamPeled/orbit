"""orbit secrets adopt: release each verified Secret from its ExternalSecret, no gap."""

from __future__ import annotations

import json
import logging
from pathlib import Path
from types import SimpleNamespace

import pytest
from kubernetes.client.exceptions import ApiException

from orbitd.secrets import (
    EXIT_DIFF, EXIT_OK, EXIT_USAGE, MANAGED_ANNOTATION, adopt_secrets, cmd_adopt, export_secrets,
)
from orbitd.testing import FakeKube
from test_secrets import (
    PLANT_ANNOT, PLANT_CHANGED_B64, PLANT_DOCKER_B64, PLANT_TOKEN_B64, RECIPIENT, FakeSops,
    _age_key, _b64, _joined_calls, assert_no_plants,
)

NS = "proj-dev"
ES_API = "external-secrets.io/v1"


def _es(name: str, uid: str, target: str | None = None) -> dict:
    spec: dict = {"target": {"name": target, "creationPolicy": "Owner"}} if target else {}
    return {
        "apiVersion": ES_API, "kind": "ExternalSecret",
        "metadata": {"name": name, "namespace": NS, "uid": uid, "resourceVersion": "3"},
        "spec": spec,
    }


def _owned_secret(name: str, uid: str, owner: str, owner_uid: str, data: dict,
                  secret_type: str = "Opaque") -> dict:
    return {
        "apiVersion": "v1", "kind": "Secret",
        "metadata": {
            "name": name, "namespace": NS, "uid": uid, "resourceVersion": "9",
            "annotations": {"kubectl.kubernetes.io/last-applied-configuration": PLANT_ANNOT},
            "ownerReferences": [{
                "apiVersion": ES_API, "kind": "ExternalSecret", "name": owner,
                "uid": owner_uid, "controller": True, "blockOwnerDeletion": True,
            }],
        },
        "type": secret_type,
        "data": dict(data),
    }


def _world(tmp_path: Path, monkeypatch) -> tuple[Path, FakeKube, FakeSops]:
    instance = tmp_path / "instance"
    instance.mkdir()
    pub = tmp_path / "recipient.pub"
    pub.write_text(RECIPIENT + "\n")
    kube = FakeKube()
    kube.add_object(_es("app-config", "es-uid-app"))
    kube.add_object(_owned_secret("app-config", "sec-uid-app", "app-config", "es-uid-app",
                                  {"TOKEN": PLANT_TOKEN_B64, "OTHER": _b64("other")}))
    # An ExternalSecret whose name differs from the Secret it writes.
    kube.add_object(_es("pull-es", "es-uid-pull", target="ghcr-pull"))
    kube.add_object(_owned_secret("ghcr-pull", "sec-uid-pull", "pull-es", "es-uid-pull",
                                  {".dockerconfigjson": PLANT_DOCKER_B64},
                                  secret_type="kubernetes.io/dockerconfigjson"))
    fake = FakeSops()
    assert export_secrets(instance, kube, recipient_file=str(pub), sops_run=fake.run) == EXIT_OK
    _age_key(tmp_path, monkeypatch)
    return instance, kube, fake


def _obj(kube: FakeKube, kind: str, name: str) -> dict | None:
    for o in kube.objects:
        if o.get("kind") == kind and o["metadata"]["name"] == name:
            return o
    return None


def _adopt(instance: Path, kube: FakeKube, fake: FakeSops, **kw) -> int:
    kw.setdefault("sleep", lambda _s: None)
    return adopt_secrets(instance, kube, sops_run=fake.run, **kw)


def _es_refs(secret: dict) -> list:
    return [r for r in secret["metadata"].get("ownerReferences") or []
            if r.get("kind") == "ExternalSecret"]


def test_adopt_yes_releases_every_secret_keeping_uid_and_data(tmp_path, monkeypatch, capsys, caplog):
    caplog.set_level(logging.DEBUG)
    instance, kube, fake = _world(tmp_path, monkeypatch)
    capsys.readouterr()
    data_before = {n: json.dumps(_obj(kube, "Secret", n)["data"], sort_keys=True)
                   for n in ("app-config", "ghcr-pull")}
    rc = _adopt(instance, kube, fake, yes=True)
    out, err = capsys.readouterr()
    assert rc == EXIT_OK, err
    assert f"{NS}/app-config released" in out
    assert f"{NS}/ghcr-pull released" in out
    assert _obj(kube, "ExternalSecret", "app-config") is None
    assert _obj(kube, "ExternalSecret", "pull-es") is None
    for name, uid in (("app-config", "sec-uid-app"), ("ghcr-pull", "sec-uid-pull")):
        secret = _obj(kube, "Secret", name)
        assert secret is not None, name
        assert secret["metadata"]["uid"] == uid
        assert _es_refs(secret) == []
        assert secret["metadata"]["annotations"][MANAGED_ANNOTATION] == "true"
        assert json.dumps(secret["data"], sort_keys=True) == data_before[name]
    # Every delete names an ExternalSecret, carries Orphan and the uid precondition in
    # the body, and passes no DeleteOptions as query parameters.
    assert {c["name"] for c in kube.delete_calls} == {"app-config", "pull-es"}
    for call in kube.delete_calls:
        assert call["kind"] == "ExternalSecret"
        assert call["body"]["propagationPolicy"] == "Orphan"
        pre = call["body"]["preconditions"]
        assert pre["uid"] == {"app-config": "es-uid-app", "pull-es": "es-uid-pull"}[call["name"]]
        assert pre["resourceVersion"]
        assert call["dry_run"] is None
        assert "propagation_policy" not in call
    patches = [m for m in kube.mutations if m["verb"] == "patch"]
    assert {p["name"] for p in patches} == {"app-config", "ghcr-pull"}
    for p in patches:
        assert p["kind"] == "Secret"
        assert p["content_type"] == "application/merge-patch+json"
        assert set(p["body"]) == {"metadata"}
    assert {m["verb"] for m in kube.mutations} == {"delete", "patch"}
    assert_no_plants(out, err, caplog.text, _joined_calls(fake))


def test_default_propagation_delete_loses_the_secret_and_adopt_fails(tmp_path, monkeypatch, capsys):
    """Old-style code: a delete without Orphan lets the garbage collector take the Secret."""
    instance, kube, fake = _world(tmp_path, monkeypatch)
    capsys.readouterr()
    monkeypatch.setattr("orbitd.secrets._delete_options", lambda uid, resource_version: {"preconditions": {"uid": uid, "resourceVersion": resource_version}})
    rc = _adopt(instance, kube, fake, yes=True, namespace=NS, name="app-config")
    out, err = capsys.readouterr()
    assert rc == EXIT_DIFF
    assert _obj(kube, "Secret", "app-config") is None
    assert "released" not in out
    assert f"{NS}/app-config is gone" in err


def test_mismatch_stops_before_any_mutation(tmp_path, monkeypatch, capsys, caplog):
    caplog.set_level(logging.DEBUG)
    instance, kube, fake = _world(tmp_path, monkeypatch)
    capsys.readouterr()
    _obj(kube, "Secret", "ghcr-pull")["data"][".dockerconfigjson"] = PLANT_CHANGED_B64
    rc = _adopt(instance, kube, fake, yes=True)
    out, err = capsys.readouterr()
    assert rc == EXIT_DIFF
    assert kube.mutations == []
    assert kube.delete_calls == []
    # The matching Secret sorts first and is still untouched.
    assert _obj(kube, "ExternalSecret", "app-config") is not None
    assert MANAGED_ANNOTATION not in _obj(kube, "Secret", "app-config")["metadata"]["annotations"]
    assert "nothing changed" in err
    assert "released" not in out
    assert_no_plants(out, err, caplog.text)


def test_without_yes_nothing_is_mutated(tmp_path, monkeypatch, capsys, caplog):
    caplog.set_level(logging.DEBUG)
    instance, kube, fake = _world(tmp_path, monkeypatch)
    capsys.readouterr()
    rc = _adopt(instance, kube, fake)
    out, err = capsys.readouterr()
    assert rc == EXIT_OK, err
    assert kube.mutations == []
    assert kube.delete_calls == []
    assert _obj(kube, "ExternalSecret", "app-config") is not None
    assert _obj(kube, "ExternalSecret", "pull-es") is not None
    assert (f"{NS}/ghcr-pull would release: delete ExternalSecret {NS}/pull-es (Orphan), "
            f"annotate {MANAGED_ANNOTATION}=true") in out
    assert "pass --yes" in out
    assert_no_plants(out, err, caplog.text)


def test_second_run_changes_nothing_and_says_already_released(tmp_path, monkeypatch, capsys, caplog):
    caplog.set_level(logging.DEBUG)
    instance, kube, fake = _world(tmp_path, monkeypatch)
    assert _adopt(instance, kube, fake, yes=True) == EXIT_OK
    capsys.readouterr()
    kube.mutations.clear()
    kube.delete_calls.clear()
    rc = _adopt(instance, kube, fake, yes=True)
    out, err = capsys.readouterr()
    assert rc == EXIT_OK, err
    assert kube.mutations == []
    assert f"{NS}/app-config already released" in out
    assert f"{NS}/ghcr-pull already released" in out
    assert [ln for ln in out.splitlines() if ln.endswith("released")] == [
        f"{NS}/app-config already released", f"{NS}/ghcr-pull already released"]
    assert_no_plants(out, err, caplog.text)


def test_named_secret_only(tmp_path, monkeypatch, capsys):
    instance, kube, fake = _world(tmp_path, monkeypatch)
    capsys.readouterr()
    rc = _adopt(instance, kube, fake, yes=True, namespace=NS, name="ghcr-pull")
    out, _ = capsys.readouterr()
    assert rc == EXIT_OK
    assert f"{NS}/ghcr-pull released" in out
    assert "app-config" not in out
    assert _obj(kube, "ExternalSecret", "app-config") is not None
    assert _obj(kube, "ExternalSecret", "pull-es") is None


def test_ownerreference_that_never_clears_stops_without_annotating(tmp_path, monkeypatch, capsys):
    instance, kube, fake = _world(tmp_path, monkeypatch)
    capsys.readouterr()
    secret = _obj(kube, "Secret", "app-config")
    secret["metadata"]["ownerReferences"].append(
        {"apiVersion": ES_API, "kind": "ExternalSecret", "name": "stale", "uid": "es-uid-stale"})
    sleeps: list[float] = []
    rc = _adopt(instance, kube, fake, yes=True, namespace=NS, name="app-config", sleep=sleeps.append)
    _, err = capsys.readouterr()
    assert rc == EXIT_DIFF
    assert sleeps and all(s > 0 for s in sleeps)
    assert "still has an ExternalSecret ownerReference" in err
    assert MANAGED_ANNOTATION not in secret["metadata"]["annotations"]
    assert _obj(kube, "Secret", "app-config")["metadata"]["uid"] == "sec-uid-app"


def test_secret_replaced_after_verify_stops_without_annotating(tmp_path, monkeypatch, capsys):
    """Between the verify read and the post-delete re-read the Secret is recreated."""
    import orbitd.secrets as secrets_mod
    instance, kube, fake = _world(tmp_path, monkeypatch)
    capsys.readouterr()
    real_read = secrets_mod.read_secret
    reads: list[str] = []

    def read_then_replace(k, namespace, name):
        reads.append(name)
        if len(reads) == 2:
            secret = _obj(kube, "Secret", name)
            secret["metadata"]["uid"] = "sec-uid-new"
            secret["metadata"].pop("ownerReferences", None)
        return real_read(k, namespace, name)

    monkeypatch.setattr(secrets_mod, "read_secret", read_then_replace)
    rc = _adopt(instance, kube, fake, yes=True, namespace=NS, name="app-config")
    _, err = capsys.readouterr()
    assert rc == EXIT_DIFF
    assert "was replaced" in err
    assert [m["verb"] for m in kube.mutations] == ["delete"]
    assert MANAGED_ANNOTATION not in _obj(kube, "Secret", "app-config")["metadata"]["annotations"]


def test_owner_not_found_by_target_stops(tmp_path, monkeypatch, capsys):
    instance, kube, fake = _world(tmp_path, monkeypatch)
    capsys.readouterr()
    kube.objects.remove(_obj(kube, "ExternalSecret", "pull-es"))
    rc = _adopt(instance, kube, fake, yes=True)
    _, err = capsys.readouterr()
    assert rc == EXIT_DIFF
    assert kube.mutations == []
    assert "ghcr-pull is owned by an ExternalSecret" in err


def test_usage_errors(tmp_path, monkeypatch, capsys):
    instance, kube, fake = _world(tmp_path, monkeypatch)
    assert _adopt(instance, kube, fake, namespace=NS) == EXIT_USAGE
    assert _adopt(instance, kube, fake, namespace=NS, name="absent") == EXIT_USAGE
    monkeypatch.delenv("SOPS_AGE_KEY_FILE")
    assert _adopt(instance, kube, fake, yes=True) == EXIT_USAGE
    assert kube.mutations == []


def test_cmd_adopt_via_args(tmp_path, monkeypatch, capsys):
    instance, kube, fake = _world(tmp_path, monkeypatch)
    capsys.readouterr()
    args = SimpleNamespace(instance_dir=str(instance), kube=kube, kubeconfig=None,
                           namespace=None, name=None, yes=True, sops_run=fake.run,
                           sleep=lambda _s: None)
    assert cmd_adopt(args) == EXIT_OK
    assert "released" in capsys.readouterr().out


@pytest.mark.anyio
async def test_orbit_cli_dispatches_secrets_adopt(tmp_path, monkeypatch, capsys):
    from orbitd.client import async_main
    instance, kube, fake = _world(tmp_path, monkeypatch)
    capsys.readouterr()
    monkeypatch.setattr("orbitd.secrets.Kube", lambda kubeconfig=None: kube)
    monkeypatch.setattr("orbitd.secrets.run_sops", fake.run)
    assert await async_main(["secrets", "adopt", "--help"]) == 0
    rc = await async_main(["secrets", "adopt", str(instance), "--kubeconfig", "/dev/null"])
    assert rc == EXIT_OK
    assert kube.mutations == []
    rc = await async_main(["secrets", "adopt", str(instance), "--kubeconfig", "/dev/null",
                           "--namespace", NS, "--name", "app-config", "--yes"])
    out, err = capsys.readouterr()
    assert rc == EXIT_OK, err
    assert f"{NS}/app-config released" in out
    assert_no_plants(out, err)


# ------------------------------------------------------------------ the fake's garbage collector


def _gc_world() -> FakeKube:
    kube = FakeKube()
    kube.add_object(_es("owner", "es-uid"))
    kube.add_object(_owned_secret("dep", "dep-uid", "owner", "es-uid", {"K": _b64("v")}))
    return kube


def test_fake_delete_default_propagation_collects_dependents():
    kube = _gc_world()
    res = kube.dynamic.resources.get(api_version=ES_API, kind="ExternalSecret")
    res.delete(name="owner", namespace=NS, body={"preconditions": {"uid": "es-uid"}})
    assert _obj(kube, "Secret", "dep") is None


def test_fake_delete_orphan_in_body_strips_references():
    kube = _gc_world()
    res = kube.dynamic.resources.get(api_version=ES_API, kind="ExternalSecret")
    res.delete(name="owner", namespace=NS, body={"propagationPolicy": "Orphan"})
    dep = _obj(kube, "Secret", "dep")
    assert dep is not None and "ownerReferences" not in dep["metadata"]


def test_fake_delete_ignores_query_orphan_beside_a_body():
    kube = _gc_world()
    res = kube.dynamic.resources.get(api_version=ES_API, kind="ExternalSecret")
    res.delete(name="owner", namespace=NS, body={"preconditions": {"uid": "es-uid"}},
               propagation_policy="Orphan")
    assert _obj(kube, "Secret", "dep") is None


def test_fake_patch_refuses_like_the_api_server():
    kube = _gc_world()
    res = kube.dynamic.resources.get(api_version="v1", kind="Secret")
    body = {"metadata": {"annotations": {"a": "b"}}}
    with pytest.raises(ApiException) as e:
        res.patch(name="dep", namespace=NS, body=body, content_type="application/json-patch+json")
    assert e.value.status == 400
    with pytest.raises(ApiException) as e:
        res.patch(name="dep", namespace=NS, content_type="application/merge-patch+json",
                  body={"metadata": {"resourceVersion": "1", "annotations": {"a": "b"}}})
    assert e.value.status == 409
    with pytest.raises(ApiException) as e:
        res.patch(name="nope", namespace=NS, body=body, content_type="application/merge-patch+json")
    assert e.value.status == 404


@pytest.mark.parametrize("policy", ["Background", "Foreground"])
def test_fake_delete_keeps_a_dependent_that_has_another_live_owner(policy):
    """Review r21b (Astra): Kubernetes keeps a dependent while any other owner lives, and
    drops only the reference to the deleted owner."""
    kube = FakeKube()
    kube.add_object(_es("owner-a", "uid-a"))
    kube.add_object(_es("owner-b", "uid-b"))
    dep = _owned_secret("dep", "dep-uid", "owner-a", "uid-a", {"K": _b64("v")})
    dep["metadata"]["ownerReferences"].append(
        {"apiVersion": ES_API, "kind": "ExternalSecret", "name": "owner-b", "uid": "uid-b"})
    kube.add_object(dep)
    res = kube.dynamic.resources.get(api_version=ES_API, kind="ExternalSecret")
    res.delete(name="owner-a", namespace=NS, body={"propagationPolicy": policy})
    kept = _obj(kube, "Secret", "dep")
    assert kept is not None
    assert [r["uid"] for r in kept["metadata"]["ownerReferences"]] == ["uid-b"]
    assert _obj(kube, "ExternalSecret", "owner-b") is not None


# ------------------------------------------------------------------ review r21b (opus): adopt safety

def _eso_cleanup(monkeypatch):
    """Simulate ESO's cleanup finalizer: when an ExternalSecret is deleted, its
    spec.target.deletionPolicy AT THAT MOMENT decides what happens to the Secret it
    wrote: Delete removes the Secret, Merge removes the keys ESO populated, Retain
    (the default when absent) keeps it. The propagation policy plays no part."""
    from orbitd import testing
    orig = testing.FakeDynamicResource.delete

    def delete(self, name=None, namespace=None, body=None, **kw):
        es = None
        if self.kind == "ExternalSecret":
            for o in self._store.objects:
                m = o.get("metadata") or {}
                if o.get("kind") == "ExternalSecret" and m.get("name") == name and m.get("namespace") == namespace:
                    es = o
        res = orig(self, name=name, namespace=namespace, body=body, **kw)
        if es is not None:
            target = (es.get("spec") or {}).get("target") or {}
            policy = target.get("deletionPolicy") or "Retain"
            tname = target.get("name") or es["metadata"]["name"]
            for o in list(self._store.objects):
                m = o.get("metadata") or {}
                if o.get("kind") == "Secret" and m.get("name") == tname and m.get("namespace") == namespace:
                    if policy == "Delete":
                        self._store.objects.remove(o)
                    elif policy == "Merge":
                        o["data"] = {}
        return res

    monkeypatch.setattr(testing.FakeDynamicResource, "delete", delete)


@pytest.mark.parametrize("policy", ["Delete", "Merge"])
def test_adopt_sets_retain_before_deleting_an_externalsecret(tmp_path, monkeypatch, capsys, policy):
    instance, kube, fake = _world(tmp_path, monkeypatch)
    capsys.readouterr()
    es = _obj(kube, "ExternalSecret", "app-config")
    es["spec"] = {"target": {"name": "app-config", "creationPolicy": "Owner", "deletionPolicy": policy}}
    data_before = json.dumps(_obj(kube, "Secret", "app-config")["data"], sort_keys=True)
    _eso_cleanup(monkeypatch)
    rc = _adopt(instance, kube, fake, yes=True, namespace=NS, name="app-config")
    out, err = capsys.readouterr()
    assert rc == EXIT_OK, err
    secret = _obj(kube, "Secret", "app-config")
    assert secret is not None and secret["metadata"]["uid"] == "sec-uid-app"
    assert json.dumps(secret["data"], sort_keys=True) == data_before
    assert _obj(kube, "ExternalSecret", "app-config") is None
    # The ExternalSecret is switched to Retain by a patch that comes before its delete.
    order = [(m["verb"], m.get("kind"), m.get("name")) for m in kube.mutations]
    patch_es = ("patch", "ExternalSecret", "app-config")
    delete_es = ("delete", "ExternalSecret", "app-config")
    assert patch_es in order and delete_es in order
    assert order.index(patch_es) < order.index(delete_es)
    es_patch = next(m for m in kube.mutations if m["verb"] == "patch" and m.get("kind") == "ExternalSecret")
    assert es_patch["body"]["spec"]["target"]["deletionPolicy"] == "Retain"


def test_adopt_leaves_a_retain_externalsecret_unpatched(tmp_path, monkeypatch, capsys):
    instance, kube, fake = _world(tmp_path, monkeypatch)
    capsys.readouterr()
    _obj(kube, "ExternalSecret", "app-config")["spec"] = {
        "target": {"name": "app-config", "creationPolicy": "Owner", "deletionPolicy": "Retain"}}
    rc = _adopt(instance, kube, fake, yes=True)
    assert rc == EXIT_OK
    assert not [m for m in kube.mutations if m["verb"] == "patch" and m.get("kind") == "ExternalSecret"]


def test_adopt_refuses_a_foreign_externalsecret_owner_before_any_mutation(tmp_path, monkeypatch, capsys):
    instance, kube, fake = _world(tmp_path, monkeypatch)
    capsys.readouterr()
    kube.add_object(_es("other", "es-uid-other", target="unrelated"))
    _obj(kube, "Secret", "app-config")["metadata"]["ownerReferences"].append(
        {"apiVersion": ES_API, "kind": "ExternalSecret", "name": "other", "uid": "es-uid-other"})
    rc = _adopt(instance, kube, fake, yes=True, namespace=NS, name="app-config")
    out, err = capsys.readouterr()
    assert rc == EXIT_DIFF
    assert kube.mutations == [] and kube.delete_calls == []
    assert _obj(kube, "ExternalSecret", "app-config") is not None
    assert "released" not in out


def test_adopt_refuses_an_externalsecret_owned_by_a_clusterexternalsecret(tmp_path, monkeypatch, capsys):
    """A ClusterExternalSecret would recreate the ExternalSecret, and ESO would re-own the Secret."""
    instance, kube, fake = _world(tmp_path, monkeypatch)
    capsys.readouterr()
    _obj(kube, "ExternalSecret", "app-config")["metadata"]["ownerReferences"] = [
        {"apiVersion": ES_API, "kind": "ClusterExternalSecret", "name": "fleet", "uid": "ces-uid"}]
    rc = _adopt(instance, kube, fake, yes=True, namespace=NS, name="app-config")
    out, err = capsys.readouterr()
    assert rc == EXIT_DIFF
    assert kube.mutations == [] and kube.delete_calls == []
    assert "ClusterExternalSecret" in err


def test_adopt_error_never_carries_api_error_text(tmp_path, monkeypatch, capsys, caplog):
    caplog.set_level(logging.DEBUG)
    instance, kube, fake = _world(tmp_path, monkeypatch)
    capsys.readouterr()
    from orbitd import testing

    def boom(self, *a, **kw):
        e = ApiException(status=422, reason="Unprocessable Entity")
        e.body = json.dumps({"message": "object echoed", "data": {"TOKEN": PLANT_TOKEN_B64}})
        raise e

    monkeypatch.setattr(testing.FakeDynamicResource, "delete", boom)
    rc = _adopt(instance, kube, fake, yes=True, namespace=NS, name="app-config")
    out, err = capsys.readouterr()
    assert rc == EXIT_DIFF
    assert "422" in err
    assert_no_plants(out, err, caplog.text)
    assert "object echoed" not in err


def test_annotation_patch_carries_the_reread_resource_version(tmp_path, monkeypatch, capsys):
    instance, kube, fake = _world(tmp_path, monkeypatch)
    capsys.readouterr()
    rc = _adopt(instance, kube, fake, yes=True, namespace=NS, name="app-config")
    assert rc == EXIT_OK
    patch = next(m for m in kube.mutations if m["verb"] == "patch" and m.get("kind") == "Secret")
    rv = patch["body"]["metadata"].get("resourceVersion")
    assert rv and rv != "9", "the guard is the version re-read after the orphaning, not the one verified"


def test_adopt_does_not_patch_an_already_managed_secret(tmp_path, monkeypatch, capsys):
    instance, kube, fake = _world(tmp_path, monkeypatch)
    capsys.readouterr()
    _obj(kube, "Secret", "app-config")["metadata"]["annotations"][MANAGED_ANNOTATION] = "true"
    rc = _adopt(instance, kube, fake, yes=True, namespace=NS, name="app-config")
    assert rc == EXIT_OK
    assert not [m for m in kube.mutations if m["verb"] == "patch" and m.get("kind") == "Secret"]


# ------------------------------------------------------------------ review r21b-2 (astra): one guarded write path

def test_adopt_refuses_an_externalsecret_replaced_after_planning(tmp_path, monkeypatch, capsys):
    """The ExternalSecret is replaced (new uid) between planning and release: nothing may touch the replacement."""
    instance, kube, fake = _world(tmp_path, monkeypatch)
    capsys.readouterr()
    _obj(kube, "ExternalSecret", "app-config")["spec"] = {
        "target": {"name": "app-config", "creationPolicy": "Owner", "deletionPolicy": "Delete"}}
    import orbitd.secrets as sec
    orig = sec._owning_external_secrets

    def owning_then_replace(kube_, namespace, name):
        found = orig(kube_, namespace, name)
        es = _obj(kube, "ExternalSecret", "app-config")
        if es is not None and name == "app-config":
            es["metadata"]["uid"] = "es-uid-replacement"
            es["metadata"]["resourceVersion"] = "77"
        return found

    monkeypatch.setattr(sec, "_owning_external_secrets", owning_then_replace)
    rc = _adopt(instance, kube, fake, yes=True, namespace=NS, name="app-config")
    capsys.readouterr()
    assert rc == EXIT_DIFF
    replacement = _obj(kube, "ExternalSecret", "app-config")
    assert replacement is not None
    assert replacement["spec"]["target"]["deletionPolicy"] == "Delete", "the replacement was patched"
    assert not [m for m in kube.mutations if m.get("kind") == "ExternalSecret"
                and m["verb"] == "patch" and m.get("result", "ok") == "ok"
                and (m.get("body") or {}).get("metadata", {}).get("uid") != "es-uid-app"]


def test_every_adopt_write_carries_uid_and_resource_version(tmp_path, monkeypatch, capsys):
    """One write path: every patch adopt sends names the uid and resourceVersion it read;
    every delete carries the uid precondition."""
    instance, kube, fake = _world(tmp_path, monkeypatch)
    capsys.readouterr()
    _obj(kube, "ExternalSecret", "app-config")["spec"] = {
        "target": {"name": "app-config", "creationPolicy": "Owner", "deletionPolicy": "Delete"}}
    rc = _adopt(instance, kube, fake, yes=True)
    assert rc == EXIT_OK
    writes = [m for m in kube.mutations if m["verb"] in ("patch", "delete")]
    assert writes
    for m in writes:
        if m["verb"] == "patch":
            meta = m["body"].get("metadata") or {}
            assert meta.get("uid"), m
            assert meta.get("resourceVersion"), m
    for call in kube.delete_calls:
        assert call["body"]["preconditions"].get("uid"), call


def _after_retain(monkeypatch, then):
    """Run `then()` right after adopt's Retain step: the window before the ExternalSecret
    delete, where ESO or another writer can still act."""
    import orbitd.secrets as sec
    orig = sec._ensure_retain

    def wrapped(*a, **kw):
        out = orig(*a, **kw)
        then()
        return out

    monkeypatch.setattr(sec, "_ensure_retain", wrapped)


def test_externalsecret_delete_refuses_a_policy_flipped_after_retain(tmp_path, monkeypatch, capsys):
    """Retain was checked, then someone set Delete: the delete must not land on that version."""
    instance, kube, fake = _world(tmp_path, monkeypatch)
    capsys.readouterr()

    def flip():
        es = _obj(kube, "ExternalSecret", "app-config")
        es.setdefault("spec", {}).setdefault("target", {})["deletionPolicy"] = "Delete"
        es["metadata"]["resourceVersion"] = str(int(es["metadata"]["resourceVersion"]) + 1)

    _after_retain(monkeypatch, flip)
    rc = _adopt(instance, kube, fake, yes=True, namespace=NS, name="app-config")
    capsys.readouterr()
    assert rc == EXIT_DIFF
    assert _obj(kube, "ExternalSecret", "app-config") is not None, "the flipped version was deleted"
    secret = _obj(kube, "Secret", "app-config")
    assert secret is not None and secret["data"]
    assert MANAGED_ANNOTATION not in secret["metadata"].get("annotations", {})


def test_every_externalsecret_delete_carries_uid_and_resource_version(tmp_path, monkeypatch, capsys):
    instance, kube, fake = _world(tmp_path, monkeypatch)
    capsys.readouterr()
    _obj(kube, "ExternalSecret", "app-config")["spec"] = {
        "target": {"name": "app-config", "creationPolicy": "Owner", "deletionPolicy": "Merge"}}
    assert _adopt(instance, kube, fake, yes=True) == EXIT_OK
    assert kube.delete_calls
    for call in kube.delete_calls:
        pre = call["body"]["preconditions"]
        assert pre.get("uid") and pre.get("resourceVersion"), call


def test_a_value_rotated_after_verify_is_not_handed_to_the_operator(tmp_path, monkeypatch, capsys):
    """ESO rewrites the Secret after verify: the encrypted file is stale, so the Secret must
    not be marked sops-managed (the operator would overwrite the new value with the old)."""
    instance, kube, fake = _world(tmp_path, monkeypatch)
    capsys.readouterr()

    def rotate():
        secret = _obj(kube, "Secret", "app-config")
        secret["data"]["TOKEN"] = PLANT_CHANGED_B64
        secret["metadata"]["resourceVersion"] = str(int(secret["metadata"]["resourceVersion"]) + 1)

    _after_retain(monkeypatch, rotate)
    rc = _adopt(instance, kube, fake, yes=True, namespace=NS, name="app-config")
    out, err = capsys.readouterr()
    assert rc == EXIT_DIFF
    secret = _obj(kube, "Secret", "app-config")
    assert secret["data"]["TOKEN"] == PLANT_CHANGED_B64, "the rotated value was touched"
    assert MANAGED_ANNOTATION not in secret["metadata"].get("annotations", {})
    assert "changed" in err and "export" in err
    assert_no_plants(out + err)


def test_the_plan_keeps_hashes_never_values(tmp_path, monkeypatch, capsys):
    import orbitd.secrets as sec
    instance, kube, fake = _world(tmp_path, monkeypatch)
    capsys.readouterr()
    plans = []
    orig = sec._plan_adopt

    def keep(*a, **kw):
        plan = orig(*a, **kw)
        plans.append(plan)
        return plan

    monkeypatch.setattr(sec, "_plan_adopt", keep)
    assert _adopt(instance, kube, fake) == EXIT_OK
    assert plans
    for plan in plans:
        assert_no_plants(repr(vars(plan)))
        assert "other" not in repr(vars(plan)) and _b64("other") not in repr(vars(plan))
