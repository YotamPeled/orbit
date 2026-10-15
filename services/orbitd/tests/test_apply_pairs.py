"""ORBITD_APPLY_PAIRS: per-pair apply switch, loop skip, apply-step skip, render_path."""

from __future__ import annotations

import logging
from dataclasses import replace
from pathlib import Path

import pytest
from conftest import mint, sdk_client

from orbitd import applier
from orbitd.applier import PAIR_NOT_ENABLED_DETAIL, PAIR_NOT_ENABLED_MODE
from orbitd.reconcile import ReconcileRunner, build_reconcile_runner
from orbitd.roles import Role
from orbitd.tools_viewer.render_get import RenderConflict, render_path

from test_reconcile import ENV, PROJECT, install, rendered, seed_world, set_mode, ship, step, use_render

pytestmark = pytest.mark.anyio


def set_pairs(state, *pairs: str) -> None:
    state.cfg = replace(state.cfg, apply_pairs=pairs)


def test_apply_pairs_env_var_defaults_empty_and_splits():
    from orbitd.config import from_env
    base = {"ORBITD_MCP_URL": "https://mcp.example/mcp", "ORBITD_SIGNING_KEY": "/k",
            "ORBITD_PRINCIPALS": "/p", "ORBITD_REVOCATIONS": "/r"}
    assert from_env(base).apply_pairs == ()
    assert from_env({**base, "ORBITD_APPLY_PAIRS": "demo/dev,demo/prod"}).apply_pairs == (
        "demo/dev", "demo/prod")
    assert from_env({**base, "ORBITD_APPLY_PAIRS": " demo/dev , "}).apply_pairs == ("demo/dev",)
    assert from_env({**base, "ORBITD_APPLY_PAIRS": ""}).apply_pairs == ()


def test_empty_apply_pairs_enables_every_catalog_pair(fake_backends):
    assert applier.pair_enabled(fake_backends, "demo", "prod")
    assert applier.pair_enabled(fake_backends, "demo", "dev")
    set_pairs(fake_backends, "demo/dev")
    assert applier.pair_enabled(fake_backends, "demo", "dev")
    assert not applier.pair_enabled(fake_backends, "demo", "prod")


def test_loop_skips_a_pair_not_in_apply_pairs_and_reports_the_mode(
        fake_backends, monkeypatch, caplog):
    """Catches: the loop applying a pair ArgoCD still owns, or logging the skip
    on every tick, or leaving orbit_deploy_status.reconcile null."""
    set_mode(fake_backends, "on")
    set_pairs(fake_backends, "demo/dev")
    install(fake_backends)
    seed_world(fake_backends)
    use_render(monkeypatch, rendered())
    runner = ReconcileRunner(fake_backends, dry_run=False)
    with caplog.at_level(logging.INFO, logger="orbitd.reconcile"):
        skipped = runner.tick(PROJECT, ENV)
        again = runner.tick(PROJECT, ENV)
    assert skipped is not None and skipped.mode == PAIR_NOT_ENABLED_MODE
    assert skipped.applied == 0 and skipped.failed == 0
    assert again is not None and again.mode == PAIR_NOT_ENABLED_MODE
    assert fake_backends.backends.kube.apply_calls == []
    logged = [r for r in caplog.records if PAIR_NOT_ENABLED_MODE in r.getMessage()]
    assert len(logged) == 1
    enabled = runner.tick(PROJECT, "dev")
    assert enabled is not None and enabled.mode == "on"


async def test_deploy_status_reconcile_mode_for_a_pair_not_enabled(
        fake_backends, http, monkeypatch):
    set_mode(fake_backends, "on")
    set_pairs(fake_backends, "demo/dev")
    install(fake_backends)
    seed_world(fake_backends)
    use_render(monkeypatch, rendered())
    runner = build_reconcile_runner(fake_backends)
    assert runner is not None
    runner.tick(PROJECT, ENV)
    async with sdk_client(http, mint(fake_backends, Role.viewer)) as client:
        r = await client.call_tool("orbit_deploy_status", {"project": PROJECT, "env": ENV})
    assert not r.is_error, r.content
    rec = r.structured_content["reconcile"]
    assert rec is not None
    assert rec["mode"] == PAIR_NOT_ENABLED_MODE


