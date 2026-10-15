"""Sizing advice: what a container should ask for, from what it used. Pure arithmetic.

No I/O and no clock: every function takes plain numbers (or a list of samples) and
returns an `Advice`. `tools_viewer/size_recommend.py` asks Prometheus and maps the
answers onto these functions; nothing here knows where a number came from.

The method is the published one, with its published numbers:

* Google Autopilot's moving-window recommender (Rzadca et al., "Autopilot: workload
  autoscaling at Google", EuroSys 2020): the memory limit follows the PEAK of usage over
  a window of days, the CPU limit follows a high PERCENTILE (p95), and a safety margin of
  10 to 15% is added on top.
* The Kubernetes Vertical Pod Autoscaler's recommender defaults: a margin of 15% over the
  estimate (`recommendation-margin-fraction=0.15`), and after an out-of-memory kill the
  larger of the old memory times 1.2 (`oom-bump-up-ratio=1.2`) and the old memory plus
  100 MiB (`oom-min-bump-up-bytes=104857600`), because a killed container's usage was cut
  off at its limit and cannot show what it needed.

As applied here (memory in Mi, CPU in millicores):

* memory: request = limit = peak working set x 1.15, rounded UP to a multiple of 16 Mi,
  never below 32 Mi. (Platform convention, charts/service `service.resources`: the
  memory limit defaults to the request.)
* CPU: request = p95 of usage x 1.15, rounded up to a multiple of 10m, never below 10m.
  A CPU limit is suggested only where one is set today, at the same limit/request ratio.
* an out-of-memory kill in the window: memory is at least max(limit x 1.2, limit + 100 Mi).
* a suggestion within 10% of the current value is `leave`.
* no suggestion at all (`no_suggestion`, with the reason) when there is less than 24 h of
  history, when gaps cover more than 20% of the history, when there are no usable samples,
  or when memory sits at its limit without a kill (median >= 85% of the limit, or peak >=
  97% of it): a garbage-collected runtime fills what it is given (.NET caps its heap at
  75% of the container limit) and can fail inside the limit, so its memory use does not
  show its need.
* a container that restarted three or more times without an out-of-memory kill, AND was
  ready for less than 90% of the window (or whose readiness is not known), is never told
  to shrink: a crash loop never reaches the usage of a healthy run. The count alone was
  the rule until 2026-10-09: on the rebuilt test box a component that had been ready for
  95.8% of its window was refused advice for a week over 22 restarts of builds long since
  replaced, and told to "fix the crash" when nothing was crashing (docs/V1.md §8). A
  window that is nearly all healthy running does show what a healthy run uses.

Two invariants hold for every answer and are asserted below: a memory suggestion (request
and limit) is never below the highest working set seen, and a CPU limit suggestion is
never below the highest CPU usage seen. The CPU REQUEST is the p95 plus the margin and
may sit below a short burst: CPU is shared, not killed for, and the request is what the
scheduler reserves, not a ceiling. No input makes these functions raise: anything that is
not a usable number (NaN, a negative, an empty list, a string that is not a number)
becomes `no_suggestion` with a reason.
"""

from __future__ import annotations

import math
import statistics
from dataclasses import dataclass
from typing import Any

MI = 1024 * 1024

MARGIN = 1.15
MEMORY_STEP_MI = 16
MEMORY_FLOOR_MI = 32
CPU_STEP_M = 10
CPU_FLOOR_M = 10
OOM_BUMP_RATIO = 1.2
OOM_BUMP_MI = 100
LEAVE_BAND = 0.10
MIN_HISTORY_S = 24 * 3600
MAX_GAP_FRACTION = 0.20
AT_LIMIT_MEDIAN = 0.85
AT_LIMIT_PEAK = 0.97
ABOVE_LIMIT = 1.02      # a peak this far above today's limit was measured under an older, larger one
RESTART_LOOP = 3
READY_MOSTLY = 0.9  # ready for at least this share of the window: its usage is a healthy run's

LOWER, RAISE, LEAVE, NO_SUGGESTION = "lower", "raise", "leave", "no_suggestion"

HEAP_CAVEAT = ("; a runtime that sizes its heap from the limit (.NET, a JVM) gets a smaller heap with "
               "it and can already be failing below the limit, so check its error rate before lowering and after")

AT_LIMIT_REASON = (
    "memory use does not show the need: it sits at the limit of {limit} Mi without being "
    "killed (median {median} Mi, peak {peak} Mi), so what it would use with more room was "
    "never measured; its error rate and the runtime's own heap metrics show the need, so "
    "consider raising it"
)


