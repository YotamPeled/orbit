"""In-memory backends for tests. The three viewer-tool jobs use only `fake_backends`."""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml
from kubernetes.client.exceptions import ApiException

from .backends import CNPG_GROUP, CNPG_PLURAL, CNPG_VERSION, InstanceRepo
from .shadow import _CLUSTER_SCOPED

PROMETHEUS_EMPTY = {"status": "success", "data": {"resultType": "vector", "result": []}}
LOKI_EMPTY = {"status": "success", "data": {"resultType": "streams", "result": []}}
TEMPO_EMPTY = {"traces": []}
ALERTMANAGER_EMPTY: list[Any] = []


# ------------------------------------------------------------------ attribute objects (kubernetes models)

class Attr:
    """Nested attribute access over a dict, so fakes look like kubernetes models."""

    def __init__(self, data: dict[str, Any]) -> None:
        self._data = data
        for key, value in data.items():
            setattr(self, key, _wrap(value))

    def to_dict(self) -> dict[str, Any]:
        return self._data


class ResourceList:
    def __init__(self, items: list[Any]) -> None:
        self.items = items


def _wrap(value: Any) -> Any:
    if isinstance(value, dict):
        return Attr(value)
    if isinstance(value, list):
        return [_wrap(v) for v in value]
    return value


def _matches_labels(labels: dict[str, str] | None, selector: str | None) -> bool:
    if not selector:
        return True
    labels = labels or {}
    for part in selector.split(","):
        key, _, want = part.strip().partition("=")
        if not key or labels.get(key.strip()) != want.strip():
            return False
    return True


def _not_found(kind: str, namespace: str, name: str) -> ApiException:
    return ApiException(status=404, reason=f"{kind} {namespace}/{name} not found")


# ------------------------------------------------------------------ fake kube

class FakeCoreV1:
    def __init__(self, store: FakeKube) -> None:
        self._store = store

    def list_namespaced_pod(self, namespace: str, **kwargs: Any) -> ResourceList:
        selector = kwargs.get("label_selector")
        items = [p for p in self._store.pods
                 if p["metadata"]["namespace"] == namespace
                 and _matches_labels(p["metadata"].get("labels"), selector)]
        return ResourceList([Attr(p) for p in items])

    def read_namespaced_pod(self, name: str, namespace: str, **kwargs: Any) -> Attr:
        for p in self._store.pods:
            if p["metadata"]["name"] == name and p["metadata"]["namespace"] == namespace:
                return Attr(p)
        raise _not_found("pod", namespace, name)

    def list_namespaced_event(self, namespace: str, **kwargs: Any) -> ResourceList:
        items = [e for e in self._store.events if e["metadata"]["namespace"] == namespace]
        field = kwargs.get("field_selector")
        if field:
            # involvedObject.name=web-0
            want = {}
            for part in field.split(","):
                k, _, v = part.strip().partition("=")
                want[k.strip()] = v.strip()
            if "involvedObject.name" in want:
                items = [e for e in items
                         if (e.get("involvedObject") or {}).get("name") == want["involvedObject.name"]]
        return ResourceList([Attr(e) for e in items])

    def list_node(self, **kwargs: Any) -> ResourceList:
        return ResourceList([Attr(n) for n in self._store.nodes])

    def list_namespace(self, **kwargs: Any) -> ResourceList:
        return ResourceList([Attr(n) for n in self._store.namespaces])

    def list_namespaced_resource_quota(self, namespace: str, **kwargs: Any) -> ResourceList:
        items = [q for q in self._store.quotas if q["metadata"]["namespace"] == namespace]
        return ResourceList([Attr(q) for q in items])

    def read_namespaced_pod_log(self, name: str, namespace: str, **kwargs: Any) -> str:
        """A pod's log. `pod_logs[(namespace, name)]` is every current container's;
        `pod_logs[(namespace, name, container, previous)]` is one run's, and the only
        way a previous run has lines."""
        self._store.pod_log_calls.append({"namespace": namespace, "name": name, **kwargs})
        run = (namespace, name, kwargs.get("container"), bool(kwargs.get("previous")))
        if run in self._store.pod_logs:
            return self._store.pod_logs[run]
        key = (namespace, name)
        if key in self._store.pod_logs and not kwargs.get("previous"):
            return self._store.pod_logs[key]
        raise _not_found("pod", namespace, name)


