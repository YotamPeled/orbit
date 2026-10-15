"""The reconcile loop: orbitd re-applies every catalog pair on a period (PHASE5-DESIGN §2).

Started with the server only when `ORBITD_APPLY` is `on` (real applies) or `dry-run`
(every request `dryRun=All`; the would-apply set goes to the log); never when `off`.
Each tick walks `shadow.catalog_pairs`: a pair whose env lock a job holds is skipped
(logged once until the lock clears); otherwise the loop takes the lock, `plan`s and
`apply`s through the applier, and releases. After the pairs the tick applies the
monitors unit the same way, under its own env lock, and then looks at the features: when
the cluster's optional features are behind the checkout (a switch whose converge did not
finish, an orbitd that restarted mid-change) it starts a converge Job. The loop never
prunes: the would-prune set is reported, not deleted. A failing object is logged with its reason
and retried next tick; nothing escalates here.

The last 20 ticks per pair sit in a ring; `orbit_deploy_status` shows the latest.
"""

from __future__ import annotations

import logging
import threading
import time
from collections import deque
from pathlib import Path
from datetime import datetime, timezone
from typing import Any

from pydantic import BaseModel, Field

from . import applier, monitors_apply
from .audit import guarded_thread
from .backends import BackendError, BackendUnconfigured
from .deploy import runtime_of, scrub
from .roles import CheckoutSource
from .shadow import catalog_pairs
from .source import read_source
from .state import AppState

log = logging.getLogger("orbitd.reconcile")

RING = 20
HOLDER = "reconcile-loop"
DEFAULT_PERIOD_S = 180


class ReconcileTick(BaseModel):
    """One tick's outcome for one pair; the shape `orbit_deploy_status` carries."""

    last_run: str = Field(description="RFC 3339 time the tick ran")
    applied: int = 0
    unchanged: int = 0
    failed: int = 0
    would_prune: list[str] = Field(
        default_factory=list,
        description="group/version/Kind/namespace/name of every object the loop would prune but never does")
    source: CheckoutSource | None = None
    mode: str | None = Field(
        default=None,
        description="on, dry-run, or 'off (pair not enabled)' when ORBITD_APPLY_PAIRS excludes the pair")
    hand_edits: list[str] = Field(
        default_factory=list,
        description="hand edits this tick reverted (would revert, in dry-run): the object and the "
                    "fields by their keys, and the field manager that wrote them — never a value")


STOP_TIMEOUT_S = 10.0


