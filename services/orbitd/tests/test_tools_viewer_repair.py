"""Repair tests: each defect from r13-verdict, reproduced then fixed."""

from __future__ import annotations

import subprocess

import httpx
import pytest
from conftest import mint, sdk_client
from mcp.server.auth.provider import AccessToken

from orbitd.backends import BackendError, HttpBackend, InstanceRepo
from orbitd.roles import Registry, Role, RoleGate
from orbitd.tools_viewer import register
from orbitd.tools_viewer.common import desired_tag
from orbitd.tools_viewer.deploy_history import _ABSENT, _tag_at

from test_tools_viewer_kube import DEMO, call, labels, seed_catalog
from test_tools_viewer_observability import _limit_loki, _loki, invoke


# ------------------------------------------------------------------ 1. memory accounting


@pytest.mark.anyio
async def test_budget_and_health_use_effective_reservation_and_skip_terminal(fake_backends, http):
    kube = fake_backends.backends.kube
    kube.namespaces.append({"metadata": {"name": "demo-prod"}})
    kube.nodes.append({
        "metadata": {"name": "k3s-1"},
        "status": {"allocatable": {"memory": "16Gi"}},
    })
    kube.quotas.append({
        "metadata": {"name": "ns-quota", "namespace": "demo-prod"},
        "spec": {"hard": {"requests.memory": "1768Mi"}},
        "status": {"hard": {"requests.memory": "1768Mi"}, "used": {"requests.memory": "768Mi"}},
    })
    app = kube.add_pod(namespace="demo-prod", name="web-0", component="web")
    app["spec"]["nodeName"] = "k3s-1"
    app["spec"]["containers"][0]["resources"] = {
        "requests": {"memory": "768Mi"}, "limits": {"memory": "768Mi"},
    }
    app["spec"]["initContainers"] = [{
        "name": "init",
        "resources": {"requests": {"memory": "448Mi"}, "limits": {"memory": "448Mi"}},
    }]
    done = kube.add_pod(namespace="demo-prod", name="job-0", component="job", phase="Succeeded")
    done["spec"]["nodeName"] = "k3s-1"
    done["spec"]["containers"][0]["resources"] = {
        "requests": {"memory": "2048Mi"}, "limits": {"memory": "2048Mi"},
    }
    dying = kube.add_pod(namespace="demo-prod", name="old-0", component="old")
    dying["spec"]["nodeName"] = "k3s-1"
    dying["metadata"]["deletionTimestamp"] = "2026-01-02T00:00:00Z"
    dying["spec"]["containers"][0]["resources"] = {
        "requests": {"memory": "4096Mi"}, "limits": {"memory": "4096Mi"},
    }

    async with sdk_client(http, mint(fake_backends, Role.viewer)) as client:
        budget = await client.call_tool("orbit_platform_budget", {})
        health = await client.call_tool("orbit_platform_health", {})
    assert not budget.is_error, budget.content
    by_ns = {item["namespace"]: item for item in budget.structured_content["items"]}
    # max(768, 448) = 768; terminal and terminating pods are excluded.
    assert by_ns["demo-prod"]["requests_mi"] == 768
    assert by_ns["_total"]["requests_mi"] == 768
    node = {n["name"]: n for n in health.structured_content["nodes"]}["k3s-1"]
    assert node["requested_mi"] == 768
    quota = {q["namespace"]: q for q in health.structured_content["quotas"]}["demo-prod"]
    assert quota["largest_component_mi"] == 768
    assert quota["headroom_mi"] == 1000
    assert quota["surge_fits"] is True


# ------------------------------------------------------------------ 2. flat tags


@pytest.mark.anyio
async def test_deploy_status_reads_flat_tag_keys(fake_backends, http):
    seed_catalog(fake_backends, [DEMO], tags={"demo/prod/web": "wanted"})
    kube = fake_backends.backends.kube
    kube.add_pod(namespace="demo-prod", name="web-0", component="web",
                 image="mirror.invalid/app:old", labels=labels("demo", "web"))
    kube.add_pod(namespace="demo-prod", name="worker-0", component="worker",
                 image="mirror.invalid/app:whatever", labels=labels("demo", "worker"))
    r = await call(http, fake_backends, "orbit_deploy_status", {"project": "demo", "env": "prod"})
    assert not r.is_error, r.content
    by_name = {c["component"]: c for c in r.structured_content["components"]}
    assert by_name["web"]["desired_tag"] == "wanted"
    assert by_name["web"]["drift"] is True
    # Missing key is catalog default: desired_tag null and drift false only then.
    assert by_name["worker"]["desired_tag"] is None
    assert by_name["worker"]["drift"] is False
    assert desired_tag(fake_backends, "demo", "prod", "web") == "wanted"
    assert desired_tag(fake_backends, "demo", "prod", "missing") is None


