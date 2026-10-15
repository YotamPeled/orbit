"""Operator deploy tools over fakes: no network, no subprocess."""

from __future__ import annotations

import copy
from dataclasses import replace

import pytest
from conftest import mint, sdk_client
from mcp_types import InputRequiredResult

from orbitd import applier
from orbitd import deploy as deploy_module
from orbitd.deploy import DeployRuntime, RESTART_ANN, SHIP_SCRIPT
from orbitd.roles import CheckoutSource, Role
from orbitd.testing import FakeClock, FakeDigest, FakeLedger, FakeRunner
from orbitd.tools_viewer.common import jobs_of

from test_tools_viewer_kube import labels, seed_catalog

pytestmark = pytest.mark.anyio

PROJECT = "demo"
ENV = "prod"
WORKER = "worker"
TAG = "ship-1"
PREV = "ship-0"
IMAGE = f"mirror.invalid/demo-worker:{TAG}"
POD = "demo-worker-6b8f9c7d5-xk2lm"
CREATED = "2026-01-01T00:00:00Z"
COMMIT = "0123456789abcdef0123456789abcdef01234567"
DIGEST = "sha256:" + "ab" * 32
SINCE = 1_710_000_000

CATALOG = {
    "id": PROJECT,
    "environments": {
        "prod": {"branch": "main"},
        "dev": {"enabled": True, "branch": "dev"},
    },
    "components": [
        {"name": "web", "kind": "web", "image": "mirror.invalid/demo-web"},
        {"name": WORKER, "kind": "worker", "image": "mirror.invalid/demo-worker"},
    ],
}


def _loki(pod: str = POD, ts: str = "1710000000000000000") -> dict:
    return {
        "status": "success",
        "data": {
            "resultType": "streams",
            "result": [{"stream": {"pod": pod, "namespace": f"{PROJECT}-{ENV}"},
                        "values": [[ts, "listening"]]}],
        },
    }


def install(state, *, runner=None, digest=None, ledger=None):
    clock = FakeClock(t=float(SINCE))
    rt = DeployRuntime(
        runner=runner or FakeRunner(commit=COMMIT),
        digest=digest or FakeDigest(DIGEST),
        ledger=ledger or FakeLedger(),
        clock=clock.now,
        sleeper=clock.sleep,
        http_get=lambda url: 200,
        rollout_timeout_s=5,
        assert_timeout_s=5,
    )
    state.deploy = rt
    return rt, clock


def seed_world(state, *, tag=TAG, image=IMAGE, env=ENV, deploy_image=None):
    seed_catalog(state, [CATALOG], tags={f"{PROJECT}/{env}/{WORKER}": tag})
    kube = state.backends.kube
    namespace = f"{PROJECT}-{env}"
    pod = kube.add_pod(
        namespace=namespace, name=POD, component=WORKER, image=image,
        labels=labels(PROJECT, WORKER),
    )
    pod["metadata"]["creationTimestamp"] = CREATED
    pod["spec"]["containers"][0]["readinessProbe"] = {"periodSeconds": 5}
    pod["status"]["conditions"] = [{"type": "Ready", "status": "True"}]
    dep = kube.add_deployment(
        namespace=namespace, name=f"{PROJECT}-{WORKER}",
        replicas=1, ready=1, image=deploy_image or image,
    )
    dep["metadata"]["generation"] = 1
    dep["status"].update({
        "observedGeneration": 1, "updatedReplicas": 1, "readyReplicas": 1,
        "replicas": 1, "availableReplicas": 1,
    })
    state.backends.loki.responses["/loki/api/v1/query_range"] = _loki()
    return kube


async def wait_result(client, running):
    assert not running.is_error, running.content
    body = running.structured_content
    assert body["status"] == "running"
    job_id = body["evidence"]["job_id"]
    waited = await client.call_tool("orbit_job_wait", {"job_id": job_id, "timeout_s": 5})
    assert not waited.is_error, waited.content
    return waited.structured_content["result"], job_id


def apply_on(state) -> None:
    """Flip the hard switch on; tests below seed the render and checkout fakes."""
    state.cfg = replace(state.cfg, apply_mode="on")


def use_apply_fakes(monkeypatch) -> None:
    """The render and checkout a real apply needs: two objects in the pair's
    namespace, and a checkout already carrying the bump commit."""
    namespace = f"{PROJECT}-{ENV}"
    objects = [
        {"apiVersion": "v1", "kind": "ConfigMap",
         "metadata": {"name": "app-config", "namespace": namespace},
         "data": {"k": "v"}},
        {"apiVersion": "v1", "kind": "ServiceAccount",
         "metadata": {"name": "demo", "namespace": namespace}},
    ]
    monkeypatch.setattr(
        applier, "render_desired",
        lambda state, p, e: (copy.deepcopy(objects), "e" * 64))

    class _FakeSource:
        def __init__(self) -> None:
            self.calls = 0

        def __call__(self, state, *, now=None) -> CheckoutSource:
            self.calls += 1
            return CheckoutSource(commit=COMMIT, age_s=0, lag_note="test")

    monkeypatch.setattr(deploy_module, "read_source", _FakeSource())