def _deep_merge(dst: dict[str, Any], src: dict[str, Any]) -> dict[str, Any]:
    for key, value in src.items():
        if isinstance(value, dict) and isinstance(dst.get(key), dict):
            _deep_merge(dst[key], value)
        else:
            dst[key] = value
    return dst


class FakeAppsV1:
    def __init__(self, store: FakeKube) -> None:
        self._store = store

    def list_namespaced_deployment(self, namespace: str, **kwargs: Any) -> ResourceList:
        items = [d for d in self._store.deployments if d["metadata"]["namespace"] == namespace]
        return ResourceList([Attr(d) for d in items])

    def read_namespaced_deployment(self, name: str, namespace: str, **kwargs: Any) -> Attr:
        for d in self._store.deployments:
            if d["metadata"]["name"] == name and d["metadata"]["namespace"] == namespace:
                return Attr(d)
        raise _not_found("deployment", namespace, name)

    def patch_namespaced_deployment(self, name: str, namespace: str, body: Any, **kwargs: Any) -> Attr:
        payload = body if isinstance(body, dict) else getattr(body, "to_dict", lambda: body)()
        if not isinstance(payload, dict):
            payload = {}
        self._store.patches.append({"name": name, "namespace": namespace, "body": payload})
        for d in self._store.deployments:
            if d["metadata"]["name"] == name and d["metadata"]["namespace"] == namespace:
                _deep_merge(d, payload)
                meta = d.setdefault("metadata", {})
                gen = int(meta.get("generation") or 0) + 1
                meta["generation"] = gen
                d.setdefault("status", {})["observedGeneration"] = gen
                return Attr(d)
        raise _not_found("deployment", namespace, name)


