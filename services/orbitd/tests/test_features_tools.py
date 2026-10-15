"""orbit_features_set and orbit_features_list (docs/V1.md M2c), offline: who may switch a
feature, what is refused before anything is written, the confirmation round, the commit
with its ledger words, the converge Job, and what the reconcile loop does when a change
did not finish."""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest
import yaml
from mcp.shared.exceptions import MCPError
from mcp_types import ElicitResult, InputRequiredResult

from orbitd import deploy as deploy_module
from orbitd import reconcile as reconcile_module
from orbitd import source as source_module
from orbitd import features_job, features_status
from orbitd import orbit_features as rules
from orbitd.features_converge import RECORD_NAME, RECORD_NS
from orbitd.platform_apply import PLATFORM_INVENTORY_PREFIX
from orbitd.reconcile import ReconcileRunner
from orbitd.roles import CheckoutSource, Role
from orbitd.testing import FakeRunner

from conftest import mint, sdk_client
from test_features_gate import BUCKET, OPTIONAL, REGISTRY, write_features
from test_tools_operator import install, wait_result

pytestmark = pytest.mark.anyio

COMMIT = "0123456789abcdef0123456789abcdef01234567"
ALL_ON = {name: True for name in OPTIONAL}
FEATURE = "traces"
SETTINGS = {"ORBITD_IMAGE": "registry.invalid/orbitd:v9", "ORBITD_GIT_SYNC_IMAGE": "registry.invalid/git-sync:v4",
            "ORBITD_INSTANCE_REPO": "https://github.com/fixture-org/fixture-instance.git"}


def owned(*names):
    return [app for name in names for app in REGISTRY[name]["apps"]]


def world(state, monkeypatch, *, block=ALL_ON, applied=None, record="same", runner=None):
    """An instance checkout declaring `block`, a cluster with `applied` Applications and a
    features record, orbitd told its own image, and a checkout already at COMMIT."""
    write_features(state, None if block is None else dict(block))
    kube = state.backends.kube
    for app in (owned(*OPTIONAL) if applied is None else applied):
        kube.add_object({"apiVersion": "v1", "kind": "ConfigMap",
                         "metadata": {"name": f"{PLATFORM_INVENTORY_PREFIX}{app}", "namespace": RECORD_NS},
                         "data": {"objects": f"/v1/ConfigMap/{app}/{app}-config\n/v1/Namespace//{app}\n"}})
    if record is not None:
        features = dict(block or ALL_ON) if record == "same" else record
        kube.add_object({"apiVersion": "v1", "kind": "ConfigMap",
                         "metadata": {"name": RECORD_NAME, "namespace": RECORD_NS},
                         "data": {"record": json.dumps({"features": features})}})
    for key, value in SETTINGS.items():
        monkeypatch.setenv(key, value)
    at_commit = lambda state, *, now=None: CheckoutSource(commit=COMMIT, age_s=0, lag_note="test")  # noqa: E731
    monkeypatch.setattr(deploy_module, "read_source", at_commit)
    monkeypatch.setattr(reconcile_module, "read_source", at_commit)
    monkeypatch.setattr(source_module, "read_head", at_commit)
    rt, clock = install(state, runner=runner or FakeRunner(
        commit=COMMIT, stdout=f"STEP edit OK\nFEATURES logs=on traces=off\nCOMMIT {COMMIT}\n"))
    state.cfg = replace(state.cfg, apply_mode="on")
    return kube, rt, clock


def jobs(kube) -> list[dict]:
    return [o for o in kube.objects if o.get("kind") == "Job"]


