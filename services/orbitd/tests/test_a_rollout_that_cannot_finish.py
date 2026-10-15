"""A new version that will never become ready (QA of v1.0.0, F16, F17, F18).

Shipped on the test box: an image that crashes at start. The ship waited out its whole
ten-minute rollout timeout and said only "timeout", though the pod had been in
CrashLoopBackOff since its first half minute; orbit_deploy_status said `drift: false`
all the while; and for those ten minutes a rollback, a restart and another ship were all
refused `busy`, with nothing to stop the wait.

Here: the wait ends at the third crash and says so in the kubelet's words; a timeout
names what the new pod was doing; the status calls the rollout `stuck` and the pair
drifted; and orbit_job_cancel stops a ship that is only waiting."""

from __future__ import annotations

import threading
import time

import pytest
from conftest import mint, sdk_client

from orbitd import deploy
from orbitd.roles import Role
from orbitd.tools_viewer.common import pod_trouble

from test_tools_operator import (
    ENV, IMAGE, POD, PROJECT, TAG, WORKER, install, seed_world,
)
from test_tools_viewer_kube import labels, seed_catalog

pytestmark = pytest.mark.anyio
NEW = "ship-2"
NEW_IMAGE = f"mirror.invalid/demo-worker:{NEW}"
NEW_POD = "demo-worker-7c9d8e6f4-new01"


def rolling(state, *, reason="CrashLoopBackOff", restarts=3, running_not_ready=False):
    """The deployment mid-roll to NEW: the old pod serving, one new pod in trouble."""
    kube = seed_world(state)
    dep = kube.deployments[0]
    dep["spec"]["template"]["spec"]["containers"][0]["image"] = NEW_IMAGE
    dep["metadata"]["generation"] = 2
    dep["status"].update({"observedGeneration": 2, "updatedReplicas": 1, "readyReplicas": 1,
                          "replicas": 2, "availableReplicas": 1})
    pod = kube.add_pod(namespace=f"{PROJECT}-{ENV}", name=NEW_POD, component=WORKER, image=NEW_IMAGE,
                       labels=labels(PROJECT, WORKER), phase=reason, restarts=restarts,
                       last_terminated="Error" if reason == "CrashLoopBackOff" else None)
    if reason == "CrashLoopBackOff":
        pod["status"]["containerStatuses"][0]["lastState"]["terminated"]["exitCode"] = 1
    pod["status"]["phase"] = "Running" if reason == "CrashLoopBackOff" or running_not_ready else "Pending"
    if running_not_ready:
        pod["status"]["containerStatuses"][0].update(
            {"ready": False, "state": {"running": {"startedAt": "2026-01-01T00:00:00Z"}}})
    return kube, pod


# ---------------------------------------------------------------- what a pod says is wrong

def test_a_pod_says_what_is_wrong_with_it_in_the_kubelets_words(fake_backends):
    kube, crashing = rolling(fake_backends)
    trouble = pod_trouble(crashing)
    assert trouble["stuck"] is True and trouble["reason"] == "CrashLoopBackOff" and trouble["restarts"] == 3
    assert trouble["sentence"] == (f"pod {NEW_POD}: container {WORKER} is in CrashLoopBackOff after 3 restart(s), "
                                   f"last exit Error (exit code 1)")
    assert pod_trouble(kube.pods[0]) is None, "a ready pod has no trouble"


def test_a_crash_loop_is_one_at_every_instant_between_two_crashes(fake_backends):
    """On the test box (v1.0.1-rc1) the status of a pod on its third crash, read between
    two crashes, was "waiting (Waiting)" and its rollout "progressing". Between crashes a
    container is `terminated`, then `waiting`, then `running` for the seconds it lasts;
    what it has done (restarts after failing exits) is what makes it a crash loop."""
    _, pod = rolling(fake_backends)
    cs = pod["status"]["containerStatuses"][0]
    last = {"terminated": {"reason": "Error", "exitCode": 3}}
    for state in ({"terminated": {"reason": "Error", "exitCode": 3}},          # just exited
                  {"waiting": {"reason": "CrashLoopBackOff"}},                 # backing off
                  {"waiting": {}},                                             # about to start
                  {"running": {"startedAt": "2026-01-01T00:00:00Z"}}):         # up for a moment
        cs.update({"ready": False, "restartCount": 3, "state": state, "lastState": last})
        trouble = pod_trouble(pod)
        assert trouble["stuck"] is True and trouble["reason"] == "CrashLoopBackOff", (state, trouble)
        assert "3 restart(s)" in trouble["sentence"] and "exit code 3" in trouble["sentence"]
    # one failing exit is a crash, not yet a loop
    cs.update({"restartCount": 1, "state": {"waiting": {}}})
    first = pod_trouble(pod)
    assert first["stuck"] is False and first["reason"] == "Crashing" and "keeps crashing" in first["sentence"]
    # a container that exited cleanly and restarts is not crashing
    cs.update({"restartCount": 4, "state": {"running": {"startedAt": "2026-01-01T00:00:00Z"}},
               "lastState": {"terminated": {"reason": "Completed", "exitCode": 0}}})
    clean = pod_trouble(pod)
    assert clean["stuck"] is False and clean["reason"] == "NotReady"


