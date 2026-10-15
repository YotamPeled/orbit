"""The reconcile loop and the `apply` step of ship/rollback/restart, over the fakes.

Every test names the break it catches. Placeholder names only; no network, no
subprocess: the runner is FakeRunner, the registry FakeDigest, the cluster FakeKube,
the checkout a fake `read_source`.
"""

from __future__ import annotations

import logging
from copy import deepcopy
from dataclasses import replace

import pytest
from conftest import mint, sdk_client

from orbitd import applier, deploy, reconcile
from orbitd.applier import ObjectRef
from orbitd.deploy import POLL_S, execute_restart, execute_ship, runtime_of
from orbitd.reconcile import ReconcileRunner, build_reconcile_runner, start_reconcile_runner
from orbitd.roles import CheckoutSource, Role, Step

from test_tools_operator import (
    CATALOG, COMMIT, ENV, PROJECT, TAG, WORKER, install, seed_world,
)

pytestmark = pytest.mark.anyio

NS = f"{PROJECT}-{ENV}"
OLD = "9" * 40
SHA = "e" * 64
INSTANCE = {"app.kubernetes.io/instance": PROJECT}
ASSERTIONS = ["image", "rollout", "ready", "log"]


def obj(api_version: str, kind: str, name: str, namespace: str = NS, **body) -> dict:
    return {"apiVersion": api_version, "kind": kind,
            "metadata": {"name": name, "namespace": namespace, "labels": dict(INSTANCE)}, **body}


def rendered() -> list[dict]:
    """A small tenant render in the pair's namespace: never the Deployment the fake
    apps API serves, so the rollout wait reads the seeded one."""
    return [
        obj("v1", "ConfigMap", "app-config", data={"k": "v"}),
        obj("v1", "ServiceAccount", "demo"),
    ]


def set_mode(state, mode: str) -> None:
    state.cfg = replace(state.cfg, apply_mode=mode)


def use_render(monkeypatch, objects: list[dict], sha: str = SHA) -> None:
    monkeypatch.setattr(applier, "render_desired", lambda state, p, e: (deepcopy(objects), sha))


class FakeSource:
    """`read_source` returning `old` for the first `stale` calls, then `new` forever."""

    def __init__(self, old: str, new: str, *, stale: int) -> None:
        self.old, self.new, self.stale = old, new, stale
        self.calls = 0

    def __call__(self, state, *, now=None) -> CheckoutSource:
        self.calls += 1
        commit = self.old if self.calls <= self.stale else self.new
        return CheckoutSource(commit=commit, age_s=0, lag_note="test")


def step(result, name: str) -> Step:
    found = [s for s in result.steps if s.name == name]
    assert found, f"no step {name!r} in {[s.name for s in result.steps]}"
    return found[0]


def names(result) -> list[str]:
    return [s.name for s in result.steps]


def ship(state, opening=None):
    return execute_ship(
        state, project=PROJECT, env=ENV, components=[CATALOG["components"][1]],
        tag=TAG, action="ship", actor="desk-op", expect=None,
        opening=opening or [Step(name="confirm", status="ok")],
    )


def restart(state):
    return execute_restart(state, project=PROJECT, env=ENV, component=WORKER, tag=TAG,
                           opening=[Step(name="confirm", status="ok")])


# ------------------------------------------------------------------ the loop starts only under the switch

def test_loop_does_not_start_unless_the_switch_is_on(fake_backends):
    """Catches: a runner spawned with ORBITD_APPLY=off (it would take env locks and
    try applies the applier then refuses, every 3 minutes)."""
    set_mode(fake_backends, "off")
    assert build_reconcile_runner(fake_backends) is None
    assert start_reconcile_runner(fake_backends) is None
    assert getattr(fake_backends, "reconcile", None) is None


def test_dry_run_mode_starts_the_loop_with_dry_run_applies(fake_backends):
    """Catches: dry-run treated as off (no shadow of the loop), or as on (real writes)."""
    set_mode(fake_backends, "dry-run")
    runner = build_reconcile_runner(fake_backends)
    assert runner is not None and runner.dry_run is True
    assert fake_backends.reconcile is runner


