"""Which fields the record protects, and on what grounds.

WHY THIS FILE EXISTS. Two rounds on this node drew the same line twice and both times drew
it in the same shape: a list of field names somebody must remember to extend. Round one said
the formatter's own fields are never shortened, because a caller could strip the timestamp.
Round two said some of them may be shortened after all, because an exception the formatter
itself put in the record destroyed the record, and it named those in a second list beside
the first. A review then found the line in the wrong place: the division is not what this
code chooses versus what came off the wire, it is BOUNDED versus UNBOUNDED. The logger name
is neither chosen by this code nor bounded by it -- this sink serves every library on the
root logger, and a logger name is whatever the code that made it passed -- so a long enough
one still destroys the record whole: the time, the actor, the role, the tool and the decision
all gone, and what lands is a stub of thirty bytes.

The front's rule is that a class found twice means the design is wrong. So this file does not
hold a third list. It holds the property that makes any list unnecessary, and the property is
about width, not about names.

WHAT THIS FILE HOLDS.

  1. Whatever the record carries, however big, the reader still gets a record: one line
     inside the bound, the time, who acted, and a mark saying something was cut. Asked once
     per ROUTE -- a way something outside this module sets a field the formatter owns.
  2. Every field the formatter owns is either covered by a route above, or keeps the same
     width whatever the drive carries. That is what "bounded by construction" means, said as
     a measurement rather than as a promise: a field somebody adds later that is neither
     bounded nor routed makes its width move, and this row goes red.
  3. A bound kept by spending everything the caller sent is not a record. However many
     fields a caller sends, something of what they sent is still readable.

The own fields are discovered by driving the formatter, not listed here: what the caller
never sent and the record carries anyway is the formatter's own. A field added to the
formatter later is therefore in this file's scope the day it is added.

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


def _emit(*, logger_name: str = "orbitd.audit", exception_size: int = 0,
          stack_size: int = 0, fields: dict | None = None,
          message: str = "a record") -> str:
    """One record through the real sink -- the root formatter every record in this process
    goes through. Each keyword is a way something outside the formatter decides how wide one
    of its fields will be."""
    stream = io.StringIO()
    saved = logging.getLogger().handlers
    audit.configure_logging(stream)
    try:
        log = logging.getLogger(logger_name)
        payload = dict(WHO)
        payload.update(fields or {})
        if exception_size:
            try:
                raise RuntimeError("E" * exception_size)
            except RuntimeError:
                # The shape the real background loops use. `log_audit` treats an exception
                # argument as a field of the caller's, so it never reaches the formatter's
                # own set; this is the path that does.
                log.exception(message)
        elif stack_size:
            log.warning(message, stack_info=True)
        else:
            audit.log_audit(log, message, **payload)
    finally:
        logging.getLogger().handlers = saved
    return stream.getvalue()


def _record(**kwargs) -> dict:
    return json.loads(_emit(**kwargs))


def _own_fields() -> set:
    """What the formatter puts in a record that nobody sent it. Discovered, so a field added
    to the formatter later is in scope the day it is added."""
    record = _record()
    own = set(record) - set(WHO) - {"message"}
    assert own, "the formatter added nothing of its own, so this file measures nothing"
    return own


# A route is a way something outside this module decides the width of one of the formatter's
# own fields. Each is ordinary: a library's logger name, a background loop logging an
# exception around a large object, a warning asked to carry its stack.
ROUTES = (
    ("a logger name from outside this tree", "logger_name"),
    ("an exception around a large object", "exception_size"),
    ("a stack the caller asked to keep", "stack_size"),
)

SIZES = (("just below the ceiling", 7_800), ("just above it", 8_200), ("far above", 60_000))


def _drive(route: str, size: int) -> dict:
    if route == "logger_name":
        return _record(logger_name="a." + "n" * size)
    return _record(**{route: size})


class TestTheMeasurementIsWorthComparingAgainst:
    def test_a_small_record_is_whole(self):
        record = _record()
        for key in ("time", "message", *WHO):
            assert key in record, f"the drive does not produce {key!r}: {record}"

    def test_the_formatter_really_owns_several_fields(self):
        own = _own_fields()
        assert len(own) >= 3, f"only {sorted(own)} came back as the formatter's own"

    @pytest.mark.parametrize("what,route", ROUTES, ids=[row[0] for row in ROUTES])
    def test_each_route_really_widens_a_field_the_formatter_owns(self, what, route):
        """Anti-vacuity for every row below: a route that changed nothing would let the
        whole file pass on a formatter that had no such field at all."""
        small = _emit() if route != "stack_size" else _emit(stack_size=1)
        big = json.dumps(_drive(route, 7_000))
        assert len(big) > len(small), (
            f"{what} did not make the record any wider, so the rows below drive nothing")


class TestARecordIsStillARecordWhateverItsOwnFieldsCarry:
    """The one property, asked once per route and size. Which field a repair shortens to
    achieve it is the repair's choice; that something is, and that the record survives, is
    not."""

    @pytest.mark.parametrize("what,route", ROUTES, ids=[row[0] for row in ROUTES])
    @pytest.mark.parametrize("how,size", SIZES, ids=[row[0] for row in SIZES])
    def test_it_is_one_line_inside_the_bound(self, what, route, how, size):
        line = json.dumps(_drive(route, size))
        assert len(line.encode()) <= audit.MAX_RECORD_LEN + 1, (
            f"{what} {how}: the record passed the bound at {len(line.encode())} bytes")

    @pytest.mark.parametrize("what,route", ROUTES, ids=[row[0] for row in ROUTES])
    @pytest.mark.parametrize("how,size", SIZES, ids=[row[0] for row in SIZES])
    def test_the_reader_can_still_place_it_in_time(self, what, route, how, size):
        record = _drive(route, size)
        assert "time" in record, (
            f"{what} {how}: a field the formatter itself put in the record took the "
            f"record's own timestamp with it, so a reader cannot place it in history: "
            f"{str(record)[:200]}")

    @pytest.mark.parametrize("what,route", ROUTES, ids=[row[0] for row in ROUTES])
    @pytest.mark.parametrize("how,size", SIZES, ids=[row[0] for row in SIZES])
    def test_the_record_is_not_replaced_by_a_stub(self, what, route, how, size):
        record = _drive(route, size)
        assert set(record) - {"verdict", audit.TRUNCATED_FIELD}, (
            f"{what} {how}: the whole record was replaced by a stub: {record}")
        assert "time" in record, (
            f"{what} {how}: what survived is not a record a reader can place in time: {record}")


class TestEveryOwnFieldIsEitherRoutedOrTheSameWidthEveryTime:
    """The row that makes the list unnecessary. A field the formatter owns that nothing here
    can widen has to be bounded by construction, and bounded by construction is a measurable
    thing: its width does not move, whatever the drive carries. A field added later that is
    neither bounded nor routed turns this red rather than waiting for the record it destroys.
    """

    ROUTED = {"logger", "exception", "stack"}

    def test_every_unrouted_own_field_keeps_its_width(self):
        quiet = _record()
        loud = _record(logger_name="a." + "n" * 4_000,
                       fields={f"f{i}": "v" * 300 for i in range(40)})
        moved = []
        for field in sorted(_own_fields() - self.ROUTED):
            if field not in quiet or field not in loud:
                moved.append(f"{field} is not in both records")
                continue
            if len(str(quiet[field])) != len(str(loud[field])):
                moved.append(f"{field}: {len(str(quiet[field]))} then "
                             f"{len(str(loud[field]))}")
        assert not moved, (
            "a field the formatter owns is neither bounded by construction nor driven by a "
            f"route in this file, so nothing holds what happens when it is huge: {moved}")

    def test_the_routed_fields_are_really_the_formatters_own(self):
        """And the other direction: a route that no longer names one of the formatter's own
        fields is measuring the caller's weight instead."""
        own = _own_fields() | {"exception", "stack"}
        assert self.ROUTED <= own, (
            f"{sorted(self.ROUTED - own)} is routed here and is not one of the formatter's "
            "own fields any more")