def test_tag_at_reads_flat_keys(tmp_path):
    root = tmp_path / "instance"
    catalog = root / "catalog"
    catalog.mkdir(parents=True)
    (catalog / "tags.yaml").write_text("tags:\n  demo/prod/web: wanted\n")
    subprocess.run(["git", "-C", str(root), "init", "-b", "main"], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(root), "add", "catalog/tags.yaml"], check=True, capture_output=True)
    subprocess.run(
        ["git", "-C", str(root), "-c", "user.name=dev", "-c", "user.email=dev@example.test",
         "-c", "commit.gpgsign=false", "commit", "-m", "tag"],
        check=True, capture_output=True,
    )
    inst = InstanceRepo(str(root))
    sha = inst.tags_history(limit=1)[0]["commit"]
    assert _tag_at(inst, sha, "demo", "prod", "web") == "wanted"
    assert _tag_at(inst, sha, "demo", "prod", "worker") is _ABSENT


# ------------------------------------------------------------------ 3. log pod selection by labels


def test_logs_query_selects_pods_by_chart_labels_not_name_prefix(fake_backends):
    state = fake_backends
    state.backends.kube.add_pod(
        namespace="demo-prod", name="unrelated-name", component="web",
        labels={"app.kubernetes.io/instance": "demo", "app.kubernetes.io/name": "web"},
    )
    state.backends.kube.add_pod(
        namespace="demo-prod", name="demo-web-0", component="other",
        labels={"app.kubernetes.io/instance": "demo", "app.kubernetes.io/name": "other"},
    )
    state.backends.loki.responses["/loki/api/v1/query_range"] = _loki(
        ("100", "unrelated-name", "ok"),
    )
    seed_catalog(state, [DEMO])
    invoke(state, "orbit_logs_query", project="demo", env="prod", component="web")
    query = state.backends.loki.calls[-1]["params"]["query"]
    assert 'pod=~"unrelated-name"' in query
    assert "demo-web-.*" not in query
    assert "demo-web-0" not in query


# ------------------------------------------------------------------ 4. resources share the tool scope gate


class _Ctx:
    def __init__(self, method, params):
        self.method = method
        self.params = params


@pytest.mark.anyio
async def test_resources_read_forbidden_matches_catalog_get_without_viewer(fake_backends, monkeypatch):
    registry = Registry()
    register(registry, fake_backends)
    gate = RoleGate(registry)
    token = AccessToken(
        token="t", client_id="op", scopes=["orbit:operator"], expires_at=2**31, subject="op",
    )
    monkeypatch.setattr("orbitd.roles.get_access_token", lambda: token)

    async def reached(ctx):
        return {"reached": True}

    async def listed_all(ctx):
        return {"resources": [
            {"uri": "orbit://catalog/projects"},
            {"uri": "orbit://budget"},
        ]}

    tool = await gate(_Ctx("tools/call", {"name": "orbit_catalog_get"}), reached)
    resource = await gate(_Ctx("resources/read", {"uri": "orbit://catalog/projects"}), reached)
    assert tool.get("isError") is True
    assert resource.get("isError") is True
    assert tool["structuredContent"]["code"] == "FORBIDDEN"
    assert resource["structuredContent"]["code"] == "FORBIDDEN"
    assert tool["structuredContent"] == resource["structuredContent"]
    assert resource["structuredContent"]["evidence"]["required_role"] == "viewer"

    listed = await gate(_Ctx("resources/list", {}), listed_all)
    assert listed["resources"] == []


# ------------------------------------------------------------------ 5. git subprocess failures


def test_instance_repo_missing_git_is_backend_error(tmp_path, monkeypatch):
    root = tmp_path / "instance"
    (root / "catalog").mkdir(parents=True)
    (root / "catalog" / "tags.yaml").write_text("tags: {}\n")
    inst = InstanceRepo(str(root))

    def no_git(*args, **kwargs):
        raise FileNotFoundError(2, "No such file or directory", "git")

    monkeypatch.setattr(subprocess, "run", no_git)
    with pytest.raises(BackendError) as ei:
        inst.tags_history()
    err = ei.value
    assert "git" in err.what_failed
    content = err.as_failure().structured_content
    assert content["code"] == "backend_error"
    assert content["retryable"] is False
    assert "git" in content["what_failed"]