class FakeDynamicResource:
    """One GVK. `get` with a name returns the object; without, a list. Honours `_request_timeout`."""

    def __init__(self, store: FakeKube, api_version: str, kind: str) -> None:
        self._store = store
        self.api_version = api_version
        self.kind = kind

    def get(self, name: str | None = None, namespace: str | None = None,
            label_selector: str | None = None, _request_timeout: Any = None,
            **kwargs: Any) -> Any:
        self._store.dynamic_calls.append({
            "api_version": self.api_version, "kind": self.kind, "name": name,
            "namespace": namespace, "label_selector": label_selector,
            "_request_timeout": _request_timeout, **kwargs,
        })
        items = []
        for obj in self._store.objects:
            if obj.get("kind") != self.kind:
                continue
            if self.api_version and obj.get("apiVersion") not in (None, self.api_version):
                continue
            meta = obj.get("metadata") or {}
            if namespace is not None and (meta.get("namespace") or "") != namespace:
                continue
            if name is not None and meta.get("name") != name:
                continue
            if not _matches_labels(meta.get("labels"), label_selector):
                continue
            items.append(obj)
        if name is not None:
            if not items:
                raise _not_found(self.kind, namespace or "", name)
            return items[0]
        return {"apiVersion": self.api_version, "kind": f"{self.kind}List", "items": items}

    def _find(self, namespace: str, name: str) -> dict[str, Any] | None:
        for obj in self._store.objects:
            meta = obj.get("metadata") or {}
            if (obj.get("kind") == self.kind and meta.get("name") == name
                    and (meta.get("namespace") or "") == namespace):
                return obj
        return None

    def _record_mutate(self, verb: str, **kwargs: Any) -> None:
        self._store.mutations.append({
            "verb": verb, "api_version": self.api_version, "kind": self.kind, **kwargs,
        })

    def create(self, body: Any = None, namespace: str | None = None,
               dry_run: str | None = None, field_manager: str | None = None,
               _request_timeout: Any = None, **kwargs: Any) -> Any:
        """POST as the API server answers it: a namespaced kind needs a namespace, an
        existing object is 409 AlreadyExists (dry run or not), `dry_run` stores nothing,
        and a fieldManager is recorded as an Update entry in managedFields."""
        self._record_mutate("create", body=body, namespace=namespace, dry_run=dry_run,
                            field_manager=field_manager, **kwargs)
        if not isinstance(body, dict):
            raise ValueError("body must be a dict")
        meta = body.get("metadata") or {}
        name = meta.get("name")
        if not name:
            raise ApiException(status=422, reason="metadata.name: Required value")
        if self.kind in _CLUSTER_SCOPED:
            namespace = ""
        else:
            namespace = namespace or meta.get("namespace")
            if not namespace:
                raise ValueError(f"Namespace is required for {self.api_version}.{self.kind}")
        if self._find(namespace, name) is not None:
            raise ApiException(status=409, reason=f"AlreadyExists: {self.kind} {namespace}/{name}")
        created = json.loads(json.dumps(body))
        created_meta = created.setdefault("metadata", {})
        created_meta["uid"] = "fake-uid"
        created_meta["resourceVersion"] = "1"
        if field_manager:
            created_meta["managedFields"] = [{"manager": field_manager, "operation": "Update",
                                              "apiVersion": self.api_version}]
        if not dry_run:
            self._store.objects.append(created)
        return created

    def replace(self, name: str | None = None, body: Any = None,
                namespace: str | None = None, **kwargs: Any) -> Any:
        self._record_mutate("replace", name=name, body=body, namespace=namespace, **kwargs)
        return body

    def patch(self, name: str | None = None, body: Any = None,
              namespace: str | None = None, content_type: str | None = None,
              **kwargs: Any) -> Any:
        """PATCH as the API server answers it. The client's default content type is a
        strategic merge patch; a JSON patch needs a list body. A merge patch merges
        dicts, deletes a key set to null, and a metadata.resourceVersion that does not
        match the stored object is a 409 Conflict."""
        self._record_mutate("patch", name=name, body=body, namespace=namespace,
                            content_type=content_type, **kwargs)
        kind_of_patch = content_type or "application/strategic-merge-patch+json"
        if kind_of_patch == "application/json-patch+json":
            if not isinstance(body, list):
                raise ApiException(status=400, reason="json patch body must be a list")
            return self._json_patch(name, namespace, body, kwargs.get("dry_run"))
        if kind_of_patch not in ("application/merge-patch+json",
                                 "application/strategic-merge-patch+json"):
            raise ApiException(status=415, reason=f"unsupported media type {kind_of_patch}")
        if not isinstance(body, dict):
            raise ApiException(status=400, reason="merge patch body must be an object")
        if not name:
            raise ValueError(f"name is required to patch {self.api_version}.{self.kind}")
        namespace = "" if self.kind in _CLUSTER_SCOPED else (namespace or "")
        if self.kind not in _CLUSTER_SCOPED and not namespace:
            raise ValueError(f"namespace is required to patch {self.api_version}.{self.kind}")
        existing = self._find(namespace, name)
        if existing is None:
            raise _not_found(self.kind, namespace, name)
        meta = existing.setdefault("metadata", {})
        want_rv = (body.get("metadata") or {}).get("resourceVersion")
        if want_rv is not None and str(want_rv) != str(meta.get("resourceVersion") or ""):
            raise ApiException(status=409, reason="Conflict: the object has been modified")
        patch = json.loads(json.dumps(body))
        patch.get("metadata", {}).pop("resourceVersion", None)
        _merge_patch(existing, patch)
        meta["resourceVersion"] = str(int(meta.get("resourceVersion") or 0) + 1)
        return existing

    def _json_patch(self, name: str | None, namespace: str | None, ops: list[Any],
                    dry_run: Any) -> Any:
        """RFC 6902 `test` and `remove`, atomically: a failed test is 422 and changes
        nothing, a path that does not exist is 422, `dry_run` stores nothing."""
        namespace = "" if self.kind in _CLUSTER_SCOPED else (namespace or "")
        existing = self._find(namespace, name or "")
        if existing is None:
            raise _not_found(self.kind, namespace, name or "")
        work = json.loads(json.dumps(existing))

        def walk(doc: Any, pointer: str) -> tuple[Any, str | int]:
            parts = [p.replace("~1", "/").replace("~0", "~") for p in pointer.split("/")[1:]]
            cur = doc
            for p in parts[:-1]:
                cur = cur[int(p)] if isinstance(cur, list) else cur[p]
            last = parts[-1]
            return cur, (int(last) if isinstance(cur, list) else last)

        for op in ops:
            try:
                parent, key = walk(work, op["path"])
                if op["op"] == "test":
                    if parent[key] != op.get("value"):
                        raise ApiException(status=422, reason=f"test failed at {op['path']}")
                elif op["op"] == "remove":
                    del parent[key]
                else:
                    raise ApiException(status=422, reason=f"the fake applies test and remove, not {op['op']}")
            except (KeyError, IndexError, ValueError, TypeError):
                raise ApiException(status=422, reason=f"path {op.get('path')} does not exist")
        if dry_run:
            return work
        meta = work.setdefault("metadata", {})
        meta["resourceVersion"] = str(int(meta.get("resourceVersion") or 0) + 1)
        existing.clear()
        existing.update(work)
        return existing

    def delete_collection(self, namespace: str | None = None, **kwargs: Any) -> Any:
        self._record_mutate("delete_collection", namespace=namespace, **kwargs)
        return None

    def apply(self, body: Any = None, namespace: str | None = None, **kwargs: Any) -> Any:
        self._record_mutate("apply", body=body, namespace=namespace, **kwargs)
        return body

    def server_side_apply(self, body: Any = None, name: str | None = None,
                          namespace: str | None = None, force_conflicts: Any = None,
                          field_manager: str | None = None, dry_run: str | None = None,
                          _request_timeout: Any = None, **kwargs: Any) -> Any:
        """The real client's contract: a dict body, a name, a namespace for a namespaced
        kind; the API server's: a fieldManager. `dry_run` never touches the store."""
        if not isinstance(body, dict):
            raise ValueError("body must be a dict")
        meta = body.get("metadata") or {}
        name = name or meta.get("name")
        if not name:
            raise ValueError(f"name is required to patch {self.api_version}.{self.kind}")
        if self.kind in _CLUSTER_SCOPED:
            namespace = ""
        else:
            namespace = namespace or meta.get("namespace")
            if not namespace:
                raise ValueError(f"namespace is required to patch {self.api_version}.{self.kind}")
        if not field_manager:
            raise ValueError("fieldManager is required for server-side apply")
        call = {
            "api_version": self.api_version, "kind": self.kind, "name": name,
            "namespace": namespace, "field_manager": field_manager,
            "force_conflicts": force_conflicts, "dry_run": dry_run,
            "_request_timeout": _request_timeout, **kwargs,
        }
        self._store.apply_calls.append(call)
        self._record_mutate("server_side_apply", body=body, **{
            k: v for k, v in call.items() if k not in ("api_version", "kind")
        })
        err = self._store.apply_errors.get((self.kind, namespace, name))
        if err is not None:
            raise err
        existing = self._find(namespace, name)
        merged = json.loads(json.dumps(body))
        merged_meta = merged.setdefault("metadata", {})
        managed: list[Any] = []
        if existing is not None:
            old_meta = existing.get("metadata") or {}
            merged_meta["uid"] = old_meta.get("uid") or "fake-uid"
            merged_meta["resourceVersion"] = str(int(old_meta.get("resourceVersion") or 0) + 1)
            managed = [e for e in old_meta.get("managedFields") or []
                       if not (isinstance(e, dict) and e.get("manager") == field_manager
                               and e.get("operation") == "Apply")]
        else:
            merged_meta["uid"] = "fake-uid"
            merged_meta["resourceVersion"] = "1"
        merged_meta["managedFields"] = managed + [
            {"manager": field_manager, "operation": "Apply", "apiVersion": self.api_version}]
        if dry_run:
            return merged
        if existing is not None:
            self._store.objects.remove(existing)
        if self.kind == "CustomResourceDefinition" and getattr(self._store, "stamp_crd_established", True):
            status = merged.setdefault("status", {})
            conds = status.setdefault("conditions", [])
            if isinstance(conds, list) and not any(
                    isinstance(c, dict) and c.get("type") == "Established" for c in conds):
                conds.append({"type": "Established", "status": "True"})
        self._store.objects.append(merged)
        return merged

    def delete(self, name: str | None = None, namespace: str | None = None, body: Any = None,
               dry_run: str | None = None, _request_timeout: Any = None, **kwargs: Any) -> Any:
        if not name:
            raise ValueError("At least one of name|label_selector|field_selector is required")
        if self.kind not in _CLUSTER_SCOPED and not namespace:
            raise ValueError("At least one of namespace|label_selector|field_selector is required")
        namespace = namespace or ""
        self._store.delete_calls.append({
            "api_version": self.api_version, "kind": self.kind, "name": name,
            "namespace": namespace, "dry_run": dry_run, "_request_timeout": _request_timeout,
            "body": body, **kwargs,
        })
        self._record_mutate(
            "delete", name=name, namespace=namespace, body=body, dry_run=dry_run,
            _request_timeout=_request_timeout, **kwargs,
        )
        err = self._store.delete_errors.get((self.kind, namespace, name))
        if err is not None:
            raise err
        existing = self._find(namespace, name)
        if existing is None:
            raise _not_found(self.kind, namespace, name)
        # DeleteOptions.preconditions, as the API server checks them: a uid or
        # resourceVersion that does not match the stored object is a 409 Conflict,
        # dry run or not.
        pre = (body or {}).get("preconditions") if isinstance(body, dict) else None
        if pre:
            meta = existing.get("metadata") or {}
            for key in ("uid", "resourceVersion"):
                want = pre.get(key)
                if want is not None and str(want) != str(meta.get(key) or ""):
                    raise ApiException(
                        status=409, reason=f"Conflict: the {key} in the precondition "
                        f"({want}) does not match the {key} in the record ({meta.get(key)})")
        # When a body is present the API server reads DeleteOptions from it and
        # ignores query parameters, including dryRun. Only the body's dryRun
        # field then decides; a dry_run keyword beside a body is dropped.
        if body is not None:
            listed = body.get("dryRun") if isinstance(body, dict) else None
            dry = isinstance(listed, (list, tuple)) and "All" in listed
        else:
            dry = bool(dry_run)
        if not dry:
            self._store.objects.remove(existing)
        if not any(o is existing for o in self._store.objects):
            self._store.collect_dependents(existing, _propagation(body, kwargs))
        return {"kind": "Status", "status": "Success"}


