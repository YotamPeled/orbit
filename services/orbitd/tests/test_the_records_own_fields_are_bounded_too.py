"""What survives when the field that is too big is one the formatter put there itself.

WHY THIS FILE EXISTS. A caller could once strip the record's timestamp by sending a long
enough tool name: the fitter shortened whatever was cheapest, and the time was cheap. That was
repaired by having the formatter name the fields it owns and telling the fitter never to
shorten, drop or rename them.

Protecting a field is not the same as bounding it. Two of the fields the formatter owns carry
text that came off the wire -- the exception, and the stack -- and neither has a size of its
own. When one of them alone passes the ceiling, every strategy refuses because everything left
is protected, the loop ends, the last resort copies the protected fields verbatim, that still
overflows, and what lands is a stub carrying a key the record never had. The time is gone, and
so are the actor, the role, the tool and the decision. The repair that saved the timestamp is
what destroys the record.

It needs no hostile caller. A background loop logs an exception around a live cluster diff,
and a diff of a large object produces a large message.

WHAT THIS FILE HOLDS. Not a size, and not a strategy. The property is that **a record is
still a record**: whatever the formatter itself put in it, however big, the reader still gets
the time, still gets who acted, and still gets one line inside the bound. Which field is
shortened to achieve that is the repair's choice -- but something must be, and the answer
cannot be "throw the record away".

The sizes below straddle the cliff that was measured rather than guessing at one, so a reader
can see where the behaviour changes and a later repair cannot quietly move it.

Run under pytest from services/orbitd.
"""

from __future__ import annotations

import io
import json
import logging

import pytest
from orbitd import audit

WHO = {"actor": "a-reader", "role": "viewer", "tool": "orbit_project_list"}


def _emit_with_exception(size: int) -> str:
    """One record through the real sink, carrying an exception whose message is `size`
    characters. The sink is the root formatter, so this is the path every record in the
    process takes."""
    stream = io.StringIO()
    saved = logging.getLogger().handlers
    audit.configure_logging(stream)
    try:
        log = logging.getLogger("orbitd.audit")
        try:
            raise RuntimeError("E" * size)
        except RuntimeError:
            # The shape the four real call sites use, one of them around a live cluster
            # diff. `log_audit` treats an exception argument as a field of the caller's, so
            # it never reaches the formatter's own set -- this is the path that does.
            log.exception("background loop %s failed", "shadow-diff")
    finally:
        logging.getLogger().handlers = saved
    return stream.getvalue()


# Either side of the cliff a review measured: below it the record is whole, above it the
# record was replaced by a stub. The rows are sizes so the place the behaviour changes stays
# visible; what is asserted is never a size.
SIZES = (
    ("comfortably below the ceiling", 1_000),
    ("just below the cliff", 7_800),
    ("just above the cliff", 8_000),
    ("far above the cliff", 60_000),
)


class TestTheMeasurementIsWorthComparingAgainst:
    def test_the_small_record_is_whole(self):
        """Anti-vacuity: the drive really reaches the formatter, and a record that fits
        carries everything the rows below ask for."""
        record = json.loads(_emit_with_exception(100))
        for key in ("time", "logger", "level", "message", "exception"):
            assert key in record, (
                f"the drive does not produce {key!r} even when small, so the rows below "
                f"measure nothing: {record}")
        assert "E" in str(record["exception"]), "the exception's own text never arrives"


class TestARecordSurvivesItsOwnOversizedField:
    @pytest.mark.parametrize("what,size", SIZES, ids=[row[0] for row in SIZES])
    def test_the_record_is_still_one_line_inside_the_bound(self, what, size):
        line = _emit_with_exception(size)
        assert line.count("\n") == 1, f"{what}: the record stopped being one line"
        assert len(line.encode()) <= audit.MAX_RECORD_LEN + 1, (
            f"{what}: the record passed the bound at {len(line.encode())} bytes")

    @pytest.mark.parametrize("what,size", SIZES, ids=[row[0] for row in SIZES])
    def test_the_record_can_still_be_placed_in_time(self, what, size):
        record = json.loads(_emit_with_exception(size))
        assert "time" in record, (
            f"{what}: an exception the formatter itself put in the record took the record's "
            f"own timestamp with it, so a reader cannot place it in history. What landed: "
            f"{record}")

    @pytest.mark.parametrize("what,size", SIZES, ids=[row[0] for row in SIZES])
    def test_the_reader_still_learns_what_failed_and_where(self, what, size):
        record = json.loads(_emit_with_exception(size))
        missing = [key for key in ("logger", "level", "message") if key not in record]
        assert not missing, (
            f"{what}: the record lost {missing} because a field the formatter owns was too "
            f"big, so what landed says something happened and not what or where: {record}")

    @pytest.mark.parametrize("what,size", SIZES, ids=[row[0] for row in SIZES])
    def test_the_record_is_not_replaced_by_something_else(self, what, size):
        """What lands today is a stub carrying a key the record never had. That is worse than
        losing a field: a reader parsing these cannot tell a cut record from a different kind
        of record, and nothing in it says a cut happened to a record of this shape."""
        record = json.loads(_emit_with_exception(size))
        assert set(record) - {"verdict"}, (
            f"{what}: the whole record was replaced by a stub: {record}")
        assert "exception" in record, (
            f"{what}: the reason the loop failed is gone, which is the one thing this record "
            f"exists to carry: {record}")