def test_a_pod_that_runs_and_is_not_ready_is_slow_not_stuck(fake_backends):
    _, slow = rolling(fake_backends, reason="Running", restarts=0, running_not_ready=True)
    trouble = pod_trouble(slow)
    assert trouble["stuck"] is False and trouble["reason"] == "NotReady"
    assert "running but not ready" in trouble["sentence"]


# ---------------------------------------------------------------- the wait

def test_a_crash_loop_ends_the_wait_at_once_and_is_named(fake_backends):
    rt, clock = install(fake_backends)
    rt.rollout_timeout_s = 600
    rolling(fake_backends)
    started = clock.now()
    step = deploy.wait_rollout(fake_backends, PROJECT, ENV, WORKER, tag=NEW, repository="mirror.invalid/demo-worker")
    assert step.status == "failed"
    assert clock.now() - started < 30, "a crash loop was waited out"
    assert step.detail.startswith("the new version crashes at start: ")
    assert "CrashLoopBackOff after 3 restart(s), last exit Error (exit code 1)" in step.detail
    assert "orbit_deploy_rollback" in step.detail and "keeps serving" in step.detail


def test_a_first_crash_is_not_yet_a_crash_loop(fake_backends):
    """One restart can be a container that came up before what it needs; the wait goes on."""
    rt, clock = install(fake_backends)
    rt.rollout_timeout_s = 40
    rolling(fake_backends, restarts=1)
    step = deploy.wait_rollout(fake_backends, PROJECT, ENV, WORKER, tag=NEW, repository="mirror.invalid/demo-worker")
    assert step.status == "failed" and step.detail.startswith("timeout: ")
    assert "CrashLoopBackOff after 1 restart(s)" in step.detail, "the timeout says what the new pod was doing"


def test_an_image_that_cannot_be_pulled_ends_the_wait_once_it_has_stayed_so(fake_backends):
    rt, clock = install(fake_backends)
    rt.rollout_timeout_s = 600
    rolling(fake_backends, reason="ImagePullBackOff", restarts=0)
    started = clock.now()
    step = deploy.wait_rollout(fake_backends, PROJECT, ENV, WORKER, tag=NEW, repository="mirror.invalid/demo-worker")
    waited = clock.now() - started
    assert step.status == "failed" and step.detail.startswith("the new version's image cannot be pulled: ")
    assert deploy.STUCK_GRACE_S <= waited < deploy.STUCK_GRACE_S + 30, waited


def test_a_version_that_is_only_slow_is_waited_for_and_the_timeout_says_what_it_saw(fake_backends):
    rt, clock = install(fake_backends)
    rt.rollout_timeout_s = 120
    rolling(fake_backends, reason="Running", restarts=0, running_not_ready=True)
    started = clock.now()
    step = deploy.wait_rollout(fake_backends, PROJECT, ENV, WORKER, tag=NEW, repository="mirror.invalid/demo-worker")
    assert clock.now() - started >= 120, "a pod that is only not ready yet must get its whole wait"
    assert step.status == "failed" and step.detail.startswith("timeout: ")
    assert "running but not ready" in step.detail


def test_a_rollout_that_finishes_is_not_disturbed_by_the_old_pods_trouble(fake_backends):
    """Only pods of the NEW tag are looked at: an old pod crash-looping is why one ships."""
    rt, clock = install(fake_backends)
    kube = seed_world(fake_backends)
    kube.pods[0]["status"]["containerStatuses"][0].update(
        {"ready": False, "restartCount": 9, "state": {"waiting": {"reason": "CrashLoopBackOff"}}})
    assert deploy.new_pod_troubles(fake_backends, PROJECT, ENV, WORKER, tag=NEW) == []
    step = deploy.wait_rollout(fake_backends, PROJECT, ENV, WORKER, tag=TAG)
    assert step.status == "ok"