def _merge_patch(dst: dict[str, Any], patch: dict[str, Any]) -> None:
    """RFC 7386: dicts merge, null deletes, anything else replaces."""
    for key, value in patch.items():
        if value is None:
            dst.pop(key, None)
        elif isinstance(value, dict) and isinstance(dst.get(key), dict):
            _merge_patch(dst[key], value)
        else:
            dst[key] = value


def _propagation(body: Any, kwargs: dict[str, Any]) -> str:
    """The propagation policy the API server uses: from the DeleteOptions body when one
    is present (query parameters beside a body are ignored), else the query parameter,
    else the default, Background."""
    if body is not None:
        policy = body.get("propagationPolicy") if isinstance(body, dict) else None
    else:
        policy = kwargs.get("propagation_policy")
    return str(policy or "Background")


class FakeDynamicResources:
    def __init__(self, store: FakeKube) -> None:
        self._store = store

    def get(self, api_version: str | None = None, kind: str | None = None, **kwargs: Any) -> FakeDynamicResource:
        if not kind:
            raise ValueError("kind is required")
        return FakeDynamicResource(self._store, api_version or "", kind)


class FakeDynamic:
    def __init__(self, store: FakeKube) -> None:
        self.resources = FakeDynamicResources(store)


class FakeCustomObjects:
    def __init__(self, store: FakeKube) -> None:
        self._store = store

    def list_namespaced_custom_object(self, group: str, version: str, namespace: str,
                                      plural: str, **kwargs: Any) -> dict[str, Any]:
        if (group, version, plural) == (CNPG_GROUP, CNPG_VERSION, CNPG_PLURAL):
            items = [c for c in self._store.clusters if c["metadata"]["namespace"] == namespace]
            return {"apiVersion": f"{group}/{version}", "kind": "ClusterList", "items": items}
        if (group, version, plural) == ("cert-manager.io", "v1", "certificates"):
            items = [c for c in self._store.certificates if c["metadata"]["namespace"] == namespace]
            return {"apiVersion": f"{group}/{version}", "kind": "CertificateList", "items": items}
        if (group, version, plural) == ("barmancloud.cnpg.io", "v1", "objectstores"):
            items = [o for o in self._store.objectstores if o["metadata"]["namespace"] == namespace]
            return {"apiVersion": f"{group}/{version}", "kind": "ObjectStoreList", "items": items}
        if (group, version, plural) == ("metrics.k8s.io", "v1beta1", "pods"):
            if self._store.pod_metrics is None:
                raise _not_found("pods.metrics.k8s.io", namespace, "")
            items = [m for m in self._store.pod_metrics if m["metadata"]["namespace"] == namespace]
            return {"apiVersion": f"{group}/{version}", "kind": "PodMetricsList", "items": items}
        return {"items": []}

    def list_custom_object_for_all_namespaces(self, group: str, version: str, plural: str,
                                              **kwargs: Any) -> dict[str, Any]:
        if (group, version, plural) == (CNPG_GROUP, CNPG_VERSION, CNPG_PLURAL):
            return {"apiVersion": f"{group}/{version}", "kind": "ClusterList", "items": list(self._store.clusters)}
        if (group, version, plural) == ("cert-manager.io", "v1", "certificates"):
            return {"apiVersion": f"{group}/{version}", "kind": "CertificateList",
                    "items": list(self._store.certificates)}
        if (group, version, plural) == ("metrics.k8s.io", "v1beta1", "pods"):
            if self._store.pod_metrics is None:
                raise _not_found("pods.metrics.k8s.io", "", "")
            return {"apiVersion": f"{group}/{version}", "kind": "PodMetricsList",
                    "items": list(self._store.pod_metrics)}
        return {"items": []}

    def get_namespaced_custom_object(self, group: str, version: str, namespace: str,
                                     plural: str, name: str, **kwargs: Any) -> dict[str, Any]:
        if (group, version, plural) == (CNPG_GROUP, CNPG_VERSION, CNPG_PLURAL):
            for c in self._store.clusters:
                if c["metadata"]["name"] == name and c["metadata"]["namespace"] == namespace:
                    return c
        raise _not_found(plural, namespace, name)