def finish_jobs(kube, clock, *, outcome="Complete", report=None):
    """Make the fake cluster run a converge Job: on the first wait it completes, and its
    pod's log carries the engine's report."""
    real_sleep = clock.sleep

    def sleep(seconds: float) -> None:
        real_sleep(seconds)
        for job in jobs(kube):
            if job.get("status"):
                continue
            name = job["metadata"]["name"]
            job["status"] = {"conditions": [{"type": outcome, "status": "True"}]}
            kube.add_pod(namespace=RECORD_NS, name=f"{name}-pod", labels={"job-name": name})
            body = report if report is not None else {
                "ok": outcome == "Complete", "features": {FEATURE: {"desired": False, "was": True, "action": "disable"}},
                "steps": [{"action": "retire", "app": "tempo", "for": [FEATURE], "ok": outcome == "Complete",
                           **({} if outcome == "Complete" else {"error": "the namespace would not go"})}]}
            kube.pod_logs[(RECORD_NS, f"{name}-pod")] = "applier chatter\n" + json.dumps(body) + "\n"

    return sleep


# ---------------------------------------------------------------- who, and what is refused

@pytest.mark.parametrize("role", [Role.viewer, Role.operator])
async def test_only_an_admin_may_switch_a_feature(fake_backends, http, monkeypatch, role):
    kube, rt, _ = world(fake_backends, monkeypatch)
    async with sdk_client(http, mint(fake_backends, role), confirm=True) as client:
        r = await client.call_tool("orbit_features_set", {"feature": FEATURE, "enabled": False})
    assert r.is_error and r.structured_content["code"] == "FORBIDDEN"
    assert r.structured_content["evidence"]["required_role"] == "admin"
    assert rt.runner.calls == [] and jobs(kube) == []


async def test_without_the_confirmation_round_nothing_changes(fake_backends, http, monkeypatch):
    kube, rt, _ = world(fake_backends, monkeypatch)
    async with sdk_client(http, mint(fake_backends, Role.admin)) as client:
        first = await client.session.call_tool("orbit_features_set", {"feature": FEATURE, "enabled": False},
                                               allow_input_required=True)
    assert isinstance(first, InputRequiredResult)
    message = first.input_requests["confirm"].params.message
    assert f"Turn the {FEATURE} feature OFF" in message
    for app in REGISTRY[FEATURE]["apps"]:
        assert app in message, "the confirmation names every Application that will be retired"
    assert rt.runner.calls == [] and jobs(kube) == []
    # declined: refused, and still nothing
    async with sdk_client(http, mint(fake_backends, Role.admin), confirm=None) as client:
        r = await client.call_tool("orbit_features_set", {"feature": FEATURE, "enabled": False})
    assert r.structured_content["status"] == "refused"
    assert rt.runner.calls == [] and jobs(kube) == []


async def test_a_confirmation_cannot_be_replayed_for_another_switch_or_by_another_admin(fake_backends, http, monkeypatch):
    kube, rt, _ = world(fake_backends, monkeypatch)
    yes = {"confirm": ElicitResult(action="accept", content={"confirm": True})}
    async with sdk_client(http, mint(fake_backends, Role.admin, "first")) as client:
        first = await client.session.call_tool("orbit_features_set", {"feature": FEATURE, "enabled": False},
                                               allow_input_required=True)
        with pytest.raises(MCPError):
            await client.session.call_tool("orbit_features_set", {"feature": "logs", "enabled": False},
                                           input_responses=yes, request_state=first.request_state)
    async with sdk_client(http, mint(fake_backends, Role.admin, "second")) as client:
        with pytest.raises(MCPError):
            await client.session.call_tool("orbit_features_set", {"feature": FEATURE, "enabled": False},
                                           input_responses=yes, request_state=first.request_state)
        # an answer with no sealed state at all is refused too
        r = await client.session.call_tool("orbit_features_set", {"feature": FEATURE, "enabled": False},
                                           input_responses=yes)
    assert r.structured_content["status"] == "refused"
    assert rt.runner.calls == [] and jobs(kube) == []