def test_on_mode_starts_the_loop_with_real_applies_and_the_configured_period(fake_backends):
    set_mode(fake_backends, "on")
    fake_backends.cfg = replace(fake_backends.cfg, reconcile_period_s=7)
    runner = build_reconcile_runner(fake_backends)
    assert runner is not None and runner.dry_run is False
    assert runner.period_s == 7.0


# ------------------------------------------------------------------ a tick

def test_a_held_env_lock_skips_the_tick_and_logs_once(fake_backends, monkeypatch, caplog):
    """Catches: the loop applying under a running ship job (two writers on one pair),
    or a log line per tick for as long as the lock is held."""
    set_mode(fake_backends, "on")
    install(fake_backends)
    seed_world(fake_backends)
    use_render(monkeypatch, rendered())
    runner = ReconcileRunner(fake_backends, dry_run=False)
    locks = runtime_of(fake_backends).locks
    assert locks.try_acquire(PROJECT, ENV, "job-1") is None
    with caplog.at_level(logging.INFO, logger="orbitd.reconcile"):
        assert runner.tick(PROJECT, ENV) is None
        assert runner.tick(PROJECT, ENV) is None
    assert fake_backends.backends.kube.apply_calls == []
    assert runner.latest(PROJECT, ENV) is None
    held = [r for r in caplog.records if "env lock held by job-1" in r.getMessage()]
    assert len(held) == 1
    # The lock is still the job's, untouched by the skip.
    assert locks.try_acquire(PROJECT, ENV, "other") == "job-1"
    locks.release(PROJECT, ENV, "job-1")
    tick = runner.tick(PROJECT, ENV)
    assert tick is not None and tick.applied == 2


def test_a_tick_applies_the_plan_and_never_prunes(fake_backends, monkeypatch):
    """Catches: the loop deleting the would-prune set (the design's 'never deletes')."""
    set_mode(fake_backends, "on")
    install(fake_backends)
    kube = seed_world(fake_backends)
    objects = rendered()
    use_render(monkeypatch, objects)
    # A previous inventory naming one object the render dropped, live and labelled:
    # exactly what an explicit reconcile would prune.
    orphan = obj("v1", "ConfigMap", "orphan", data={})
    kube.add_object(deepcopy(orphan))
    refs = [ObjectRef.of(o) for o in objects] + [ObjectRef.of(orphan)]
    kube.add_object({
        "apiVersion": "v1", "kind": "ConfigMap",
        "metadata": {"name": f"orbit-inventory-{PROJECT}-{ENV}", "namespace": "orbit"},
        "data": {"objects": "\n".join(sorted(r.line for r in refs)), "render_sha": "old",
                 "commit": "old", "applied_at": "2026-01-01T00:00:00Z"},
    })
    runner = ReconcileRunner(fake_backends, dry_run=False)
    tick = runner.tick(PROJECT, ENV)
    assert tick is not None
    assert tick.applied == 2 and tick.unchanged == 0 and tick.failed == 0
    assert tick.would_prune == [f"/v1/ConfigMap/{NS}/orphan"]
    assert tick.source is not None and tick.source.commit
    assert tick.last_run.endswith("Z")
    applied = [(c["kind"], c["namespace"], c["name"]) for c in kube.apply_calls]
    assert ("ConfigMap", NS, "app-config") in applied
    assert ("ServiceAccount", NS, "demo") in applied
    assert all(c["dry_run"] is None for c in kube.apply_calls)
    assert kube.delete_calls == []
    assert any(o["metadata"]["name"] == "orphan" for o in kube.objects)
    inventory = [o for o in kube.objects if o["kind"] == "ConfigMap"
                 and o["metadata"].get("namespace") == "orbit"]
    assert len(inventory) == 1
    assert f"/v1/ConfigMap/{NS}/orphan" in inventory[0]["data"]["objects"].splitlines()
    # The lock is released after the tick.
    assert runtime_of(fake_backends).locks.try_acquire(PROJECT, ENV, "after") is None


