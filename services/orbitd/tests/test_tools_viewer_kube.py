"""Viewer tools over the instance catalog and the Kubernetes API.

Uses only the fake_backends fixture: no cluster, no network.
"""

from __future__ import annotations

import subprocess

import pytest
import yaml
from conftest import mint, sdk_client

from orbitd.backends import BackendUnconfigured
from orbitd.roles import Role

pytestmark = pytest.mark.anyio

VIEWER_TOOLS = (
    "orbit_project_list",
    "orbit_catalog_get",
    "orbit_deploy_status",
    "orbit_deploy_history",
    "orbit_workload_list",
    "orbit_workload_describe",
    "orbit_db_status",
    "orbit_job_status",
    "orbit_job_wait",
)

DEMO = {
    "id": "demo",
    "name": "Demo",
    "environments": {"prod": {"branch": "main"}, "dev": {"branch": "dev", "enabled": True}},
    "components": [{"name": "web", "kind": "web"}, {"name": "worker", "kind": "worker"}],
}
DEMO_DB = {
    **DEMO,
    "database": {"size": "2Gi", "memory": "256Mi"},
}


@pytest.fixture
def state(fake_backends):
    return fake_backends


class UnconfiguredKube:
    def __getattr__(self, name):
        if name in {"core", "apps", "custom"}:
            raise BackendUnconfigured("ORBITD_KUBECONFIG")
        raise AttributeError(name)


def labels(project: str, component: str) -> dict[str, str]:
    return {"app.kubernetes.io/instance": project, "app.kubernetes.io/name": component}


def seed_catalog(state, projects, tags=None) -> None:
    root = state.backends.instance_root
    (root / "catalog" / "projects.yaml").write_text(
        yaml.safe_dump({"version": 3, "projects": projects}, sort_keys=False))
    if tags is not None:
        (root / "catalog" / "tags.yaml").write_text(
            yaml.safe_dump({"tags": tags}, sort_keys=False))


def commit_tags(state, tags, message: str) -> None:
    root = state.backends.instance_root
    (root / "catalog" / "tags.yaml").write_text(
        yaml.safe_dump({"tags": tags}, sort_keys=False))
    subprocess.run(["git", "-C", str(root), "add", "catalog/tags.yaml"], check=True, capture_output=True)
    subprocess.run(
        ["git", "-C", str(root), "-c", "user.name=dev", "-c", "user.email=dev@example.test",
         "-c", "commit.gpgsign=false", "commit", "-m", message],
        check=True, capture_output=True,
    )


async def call(http, state, name, arguments=None, role=Role.viewer):
    async with sdk_client(http, mint(state, role)) as client:
        return await client.call_tool(name, arguments or {})


async def test_project_list_and_catalog_get(state, http):
    seed_catalog(state, [DEMO])
    listed = await call(http, state, "orbit_project_list")
    assert not listed.is_error, listed.content
    items = listed.structured_content["items"]
    assert items == [{
        "id": "demo",
        "name": "Demo",
        "environments": ["prod", "dev"],
        "components": ["web", "worker"],
    }]
    assert listed.structured_content["truncated"] is False

    one = await call(http, state, "orbit_catalog_get", {"project": "demo"})
    assert not one.is_error
    assert one.structured_content["data"]["id"] == "demo"
    assert "web" in [c["name"] for c in one.structured_content["data"]["components"]]

    all_of = await call(http, state, "orbit_catalog_get", {})
    assert not all_of.is_error
    assert [p["id"] for p in all_of.structured_content["data"]["projects"]] == ["demo"]


async def test_catalog_get_unknown_project_is_not_found(state, http):
    seed_catalog(state, [DEMO])
    r = await call(http, state, "orbit_catalog_get", {"project": "missing"})
    assert r.is_error
    assert r.structured_content["code"] == "not_found"