def test_a_restart_is_not_failed_for_the_crash_loop_it_was_asked_to_cure(fake_backends):
    """A restart keeps the tag, so the pod being replaced carries it too. That pod
    crash-looping is why the restart was asked for: it is not the rollout failing. Only
    pods created since the job began are the rollout's own."""
    from datetime import datetime, timezone
    rt, clock = install(fake_backends)
    rt.rollout_timeout_s = 30
    kube = seed_world(fake_backends)
    began = clock.now()
    old = kube.pods[0]
    old["metadata"]["creationTimestamp"] = datetime.fromtimestamp(began - 3600, tz=timezone.utc).isoformat()
    old["status"]["containerStatuses"][0].update(
        {"ready": False, "restartCount": 9, "state": {"waiting": {"reason": "CrashLoopBackOff"}}})
    dep = kube.deployments[0]
    dep["status"].update({"updatedReplicas": 1, "readyReplicas": 0, "replicas": 2, "availableReplicas": 0})
    assert deploy.new_pod_troubles(fake_backends, PROJECT, ENV, WORKER, tag=TAG, since=began) == []
    step = deploy.wait_rollout(fake_backends, PROJECT, ENV, WORKER, tag=TAG, since=began)
    assert step.status == "failed" and step.detail.startswith("timeout: "), step.detail
    assert "crashes at start" not in step.detail
    # the pod the restart created, crash-looping in its turn, IS the rollout failing
    new = kube.add_pod(namespace=f"{PROJECT}-{ENV}", name=NEW_POD, component=WORKER, image=IMAGE,
                       labels=labels(PROJECT, WORKER), phase="CrashLoopBackOff", restarts=3, last_terminated="Error")
    new["metadata"]["creationTimestamp"] = datetime.fromtimestamp(began + 4, tz=timezone.utc).isoformat()
    step = deploy.wait_rollout(fake_backends, PROJECT, ENV, WORKER, tag=TAG, since=began)
    assert step.status == "failed" and step.detail.startswith("the new version crashes at start: ")
    assert NEW_POD in step.detail


# ---------------------------------------------------------------- the status

async def test_the_status_calls_a_crash_looping_rollout_stuck_and_the_pair_drifted(fake_backends, http):
    rolling(fake_backends)
    seed_catalog(fake_backends, [__import__("test_tools_operator").CATALOG], tags={f"{PROJECT}/{ENV}/{WORKER}": NEW})
    async with sdk_client(http, mint(fake_backends, Role.viewer)) as client:
        r = await client.call_tool("orbit_deploy_status", {"project": PROJECT, "env": ENV})
    row = {c["component"]: c for c in r.structured_content["components"]}[WORKER]
    assert row["running_images"] == [IMAGE, NEW_IMAGE]
    assert row["drift"] is True, "the old version still serves beside a new one that cannot start"
    assert row["rollout"] == "stuck"
    assert "CrashLoopBackOff" in row["rollout_detail"] and NEW_POD in row["rollout_detail"]


async def test_the_status_of_a_settled_pair_is_complete_and_not_drifted(fake_backends, http):
    seed_world(fake_backends)
    async with sdk_client(http, mint(fake_backends, Role.viewer)) as client:
        r = await client.call_tool("orbit_deploy_status", {"project": PROJECT, "env": ENV})
    row = {c["component"]: c for c in r.structured_content["components"]}[WORKER]
    assert row["drift"] is False and row["rollout"] == "complete" and row["rollout_detail"] is None


async def test_the_status_of_a_rollout_under_way_is_progressing(fake_backends, http):
    rolling(fake_backends, reason="Running", restarts=0, running_not_ready=True)
    seed_catalog(fake_backends, [__import__("test_tools_operator").CATALOG], tags={f"{PROJECT}/{ENV}/{WORKER}": NEW})
    async with sdk_client(http, mint(fake_backends, Role.viewer)) as client:
        r = await client.call_tool("orbit_deploy_status", {"project": PROJECT, "env": ENV})
    row = {c["component"]: c for c in r.structured_content["components"]}[WORKER]
    assert row["rollout"] == "progressing" and "not ready" in row["rollout_detail"]
    assert row["drift"] is True


