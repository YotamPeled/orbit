"""What survives the fitting, read off the sink rather than out of the fitter.

A review found the ceiling holds and almost nothing around it does. Two of its findings are
about these cases rather than about the product: reverting the walk that this round exists for
left every case green, and deleting the rule that the decision is cut last left every case
green. Both were invisible for the same reason. The cases asked whether the line was short
enough and whether it parsed, and a record cut down to `{"verdict": "ok"}` -- or to `{}` --
answers yes to both.

So they ask the wrong question. A bound is not the point of an audit record; being readable
afterwards is. These ask what a reader can still do with what came out: name who acted, see
what was decided, see that something was cut, recover some of what the caller sent, and place
it in time.

The last of those was added a round later, and it was the one the caller could take. Four
reviews read these cases without noticing that none of them asked *when*, and a caller who
sent a long enough tool name got a refusal recorded against them with its timestamp cut to
the mark. A record that cannot be ordered against anything else is most of the way to not
being a record, whatever else survives in it.

And they read it off the sink. The previous cases called the fitter directly, which is why a
record the formatter itself cannot render -- one that leaves no line at all -- was invisible to
every one of them.
"""

from __future__ import annotations

import io
import json
import logging
import threading
import time
from datetime import datetime, timezone

import pytest

from orbitd import audit


def emit(message, *args, **fields) -> str:
    """One record through the real sink, returned as the raw line it wrote. This is the
    whole change of shape: what a reader gets is what the handler emitted, not what an
    internal returned.

    ``_message_weight`` is not a field: it puts the caller's weight in the message, which is
    where the live surface puts it, rather than in an extra field, which no call site does."""
    weight = fields.pop("_message_weight", 0)
    if weight:
        message = str(message) + " " + MARKER + "y" * weight
    stream = io.StringIO()
    saved = logging.getLogger().handlers
    audit.configure_logging(stream)
    try:
        audit.log_audit(logging.getLogger("orbitd.audit"), message, *args, **fields)
    finally:
        logging.getLogger().handlers = saved
    return stream.getvalue()


def decoded(line: str) -> dict:
    assert line.strip(), "the sink wrote nothing at all"
    assert line.count("\n") == 1, "a record stopped being one line: " + repr(line[:200])
    return json.loads(line)


WHO = {"actor": "a-reader", "role": "viewer", "tool": "orbit_project_list"}
MARKER = "findmeplease"


# Every way a record can be too heavy. The caller's text is the same recognisable string in
# each, so a case can ask whether any of it survived without knowing how it was carried.
WEIGHTY = (
    ("one long value", {"detail": MARKER + "y" * 40_000}),
    ("a list of values", {"detail": [MARKER + "y" * 200 for _ in range(100)]}),
    ("a mapping of values", {"detail": {f"k{i}": MARKER + "y" * 200 for i in range(100)}}),
    ("a long key", {MARKER + "k" * 40_000: "v"}),
    ("many short fields", {f"f{i}": MARKER for i in range(700)}),
    ("many long fields", {f"f{i}": MARKER + "y" * 200 for i in range(100)}),
    # The three below carry the weight where a caller really puts it. roles.py logs the
    # tool name the caller chose on every unknown-tool and forbidden refusal, so the tool
    # field is caller text on the live surface; the message carries the same name again.
    # The rows above put every heavy thing in an extra field, which no call site does.
    ("a tool name of the caller's choosing", {"tool": MARKER + "y" * 40_000}),
    ("the caller's text in the message", {"detail": MARKER, "_message_weight": 40_000}),
    ("caller text that lengthens when escaped", {"detail": MARKER + '"\\' * 20_000}),
)


