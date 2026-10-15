"""Operator monitor tools over fakes: orbit_monitor_upsert, orbit_monitor_delete,
orbit_monitor_silence. A monitor change is a commit to the instance repo made by the
monitor commit script (the GitHub App push path ship uses), then, with ORBITD_APPLY=on,
the monitors unit applied or pruned from the checkout that carries that commit."""

from __future__ import annotations

import json
import subprocess
from dataclasses import replace
from datetime import datetime
from pathlib import Path

import pytest
import yaml
from conftest import mint, sdk_client
from mcp_types import InputRequiredResult

from orbitd.deploy import MONITOR_SCRIPT, DeployRuntime, RunResult
from orbitd.roles import Role
from orbitd.testing import FakeClock

from test_monitors import metric, probe
from test_tools_operator import CATALOG as SHIP_CATALOG, wait_result
from test_roles import rpc
from test_tools_viewer_kube import seed_catalog

pytestmark = pytest.mark.anyio

SILENCES = "/api/v2/silences"


def git(root: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(root), *args], check=True,
                          capture_output=True, text=True).stdout.strip()


def commit_all(root: Path, message: str) -> str:
    git(root, "add", "-A", "catalog", "monitors")
    git(root, "-c", "user.name=t", "-c", "user.email=t@example.test",
        "-c", "commit.gpgsign=false", "commit", "-q", "--allow-empty", "-m", message)
    return git(root, "rev-parse", "HEAD")


class CommittingRunner:
    """Stands in for the monitor commit script and git-sync at once: writes or removes
    monitors/<name>.yaml in the checkout, commits, and prints the COMMIT line."""

    def __init__(self, root: Path, *, returncode: int = 0) -> None:
        self.root = root
        self.returncode = returncode
        self.calls: list[dict] = []
        self.bodies: list[str] = []

    def run(self, argv, extra_env):
        self.calls.append({"argv": list(argv), "env": dict(extra_env)})
        if self.returncode:
            return RunResult(returncode=self.returncode, stdout="STEP push FAIL\n", stderr="")
        op, name = argv[1], argv[2]
        path = self.root / "monitors" / f"{name}.yaml"
        if op == "upsert":
            text = Path(extra_env["MONITOR_FILE"]).read_text()
            self.bodies.append(text)
            path.write_text(text)
        else:
            path.unlink()
        sha = commit_all(self.root, f"monitor {op} {name}")
        return RunResult(returncode=0, stdout=f"STEP commit OK\nCOMMIT {sha}\n", stderr="")


def world(state, *, mode="on", monitors=(), returncode=0) -> CommittingRunner:
    seed_catalog(state, [SHIP_CATALOG])
    root = state.backends.instance_root
    (root / "monitors").mkdir(exist_ok=True)
    for body in monitors:
        (root / "monitors" / f"{body['name']}.yaml").write_text(yaml.safe_dump(body))
    commit_all(root, "seed")
    runner = CommittingRunner(root, returncode=returncode)
    clock = FakeClock(t=1_710_000_000.0)
    state.deploy = DeployRuntime(runner=runner, clock=clock.now, sleeper=clock.sleep,
                                 rollout_timeout_s=5, assert_timeout_s=5)
    state.cfg = replace(state.cfg, apply_mode=mode)
    return runner


def live(state, kind, namespace, name):
    for o in state.backends.kube.objects:
        m = o.get("metadata") or {}
        if o.get("kind") == kind and m.get("namespace") == namespace and m.get("name") == name:
            return o
    return None


def text_of(r) -> str:
    return json.dumps(r.structured_content) + " ".join(
        getattr(c, "text", "") for c in (r.content or []))


# ------------------------------------------------------------------ upsert