async def test_a_feature_turned_on_without_its_bucket_is_refused_naming_the_bucket(fake_backends, http, monkeypatch):
    kube, rt, _ = world(fake_backends, monkeypatch, block={}, applied=[], record={})
    root = Path(fake_backends.backends.instance.root)
    (root / "instance" / "config.yaml").write_text(yaml.safe_dump({"features": {}}))   # no bucket at all
    fake_backends.features.snapshot(fresh=True)
    async with sdk_client(http, mint(fake_backends, Role.admin), confirm=True) as client:
        for feature in ("logs", "traces"):
            r = await client.call_tool("orbit_features_set", {"feature": feature, "enabled": True})
            body = r.structured_content
            assert r.is_error and body["code"] == "refused", body
            assert "bucket" in body["what_failed"] and "objectStorage.buckets.platform" in body["what_failed"]
    assert rt.runner.calls == [] and jobs(kube) == []


async def test_what_is_not_a_switchable_feature_is_refused_before_any_write(fake_backends, http, monkeypatch):
    kube, rt, _ = world(fake_backends, monkeypatch)
    async with sdk_client(http, mint(fake_backends, Role.admin), confirm=True) as client:
        unknown = await client.call_tool("orbit_features_set", {"feature": "nope", "enabled": True})
        core = await client.call_tool("orbit_features_set", {"feature": "core", "enabled": False})
    assert unknown.structured_content["code"] == "refused" and "not a feature" in unknown.structured_content["what_failed"]
    assert core.structured_content["code"] == "refused" and "always on" in core.structured_content["what_failed"]
    assert rt.runner.calls == [] and jobs(kube) == []


async def test_a_dry_run_names_the_steps_and_writes_nothing(fake_backends, http, monkeypatch):
    kube, rt, _ = world(fake_backends, monkeypatch)
    async with sdk_client(http, mint(fake_backends, Role.admin)) as client:
        r = await client.call_tool("orbit_features_set", {"feature": FEATURE, "enabled": False, "dry_run": True})
    body = r.structured_content
    assert body["status"] == "dry_run"
    actions = [(s["action"], s["app"]) for s in body["evidence"]["steps"]]
    assert [a for a in actions if a[0] == "retire"] == [("retire", app) for app in reversed(REGISTRY[FEATURE]["apps"])]
    assert rt.runner.calls == [] and jobs(kube) == []


async def test_a_feature_already_as_asked_on_a_matching_cluster_is_applied_unchanged(fake_backends, http, monkeypatch):
    kube, rt, _ = world(fake_backends, monkeypatch)
    async with sdk_client(http, mint(fake_backends, Role.admin)) as client:
        r = await client.call_tool("orbit_features_set", {"feature": FEATURE, "enabled": True})
    assert r.structured_content["status"] == "applied" and r.structured_content["evidence"]["unchanged"] is True
    assert rt.runner.calls == [] and jobs(kube) == []


# ---------------------------------------------------------------- the accepted change

async def test_an_accepted_switch_commits_once_with_the_ledger_words_and_converges_in_a_job(fake_backends, http, monkeypatch):
    kube, rt, clock = world(fake_backends, monkeypatch)
    rt.sleeper = finish_jobs(kube, clock)
    async with sdk_client(http, mint(fake_backends, Role.admin, "ada"), confirm=True) as client:
        running = await client.call_tool("orbit_features_set", {"feature": FEATURE, "enabled": False})
        result, _ = await wait_result(client, running)
    assert result["status"] == "applied", result
    assert [s["name"] for s in result["steps"]] == ["confirm", "validate", "commit", "checkout", "converge"]
    assert all(s["status"] == "ok" for s in result["steps"])
    assert result["commit"] == COMMIT
    # one commit, by the features script, carrying who did it
    assert len(rt.runner.calls) == 1
    call = rt.runner.calls[0]
    assert call["argv"] == [rt.features_script, FEATURE, "off"]
    assert call["env"] == {"ORBIT_ACTION": "features-set", "ORBIT_ACTOR": "ada"}
    # one Job, pinned to that commit, under the platform ServiceAccount, running the engine
    (job,) = jobs(kube)
    spec = job["spec"]["template"]["spec"]
    assert spec["serviceAccountName"] == "orbit-platform"
    assert spec["containers"][0]["command"] == ["orbit", "features", "converge", "/var/lib/instance/current", "--gated"]
    assert spec["containers"][0]["image"] == SETTINGS["ORBITD_IMAGE"]
    assert f"--ref={COMMIT}" in spec["initContainers"][0]["args"]
    assert f"--repo={SETTINGS['ORBITD_INSTANCE_REPO']}" in spec["initContainers"][0]["args"]
    assert job["spec"]["backoffLimit"] == 0
    assert result["evidence"]["converge_job"] == job["metadata"]["name"]
    assert result["evidence"]["converge"]["steps"][0]["app"] == "tempo"