class FakeKube:
    """In-memory pods, deployments, CNPG clusters, events, nodes, quotas."""

    def __init__(self) -> None:
        self.pods: list[dict[str, Any]] = []
        self.deployments: list[dict[str, Any]] = []
        self.clusters: list[dict[str, Any]] = []
        self.events: list[dict[str, Any]] = []
        self.nodes: list[dict[str, Any]] = []
        self.namespaces: list[dict[str, Any]] = []
        self.quotas: list[dict[str, Any]] = []
        self.certificates: list[dict[str, Any]] = []
        self.objectstores: list[dict[str, Any]] = []
        self.pod_metrics: list[dict[str, Any]] | None = None
        self.pod_logs: dict[tuple[Any, ...], str] = {}
        self.pod_log_calls: list[dict[str, Any]] = []
        self.patches: list[dict[str, Any]] = []
        self.objects: list[dict[str, Any]] = []
        self.dynamic_calls: list[dict[str, Any]] = []
        self.mutations: list[dict[str, Any]] = []
        self.apply_calls: list[dict[str, Any]] = []
        self.delete_calls: list[dict[str, Any]] = []
        self.apply_errors: dict[tuple[str, str, str], Exception] = {}
        self.delete_errors: dict[tuple[str, str, str], Exception] = {}
        self.stamp_crd_established: bool = True
        self.core = FakeCoreV1(self)
        self.apps = FakeAppsV1(self)
        self.custom = FakeCustomObjects(self)
        self.dynamic = FakeDynamic(self)

    def collect_dependents(self, owner: dict[str, Any], policy: str) -> None:
        """The garbage collector after `owner` is deleted: Orphan strips the
        ownerReferences that point at its uid; Background and Foreground delete the
        dependents, and theirs in turn, except a dependent that still has another
        owner reference whose owner exists is kept with only the deleted owner's
        reference removed."""
        uid = (owner.get("metadata") or {}).get("uid")
        if not uid:
            return
        for obj in list(self.objects):
            meta = obj.get("metadata") or {}
            refs = meta.get("ownerReferences") or []
            if not isinstance(refs, list):
                continue
            if not any(isinstance(r, dict) and r.get("uid") == uid for r in refs):
                continue
            if policy == "Orphan":
                kept = [r for r in refs if not (isinstance(r, dict) and r.get("uid") == uid)]
                if kept:
                    meta["ownerReferences"] = kept
                else:
                    meta.pop("ownerReferences", None)
                meta["resourceVersion"] = str(int(meta.get("resourceVersion") or 0) + 1)
            elif policy in ("Background", "Foreground"):
                live_uids = {
                    (o.get("metadata") or {}).get("uid")
                    for o in self.objects
                    if isinstance(o.get("metadata"), dict)
                }
                if any(isinstance(r, dict) and r.get("uid") != uid and r.get("uid") in live_uids
                       for r in refs):
                    kept = [r for r in refs
                            if not (isinstance(r, dict) and r.get("uid") == uid)]
                    if kept:
                        meta["ownerReferences"] = kept
                    else:
                        meta.pop("ownerReferences", None)
                    meta["resourceVersion"] = str(int(meta.get("resourceVersion") or 0) + 1)
                elif any(o is obj for o in self.objects):
                    self.objects.remove(obj)
                    self.collect_dependents(obj, policy)
            elif any(o is obj for o in self.objects):
                self.objects.remove(obj)
                self.collect_dependents(obj, policy)

    def add_object(self, obj: dict[str, Any]) -> dict[str, Any]:
        self.objects.append(obj)
        return obj

    def add_pod(self, *, namespace: str, name: str, phase: str = "Running",
                restarts: int = 0, image: str = "mirror.invalid/app:tag",
                labels: dict[str, str] | None = None,
                last_terminated: str | None = None,
                component: str | None = None) -> dict[str, Any]:
        component = component or name
        labels = dict(labels or {"app.kubernetes.io/name": component})
        state: dict[str, Any]
        last_state: dict[str, Any] = {}
        if phase == "Running":
            state = {"running": {"startedAt": "2026-01-01T00:00:00Z"}}
        else:
            state = {"waiting": {"reason": phase}}
        if last_terminated:
            last_state = {"terminated": {"reason": last_terminated, "exitCode": 137}}
        pod = {
            "metadata": {"name": name, "namespace": namespace, "labels": labels,
                         "creationTimestamp": "2026-01-01T00:00:00Z"},
            "spec": {"containers": [{"name": component, "image": image,
                                     "resources": {"limits": {"memory": "256Mi"}}}]},
            "status": {
                "phase": phase,
                "containerStatuses": [{
                    "name": component, "ready": phase == "Running", "restartCount": restarts,
                    "state": state, "lastState": last_state,
                }],
            },
        }
        self.pods.append(pod)
        return pod

    def add_deployment(self, *, namespace: str, name: str, replicas: int = 1,
                       ready: int | None = None, image: str = "mirror.invalid/app:tag") -> dict[str, Any]:
        ready = replicas if ready is None else ready
        dep = {
            "metadata": {"name": name, "namespace": namespace},
            "spec": {"replicas": replicas,
                     "template": {"spec": {"containers": [{"name": name, "image": image}]}}},
            "status": {"replicas": replicas, "readyReplicas": ready,
                       "updatedReplicas": ready, "availableReplicas": ready},
        }
        self.deployments.append(dep)
        return dep

    def add_cluster(self, *, namespace: str, name: str, instances: int = 1,
                    ready: int | None = None, phase: str = "Cluster in healthy state") -> dict[str, Any]:
        ready = instances if ready is None else ready
        cluster = {
            "apiVersion": f"{CNPG_GROUP}/{CNPG_VERSION}",
            "kind": "Cluster",
            "metadata": {"name": name, "namespace": namespace},
            "status": {"phase": phase, "instances": instances, "readyInstances": ready},
        }
        self.clusters.append(cluster)
        return cluster

    def add_event(self, *, namespace: str, name: str, reason: str, message: str,
                  involved_name: str) -> dict[str, Any]:
        event = {
            "metadata": {"name": name, "namespace": namespace},
            "reason": reason, "message": message,
            "involvedObject": {"name": involved_name, "namespace": namespace},
        }
        self.events.append(event)
        return event