async def test_upsert_dry_run_renders_and_writes_nothing(fake_backends, http):
    runner = world(fake_backends)
    async with sdk_client(http, mint(fake_backends, Role.operator)) as client:
        r = await client.call_tool("orbit_monitor_upsert", {"monitor": metric(), "dry_run": True})
    assert not r.is_error, r.content
    body = r.structured_content
    assert body["status"] == "dry_run"
    assert "PrometheusRule/monitoring/orbit-monitor-queue-backlog" in json.dumps(body["evidence"])
    assert runner.calls == []


async def test_upsert_asks_for_confirmation_naming_the_monitor(fake_backends, http):
    world(fake_backends)
    async with sdk_client(http, mint(fake_backends, Role.operator)) as client:
        first = await client.session.call_tool(
            "orbit_monitor_upsert", {"monitor": metric()}, allow_input_required=True)
    assert isinstance(first, InputRequiredResult)
    assert "queue-backlog" in first.input_requests["confirm"].params.message


async def test_upsert_not_confirmed_writes_nothing(fake_backends, http):
    runner = world(fake_backends)
    async with sdk_client(http, mint(fake_backends, Role.operator), confirm=None) as client:
        r = await client.call_tool("orbit_monitor_upsert", {"monitor": metric()})
    assert r.structured_content["status"] == "refused"
    assert runner.calls == []


async def test_an_invalid_monitor_is_refused_before_any_write(fake_backends, http):
    runner = world(fake_backends)
    async with sdk_client(http, mint(fake_backends, Role.operator), confirm=True) as client:
        r = await client.call_tool("orbit_monitor_upsert",
                                   {"monitor": metric(severity="critical")})
    assert r.is_error
    assert "severity" in text_of(r)
    assert runner.calls == []


@pytest.mark.parametrize("tool,args", [
    ("orbit_monitor_upsert", {"monitor": metric()}),
    ("orbit_monitor_delete", {"name": "queue-backlog"}),
    ("orbit_monitor_silence", {"name": "queue-backlog", "duration": "1h", "reason": "x"}),
])
async def test_a_viewer_is_refused_at_the_gate_naming_operator(fake_backends, http, tool, args):
    """The same refusal every operator tool gives (test_roles), naming the required role,
    before the tool runs. It arrives in the protocol rather than as a transport error, so a
    caller still has a session to act on it in; what matters here is unchanged -- nothing
    ran, and the caller is told which role would have carried the call."""
    runner = world(fake_backends, monitors=[metric()])
    async with http(mint(fake_backends, Role.viewer)) as h:
        r = await rpc(h, "tools/call", {"name": tool, "arguments": args})
    assert r.status_code == 200, r.text[:200]
    result = r.json()["result"]
    assert result.get("isError"), "an operator tool was not refused for a viewer"
    body = json.loads(result["content"][0]["text"])
    assert body["evidence"]["required_role"] == "operator"
    assert body["evidence"]["held_role"] == "viewer"
    assert runner.calls == []
    assert not [c for c in fake_backends.backends.alertmanager.calls if c["method"] == "POST"]


async def test_upsert_commits_through_the_script_then_applies(fake_backends, http):
    runner = world(fake_backends)
    async with sdk_client(http, mint(fake_backends, Role.operator, "desk-op"), confirm=True) as client:
        running = await client.call_tool("orbit_monitor_upsert", {"monitor": metric()})
        result, _ = await wait_result(client, running)
    assert result["status"] == "applied", result
    assert [s["name"] for s in result["steps"]] == ["confirm", "validate", "commit", "apply"]
    assert all(s["status"] == "ok" for s in result["steps"])
    (call,) = runner.calls
    assert call["argv"] == [MONITOR_SCRIPT, "upsert", "queue-backlog"]
    assert call["env"]["ORBIT_ACTION"] == "monitor-upsert"
    assert call["env"]["ORBIT_ACTOR"] == "desk-op"
    assert yaml.safe_load(runner.bodies[0]) == metric()
    assert result["commit"] == git(fake_backends.backends.instance_root, "rev-parse", "HEAD")
    rule = live(fake_backends, "PrometheusRule", "monitoring", "orbit-monitor-queue-backlog")
    assert rule is not None


