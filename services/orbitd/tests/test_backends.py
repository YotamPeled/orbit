"""Backend clients: unconfigured is a named failure, not a crash; HTTP bodies
are truncated; tags_history reads git; kube construction reads a file only."""

from __future__ import annotations

import subprocess
from dataclasses import replace
from pathlib import Path

import httpx
import pytest
from conftest import MCP_URL

from orbitd.backends import (
    BODY_LIMIT, BackendError, BackendUnconfigured, HttpBackend, InstanceRepo, Kube,
)
from orbitd.config import from_env
from orbitd.state import AppState

KUBECONFIG = """apiVersion: v1
kind: Config
clusters:
- cluster:
    server: https://127.0.0.1:1
    insecure-skip-tls-verify: true
  name: fake
contexts:
- context:
    cluster: fake
    user: fake
  name: fake
current-context: fake
users:
- name: fake
  user:
    token: unused
"""


def _env(cfg, **extra: str) -> dict[str, str]:
    return {
        "ORBITD_MCP_URL": MCP_URL,
        "ORBITD_SIGNING_KEY": cfg.signing_key_path,
        "ORBITD_PRINCIPALS": cfg.principals_path,
        "ORBITD_REVOCATIONS": cfg.revocations_path,
        **extra,
    }


def _git(root: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True, check=check)


def test_from_env_absent_backend_vars_are_none_not_a_crash(cfg):
    got = from_env(_env(cfg))
    assert got.instance_dir is None
    assert got.source_sync_period_s == 60
    assert got.prometheus_url is None
    assert got.loki_url is None
    assert got.tempo_url is None
    assert got.alertmanager_url is None
    assert got.kubeconfig is None


def test_from_env_reads_backend_vars_when_set(cfg, tmp_path):
    got = from_env(_env(
        cfg,
        ORBITD_INSTANCE_DIR=str(tmp_path / "instance"),
        ORBITD_PROMETHEUS_URL="http://prom.invalid:9090/",
        ORBITD_LOKI_URL="http://loki.invalid:3100/",
        ORBITD_TEMPO_URL="http://tempo.invalid:3200/",
        ORBITD_ALERTMANAGER_URL="http://am.invalid:9093/",
        ORBITD_KUBECONFIG=str(tmp_path / "kubeconfig"),
        ORBITD_STATE_DIR=str(tmp_path / "state"),
    ))
    assert got.instance_dir == str(tmp_path / "instance")
    assert got.state_dir == str(tmp_path / "state")
    assert got.prometheus_url == "http://prom.invalid:9090"
    assert got.loki_url == "http://loki.invalid:3100"
    assert got.tempo_url == "http://tempo.invalid:3200"
    assert got.alertmanager_url == "http://am.invalid:9093"
    assert got.kubeconfig == str(tmp_path / "kubeconfig")


def test_empty_backend_vars_are_treated_as_absent(cfg):
    got = from_env(_env(cfg, ORBITD_PROMETHEUS_URL="  ", ORBITD_INSTANCE_DIR=""))
    assert got.prometheus_url is None and got.instance_dir is None


@pytest.mark.parametrize("attr,variable", [
    ("prom", "ORBITD_PROMETHEUS_URL"),
    ("loki", "ORBITD_LOKI_URL"),
    ("tempo", "ORBITD_TEMPO_URL"),
    ("alertmanager", "ORBITD_ALERTMANAGER_URL"),
    ("instance", "ORBITD_INSTANCE_DIR"),
])
def test_absent_backend_is_unconfigured_failure_not_a_crash(cfg, attr, variable):
    state = AppState.build(cfg)
    with pytest.raises(BackendUnconfigured) as ei:
        getattr(state.backends, attr).require()
    assert ei.value.variable == variable
    content = ei.value.as_failure().structured_content
    assert content["code"] == "backend_unconfigured"
    assert content["evidence"]["variable"] == variable
    assert variable in content["what_failed"]