def test_ship_apply_keeps_an_object_the_render_dropped(fake_backends, monkeypatch):
    """The break: a ship whose render no longer includes an object rewrites the
    inventory to the new render, so the explicit job can never prune it."""
    set_mode(fake_backends, "on")
    install(fake_backends)
    kube = seed_world(fake_backends)
    kept = rendered()
    orphan = obj("v1", "ConfigMap", "gone", data={})
    kube.add_object(deepcopy(orphan))
    use_render(monkeypatch, kept)
    refs = [ObjectRef.of(o) for o in kept] + [ObjectRef.of(orphan)]
    kube.add_object({
        "apiVersion": "v1", "kind": "ConfigMap",
        "metadata": {"name": f"orbit-inventory-{PROJECT}-{ENV}", "namespace": "orbit"},
        "data": {"objects": "\n".join(sorted(r.line for r in refs)), "render_sha": "old",
                 "commit": "old", "applied_at": "2026-01-01T00:00:00Z"},
    })
    monkeypatch.setattr(deploy, "read_source", FakeSource(OLD, COMMIT, stale=0))
    result = ship(fake_backends)
    assert result.status == "applied", result
    inventory = [o for o in kube.objects if o["kind"] == "ConfigMap"
                 and o["metadata"].get("namespace") == "orbit"]
    assert len(inventory) == 1
    lines = inventory[0]["data"]["objects"].splitlines()
    assert f"/v1/ConfigMap/{NS}/gone" in lines
    assert f"/v1/ConfigMap/{NS}/app-config" in lines
    assert kube.delete_calls == []
    assert any(o["metadata"]["name"] == "gone" for o in kube.objects)


def test_dry_run_tick_sends_dry_run_all_and_writes_nothing(fake_backends, monkeypatch):
    set_mode(fake_backends, "dry-run")
    install(fake_backends)
    kube = seed_world(fake_backends)
    use_render(monkeypatch, rendered())
    runner = build_reconcile_runner(fake_backends)
    tick = runner.tick(PROJECT, ENV)
    assert tick is not None and tick.applied == 2
    assert kube.apply_calls and all(c["dry_run"] == "All" for c in kube.apply_calls)
    assert not any(o.get("kind") == "ConfigMap" and o["metadata"].get("namespace") == "orbit"
                   for o in kube.objects)


def test_a_failing_object_is_counted_logged_and_retried_next_tick(fake_backends, monkeypatch, caplog):
    """Catches: a failed apply escalating (raising out of the loop) or vanishing."""
    set_mode(fake_backends, "on")
    install(fake_backends)
    kube = seed_world(fake_backends)
    use_render(monkeypatch, rendered())
    kube.apply_errors[("ConfigMap", NS, "app-config")] = RuntimeError("webhook said no")
    runner = ReconcileRunner(fake_backends, dry_run=False)
    with caplog.at_level(logging.ERROR, logger="orbitd.reconcile"):
        first = runner.tick(PROJECT, ENV)
    # ServiceAccount sorts before ConfigMap in the install order: one applied, then the stop.
    assert first is not None and first.failed == 1 and first.applied == 1
    assert any("app-config failed: RuntimeError: webhook said no" in r.getMessage()
               for r in caplog.records)
    del kube.apply_errors[("ConfigMap", NS, "app-config")]
    second = runner.tick(PROJECT, ENV)
    assert second is not None and second.failed == 0 and (second.applied, second.unchanged) == (1, 1)
    assert runner.history(PROJECT, ENV) == [first, second]


def test_the_ring_keeps_the_last_twenty_ticks(fake_backends, monkeypatch):
    set_mode(fake_backends, "on")
    install(fake_backends)
    seed_world(fake_backends)
    use_render(monkeypatch, rendered())
    runner = ReconcileRunner(fake_backends, dry_run=False)
    ticks = [runner.tick(PROJECT, ENV) for _ in range(23)]
    assert runner.history(PROJECT, ENV) == ticks[-20:]
    assert runner.latest(PROJECT, ENV) is ticks[-1]