def test_instance_repo_nonzero_git_is_backend_error(tmp_path):
    inst = InstanceRepo(str(tmp_path / "not-a-repo"))
    (tmp_path / "not-a-repo").mkdir()
    with pytest.raises(BackendError) as ei:
        inst.tags_history()
    err = ei.value
    assert err.what_failed.startswith("git ")
    assert err.as_failure().structured_content["retryable"] is False


# ------------------------------------------------------------------ 6. traces_find TraceQL only


def test_traces_find_builds_one_traceql_query_never_tags_param(fake_backends):
    state = fake_backends
    state.backends.tempo.responses["/api/search"] = {"traces": []}
    invoke(state, "orbit_traces_find", service="web", tags={"http.method": "GET"})
    params = state.backends.tempo.calls[-1]["params"]
    assert "tags" not in params
    assert params["q"] == '{resource.service.name="web" && span.http.method="GET"}'


# ------------------------------------------------------------------ 7. log pagination 100..500 limit=2


def test_logs_pagination_limit_2_over_100_to_500(fake_backends, monkeypatch):
    monkeypatch.setattr("orbitd.tools_viewer.logs_query.now", lambda: 900.0)
    state = fake_backends
    state.backends.kube.add_pod(
        namespace="demo-prod", name="web-0", component="web",
        labels={"app.kubernetes.io/instance": "demo", "app.kubernetes.io/name": "web"},
    )
    rows = [(str(ts), "web-0", f"l-{ts}") for ts in (100, 200, 300, 400, 500)]
    state.backends.loki.responses["/loki/api/v1/query_range"] = _limit_loki(rows)
    seed_catalog(state, [DEMO])
    page1 = invoke(state, "orbit_logs_query", project="demo", env="prod",
                   component="web", limit=2)
    assert [i["ts"] for i in page1.items] == ["100", "200"]
    assert page1.truncated is True and page1.next_cursor
    page2 = invoke(state, "orbit_logs_query", project="demo", env="prod",
                   component="web", limit=2, cursor=page1.next_cursor)
    assert [i["ts"] for i in page2.items] == ["300", "400"]
    assert page2.truncated is True and page2.next_cursor
    page3 = invoke(state, "orbit_logs_query", project="demo", env="prod",
                   component="web", limit=2, cursor=page2.next_cursor)
    assert [i["ts"] for i in page3.items] == ["500"]
    assert page3.truncated is False and page3.next_cursor is None


def test_logs_pagination_keeps_ties_at_the_cursor_timestamp(fake_backends, monkeypatch):
    monkeypatch.setattr("orbitd.tools_viewer.logs_query.now", lambda: 900.0)
    state = fake_backends
    state.backends.kube.add_pod(
        namespace="demo-prod", name="web-0", component="web",
        labels={"app.kubernetes.io/instance": "demo", "app.kubernetes.io/name": "web"},
    )
    rows = [("200", "web-0", "a"), ("200", "web-0", "b"), ("300", "web-0", "c")]
    state.backends.loki.responses["/loki/api/v1/query_range"] = _limit_loki(rows)
    seed_catalog(state, [DEMO])
    page1 = invoke(state, "orbit_logs_query", project="demo", env="prod",
                   component="web", limit=1)
    assert page1.items[0]["ts"] == "200" and page1.items[0]["line"] == "a"
    page2 = invoke(state, "orbit_logs_query", project="demo", env="prod",
                   component="web", limit=2, cursor=page1.next_cursor)
    assert [i["line"] for i in page2.items] == ["b", "c"]


# ------------------------------------------------------------------ 8. deploy_status ignores terminating/terminal