async def test_upsert_with_apply_off_commits_and_skips_apply(fake_backends, http):
    runner = world(fake_backends, mode="off")
    async with sdk_client(http, mint(fake_backends, Role.operator), confirm=True) as client:
        running = await client.call_tool("orbit_monitor_upsert", {"monitor": metric()})
        result, _ = await wait_result(client, running)
    assert runner.calls
    apply = next(s for s in result["steps"] if s["name"] == "apply")
    assert apply["status"] == "skipped"
    assert live(fake_backends, "PrometheusRule", "monitoring", "orbit-monitor-queue-backlog") is None


async def test_a_failed_commit_is_reported_and_applies_nothing(fake_backends, http):
    world(fake_backends, returncode=1)
    async with sdk_client(http, mint(fake_backends, Role.operator), confirm=True) as client:
        running = await client.call_tool("orbit_monitor_upsert", {"monitor": metric()})
        result, _ = await wait_result(client, running)
    assert result["status"] == "failed"
    commit = next(s for s in result["steps"] if s["name"] == "commit")
    assert commit["status"] == "failed"
    assert live(fake_backends, "PrometheusRule", "monitoring", "orbit-monitor-queue-backlog") is None


# ------------------------------------------------------------------ delete

async def test_delete_confirms_naming_every_object_it_removes(fake_backends, http):
    world(fake_backends, monitors=[metric()])
    async with sdk_client(http, mint(fake_backends, Role.operator)) as client:
        first = await client.session.call_tool(
            "orbit_monitor_delete", {"name": "queue-backlog"}, allow_input_required=True)
    assert isinstance(first, InputRequiredResult)
    assert "orbit-monitor-queue-backlog" in first.input_requests["confirm"].params.message


async def test_delete_commits_then_prunes_exactly_that_monitor(fake_backends, http):
    runner = world(fake_backends, monitors=[metric(), metric(name="keep")])
    async with sdk_client(http, mint(fake_backends, Role.operator, "desk-op"), confirm=True) as client:
        first = await client.call_tool("orbit_monitor_upsert", {"monitor": metric(name="keep")})
        await wait_result(client, first)
        again = await client.call_tool("orbit_monitor_upsert", {"monitor": metric()})
        await wait_result(client, again)
        running = await client.call_tool("orbit_monitor_delete", {"name": "queue-backlog"})
        result, _ = await wait_result(client, running)
    assert result["status"] == "applied", result
    assert [s["name"] for s in result["steps"]] == ["confirm", "commit", "prune"]
    assert runner.calls[-1]["argv"] == [MONITOR_SCRIPT, "delete", "queue-backlog"]
    assert runner.calls[-1]["env"]["ORBIT_ACTION"] == "monitor-delete"
    assert "MONITOR_FILE" not in runner.calls[-1]["env"]
    assert live(fake_backends, "PrometheusRule", "monitoring", "orbit-monitor-queue-backlog") is None
    assert live(fake_backends, "PrometheusRule", "monitoring", "orbit-monitor-keep") is not None


async def test_delete_of_an_unknown_monitor_is_not_found(fake_backends, http):
    runner = world(fake_backends)
    async with sdk_client(http, mint(fake_backends, Role.operator), confirm=True) as client:
        r = await client.call_tool("orbit_monitor_delete", {"name": "ghost"})
    assert r.is_error
    assert r.structured_content["code"] == "not_found"
    assert runner.calls == []


