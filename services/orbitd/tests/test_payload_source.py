"""Checkout `source` on desired-state payloads, and evidence.components stages."""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest
from conftest import mint, sdk_client

from orbitd.deploy import execute_ship
from orbitd.roles import Role, Step
from orbitd.shadow import ShadowReport
from orbitd.source import LAG_NOTE
from orbitd.testing import FakeRunner

from test_tools_operator import (
    CATALOG, COMMIT, ENV, PROJECT, TAG, WORKER, install, seed_world, wait_result,
)
from test_tools_viewer_kube import seed_catalog
from test_tools_viewer_platform import RENDER_YAML, seed_instance

pytestmark = pytest.mark.anyio

SHA_RE = re.compile(r"^[0-9a-f]{40}$")
SOURCE_KEYS = {"commit", "age_s", "lag_note"}


def _head(root: Path) -> str:
    return subprocess.check_output(
        ["git", "-C", str(root), "rev-parse", "HEAD"], text=True,
    ).strip()


def assert_source(payload: dict, root: Path, period: int = 60) -> None:
    source = payload["source"]
    assert SOURCE_KEYS <= set(source)
    assert SHA_RE.fullmatch(source["commit"]), source["commit"]
    assert isinstance(source["age_s"], (int, float)) and source["age_s"] >= 0
    assert source["lag_note"] == LAG_NOTE.format(n=period)
    assert source["commit"] == _head(root)


def _by_name(evidence: dict) -> dict:
    rows = evidence.get("components") or []
    assert isinstance(rows, list), rows
    return {row["name"]: row for row in rows}


async def test_render_get_payload_includes_source(fake_backends, http):
    seed_instance(Path(fake_backends.backends.instance.root))
    async with sdk_client(http, mint(fake_backends, Role.viewer)) as client:
        rendered = await client.call_tool(
            "orbit_render_get", {"project": "demo", "env": "prod"},
        )
    assert not rendered.is_error, rendered.content
    body = rendered.structured_content
    assert body["yaml"] == RENDER_YAML
    assert_source(body, Path(fake_backends.backends.instance.root))


async def test_platform_diff_payload_includes_source(fake_backends, http, monkeypatch):
    seed_catalog(fake_backends, [CATALOG])

    def fake_diff(state, project, env):
        return ShadowReport(project=project, env=env, empty=True, render_sha="abc")

    monkeypatch.setattr("orbitd.shadow.diff", fake_diff)
    async with sdk_client(http, mint(fake_backends, Role.admin)) as client:
        result = await client.call_tool(
            "orbit_platform_diff", {"project": PROJECT, "env": ENV},
        )
    assert not result.is_error, result.content
    body = result.structured_content
    assert body["returned"] == 1
    assert "source" not in body["items"][0]
    assert_source(body, Path(fake_backends.backends.instance.root))


async def test_ship_mutation_payload_includes_source(fake_backends, http):
    """The checkout source rides every terminal mutation, including a refusal for
    a skipped apply: the bump's commit is still recorded on the component."""
    install(fake_backends)
    seed_world(fake_backends)
    async with sdk_client(http, mint(fake_backends, Role.operator), confirm=True) as client:
        running = await client.call_tool("orbit_deploy_ship", {
            "project": PROJECT, "env": ENV, "components": [WORKER], "tag": TAG,
        })
        result, _ = await wait_result(client, running)
    assert result["status"] == "refused", result
    assert_source(result, Path(fake_backends.backends.instance.root))
    comps = _by_name(result["evidence"])
    assert comps[WORKER]["stage"] == "refused"
    assert comps[WORKER]["commit"] == COMMIT


def test_ship_bump_failure_marks_every_component_not_reached(fake_backends):
    install(fake_backends, runner=FakeRunner(returncode=1, stdout="bump FAIL rc=1\n"))
    seed_world(fake_backends)
    result = execute_ship(
        fake_backends, project=PROJECT, env=ENV,
        components=list(CATALOG["components"]),
        tag=TAG, action="ship", actor="desk-op", expect=None,
        opening=[Step(name="confirm", status="ok")],
    )
    assert result.status == "failed"
    comps = _by_name(result.evidence)
    assert set(comps) == {"web", WORKER}
    assert all(row["stage"] == "not_reached" for row in comps.values())
    assert_source(result.model_dump(), Path(fake_backends.backends.instance.root))


def test_ship_second_rollout_failure_keeps_first_asserted(fake_backends, monkeypatch):
    """Two components, the second one's rollout fails: the first stays asserted.
    The switch is on so both components reach rollout past the shared apply step."""
    from test_reconcile import FakeSource, OLD, rendered, set_mode, use_render

    set_mode(fake_backends, "on")
    use_render(monkeypatch, rendered())
    monkeypatch.setattr("orbitd.deploy.read_source", FakeSource(OLD, COMMIT, stale=0))
    install(fake_backends)
    seed_world(fake_backends)
    web, worker = CATALOG["components"]
    result = execute_ship(
        fake_backends, project=PROJECT, env=ENV,
        components=[worker, web],
        tag=TAG, action="ship", actor="desk-op", expect=None,
        opening=[Step(name="confirm", status="ok")],
    )
    assert result.status == "failed"
    comps = _by_name(result.evidence)
    assert comps[WORKER]["stage"] == "asserted"
    assert comps["web"]["stage"] == "failed"
    assert comps[WORKER].get("commit") == COMMIT
    assert result.evidence.get("verdict") == "LANDED NOT DEPLOYED rollout"


def test_tool_descriptions_mention_source(fake_backends):
    from orbitd.roles import Registry
    from orbitd.tools_operator import register as register_operator
    from orbitd.tools_viewer import register as register_viewer

    registry = Registry()
    register_viewer(registry, fake_backends)
    register_operator(registry, fake_backends)
    for name in ("orbit_render_get", "orbit_platform_diff",
                 "orbit_deploy_ship", "orbit_deploy_rollback",
                 "orbit_workload_restart"):
        text = registry.get(name).description
        assert "source" in text, name


def test_gitsync_sidecar_sets_age(fake_backends):
    from dataclasses import replace

    from orbitd.source import read_source

    root = Path(fake_backends.backends.instance.root)
    (root / ".git" / "git-sync").write_text('{"synced": 1710000000, "hash": "abc"}\n')
    fake_backends.cfg = replace(fake_backends.cfg, source_sync_period_s=15)
    source = read_source(fake_backends, now=1710000012)
    assert source.age_s == 12
    assert source.lag_note == LAG_NOTE.format(n=15)
    assert SHA_RE.fullmatch(source.commit)