async def test_a_converge_that_fails_is_failed_with_the_step_that_broke_and_the_commit_kept(fake_backends, http, monkeypatch):
    kube, rt, clock = world(fake_backends, monkeypatch)
    rt.sleeper = finish_jobs(kube, clock, outcome="Failed")
    async with sdk_client(http, mint(fake_backends, Role.admin), confirm=True) as client:
        running = await client.call_tool("orbit_features_set", {"feature": FEATURE, "enabled": False})
        body = running.structured_content
        waited = await client.call_tool("orbit_job_wait", {"job_id": body["evidence"]["job_id"], "timeout_s": 5})
    result = waited.structured_content["result"]
    assert result["status"] == "failed"
    converge = next(s for s in result["steps"] if s["name"] == "converge")
    assert converge["status"] == "failed" and "retire tempo: the namespace would not go" in converge["detail"]
    assert result["commit"] == COMMIT and result["evidence"]["verdict"] == "LANDED NOT CONVERGED"


async def test_a_commit_script_that_refuses_fails_the_job_and_starts_no_converge(fake_backends, http, monkeypatch):
    runner = FakeRunner(returncode=1, stdout="STEP args OK\nSTEP edit FAIL\ntraces cannot be turned off while x is on: x needs it\n")
    kube, rt, _ = world(fake_backends, monkeypatch, runner=runner)
    async with sdk_client(http, mint(fake_backends, Role.admin), confirm=True) as client:
        running = await client.call_tool("orbit_features_set", {"feature": FEATURE, "enabled": False})
        waited = await client.call_tool("orbit_job_wait", {"job_id": running.structured_content["evidence"]["job_id"], "timeout_s": 5})
    result = waited.structured_content["result"]
    commit = next(s for s in result["steps"] if s["name"] == "commit")
    assert result["status"] == "failed" and commit["status"] == "failed"
    assert "cannot be turned off" in commit["detail"]
    assert jobs(kube) == []


async def test_without_its_own_image_orbitd_says_it_cannot_and_points_at_the_desk(fake_backends, http, monkeypatch):
    kube, rt, _ = world(fake_backends, monkeypatch)
    monkeypatch.delenv("ORBITD_IMAGE")
    async with sdk_client(http, mint(fake_backends, Role.admin), confirm=True) as client:
        r = await client.call_tool("orbit_features_set", {"feature": FEATURE, "enabled": False})
    body = r.structured_content
    assert r.is_error and body["code"] == "backend_unconfigured"
    assert "orbit features converge" in body["next_action"]
    assert rt.runner.calls == [] and jobs(kube) == []


# ---------------------------------------------------------------- the Job, and the loop that retries

def test_the_job_is_pinned_to_a_full_commit_and_nothing_else_is_callers_text():
    settings = features_job.JobSettings.from_env(SETTINGS)
    body = features_job.manifest(settings, COMMIT)
    assert body["metadata"]["namespace"] == RECORD_NS
    assert body["metadata"]["labels"][features_job.LABEL] == features_job.LABEL_VALUE
    for container in body["spec"]["template"]["spec"]["initContainers"] + body["spec"]["template"]["spec"]["containers"]:
        sc = container["securityContext"]
        assert sc["runAsNonRoot"] and sc["readOnlyRootFilesystem"] and not sc["allowPrivilegeEscalation"]
        assert sc["capabilities"] == {"drop": ["ALL"]}
        assert container["resources"]["limits"]["memory"]
    for bad in ("main", "0123456", COMMIT.upper(), COMMIT + " --repo=https://evil.invalid/x.git", ""):
        with pytest.raises(features_job.ConvergeUnavailable):
            features_job.manifest(settings, bad)
    assert features_job.JobSettings.from_env({}) is None