class TestWhatComesOutIsStillARecord:
    """The question the old cases did not ask. Every one of these shapes used to be checked
    only for its length."""

    @pytest.mark.parametrize("what,fields", WEIGHTY)
    def test_it_still_says_who_acted(self, what, fields):
        one = decoded(emit("a call", **{**WHO, **fields}, verdict="refused"))
        assert one.get("actor") == "a-reader", (
            f"{what}: the fitting cost the actor, so the record cannot answer who did it: "
            + repr(one)[:300])

    @pytest.mark.parametrize("what,fields", WEIGHTY)
    def test_it_still_carries_the_decision(self, what, fields):
        one = decoded(emit("a call", **{**WHO, **fields}, verdict="refused"))
        assert str(one.get("verdict", "")).startswith("refused"), (
            f"{what}: the fitting cost the decision: " + repr(one)[:300])

    @pytest.mark.parametrize("what,fields", WEIGHTY)
    def test_it_says_that_it_was_cut(self, what, fields):
        line = emit("a call", **{**WHO, **fields}, verdict="refused")
        assert audit.RECORD_TRUNCATED in line, (
            f"{what}: a record was cut and nothing in it says so, so a reader takes what is "
            "left for the whole of it: " + repr(line[:300]))

    @pytest.mark.parametrize("what,fields", WEIGHTY)
    def test_some_of_what_the_caller_sent_survives(self, what, fields):
        """The finding that this round's own repair was held by nothing: reverting the walk
        into lists, mappings and keys left every case green, because dropping the caller's
        text whole is just as short as shortening it."""
        line = emit("a call", **{**WHO, **fields}, verdict="refused")
        assert MARKER in line, (
            f"{what}: the caller's text was discarded whole rather than shortened, so the "
            "record says something was refused but not what: " + repr(line[:300]))

    @pytest.mark.parametrize("what,fields", WEIGHTY)
    def test_it_still_fits(self, what, fields):
        line = emit("a call", **{**WHO, **fields}, verdict="refused")
        for one in line.splitlines():
            assert len(one.encode("utf-8")) <= audit.MAX_RECORD_LEN, (
                f"{what}: {len(one.encode('utf-8'))} bytes against a "
                f"{audit.MAX_RECORD_LEN} ceiling")

    def test_an_ordinary_record_is_not_cut_at_all(self):
        """Anti-vacuity: a sink that marked everything as cut would pass the mark case for
        every shape above."""
        line = emit("a call", **WHO, verdict="allowed")
        one = decoded(line)
        assert audit.RECORD_TRUNCATED not in line, "an ordinary record was reported as cut"
        assert one["actor"] == "a-reader" and one["verdict"] == "allowed"


class TestACutRecordCanStillBePlacedInTime:
    """The question four reviews of this file did not ask, and the one a caller could take.

    The fitter's planner treats the timestamp as ordinary weight: it is neither the decision
    nor an identity field, so cutting a 29-character time to half of itself plus the mark
    saves exactly one byte, which is a saving, so it is always taken. On the live surface a
    caller chooses the tool name and that name is logged on every refusal, so the caller
    chooses whether their own refusal can be ordered against anything else in the trail.

    Every ordinary spelling of the weight is here, and the control is in the same rows: an
    ordinary record's time must be a real timestamp too, so a repair that simply stops
    writing a time fails in the same place.
    """

    @staticmethod
    def _when(one: dict, what: str):
        said = one.get("time")
        assert said is not None, (
            f"{what}: the record has no time at all, so nothing can be ordered against it: "
            + repr(one)[:300])
        assert audit.RECORD_TRUNCATED not in str(said), (
            f"{what}: the timestamp was cut, so the record cannot be placed in time and a "
            f"caller chose that by sending a long name: time={said!r}")
        try:
            return datetime.fromisoformat(str(said))
        except ValueError as bad:
            raise AssertionError(
                f"{what}: the record's time is not a time, so nothing can be ordered "
                f"against it: {said!r} ({bad})") from None

    @pytest.mark.parametrize("what,fields", WEIGHTY)
    def test_a_cut_record_still_says_when_it_happened(self, what, fields):
        one = decoded(emit("a call", **{**WHO, **fields}, verdict="refused"))
        said = self._when(one, what)
        now = datetime.now(timezone.utc)
        assert abs((now - said).total_seconds()) < 300, (
            f"{what}: the record's time is not the time it was written, so it places the "
            f"call somewhere else entirely: {said.isoformat()} against {now.isoformat()}")

    @pytest.mark.parametrize("what,fields", WEIGHTY)
    def test_two_cut_records_can_still_be_ordered_against_each_other(self, what, fields):
        """Being placeable in time is only worth something if the order survives. Two heavy
        refusals a moment apart must come out in the order they happened."""
        first = decoded(emit("a call", **{**WHO, **fields}, verdict="refused"))
        time.sleep(0.005)
        second = decoded(emit("a call", **{**WHO, **fields}, verdict="refused"))
        early, late = self._when(first, what), self._when(second, what)
        assert early <= late, (
            f"{what}: two refusals came out in the wrong order, so the trail cannot be read "
            f"as a sequence: {early.isoformat()} then {late.isoformat()}")

    def test_an_ordinary_record_says_when_it_happened(self):
        """The control, in the same rows: a repair that stopped writing a time, or wrote
        something that is not one, fails here as well as above."""
        one = decoded(emit("a call", **WHO, verdict="allowed"))
        said = self._when(one, "an ordinary record")
        assert abs((datetime.now(timezone.utc) - said).total_seconds()) < 300

    def test_the_rows_above_really_are_cut(self):
        """Anti-vacuity. If none of the weighty shapes reached the ceiling, every row above
        would pass on a record nothing had touched."""
        uncut = [what for what, fields in WEIGHTY
                 if audit.RECORD_TRUNCATED not in emit("a call", **{**WHO, **fields},
                                                       verdict="refused")]
        assert not uncut, (
            f"these shapes were not cut at all, so they prove nothing about a cut record: "
            f"{uncut}")