# ------------------------------------------------------------------ fake HTTP

class FakeHttp:
    """Canned JSON responses. Same get/post surface as HttpBackend; never goes on the wire."""

    def __init__(self, env_var: str, default: Any) -> None:
        self.env_var = env_var
        self.base_url = "http://backend.invalid"
        self.default = default
        self.responses: dict[Any, Any] = {}
        self.calls: list[dict[str, Any]] = []

    def require(self) -> FakeHttp:
        return self

    def get(self, path: str, *, params: dict[str, Any] | None = None,
            headers: dict[str, str] | None = None) -> Any:
        return self._dispatch("GET", path, params=params, headers=headers)

    def post(self, path: str, *, json: Any = None, params: dict[str, Any] | None = None,
             headers: dict[str, str] | None = None) -> Any:
        return self._dispatch("POST", path, params=params, json=json, headers=headers)

    def delete(self, path: str, *, params: dict[str, Any] | None = None,
               headers: dict[str, str] | None = None) -> Any:
        return self._dispatch("DELETE", path, params=params, headers=headers)

    def _dispatch(self, method: str, path: str, **kwargs: Any) -> Any:
        self.calls.append({"method": method, "path": path, **kwargs})
        value = self.responses.get((method, path), self.responses.get(path, self.default))
        if isinstance(value, Exception):
            raise value
        if callable(value):
            return value({"method": method, "path": path, **kwargs})
        return value


