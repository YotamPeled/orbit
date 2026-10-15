"""Whether what the sink writes is JSON that any reader can read.

WHY THIS FILE EXISTS. Every guarantee this node has built rests on one assumption nobody
checked: that the line the sink writes is JSON. It is written by Python's encoder, which by
default emits three tokens that JSON does not have -- `NaN`, `Infinity` and `-Infinity` --
for the three float values that have no JSON spelling. Python's own reader accepts them, so
every case in this suite that parses a record with the standard library passes while a
conforming reader refuses the whole line.

It needs nothing hostile and no unusual call. A duration that divided by zero, a ratio with
no denominator, a metric that came back empty: any of these reaches a record as an ordinary
field, and one of them makes the record unreadable to the thing that reads records.

What is lost is not the field. It is the line: a strict parser rejects the document, so the
actor, the decision, the time and everything else go with it.

WHAT THIS FILE HOLDS. That a reader which implements JSON, and nothing more, can read what
this sink writes -- whatever a caller put in a field. What a repair does with a value that
has no JSON spelling is its choice: report it as text, replace it, drop the field with the
mark. This file does not choose, and it does not name a spelling. It parses the line the way
a reader that has never heard of Python would, and requires the record to survive.

Run under pytest from services/orbitd.
"""

from __future__ import annotations

import io
import json
import logging

import pytest
from orbitd import audit

WHO = {"actor": "a-reader", "role": "viewer", "tool": "orbit_project_list",
       "verdict": "allowed"}


def _emit(**fields) -> str:
    """One record through the real sink -- the root formatter every record in this process
    goes through."""
    stream = io.StringIO()
    saved = logging.getLogger().handlers
    audit.configure_logging(stream)
    try:
        audit.log_audit(logging.getLogger("orbitd.audit"), "a record", **WHO, **fields)
    finally:
        logging.getLogger().handlers = saved
    return stream.getvalue()


def _strictly(line: str) -> dict:
    """Parse the line the way a reader that implements JSON and nothing else would: the three
    tokens Python writes for the floats JSON cannot spell are not JSON, so a reader that meets
    one rejects the document. Nothing else about this parse is unusual."""
    def refuse(token):
        raise ValueError(f"{token} is not a JSON value, so the whole line is refused")

    return json.loads(line, parse_constant=refuse)


# The values that have no JSON spelling, and the shapes a caller reaches them by. Each row is
# an ordinary arithmetic accident, not an attack: a ratio with no denominator, a duration that
# divided by zero, a measurement that never arrived.
NO_JSON_SPELLING = (
    ("a measurement that never arrived", {"latency": float("nan")}),
    ("a ratio with no denominator", {"ratio": float("inf")}),
    ("a countdown that went the other way", {"budget": float("-inf")}),
    ("one inside a list the caller sent", {"samples": [1.0, float("nan"), 3.0]}),
    ("one inside a mapping the caller sent", {"window": {"p99": float("inf")}}),
)


class TestTheMeasurementIsWorthComparingAgainst:
    def test_an_ordinary_record_reads_strictly(self):
        """Anti-vacuity: the strict parse is not refusing everything."""
        record = _strictly(_emit(latency=12.5, samples=[1, 2, 3]))
        assert record["latency"] == 12.5, record
        assert record["samples"] == [1, 2, 3], record

    def test_an_ordinary_number_still_arrives_as_a_number(self):
        record = _strictly(_emit(count=7))
        assert record["count"] == 7, f"an ordinary number stopped being a number: {record}"


class TestAnyReaderCanReadWhatTheSinkWrites:
    @pytest.mark.parametrize("what,fields", NO_JSON_SPELLING,
                             ids=[row[0] for row in NO_JSON_SPELLING])
    def test_the_line_is_still_json(self, what, fields):
        line = _emit(**fields)
        try:
            _strictly(line)
        except ValueError as refused:
            pytest.fail(
                f"{what}: the record is not JSON, so a reader that implements the format "
                f"refuses the whole line and loses the actor, the decision and the time "
                f"with it ({refused}). What was written: {line.strip()[:160]}")

    @pytest.mark.parametrize("what,fields", NO_JSON_SPELLING,
                             ids=[row[0] for row in NO_JSON_SPELLING])
    def test_the_rest_of_the_record_is_still_there(self, what, fields):
        """And the repair may not answer by throwing the record away: what the caller sent
        that had a JSON spelling, and who acted, are still in it."""
        record = _strictly(_emit(kept="a value with a spelling", **fields))
        missing = [key for key in (*WHO, "time", "kept") if key not in record]
        assert not missing, (
            f"{what}: the record lost {missing} because one field had no JSON spelling: "
            f"{str(record)[:200]}")

    @pytest.mark.parametrize("what,fields", NO_JSON_SPELLING,
                             ids=[row[0] for row in NO_JSON_SPELLING])
    def test_the_field_is_still_accounted_for(self, what, fields):
        """A reader should be able to tell that something was there. Its name survives, or
        the record says it was cut -- silently dropping a field a caller sent makes the record
        lie by omission."""
        record = _strictly(_emit(**fields))
        name = next(iter(fields))
        accounted = name in record or audit.RECORD_TRUNCATED in json.dumps(record)
        assert accounted, (
            f"{what}: the field {name!r} left the record with nothing to say it was ever "
            f"there: {str(record)[:200]}")