async def test_what_an_unfinished_delete_left_behind_is_cleaned_by_deleting_again(fake_backends, http):
    """The file is gone from the repository and the rule is still in the cluster (the
    delete's prune never ran). Nothing else prunes it, so delete does, naming it; the
    monitor beside it stays (docs/V1.md §8)."""
    runner = world(fake_backends, monitors=[metric(), metric(name="keep")])
    root = fake_backends.backends.instance_root
    async with sdk_client(http, mint(fake_backends, Role.operator, "desk-op"), confirm=True) as client:
        for body in (metric(name="keep"), metric()):
            await wait_result(client, await client.call_tool("orbit_monitor_upsert", {"monitor": body}))
        assert live(fake_backends, "PrometheusRule", "monitoring", "orbit-monitor-queue-backlog") is not None
        # the unfinished delete: the commit landed, the prune did not
        (root / "monitors" / "queue-backlog.yaml").unlink()
        commit_all(root, "monitor delete queue-backlog")
        calls_before = len(runner.calls)
        plan = await client.call_tool("orbit_monitor_delete", {"name": "queue-backlog", "dry_run": True})
        assert plan.structured_content["evidence"]["left_behind"] is True
        assert plan.structured_content["evidence"]["objects"] == ["PrometheusRule/monitoring/orbit-monitor-queue-backlog"]
        running = await client.call_tool("orbit_monitor_delete", {"name": "queue-backlog"})
        result, _ = await wait_result(client, running)
        ghost = await client.call_tool("orbit_monitor_delete", {"name": "queue-backlog"})
    assert result["status"] == "applied", result
    assert [(s["name"], s["status"]) for s in result["steps"]] == [("confirm", "ok"), ("commit", "skipped"), ("prune", "ok")]
    assert len(runner.calls) == calls_before          # no commit script: the file was already gone
    assert live(fake_backends, "PrometheusRule", "monitoring", "orbit-monitor-queue-backlog") is None
    assert live(fake_backends, "PrometheusRule", "monitoring", "orbit-monitor-keep") is not None
    assert ghost.is_error and ghost.structured_content["code"] == "not_found"


async def test_cleaning_what_was_left_behind_asks_first_and_names_it(fake_backends, http):
    world(fake_backends, monitors=[metric()])
    root = fake_backends.backends.instance_root
    async with sdk_client(http, mint(fake_backends, Role.operator), confirm=True) as client:
        await wait_result(client, await client.call_tool("orbit_monitor_upsert", {"monitor": metric()}))
    (root / "monitors" / "queue-backlog.yaml").unlink()
    commit_all(root, "monitor delete queue-backlog")
    async with sdk_client(http, mint(fake_backends, Role.operator)) as client:
        first = await client.session.call_tool(
            "orbit_monitor_delete", {"name": "queue-backlog"}, allow_input_required=True)
    assert isinstance(first, InputRequiredResult)
    message = first.input_requests["confirm"].params.message
    assert "not defined any more" in message and "PrometheusRule/monitoring/orbit-monitor-queue-backlog" in message
    assert live(fake_backends, "PrometheusRule", "monitoring", "orbit-monitor-queue-backlog") is not None


# ------------------------------------------------------------------ silence

async def test_silence_posts_an_alertmanager_silence_carrying_actor_and_reason(fake_backends, http):
    world(fake_backends, monitors=[metric()])
    am = fake_backends.backends.alertmanager
    am.responses[("POST", SILENCES)] = {"silenceID": "sil-1"}
    async with sdk_client(http, mint(fake_backends, Role.operator, "desk-op")) as client:
        r = await client.call_tool("orbit_monitor_silence", {
            "name": "queue-backlog", "duration": "2h", "reason": "load test in progress"})
    assert not r.is_error, r.content
    assert r.structured_content["evidence"]["silence_id"] == "sil-1"
    (call,) = [c for c in am.calls if c["method"] == "POST"]
    assert call["path"] == SILENCES
    body = call["json"]
    # alertname alone could match an unrelated rule of the same name
    assert body["matchers"] == [{"name": "alertname", "value": "queue-backlog",
                                 "isRegex": False, "isEqual": True},
                                {"name": "orbit_monitor", "value": "queue-backlog",
                                 "isRegex": False, "isEqual": True}]
    assert body["createdBy"] == "desk-op"
    assert body["comment"] == "load test in progress"
    start = datetime.fromisoformat(body["startsAt"].replace("Z", "+00:00"))
    end = datetime.fromisoformat(body["endsAt"].replace("Z", "+00:00"))
    assert (end - start).total_seconds() == 7200