def test_ship_skips_apply_when_pair_not_in_apply_pairs(fake_backends):
    """Catches: ship applying a pair this orbitd must not apply, reporting
    `applied` for the skip, or the skip detail falling back to ORBITD_APPLY=on
    instead of naming the pair list."""
    set_mode(fake_backends, "on")
    set_pairs(fake_backends, "demo/dev")
    install(fake_backends)
    kube = seed_world(fake_backends)
    result = ship(fake_backends)
    assert result.status == "refused", result
    applied = step(result, "apply")
    assert applied.status == "skipped"
    assert applied.detail == PAIR_NOT_ENABLED_DETAIL
    assert "ORBITD_APPLY" in applied.detail
    assert result.evidence["verdict"] == "LANDED NOT DEPLOYED apply"
    assert kube.apply_calls == []


def test_dry_run_mode_uses_the_pair_detail_for_an_unlisted_pair(fake_backends):
    """Catches: a dry-run-mode ship for an unlisted pair reporting `applied`, or
    the skip detail naming the switch instead of the pair list that excludes it."""
    set_mode(fake_backends, "dry-run")
    set_pairs(fake_backends, "demo/dev")
    install(fake_backends)
    seed_world(fake_backends)
    result = ship(fake_backends)
    assert result.status == "refused", result
    assert step(result, "apply").detail == PAIR_NOT_ENABLED_DETAIL


def test_render_path_prefers_orbit_then_apps(tmp_path):
    (tmp_path / "clusters/orbit").mkdir(parents=True)
    (tmp_path / "clusters/apps").mkdir(parents=True)
    (tmp_path / "clusters/orbit/demo-dev.yaml").write_text("kind: Application\n")
    assert render_path("demo", "dev", tmp_path) == "clusters/orbit/demo-dev.yaml"
    (tmp_path / "clusters/orbit/demo-dev.yaml").unlink()
    (tmp_path / "clusters/apps/demo-dev.yaml").write_text("kind: Application\n")
    assert render_path("demo", "dev", tmp_path) == "clusters/apps/demo-dev.yaml"
    assert render_path("demo", "prod", tmp_path) == "clusters/apps/demo-prod.yaml"


def test_render_path_errors_naming_both_when_present_in_both(tmp_path):
    (tmp_path / "clusters/orbit").mkdir(parents=True)
    (tmp_path / "clusters/apps").mkdir(parents=True)
    (tmp_path / "clusters/orbit/demo-dev.yaml").write_text("orbit: 1\n")
    (tmp_path / "clusters/apps/demo-dev.yaml").write_text("apps: 1\n")
    with pytest.raises(RenderConflict) as ei:
        render_path("demo", "dev", tmp_path)
    assert "clusters/orbit/demo-dev.yaml" in str(ei.value)
    assert "clusters/apps/demo-dev.yaml" in str(ei.value)


async def test_render_get_reads_orbit_then_apps_and_conflicts_when_both(
        fake_backends, http):
    root = Path(fake_backends.backends.instance.root)
    (root / "clusters/orbit").mkdir(parents=True)
    (root / "clusters/apps").mkdir(parents=True)
    (root / "clusters/orbit/demo-dev.yaml").write_text("kind: Application\norbit: true\n")
    (root / "clusters/apps/demo-prod.yaml").write_text("kind: Application\napps: true\n")
    async with sdk_client(http, mint(fake_backends, Role.viewer)) as client:
        orbit = await client.call_tool("orbit_render_get", {"project": "demo", "env": "dev"})
        apps = await client.call_tool("orbit_render_get", {"project": "demo", "env": "prod"})
        (root / "clusters/apps/demo-dev.yaml").write_text("kind: Application\napps: true\n")
        both = await client.call_tool("orbit_render_get", {"project": "demo", "env": "dev"})
    assert not orbit.is_error, orbit.content
    assert orbit.structured_content["path"] == "clusters/orbit/demo-dev.yaml"
    assert "orbit: true" in orbit.structured_content["yaml"]
    assert not apps.is_error, apps.content
    assert apps.structured_content["path"] == "clusters/apps/demo-prod.yaml"
    assert both.is_error and both.structured_content["code"] == "conflict"
    assert "clusters/orbit/demo-dev.yaml" in both.structured_content["what_failed"]
    assert "clusters/apps/demo-dev.yaml" in both.structured_content["what_failed"]