async def test_deploy_status_detects_drift_and_reports_generation(state, http):
    seed_catalog(state, [DEMO], tags={"demo/prod/web": "abc1234", "demo/prod/worker": "abc1234"})
    kube = state.backends.kube
    kube.add_pod(namespace="demo-prod", name="web-0", component="web",
                 image="mirror.invalid/app:abc1234", labels=labels("demo", "web"))
    kube.add_pod(namespace="demo-prod", name="worker-0", component="worker",
                 image="mirror.invalid/app:other", labels=labels("demo", "worker"),
                 last_terminated="OOMKilled")
    web = kube.add_deployment(namespace="demo-prod", name="demo-web",
                              image="mirror.invalid/app:abc1234")
    web["metadata"]["generation"] = 3
    web["status"]["observedGeneration"] = 2
    kube.add_deployment(namespace="demo-prod", name="demo-worker",
                        image="mirror.invalid/app:other")

    r = await call(http, state, "orbit_deploy_status", {"project": "demo", "env": "prod"})
    assert not r.is_error, r.content
    by_name = {c["component"]: c for c in r.structured_content["components"]}
    assert by_name["web"]["desired_tag"] == "abc1234"
    assert by_name["web"]["drift"] is False
    assert by_name["web"]["generation"] == 3
    assert by_name["web"]["observed_generation"] == 2
    assert "web-0" in by_name["web"]["pods"]
    assert by_name["worker"]["drift"] is True
    assert by_name["worker"]["running_images"] == ["mirror.invalid/app:other"]


async def test_deploy_status_uses_spec_image_not_kubelet_status_name(state, http):
    """A pod whose spec pins tag A must not look like drift when the kubelet
    reports a sibling tag B that shares the digest."""
    seed_catalog(state, [DEMO], tags={"demo/prod/web": "tag-a"})
    kube = state.backends.kube
    digest = "docker-pullable://mirror.invalid/app@sha256:" + ("a" * 64)
    pod = kube.add_pod(namespace="demo-prod", name="web-0", component="web",
                       image="mirror.invalid/app:tag-a", labels=labels("demo", "web"))
    pod["status"]["containerStatuses"][0]["image"] = "mirror.invalid/app:tag-b"
    pod["status"]["containerStatuses"][0]["imageID"] = digest
    r = await call(http, state, "orbit_deploy_status", {"project": "demo", "env": "prod"})
    assert not r.is_error, r.content
    web = next(c for c in r.structured_content["components"] if c["component"] == "web")
    assert web["running_images"] == ["mirror.invalid/app:tag-a"]
    assert web["drift"] is False
    assert digest in web["running_digests"]


async def test_deploy_status_drift_when_spec_tag_differs_from_desired(state, http):
    """A pod whose spec pins tag B while desired is A is real drift."""
    seed_catalog(state, [DEMO], tags={"demo/prod/web": "tag-a"})
    kube = state.backends.kube
    kube.add_pod(namespace="demo-prod", name="web-0", component="web",
                 image="mirror.invalid/app:tag-b", labels=labels("demo", "web"))
    r = await call(http, state, "orbit_deploy_status", {"project": "demo", "env": "prod"})
    assert not r.is_error, r.content
    web = next(c for c in r.structured_content["components"] if c["component"] == "web")
    assert web["running_images"] == ["mirror.invalid/app:tag-b"]
    assert web["drift"] is True


async def test_deploy_history_paginates_and_filters_to_the_component(state, http):
    seed_catalog(state, [DEMO])
    for i in range(25):
        commit_tags(state, {"demo/prod/web": f"tag{i:02d}", "demo/prod/worker": "same"},
                    f"tag web to tag{i:02d}")
    page = await call(http, state, "orbit_deploy_history",
                      {"project": "demo", "env": "prod", "component": "web", "limit": 20})
    assert not page.is_error, page.content
    body = page.structured_content
    assert body["returned"] == 20
    assert body["truncated"] is True
    assert body["next_cursor"]
    assert body["total"] == 25
    assert body["items"][0]["tag"] == "tag24"
    assert body["items"][0]["commit"]
    assert body["items"][0]["at"]
    rest = await call(http, state, "orbit_deploy_history", {
        "project": "demo", "env": "prod", "component": "web", "limit": 20,
        "cursor": body["next_cursor"],
    })
    assert rest.structured_content["returned"] == 5
    assert rest.structured_content["truncated"] is False
    assert rest.structured_content["items"][-1]["tag"] == "tag00"
    worker = await call(http, state, "orbit_deploy_history",
                        {"project": "demo", "env": "prod", "component": "worker"})
    assert [item["tag"] for item in worker.structured_content["items"]] == ["same"],         "the worker's tag was set once: one change, not one row for every ship of web"