@pytest.mark.parametrize("duration", ["0m", "8d", "2 hours", "90s"])
async def test_silence_refuses_a_duration_outside_one_minute_to_seven_days(fake_backends, http, duration):
    world(fake_backends, monitors=[metric()])
    async with sdk_client(http, mint(fake_backends, Role.operator)) as client:
        r = await client.call_tool("orbit_monitor_silence", {
            "name": "queue-backlog", "duration": duration, "reason": "x"})
    assert r.is_error
    assert not [c for c in fake_backends.backends.alertmanager.calls if c["method"] == "POST"]


async def test_silence_refuses_an_unknown_monitor_and_an_empty_reason(fake_backends, http):
    world(fake_backends, monitors=[metric()])
    async with sdk_client(http, mint(fake_backends, Role.operator)) as client:
        ghost = await client.call_tool("orbit_monitor_silence", {
            "name": "ghost", "duration": "1h", "reason": "x"})
        empty = await client.call_tool("orbit_monitor_silence", {
            "name": "queue-backlog", "duration": "1h", "reason": ""})
    assert ghost.is_error and ghost.structured_content["code"] == "not_found"
    assert empty.is_error
    assert not [c for c in fake_backends.backends.alertmanager.calls if c["method"] == "POST"]


async def test_a_refused_probe_target_is_not_echoed_by_upsert(fake_backends, http):
    runner = world(fake_backends)
    bad = probe(target="https://user:REVIEW_SECRET@example.com/")
    async with sdk_client(http, mint(fake_backends, Role.operator), confirm=True) as client:
        r = await client.call_tool("orbit_monitor_upsert", {"monitor": bad, "dry_run": True})
    assert r.is_error
    assert "target" in text_of(r)
    assert "REVIEW_SECRET" not in text_of(r)
    assert runner.calls == []


async def test_the_caller_is_told_who_hears_the_monitor_fire(fake_backends, http):
    """A monitor with no `notify` messages nobody, and the answer says so; with telegram
    it says the chat. An agent asked for "a monitor that alerts" made a silent one and
    nothing it had read told it (docs/V1.md §8, S10a on v0.34.1)."""
    world(fake_backends)
    async with sdk_client(http, mint(fake_backends, Role.operator)) as client:
        without = {k: v for k, v in metric().items() if k != "notify"}
        silent = await client.call_tool("orbit_monitor_upsert", {"monitor": without, "dry_run": True})
        empty = await client.call_tool("orbit_monitor_upsert", {"monitor": dict(without, notify=[]), "dry_run": True})
        loud = await client.call_tool("orbit_monitor_upsert",
                                      {"monitor": dict(without, notify=["telegram"]), "dry_run": True})
        listed = {tool.name: tool for tool in (await client.list_tools()).tools}
    quiet = silent.structured_content
    assert "nobody is messaged" in quiet["evidence"]["reaches"]
    assert "nobody is messaged" in quiet["steps"][-1]["detail"]
    assert "nobody is messaged" in empty.structured_content["evidence"]["reaches"]
    assert "sent to the human chat (telegram)" in loud.structured_content["evidence"]["reaches"]
    described = listed["orbit_monitor_upsert"].description
    assert "`notify`" in described and "nobody" in described and "alerts-telegram" in described



# ------------------------------------------------------------------ what a monitor is, and its silences

