"""What a reader is told when the record they are holding is not all of it.

WHY THIS FILE EXISTS. A review drove seven hundred ordinary caller fields and a human sentence
of the length a real one has, and got back a hundred and forty-five bytes: who acted, what was
decided, the time -- and not one of the seven hundred fields, with nothing anywhere saying they
had ever existed. Against a ceiling of eight thousand. The fitter had stopped while there was
still room, because dropping the sentence made it write the mark into a field of its own, that
field was then the heaviest thing it could drop, and each round dropped it and put it back byte
for byte until the loop ran out of rounds and handed over what it had.

The row that was supposed to hold this passed, and it passed by luck: its message was the
six-character string `a call` and its value happened to be exactly twelve characters. A real
sentence, or two characters less, and it went red on code nobody had touched.

The second half is the same question from the other side. What tells a reader the record was
cut is a run of plain characters chosen to survive escaping, which means a caller can type it.
With it inside a tool name, three hundred and sixty-two fields and the sentence were dropped
and the mark appeared nowhere else: a reader sees a record that looks whole.

WHAT THIS FILE ASKS. Two things, of a record the caller made too big in the ordinary way --
many fields, none of them remarkable, and a sentence of the length people write.

  - **It used the room it had.** A record that had to lose things must come back near the
    ceiling, not far under it. Stopping early is the signature of a loop that gave up, and it
    is measurable without knowing anything about how the loop works.
  - **It says it lost them, in something the caller could not have written.** A reader who
    cannot tell a cut record from a whole one has to treat every record as suspect.

Neither question mentions a field name, a round, or a mark: both are asked of the line a reader
receives. Run under pytest from services/orbitd.
"""

from __future__ import annotations

import json
import logging

import pytest

from orbitd import audit

#: A sentence of the length people actually write. The row this file replaces used six
#: characters, and six characters is what made it pass.
A_REAL_SENTENCE = ("the operator asked to ship a tag to an environment and the surface "
                   "refused it")

#: Many fields, none remarkable: the ordinary way a caller makes a record too big. Twelve
#: characters each -- the length the old row used, kept so that the case it thought it was
#: driving is the case that runs here.
HOW_MANY = 700
EACH = "v" * 12

#: The name of the field a cut record uses to say so, discovered by cutting one rather than
#: read out of the module: a contract that reads the name it is checking cannot see it change.
def _what_tells_the_reader() -> str | None:
    """None when a cut record carries no field of its own that a whole one lacks -- which is
    itself the defect, reported as a red row below rather than as a collection error."""
    whole = json.loads(_line(project="demo"))
    cut = json.loads(_line(**{f"field_{i:03d}": EACH for i in range(HOW_MANY)}))
    new = [key for key in cut if key not in whole and not key.startswith("field_")]
    return new[0] if len(new) == 1 else None


def _line(message: str = A_REAL_SENTENCE, **extra) -> str:
    logger = logging.getLogger(f"orbitd.audit.cut{len(extra)}{len(message)}")
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
        audit.log_audit(logger, message, actor="an-operator", verdict="refused", **extra)
    finally:
        logger.handlers = []
    assert len(written) == 1
    return written[0]


def _crowded() -> dict[str, str]:
    return {f"field_{index:03d}": EACH for index in range(HOW_MANY)}


TELLS_THE_READER = _what_tells_the_reader()


class TestTheDriveIsTheOrdinaryWayToBeTooBig:
    def test_the_caller_really_sent_more_than_fits(self):
        rough = sum(len(key) + len(value) + 6 for key, value in _crowded().items())
        assert rough > audit.MAX_RECORD_LEN, (
            f"the drive builds about {rough} bytes against {audit.MAX_RECORD_LEN}, so nothing "
            "below is about a record that had to lose anything")

    def test_an_uncrowded_record_keeps_what_it_was_given(self):
        record = json.loads(_line(project="demo"))
        assert record.get("project") == "demo" and record.get("message") == A_REAL_SENTENCE, (
            "an ordinary record already loses things, so the crowded rows prove nothing")


class TestARecordThatHadToLoseThingsUsesTheRoomItHas:
    """The property that catches a loop which stopped while there was still room. It knows
    nothing about rounds or marks: it weighs the line."""

    def test_it_comes_back_near_the_ceiling(self):
        line = _line(**_crowded())
        used = len(line.encode())
        assert used <= audit.MAX_RECORD_LEN, f"the record passed the bound at {used} bytes"
        assert used * 2 > audit.MAX_RECORD_LEN, (
            f"the record came back at {used} bytes against a ceiling of "
            f"{audit.MAX_RECORD_LEN} while {HOW_MANY} fields of the caller's were waiting to "
            "be spent: the fitting stopped with room to spare, which is what a loop does when "
            "it gives up rather than when it is finished")

    def test_it_kept_some_of_what_the_caller_sent(self):
        record = json.loads(_line(**_crowded()))
        kept = [key for key in record if key.startswith("field_")]
        assert kept, (
            "not one of the caller's fields survived, though hundreds would have fitted: what "
            f"came back was {str(record)[:200]}")


class TestARecordThatLostThingsSaysSo:
    def test_a_reader_is_told(self):
        whole = json.loads(_line(project="demo"))
        cut = json.loads(_line(**_crowded()))
        assert set(cut) - set(whole) or any(
            cut.get(key) != whole.get(key) for key in ("message",)), (
            "a record that lost hundreds of fields is indistinguishable from one that lost "
            "nothing")

    def test_what_tells_the_reader_is_not_something_a_caller_can_write(self):
        """The counterfeit. A caller sends, as its own ordinary text, whatever the record uses
        to say it was cut; the record is then cut for real. If the two are the same kind of
        thing, the reader cannot tell the record's own statement from the caller's text."""
        whole = json.loads(_line(project="demo"))
        counterfeit = dict(_crowded())
        counterfeit["tool_name"] = audit.RECORD_TRUNCATED
        cut = json.loads(_line(**counterfeit))
        told = [key for key in cut
                if key not in whole and key not in counterfeit]
        assert told, (
            "a record cut while the caller was also sending the mark as its own text carries "
            f"no field of its own saying it was cut: {str(cut)[:200]}")

    def test_a_cut_record_carries_a_field_of_its_own(self):
        assert TELLS_THE_READER is not None, (
            "a cut record carries no single field of its own that a whole record lacks, so "
            "there is nothing a reader can look at to know what they are holding is partial")

    def test_a_caller_cannot_make_a_whole_record_look_cut(self):
        if TELLS_THE_READER is None:
            pytest.skip("there is no such field yet; the row above is the one that says so")
        """The other direction of the same forgery, and the one that decides whether a reader
        can trust the field at all: a caller sends it by name on a record small enough that
        nothing was cut."""
        forged = json.loads(_line(project="demo",
                                  **{TELLS_THE_READER: audit.RECORD_TRUNCATED}))
        assert TELLS_THE_READER not in forged, (
            f"a caller occupied {TELLS_THE_READER!r} on a record that lost nothing, so a "
            f"reader cannot tell a cut record from a whole one: {str(forged)[:200]}")