# ------------------------------------------------------------------ instance dir

def write_instance_dir(root: Path, *, projects: dict[str, Any] | None = None,
                       tags: dict[str, Any] | None = None,
                       monitors: dict[str, dict[str, Any]] | None = None,
                       git: bool = True) -> Path:
    """Write catalog/projects.yaml, catalog/tags.yaml and monitors/*.yaml under `root`."""
    root = Path(root)
    catalog = root / "catalog"
    monitors_dir = root / "monitors"
    catalog.mkdir(parents=True, exist_ok=True)
    monitors_dir.mkdir(parents=True, exist_ok=True)
    (catalog / "projects.yaml").write_text(
        yaml.safe_dump(projects if projects is not None else {"version": 3, "projects": []},
                       sort_keys=False))
    (catalog / "tags.yaml").write_text(
        yaml.safe_dump(tags if tags is not None else {"tags": {}}, sort_keys=False))
    for name, body in (monitors or {}).items():
        (monitors_dir / f"{name}.yaml").write_text(yaml.safe_dump(body, sort_keys=False))
    if git:
        _git(root, ["init", "-b", "main"])
        _git(root, ["add", "catalog", "monitors"])
        _git(root, ["-c", "user.name=dev", "-c", "user.email=dev@example.test",
                    "-c", "commit.gpgsign=false", "commit", "-m", "instance"], check=False)
    return root