class TestABoundKeptBySpendingEverythingIsNotARecord:
    """A record cut until nothing the caller sent is readable has kept its bound and lost its
    purpose. Measured at field counts either side of where the behaviour changes."""

    COUNTS = (("a few fields", 20), ("many", 100), ("past the cliff", 300), ("far past", 500))

    @pytest.mark.parametrize("what,count", COUNTS, ids=[row[0] for row in COUNTS])
    def test_something_the_caller_sent_is_still_readable(self, what, count):
        record = _record(fields={f"f{i}": f"value-{i}-" + "x" * 200 for i in range(count)})
        theirs = [value for key, value in record.items()
                  if key.startswith("f") and isinstance(value, str)]
        readable = [value for value in theirs
                    if value.replace(audit.RECORD_TRUNCATED, "").strip(". ")]
        assert readable, (
            f"{what}: every one of the {len(theirs)} fields the caller sent was cut to the "
            f"bare mark, so the record kept its bound and says nothing: {str(record)[:200]}")

    def test_who_acted_survives_a_record_made_of_many_fields(self):
        record = _record(fields={f"f{i}": "x" * 200 for i in range(500)})
        missing = [key for key in WHO if key not in record]
        assert not missing, (
            f"the record lost {missing} to a caller who sent many fields, so it no longer "
            f"says who acted: {str(record)[:200]}")


class TestACallerCannotTakeOverAFieldTheFormatterOwns:
    """The repair that stopped a sealed field overwriting one of the formatter's own landed
    with no case beside it. This is that case."""

    def test_a_sealed_field_named_like_the_formatters_own_does_not_replace_it(self):
        """A caller cannot name a field `logger`: that is the parameter the writing verb
        takes. `time` and `level` it can, and did."""
        quiet = _record()
        record = _record(fields={"time": "not-a-time", "level": "not-a-level"})
        assert record.get("time") != "not-a-time", (
            f"a caller's field replaced the record's own timestamp: {record}")
        assert record.get("level") != "not-a-level", (
            f"a caller's field replaced the record's own level: {record}")
        assert len(str(record["time"])) == len(str(quiet["time"])), (
            "the record's own timestamp changed shape when a caller sent a field of the "
            "same name")