def test_run_once_walks_every_catalog_pair(fake_backends, monkeypatch):
    set_mode(fake_backends, "on")
    install(fake_backends)
    seed_world(fake_backends)
    monkeypatch.setattr(applier, "render_desired",
                        lambda state, p, e: ([obj("v1", "ConfigMap", "cfg", namespace=f"{p}-{e}")], SHA))
    runner = ReconcileRunner(fake_backends, dry_run=False)
    ticks = runner.run_once()
    assert len(ticks) == 2
    assert {(c["namespace"]) for c in fake_backends.backends.kube.apply_calls
            if c["kind"] == "ConfigMap" and c["name"] == "cfg"} == {f"{PROJECT}-prod", f"{PROJECT}-dev"}
    assert runner.latest(PROJECT, "dev") is not None and runner.latest(PROJECT, "prod") is not None


# ------------------------------------------------------------------ ship: the apply step

def test_ship_with_switch_off_records_apply_skipped_and_still_asserts(fake_backends):
    """Catches: the ship reporting `applied` (and `DEPLOYED`) when the apply step
    was skipped and nothing was written, or the skip detail not naming the switch
    the operator must flip. The rollout and assertions never run behind a skip."""
    set_mode(fake_backends, "off")
    install(fake_backends)
    kube = seed_world(fake_backends)
    result = ship(fake_backends)
    assert result.status == "refused", result
    assert names(result) == ["confirm", "registry", "bump", "apply"]
    applied = step(result, "apply")
    assert applied.status == "skipped"
    assert applied.detail == "ORBITD_APPLY=off: apply is disabled on this orbitd; set ORBITD_APPLY=on"
    assert result.evidence["verdict"] == "LANDED NOT DEPLOYED apply"
    assert result.evidence["components"] == [{"name": WORKER, "stage": "refused", "commit": COMMIT}]
    assert kube.apply_calls == []


def test_ship_with_dry_run_mode_also_skips_the_step(fake_backends):
    """Catches: a dry-run-mode ship reporting `applied` for an apply it skipped."""
    set_mode(fake_backends, "dry-run")
    install(fake_backends)
    kube = seed_world(fake_backends)
    result = ship(fake_backends)
    assert result.status == "refused", result
    assert step(result, "apply").detail == "ORBITD_APPLY=dry-run: apply is disabled on this orbitd; set ORBITD_APPLY=on"
    assert result.evidence["verdict"] == "LANDED NOT DEPLOYED apply"
    assert kube.apply_calls == []


def test_ship_with_switch_on_waits_for_the_checkout_then_applies_then_rolls_out(fake_backends, monkeypatch):
    """Catches: applying from a checkout that does not yet carry the bump (the old
    render would be applied and 'proven'), and apply after rollout."""
    set_mode(fake_backends, "on")
    _, clock = install(fake_backends)
    kube = seed_world(fake_backends)
    use_render(monkeypatch, rendered())
    # execute_ship reads the source once on entry; the wait then sees old, old, new.
    source = FakeSource(OLD, COMMIT, stale=3)
    monkeypatch.setattr(deploy, "read_source", source)
    result = ship(fake_backends)
    assert result.status == "applied", result
    assert result.commit == COMMIT
    assert names(result) == ["confirm", "registry", "bump", "apply", "rollout", *ASSERTIONS]
    applied = step(result, "apply")
    assert applied.status == "ok"
    assert applied.detail == f"2 applied, 0 unchanged, render_sha={SHA}"
    assert source.calls >= 4
    assert clock.sleeps[:2] == [POLL_S, POLL_S]
    assert [(c["kind"], c["name"], c["dry_run"], c["field_manager"]) for c in kube.apply_calls][:2] == [
        ("ServiceAccount", "demo", None, "orbit"), ("ConfigMap", "app-config", None, "orbit")]
    inventory = [o for o in kube.objects if o["kind"] == "ConfigMap"
                 and o["metadata"].get("namespace") == "orbit"]
    assert len(inventory) == 1 and inventory[0]["data"]["render_sha"] == SHA
    assert result.evidence["verdict"] == "DEPLOYED"
    assert kube.delete_calls == []


