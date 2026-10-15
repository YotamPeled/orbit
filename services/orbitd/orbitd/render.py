"""Render the desired objects for one project/env pair from the instance checkout.

`render(state, project, env)` returns the union of: every non-Application object in
`clusters/orbit/<project>-<env>.yaml` (or `clusters/apps/` if the pair is still
there) plus `helm template` of `charts/service` with the
Application's valuesObject, release name and destination namespace; every document in
`clusters/apps/namespaces.yaml` whose namespace is the env's destination namespace, or
whose kind is Namespace with matching `orbit.local/project`+`orbit.local/env` labels;
the ConfigMap in `clusters/apps/dashboard-<project>-<env>.yaml`; and every Probe in
`clusters/apps/*.yaml` whose `orbit.local/project`/`orbit.local/env` labels match.
Selection is by labels and by the env namespace, never by parsing names (dashboards:
by the filename the generator reserves). Order is (kind, namespace, name). The sha256
of that list is returned with it.
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from pathlib import Path
from typing import Any

import yaml

from .backends import BackendError
from .helm import HELM, HelmFailure, dns_label, documents_bounded, run_helm, tree_under_root, under_root
from .state import AppState
from .tools_viewer.render_get import read_render, render_path

APPS_REL = "clusters/apps"
NAMESPACES_FILE = "namespaces.yaml"
LABEL_PROJECT = "orbit.local/project"
LABEL_ENV = "orbit.local/env"
KIND_NAMESPACE = "Namespace"
KIND_PROBE = "Probe"


def render(state: AppState, project: str, env: str) -> tuple[list[dict[str, Any]], str]:
    """Desired objects for `project`/`env`, plus the canonical sha256 of that list."""
    text = read_render(state, project, env)
    docs = [d for d in yaml.safe_load_all(text) if isinstance(d, dict)]
    application = next((d for d in docs if d.get("kind") == "Application"), None)
    if application is None:
        root = state.backends.instance.require()
        rel = render_path(project, env, root)
        raise BackendError(
            status=None, body=f"{rel} has no Application",
            what_failed=f"reading {rel} failed", env_var="ORBITD_INSTANCE_DIR",
            retryable=False,
        )
    extras = [d for d in docs if d.get("kind") != "Application"]
    helm_objects = _helm_template(state, application)
    dest_ns = ((application.get("spec") or {}).get("destination") or {}).get("namespace") or ""
    root = state.backends.instance.require()
    by_key: dict[tuple[str, str, str], dict[str, Any]] = {}
    for obj in extras + helm_objects + _tenant_tree(root, project, env, str(dest_ns)):
        obj = _with_namespace(obj, str(dest_ns))
        by_key[_obj_key(obj)] = obj
    objects = [by_key[k] for k in sorted(by_key)]
    return objects, _sha256(objects)


def _tenant_tree(root: Path, project: str, env: str, dest_ns: str) -> list[dict[str, Any]]:
    """Generator objects for this pair: namespaces.yaml, the dashboard file, matching Probes."""
    apps = root / APPS_REL
    out: list[dict[str, Any]] = []
    for doc in _load_yaml_docs(apps / NAMESPACES_FILE):
        if doc.get("kind") == KIND_NAMESPACE:
            if _matches_pair(doc, project, env):
                out.append(doc)
        elif str((doc.get("metadata") or {}).get("namespace") or "") == dest_ns:
            out.append(doc)
    out.extend(_load_yaml_docs(apps / f"dashboard-{project}-{env}.yaml"))
    if apps.is_dir():
        for path in sorted(apps.glob("*.yaml")):
            for doc in _load_yaml_docs(path):
                if doc.get("kind") == KIND_PROBE and _matches_pair(doc, project, env):
                    out.append(doc)
    return out


def _load_yaml_docs(path: Path) -> list[dict[str, Any]]:
    if not path.is_file():
        return []
    try:
        text = path.read_text()
    except OSError as e:
        raise BackendError(
            status=None, body=str(e),
            what_failed=f"reading {path.name} failed", env_var="ORBITD_INSTANCE_DIR",
            retryable=False,
        ) from e
    return [d for d in yaml.safe_load_all(text) if isinstance(d, dict)]


def _matches_pair(obj: dict[str, Any], project: str, env: str) -> bool:
    labels = (obj.get("metadata") or {}).get("labels") or {}
    return (
        str(labels.get(LABEL_PROJECT) or "") == project
        and str(labels.get(LABEL_ENV) or "") == env
    )


def _helm_template(state: AppState, application: dict[str, Any]) -> list[dict[str, Any]]:
    root = state.backends.instance.require()
    spec = application.get("spec") or {}
    source = spec.get("source") or {}
    dest = spec.get("destination") or {}
    helm = source.get("helm") or {}
    values = helm.get("valuesObject")
    if not isinstance(values, dict):
        raise BackendError(
            status=None, body="Application spec.source.helm.valuesObject is missing",
            what_failed="helm template failed", env_var="ORBITD_INSTANCE_DIR",
            retryable=False,
        )
    chart_rel = source.get("path") or "charts/service"
    chart = root / str(chart_rel)
    release = str(helm.get("releaseName") or "").strip()
    if not release:
        release = str((application.get("metadata") or {}).get("name") or "").strip()
    namespace = str(dest.get("namespace") or "").strip()
    if not release or not namespace:
        raise BackendError(
            status=None, body="Application is missing metadata.name or destination.namespace",
            what_failed="helm template failed", env_var="ORBITD_INSTANCE_DIR",
            retryable=False,
        )
    # Shape before argv: a release or namespace of another shape would be read by
    # helm as a flag; a chart path must stay inside the checkout. Neither value
    # is repeated in the refusal.
    if not dns_label(release) or not dns_label(namespace):
        raise BackendError(
            status=None, body="Application release name or destination.namespace is not a DNS label",
            what_failed="helm template failed", env_var="ORBITD_INSTANCE_DIR",
            retryable=False,
        )
    if not isinstance(chart_rel, str) or not under_root(root, chart) or not chart.is_dir():
        raise BackendError(
            status=None, body="Application spec.source.path is not a chart directory inside the checkout",
            what_failed="helm template failed", env_var="ORBITD_INSTANCE_DIR",
            retryable=False,
        )
    if not tree_under_root(root, chart):
        # helm follows a symlink inside a chart's templates
        raise BackendError(
            status=None, body="a file under the chart resolves outside the checkout",
            what_failed="helm template failed", env_var="ORBITD_INSTANCE_DIR",
            retryable=False,
        )
    with tempfile.TemporaryDirectory(prefix="orbitd-helm-") as tmp:
        tmp_path = Path(tmp)
        values_path = tmp_path / "values.yaml"
        values_path.write_text(yaml.safe_dump(values, sort_keys=True))
        env = os.environ.copy()
        env.setdefault("HELM_CACHE_HOME", str(tmp_path / "cache"))
        env.setdefault("HELM_CONFIG_HOME", str(tmp_path / "config"))
        env.setdefault("HELM_DATA_HOME", str(tmp_path / "data"))
        try:
            stdout = run_helm(
                [HELM, "template", release, str(chart), "--namespace", namespace,
                 "-f", str(values_path)],
                cwd=root, env=env,
            )
        except HelmFailure as e:
            raise BackendError(
                status=None, body=str(e), what_failed="helm template failed",
                env_var="ORBITD_INSTANCE_DIR", retryable=False,
            ) from e
    docs = [d for d in yaml.safe_load_all(stdout) if isinstance(d, dict)]
    try:
        documents_bounded(len(docs))
    except HelmFailure as e:
        raise BackendError(
            status=None, body=str(e), what_failed="helm template failed",
            env_var="ORBITD_INSTANCE_DIR", retryable=False,
        ) from e
    return docs


def _with_namespace(obj: dict[str, Any], dest_ns: str) -> dict[str, Any]:
    if obj.get("kind") == "Namespace" or not dest_ns:
        return obj
    meta = obj.setdefault("metadata", {})
    if not meta.get("namespace"):
        meta["namespace"] = dest_ns
    return obj


def _obj_key(obj: dict[str, Any]) -> tuple[str, str, str]:
    meta = obj.get("metadata") or {}
    return (str(obj.get("kind") or ""), str(meta.get("namespace") or ""), str(meta.get("name") or ""))


def _sha256(objects: list[dict[str, Any]]) -> str:
    payload = json.dumps(objects, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(payload.encode()).hexdigest()


__all__ = ["render"]