async def test_another_projects_ships_are_not_in_a_projects_history(state, http):
    """On the test box (2026-10-12) the newest thirty entries of checkers-lab dev, in both
    history tools, were the ships of guestbook: every commit of the tags file was listed
    for every pair it still carried. An agent told to read the last ten entries for the
    tag to roll back to found one tag, ten times."""
    seed_catalog(state, [DEMO])
    commit_tags(state, {"demo/prod/web": "one", "demo/prod/worker": "w1"}, "deploy: demo one")
    commit_tags(state, {"demo/prod/web": "two", "demo/prod/worker": "w1"}, "deploy: demo two")
    for i in range(40):
        commit_tags(state, {"demo/prod/web": "two", "demo/prod/worker": "w1", "other/prod/web": f"o{i:02d}"},
                    f"deploy: other o{i:02d}")
    history = (await call(http, state, "orbit_deploy_history", {"project": "demo", "env": "prod"})).structured_content
    assert [i["subject"] for i in history["items"]] == ["deploy: demo two", "deploy: demo one"]
    assert history["items"][0]["tag"] == {"demo/prod/web": "two", "demo/prod/worker": "w1"}
    ledger = (await call(http, state, "orbit_ledger_get", {"project": "demo", "env": "prod", "limit": 10})).structured_content
    assert [i["subject"] for i in ledger["items"]] == ["deploy: demo two", "deploy: demo one"]
    assert [i["tags"]["demo/prod/web"] for i in ledger["items"]] == ["two", "one"]
    assert ledger["total"] == 2 and ledger["truncated"] is False
    other = (await call(http, state, "orbit_ledger_get", {"project": "other", "env": "prod", "limit": 100})).structured_content
    assert other["total"] == 40 and other["items"][0]["tags"] == {"other/prod/web": "o39"}


async def test_a_pair_taken_out_of_the_tags_file_and_put_back_is_two_changes(state, http):
    seed_catalog(state, [DEMO])
    commit_tags(state, {"demo/prod/web": "one"}, "deploy: one")
    commit_tags(state, {"other/prod/web": "x"}, "catalog: remove demo")
    commit_tags(state, {"demo/prod/web": "one", "other/prod/web": "x"}, "deploy: one again")
    ledger = (await call(http, state, "orbit_ledger_get", {"project": "demo", "env": "prod"})).structured_content
    assert [i["subject"] for i in ledger["items"]] == ["deploy: one again", "deploy: one"]


async def test_workload_list_paginates_at_100_and_describes_a_pod(state, http):
    seed_catalog(state, [DEMO])
    kube = state.backends.kube
    first = kube.add_pod(namespace="demo-prod", name="web-0", component="web",
                         image="mirror.invalid/app:abc1234", labels=labels("demo", "web"),
                         restarts=2, last_terminated="OOMKilled")
    first["spec"]["nodeName"] = "node-a"
    for i in range(1, 101):
        kube.add_pod(namespace="demo-prod", name=f"web-{i}", component="web",
                     labels=labels("demo", "web"))
    listed = await call(http, state, "orbit_workload_list", {"project": "demo", "env": "prod"})
    assert not listed.is_error, listed.content
    body = listed.structured_content
    assert body["returned"] == 100
    assert body["total"] == 101
    assert body["truncated"] is True
    assert body["next_cursor"] == "100"
    row = next(item for item in body["items"] if item["pod"] == "web-0")
    assert row["component"] == "web"
    assert row["phase"] == "Running"
    assert row["ready"] is True
    assert row["restarts"] == 2
    assert row["last_terminated_reason"] == "OOMKilled"
    assert row["image"] == "mirror.invalid/app:abc1234"
    assert row["node"] == "node-a"
    assert row["age"] is not None and row["age"] >= 0
    page2 = await call(http, state, "orbit_workload_list",
                       {"project": "demo", "env": "prod", "cursor": body["next_cursor"]})
    assert page2.structured_content["returned"] == 1
    assert page2.structured_content["truncated"] is False