def test_ship_accepts_a_short_commit_from_the_bump(fake_backends, monkeypatch):
    set_mode(fake_backends, "on")
    install(fake_backends, runner=__import__("orbitd.testing", fromlist=["FakeRunner"]).FakeRunner(commit=COMMIT[:7]))
    seed_world(fake_backends)
    use_render(monkeypatch, rendered())
    monkeypatch.setattr(deploy, "read_source", FakeSource(OLD, COMMIT, stale=1))
    result = ship(fake_backends)
    assert result.status == "applied", result
    assert step(result, "apply").status == "ok"


def test_ship_checkout_lag_fails_the_apply_step_with_both_shas(fake_backends, monkeypatch):
    """Catches: an apply of the stale render when the checkout never catches up, and
    the lag masquerading as the job timer's `rollout` timeout."""
    set_mode(fake_backends, "on")
    rt, clock = install(fake_backends)
    rt.runner_timeout_s = 30
    kube = seed_world(fake_backends)
    use_render(monkeypatch, rendered())
    monkeypatch.setattr(deploy, "read_source", FakeSource(OLD, COMMIT, stale=10_000))
    result = ship(fake_backends)
    assert result.status == "failed"
    assert result.commit == COMMIT
    assert names(result) == ["confirm", "registry", "bump", "apply"]
    applied = step(result, "apply")
    assert applied.status == "failed"
    assert applied.detail == f"checkout lag: HEAD {OLD} is not {COMMIT}"
    assert result.evidence["verdict"] == "LANDED NOT DEPLOYED apply"
    assert result.evidence["components"] == [{"name": WORKER, "stage": "failed", "commit": COMMIT}]
    assert kube.apply_calls == []
    # Bounded by the job budget less the rollout and assertion phase: 30 + 5 - 5 - 5.
    budget = rt.runner_timeout_s + rt.rollout_timeout_s - rt.rollout_timeout_s - rt.assert_timeout_s
    assert sum(clock.sleeps) == pytest.approx(budget)


def test_ship_apply_failure_is_landed_not_deployed_apply(fake_backends, monkeypatch):
    """Catches: an apply failure reported as a rollout failure, or the failing object
    and reason missing from the step."""
    set_mode(fake_backends, "on")
    install(fake_backends)
    kube = seed_world(fake_backends)
    use_render(monkeypatch, rendered())
    monkeypatch.setattr(deploy, "read_source", FakeSource(OLD, COMMIT, stale=0))
    kube.apply_errors[("ServiceAccount", NS, "demo")] = RuntimeError("forbidden: Bearer abc.def.ghi")
    result = ship(fake_backends)
    assert result.status == "failed"
    assert result.commit == COMMIT
    assert names(result) == ["confirm", "registry", "bump", "apply"]
    applied = step(result, "apply")
    assert applied.status == "failed"
    assert applied.detail == f"ServiceAccount {NS}/demo: RuntimeError: forbidden: <token>"
    assert result.evidence["verdict"] == "LANDED NOT DEPLOYED apply"
    assert result.evidence["components"][0]["stage"] == "failed"
    assert not any(o["kind"] == "ConfigMap" and o["metadata"].get("namespace") == "orbit"
                   for o in kube.objects), "no inventory after a failed apply"


def test_ship_render_failure_is_landed_not_deployed_apply(fake_backends, monkeypatch):
    set_mode(fake_backends, "on")
    install(fake_backends)
    seed_world(fake_backends)
    monkeypatch.setattr(deploy, "read_source", FakeSource(OLD, COMMIT, stale=0))

    def broken(state, p, e):
        raise RuntimeError("helm template exploded")

    monkeypatch.setattr(applier, "render_desired", broken)
    result = ship(fake_backends)
    assert result.status == "failed"
    assert step(result, "apply").detail == "RuntimeError: helm template exploded"
    assert result.evidence["verdict"] == "LANDED NOT DEPLOYED apply"