def test_the_report_is_the_last_json_object_the_engine_printed():
    text = 'prune set for x:\n  {"not": "a report"}\n{"ok": true, "steps": [], "features": {}}\ntrailing\n'
    assert features_job.last_json_object(text) == {"ok": True, "steps": [], "features": {}}
    assert features_job.last_json_object("no json here") is None


def runner_for(state):
    return ReconcileRunner(state, dry_run=False, period_s=180)


def test_the_loop_starts_a_converge_when_the_cluster_is_behind_and_only_one(fake_backends, monkeypatch):
    """The checkout says traces is off; the cluster still has it (the change's own converge
    never finished: orbitd restarted mid-change)."""
    state = fake_backends
    kube, _, _ = world(state, monkeypatch, block={**ALL_ON, FEATURE: False}, record=dict(ALL_ON))
    loop = runner_for(state)
    name = loop.tick_features(now=1000.0)
    assert name and [j["metadata"]["name"] for j in jobs(kube)] == [name]
    assert f"--ref={COMMIT}" in jobs(kube)[0]["spec"]["template"]["spec"]["initContainers"][0]["args"]
    assert loop.tick_features(now=1180.0) is None, "a converge is running: no second one"
    assert len(jobs(kube)) == 1


def test_the_loop_backs_off_after_a_failed_converge_and_cleans_up_once_matched(fake_backends, monkeypatch):
    state = fake_backends
    kube, _, _ = world(state, monkeypatch, block={**ALL_ON, FEATURE: False}, record=dict(ALL_ON))
    loop = runner_for(state)
    first = loop.tick_features(now=1000.0)
    jobs(kube)[0]["status"] = {"conditions": [{"type": "Failed", "status": "True"}]}
    assert loop.tick_features(now=1180.0) is None, "three minutes after a failure is too soon"
    assert loop.tick_features(now=1000.0 + 599) is None
    second = loop.tick_features(now=1000.0 + 601)
    assert second and second != first
    jobs(kube)[-1]["status"] = {"conditions": [{"type": "Failed", "status": "True"}]}
    assert loop.tick_features(now=1601.0 + 601) is None, "the second wait is twice the first"
    assert loop.tick_features(now=1601.0 + 1201) is not None
    # the cluster now matches (the converge worked): nothing starts, failed Jobs are removed
    for job in jobs(kube):
        job["status"] = {"conditions": [{"type": "Failed", "status": "True"}]}
    kube.objects[:] = [o for o in kube.objects
                       if (o.get("metadata") or {}).get("name") not in
                       {f"{PLATFORM_INVENTORY_PREFIX}{a}" for a in REGISTRY[FEATURE]["apps"]} | {RECORD_NAME}]
    kube.add_object({"apiVersion": "v1", "kind": "ConfigMap", "metadata": {"name": RECORD_NAME, "namespace": RECORD_NS},
                     "data": {"record": json.dumps({"features": {**ALL_ON, FEATURE: False}})}})
    assert loop.tick_features(now=99999.0) is None
    assert {c["name"] for c in kube.delete_calls if c["kind"] == "Job"} == {j["metadata"]["name"] for j in jobs(kube)} or jobs(kube) == []


def test_the_loop_does_nothing_when_they_agree_or_in_dry_run(fake_backends, monkeypatch):
    state = fake_backends
    kube, _, _ = world(state, monkeypatch)
    assert runner_for(state).tick_features() is None and jobs(kube) == []
    write_features(state, {**ALL_ON, FEATURE: False})
    assert ReconcileRunner(state, dry_run=True, period_s=180).tick_features() is None
    assert jobs(kube) == []


# ---------------------------------------------------------------- status

