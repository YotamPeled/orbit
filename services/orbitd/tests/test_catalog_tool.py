"""orbit_catalog_set (docs/V1.md M2c), offline: who may change a catalog entry, what is
refused before anything is written, the confirmation that names what changes, the one
commit with its ledger words, and what the tool says when the generator or the commit
gate refuses the entry. The splice itself is scripts/test_catalog_edit.py; the commit is
scripts/test_catalog_commit.py."""

from __future__ import annotations

import copy
import json
from dataclasses import replace

import pytest
from mcp.shared.exceptions import MCPError
from mcp_types import ElicitResult, InputRequiredResult

from orbitd import deploy as deploy_module
from orbitd import source as source_module
from orbitd.roles import CheckoutSource, Role, Step
from orbitd.testing import FakeRunner
from orbitd.tools_operator import catalog as catalog_tool

from conftest import mint, sdk_client
from test_tools_operator import install, wait_result
from test_tools_viewer_kube import seed_catalog

pytestmark = pytest.mark.anyio

COMMIT = "0123456789abcdef0123456789abcdef01234567"
DEMO = {"id": "demo", "name": "Demo", "environments": {"prod": {}, "dev": {"enabled": True}},
        "components": [{"name": "api", "kind": "web", "image": "mirror.invalid/demo-api", "port": 8080},
                       {"name": "worker", "kind": "worker", "image": "mirror.invalid/demo-worker"}]}
OTHER = {"id": "other", "environments": {"prod": {}},
         "components": [{"name": "web", "kind": "web", "image": "mirror.invalid/other-web"}]}
YES = {"confirm": ElicitResult(action="accept", content={"confirm": True})}


def wanted() -> dict:
    """The S09 change: two api replicas, more memory in dev only, one more hostname."""
    entry = copy.deepcopy(DEMO)
    entry["components"][0]["replicas"] = 2
    entry["environments"]["dev"]["memory"] = {"api": "384Mi"}
    entry["environments"]["prod"]["hostnames"] = ["demo-two.fixture.invalid"]
    return entry


def world(state, monkeypatch, *, runner=None, gate=None):
    seed_catalog(state, [DEMO, OTHER])
    at_commit = lambda state, *, now=None: CheckoutSource(commit=COMMIT, age_s=0, lag_note="test")  # noqa: E731
    monkeypatch.setattr(deploy_module, "read_source", at_commit)
    monkeypatch.setattr(source_module, "read_head", at_commit)
    monkeypatch.setattr(catalog_tool, "read_source", at_commit)
    if gate is not None:
        monkeypatch.setattr(deploy_module, "_gate_step", lambda state: gate)
    rt, clock = install(state, runner=runner)
    state.cfg = replace(state.cfg, apply_mode="on")
    return rt, clock


# ---------------------------------------------------------------- who, and the confirmation

async def test_a_viewer_may_not_change_the_catalog(fake_backends, http, monkeypatch):
    rt, _ = world(fake_backends, monkeypatch)
    async with sdk_client(http, mint(fake_backends, Role.viewer), confirm=True) as client:
        r = await client.call_tool("orbit_catalog_set", {"project": "demo", "entry": wanted()})
    assert r.is_error and r.structured_content["code"] == "FORBIDDEN"
    assert r.structured_content["evidence"]["required_role"] == "operator"
    assert rt.runner.calls == []


async def test_the_confirmation_names_every_path_that_changes_and_nothing_is_written_without_it(fake_backends, http, monkeypatch):
    rt, _ = world(fake_backends, monkeypatch)
    async with sdk_client(http, mint(fake_backends, Role.operator)) as client:
        first = await client.session.call_tool("orbit_catalog_set", {"project": "demo", "entry": wanted()},
                                               allow_input_required=True)
    assert isinstance(first, InputRequiredResult)
    message = first.input_requests["confirm"].params.message
    for path in ("components[api].replicas (added)", "environments.dev.memory (added)", "environments.prod.hostnames (added)"):
        assert path in message, message
    assert "production included" in message
    assert rt.runner.calls == []
    async with sdk_client(http, mint(fake_backends, Role.operator), confirm=None) as client:
        r = await client.call_tool("orbit_catalog_set", {"project": "demo", "entry": wanted()})
    assert r.structured_content["status"] == "refused"
    assert rt.runner.calls == []