class TestARecordTheFormatterCannotRenderStillLandsALine:
    """Read off the sink, which is the only place this is visible. The fitter learned to
    survive a value it cannot encode; the formatter is reached first and did not, so a
    record whose message refuses to become text left no line on either stream."""

    def test_a_message_that_cannot_become_text_still_lands(self):
        class Refuses:
            def __str__(self):
                raise RuntimeError("this object will not be rendered")

            def __repr__(self):
                return "<the object that refuses>"

        line = emit(Refuses(), **WHO, verdict="refused")
        assert line.strip(), (
            "a record whose message cannot become text left no line at all, so the refusal "
            "it recorded is simply absent from the trail")
        one = decoded(line)
        assert one.get("actor") == "a-reader", (
            "the line that landed is not the record: " + repr(one)[:300])

    def test_a_key_the_encoder_cannot_sort_still_lands(self):
        """The same loss by another route: the encoder sorts keys, so a key that raises when
        compared escapes the fitter and the handler drops the record silently."""
        class Unsortable(str):
            def __lt__(self, other):
                raise RuntimeError("this key will not be compared")

        line = emit("a call", **WHO, verdict="refused", **{Unsortable("zz"): "v"})
        assert line.strip(), (
            "a record carrying a key the encoder cannot sort left no line at all")
        assert decoded(line).get("actor") == "a-reader"

    def test_an_ordinary_record_still_lands(self):
        """Anti-vacuity for both: a sink that wrote a line for everything regardless would
        pass the two above."""
        assert decoded(emit("a call", **WHO, verdict="allowed"))["verdict"] == "allowed"


class TestWhatTheReaderIsToldWhenTheTextWillNotRender:
    """The three repairs this node made to the unrenderable-message path, none of which any
    case could see: a review reverted each of them in turn and the whole suite stayed green.

    Interpolation fails for two unlike reasons and a reader needs different things from each.
    When a value refuses to become text, asking it again raises the same refusal, so the
    answer can only name its type. When the caller merely passed the wrong number of
    arguments, nothing refuses anything -- the format and the arguments are ordinary values,
    and throwing them away discards the only evidence of what the caller meant.
    """

    class Refuses:
        """Refuses to become text at all: neither str nor repr will answer."""
        def __str__(self):
            raise RuntimeError("this object will not be rendered")

        def __repr__(self):
            raise RuntimeError("this object will not be described either")

    def test_a_format_the_arguments_do_not_fit_is_still_shown(self):
        """Nothing here refuses anything: the caller passed one argument to a format that
        wants two. Reverting to naming only the type of the message leaves the record saying
        a string would not render, which is both useless and untrue."""
        line = emit("a call against %s by %s", "orbit_project_list", **WHO, verdict="refused")
        assert "a call against %s by %s" in line, (
            "the record does not show the format the caller used, so a reader cannot tell "
            "what the message was meant to say: " + repr(line[:400]))

    def test_the_arguments_the_caller_passed_are_still_shown(self):
        """The other half of the same record. Dropping the arguments leaves a reader the
        shape of the sentence and none of its content."""
        line = emit("a call against %s by %s", "orbit_project_list", **WHO, verdict="refused")
        assert "orbit_project_list" in line, (
            "the record shows the format but not what the caller passed into it, so the "
            "evidence of what was meant is gone: " + repr(line[:400]))

    def test_one_argument_that_will_not_answer_does_not_cost_the_others(self):
        """The repair that closed the widest loss. Asking the argument tuple as a whole lets
        a single hostile value discard every other value in the same record -- which is the
        loss this path exists to stop, not to cause."""
        line = emit("a call against %s and %s and %s",
                    "first-value", self.Refuses(), "third-value",
                    **WHO, verdict="refused")
        assert line.strip(), "the record left no line at all"
        for kept in ("first-value", "third-value"):
            assert kept in line, (
                f"{kept!r} was discarded because a different argument in the same record "
                "would not answer, so one hostile value cost its siblings: "
                + repr(line[:400]))

    def test_the_argument_that_would_not_answer_is_named_as_such(self):
        """And the reader is told which one it was, rather than left to guess why the
        sentence has a hole in it."""
        line = emit("a call against %s and %s", "first-value", self.Refuses(),
                    **WHO, verdict="refused")
        assert "unrepresentable" in line.lower(), (
            "an argument that would not answer is simply absent, so a reader cannot tell a "
            "value that was empty from one that refused: " + repr(line[:400]))

    def test_the_record_is_still_a_record(self):
        """All of the above is worth nothing if the decision did not survive with it."""
        one = decoded(emit("a call against %s by %s", "orbit_project_list",
                           **WHO, verdict="refused"))
        assert one.get("actor") == "a-reader" and one.get("verdict") == "refused", (
            "the unrenderable-message path cost the decision: " + repr(one)[:300])

    def test_an_ordinary_message_is_not_reported_as_unformattable(self):
        """Anti-vacuity for every case above: a sink that took this path for every record
        would satisfy none of them honestly, and would satisfy the format case by accident."""
        line = emit("a call against %s", "orbit_project_list", **WHO, verdict="refused")
        assert "unformattable" not in line.lower(), (
            "an ordinary record that interpolates cleanly was reported as unformattable: "
            + repr(line[:300]))
        assert "a call against orbit_project_list" in line, (
            "an ordinary message was not interpolated: " + repr(line[:300]))