def test_ship_second_component_is_skipped_after_an_apply_failure(fake_backends, monkeypatch):
    set_mode(fake_backends, "on")
    install(fake_backends)
    kube = seed_world(fake_backends)
    use_render(monkeypatch, rendered())
    monkeypatch.setattr(deploy, "read_source", FakeSource(OLD, COMMIT, stale=0))
    kube.apply_errors[("ConfigMap", NS, "app-config")] = RuntimeError("nope")
    result = execute_ship(
        fake_backends, project=PROJECT, env=ENV,
        components=[CATALOG["components"][1], CATALOG["components"][0]],
        tag=TAG, action="ship", actor="desk-op", expect=None,
        opening=[Step(name="confirm", status="ok")],
    )
    assert result.status == "failed"
    assert result.evidence["skipped"] == ["web"]
    assert [(c["kind"], c["name"]) for c in kube.apply_calls] == [
        ("ServiceAccount", "demo"), ("ConfigMap", "app-config")]


# ------------------------------------------------------------------ restart: the same step

def test_restart_with_switch_off_records_apply_skipped(fake_backends):
    """Catches: a restart reporting `refused` with nothing applied when its
    pod-template patch landed and replaced the pods. The patch IS the restart --
    it needs no applier -- so the skipped apply step stays visible in the record
    while the rollout is waited on and asserted, exactly as with the switch on."""
    set_mode(fake_backends, "off")
    install(fake_backends)
    kube = seed_world(fake_backends)
    result = restart(fake_backends)
    assert kube.patches, "the restart wrote no patch, so this case measures nothing"
    assert result.status == "applied", result
    assert names(result) == ["confirm", "restart", "apply", "rollout", *ASSERTIONS]
    assert step(result, "restart").status == "ok"
    assert step(result, "apply").status == "skipped"
    assert step(result, "apply").detail == "ORBITD_APPLY=off: apply is disabled on this orbitd; set ORBITD_APPLY=on"
    assert result.evidence["verdict"] == "DEPLOYED"
    assert kube.apply_calls == []


def test_restart_with_switch_on_applies_then_rolls_out(fake_backends, monkeypatch):
    """Catches: restart waiting for a commit it never made, or skipping the apply."""
    set_mode(fake_backends, "on")
    _, clock = install(fake_backends)
    kube = seed_world(fake_backends)
    use_render(monkeypatch, rendered())
    source = FakeSource(OLD, COMMIT, stale=10_000)  # a checkout that never moves: no wait
    monkeypatch.setattr(deploy, "read_source", source)
    result = restart(fake_backends)
    assert result.status == "applied", result
    assert names(result) == ["confirm", "restart", "apply", "rollout", *ASSERTIONS]
    assert step(result, "apply").detail == f"2 applied, 0 unchanged, render_sha={SHA}"
    assert clock.sleeps[:1] != [POLL_S] or source.calls <= 2
    assert kube.patches and kube.patches[-1]["name"] == f"{PROJECT}-{WORKER}"
    assert [(c["kind"], c["name"]) for c in kube.apply_calls][:2] == [
        ("ServiceAccount", "demo"), ("ConfigMap", "app-config")]
    assert result.evidence["verdict"] == "DEPLOYED"


def test_restart_apply_failure_is_landed_not_deployed_apply(fake_backends, monkeypatch):
    set_mode(fake_backends, "on")
    install(fake_backends)
    kube = seed_world(fake_backends)
    use_render(monkeypatch, rendered())
    kube.apply_errors[("ConfigMap", NS, "app-config")] = RuntimeError("quota exceeded")
    result = restart(fake_backends)
    assert result.status == "failed"
    assert names(result) == ["confirm", "restart", "apply"]
    assert step(result, "apply").detail == f"ConfigMap {NS}/app-config: RuntimeError: quota exceeded"
    assert result.evidence["verdict"] == "LANDED NOT DEPLOYED apply"
    assert result.evidence["components"] == [{"name": WORKER, "stage": "failed"}]


# ------------------------------------------------------------------ orbit_deploy_status carries reconcile