@dataclass(frozen=True)
class Advice:
    """One resource of one component: the verdict, why, and the numbers suggested."""

    verdict: str
    reason: str
    request: int | None = None
    limit: int | None = None


@dataclass(frozen=True)
class Usage:
    """What a list of samples at a fixed step says. `None` where nothing was usable."""

    count: int
    peak: float | None
    median: float | None
    p95: float | None
    mean: float | None
    history_s: float
    gap_fraction: float


# ------------------------------------------------------------------ numbers

def number(value: Any) -> float | None:
    """A finite, non-negative float, or None. Prometheus hands values over as strings
    ("12.5", "NaN", "+Inf"); nothing a caller passes may raise."""
    if value is None or isinstance(value, bool):
        return None
    try:
        out = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    if math.isnan(out) or math.isinf(out) or out < 0:
        return None
    return out


def mi(value: Any) -> float | None:
    """Bytes to Mi."""
    out = number(value)
    return None if out is None else out / MI


def millicores(value: Any) -> float | None:
    """Cores to millicores."""
    out = number(value)
    return None if out is None else out * 1000.0


def count(value: Any) -> int:
    """A count of events (kills, restarts) from a possibly fractional `increase()`."""
    out = number(value)
    return 0 if out is None else int(out + 0.5)


def round_up(value: float, multiple: int) -> int:
    return int(math.ceil(value / multiple - 1e-9)) * multiple


def gap_fraction(samples_found: Any, history_s: Any, step_s: Any) -> float | None:
    """The share of the history with no sample: `samples_found` points at one per
    `step_s`, against the points a history of `history_s` seconds would hold."""
    found, history, step = number(samples_found), number(history_s), number(step_s)
    if found is None or history is None or not step:
        return None
    expected = math.floor(history / step) + 1
    return max(0.0, 1.0 - min(1.0, found / expected))


def _quantile(ordered: list[float], q: float) -> float:
    """Linear interpolation between the two nearest ranks, as Prometheus computes
    `quantile_over_time`."""
    rank = q * (len(ordered) - 1)
    low = int(math.floor(rank))
    high = min(low + 1, len(ordered) - 1)
    return ordered[low] + (ordered[high] - ordered[low]) * (rank - low)


def summarize(samples: Any, step_s: Any = 300) -> Usage:
    """Peak, median, p95, mean, history and gaps of samples taken every `step_s` seconds,
    the last one being now. A sample that is not a usable number is a gap. The history
    starts at the first usable sample: the time before a container existed is not a gap."""
    empty = Usage(count=0, peak=None, median=None, p95=None, mean=None, history_s=0.0, gap_fraction=1.0)
    try:
        step = number(step_s)
        values = [number(v) for v in samples]
        found = [(i, v) for i, v in enumerate(values) if v is not None]
        if not found or not step:
            return empty
        ordered = sorted(v for _, v in found)
        first = found[0][0]
        history = (len(values) - 1 - first) * step
        return Usage(
            count=len(found), peak=ordered[-1], median=statistics.median(ordered),
            p95=_quantile(ordered, 0.95), mean=sum(ordered) / len(ordered),
            history_s=history, gap_fraction=gap_fraction(len(found), history, step) or 0.0,
        )
    except Exception:
        return empty


# ------------------------------------------------------------------ shared judgement

def _no(reason: str) -> Advice:
    return Advice(verdict=NO_SUGGESTION, reason=reason)


def _unjudgeable(history_s: Any, gaps: Any) -> str | None:
    """The reason the window cannot be judged, or None when it can."""
    history = number(history_s)
    if history is None:
        return "no usable history in the window"
    if history < MIN_HISTORY_S:
        return f"not enough history: {history / 3600:.1f} h found, 24 h needed"
    gap = number(gaps)
    if gap is None:
        return "the samples found in the window could not be counted"
    if gap > MAX_GAP_FRACTION:
        return (f"the data has gaps covering {gap * 100:.0f}% of the history; "
                "more than 20% is not judged")
    return None


def _direction(current: float | None, suggested: int) -> str | None:
    if current is None or current <= 0:
        return None
    if abs(suggested - current) <= LEAVE_BAND * current:
        return LEAVE
    return RAISE if suggested > current else LOWER


def _days(history_s: Any) -> str:
    return f"{(number(history_s) or 0.0) / 86400:.1f} d"