class TestTheFittingGetsSomewhereOrSaysWhatItLost:
    """Three defects a review found in the fitting loop, all of one shape: the loop believes
    it is making progress when it is not, spends its whole budget, and throws the record away.

    What the loop is supposed to do is written down in the module: each round strictly shrinks
    what the next round sees, so it cannot spin, and the round cap is there only so that a
    miscounted saving ends the fitting instead of the process. That invariant is false in
    three places. A round that removes the mark it added last round and adds it back reports
    progress while the record is byte-for-byte what it was. Keys that shorten onto one another
    are kept apart by a suffix that costs more than the cut saved, so each round re-inflates
    the next. And the per-field saving the planner works out is not always what comes off the
    wire.

    The cost is not a slow record. It is that the record goes to the last resort, which keeps
    who acted, what was decided and when, and none of what the caller sent -- so the trail
    says a call was refused and cannot say what call. Measured through the sink before this
    was written: forty fields with three-hundred-character keys took 124 seconds and came back
    as the last resort; the same keys sharing a prefix took 8.3 seconds and the same; and a
    decision one line longer than the ceiling took no time at all and the same, although the
    module says an oversized decision is cut like anything else, with the mark.

    The time matters on its own. The handler holds its lock across the formatting, so two
    minutes in here is two minutes of every other thread's logging stopped.
    """

    # Each row is a shape that makes the planner work, with the weight where an extra field
    # would really carry it. The controls are the rows that must still come out whole.
    # The cost has a cliff, not a slope, and the cliff is nowhere a caller could guess.
    # Measured on the code these were written against, with three-hundred-character keys:
    # 28 fields fit in no measurable time and keep all 28 pieces of the caller's text; 30
    # take 83 seconds and come back as the last resort with none of it; 40 take 124 seconds
    # and the same. So nothing about the size of a record tells a caller which side of it
    # they are on, which is why the rows below are shapes and not sizes.
    #
    # The first row below was the cheapest shape past the cliff -- nine seconds --
    # so a red contract cost seconds rather than minutes. It was one row and not four
    # on purpose: every other shape measured (30 and 40 distinct long keys, one long
    # key beside one long value) cost a minute or more while the defect stood, and four
    # of those would have made the suite unusable for as long as it took to repair.
    # They are the same defect with a bigger number on it. The repair made all four
    # fast, so all four run: a repair that is fast on one shape and slow on another
    # has not fixed it.
    HARD = (
        ("many fields whose keys shorten onto one another",
         {("k" * 300) + str(i): MARKER + "y" * 100 for i in range(40)}),
        ("thirty distinct long keys",
         {("%08d" % i) + "k" * 292: MARKER + "y" * 100 for i in range(30)}),
        ("forty distinct long keys",
         {("%08d" % i) + "k" * 292: MARKER + "y" * 100 for i in range(40)}),
        ("one long key beside one long value",
         {MARKER + "k" * 20000: MARKER + "y" * 20000}),
    )

    @pytest.mark.parametrize("what,fields", HARD)
    def test_it_finishes_before_the_process_notices(self, what, fields):
        started = time.monotonic()
        emit("a call", **WHO, verdict="refused", **fields)
        spent = time.monotonic() - started
        assert spent < 1.0, (
            f"{what}: one record spent {spent:.1f}s being fitted, and the sink holds its "
            "lock while it does, so every other thread's logging waited that long")

    @pytest.mark.parametrize("what,fields", HARD)
    def test_some_of_the_caller_survives_rather_than_none(self, what, fields):
        """The loss the last resort is: a record that says a call was refused and cannot say
        what call. Every shape here has room for some of the caller's text under the ceiling,
        so coming back with none of it means the loop gave up, not that it could not fit."""
        line = emit("a call", **WHO, verdict="refused", **fields)
        assert MARKER in line, (
            f"{what}: the record came back with none of the caller's text at all, so the "
            "trail records a refusal it cannot attribute to any call: " + repr(line[:300]))

    def test_a_decision_longer_than_the_record_is_cut_rather_than_discarded(self):
        """The module says an oversized decision is cut like anything else, with the mark,
        because silently corrupting it is worse than either. Measured: the record goes to the
        last resort instead, and loses the caller's text on the way."""
        line = emit("a call", **WHO, verdict="refused_" + "y" * 20_000, detail=MARKER)
        one = decoded(line)
        assert str(one.get("verdict", "")).startswith("refused"), (
            "the decision did not survive at all: " + repr(one)[:300])
        assert MARKER in line, (
            "an oversized decision cost the record everything the caller sent, although the "
            "design says the decision is cut like anything else: " + repr(line[:300]))

    @pytest.mark.parametrize("what,fields", HARD)
    def test_it_still_fits_and_still_says_it_was_cut(self, what, fields):
        """The properties the rows above must not be bought with."""
        line = emit("a call", **WHO, verdict="refused", **fields).strip()
        assert len(line.encode("utf-8")) <= audit.MAX_RECORD_LEN, (
            f"{what}: {len(line.encode('utf-8'))} bytes against a "
            f"{audit.MAX_RECORD_LEN} ceiling")
        assert audit.RECORD_TRUNCATED in line, (
            f"{what}: a record was cut and nothing in it says so")

    def test_an_ordinary_record_goes_nowhere_near_any_of_this(self):
        """Anti-vacuity for the rows above: a fitter that kept everything, or that reported
        every record as cut, would satisfy some of them by accident."""
        line = emit("a call", **WHO, verdict="allowed", detail=MARKER)
        assert audit.RECORD_TRUNCATED not in line and MARKER in line
        assert len(line.strip().encode("utf-8")) < 400