async def test_the_list_says_each_features_state_in_the_three_situations(fake_backends, http, monkeypatch):
    state = fake_backends
    kube, _, _ = world(state, monkeypatch)
    root = Path(state.backends.instance.root)
    for ref in sorted({ref for spec in REGISTRY.values() for ref in spec.get("secrets") or []}):
        ns, name = ref.split("/")
        (root / "clusters" / "platform" / "secrets" / ns).mkdir(parents=True, exist_ok=True)
        (root / "clusters" / "platform" / "secrets" / ns / f"{name}.yaml").write_text("kind: SopsSecret\n")
        kube.add_object({"apiVersion": "isindir.github.com/v1alpha3", "kind": "SopsSecret",
                         "metadata": {"name": name, "namespace": ns}})
    async with sdk_client(http, mint(state, Role.viewer)) as client:
        # 1. everything as declared
        rows = {r["feature"]: r for r in (await client.call_tool("orbit_features_list", {})).structured_content["items"]}
        assert rows["core"]["always"] and rows["core"]["says"] == "core: always on"
        for name in OPTIONAL:
            assert rows[name]["declared"] and rows[name]["applied"] and rows[name]["pending"] is None
            assert all(r["met"] for r in rows[name]["requires"]), rows[name]["requires"]
            assert rows[name]["tools"] == REGISTRY[name]["tools"]
            assert "applied and ready" in rows[name]["says"]
        # 2. declared on, a workload broken: not ready, and it is named
        kube.objects.append({"apiVersion": "apps/v1", "kind": "StatefulSet",
                             "metadata": {"name": "loki", "namespace": "loki", "generation": 1},
                             "spec": {"replicas": 1}, "status": {"observedGeneration": 1, "readyReplicas": 0}})
        inv = next(o for o in kube.objects if o["metadata"]["name"] == f"{PLATFORM_INVENTORY_PREFIX}loki")
        inv["data"]["objects"] = "apps/v1/StatefulSet/loki/loki\n"
        rows = {r["feature"]: r for r in (await client.call_tool("orbit_features_list", {})).structured_content["items"]}
        assert rows["logs"]["ready"] is False and rows["logs"]["not_ready"] == ["StatefulSet/loki/loki"]
        assert "NOT READY: StatefulSet/loki/loki" in rows["logs"]["says"]
        # ...and a workload scaled to zero is not "ready" because zero of zero answer (seen
        # live, docs/V1.md §8: Loki at 0 replicas read "applied and ready" in all three views)
        loki = next(o for o in kube.objects if o.get("kind") == "StatefulSet" and o["metadata"]["name"] == "loki")
        loki["spec"]["replicas"], loki["status"] = 0, {"observedGeneration": 1, "readyReplicas": 0, "updatedReplicas": 0}
        rows = {r["feature"]: r for r in (await client.call_tool("orbit_features_list", {})).structured_content["items"]}
        assert rows["logs"]["ready"] is False and rows["logs"]["not_ready"] == ["StatefulSet/loki/loki (scaled to 0)"]
        assert "NOT READY: StatefulSet/loki/loki (scaled to 0)" in rows["logs"]["says"]
        loki["spec"]["replicas"], loki["status"] = 1, {"observedGeneration": 1, "readyReplicas": 0}
        # 3. a requirement removed: the sealed secret is gone from the cluster
        kube.objects[:] = [o for o in kube.objects
                           if not (o.get("kind") == "SopsSecret" and o["metadata"]["namespace"] == "tempo")]
        rows = {r["feature"]: r for r in (await client.call_tool("orbit_features_list", {})).structured_content["items"]}
        unmet = [r for r in rows["traces"]["requires"] if not r["met"]]
        assert [r["name"] for r in unmet] == ["secret tempo/orbit-s3"]
        assert "not applied on the cluster" in unmet[0]["detail"]
        assert "MISSING" in rows["traces"]["says"] and "tempo/orbit-s3" in rows["traces"]["says"]


def test_the_collector_says_why_when_it_cannot_read(fake_backends):
    state = fake_backends
    data = features_status.collect(state)     # no registry in the fixture checkout
    assert data["features"] == [] and "predates features" in data["problem"]
    write_features(state, {"no-such-feature": True})
    data = features_status.collect(state)
    assert "no-such-feature" in data["problem"]