async def test_a_confirmation_cannot_be_replayed_for_another_entry_or_by_another_caller(fake_backends, http, monkeypatch):
    rt, _ = world(fake_backends, monkeypatch)
    other = wanted()
    other["components"][0]["replicas"] = 9
    async with sdk_client(http, mint(fake_backends, Role.operator, "first")) as client:
        first = await client.session.call_tool("orbit_catalog_set", {"project": "demo", "entry": wanted()},
                                               allow_input_required=True)
        with pytest.raises(MCPError):
            await client.session.call_tool("orbit_catalog_set", {"project": "demo", "entry": other},
                                           input_responses=YES, request_state=first.request_state)
    async with sdk_client(http, mint(fake_backends, Role.operator, "second")) as client:
        with pytest.raises(MCPError):
            await client.session.call_tool("orbit_catalog_set", {"project": "demo", "entry": wanted()},
                                           input_responses=YES, request_state=first.request_state)
    assert rt.runner.calls == []


# ---------------------------------------------------------------- refused before any write

async def test_what_is_not_this_projects_whole_entry_is_refused(fake_backends, http, monkeypatch):
    rt, _ = world(fake_backends, monkeypatch)
    async with sdk_client(http, mint(fake_backends, Role.admin), confirm=True) as client:
        unknown = await client.call_tool("orbit_catalog_set", {"project": "ghost", "entry": {"id": "ghost"}})
        renamed = await client.call_tool("orbit_catalog_set", {"project": "demo", "entry": dict(wanted(), id="other")})
        no_id = await client.call_tool("orbit_catalog_set", {"project": "demo", "entry": {"replicas": 2}})
        huge = await client.call_tool("orbit_catalog_set", {"project": "demo", "entry": dict(wanted(), blurb="x" * 70000)})
    assert unknown.structured_content["code"] == "not_found"
    for r in (renamed, no_id):
        assert r.is_error and r.structured_content["code"] == "refused"
        assert "an id cannot change" in r.structured_content["what_failed"]
    assert huge.structured_content["code"] == "refused" and "larger than" in huge.structured_content["what_failed"]
    assert rt.runner.calls == []


async def test_a_dry_run_lists_the_changes_and_writes_nothing(fake_backends, http, monkeypatch):
    rt, _ = world(fake_backends, monkeypatch)
    async with sdk_client(http, mint(fake_backends, Role.operator)) as client:
        r = await client.call_tool("orbit_catalog_set", {"project": "demo", "entry": wanted(), "dry_run": True})
    body = r.structured_content
    assert body["status"] == "dry_run"
    assert body["evidence"]["changed"] == ["environments.prod.hostnames (added)", "environments.dev.memory (added)",
                                           "components[api].replicas (added)"]
    # nothing runs the commit script: the check is made in this process
    assert rt.runner.calls == []
    # this fixture's orbitd is a development build (no version, no copy of core): it has
    # no commit gate, and the dry run says the entry was not checked rather than "ok"
    assert [(s["name"], s["status"]) for s in body["steps"]] == [("check", "skipped")]
    assert "not checked" in body["steps"][0]["detail"]