class TestTheFittingDoesNotStopTheProcess:
    """The sink is on the request path and the handler holds its lock across the formatting,
    so the time one record takes is time every other thread's logging waits. A review
    measured a megabyte-sized record taking over four minutes in there."""

    # A review measured the cost growing with the square of the record: 97 KB took 2.5s,
    # 195 KB took 10.4s, 1 MB took 262s. The smallest of those is enough to show it, and
    # keeps a red contract cheap in both time and memory -- the fitter re-encodes the whole
    # record on every cut, so the big shapes allocate as hard as they compute.
    HEAVY = {f"f{i}": "y" * 200 for i in range(500)}

    def test_a_heavy_record_is_fitted_promptly(self):
        started = time.monotonic()
        emit("a call", **WHO, verdict="refused", **self.HEAVY)
        spent = time.monotonic() - started
        assert spent < 1.0, (
            f"one record spent {spent:.1f}s being fitted, and the sink holds its lock while "
            "it does, so every other thread's logging waited that long")

    def test_another_thread_is_not_held_up_behind_it(self):
        """The property the bound above exists for, measured where it bites."""
        stream = io.StringIO()
        saved = logging.getLogger().handlers
        audit.configure_logging(stream)
        log = logging.getLogger("orbitd.audit")
        waited = []

        def heavy():
            audit.log_audit(log, "a heavy call", **WHO, verdict="refused", **self.HEAVY)

        try:
            worker = audit.guarded_thread(target=heavy, name="heavy")
            worker.start()
            time.sleep(0.05)
            started = time.monotonic()
            audit.log_audit(log, "a short call", **WHO, verdict="allowed")
            waited.append(time.monotonic() - started)
            worker.join(60)
        finally:
            logging.getLogger().handlers = saved
        assert waited[0] < 1.0, (
            f"an ordinary record waited {waited[0]:.1f}s behind one heavy one, which is the "
            "whole process's logging stalled by a single caller")