async def test_the_api_route_returns_the_same_listing_to_any_valid_token_and_nothing_without_one(fake_backends, http, monkeypatch):
    state = fake_backends
    world(state, monkeypatch)
    async with http() as h:
        assert (await h.get("/api/features")).status_code == 401
    async with http("not-a-token") as h:
        assert (await h.get("/api/features")).status_code == 401
    async with http(mint(state, Role.viewer)) as h:
        r = await h.get("/api/features")
    assert r.status_code == 200 and r.headers["cache-control"] == "no-store"
    async with sdk_client(http, mint(state, Role.viewer)) as client:
        tool = (await client.call_tool("orbit_features_list", {})).structured_content
    assert [row["feature"] for row in r.json()["items"]] == [row["feature"] for row in tool["items"]]
    assert [row["says"] for row in r.json()["items"]] == [row["says"] for row in tool["items"]]


def test_an_app_whose_inventory_holds_only_its_namespace_is_not_applied(fake_backends, monkeypatch):
    """What a retire leaves: the inventory ConfigMap, listing the namespace it never deletes.
    Read as applied, the loop would retire it again on every tick."""
    from orbitd.features_converge import ClusterOps
    state = fake_backends
    kube, _, _ = world(state, monkeypatch, block={**ALL_ON, FEATURE: False}, record={**ALL_ON, FEATURE: False},
                       applied=owned(*[n for n in OPTIONAL if n != FEATURE]))
    for app in REGISTRY[FEATURE]["apps"]:
        kube.add_object({"apiVersion": "v1", "kind": "ConfigMap",
                         "metadata": {"name": f"{PLATFORM_INVENTORY_PREFIX}{app}", "namespace": RECORD_NS},
                         "data": {"objects": f"/v1/Namespace//{app}\n"}})
    ops = ClusterOps(Path(state.backends.instance.root), kube)
    assert [ops.applied(app) for app in REGISTRY[FEATURE]["apps"]] == [False] * len(REGISTRY[FEATURE]["apps"])
    assert all(ops.applied(app) for app in owned(*[n for n in OPTIONAL if n != FEATURE]))
    assert runner_for(state).tick_features() is None and jobs(kube) == [], "nothing is left to converge"


def test_the_loop_never_converges_at_a_commit_the_gate_refused(fake_backends, monkeypatch):
    """The Job checks out the commit it is given. While the newest commit is refused it is
    given the last good one; with no good commit there is no Job."""
    from orbitd.gate import GateState
    state = fake_backends
    kube, _, _ = world(state, monkeypatch, block={**ALL_ON, FEATURE: False}, record=dict(ALL_ON))
    good = "1" * 40

    class Gate:
        def __init__(self, verdict):
            self.verdict = verdict

        def evaluate(self):
            return self.verdict

    state.commit_gate = Gate(GateState(enabled=True, head=COMMIT, good=good, refused=True,
                                       check="scaffold", sentence="x"))
    name = runner_for(state).tick_features(now=1000.0)
    assert name
    args = jobs(kube)[0]["spec"]["template"]["spec"]["initContainers"][0]["args"]
    assert f"--ref={good}" in args and f"--ref={COMMIT}" not in args
    kube.objects[:] = [o for o in kube.objects if o.get("kind") != "Job"]
    state.commit_gate = Gate(GateState(enabled=True, head=COMMIT, good=None, refused=True, check="core", sentence="x"))
    assert runner_for(state).tick_features(now=1000.0) is None and jobs(kube) == []
    state.commit_gate = Gate(GateState(enabled=True, head=COMMIT, good=good, undecided=True, sentence="x"))
    runner_for(state).tick_features(now=1000.0)
    assert f"--ref={good}" in jobs(kube)[0]["spec"]["template"]["spec"]["initContainers"][0]["args"]


# ---------------------------------------------------------------- whose memory a feature's is

