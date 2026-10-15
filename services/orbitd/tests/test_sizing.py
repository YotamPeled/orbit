"""orbitd.sizing over synthetic series (docs/V1.md M4b, proof 1): for each shape of usage
the suggestion, or the refusal to suggest, is the expected one; no suggestion is ever below
the highest usage in the series; no input raises.

Every series is one sample per 5 minutes, newest last; a week is 2016 samples. Memory is in
Mi, CPU in millicores. The expected numbers are computed by hand in the comments."""

from __future__ import annotations

import math
from typing import Any, NamedTuple

import pytest

from orbitd import sizing
from orbitd.sizing import LEAVE, LOWER, NO_SUGGESTION, RAISE

WEEK = 2016          # 7 d of 5-minute samples
DAY = 288
VERDICTS = {LOWER, RAISE, LEAVE, NO_SUGGESTION}


def flat(value: float, n: int = WEEK) -> list[float]:
    return [float(value)] * n


def daily_cycle(low: float, high: float, n: int = WEEK) -> list[float]:
    mid, swing = (low + high) / 2, (high - low) / 2
    return [mid + swing * math.sin(2 * math.pi * i / DAY) for i in range(n)]


def spike(base: float, top: float, n: int = WEEK) -> list[float]:
    out = flat(base, n)
    out[n // 2] = float(top)
    return out


def leak(start: float, end: float, n: int = WEEK) -> list[float]:
    return [start + (end - start) * i / (n - 1) for i in range(n)]


def sawtooth(low: float, high: float, period: int = 144, n: int = WEEK) -> list[float]:
    """Climbs to `high`, is killed, starts again: what an out-of-memory loop draws."""
    return [low + (high - low) * (i % period) / (period - 1) for i in range(n)]


def with_gaps(series: list[Any], every: int, n: int = WEEK) -> list[Any]:
    """Every `every`-th sample missing (after the first)."""
    return [None if i and i % every == 0 else v for i, v in enumerate(series)]


class Case(NamedTuple):
    name: str
    resource: str                 # "memory" | "cpu"
    samples: Any
    current: dict[str, Any]       # request/limit set today, kills, restarts
    verdict: str
    request: int | None
    limit: int | None
    says: str                     # a phrase the reason must carry


M = "memory"
C = "cpu"
BOTH_256 = {"request_mi": 256, "limit_mi": 256}

CASES = [
    # ---- memory -------------------------------------------------------------------
    # peak 100 x 1.15 = 115 -> up to 128; 128 is half of 256
    Case("steady, twice what it needs", M, flat(100), BOTH_256, LOWER, 128, 128, "below the current 256 Mi"),
    # 115 -> 128, which is what it has
    Case("steady, sized right", M, flat(100), {"request_mi": 128, "limit_mi": 128}, LEAVE, 128, 128, "within 10%"),
    # 115 -> 128 against 64: its peak is above what it was given
    Case("steady, given less than its peak", M, flat(100), {"request_mi": 64, "limit_mi": None}, RAISE, 128, 128,
         "above the current 64 Mi"),
    # the cycle peaks at 120: 120 x 1.15 = 138 -> 144
    Case("daily cycle", M, daily_cycle(40, 120), BOTH_256, LOWER, 144, 144, "peak 120 Mi"),
    # one sample at 200 decides: 200 x 1.15 = 230 -> 240, 16 under 256 (band is 25.6)
    Case("one spike", M, spike(100, 200), BOTH_256, LEAVE, 240, 240, "peak 200 Mi"),
    # ends at 180: 180 x 1.15 = 207 -> 208. The method reads no trend; it stays above
    # everything seen, and the same question a week later sees the higher peak.
    Case("slow leak", M, leak(100, 180), BOTH_256, LOWER, 208, 208, "peak 180 Mi"),
    # killed at 256: max(256 x 1.2 = 307.2, 256 + 100 = 356) = 356 -> 368 (peak x 1.15 is 287.5)
    Case("an out-of-memory kill", M, sawtooth(120, 250), {**BOTH_256, "oom_kills": 3, "restarts": 3},
         RAISE, 368, 368, "killed for memory 3 time(s)"),
    # a small limit: max(64 x 1.2 = 76.8, 64 + 100 = 164) = 164 -> 176
    Case("an out-of-memory kill at a small limit", M, sawtooth(30, 63),
         {"request_mi": 64, "limit_mi": 64, "oom_kills": 1, "restarts": 1}, RAISE, 176, 176, "+ 100 Mi"),
    # 60 x 1.15 = 69 -> 80 would be "lower", but twelve restarts without a kill: left alone
    Case("a restart loop", M, with_gaps(flat(60), 10), {**BOTH_256, "restarts": 12}, LEAVE, 80, 80,
         "restarted 12 times"),
    # 200 samples are 16.6 h
    Case("less than a day of history", M, flat(100, 200), BOTH_256, NO_SUGGESTION, None, None, "not enough history"),
    # every third sample missing: a third of the history
    Case("gaps over a fifth of the history", M, with_gaps(flat(100), 3), BOTH_256, NO_SUGGESTION, None, None,
         "gaps covering 33%"),
    # every tenth missing is 10%: judged
    Case("gaps under a fifth", M, with_gaps(flat(100), 10), BOTH_256, LOWER, 128, 128, "peak 100 Mi"),
    # 230 of 256 is 90%, all week, never killed: the runtime fills what it has
    Case("flat at the limit", M, flat(230), BOTH_256, NO_SUGGESTION, None, None,
         "memory use does not show the need"),
    # median 150 but it touched 250 (97.6% of 256) and was not killed
    Case("touched the limit, not killed", M, spike(150, 250), BOTH_256, NO_SUGGESTION, None, None,
         "memory use does not show the need"),
    # 5 x 1.15 is under the floor of 32
    Case("tiny", M, flat(5), {"request_mi": 32, "limit_mi": 32}, LEAVE, 32, 32, "within 10%"),
    Case("nothing set today", M, flat(100), {}, RAISE, 128, 128, "no memory request or limit is set"),
    Case("no samples", M, [], BOTH_256, NO_SUGGESTION, None, None, "no usable memory samples"),
    Case("garbage samples", M, ["NaN", -5, None, "abc", float("inf"), True, {}], BOTH_256,
         NO_SUGGESTION, None, None, "no usable memory samples"),
    Case("not a series at all", M, 42, BOTH_256, NO_SUGGESTION, None, None, "no usable memory samples"),
    Case("garbage for what is set today", M, flat(100), {"request_mi": "NaN", "limit_mi": -1},
         RAISE, 128, 128, "no memory request or limit is set"),
    # numbers as Prometheus sends them
    Case("strings from prometheus", M, [str(v) for v in flat(100)], {"request_mi": "256", "limit_mi": "256"},
         LOWER, 128, 128, "below the current 256 Mi"),
    # ---- cpu ----------------------------------------------------------------------
    # p95 40 x 1.15 = 46 -> 50
    Case("cpu steady, sized right", C, flat(40), {"request_m": 50}, LEAVE, 50, None, "within 10%"),
    # p95 1 x 1.15 is under the floor of 10m
    Case("cpu idle", C, flat(1), {"request_m": 100}, LOWER, 10, None, "below the current 100m"),
    # p95 200 x 1.15 = 230
    Case("cpu busy", C, flat(200), {"request_m": 100}, RAISE, 230, None, "above the current 100m"),
    # one burst does not move the p95: 20 x 1.15 = 23 -> 30
    Case("cpu one spike", C, spike(20, 500), {"request_m": 30}, LEAVE, 30, None, "p95 20m"),
    # request 50; the limit keeps its 2x ratio (100) but never under the burst of 150
    Case("cpu with a limit today", C, spike(40, 150), {"request_m": 100, "limit_m": 200}, LOWER, 50, 150,
         "below the current 100m"),
    # 400 x 1.15 = 460, limit at 2x = 920
    Case("cpu raise keeps the limit ratio", C, flat(400), {"request_m": 200, "limit_m": 400}, RAISE, 460, 920,
         "above the current 200m"),
    Case("cpu restart loop", C, flat(5), {"request_m": 100, "restarts": 9}, LEAVE, 10, None, "restarted 9 times"),
    Case("cpu less than a day", C, flat(40, 100), {"request_m": 50}, NO_SUGGESTION, None, None, "not enough history"),
    Case("cpu gaps", C, with_gaps(flat(40), 2), {"request_m": 50}, NO_SUGGESTION, None, None, "gaps covering 50%"),
    Case("cpu no samples", C, [], {"request_m": 50}, NO_SUGGESTION, None, None, "no usable CPU samples"),
    Case("cpu garbage", C, ["x", None, float("nan")], {"request_m": "y"}, NO_SUGGESTION, None, None,
         "no usable CPU samples"),
]


def advise(case: Case) -> sizing.Advice:
    if case.resource == M:
        return sizing.memory_from_samples(case.samples, **case.current)
    return sizing.cpu_from_samples(case.samples, **case.current)


def usable(samples: Any) -> list[float]:
    try:
        return [v for v in (sizing.number(s) for s in samples) if v is not None]
    except TypeError:
        return []


@pytest.mark.parametrize("case", CASES, ids=[c.name for c in CASES])
def test_the_advice_is_the_expected_one(case: Case):
    advice = advise(case)
    assert (advice.verdict, advice.request, advice.limit) == (case.verdict, case.request, case.limit), advice
    assert case.says in advice.reason, advice.reason
    assert advice.reason.count(". ") == 0, "the reason is one sentence"


@pytest.mark.parametrize("case", CASES, ids=[c.name for c in CASES])
def test_no_suggestion_is_below_the_highest_usage_and_nothing_raises(case: Case):
    advice = advise(case)                       # a raise fails the case
    assert advice.verdict in VERDICTS and advice.reason
    seen = usable(case.samples)
    if advice.verdict == NO_SUGGESTION:
        assert advice.request is None and advice.limit is None
        return
    assert seen, "a suggestion was made from no usable sample"
    if case.resource == M:
        # memory is killed for: request and limit both hold the highest working set seen
        assert advice.request >= max(seen) and advice.limit >= max(seen)
        assert advice.request == advice.limit
        assert advice.request % 16 == 0 and advice.request >= 32
    else:
        # a CPU ceiling holds the highest usage seen; the request holds the p95 it is made of
        assert advice.request >= sizing.summarize(case.samples).p95
        assert advice.request % 10 == 0 and advice.request >= 10
        if advice.limit is not None:
            assert advice.limit >= max(seen) and advice.limit >= advice.request


ODD = [None, "", "NaN", "abc", float("nan"), float("inf"), -float("inf"), -1, 0, True, [], {}, object(),
       "1e999", 10 ** 400, b"12"]


@pytest.mark.parametrize("odd", ODD, ids=[repr(o)[:24] for o in ODD])
def test_odd_numbers_never_raise(odd):
    good = dict(request_mi=256, limit_mi=256, peak_mi=100, median_mi=90, history_s=7 * 86400,
                gap_fraction=0.0, oom_kills=0, restarts=0)
    for field in good:
        advice = sizing.recommend_memory(**{**good, field: odd})
        assert advice.verdict in VERDICTS and advice.reason
        if advice.request is not None and field != "peak_mi":
            assert advice.request >= 100 and advice.limit >= 100
    good_cpu = dict(request_m=50, limit_m=100, p95_m=40, peak_m=80, history_s=7 * 86400,
                    gap_fraction=0.0, restarts=0)
    for field in good_cpu:
        advice = sizing.recommend_cpu(**{**good_cpu, field: odd})
        assert advice.verdict in VERDICTS and advice.reason
        if advice.limit is not None and field != "peak_m":
            assert advice.limit >= 80
    assert sizing.summarize(odd, odd).count >= 0
    assert sizing.gap_fraction(odd, odd, odd) in (None, 0.0)
    assert sizing.count(odd) >= 0


def test_the_at_limit_reason_says_what_to_read_instead():
    reason = sizing.memory_from_samples(flat(230), request_mi=256, limit_mi=256).reason
    for phrase in ("memory use does not show the need", "error rate", "heap metrics", "consider raising it"):
        assert phrase in reason, reason


def test_a_kill_outranks_sitting_at_the_limit():
    """Killed AND flat at the limit: the kill is the answer, not the refusal."""
    advice = sizing.memory_from_samples(flat(250), request_mi=256, limit_mi=256, oom_kills=1)
    assert (advice.verdict, advice.limit) == (RAISE, 368)


def test_the_time_before_a_container_existed_is_not_a_gap():
    """Three days old in a seven-day window: three days of history, no gaps."""
    usage = sizing.summarize([None] * (4 * DAY) + flat(100, 3 * DAY))
    assert usage.gap_fraction == 0.0 and usage.history_s == (3 * DAY - 1) * 300
    assert sizing.memory_from_samples([None] * (4 * DAY) + flat(100, 3 * DAY), **BOTH_256).verdict == LOWER


def test_gaps_are_counted_against_the_history_at_the_steps_resolution():
    assert sizing.gap_fraction(2016, 2015 * 300, 300) == 0.0
    assert sizing.gap_fraction(1008, 2015 * 300, 300) == 0.5
    assert sizing.gap_fraction(5000, 2015 * 300, 300) == 0.0     # more samples than steps is not negative


def test_the_published_numbers_are_the_ones_in_force():
    assert (sizing.MARGIN, sizing.MEMORY_STEP_MI, sizing.MEMORY_FLOOR_MI) == (1.15, 16, 32)
    assert (sizing.CPU_STEP_M, sizing.CPU_FLOOR_M) == (10, 10)
    assert (sizing.OOM_BUMP_RATIO, sizing.OOM_BUMP_MI) == (1.2, 100)
    assert (sizing.LEAVE_BAND, sizing.MIN_HISTORY_S, sizing.MAX_GAP_FRACTION) == (0.10, 86400, 0.20)
    assert (sizing.AT_LIMIT_MEDIAN, sizing.AT_LIMIT_PEAK) == (0.85, 0.97)
    for number in ("1.15", "16 Mi", "32 Mi", "10m", "1.2", "100 Mi", "10%", "24 h", "20%", "85%", "97%", "75%"):
        assert number in sizing.__doc__, number
    assert "Autopilot" in sizing.__doc__ and "Vertical Pod Autoscaler" in sizing.__doc__


def test_a_limit_set_below_what_was_already_used_is_a_raise():
    """The window holds usage from before the limit was lowered: 255 Mi used, 208 Mi allowed
    today. That is not "sits at its limit" (no suggestion); it is a limit below known use."""
    advice = sizing.recommend_memory(request_mi=208, limit_mi=208, peak_mi=255, median_mi=190,
                                     history_s=7 * 86400, gap_fraction=0.0, oom_kills=0, restarts=0)
    assert advice.verdict == sizing.RAISE
    assert advice.limit == 304 and advice.request == 304          # 255 x 1.15 = 293.25 -> 304
    assert "more than the 208 Mi" in advice.reason



def test_a_suggestion_to_lower_memory_says_what_the_numbers_cannot_tell():
    """Measured on the rebuilt test box, 2026-10-08: a .NET service throwing
    OutOfMemoryException with its container at 56% of the limit (the runtime caps its heap
    at 75% of the limit and counts what it committed). From memory use that is room to
    spare, and the advice would be to lower it (docs/V1.md §8). The advice cannot know; it
    says what to check. Raising, leaving, no suggestion and CPU carry no such sentence."""
    assert "check its error rate before lowering and after" in sizing.HEAP_CAVEAT
    lowered = 0
    for case in CASES:
        got = advise(case)
        if case.resource == M and got.verdict == LOWER:
            lowered += 1
            assert got.reason.endswith(sizing.HEAP_CAVEAT), case
        else:
            assert sizing.HEAP_CAVEAT not in got.reason, case
    assert lowered >= 2


# ------------------------------------------------------------------ restarts and the share of time ready

def _lower_case(**more):
    return dict(request_mi=960, limit_mi=960, peak_mi=181, median_mi=174, history_s=2 * 86400,
                gap_fraction=0.0, oom_kills=0, **more)


def test_restarts_alone_no_longer_refuse_a_component_that_was_ready_nearly_all_the_window():
    """The rebuilt test box, 2026-10-09: a component ready for 95.8% of its window was
    refused advice over 22 restarts of builds long since replaced, and told to fix a crash
    that was not happening (docs/V1.md §8)."""
    got = sizing.recommend_memory(**_lower_case(restarts=22, ready_fraction=0.958))
    assert got.verdict == LOWER and got.request == 224 and got.limit == 224    # 181 x 1.15 = 208.2, up to 224
    assert "restarted 22 times in the window and was ready for 96% of it" in got.reason
    cpu = sizing.recommend_cpu(request_m=250, p95_m=44.8, peak_m=48.2, history_s=2 * 86400,
                               gap_fraction=0.0, restarts=22, ready_fraction=0.958)
    assert cpu.verdict == LOWER and "ready for 96% of it" in cpu.reason


def test_a_real_crash_loop_is_still_left_alone_and_so_is_one_whose_readiness_is_unknown():
    for ready, words in ((0.5, "was ready for only 50% of it"), (0.899, "only 90% of it"),
                         (None, "without an out-of-memory kill, and"), ("NaN", "without an out-of-memory kill, and")):
        got = sizing.recommend_memory(**_lower_case(restarts=3, ready_fraction=ready))
        assert got.verdict == LEAVE, ready
        assert "fix the crash before sizing" in got.reason and words in got.reason, got.reason
        cpu = sizing.recommend_cpu(request_m=250, p95_m=44.8, peak_m=48.2, history_s=2 * 86400,
                                   gap_fraction=0.0, restarts=3, ready_fraction=ready)
        assert cpu.verdict == LEAVE, ready


def test_the_share_of_time_ready_changes_nothing_without_restarts_or_for_a_raise():
    plain = sizing.recommend_memory(**_lower_case(restarts=0))
    assert sizing.recommend_memory(**_lower_case(restarts=2, ready_fraction=0.1)) == plain
    up = dict(request_mi=208, limit_mi=208, peak_mi=256, median_mi=200, history_s=2 * 86400, gap_fraction=0.0)
    assert sizing.recommend_memory(**up, restarts=16, ready_fraction=0.2).verdict == RAISE