def _crash_loop(restarts: int, ready: float | None = None) -> str:
    was = "" if ready is None else f" and was ready for only {ready * 100:.0f}% of it"
    return (f"left alone: it restarted {restarts} times in the window without an "
            f"out-of-memory kill{was}, and a crash loop never reaches the usage of a healthy run; "
            "fix the crash before sizing")


def _looping(restarts: int, ready_fraction: Any) -> tuple[bool, float | None]:
    """Whether the restarts make the window's usage unfit to shrink from, and the share of
    the window the container was ready (None when not known, which is read as unfit)."""
    ready = number(ready_fraction)
    if ready is not None:
        ready = min(max(ready, 0.0), 1.0)
    return restarts >= RESTART_LOOP and (ready is None or ready < READY_MOSTLY), ready


def _despite(restarts: int, ready: float | None) -> str:
    if restarts < RESTART_LOOP or ready is None:
        return ""
    return (f" (it restarted {restarts} times in the window and was ready for {ready * 100:.0f}% of it, "
            f"so the usage is that of a healthy run)")


# ------------------------------------------------------------------ memory

def recommend_memory(*, request_mi: Any = None, limit_mi: Any = None, peak_mi: Any = None,
                     median_mi: Any = None, history_s: Any = None, gap_fraction: Any = None,
                     oom_kills: Any = 0, restarts: Any = 0, ready_fraction: Any = None) -> Advice:
    """Memory advice from the peak and median working set (Mi) over the window."""
    try:
        return _memory(request_mi, limit_mi, peak_mi, median_mi, history_s, gap_fraction,
                       oom_kills, restarts, ready_fraction)
    except Exception:  # the invariant below included: an answer, never a traceback
        return _no("the memory numbers given could not be judged")


def _memory(request_mi: Any, limit_mi: Any, peak_mi: Any, median_mi: Any, history_s: Any,
            gaps: Any, oom_kills: Any, restarts: Any, ready_fraction: Any = None) -> Advice:
    peak = number(peak_mi)
    if peak is None or peak <= 0:
        return _no("no usable memory samples in the window")
    if (why := _unjudgeable(history_s, gaps)) is not None:
        return _no(why)
    request = number(request_mi) or None
    limit = number(limit_mi) or None
    median = number(median_mi)
    kills, loops = count(oom_kills), count(restarts)
    seen = math.ceil(peak)

    if kills > 0:
        base = limit or request or peak
        bumped = max(base * OOM_BUMP_RATIO, base + OOM_BUMP_MI)
        suggested = round_up(max(peak * MARGIN, bumped, MEMORY_FLOOR_MI), MEMORY_STEP_MI)
        reason = (f"killed for memory {kills} time(s) in the window at {base:.0f} Mi, where a "
                  f"kill cuts the peak off, so the suggestion is the larger of that x 1.2 "
                  f"and that + 100 Mi: {suggested} Mi")
    elif limit and peak > limit * ABOVE_LIMIT:
        # the limit was set below what the component has already used in the window
        suggested = round_up(max(peak * MARGIN, MEMORY_FLOOR_MI), MEMORY_STEP_MI)
        reason = (f"it used {seen} Mi inside the window, more than the {limit:.0f} Mi it is allowed "
                  f"today; that peak plus 15% is {suggested} Mi")
    elif limit and ((median is not None and median >= AT_LIMIT_MEDIAN * limit)
                    or peak >= AT_LIMIT_PEAK * limit):
        shown = "unknown" if median is None else f"{median:.0f}"
        return _no(AT_LIMIT_REASON.format(limit=f"{limit:.0f}", median=shown, peak=seen))
    else:
        suggested = round_up(max(peak * MARGIN, MEMORY_FLOOR_MI), MEMORY_STEP_MI)
        reason = ""

    # The invariant: never less than the highest usage seen.
    assert suggested >= peak, f"memory suggestion {suggested} Mi is below the peak {peak} Mi"

    by_limit, by_request = _direction(limit, suggested), _direction(request, suggested)
    verdicts = [v for v in (by_limit, by_request) if v is not None]
    verdict = next((v for v in verdicts if v != LEAVE), LEAVE) if verdicts else RAISE
    current = limit or request
    unfit, ready = _looping(loops, ready_fraction)
    if verdict == LOWER and unfit and kills == 0:
        verdict, reason = LEAVE, _crash_loop(loops, ready)
    if not reason:
        basis = f"peak {seen} Mi over {_days(history_s)} plus 15% is {suggested} Mi"
        if current is None:
            reason = f"{basis}, and no memory request or limit is set today"
        elif verdict == LEAVE:
            reason = f"{basis}, within 10% of the current {current:.0f} Mi"
        else:
            side = "above" if verdict == RAISE else "below"
            reason = f"{basis}, {side} the current {current:.0f} Mi"
            if verdict == LOWER:
                reason += _despite(loops, ready)
                # A runtime that sizes its heap from the container limit (.NET takes 75% of
                # it, a JVM 25% by default) can be refusing allocations at a use that reads
                # here as room to spare (measured 2026-10-08: OutOfMemoryException at 56%
                # of the limit), and a lower limit gives it a smaller heap. The numbers
                # cannot tell; the advice says what to look at before and after.
                reason += HEAP_CAVEAT
    return Advice(verdict=verdict, reason=reason, request=suggested, limit=suggested)


