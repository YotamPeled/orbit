"""Platform viewer tools and orbit:// resources. Fake backends only."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest
from conftest import envelope, mcp_headers, mint, sdk_client

from orbitd.roles import Role

pytestmark = pytest.mark.anyio

SCHEMA_JSON = '{"title": "Orbit service values", "type": "object"}'
RENDER_YAML = "kind: Application\nmetadata:\n  name: demo-prod\n"


def _git(root: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True, text=True)


def _commit(root: Path, message: str) -> None:
    _git(root, "add", "catalog/tags.yaml")
    _git(root, "-c", "user.name=dev", "-c", "user.email=dev@example.test",
         "-c", "commit.gpgsign=false", "commit", "-m", message)


def seed_cluster(kube) -> None:
    kube.namespaces.extend([
        {"metadata": {"name": "demo-prod"}},
        {"metadata": {"name": "demo-dev"}},
    ])
    kube.nodes.append({
        "metadata": {"name": "k3s-1"},
        "status": {"allocatable": {"memory": "16Gi"}},
    })
    prod = kube.add_pod(namespace="demo-prod", name="web-0", component="web")
    prod["spec"]["nodeName"] = "k3s-1"
    prod["spec"]["containers"][0]["resources"] = {
        "requests": {"memory": "2Gi"}, "limits": {"memory": "3Gi"},
    }
    dev = kube.add_pod(namespace="demo-dev", name="web-0", component="web")
    dev["spec"]["nodeName"] = "k3s-1"
    dev["spec"]["containers"][0]["resources"] = {
        "requests": {"memory": "256Mi"}, "limits": {"memory": "512Mi"},
    }
    kube.quotas.append({
        "metadata": {"name": "ns-quota", "namespace": "demo-prod"},
        "spec": {"hard": {"requests.memory": "4Gi"}},
        "status": {"hard": {"requests.memory": "4Gi"}, "used": {"requests.memory": "3Gi"}},
    })
    kube.quotas.append({
        "metadata": {"name": "ns-quota", "namespace": "demo-dev"},
        "spec": {"hard": {"requests.memory": "4Gi"}},
        "status": {"hard": {"requests.memory": "4Gi"}, "used": {"requests.memory": "512Mi"}},
    })
    kube.add_deployment(namespace="demo-prod", name="web", replicas=2, ready=1)
    kube.add_deployment(namespace="demo-dev", name="web", replicas=1, ready=1)
    kube.certificates.append({
        "metadata": {"name": "wildcard", "namespace": "traefik"},
        "status": {"notAfter": "2099-01-01T00:00:00Z"},
    })
    kube.certificates.append({
        "metadata": {"name": "expired", "namespace": "traefik"},
        "status": {"notAfter": "2020-01-01T00:00:00Z"},
    })
    kube.pod_metrics = [
        {"metadata": {"name": "web-0", "namespace": "demo-prod"},
         "containers": [{"name": "web", "usage": {"memory": "400Mi"}}]},
        {"metadata": {"name": "web-0", "namespace": "demo-dev"},
         "containers": [{"name": "web", "usage": {"memory": "80Mi"}}]},
    ]


def seed_instance(root: Path) -> None:
    (root / "charts/service").mkdir(parents=True, exist_ok=True)
    (root / "charts/service/values.schema.json").write_text(SCHEMA_JSON)
    (root / "clusters/apps").mkdir(parents=True, exist_ok=True)
    (root / "clusters/apps/demo-prod.yaml").write_text(RENDER_YAML)
    tags = root / "catalog/tags.yaml"
    tags.write_text("tags:\n  demo/prod/web: first\n  other/prod/web: skip-me\n")
    _commit(root, "tag demo prod first")
    tags.write_text("tags:\n  demo/prod/web: second\n  other/prod/web: skip-me\n")
    _commit(root, "tag demo prod second")


def rows_by_ns(body: dict) -> dict:
    return {item["namespace"]: item for item in body["items"]}


async def test_budget_per_namespace_with_total_and_allocatable(fake_backends, http):
    seed_cluster(fake_backends.backends.kube)
    async with sdk_client(http, mint(fake_backends, Role.viewer)) as client:
        r = await client.call_tool("orbit_platform_budget", {})
    assert not r.is_error, r.content
    body = r.structured_content
    by_ns = rows_by_ns(body)
    prod = by_ns["demo-prod"]
    assert prod["requests_mi"] == 2048 and prod["limits_mi"] == 3072
    assert prod["used_mi"] == 400
    assert prod["quota_hard_mi"] == 4096 and prod["quota_used_mi"] == 3072
    dev = by_ns["demo-dev"]
    assert dev["requests_mi"] == 256 and dev["limits_mi"] == 512 and dev["used_mi"] == 80
    total = by_ns["_total"]
    assert total["requests_mi"] == 2048 + 256
    assert total["limits_mi"] == 3072 + 512
    assert total["used_mi"] == 480
    assert body["node_allocatable_mi"] == 16384
    assert body["truncated"] is False and body["next_cursor"] is None


async def test_budget_used_mi_absent_when_metrics_api_missing(fake_backends, http):
    kube = fake_backends.backends.kube
    kube.namespaces.append({"metadata": {"name": "demo-prod"}})
    pod = kube.add_pod(namespace="demo-prod", name="web-0")
    pod["spec"]["containers"][0]["resources"] = {"requests": {"memory": "256Mi"}, "limits": {"memory": "256Mi"}}
    assert kube.pod_metrics is None
    async with sdk_client(http, mint(fake_backends, Role.viewer)) as client:
        r = await client.call_tool("orbit_platform_budget", {})
    assert not r.is_error, r.content
    prod = rows_by_ns(r.structured_content)["demo-prod"]
    assert prod["used_mi"] is None
    assert rows_by_ns(r.structured_content)["_total"]["used_mi"] is None


async def test_health_headroom_surge_certs_and_readiness(fake_backends, http):
    seed_cluster(fake_backends.backends.kube)
    async with sdk_client(http, mint(fake_backends, Role.viewer)) as client:
        r = await client.call_tool("orbit_platform_health", {})
    assert not r.is_error, r.content
    body = r.structured_content
    nodes = {n["name"]: n for n in body["nodes"]}
    assert nodes["k3s-1"]["allocatable_mi"] == 16384
    assert nodes["k3s-1"]["requested_mi"] == 2048 + 256
    quotas = {q["namespace"]: q for q in body["quotas"]}
    prod = quotas["demo-prod"]
    assert prod["hard_mi"] == 4096 and prod["used_mi"] == 3072
    assert prod["headroom_mi"] == 1024
    assert prod["largest_component_mi"] == 2048
    assert prod["surge_fits"] is False
    dev = quotas["demo-dev"]
    assert dev["headroom_mi"] == 4096 - 512
    assert dev["largest_component_mi"] == 256
    assert dev["surge_fits"] is True
    certs = {c["name"]: c for c in body["certificates"]}
    assert certs["wildcard"]["days_left"] > 0
    assert certs["expired"]["days_left"] < 0
    assert certs["wildcard"]["not_after"] == "2099-01-01T00:00:00Z"
    unready = {(d["namespace"], d["name"]): d for d in body["readiness"]}
    assert unready["demo-prod", "web"]["unavailable"] == 1
    assert ("demo-dev", "web") not in unready


async def test_resource_read_with_viewer_token_and_refusal_without(fake_backends, http):
    seed_instance(Path(fake_backends.backends.instance.root))
    token = mint(fake_backends, Role.viewer)
    async with sdk_client(http, token) as client:
        listed = await client.list_resources()
        uris = {str(r.uri) for r in listed.resources}
        assert "orbit://catalog/projects" in uris
        assert "orbit://budget" in uris
        got = await client.read_resource("orbit://catalog/projects")
    text = got.contents[0].text
    payload = json.loads(text)
    assert payload["projects"] == []
    async with http() as h:
        refused = await h.post(
            "/mcp",
            json=envelope("resources/read", {"uri": "orbit://budget"}),
            headers=mcp_headers("resources/read", "orbit://budget"),
        )
    assert refused.status_code == 401


async def test_render_and_ledger_templates_resolve(fake_backends, http):
    seed_instance(Path(fake_backends.backends.instance.root))
    async with sdk_client(http, mint(fake_backends, Role.viewer)) as client:
        templates = await client.list_resource_templates()
        uris = {t.uri_template for t in templates.resource_templates}
        assert "orbit://render/{project}/{env}" in uris
        assert "orbit://ledger/{project}/{env}" in uris
        rendered = await client.read_resource("orbit://render/demo/prod")
        assert RENDER_YAML in rendered.contents[0].text
        ledger = await client.read_resource("orbit://ledger/demo/prod")
        body = json.loads(ledger.contents[0].text)
        subjects = [item["subject"] for item in body["items"]]
        assert "tag demo prod second" in subjects
        assert "tag demo prod first" in subjects
        for item in body["items"]:
            assert list(item["tags"]) == ["demo/prod/web"]


async def test_schema_render_ledger_tools(fake_backends, http):
    seed_instance(Path(fake_backends.backends.instance.root))
    async with sdk_client(http, mint(fake_backends, Role.viewer)) as client:
        schema = await client.call_tool("orbit_catalog_schema", {})
        rendered = await client.call_tool("orbit_render_get", {"project": "demo", "env": "prod"})
        missing = await client.call_tool("orbit_render_get", {"project": "demo", "env": "dev"})
        ledger = await client.call_tool("orbit_ledger_get", {"project": "demo", "env": "prod", "limit": 1})
        other = await client.call_tool("orbit_ledger_get", {"project": "other", "env": "prod"})
        budget_res = await client.read_resource("orbit://budget")
        budget_tool = await client.call_tool("orbit_platform_budget", {})
    assert not schema.is_error, schema.content
    assert schema.structured_content["path"] == "charts/service/values.schema.json"
    assert "Orbit service values" in schema.structured_content["body"]
    assert not rendered.is_error
    assert rendered.structured_content["yaml"] == RENDER_YAML
    assert missing.is_error and missing.structured_content["code"] == "not_found"
    assert not ledger.is_error
    assert ledger.structured_content["returned"] == 1
    assert ledger.structured_content["truncated"] is True
    assert ledger.structured_content["next_cursor"] == "1"
    assert ledger.structured_content["items"][0]["tags"]["demo/prod/web"] == "second"
    other_subjects = [i["subject"] for i in other.structured_content["items"]]
    assert other_subjects  # other/prod/web is in both commits
    assert json.loads(budget_res.contents[0].text)["items"] == budget_tool.structured_content["items"]


async def test_admin_token_can_call_viewer_platform_tools(fake_backends, http):
    seed_cluster(fake_backends.backends.kube)
    async with sdk_client(http, mint(fake_backends, Role.admin)) as client:
        health = await client.call_tool("orbit_platform_health", {})
        budget = await client.call_tool("orbit_platform_budget", {})
    assert not health.is_error and not budget.is_error


async def test_schema_missing_is_not_found(fake_backends, http):
    async with sdk_client(http, mint(fake_backends, Role.viewer)) as client:
        r = await client.call_tool("orbit_catalog_schema", {})
    assert r.is_error and r.structured_content["code"] == "not_found"