class TestTheDecisionIsCutLastAndNotForNothing:
    """A review deleted the rule that the decision is cut last and every case stayed green,
    then found that in the one shape where the rule decides anything it spends the rest of
    the record to protect a decision it cuts to the same length regardless."""

    LONG_DECISION = {"actor": "an-actor", "tool": "orbit_deploy_ship",
                     "verdict": "refused_" + "y" * 20_000}

    def test_protecting_the_decision_does_not_cost_who_acted(self):
        one = decoded(emit("a call", **self.LONG_DECISION))
        assert one.get("actor") == "an-actor", (
            "the decision was kept by spending the actor, so the record says a decision was "
            "made and cannot say who it was about: " + repr(one)[:300])
        assert one.get("tool") == "orbit_deploy_ship", (
            "the decision was kept by spending the tool: " + repr(one)[:300])

    def test_everything_else_is_cut_before_the_decision(self):
        """The rule itself, in the only shape that can see it.

        A review found the two cases written for this rule blind, and both for the same
        reason: their decision was twelve characters, far too short for the fitter to
        consider cutting at all, so the rule never decided anything and deleting it changed
        nothing. The decision has to be the longest thing in the record before `cut last`
        means anything -- otherwise it survives whether it is protected or not."""
        one = decoded(emit("a call", actor="an-actor", role="viewer",
                           tool="orbit_deploy_ship",
                           verdict="refused_" + "y" * 8_000,
                           detail=MARKER + "z" * 3_000))
        verdict = str(one.get("verdict", ""))
        assert verdict, "the decision is gone entirely: " + repr(one)[:300]
        if audit.RECORD_TRUNCATED in verdict:
            others = [(k, v) for k, v in one.items()
                      if k != "verdict" and isinstance(v, str) and len(v) > 40]
            unspent = [k for k, v in others if audit.RECORD_TRUNCATED not in v]
            assert not unspent, (
                "the decision was cut while these were left whole beside it, so it was not "
                f"cut last: {unspent}")


class TestNothingIsLostWithoutSaying:
    def test_two_long_keys_do_not_collapse_onto_one_another(self):
        """When a shortened key lands on one already there, the value it displaces goes with
        no record of it, and the reader sees a marked entry whose value belongs to another
        field."""
        line = emit("a call", **WHO, verdict="refused",
                    **{"x" * 40_000 + "a": MARKER + "-A", "x" * 40_000 + "b": MARKER + "-B"})
        one = decoded(line)
        values = [str(v) for v in one.values()]
        kept = [v for v in values if MARKER in v]
        assert kept, "both of the caller's values vanished: " + repr(one)[:300]
        assert len(kept) >= 2 or audit.RECORD_TRUNCATED in line, (
            "one of two fields was dropped with nothing to say so, and what is left reads as "
            "though it were the whole of it: " + repr(one)[:300])


class TestTheLastResortAlwaysMakesProgress:
    """The verdict clamp with no progress guard: once the decision is down to the
    bare mark its transformation is the identity, and it terminated only because
    the loop before it had already emptied the record."""

    def test_a_bare_mark_verdict_is_no_progress_not_another_round(self):
        payload = {"actor": "a", "verdict": audit.RECORD_TRUNCATED}
        assert audit._clamp_verdict(payload) is False, (
            "the clamp rewrote a verdict it cannot shorten, which is the identity "
            "loop the guard exists to refuse: " + repr(payload)[:200])
        assert payload["verdict"] == audit.RECORD_TRUNCATED

    def test_two_colliding_keys_keep_both_values(self):
        """Stronger than the symptom case above: the mechanism must not silently
        drop the displaced value even though the mark is present."""
        line = emit("a call", **WHO, verdict="refused",
                    **{"x" * 40_000 + "a": MARKER + "-A", "x" * 40_000 + "b": MARKER + "-B"})
        values = [str(v) for v in decoded(line).values()]
        assert sum(MARKER + "-A" in v for v in values) == 1, repr(values)[:300]
        assert sum(MARKER + "-B" in v for v in values) == 1, repr(values)[:300]