async def test_a_dry_run_says_what_the_generator_or_the_gate_would_refuse(fake_backends, http, monkeypatch):
    """QA of v1.0.0, F22: thirteen invalid entries each answered `dry_run`, as if fine."""
    sentence = "fit: the catalog reserves 70000 Mi for projects; the node has 15000 Mi to give"
    rt, _ = world(fake_backends, monkeypatch)
    asked = []
    monkeypatch.setattr(catalog_tool, "precheck_entry",
                        lambda state, project, entry: (asked.append((project, entry)), (False, sentence))[1])
    async with sdk_client(http, mint(fake_backends, Role.operator)) as client:
        r = await client.call_tool("orbit_catalog_set", {"project": "demo", "entry": wanted(), "dry_run": True})
    body = r.structured_content
    assert body["status"] == "failed"
    assert body["steps"] == [{"name": "check", "status": "failed", "detail": sentence}]
    assert body["evidence"]["changed"]
    assert asked == [("demo", wanted())] and rt.runner.calls == []
    # and an entry that passes is said to pass
    monkeypatch.setattr(catalog_tool, "precheck_entry", lambda state, project, entry: (True, ""))
    async with sdk_client(http, mint(fake_backends, Role.operator)) as client:
        r = await client.call_tool("orbit_catalog_set", {"project": "demo", "entry": wanted(), "dry_run": True})
    assert r.structured_content["status"] == "dry_run"
    assert [(s["name"], s["status"]) for s in r.structured_content["steps"]] == [("check", "ok")]


async def test_what_the_gate_would_refuse_is_not_pushed(fake_backends, http, monkeypatch):
    """QA of v1.0.0, F26: an entry the generator accepts and the gate refuses used to land
    on main and be refused there, and a refused main blocks every deploy. It is checked
    before the commit script runs; refused, the script never runs."""
    sentence = "fit: the catalog reserves 70000 Mi for projects; the node has 15000 Mi to give"
    rt, _ = world(fake_backends, monkeypatch)
    monkeypatch.setattr(catalog_tool, "precheck_entry", lambda state, project, entry: (False, sentence))
    async with sdk_client(http, mint(fake_backends, Role.operator), confirm=True) as client:
        running = await client.call_tool("orbit_catalog_set", {"project": "demo", "entry": wanted()})
        result, _ = await wait_result(client, running)
    assert result["status"] == "failed" and result["commit"] is None
    assert result["steps"][-1] == {"name": "commit", "status": "failed", "detail": sentence}
    assert rt.runner.calls == [], "the commit script ran for an entry the gate would refuse"


class _Refused:
    enabled, refused, blocks, undecided = True, True, True, False
    head, good, check = "f" * 40, COMMIT, "fit"
    sentence = "the catalog reserves more than the node has"


async def test_the_last_accepted_entry_is_committed_over_a_refused_main(fake_backends, http, monkeypatch):
    """QA of v1.0.0, F26: main's newest commit is refused, orbitd reads the last accepted
    entry, and passing that entry back answered `unchanged` and wrote nothing."""
    from orbitd import gate as gate_module
    rt, _ = world(fake_backends, monkeypatch, runner=FakeRunner(commit=COMMIT))
    monkeypatch.setattr(gate_module.CommitGate, "evaluate", lambda self: _Refused())
    monkeypatch.setattr(deploy_module, "_gate_step", lambda state: None)
    async with sdk_client(http, mint(fake_backends, Role.operator)) as client:
        first = await client.session.call_tool("orbit_catalog_set", {"project": "demo", "entry": copy.deepcopy(DEMO)},
                                               allow_input_required=True)
    assert isinstance(first, InputRequiredResult), "the same entry was called unchanged on a refused main"
    message = first.input_requests["confirm"].params.message
    assert "ffffffffffff" in message and "committed over the refused one" in message
    assert rt.runner.calls == []
    async with sdk_client(http, mint(fake_backends, Role.operator), confirm=True) as client:
        running = await client.call_tool("orbit_catalog_set", {"project": "demo", "entry": copy.deepcopy(DEMO)})
        result, _ = await wait_result(client, running)
    assert result["status"] == "applied", result
    assert result["evidence"]["restores"] == "ffffffffffff"
    (call,) = rt.runner.calls
    assert call["argv"] == [rt.catalog_script, "set", "demo"]


async def test_the_same_entry_is_applied_unchanged_with_no_commit(fake_backends, http, monkeypatch):
    rt, _ = world(fake_backends, monkeypatch)
    async with sdk_client(http, mint(fake_backends, Role.operator)) as client:
        r = await client.call_tool("orbit_catalog_set", {"project": "demo", "entry": copy.deepcopy(DEMO)})
    assert r.structured_content["status"] == "applied" and r.structured_content["evidence"]["unchanged"] is True
    assert rt.runner.calls == []


