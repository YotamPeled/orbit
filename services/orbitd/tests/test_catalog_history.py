"""orbit_catalog_history (QA of v1.0.0, F31): where a change of configuration is found.

On the test box a setting was changed that made a scheduled job delete its data. The
ledger and the deploy history list image tags only, so the change that caused the loss
was in neither, and the restore skill's "find the destructive deploy in the ledger" found
nothing. This tool reads the catalog's own history: the commits that changed one
project's entry, when, by whom, and which fields."""

from __future__ import annotations

import copy
import subprocess

import pytest
import yaml
from conftest import mint, sdk_client

from orbitd.roles import Role

pytestmark = pytest.mark.anyio

DEMO = {"id": "demo", "environments": {"prod": {}, "dev": {"enabled": True}},
        "components": [{"name": "web", "kind": "web", "image": "x/web", "memory": "128Mi"},
                       {"name": "janitor", "kind": "cron", "image": "x/janitor", "schedule": "0 * * * *",
                        "env": {"KEEP_DAYS": "30"}}]}
OTHER = {"id": "other", "environments": {"prod": {}}, "components": [{"name": "web", "kind": "web", "image": "x/o"}]}


def commit(state, projects, message, *, trailers=()):
    root = state.backends.instance_root
    (root / "catalog" / "projects.yaml").write_text(yaml.safe_dump({"version": 3, "projects": projects}, sort_keys=False))
    git = ["git", "-C", str(root), "-c", "user.name=t", "-c", "user.email=t@example.test", "-c", "commit.gpgsign=false"]
    subprocess.run(git + ["add", "-A", "catalog"], check=True, capture_output=True)
    argv = git + ["commit", "-q", "--allow-empty", "-m", message]
    for trailer in trailers:
        argv += ["--trailer", trailer]
    subprocess.run(argv, check=True, capture_output=True)
    return subprocess.run(git + ["rev-parse", "HEAD"], check=True, capture_output=True, text=True).stdout.strip()


async def history(http, state, project, **kw):
    async with sdk_client(http, mint(state, Role.viewer)) as client:
        return await client.call_tool("orbit_catalog_history", {"project": project, **kw})


async def test_it_lists_the_commits_that_changed_the_entry_and_which_fields(fake_backends, http):
    state = fake_backends
    added = commit(state, [DEMO, OTHER], "onboard demo")
    bigger = copy.deepcopy(DEMO)
    bigger["components"][0]["memory"] = "256Mi"
    grew = commit(state, [bigger, OTHER], "catalog: set demo",
                  trailers=("Orbit-Action: catalog-set", "Orbit-Actor: ada", "Orbit-Role: operator"))
    # a commit that changes ANOTHER project's entry is not part of this one's history
    theirs = copy.deepcopy(OTHER)
    theirs["components"][0]["memory"] = "512Mi"
    commit(state, [bigger, theirs], "catalog: set other", trailers=("Orbit-Actor: bob",))
    destructive = copy.deepcopy(bigger)
    destructive["components"][1]["env"]["KEEP_DAYS"] = "0"
    destructive["environments"]["dev"]["hostnames"] = ["demo2.dev.example.test"]
    lost = commit(state, [destructive, theirs], "catalog: set demo",
                  trailers=("Orbit-Action: catalog-set", "Orbit-Actor: grace"))
    r = await history(http, state, "demo")
    assert not r.is_error, r.content
    items = r.structured_content["items"]
    assert [i["commit"] for i in items] == [lost, grew, added], "newest first, and only this project's commits"
    newest, middle, first = items
    assert newest["what"] == "changed" and newest["actor"] == "grace" and newest["action"] == "catalog-set"
    assert newest["changed"] == ["environments.dev.hostnames (added)", "components[janitor].env.KEEP_DAYS"]
    assert "0" not in newest["changed"] and newest["at"].startswith("20")
    assert middle["changed"] == ["components[web].memory"] and middle["actor"] == "ada"
    assert first["what"] == "added" and first["changed"] == [] and "actor" not in first
    theirs_history = (await history(http, state, "other")).structured_content["items"]
    assert [i["what"] for i in theirs_history] == ["changed", "added"]
    assert theirs_history[0]["changed"] == ["components[web].memory (added)"]


async def test_a_removal_is_in_the_history_of_a_project_that_is_gone(fake_backends, http):
    state = fake_backends
    commit(state, [DEMO, OTHER], "onboard")
    gone = commit(state, [OTHER], "catalog: remove demo", trailers=("Orbit-Action: catalog-remove", "Orbit-Actor: root"))
    r = await history(http, state, "demo")
    items = r.structured_content["items"]
    assert [(i["what"], i.get("actor")) for i in items] == [("removed", "root"), ("added", None)]
    assert items[0]["commit"] == gone
    ghost = await history(http, state, "never-was")
    assert ghost.is_error and ghost.structured_content["code"] == "not_found"


async def test_it_pages(fake_backends, http):
    state = fake_backends
    entry = copy.deepcopy(DEMO)
    commit(state, [entry], "onboard")
    for size in ("192Mi", "256Mi", "320Mi"):
        entry = copy.deepcopy(entry)
        entry["components"][0]["memory"] = size
        commit(state, [entry], f"web {size}")
    first = (await history(http, state, "demo", limit=2)).structured_content
    assert first["returned"] == 2 and first["total"] == 4 and first["next_cursor"] == "2"
    rest = (await history(http, state, "demo", limit=2, cursor=first["next_cursor"])).structured_content
    assert [i["what"] for i in rest["items"]] == ["changed", "added"] and rest["next_cursor"] is None
    bad = await history(http, state, "demo", cursor="x")
    assert bad.is_error and bad.structured_content["code"] == "invalid_cursor"