async def test_confirmation_round(fake_backends, http):
    install(fake_backends)
    seed_world(fake_backends)
    async with sdk_client(http, mint(fake_backends, Role.operator)) as client:
        first = await client.session.call_tool(
            "orbit_deploy_ship",
            {"project": PROJECT, "env": ENV, "components": [WORKER], "tag": TAG},
            allow_input_required=True,
        )
    assert isinstance(first, InputRequiredResult)
    assert list(first.input_requests) == ["confirm"]
    assert TAG in first.input_requests["confirm"].params.message


async def test_dry_run_plan(fake_backends, http):
    install(fake_backends)
    seed_world(fake_backends)
    async with sdk_client(http, mint(fake_backends, Role.operator)) as client:
        r = await client.call_tool("orbit_deploy_ship", {
            "project": PROJECT, "env": ENV, "components": [WORKER], "tag": TAG,
            "dry_run": True,
        })
    assert not r.is_error, r.content
    body = r.structured_content
    assert body["status"] == "dry_run"
    assert [s["name"] for s in body["steps"]][:4] == ["confirm", "catalog", "registry", "bump"]
    assert {s["name"] for s in body["steps"]} >= {"confirm", "catalog", "registry", "bump",
                                                  "rollout", "image", "ready", "log"}
    assert fake_backends.deploy.runner.calls == []


async def test_refused(fake_backends, http):
    rt, _ = install(fake_backends)
    seed_world(fake_backends)
    async with sdk_client(http, mint(fake_backends, Role.operator), confirm=None) as client:
        r = await client.call_tool("orbit_deploy_ship", {
            "project": PROJECT, "env": ENV, "components": [WORKER], "tag": TAG,
        })
    assert r.structured_content["status"] == "refused"
    assert rt.runner.calls == []


async def test_unknown_component(fake_backends, http):
    install(fake_backends)
    seed_world(fake_backends)
    async with sdk_client(http, mint(fake_backends, Role.operator), confirm=True) as client:
        r = await client.call_tool("orbit_deploy_ship", {
            "project": PROJECT, "env": ENV, "components": ["ghost"], "tag": TAG,
        })
    assert not r.is_error
    body = r.structured_content
    assert body["status"] == "failed"
    assert body["steps"][0]["name"] == "catalog"
    assert body["steps"][0]["status"] == "failed"
    assert fake_backends.deploy.runner.calls == []


async def test_expect_mismatch(fake_backends, http):
    rt, _ = install(fake_backends)
    seed_world(fake_backends)
    async with sdk_client(http, mint(fake_backends, Role.operator), confirm=True) as client:
        running = await client.call_tool("orbit_deploy_ship", {
            "project": PROJECT, "env": ENV, "components": [WORKER], "tag": TAG,
            "expect": "sha256:deadbeef",
        })
        result, _ = await wait_result(client, running)
    assert result["status"] == "failed"
    by_name = {s["name"]: s for s in result["steps"]}
    assert by_name["registry"]["status"] == "failed"
    assert "sha256:deadbeef" in (by_name["registry"]["detail"] or "")
    assert rt.runner.calls == []


async def test_busy_lock(fake_backends, http):
    rt, _ = install(fake_backends)
    seed_world(fake_backends)
    held = rt.locks.try_acquire(PROJECT, ENV, "alreadyrunningjob")
    assert held is None
    async with sdk_client(http, mint(fake_backends, Role.operator), confirm=True) as client:
        r = await client.call_tool("orbit_deploy_ship", {
            "project": PROJECT, "env": ENV, "components": [WORKER], "tag": TAG,
        })
    assert r.is_error
    assert r.structured_content["code"] == "busy"
    assert r.structured_content["evidence"]["job_id"] == "alreadyrunningjob"
    assert rt.runner.calls == []


