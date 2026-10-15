"""Server shutdown stops the background loops (review finding: the reconcile runner's
thread outlived the app's lifespan; the shadow runner had the same gap).

Every test names the break it catches. Fakes only: no network, no subprocess, no port.
"""

from __future__ import annotations

import threading
from copy import deepcopy

import anyio
import pytest

from orbitd import applier, server
from orbitd.reconcile import ReconcileRunner, start_reconcile_runner
from orbitd.shadow import DailyRunner, start_daily_runner
from orbitd.state import AppState

from test_reconcile import SHA, obj, set_mode
from test_tools_operator import PROJECT, install, seed_world

LONG = 3600.0  # a period the test never reaches: the thread parks on stop_event.wait


def _runners(state) -> tuple[DailyRunner, ReconcileRunner]:
    daily = start_daily_runner(state, first_delay=LONG, interval=LONG)
    loop = start_reconcile_runner(state, period_s=LONG)
    assert loop is not None
    return daily, loop


def _assert_stopped(*runners) -> None:
    for r in runners:
        assert r.stop_event.is_set(), f"{type(r).__name__}: stop_event not set"
        assert r.thread is not None and not r.thread.is_alive(), f"{type(r).__name__}: thread alive"


@pytest.mark.anyio
@pytest.mark.parametrize("mode", ["on", "dry-run"])
async def test_lifespan_exit_stops_both_runners(fake_backends, mode):
    """Catches: the lifespan (or serve) forgetting the runner handles, so the reconcile
    and shadow threads keep ticking after the app has shut down."""
    set_mode(fake_backends, mode)
    daily, loop = _runners(fake_backends)
    app = server.build_app(fake_backends, runners=[daily, loop])
    async with app.router.lifespan_context(app):
        assert loop.thread is not None and loop.thread.is_alive()
        assert daily.thread is not None and daily.thread.is_alive()
        assert not loop.stop_event.is_set() and not daily.stop_event.is_set()
    _assert_stopped(daily, loop)


@pytest.mark.parametrize("mode", ["on", "dry-run"])
def test_serve_stops_the_runners_when_uvicorn_returns(fake_backends, monkeypatch, mode):
    """Catches: serve() starting the loops but never stopping them (the review's
    reproduction: patch uvicorn.run, run the app's lifespan, look at the threads)."""
    set_mode(fake_backends, mode)
    seen: dict = {}

    def fake_run(app, **_kw):
        seen["app"] = app

        async def go():
            async with app.router.lifespan_context(app):
                seen["alive_inside"] = [r.thread.is_alive() for r in app.state.runners]
        anyio.run(go)

    monkeypatch.setattr(server.AppState, "build", classmethod(lambda cls, cfg: fake_backends))
    monkeypatch.setattr("uvicorn.run", fake_run)
    server.serve(fake_backends.cfg)
    daily, loop = seen["app"].state.runners
    assert isinstance(daily, DailyRunner) and isinstance(loop, ReconcileRunner)
    assert loop.dry_run is (mode == "dry-run")
    assert seen["alive_inside"] == [True, True]
    _assert_stopped(daily, loop)


def test_serve_stops_the_runners_when_uvicorn_raises(fake_backends, monkeypatch):
    """Catches: an exception out of uvicorn (bind error, startup failure) leaving the
    loops running behind a process that is on its way out."""
    set_mode(fake_backends, "on")
    seen: dict = {}

    def fake_run(app, **_kw):
        seen["app"] = app
        raise RuntimeError("bind: address in use")

    monkeypatch.setattr(server.AppState, "build", classmethod(lambda cls, cfg: fake_backends))
    monkeypatch.setattr("uvicorn.run", fake_run)
    with pytest.raises(RuntimeError, match="address in use"):
        server.serve(fake_backends.cfg)
    _assert_stopped(*seen["app"].state.runners)