async def test_workload_describe_returns_spec_pods_and_events(state, http):
    seed_catalog(state, [DEMO])
    kube = state.backends.kube
    kube.add_pod(namespace="demo-prod", name="web-0", component="web",
                 image="mirror.invalid/app:abc1234", labels=labels("demo", "web"))
    dep = kube.add_deployment(namespace="demo-prod", name="demo-web",
                              image="mirror.invalid/app:abc1234")
    container = dep["spec"]["template"]["spec"]["containers"][0]
    container["resources"] = {
        "requests": {"cpu": "100m", "memory": "128Mi"},
        "limits": {"memory": "256Mi"},
    }
    container["livenessProbe"] = {"httpGet": {"path": "/healthz", "port": 8080}}
    container["readinessProbe"] = {"httpGet": {"path": "/readyz", "port": 8080}}
    kube.add_event(namespace="demo-prod", name="e1", reason="Killing",
                   message="Container was OOMKilled", involved_name="web-0")
    kube.add_event(namespace="demo-prod", name="e2", reason="Pulled",
                   message="image pulled", involved_name="demo-web")
    kube.add_event(namespace="demo-prod", name="other", reason="Noise",
                   message="other component", involved_name="worker-0")

    r = await call(http, state, "orbit_workload_describe",
                   {"project": "demo", "env": "prod", "component": "web"})
    assert not r.is_error, r.content
    body = r.structured_content
    assert body["spec"]["image"] == "mirror.invalid/app:abc1234"
    assert body["spec"]["requests"]["memory"] == "128Mi"
    assert body["spec"]["limits"]["memory"] == "256Mi"
    probe_paths = {p["path"] for p in body["spec"]["probes"]}
    assert probe_paths == {"/healthz", "/readyz"}
    assert [p["pod"] for p in body["pods"]["items"]] == ["web-0"]
    assert body["pods"]["truncated"] is False
    reasons = {e["reason"] for e in body["events"]}
    assert reasons == {"Killing", "Pulled"}
    assert "Noise" not in reasons
    # metrics.k8s.io is empty on the fake: usage is omitted, not an error
    assert body["usage_vs_limit"] is None


async def test_db_status_reads_cnpg_cluster_and_not_found_without_database(state, http):
    seed_catalog(state, [DEMO])
    missing = await call(http, state, "orbit_db_status", {"project": "demo", "env": "prod"})
    assert missing.is_error
    assert missing.structured_content["code"] == "not_found"

    seed_catalog(state, [DEMO_DB])
    cluster = state.backends.kube.add_cluster(namespace="demo-prod", name="demo-db")
    cluster["status"].update({
        "currentPrimary": "demo-db-1",
        "firstRecoverabilityPoint": "2026-01-01T00:00:00Z",
        "lastSuccessfulBackup": "2026-01-02T00:00:00Z",
        "conditions": [{"type": "ContinuousArchiving", "status": "True"}],
    })
    r = await call(http, state, "orbit_db_status", {"project": "demo", "env": "prod"})
    assert not r.is_error, r.content
    body = r.structured_content
    assert body["cluster"] == "demo-db"
    assert body["phase"] == "Cluster in healthy state"
    assert body["instances"] == 1 and body["ready"] == 1
    assert body["primary"] == "demo-db-1"
    assert body["first_recoverability_point"] == "2026-01-01T00:00:00Z"
    assert body["last_successful_backup"] == "2026-01-02T00:00:00Z"
    assert body["wal_archive"] == "True"


async def test_unknown_project_is_not_found_on_cluster_tools(state, http):
    seed_catalog(state, [DEMO])
    for name, args in (
        ("orbit_deploy_status", {"project": "missing", "env": "prod"}),
        ("orbit_deploy_history", {"project": "missing", "env": "prod"}),
        ("orbit_workload_list", {"project": "missing", "env": "prod"}),
        ("orbit_workload_describe", {"project": "missing", "env": "prod", "component": "web"}),
        ("orbit_db_status", {"project": "missing", "env": "prod"}),
        ("orbit_workload_describe", {"project": "demo", "env": "prod", "component": "nope"}),
    ):
        r = await call(http, state, name, args)
        assert r.is_error, name
        assert r.structured_content["code"] == "not_found", name


async def test_unconfigured_kube_is_actionable_failure(state, http):
    seed_catalog(state, [DEMO])
    state.backends.kube = UnconfiguredKube()
    r = await call(http, state, "orbit_workload_list", {"project": "demo", "env": "prod"})
    assert r.is_error
    assert r.structured_content["code"] == "backend_unconfigured"
    assert r.structured_content["evidence"]["variable"] == "ORBITD_KUBECONFIG"
    assert r.structured_content["retryable"] is False