def _git(root: Path, args: list[str], *, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True, check=check)


# ------------------------------------------------------------------ deploy fakes (no network, no subprocess)


class FakeRunner:
    """Records argv + extra env. Returns a COMMIT line; never spawns a process."""

    def __init__(self, *, commit: str = "0123456789abcdef0123456789abcdef01234567",
                 returncode: int = 0, stdout: str | None = None,
                 stderr: str = "") -> None:
        self.commit = commit
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr
        self.calls: list[dict[str, Any]] = []

    def run(self, argv: list[str], extra_env: dict[str, str]) -> Any:
        from .deploy import RunResult
        self.calls.append({"argv": list(argv), "env": dict(extra_env)})
        out = self.stdout if self.stdout is not None else f"STEP bump OK\nCOMMIT {self.commit}\n"
        return RunResult(returncode=self.returncode, stdout=out, stderr=self.stderr)


class FakeDigest:
    def __init__(self, digest: str = "sha256:" + "ab" * 32, *, error: str | None = None) -> None:
        self.digest = digest
        self.error = error
        self.calls: list[tuple[str, str]] = []

    def lookup(self, image: str, tag: str) -> str:
        from .deploy import DigestError
        self.calls.append((image, tag))
        if self.error:
            raise DigestError(self.error)
        return self.digest


class FakeLedger:
    """Newest-first tag sequences keyed `project/env/component`. No git."""

    def __init__(self, sequences: dict[str, list[str]] | None = None) -> None:
        self.sequences = dict(sequences or {})

    def current_tag(self, project: str, env: str, component: str) -> str | None:
        seq = self.sequences.get(f"{project}/{env}/{component}") or []
        return seq[0] if seq else None

    def previous_tag(self, project: str, env: str, component: str) -> str | None:
        seq = self.sequences.get(f"{project}/{env}/{component}") or []
        current = None
        for val in seq:
            if current is None:
                current = val
                continue
            if val != current:
                return val
        return None


class FakeClock:
    def __init__(self, t: float = 1_710_000_000.0) -> None:
        self.t = t
        self.sleeps: list[float] = []

    def now(self) -> float:
        return self.t

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.t += seconds


# ------------------------------------------------------------------ bundle

@dataclass
class FakeBackends:
    kube: FakeKube
    prom: FakeHttp
    loki: FakeHttp
    tempo: FakeHttp
    alertmanager: FakeHttp
    instance: InstanceRepo
    instance_root: Path

    @classmethod
    def build(cls, tmp_path: Path, *, projects: dict[str, Any] | None = None,
              tags: dict[str, Any] | None = None,
              monitors: dict[str, dict[str, Any]] | None = None) -> FakeBackends:
        root = write_instance_dir(Path(tmp_path) / "instance",
                                  projects=projects, tags=tags, monitors=monitors)
        return cls(
            kube=FakeKube(),
            prom=FakeHttp("ORBITD_PROMETHEUS_URL", PROMETHEUS_EMPTY),
            loki=FakeHttp("ORBITD_LOKI_URL", LOKI_EMPTY),
            tempo=FakeHttp("ORBITD_TEMPO_URL", TEMPO_EMPTY),
            alertmanager=FakeHttp("ORBITD_ALERTMANAGER_URL", ALERTMANAGER_EMPTY),
            instance=InstanceRepo(str(root)),
            instance_root=root,
        )


__all__ = [
    "Attr", "ResourceList", "FakeKube", "FakeHttp", "FakeBackends",
    "write_instance_dir", "PROMETHEUS_EMPTY", "LOKI_EMPTY", "TEMPO_EMPTY", "ALERTMANAGER_EMPTY",
    "FakeRunner", "FakeDigest", "FakeLedger", "FakeClock",
]