ACTIVE = [
    {"id": "11111111-aaaa-4bbb-8ccc-000000000001", "status": {"state": "active"}, "endsAt": "2026-10-12T12:00:00Z",
     "createdBy": "desk-op", "comment": "load test",
     "matchers": [{"name": "alertname", "value": "queue-backlog", "isRegex": False, "isEqual": True},
                  {"name": "orbit_monitor", "value": "queue-backlog", "isRegex": False, "isEqual": True}]},
    {"id": "11111111-aaaa-4bbb-8ccc-000000000002", "status": {"state": "expired"}, "endsAt": "2026-10-11T12:00:00Z",
     "createdBy": "desk-op", "comment": "over",
     "matchers": [{"name": "orbit_monitor", "value": "queue-backlog", "isRegex": False, "isEqual": True}]},
    {"id": "11111111-aaaa-4bbb-8ccc-000000000003", "status": {"state": "active"}, "endsAt": "2026-10-12T12:00:00Z",
     "createdBy": "someone", "comment": "not a monitor's",
     "matchers": [{"name": "alertname", "value": "KubeJobFailed", "isRegex": False, "isEqual": True}]},
    {"id": "11111111-aaaa-4bbb-8ccc-000000000004", "status": {"state": "active"}, "endsAt": "2026-10-12T12:00:00Z",
     "createdBy": "desk-op", "comment": "the other one",
     "matchers": [{"name": "orbit_monitor", "value": "keep", "isRegex": False, "isEqual": True}]},
]


async def test_status_gives_back_the_whole_definition_and_the_silences_in_force(fake_backends, http):
    """QA of v1.0.0, F38 and F15: no tool returned a monitor's query, threshold or
    notify, and a silence could not be listed: it only ran out."""
    body = metric(notify=["telegram"])
    world(fake_backends, monitors=[body, metric(name="keep", notify=[])])
    fake_backends.backends.alertmanager.responses[("GET", SILENCES)] = ACTIVE
    async with sdk_client(http, mint(fake_backends, Role.viewer)) as client:
        one = await client.call_tool("orbit_monitor_status", {"name": "queue-backlog"})
        listed = await client.call_tool("orbit_monitor_list", {})
    (item,) = one.structured_content["items"]
    assert item["definition"] == body, "the definition is the file: what upsert takes back"
    assert item["silences"] == [{"id": "11111111-aaaa-4bbb-8ccc-000000000001", "ends_at": "2026-10-12T12:00:00Z",
                                 "created_by": "desk-op", "comment": "load test"}]
    by_name = {i["name"]: i for i in listed.structured_content["items"]}
    assert by_name["queue-backlog"]["notify"] == ["telegram"] and by_name["keep"]["notify"] == []


async def test_status_still_answers_when_alertmanager_does_not(fake_backends, http):
    from orbitd.backends import BackendError
    world(fake_backends, monitors=[metric()])
    fake_backends.backends.alertmanager.responses[("GET", SILENCES)] = BackendError(
        status=503, body="down", what_failed="ORBITD_ALERTMANAGER_URL returned 503")
    async with sdk_client(http, mint(fake_backends, Role.viewer)) as client:
        r = await client.call_tool("orbit_monitor_status", {"name": "queue-backlog"})
    assert not r.is_error
    assert r.structured_content["items"][0]["silences"] is None


async def test_unsilence_expires_this_monitors_silences_in_force_and_no_others(fake_backends, http, caplog):
    world(fake_backends, monitors=[metric(), metric(name="keep")])
    am = fake_backends.backends.alertmanager
    am.responses[("GET", SILENCES)] = ACTIVE
    with caplog.at_level("INFO"):
        async with sdk_client(http, mint(fake_backends, Role.operator, "desk-op")) as client:
            r = await client.call_tool("orbit_monitor_unsilence", {"name": "queue-backlog"})
    assert not r.is_error, r.content
    assert r.structured_content["status"] == "applied"
    assert r.structured_content["evidence"]["lifted"] == ["11111111-aaaa-4bbb-8ccc-000000000001"]
    deleted = [c["path"] for c in am.calls if c["method"] == "DELETE"]
    assert deleted == ["/api/v2/silence/11111111-aaaa-4bbb-8ccc-000000000001"]
    assert any(" orbit_monitor_unsilence " in rec.getMessage() and rec.getMessage().endswith(": applied")
               for rec in caplog.records)