async def test_deploy_status_carries_the_latest_reconcile_tick(fake_backends, http, monkeypatch):
    """Catches: the payload without `reconcile`, or a stale tick shown for the pair."""
    set_mode(fake_backends, "on")
    install(fake_backends)
    kube = seed_world(fake_backends)
    use_render(monkeypatch, rendered())
    token = mint(fake_backends, Role.viewer)
    async with sdk_client(http, token) as client:
        before = await client.call_tool("orbit_deploy_status", {"project": PROJECT, "env": ENV})
    assert not before.is_error, before.content
    assert before.structured_content["reconcile"] is None
    runner = build_reconcile_runner(fake_backends)
    kube.add_object({
        "apiVersion": "v1", "kind": "ConfigMap",
        "metadata": {"name": f"orbit-inventory-{PROJECT}-{ENV}", "namespace": "orbit"},
        # only `gone`: an inventory that also listed a rendered object missing from the
        # cluster would describe an object deleted by hand, which the tick now names
        # (tests/test_hand_edits.py); this test is about a tick with no hand edit
        "data": {"objects": f"/v1/ConfigMap/{NS}/gone",
                 "render_sha": "old", "commit": "old", "applied_at": "2026-01-01T00:00:00Z"},
    })
    kube.add_object(obj("v1", "ConfigMap", "gone", data={}))
    first = runner.tick(PROJECT, ENV)
    assert first is not None
    async with sdk_client(http, token) as client:
        after = await client.call_tool("orbit_deploy_status", {"project": PROJECT, "env": ENV})
    assert not after.is_error, after.content
    payload = after.structured_content["reconcile"]
    assert set(payload) == {"last_run", "applied", "unchanged", "failed", "would_prune", "source", "mode",
                            "hand_edits", "recent_hand_edits"}
    assert (payload["hand_edits"], payload["recent_hand_edits"]) == ([], [])
    assert payload["mode"] == "on"
    assert payload["last_run"] == first.last_run
    assert (payload["applied"], payload["unchanged"], payload["failed"]) == (2, 0, 0)
    assert payload["would_prune"] == [f"/v1/ConfigMap/{NS}/gone"]
    assert payload["source"]["commit"] == first.source.commit
    # The real apply kept the dropped entry; the next tick still names it as
    # would-prune, the payload follows the latest tick, and `gone` is still live.
    second = runner.tick(PROJECT, ENV)
    assert second is not None
    async with sdk_client(http, token) as client:
        later = await client.call_tool("orbit_deploy_status", {"project": PROJECT, "env": ENV})
    payload = later.structured_content["reconcile"]
    assert payload["last_run"] == second.last_run
    assert (payload["applied"], payload["unchanged"], payload["failed"], payload["would_prune"]) == (
        0, 2, 0, [f"/v1/ConfigMap/{NS}/gone"])
    assert kube.delete_calls == []
    assert any(o["metadata"]["name"] == "gone" for o in kube.objects)


async def test_deploy_status_reconcile_is_null_for_a_pair_without_a_tick(fake_backends, http, monkeypatch):
    set_mode(fake_backends, "on")
    install(fake_backends)
    seed_world(fake_backends)
    use_render(monkeypatch, rendered())
    runner = build_reconcile_runner(fake_backends)
    runner.tick(PROJECT, "dev")
    async with sdk_client(http, mint(fake_backends, Role.viewer)) as client:
        r = await client.call_tool("orbit_deploy_status", {"project": PROJECT, "env": ENV})
    assert not r.is_error, r.content
    assert r.structured_content["reconcile"] is None


# ------------------------------------------------------------------ config

def test_reconcile_period_env_var_defaults_to_180():
    from orbitd.config import from_env
    base = {"ORBITD_MCP_URL": "https://mcp.example/mcp", "ORBITD_SIGNING_KEY": "/k",
            "ORBITD_PRINCIPALS": "/p", "ORBITD_REVOCATIONS": "/r"}
    assert from_env(base).reconcile_period_s == 180
    assert from_env({**base, "ORBITD_RECONCILE_PERIOD_S": "45"}).reconcile_period_s == 45
    assert from_env({**base, "ORBITD_RECONCILE_PERIOD_S": "garbage"}).reconcile_period_s == 180