@pytest.mark.anyio
async def test_deploy_status_ignores_terminating_and_terminal_pods(fake_backends, http):
    seed_catalog(fake_backends, [DEMO], tags={"demo/prod/web": "wanted"})
    kube = fake_backends.backends.kube
    live = kube.add_pod(namespace="demo-prod", name="web-0", component="web",
                        image="mirror.invalid/app:old", labels=labels("demo", "web"))
    terminating = kube.add_pod(namespace="demo-prod", name="web-1", component="web",
                               image="mirror.invalid/app:wanted", labels=labels("demo", "web"))
    terminating["metadata"]["deletionTimestamp"] = "2026-01-02T00:00:00Z"
    done = kube.add_pod(namespace="demo-prod", name="web-2", component="web",
                        image="mirror.invalid/app:wanted", labels=labels("demo", "web"),
                        phase="Succeeded")
    r = await call(http, fake_backends, "orbit_deploy_status", {"project": "demo", "env": "prod"})
    assert not r.is_error, r.content
    web = next(c for c in r.structured_content["components"] if c["component"] == "web")
    assert web["running_images"] == [live["spec"]["containers"][0]["image"]]
    assert "wanted" not in "".join(web["running_images"])
    assert web["drift"] is True
    assert "web-1" not in web["pods"] and "web-2" not in web["pods"]
    assert done["status"]["phase"] == "Succeeded"


# ------------------------------------------------------------------ 9. workload_describe nested pods cap


@pytest.mark.anyio
async def test_workload_describe_caps_nested_pods_at_100(fake_backends, http):
    seed_catalog(fake_backends, [DEMO])
    kube = fake_backends.backends.kube
    for i in range(104):
        kube.add_pod(namespace="demo-prod", name=f"web-{i}", component="web",
                     labels=labels("demo", "web"))
    r = await call(http, fake_backends, "orbit_workload_describe",
                   {"project": "demo", "env": "prod", "component": "web"})
    assert not r.is_error, r.content
    pods = r.structured_content["pods"]
    assert pods["returned"] == 100
    assert pods["total"] == 104
    assert pods["truncated"] is True
    assert pods["next_cursor"] == "100"
    assert len(pods["items"]) == 100


# ------------------------------------------------------------------ 10. failure envelopes


@pytest.mark.anyio
async def test_kube_maxretry_becomes_backend_error_envelope(fake_backends, http):
    seed_catalog(fake_backends, [DEMO])

    class MaxRetryError(Exception):
        pass

    def boom(*args, **kwargs):
        raise MaxRetryError("connection refused")

    fake_backends.backends.kube.core.list_namespaced_pod = boom
    r = await call(http, fake_backends, "orbit_workload_list", {"project": "demo", "env": "prod"})
    assert r.is_error
    content = r.structured_content
    assert content["code"] == "backend_error"
    assert content["retryable"] is True
    assert "ORBITD_KUBECONFIG" in content["next_action"]
    assert "namespace orbit" in content["next_action"]
    assert "NetworkPolicy" in content["next_action"]
    assert "http://" not in content["next_action"]
    assert "https://" not in content["what_failed"]


def test_backend_error_retryable_only_for_transport_and_5xx():
    refused = BackendError(
        status=None, body="[Errno 111] Connection refused",
        what_failed="ORBITD_LOKI_URL request failed", env_var="ORBITD_LOKI_URL",
    ).as_failure().structured_content
    assert refused["retryable"] is True
    assert refused["evidence"]["variable"] == "ORBITD_LOKI_URL"
    assert "ORBITD_LOKI_URL" in refused["next_action"]
    assert "namespace orbit" in refused["next_action"]
    assert "NetworkPolicy" in refused["next_action"]
    assert "http://" not in refused["next_action"]

    bad_query = BackendError(
        status=400, body="invalid query",
        what_failed="ORBITD_TEMPO_URL returned 400", env_var="ORBITD_TEMPO_URL",
    ).as_failure().structured_content
    assert bad_query["retryable"] is False
    assert "query syntax" in bad_query["next_action"]
    assert "ORBITD_TEMPO_URL" in bad_query["next_action"]

    server = BackendError(
        status=503, body="z" * 50,
        what_failed="ORBITD_LOKI_URL returned 503", env_var="ORBITD_LOKI_URL",
    ).as_failure().structured_content
    assert server["retryable"] is True


def test_http_400_is_not_retryable():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, text="bad query")

    backend = HttpBackend("http://tempo.invalid", "ORBITD_TEMPO_URL",
                          transport=httpx.MockTransport(handler))
    with pytest.raises(BackendError) as ei:
        backend.get("/api/search")
    content = ei.value.as_failure().structured_content
    assert content["retryable"] is False
    assert "query syntax" in content["next_action"]
    assert "ORBITD_TEMPO_URL" in content["next_action"]
    assert "http://tempo.invalid" not in content["next_action"]
    assert "http://tempo.invalid" not in content["what_failed"]