async def test_job_status_and_wait_over_the_in_process_registry(state, http):
    done = state.jobs.create(status="completed", result={"ok": True})
    status = await call(http, state, "orbit_job_status", {"job_id": done.id})
    assert not status.is_error, status.content
    assert status.structured_content["job_id"] == done.id
    assert status.structured_content["status"] == "completed"
    assert status.structured_content["result"] == {"ok": True}
    waited = await call(http, state, "orbit_job_wait", {"job_id": done.id, "timeout_s": 5})
    assert waited.structured_content["status"] == "completed"

    pending = state.jobs.create(status="pending")
    still = await call(http, state, "orbit_job_wait", {"job_id": pending.id, "timeout_s": 0})
    assert still.structured_content["status"] == "pending"
    assert still.structured_content["poll_after_ms"] == 1000

    missing = await call(http, state, "orbit_job_status", {"job_id": "missingjobid"})
    assert missing.is_error
    assert missing.structured_content["code"] == "not_found"


@pytest.mark.parametrize("role", [Role.viewer, Role.admin])
async def test_viewer_and_admin_may_call_every_kube_viewer_tool(state, http, role):
    seed_catalog(state, [DEMO_DB], tags={"demo/prod/web": "abc1234", "demo/prod/worker": "abc1234"})
    kube = state.backends.kube
    kube.add_pod(namespace="demo-prod", name="web-0", component="web",
                 image="mirror.invalid/app:abc1234", labels=labels("demo", "web"))
    kube.add_deployment(namespace="demo-prod", name="demo-web")
    kube.add_cluster(namespace="demo-prod", name="demo-db")
    job = state.jobs.create(status="completed")
    calls = [
        ("orbit_project_list", {}),
        ("orbit_catalog_get", {}),
        ("orbit_catalog_get", {"project": "demo"}),
        ("orbit_deploy_status", {"project": "demo", "env": "prod"}),
        ("orbit_deploy_history", {"project": "demo", "env": "prod"}),
        ("orbit_workload_list", {"project": "demo", "env": "prod"}),
        ("orbit_workload_describe", {"project": "demo", "env": "prod", "component": "web"}),
        ("orbit_db_status", {"project": "demo", "env": "prod"}),
        ("orbit_job_status", {"job_id": job.id}),
        ("orbit_job_wait", {"job_id": job.id, "timeout_s": 0}),
    ]
    async with sdk_client(http, mint(state, role)) as client:
        listed = {t.name for t in (await client.list_tools()).tools}
        for name in VIEWER_TOOLS:
            assert name in listed, name
        for name, args in calls:
            r = await client.call_tool(name, args)
            assert not r.is_error, (name, r.structured_content)
            code = (r.structured_content or {}).get("code")
            assert code != "FORBIDDEN", name


CRON_DEMO = {
    "id": "demo",
    "name": "Demo",
    "environments": {"prod": {"branch": "main"}},
    "components": [{"name": "bot", "kind": "cron", "schedule": "*/10 * * * *"}],
}


def _cronjob(image: str) -> dict:
    return {"apiVersion": "batch/v1", "kind": "CronJob",
            "metadata": {"name": "demo-bot", "namespace": "demo-prod"},
            "spec": {"schedule": "*/10 * * * *", "jobTemplate": {"spec": {"template": {"spec": {
                "containers": [{"name": "bot", "image": image}]}}}}}}


async def test_a_cron_component_between_runs_is_not_drift(state, http):
    """docs/V1.md §8: a scheduled job has a pod only while a run lasts, so 'no running
    image' read as drift on every cron component. Drift is judged on the CronJob."""
    seed_catalog(state, [CRON_DEMO], tags={"demo/prod/bot": "main-1234567"})
    state.backends.kube.objects.append(_cronjob("mirror.invalid/bot:main-1234567"))
    r = await call(http, state, "orbit_deploy_status", {"project": "demo", "env": "prod"})
    assert not r.is_error, r.content
    bot = r.structured_content["components"][0]
    assert bot["kind"] == "cron"
    assert bot["running_images"] == []
    assert bot["template_images"] == ["mirror.invalid/bot:main-1234567"]
    assert bot["drift"] is False


async def test_a_cronjob_on_another_tag_is_drift(state, http):
    seed_catalog(state, [CRON_DEMO], tags={"demo/prod/bot": "main-1234567"})
    state.backends.kube.objects.append(_cronjob("mirror.invalid/bot:main-0000000"))
    r = await call(http, state, "orbit_deploy_status", {"project": "demo", "env": "prod"})
    assert r.structured_content["components"][0]["drift"] is True
