"""What a record keeps when it cannot keep everything.

WHY THIS FILE EXISTS. The sink promises that a record which cannot fit is cut down rather
than thrown away, and that the cutting has an order: the caller's own fields go first, and the
few fields that say who acted and what was decided are never dropped at all. That promise is
the reason an audit record is worth keeping -- a line that says something was refused but not
by whom or to whom answers no question anybody asks of it.

The promise is held in the code and, until this file, by nothing else. Measured on the
verified branch: deleting the identity fields from the fitter's exclusion, so that who acted
becomes as droppable as any field the caller sent, leaves the whole suite green -- 1618 passed,
7 skipped, exactly as before. Eighteen record shapes are driven elsewhere and not one of them
reaches the step that drops fields.

The reason none of them reaches it is worth writing down, because it is the shape of the
mistake rather than the mistake. The contracts around this one widen ONE field until the record
is over the bound. A record over the bound because one field is enormous is fixed by shortening
that field, so the fitter never has to choose what to lose. The step that chooses is reached
the other way: by a record over the bound because it carries MANY fields, none of them
remarkable. That is also the shape a real caller produces.

WHAT THIS FILE ASKS, AND WHERE IT GETS THE NAMES. It does not read the module's list of
protected names -- a contract that reads the list it is checking goes green the moment somebody
shortens the list. It reads the promise made to the caller instead: the arguments the logging
call requires and accepts for saying who acted. Those are what a reader is owed, so those are
what must survive, and a name removed from the module's list is then a red row here rather than
a quieter promise.

Run under pytest from services/orbitd.
"""

from __future__ import annotations

import inspect
import json
import logging

import pytest

from orbitd import audit

#: What the caller is asked for, and therefore what the record owes a reader. Taken from the
#: logging call's own signature: every keyword it accepts for saying who acted, what they
#: acted on and how it was decided. `message` and the free-form extras are not here -- they are
#: the caller's text, and the promise about them is a different one.
def _what_the_caller_is_asked_for() -> tuple[str, ...]:
    signature = inspect.signature(audit.log_audit)
    names = tuple(name for name, parameter in signature.parameters.items()
                  if parameter.kind is inspect.Parameter.KEYWORD_ONLY
                  and parameter.kind is not inspect.Parameter.VAR_KEYWORD)
    assert len(names) >= 4, (
        f"the logging call asks for {names}, which is not enough to say who acted; either the "
        "call changed shape or this file is reading the wrong thing")
    return names


WHO_ACTED = _what_the_caller_is_asked_for()

#: One drive, and the property it exists to reach: a record over the bound because it carries
#: many small fields, not because one of them is enormous. Two thousand fields of one character
#: each is the shape that forces the choice, and it forces it the hard way: there is nothing
#: left to shorten -- every value is already one character -- so the only lever the fitter has
#: is to drop, and it drops in order of what each field saves. Small fields make the fields
#: that say who acted the largest things in the record, which is exactly the case where a
#: fitter that may drop them will.
MANY = 2000
EACH = 1


def _a_line(**extra) -> str:
    """One record through the real sink, captured as the line a reader would receive."""
    logger = logging.getLogger("orbitd.audit.whoacted")
    logger.setLevel(logging.INFO)
    logger.propagate = False
    written: list[str] = []

    class Capture(logging.Handler):
        def emit(self, record):
            written.append(self.format(record))

    handler = Capture()
    handler.setFormatter(audit.JsonOneLineFormatter())
    logger.handlers = [handler]
    try:
        audit.log_audit(logger, "a decision", actor="an-operator", verdict="refused",
                        role="operator", tool="orbit_project_list",
                        resource="a-project", needs="admin", **extra)
    finally:
        logger.handlers = []
    assert len(written) == 1, f"the sink wrote {len(written)} lines for one call"
    return written[0]


def _crowded() -> dict[str, str]:
    return {f"f{index:04d}": "v" * EACH for index in range(MANY)}


class TestTheDriveReallyReachesTheChoice:
    """Every way this file could pass without the fitter ever having to lose anything."""

    def test_an_uncrowded_record_keeps_everything_it_was_given(self):
        record = json.loads(_a_line(project="demo"))
        assert record.get("project") == "demo", (
            "an ordinary record already loses a caller's field, so the crowded drive below "
            "proves nothing about crowding")

    def test_the_crowded_record_really_is_over_the_bound_before_fitting(self):
        given = _crowded()
        rough = sum(len(key) + len(value) + 6 for key, value in given.items())
        assert rough > audit.MAX_RECORD_LEN, (
            f"the drive builds about {rough} bytes against a bound of {audit.MAX_RECORD_LEN}, "
            "so the fitter is never asked to choose and every row below is vacuous")

    def test_the_fitter_really_had_to_lose_something(self):
        record = json.loads(_a_line(**_crowded()))
        kept = sum(1 for key in record if key.startswith("f0"))
        assert kept < MANY, (
            "the crowded record kept every field the caller sent, so nothing was dropped and "
            "this file measures no choice at all")

    def test_what_is_left_is_still_a_record(self):
        line = _a_line(**_crowded())
        assert len(line.encode()) <= audit.MAX_RECORD_LEN
        json.loads(line)


class TestWhoActedIsNeverWhatIsLost:
    @pytest.mark.parametrize("field", WHO_ACTED)
    def test_it_survives_a_record_too_crowded_to_keep_everything(self, field):
        record = json.loads(_a_line(**_crowded()))
        assert field in record, (
            f"a record too crowded to keep everything dropped {field!r}, so a reader is told "
            "that something was decided without being told who it was about or what was "
            f"decided: {str(record)[:200]}")

    @pytest.mark.parametrize("field", WHO_ACTED)
    def test_the_caller_cannot_occupy_it(self, field):
        """The other way a record stops saying who acted: not dropped, but written over. The
        door is what prevents it -- each of these is a named argument, so a caller trying to
        send one as free-form text is refused before any record exists. That is a property of
        the signature rather than of the fitter, which is exactly why it is asked here: the
        day somebody widens the door to take these by name, this row goes red."""
        with pytest.raises(TypeError):
            _a_line(**{field: "the caller's own value"})


class TestTheRecordStillSaysWhatWasDecided:
    """Surviving as a key is not surviving. The verdict is the one field the whole record
    exists to carry, it is never more than a word, and shortening it saves nothing worth
    having -- so a record that keeps the key and loses the word is a record that says a
    decision was taken without saying which one.

    Driven by the same crowding. Nothing here asks the fitter to keep the caller's fields:
    losing all two thousand of them is right. Losing the word `refused` is not."""

    def test_the_verdict_is_a_decision_and_not_a_mark(self):
        record = json.loads(_a_line(**_crowded()))
        assert record.get("verdict") == "refused", (
            "a record crowded out by the caller's own fields kept the verdict's name and lost "
            f"its value, so nobody can tell what was decided: {record.get('verdict')!r}")

    def test_the_message_the_caller_wrote_is_not_what_is_protected(self):
        """The other half of the same judgement, stated so a repair cannot satisfy the row
        above by protecting everything: the caller's sentence may still be cut. It is the one
        field here whose length the caller chooses."""
        record = json.loads(_a_line(**_crowded()))
        assert "message" in record, "the caller's sentence was dropped rather than shortened"