def test_http_error_body_is_truncated_to_200_chars():
    payload = "z" * 500

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, text=payload)

    backend = HttpBackend("http://prom.invalid", "ORBITD_PROMETHEUS_URL",
                          transport=httpx.MockTransport(handler))
    with pytest.raises(BackendError) as ei:
        backend.get("/api/v1/query")
    err = ei.value
    assert err.status == 503
    assert err.body == "z" * BODY_LIMIT
    assert len(err.body) == BODY_LIMIT
    content = err.as_failure().structured_content
    assert content["code"] == "backend_error"
    assert content["evidence"]["status"] == 503
    assert content["evidence"]["body"] == "z" * BODY_LIMIT
    dumped = str(content)
    assert "z" * 201 not in dumped


def test_http_get_returns_json_on_success():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"status": "success", "data": {"result": []}})

    backend = HttpBackend("http://prom.invalid", "ORBITD_PROMETHEUS_URL",
                          transport=httpx.MockTransport(handler))
    assert backend.get("/api/v1/query", params={"query": "up"}) == {
        "status": "success", "data": {"result": []},
    }


def test_tags_history_on_a_temporary_git_repo_with_two_commits(tmp_path):
    root = tmp_path / "instance"
    catalog = root / "catalog"
    catalog.mkdir(parents=True)
    tags = catalog / "tags.yaml"
    tags.write_text("tags: {}\n")
    _git(root, "init", "-b", "main")
    _git(root, "add", "catalog/tags.yaml")
    _git(root, "-c", "user.name=dev", "-c", "user.email=dev@example.test",
         "-c", "commit.gpgsign=false", "commit", "-m", "first")
    tags.write_text("tags:\n  app:\n    prod:\n      web: abc1234\n")
    _git(root, "add", "catalog/tags.yaml")
    _git(root, "-c", "user.name=dev", "-c", "user.email=dev@example.test",
         "-c", "commit.gpgsign=false", "commit", "-m", "second")
    history = InstanceRepo(str(root)).tags_history(limit=10)
    assert [row["subject"] for row in history] == ["second", "first"]
    assert all(len(row["commit"]) == 40 for row in history)
    assert all("T" in row["authored"] for row in history)
    assert InstanceRepo(str(root)).tags_history(limit=1)[0]["subject"] == "second"
    skipped = InstanceRepo(str(root)).tags_history(limit=1, skip=1)
    assert [row["subject"] for row in skipped] == ["first"]


def test_kube_client_from_kubeconfig_does_not_call_the_cluster(cfg, tmp_path):
    path = tmp_path / "kubeconfig"
    path.write_text(KUBECONFIG)
    state = AppState.build(replace(cfg, kubeconfig=str(path)))
    core = state.backends.kube.core
    host = core.api_client.configuration.host.rstrip("/")
    assert host == "https://127.0.0.1:1"
    # Same construction path, no AppState: the file is read, the cluster is not.
    kube = Kube(str(path))
    assert kube.core.api_client.configuration.host.rstrip("/") == "https://127.0.0.1:1"


def test_fake_backends_fixture_is_appstate_with_in_memory_kube(fake_backends):
    state = fake_backends
    state.backends.kube.add_pod(namespace="demo-prod", name="web-0", last_terminated="OOMKilled")
    state.backends.kube.add_deployment(namespace="demo-prod", name="web")
    state.backends.kube.add_cluster(namespace="demo-prod", name="demo-db")
    pods = state.backends.kube.core.list_namespaced_pod("demo-prod").items
    assert pods[0].metadata.name == "web-0"
    assert pods[0].status.containerStatuses[0].lastState.terminated.reason == "OOMKilled"
    assert state.backends.kube.apps.read_namespaced_deployment("web", "demo-prod").spec.replicas == 1
    clusters = state.backends.kube.custom.list_namespaced_custom_object(
        "postgresql.cnpg.io", "v1", "demo-prod", "clusters")
    assert clusters["items"][0]["metadata"]["name"] == "demo-db"
    assert state.backends.prom.get("/api/v1/query")["status"] == "success"
    assert state.backends.loki.get("/loki/api/v1/query_range")["status"] == "success"
    assert state.backends.instance.projects()["projects"] == []
    assert state.backends.instance.tags_history(limit=5)[0]["subject"] == "instance"