async def test_ship_happy_path(fake_backends, http):
    """The switch-off ship over the protocol: the bump lands, the apply step is
    skipped, and the job is refused — never `applied` with `DEPLOYED` when this
    orbitd wrote no object. Nothing to roll out or assert behind a skip."""
    rt, _ = install(fake_backends)
    seed_world(fake_backends)
    async with sdk_client(http, mint(fake_backends, Role.operator, "desk-op"), confirm=True) as client:
        running = await client.call_tool("orbit_deploy_ship", {
            "project": PROJECT, "env": ENV, "components": [WORKER], "tag": TAG,
        })
        result, job_id = await wait_result(client, running)
    assert result["status"] == "refused", result
    assert result["commit"] == COMMIT
    assert result["evidence"]["digest"] == DIGEST
    assert result["evidence"]["verdict"] == "LANDED NOT DEPLOYED apply"
    names = [s["name"] for s in result["steps"]]
    assert names == ["confirm", "catalog", "registry", "bump", "apply"]
    apply = next(s for s in result["steps"] if s["name"] == "apply")
    assert apply["status"] == "skipped"
    assert apply["detail"] == "ORBITD_APPLY=off: apply is disabled on this orbitd; set ORBITD_APPLY=on"
    assert "ORBITD_APPLY" in apply["detail"]
    assert result["evidence"]["components"] == [{"name": WORKER, "stage": "refused", "commit": COMMIT}]
    assert rt.runner.calls == [{
        "argv": [SHIP_SCRIPT, PROJECT, ENV, WORKER, TAG],
        "env": {"ORBIT_ACTION": "ship", "ORBIT_ACTOR": "desk-op"},
    }]
    job = jobs_of(fake_backends).get(job_id)
    assert job is not None and job.status == "failed"


async def test_failing_assertion(fake_backends, http, monkeypatch):
    """A ship whose pods never carry the tag fails the image assertion. The switch
    is on so the ship reaches the assertions it is here to break."""
    apply_on(fake_backends)
    use_apply_fakes(monkeypatch)
    rt, _ = install(fake_backends)
    seed_world(fake_backends, image="mirror.invalid/demo-worker:old",
               deploy_image=IMAGE)
    async with sdk_client(http, mint(fake_backends, Role.operator), confirm=True) as client:
        running = await client.call_tool("orbit_deploy_ship", {
            "project": PROJECT, "env": ENV, "components": [WORKER], "tag": TAG,
        })
        result, job_id = await wait_result(client, running)
    assert result["status"] == "failed"
    assert result["evidence"]["verdict"].startswith("LANDED NOT DEPLOYED")
    by_name = {s["name"]: s for s in result["steps"]}
    assert by_name["image"]["status"] == "failed"
    assert rt.runner.calls  # bump ran; assertion failed after
    job = jobs_of(fake_backends).get(job_id)
    assert job is not None and job.status == "failed"


async def test_rollback_picks_previous_tag(fake_backends, http):
    """Rollback resolves the previous ledger tag. The switch is off so the apply
    is skipped and the job is refused — the tag choice is proven by the bump call."""
    ledger = FakeLedger({f"{PROJECT}/{ENV}/{WORKER}": [TAG, PREV]})
    rt, _ = install(fake_backends, ledger=ledger)
    seed_world(fake_backends, tag=PREV, image=f"mirror.invalid/demo-worker:{PREV}")
    async with sdk_client(http, mint(fake_backends, Role.operator, "desk-op"), confirm=True) as client:
        running = await client.call_tool("orbit_deploy_rollback", {
            "project": PROJECT, "env": ENV, "component": WORKER,
        })
        result, _ = await wait_result(client, running)
    assert result["status"] == "refused", result
    assert result["evidence"]["verdict"] == "LANDED NOT DEPLOYED apply"
    assert rt.runner.calls == [{
        "argv": [SHIP_SCRIPT, PROJECT, ENV, WORKER, PREV],
        "env": {"ORBIT_ACTION": "rollback", "ORBIT_ACTOR": "desk-op"},
    }]


async def test_rollback_refuses_with_none(fake_backends, http):
    install(fake_backends, ledger=FakeLedger())
    seed_world(fake_backends)
    async with sdk_client(http, mint(fake_backends, Role.operator), confirm=True) as client:
        r = await client.call_tool("orbit_deploy_rollback", {
            "project": PROJECT, "env": ENV, "component": WORKER,
        })
    assert r.is_error
    assert r.structured_content["code"] == "no_previous_tag"
    assert fake_backends.deploy.runner.calls == []