# ---------------------------------------------------------------- cancelling the wait

def _running_ship(state, rt, *, tool="orbit_deploy_ship"):
    """A job that holds the pair and waits, as a ship waits for its rollout, until told to stop."""
    from orbitd.roles import Mutation, Step
    release = threading.Event()

    def fn():
        while not rt.locks.expired(PROJECT, ENV) and not release.is_set():
            time.sleep(0.01)
        return Mutation(status="failed", commit="c" * 40, evidence={"verdict": "LANDED NOT DEPLOYED rollout"},
                        steps=[Step(name="rollout", status="failed",
                                    detail=f"{deploy.stop_reason(rt, PROJECT, ENV)}: waiting")])

    started = deploy.start_job(state, project=PROJECT, env=ENV, fn=fn, actor="ada", tool=tool,
                               component=WORKER, tag=NEW, opening=[])
    return started.evidence["job_id"], release


async def test_cancel_stops_a_waiting_ship_and_frees_the_pair(fake_backends, http):
    rt, _ = install(fake_backends)
    seed_world(fake_backends)
    job_id, _ = _running_ship(fake_backends, rt)
    async with sdk_client(http, mint(fake_backends, Role.operator, "grace")) as client:
        busy = await client.call_tool("orbit_workload_restart", {"project": PROJECT, "env": ENV, "component": WORKER,
                                                                 "dry_run": False})
        r = await client.call_tool("orbit_job_cancel", {"job_id": job_id})
        record = await client.call_tool("orbit_job_status", {"job_id": job_id})
        again = await client.call_tool("orbit_job_cancel", {"job_id": job_id})
    assert not r.is_error, r.content
    body = r.structured_content
    assert body["status"] == "applied" and body["evidence"]["job_status"] == "cancelled"
    assert "pending" not in body["evidence"]
    rec = record.structured_content
    assert rec["status"] == "cancelled"
    assert rec["result"]["steps"][-1]["name"] == "cancel" and "cancelled by grace" in rec["result"]["steps"][-1]["detail"]
    assert rec["result"]["steps"][0]["detail"].startswith("cancelled by grace")
    assert rec["result"]["evidence"]["cancelled_by"] == "grace"
    assert rec["result"]["commit"] == "c" * 40, "what had landed is still named"
    assert rt.locks.try_acquire(PROJECT, ENV, "next") is None, "the pair was not freed"
    rt.locks.release(PROJECT, ENV, "next")
    assert rt.locks.cancelled_by(PROJECT, ENV) is None, "the next holder inherited the cancellation"
    assert again.is_error and again.structured_content["code"] == "not_running"


async def test_a_viewer_cannot_cancel_and_an_unknown_job_is_not_found(fake_backends, http):
    rt, _ = install(fake_backends)
    seed_world(fake_backends)
    job_id, release = _running_ship(fake_backends, rt)
    async with sdk_client(http, mint(fake_backends, Role.viewer)) as client:
        denied = await client.call_tool("orbit_job_cancel", {"job_id": job_id})
    async with sdk_client(http, mint(fake_backends, Role.operator)) as client:
        ghost = await client.call_tool("orbit_job_cancel", {"job_id": "0" * 32})
    assert denied.is_error and denied.structured_content["code"] == "FORBIDDEN"
    assert ghost.is_error and ghost.structured_content["code"] == "not_found"
    assert not rt.locks.expired(PROJECT, ENV)
    release.set()


async def test_a_job_whose_steps_must_finish_cannot_be_cancelled(fake_backends, http):
    rt, _ = install(fake_backends)
    seed_world(fake_backends)
    for tool in ("orbit_db_restore", "orbit_project_remove", "orbit_catalog_set", "orbit_features_set"):
        job_id, release = _running_ship(fake_backends, rt, tool=tool)
        async with sdk_client(http, mint(fake_backends, Role.admin)) as client:
            r = await client.call_tool("orbit_job_cancel", {"job_id": job_id})
        assert r.is_error and r.structured_content["code"] == "not_cancellable", tool
        assert not rt.locks.expired(PROJECT, ENV), f"{tool} was told to stop"
        release.set()
        from orbitd.tools_viewer.common import jobs_of
        jobs_of(fake_backends).wait(job_id, 5)