# ------------------------------------------------------------------ cpu

def recommend_cpu(*, request_m: Any = None, limit_m: Any = None, p95_m: Any = None,
                  peak_m: Any = None, history_s: Any = None, gap_fraction: Any = None,
                  restarts: Any = 0, ready_fraction: Any = None) -> Advice:
    """CPU advice from the p95 and the peak of usage (millicores) over the window."""
    try:
        return _cpu(request_m, limit_m, p95_m, peak_m, history_s, gap_fraction, restarts, ready_fraction)
    except Exception:  # the invariant below included: an answer, never a traceback
        return _no("the CPU numbers given could not be judged")


def _cpu(request_m: Any, limit_m: Any, p95_m: Any, peak_m: Any, history_s: Any,
         gaps: Any, restarts: Any, ready_fraction: Any = None) -> Advice:
    p95 = number(p95_m)
    if p95 is None:
        return _no("no usable CPU samples in the window")
    if (why := _unjudgeable(history_s, gaps)) is not None:
        return _no(why)
    request = number(request_m) or None
    limit = number(limit_m) or None
    peak = max(number(peak_m) or 0.0, p95)
    loops = count(restarts)

    suggested = round_up(max(p95 * MARGIN, CPU_FLOOR_M), CPU_STEP_M)
    assert suggested >= p95, f"CPU request {suggested}m is below the p95 {p95}m"
    suggested_limit = None
    if limit:
        ratio = limit / request if request else 1.0
        suggested_limit = round_up(max(suggested * ratio, peak, suggested), CPU_STEP_M)
        # The invariant: a ceiling is never below the highest usage seen.
        assert suggested_limit >= peak, f"CPU limit {suggested_limit}m is below the peak {peak}m"

    verdict = _direction(request, suggested) or RAISE
    basis = f"p95 {p95:.0f}m over {_days(history_s)} plus 15% is {suggested}m"
    if request is None:
        reason = f"{basis}, and no CPU request is set today"
    elif verdict == LOWER and _looping(loops, ready_fraction)[0]:
        verdict, reason = LEAVE, _crash_loop(loops, _looping(loops, ready_fraction)[1])
    elif verdict == LEAVE:
        reason = f"{basis}, within 10% of the current {request:.0f}m"
    else:
        side = "above" if verdict == RAISE else "below"
        reason = f"{basis}, {side} the current {request:.0f}m"
        if verdict == LOWER:
            reason += _despite(loops, _looping(loops, ready_fraction)[1])
    return Advice(verdict=verdict, reason=reason, request=suggested, limit=suggested_limit)


# ------------------------------------------------------------------ from a series

def memory_from_samples(samples: Any, *, step_s: Any = 300, request_mi: Any = None,
                        limit_mi: Any = None, oom_kills: Any = 0, restarts: Any = 0) -> Advice:
    """Memory advice from working-set samples (Mi) every `step_s` seconds, newest last."""
    usage = summarize(samples, step_s)
    return recommend_memory(request_mi=request_mi, limit_mi=limit_mi, peak_mi=usage.peak,
                            median_mi=usage.median, history_s=usage.history_s,
                            gap_fraction=usage.gap_fraction, oom_kills=oom_kills, restarts=restarts)


def cpu_from_samples(samples: Any, *, step_s: Any = 300, request_m: Any = None,
                     limit_m: Any = None, restarts: Any = 0) -> Advice:
    """CPU advice from usage samples (millicores) every `step_s` seconds, newest last."""
    usage = summarize(samples, step_s)
    return recommend_cpu(request_m=request_m, limit_m=limit_m, p95_m=usage.p95, peak_m=usage.peak,
                         history_s=usage.history_s, gap_fraction=usage.gap_fraction, restarts=restarts)