def test_a_features_memory_is_its_own_workloads_not_its_namespaces():
    """QA of v1.0.0, F39: the uptime feature (one exporter in `monitoring`) read 708 MiB,
    the memory of everything in that namespace, Prometheus and Grafana included."""
    from types import SimpleNamespace

    from orbitd import features_status

    asked = []

    class Prom:
        def get(self, path, params=None):
            asked.append(params["query"])
            return {"data": {"result": [{"value": [0, str(60 * 1048576)]}]}}

    got = features_status._memory_mib(Prom(), [("monitoring", "blackbox-exporter"), ("loki", "loki"),
                                              ("monitoring", "a.b")])
    assert got == 60.0
    (query,) = asked
    assert query == (
        'sum(container_memory_rss{container!="",namespace="loki",pod=~"(loki)-.+"} or '
        'container_memory_rss{container!="",namespace="monitoring",pod=~"(a' + chr(92) * 2 + '.b|blackbox-exporter)-.+"})'), query
    assert 'namespace=~' not in query, "a whole namespace was summed"
    # nothing of its own to measure, and a Prometheus that does not answer: no number
    assert features_status._memory_mib(Prom(), []) is None
    assert features_status._memory_mib(Prom(), None) is None
    broken = SimpleNamespace(get=lambda path, params=None: (_ for _ in ()).throw(RuntimeError("down")))
    assert features_status._memory_mib(broken, [("loki", "loki")]) is None


def test_the_memory_query_is_one_prometheus_accepts_and_counts_only_the_features_pods(tmp_path):
    """The query for `blackbox-exporter` was a parse error on the test box (an escaped
    hyphen is no escape PromQL knows), so the feature's memory read null. The expression
    the tool builds runs here in Prometheus's own evaluator: it must parse, and it must
    count the feature's pods and nobody else's in the same namespace."""
    import shutil
    import subprocess

    import pytest
    import yaml

    from orbitd import features_status

    promtool = shutil.which("promtool")
    if promtool is None:
        pytest.skip("promtool is not installed here (scripts/local-tests.sh has it)")
    asked = []

    class Prom:
        def get(self, path, params=None):
            asked.append(params["query"])
            return {"data": {"result": []}}

    features_status._memory_mib(Prom(), [("monitoring", "blackbox-exporter"), ("tempo", "tempo"), ("monitoring", "a.b")])
    mi = 1048576
    series = {
        'container_memory_rss{namespace="monitoring",pod="blackbox-exporter-96dc99ff4-tzdlm",container="blackbox-exporter"}': f"{40 * mi}x10",
        'container_memory_rss{namespace="monitoring",pod="prometheus-kube-prometheus-stack-prometheus-0",container="prometheus"}': f"{500 * mi}x10",
        'container_memory_rss{namespace="monitoring",pod="blackbox-exporterx-1",container="x"}': f"{7 * mi}x10",
        'container_memory_rss{namespace="monitoring",pod="aXb-1",container="x"}': f"{9 * mi}x10",
        'container_memory_rss{namespace="monitoring",pod="a.b-1",container="x"}': f"{3 * mi}x10",
        'container_memory_rss{namespace="tempo",pod="tempo-0",container="tempo"}': f"{60 * mi}x10",
        'container_memory_rss{namespace="tempo",pod="tempo-0",container=""}': f"{999 * mi}x10",
        'container_memory_rss{namespace="other",pod="tempo-0",container="tempo"}': f"{999 * mi}x10",
    }
    cases = {"rule_files": [], "evaluation_interval": "1m", "tests": [{
        "interval": "1m", "input_series": [{"series": k, "values": v} for k, v in series.items()],
        "promql_expr_test": [{"expr": asked[0], "eval_time": "5m",
                              "exp_samples": [{"labels": "{}", "value": (40 + 3 + 60) * mi}]}]}]}
    (tmp_path / "mem.test.yaml").write_text(yaml.safe_dump(cases, sort_keys=False), encoding="utf-8")
    proc = subprocess.run([promtool, "test", "rules", "mem.test.yaml"], cwd=tmp_path, capture_output=True, text=True, timeout=120)
    assert proc.returncode == 0, proc.stdout + proc.stderr