async def test_unsilence_of_a_monitor_that_is_not_silenced_lifts_nothing(fake_backends, http):
    world(fake_backends, monitors=[metric()])
    am = fake_backends.backends.alertmanager
    am.responses[("GET", SILENCES)] = []
    async with sdk_client(http, mint(fake_backends, Role.operator)) as client:
        r = await client.call_tool("orbit_monitor_unsilence", {"name": "queue-backlog"})
        ghost = await client.call_tool("orbit_monitor_unsilence", {"name": "ghost"})
    async with sdk_client(http, mint(fake_backends, Role.viewer)) as client:
        denied = await client.call_tool("orbit_monitor_unsilence", {"name": "queue-backlog"})
    assert r.structured_content["evidence"]["lifted"] == []
    assert "was not silenced" in r.structured_content["steps"][0]["detail"]
    assert ghost.is_error and ghost.structured_content["code"] == "not_found"
    assert denied.is_error and denied.structured_content["code"] == "FORBIDDEN"
    assert not [c for c in am.calls if c["method"] == "DELETE"]


async def test_unsilence_never_puts_an_id_that_is_not_an_ids_shape_into_a_path(fake_backends, http):
    world(fake_backends, monitors=[metric()])
    am = fake_backends.backends.alertmanager
    am.responses[("GET", SILENCES)] = [dict(ACTIVE[0], id="../../api/v2/alerts")]
    async with sdk_client(http, mint(fake_backends, Role.operator)) as client:
        r = await client.call_tool("orbit_monitor_unsilence", {"name": "queue-backlog"})
    assert r.structured_content["evidence"]["lifted"] == []
    assert not [c for c in am.calls if c["method"] == "DELETE"]


async def test_a_log_monitors_state_comes_from_the_loki_ruler(fake_backends, http):
    """On the test box (v1.0.1-rc1) a log monitor read `inactive` while its alert was
    firing: its rule is evaluated by the Loki ruler and is not among Prometheus's."""
    from test_monitors import log as log_monitor
    from orbitd.backends import BackendError
    world(fake_backends, monitors=[metric(), log_monitor()])
    rules = lambda *names: {"status": "success", "data": {"groups": [{"name": "g", "rules": [  # noqa: E731
        {"type": "alerting", "name": n, "state": st} for n, st in names]}]}}
    fake_backends.backends.prom.responses["/api/v1/rules"] = rules(("queue-backlog", "pending"))
    fake_backends.backends.loki.responses["/prometheus/api/v1/rules"] = rules(("error-burst", "firing"))
    async with sdk_client(http, mint(fake_backends, Role.viewer)) as client:
        every = await client.call_tool("orbit_monitor_status", {})
        one = await client.call_tool("orbit_monitor_status", {"name": "queue-backlog"})
    states = {i["name"]: i["state"] for i in every.structured_content["items"]}
    assert states == {"queue-backlog": "pending", "error-burst": "firing"}
    # a metric monitor alone does not ask Loki at all
    asked = [c["path"] for c in fake_backends.backends.loki.calls]
    assert asked.count("/prometheus/api/v1/rules") == 1 and one.structured_content["items"][0]["state"] == "pending"
    # a Loki that does not answer is not an error of the tool
    fake_backends.backends.loki.responses["/prometheus/api/v1/rules"] = BackendError(
        status=503, body="down", what_failed="ORBITD_LOKI_URL returned 503")
    async with sdk_client(http, mint(fake_backends, Role.viewer)) as client:
        again = await client.call_tool("orbit_monitor_status", {"name": "error-burst"})
    assert not again.is_error and again.structured_content["items"][0]["state"] == "inactive"