class ReconcileRunner:
    """Sleep `period_s`, tick every pair, repeat; stop on `stop_event`. Clock is injectable
    the way shadow.DailyRunner's is (an object with `sleep`)."""

    def __init__(self, state: AppState, *, dry_run: bool, period_s: float | None = None,
                 stop_event: threading.Event | None = None, clock: Any | None = None,
                 holder: str = HOLDER) -> None:
        self.state = state
        self.dry_run = dry_run
        self.period_s = float(period_s if period_s is not None else _period_of(state))
        self.stop_event = stop_event or threading.Event()
        self.clock = clock
        self.holder = holder
        self.ticks: dict[tuple[str, str], deque[ReconcileTick]] = {}
        self.thread: threading.Thread | None = None
        self._skip_logged: set[tuple[str, str]] = set()
        self._pair_off_logged: set[tuple[str, str]] = set()
        self._mu = threading.Lock()
        # the converge Job this loop last started: (commit, started at, tries at that commit)
        self._converge: tuple[str, float, int] | None = None
        self._converge_note: str | None = None

    # ------------------------------------------------------------ the ring

    def latest(self, project: str, env: str) -> ReconcileTick | None:
        with self._mu:
            ring = self.ticks.get((project, env))
            return ring[-1] if ring else None

    def history(self, project: str, env: str) -> list[ReconcileTick]:
        with self._mu:
            return list(self.ticks.get((project, env)) or ())

    def _record(self, project: str, env: str, tick: ReconcileTick) -> None:
        with self._mu:
            self.ticks.setdefault((project, env), deque(maxlen=RING)).append(tick)

    def _forget_removed(self, pairs: set[tuple[str, str]]) -> None:
        """Drop the ticks of a pair the catalog no longer carries. A project made again
        under a removed project's name showed that project's hand edits as its own recent
        ones (the test box, v1.0.3). The units (monitors, features) are kept: they are
        recorded with no environment."""
        with self._mu:
            for key in [k for k in self.ticks if k[1] and k not in pairs]:
                del self.ticks[key]
        self._skip_logged &= pairs
        self._pair_off_logged &= pairs

    # ------------------------------------------------------------ the loop

    def _sleep(self, seconds: float) -> None:
        if self.stop_event.is_set():
            return
        if self.clock is not None and hasattr(self.clock, "sleep"):
            self.clock.sleep(seconds)
            return
        self.stop_event.wait(timeout=seconds)

    def loop(self) -> None:
        while not self.stop_event.is_set():
            self._sleep(self.period_s)
            if self.stop_event.is_set():
                break
            try:
                self.run_once()
            except Exception as e:  # noqa: BLE001 - a tick never ends the loop
                log.error("reconcile tick failed: %s (next tick in %ss)", _reason(e), self.period_s)

    def run_once(self) -> list[ReconcileTick]:
        # The commit gate first: a commit it refuses is never read below (the last good
        # commit's tree is pinned in its place), and with no good commit at all there is
        # nothing to reconcile.
        try:
            from .gate import gate_of
            verdict = gate_of(self.state).evaluate()
            if verdict.blocks and verdict.good is None:
                log.error("reconcile: the commit gate holds %s and there is no last good commit; "
                          "nothing is applied (%s)", verdict.head[:12] or "(no commit)", verdict.sentence)
                return []
        except Exception as e:  # noqa: BLE001 - a gate that cannot run stops the tick, not the loop
            log.error("reconcile: the commit gate could not be evaluated: %s (nothing applied this tick)", _reason(e))
            return []
        try:
            pairs = catalog_pairs(self.state, rendered=True)
        except (BackendUnconfigured, BackendError) as e:
            log.warning("reconcile catalog: %s", e)
            return []
        except Exception as e:  # noqa: BLE001 - an unreadable catalog costs one tick
            log.error("reconcile catalog unreadable: %s (retried next tick)", _reason(e))
            return []
        ticks: list[ReconcileTick] = []
        for project, env in pairs:
            if self.stop_event.is_set():
                break  # the pair in progress finished; no other starts after a stop
            try:
                tick = self.tick(project, env)
            except Exception as e:  # noqa: BLE001 - a failing pair is recorded, never fatal
                log.error("reconcile %s/%s: tick failed: %s (retried next tick)",
                          project, env, _reason(e))
                tick = ReconcileTick(last_run=_now(), failed=1, source=_source(self.state),
                                     mode="dry-run" if self.dry_run else "on")
                self._record(project, env, tick)
            if tick is not None:
                ticks.append(tick)
        if not self.stop_event.is_set():
            self._forget_removed(set(pairs))
        if not self.stop_event.is_set():
            # The monitors unit rides along but is not a pair: its tick is recorded
            # under the unit (see latest) and never joins the pairs' return.
            try:
                self.tick_monitors()
            except Exception as e:  # noqa: BLE001
                log.error("reconcile monitors: tick failed: %s (retried next tick)", _reason(e))
        if not self.stop_event.is_set():
            try:
                self.tick_features()
            except Exception as e:  # noqa: BLE001
                log.error("reconcile features: tick failed: %s (retried next tick)", _reason(e))
        return ticks

    def tick(self, project: str, env: str) -> ReconcileTick | None:
        """Plan and apply one pair under its env lock; None when a job holds the lock."""
        if not applier.pair_enabled(self.state, project, env):
            if (project, env) not in self._pair_off_logged:
                self._pair_off_logged.add((project, env))
                log.info("reconcile %s/%s: %s", project, env, applier.PAIR_NOT_ENABLED_MODE)
            tick = ReconcileTick(last_run=_now(), mode=applier.PAIR_NOT_ENABLED_MODE,
                                 source=_source(self.state))
            self._record(project, env, tick)
            return tick
        locks = runtime_of(self.state).locks
        held = locks.try_acquire(project, env, self.holder)
        if held is not None:
            if (project, env) not in self._skip_logged:
                self._skip_logged.add((project, env))
                log.info("reconcile %s/%s: env lock held by %s, skipping until it clears",
                         project, env, held)
            return None
        self._skip_logged.discard((project, env))
        try:
            tick = self._reconcile(project, env)
        finally:
            locks.release(project, env, self.holder)
        self._record(project, env, tick)
        return tick

    def _reconcile(self, project: str, env: str) -> ReconcileTick:
        try:
            plan = applier.plan(self.state, project, env)
            result = applier.apply(self.state, plan, dry_run=self.dry_run)
        except Exception as e:
            log.error("reconcile %s/%s: plan/apply failed: %s", project, env, _reason(e))
            return ReconcileTick(last_run=_now(), failed=1, source=_source(self.state),
                                 mode="dry-run" if self.dry_run else "on")
        counts = {"applied": 0, "unchanged": 0, "failed": 0}
        would_apply: list[str] = []
        for r in result.results:
            counts[r.result] = counts.get(r.result, 0) + 1
            if r.result == "applied":
                would_apply.append(r.ref.line)
            if r.result == "failed":
                log.error("reconcile %s/%s: %s %s/%s failed: %s (retried next tick)",
                          project, env, r.ref.kind, r.ref.namespace, r.ref.name, r.reason)
        would_prune = [r.line for r in plan.prune]
        log.info("reconcile %s/%s%s: %d applied, %d unchanged, %d failed, would prune %d, render %s",
                 project, env, " (dryRun=All)" if result.dry_run else "",
                 counts["applied"], counts["unchanged"], counts["failed"], len(would_prune),
                 plan.render_sha[:12])
        if result.dry_run and would_apply:
            log.info("reconcile %s/%s would apply: %s", project, env, ", ".join(would_apply))
        if would_prune:
            log.info("reconcile %s/%s would prune (never deleted here): %s",
                     project, env, ", ".join(would_prune))
        # A hand edit the loop reverts is gone from the live object minutes after it was
        # made; the tick keeps what it was, so orbit_deploy_status can still name it to
        # someone asking afterwards (S14, docs/V1.md §8).
        hand_edits = [f"{r.ref.kind} {r.ref.namespace}/{r.ref.name}: {r.reason}"
                      for r in result.results if r.reason and "a hand edit:" in r.reason]
        return ReconcileTick(
            last_run=_now(), applied=counts["applied"], unchanged=counts["unchanged"],
            failed=counts["failed"], would_prune=would_prune, source=plan.source,
            mode="dry-run" if result.dry_run else "on", hand_edits=hand_edits,
        )

    def tick_features(self, *, now: float | None = None) -> str | None:
        """Start a converge Job when the cluster's features are behind the checkout;
        returns the Job's name when one was started.

        Nothing is started in dry-run, while a tool's features job holds the lock, while
        a converge Job is already running, or sooner than the backoff after the last one
        this loop started for the same commit (10 minutes, doubling to an hour): a
        converge that cannot succeed must not be retried every tick. Once the cluster
        matches, the failed converge Jobs left behind are deleted, so the failed-Job
        alert they raised resolves."""
        from . import features as features_module
        from . import features_converge, features_job
        from . import orbit_features as rules
        if self.dry_run:
            return None
        root = self.state.backends.instance.root
        if root is None:
            return None
        try:
            snap = features_module.read(Path(root))
        except rules.FeatureError:
            return None            # no registry, or a block the gate should have refused
        kube = self.state.backends.kube
        plan = features_converge.make_plan(
            snap.registry, snap.on, features_converge.ClusterOps(Path(root), kube),
            assumed=features_converge.assumed_states(Path(root), snap.registry))
        if not plan.changed:
            self._converge = None
            self._converge_note = None
            for job in features_job.list_jobs(kube):
                if features_job._finished(job) == "failed":
                    features_job.delete(kube, str((job.get("metadata") or {}).get("name") or ""))
            return None
        locks = runtime_of(self.state).locks
        held = locks.try_acquire("features", "", self.holder)
        if held is not None:
            return None            # orbit_features_set is at work; it runs its own converge
        try:
            if features_job.active(kube) is not None:
                return None
            # The Job checks out a commit by itself, so it is given one the gate accepted:
            # the newest commit when that passed, the last good one while the newest is
            # refused, and none at all when there is no good commit. (Seen on the test box
            # with v0.34.0-rc1: the loop handed the Job the commit the gate had just
            # refused, and the Job applied the platform from it.)
            commit = self._converge_commit()
            if not commit:
                return None
            clock = time.time() if now is None else now
            if self._converge is not None and self._converge[0] == commit:
                _, started, tries = self._converge
                wait = min(3600.0, 600.0 * (2 ** (tries - 1)))
                if clock - started < wait:
                    return None
            else:
                tries = 0
            settings = features_job.JobSettings.from_env()
            if settings is None:
                note = ("features are behind the checkout and this orbitd cannot start a converge Job "
                        "(ORBITD_IMAGE, ORBITD_GIT_SYNC_IMAGE, ORBITD_INSTANCE_REPO are not all set)")
                if self._converge_note != note:
                    self._converge_note = note
                    log.error("reconcile features: %s", note)
                return None
            name = features_job.start(kube, settings, commit)
            self._converge = (commit, clock, tries + 1)
            log.info("reconcile features: started converge Job %s at %s (enable %s, disable %s)",
                     name, commit[:12], ",".join(plan.enable) or "-", ",".join(plan.disable) or "-")
            return name
        finally:
            locks.release("features", "", self.holder)

    def _converge_commit(self) -> str | None:
        """The commit a converge Job may run at, or None."""
        from .gate import gate_of
        verdict = gate_of(self.state).evaluate()
        if not verdict.enabled:
            from .source import read_head
            return read_head(self.state).commit or None
        if verdict.blocks:
            return verdict.good
        return verdict.head or None

    def tick_monitors(self) -> ReconcileTick | None:
        """Plan and apply the monitors unit under its env lock; None when a job
        holds the lock. Never prunes and never consults ORBITD_APPLY_PAIRS."""
        unit = monitors_apply.UNIT
        locks = runtime_of(self.state).locks
        held = locks.try_acquire(unit, "", self.holder)
        if held is not None:
            if (unit, "") not in self._skip_logged:
                self._skip_logged.add((unit, ""))
                log.info("reconcile %s: env lock held by %s, skipping until it clears",
                         unit, held)
            return None
        self._skip_logged.discard((unit, ""))
        try:
            tick = self._reconcile_monitors()
        finally:
            locks.release(unit, "", self.holder)
        self._record(unit, "", tick)
        return tick

    def _reconcile_monitors(self) -> ReconcileTick:
        try:
            plan = monitors_apply.plan_monitors(self.state)
            result = monitors_apply.apply_monitors(self.state, plan, dry_run=self.dry_run)
        except Exception as e:
            log.error("reconcile monitors: plan/apply failed: %s", _reason(e))
            return ReconcileTick(last_run=_now(), failed=1, source=_source(self.state),
                                 mode="dry-run" if self.dry_run else "on")
        counts = {"applied": 0, "unchanged": 0, "failed": 0}
        would_apply: list[str] = []
        for r in result.results:
            counts[r.result] = counts.get(r.result, 0) + 1
            if r.result == "applied":
                would_apply.append(r.ref.line)
            if r.result == "failed":
                log.error("reconcile monitors: %s %s/%s failed: %s (retried next tick)",
                          r.ref.kind, r.ref.namespace, r.ref.name, r.reason)
        would_prune = [r.line for r in plan.prune]
        log.info("reconcile monitors%s: %d applied, %d unchanged, %d failed, would prune %d, render %s",
                 " (dryRun=All)" if result.dry_run else "",
                 counts["applied"], counts["unchanged"], counts["failed"], len(would_prune),
                 plan.render_sha[:12])
        if result.dry_run and would_apply:
            log.info("reconcile monitors would apply: %s", ", ".join(would_apply))
        if would_prune:
            log.info("reconcile monitors would prune (never deleted here): %s",
                     ", ".join(would_prune))
        return ReconcileTick(
            last_run=_now(), applied=counts["applied"], unchanged=counts["unchanged"],
            failed=counts["failed"], would_prune=would_prune, source=plan.source,
            mode="dry-run" if result.dry_run else "on",
        )

    def start(self) -> ReconcileRunner:
        self.thread = guarded_thread(target=self.loop, name="orbitd-reconcile", daemon=True)
        self.thread.start()
        return self

    def stop(self, timeout: float = STOP_TIMEOUT_S) -> bool:
        """Signal the loop and wait for its thread; the pair being applied finishes, no
        other starts. False when the thread outlived `timeout`. Idempotent."""
        self.stop_event.set()
        thread = self.thread
        if thread is not None and thread.is_alive() and thread is not threading.current_thread():
            thread.join(timeout)
        alive = thread is not None and thread.is_alive()
        if alive:
            log.warning("reconcile loop still running %ss after stop; abandoning the thread", timeout)
        return not alive