async def test_restart_patches_annotation_and_asserts(fake_backends, http, monkeypatch):
    """Restart patches the pod-template annotation, applies, rolls out and asserts.
    The switch is on so the job reaches the patch it is here to check."""
    apply_on(fake_backends)
    use_apply_fakes(monkeypatch)
    rt, _ = install(fake_backends)
    kube = seed_world(fake_backends)
    async with sdk_client(http, mint(fake_backends, Role.operator), confirm=True) as client:
        running = await client.call_tool("orbit_workload_restart", {
            "project": PROJECT, "env": ENV, "component": WORKER,
        })
        result, _ = await wait_result(client, running)
    assert result["status"] == "applied", result
    assert result["evidence"]["verdict"] == "DEPLOYED"
    assert kube.patches
    patch = kube.patches[-1]
    assert patch["name"] == f"{PROJECT}-{WORKER}"
    assert patch["namespace"] == f"{PROJECT}-{ENV}"
    ann = patch["body"]["spec"]["template"]["metadata"]["annotations"]
    assert RESTART_ANN in ann
    assert "T" in ann[RESTART_ANN]
    dep = kube.deployments[0]
    stored = dep["spec"]["template"]["metadata"]["annotations"][RESTART_ANN]
    assert stored == ann[RESTART_ANN]
    names = [s["name"] for s in result["steps"]]
    assert names[0:3] == ["confirm", "catalog", "restart"]
    assert rt.runner.calls == []


async def test_busy_behind_the_reconcile_loop_names_no_job(fake_backends, http):
    """docs/V1.md §8 (S04): the reconcile loop holds the pair lock under the holder name
    `reconcile-loop`, which is no job; busy told the agent to orbit_job_wait on it."""
    rt, _ = install(fake_backends)
    seed_world(fake_backends)
    assert rt.locks.try_acquire(PROJECT, ENV, "reconcile-loop") is None
    async with sdk_client(http, mint(fake_backends, Role.operator), confirm=True) as client:
        r = await client.call_tool("orbit_deploy_ship", {
            "project": PROJECT, "env": ENV, "components": [WORKER], "tag": TAG,
        })
    assert r.is_error and r.structured_content["code"] == "busy"
    assert "job_id" not in r.structured_content["evidence"]
    assert r.structured_content["evidence"]["holder"] == "reconcile"
    assert "orbit_job_wait can" not in r.structured_content["next_action"].replace("not a job orbit_job_wait can", "")
    assert "retry" in r.structured_content["next_action"]
    assert rt.runner.calls == []


async def test_nothing_is_shipped_onto_a_main_the_gate_refused(fake_backends, http, monkeypatch):
    """QA of v1.0.0, F26: with main's newest commit refused, a ship wrote its bump on top,
    waited, and ended `LANDED NOT DEPLOYED apply`. It is refused before any write now,
    for ship and for rollback, and says how to get out."""
    from orbitd import gate as gate_module

    class Refused:
        enabled, refused, blocks, undecided = True, True, True, False
        head, good, check = "f" * 40, "a" * 40, "fit"
        sentence = "the catalog reserves more than the node has."

    rt, _ = install(fake_backends)
    seed_world(fake_backends)
    monkeypatch.setattr(gate_module.CommitGate, "evaluate", lambda self: Refused())
    async with sdk_client(http, mint(fake_backends, Role.operator), confirm=True) as client:
        ship = await client.call_tool("orbit_deploy_ship", {
            "project": PROJECT, "env": ENV, "components": [WORKER], "tag": TAG})
        back = await client.call_tool("orbit_deploy_rollback", {
            "project": PROJECT, "env": ENV, "component": WORKER, "to": TAG})
    for r in (ship, back):
        assert r.is_error
        body = r.structured_content
        assert body["code"] == "main_refused" and body["retryable"] is True
        assert "ffffffffffff" in body["what_failed"] and "check fit" in body["what_failed"]
        assert "orbit_catalog_set" in body["next_action"]
    assert rt.runner.calls == []


async def test_a_scheduled_job_cannot_be_restarted_and_says_why(fake_backends, http):
    """QA of v1.0.0, F36: restarting a cron component answered "restart failed |
    kubernetes request failed". There is nothing to restart, and that is the answer."""
    rt, _ = install(fake_backends)
    seed_world(fake_backends)
    catalog = copy.deepcopy(CATALOG)
    catalog["components"].append({"name": "janitor", "kind": "cron", "schedule": "0 * * * *",
                                  "image": "mirror.invalid/demo-janitor"})
    seed_catalog(fake_backends, [catalog], tags={f"{PROJECT}/{ENV}/janitor": TAG})
    async with sdk_client(http, mint(fake_backends, Role.operator), confirm=True) as client:
        r = await client.call_tool("orbit_workload_restart", {"project": PROJECT, "env": ENV, "component": "janitor"})
        dry = await client.call_tool("orbit_workload_restart", {"project": PROJECT, "env": ENV, "component": "janitor",
                                                                "dry_run": True})
    for result in (r, dry):
        body = result.structured_content
        assert result.is_error and body["code"] == "not_restartable"
        assert "scheduled job" in body["what_failed"] and "'0 * * * *'" in body["what_failed"]
        assert "next scheduled run" in body["next_action"]
    assert not rt.locks.expired(PROJECT, ENV) and rt.locks.try_acquire(PROJECT, ENV, "x") is None