# ---------------------------------------------------------------- the accepted change

async def test_an_accepted_change_is_one_commit_with_the_ledger_words(fake_backends, http, monkeypatch, caplog, tmp_path):
    seen = {}

    class Runner(FakeRunner):
        def run(self, argv, extra_env):
            # the entry travels in a file the script reads, never on the command line
            seen["entry"] = json.loads(open(extra_env["CATALOG_ENTRY_FILE"], encoding="utf-8").read())
            seen["file"] = extra_env["CATALOG_ENTRY_FILE"]
            return super().run(argv, extra_env)

    rt, _ = world(fake_backends, monkeypatch, runner=Runner(commit=COMMIT))
    with caplog.at_level("INFO"):
        async with sdk_client(http, mint(fake_backends, Role.operator, "ada"), confirm=True) as client:
            running = await client.call_tool("orbit_catalog_set", {"project": "demo", "entry": wanted()})
            result, _ = await wait_result(client, running)
    assert result["status"] == "applied", result
    assert [(s["name"], s["status"]) for s in result["steps"]] == [("confirm", "ok"), ("commit", "ok"), ("checkout", "ok")]
    assert result["commit"] == COMMIT
    (call,) = rt.runner.calls
    assert call["argv"] == [rt.catalog_script, "set", "demo"]
    assert {k: v for k, v in call["env"].items() if k != "CATALOG_ENTRY_FILE"} == {
        "ORBIT_ACTION": "catalog-set", "ORBIT_ACTOR": "ada", "ORBIT_ROLE": "operator"}
    assert seen["entry"] == wanted()
    import os
    assert not os.path.exists(seen["file"]), "the entry's temporary file is removed"
    applied = [r.getMessage() for r in caplog.records if " orbit_catalog_set " in r.getMessage() and r.getMessage().endswith(": applied")]
    assert len(applied) == 1


async def test_an_entry_the_generator_refuses_fails_the_commit_step_with_its_sentence(fake_backends, http, monkeypatch):
    sentence = "widgets: components[0].kind 'daemon' is not one of web, worker, cron"
    rt, _ = world(fake_backends, monkeypatch, runner=FakeRunner(
        returncode=1, stdout=f"STEP args OK\nSTEP config OK\nSTEP render FAIL\n{sentence}\n"))
    async with sdk_client(http, mint(fake_backends, Role.operator), confirm=True) as client:
        running = await client.call_tool("orbit_catalog_set", {"project": "demo", "entry": wanted()})
        result, _ = await wait_result(client, running)
    assert result["status"] == "failed"
    assert result["steps"][-1] == {"name": "commit", "status": "failed", "detail": sentence}
    assert result["commit"] is None


async def test_a_commit_the_gate_refuses_fails_the_checkout_step_with_the_checks_sentence(fake_backends, http, monkeypatch):
    refusal = Step(name="apply", status="failed",
                   detail="the commit gate refused this commit (check fit): demo-prod reserves more memory than the node has")
    rt, _ = world(fake_backends, monkeypatch, gate=refusal)
    async with sdk_client(http, mint(fake_backends, Role.operator), confirm=True) as client:
        running = await client.call_tool("orbit_catalog_set", {"project": "demo", "entry": wanted()})
        result, _ = await wait_result(client, running)
    assert result["status"] == "failed"
    last = result["steps"][-1]
    assert last["name"] == "checkout" and last["status"] == "failed"
    assert "check fit" in last["detail"]


# ---------------------------------------------------------------- the comparison

def test_changed_names_paths_a_person_can_read():
    old, new = copy.deepcopy(DEMO), wanted()
    assert catalog_tool.changed(old, old) == []
    new["components"].append({"name": "cron", "kind": "cron", "image": "x"})
    assert "components" in catalog_tool.changed(old, new), "a list that grew is named whole"
    del new["name"]
    assert "name (removed)" in catalog_tool.changed(old, new)