def test_serve_with_apply_off_stops_the_shadow_runner_and_skips_the_missing_one(fake_backends, monkeypatch):
    """Catches: stop_runners tripping over the None the reconcile starter returns under
    ORBITD_APPLY=off, or the shadow loop surviving because the list was skipped."""
    set_mode(fake_backends, "off")
    seen: dict = {}

    def fake_run(app, **_kw):
        seen["app"] = app

        async def go():
            async with app.router.lifespan_context(app):
                pass
        anyio.run(go)

    monkeypatch.setattr(server.AppState, "build", classmethod(lambda cls, cfg: fake_backends))
    monkeypatch.setattr("uvicorn.run", fake_run)
    server.serve(fake_backends.cfg)
    daily, loop = seen["app"].state.runners
    assert loop is None
    _assert_stopped(daily)


def test_stop_waits_for_the_pair_in_progress_and_starts_no_other(fake_backends, monkeypatch):
    """Catches: stop() returning while an apply is mid-flight (a half-applied pair the
    join was meant to wait for), or the runner starting the second catalog pair after
    the stop was signalled."""
    set_mode(fake_backends, "on")
    install(fake_backends)
    seed_world(fake_backends)
    monkeypatch.setattr(applier, "render_desired",
                        lambda state, p, e: ([obj("v1", "ConfigMap", "cfg", namespace=f"{p}-{e}")], SHA))
    entered, release = threading.Event(), threading.Event()
    applied: list[tuple[str, str]] = []
    real_apply = applier.apply

    def slow_apply(state, plan, *, dry_run):
        applied.append((plan.project, plan.env))
        entered.set()
        assert release.wait(5), "the test never released the apply"
        return real_apply(state, plan, dry_run=dry_run)

    monkeypatch.setattr(applier, "apply", slow_apply)
    runner = ReconcileRunner(fake_backends, dry_run=False, period_s=0.01).start()
    assert entered.wait(5), "the loop never reached an apply"

    outcome: list[bool] = []
    stopper = threading.Thread(target=lambda: outcome.append(runner.stop(timeout=5)))
    stopper.start()
    stopper.join(0.3)
    assert stopper.is_alive(), "stop() returned while the apply was still in flight"
    assert runner.stop_event.is_set()

    release.set()
    stopper.join(5)
    assert outcome == [True]
    _assert_stopped(runner)
    assert len(applied) == 1, f"a second pair started after stop: {applied}"
    first = applied[0]
    assert runner.latest(*first) is not None and runner.latest(*first).applied == 1
    other = [(PROJECT, e) for e in ("prod", "dev") if (PROJECT, e) != first][0]
    assert runner.latest(*other) is None


def test_stop_reports_a_thread_that_outlives_the_timeout(fake_backends, monkeypatch, caplog):
    """Catches: stop() claiming success on a thread that is still running (a caller
    would then believe the loop is gone)."""
    import logging
    caplog.set_level(logging.WARNING, logger="orbitd.reconcile")
    set_mode(fake_backends, "on")
    entered, release = threading.Event(), threading.Event()

    def stuck_run_once():
        entered.set()
        release.wait(5)

    runner = ReconcileRunner(fake_backends, dry_run=False, period_s=0.01)
    monkeypatch.setattr(runner, "run_once", stuck_run_once)
    runner.start()
    try:
        assert entered.wait(5), "the loop never reached run_once"
        assert runner.stop(timeout=0.05) is False
        assert "still running" in caplog.text
    finally:
        release.set()
        runner.stop(timeout=5)
    _assert_stopped(runner)


def test_daily_runner_stop_ends_a_run_between_pairs(fake_backends):
    """Catches: the shadow loop diffing the next pair after stop was signalled."""
    calls: list[tuple[str, str]] = []
    stop = threading.Event()

    def differ(state, project, env, clock=None):
        calls.append((project, env))
        stop.set()
        from orbitd.shadow import ShadowReport
        return ShadowReport(project=project, env=env, empty=True)

    seed_world(fake_backends)  # two catalog pairs: prod and dev
    runner = DailyRunner(fake_backends, stop_event=stop, differ=differ)
    reports = runner.run_once()
    assert len(calls) == 1 and len(reports) == 1
    assert runner.stop() is True  # never started: nothing to join, still idempotent