def build_reconcile_runner(state: AppState, **kwargs: Any) -> ReconcileRunner | None:
    """The runner for this ORBITD_APPLY mode, attached as `state.reconcile`; None when `off`."""
    mode = applier.apply_mode(state)
    if mode == applier.MODE_OFF:
        log.info("reconcile loop not started: ORBITD_APPLY=off")
        return None
    runner = ReconcileRunner(state, dry_run=(mode == applier.MODE_DRY_RUN), **kwargs)
    state.reconcile = runner  # type: ignore[attr-defined]
    log.info("reconcile loop: ORBITD_APPLY=%s, every %ds", mode, int(runner.period_s))
    return runner


def start_reconcile_runner(state: AppState, **kwargs: Any) -> ReconcileRunner | None:
    runner = build_reconcile_runner(state, **kwargs)
    return runner.start() if runner is not None else None


def latest_tick(state: AppState, project: str, env: str) -> ReconcileTick | None:
    runner = getattr(state, "reconcile", None)
    if runner is None:
        return None
    return runner.latest(project, env)


def _period_of(state: AppState) -> int:
    raw = getattr(state.cfg, "reconcile_period_s", DEFAULT_PERIOD_S)
    try:
        n = int(raw)
    except (TypeError, ValueError):
        return DEFAULT_PERIOD_S
    return n if n > 0 else DEFAULT_PERIOD_S


def _source(state: AppState) -> CheckoutSource | None:
    try:
        return read_source(state)
    except Exception:
        return None


def _reason(exc: BaseException) -> str:
    return scrub(f"{exc.__class__.__name__}: {exc}")[:1000]


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


__all__ = [
    "RING", "HOLDER", "ReconcileTick", "ReconcileRunner", "build_reconcile_runner",
    "start_reconcile_runner", "latest_tick",
]
