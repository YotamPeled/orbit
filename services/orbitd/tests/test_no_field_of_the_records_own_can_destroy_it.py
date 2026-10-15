"""What the formatter reads that nobody in this code chose the width of.

WHY THIS FILE EXISTS. The record's own fields are now protected by a bound declared where
each field is set, which was the right change of shape: the fitter and the last resort read
one declaration instead of two lists of names. A review then found the declaration wrong
about one field and dangerous in its default.

Wrong about one: the level name is declared as a width this code chooses. It is not. Any
library in this process can rename a level, and the name reaches every record afterwards. A
level renamed to sixty thousand characters leaves a twenty-nine byte stub -- no time, no
actor, no decision.

Dangerous in its default: a field added to the formatter later with no declaration inherits
never-shorten, which is the fatal side of the two. A thread name added as a field does the
same thing to the record, and both contracts that exist for this pass while it happens.

And the reason they pass is a defect of mine. The guard I wrote says every own field is
either driven by a route named in the file or keeps the same width on every drive. A field
whose width comes from the logging machinery keeps the same width under drives that never
vary it, so the guard sees nothing.

WHAT THIS FILE HOLDS, AND WHY IT NAMES NO ROUTES. The formatter reads a record. Every string
on that record is something this code did not choose the width of -- the logger's name, the
level's name, the thread's name, the file's name, the caller's message. So the drive is not a
list of ways to widen a field: it is every string attribute a real record carries, widened one
at a time, taken from the record itself. A field the formatter starts reading next year is
covered the day it is read, and a bound somebody forgets to declare shows up here rather than
in production.

The property is the node's own, unchanged: a record is still a record. One line inside the
bound, the time, who acted, and a mark saying something was cut.

Run under pytest from services/orbitd.
"""

from __future__ import annotations

import io
import ast
import json
import logging
import pathlib

import pytest
from orbitd import audit

WHO = {"actor": "a-reader", "role": "viewer", "tool": "orbit_project_list",
       "verdict": "allowed"}

#: Wide enough to pass the ceiling on its own, in one attribute.
ENORMOUS = 60_000


def _an_exception() -> tuple:
    try:
        raise ValueError("something went wrong")
    except ValueError as error:
        return type(error), error, error.__traceback__


def _a_record(*, carrying_everything: bool = True) -> logging.LogRecord:
    """A record of the shape the formatter actually meets in this process.

    It carries an exception and a stack, and that is the correction this file most needed. The
    version before it built a record with neither, so two fields the formatter reads -- the
    exception's text and the stack's -- were outside every row below, and the guard written to
    report a field the sweep cannot move read its field list off a line that same record
    produced. A guard that asks a record about a field the record never had cannot answer, and
    it reported nothing while a stack of sixty thousand characters reduced the record to
    twenty-nine bytes."""
    record = logging.LogRecord("orbitd.audit", logging.INFO, "p.py", 1,
                               "a record", None, _an_exception() if carrying_everything else None)
    if carrying_everything:
        record.stack_info = "a stack"
    setattr(record, audit._SEALED_AUDIT_ATTR, dict(WHO))
    return record


#: What the formatter reads off a record that is not one of its strings. An exception is a
#: triple and a traceback, and its text is built from all of it; the drive for it is therefore
#: an exception whose own message is enormous, which is what a real one looks like when a
#: library puts a response body in it. Named here as drives, not as routes: each one is a thing
#: the record carries, and the row below asks the same question of it as of every string.
def _a_record_carrying_a_huge_exception() -> logging.LogRecord:
    record = _a_record()
    try:
        raise ValueError("W" * ENORMOUS)
    except ValueError as error:
        record.exc_info = (type(error), error, error.__traceback__)
    return record


NOT_STRINGS = (("the exception it was given", _a_record_carrying_a_huge_exception),)


def _every_string_the_formatter_can_read() -> list:
    """Every string a real record carries. Not a list of routes: the record is asked what it
    holds, so a field the formatter starts reading later is covered the day it is read."""
    names = sorted(key for key, value in vars(_a_record()).items()
                   if isinstance(value, str))
    assert len(names) > 4, f"a record carries only {names}, so this file drives almost nothing"
    return names


def _formatted(attribute: str | None = None, width: int = ENORMOUS,
               record: logging.LogRecord | None = None) -> str:
    """One record through the real formatter, with one of its strings made enormous."""
    record = record if record is not None else _a_record()
    if attribute is not None:
        setattr(record, attribute, "W" * width)
    formatter = audit.JsonOneLineFormatter()
    return formatter.format(record)


ATTRIBUTES = _every_string_the_formatter_can_read()


def _without_the_clock(line: str) -> dict:
    """The record as written, less the one field the formatter takes from the
    clock. Two lines formatted a millisecond apart differ in that field alone,
    and a guard that compared whole lines read that tick as the attribute being
    read: it then skipped or ran an attribute's rows depending on when the clock
    moved, and the suite's skip count moved with it."""
    record = json.loads(line)
    record.pop("time", None)
    return record


class TestTheMeasurementIsWorthComparingAgainst:
    def test_an_ordinary_record_is_whole(self):
        record = json.loads(_formatted())
        for key in ("time", *WHO):
            assert key in record, f"the drive does not produce {key!r}: {record}"

    @pytest.mark.parametrize("attribute", ATTRIBUTES)
    def test_widening_this_string_really_changes_the_record(self, attribute):
        """Anti-vacuity: an attribute the formatter never reads would let its rows below pass
        without measuring anything. Such an attribute is not a defect -- it is simply not in
        this file's reach -- so it is skipped, loudly, rather than passing quietly."""
        small = _without_the_clock(_formatted())
        wide = _without_the_clock(_formatted(attribute, width=3_000))
        if wide == small:
            pytest.skip(f"the formatter does not read {attribute!r}, so nothing here can "
                        "reach it")


class TestARecordSurvivesEveryStringItWasHandedTaggedWide:
    """One property, asked once per string a record carries."""

    @pytest.mark.parametrize("attribute", ATTRIBUTES)
    def test_the_line_is_one_line_inside_the_bound(self, attribute):
        line = _formatted(attribute)
        assert line.count("\n") == 0, f"{attribute}: the record stopped being one line"
        assert len(line.encode()) <= audit.MAX_RECORD_LEN, (
            f"{attribute}: the record passed the bound at {len(line.encode())} bytes")

    @pytest.mark.parametrize("attribute", ATTRIBUTES)
    def test_the_reader_can_still_place_it_in_time(self, attribute):
        record = json.loads(_formatted(attribute))
        assert "time" in record, (
            f"a record whose {attribute} was enormous lost its own timestamp, so a reader "
            f"cannot place it in history: {str(record)[:200]}")

    @pytest.mark.parametrize("attribute", ATTRIBUTES)
    def test_who_acted_survives(self, attribute):
        record = json.loads(_formatted(attribute))
        missing = [key for key in WHO if key not in record]
        assert not missing, (
            f"a record whose {attribute} was enormous lost {missing}, so it no longer says "
            f"who acted or what was decided: {str(record)[:200]}")


class TestNothingTheFormatterWritesIsOutOfThisFileSReach:
    """The guard that keeps the sweep from going blind, and the one my last contract for this
    node lacked.

    The sweep drives the record's own strings. A field the formatter starts writing from
    somewhere else -- a global, a process-wide table, a name some other library chose -- is
    then outside every row above, and the file passes while the record dies. That is exactly
    how the level name got through: it is declared a width this code chooses, but any library
    can rename a level, and no drive here varies it.

    So the field list is taken from the formatted line, not from this file, and each field the
    formatter writes must be reachable by widening one of the record's own strings. A field
    nothing here can move is reported, with its name, as a field whose width nobody in this
    process controls."""

    def _own_fields(self) -> list:
        record = json.loads(_formatted())
        placed_by_the_caller = set(WHO) | {"message"}
        # The time is stamped by the formatter from the clock, not read off the record, and
        # this code does choose its width.
        return sorted(set(record) - placed_by_the_caller - {"time"})

    def test_the_formatter_writes_something_of_its_own(self):
        assert self._own_fields(), (
            "the formatter wrote no field of its own, so every row in this file is vacuous")

    def test_every_field_the_formatter_writes_can_be_moved_from_the_record(self):
        control = json.loads(_formatted())
        unreachable = []
        for field in self._own_fields():
            moved = any(json.loads(_formatted(attribute, width=3_000)).get(field)
                        != control.get(field) for attribute in ATTRIBUTES)
            moved = moved or any(
                json.loads(_formatted(record=make())).get(field) != control.get(field)
                for _how, make in NOT_STRINGS)
            if not moved:
                unreachable.append(field)
        assert not unreachable, (
            f"the formatter writes {unreachable} from something that is not the record, so no "
            "row in this file measures what happens when that text is enormous -- and whoever "
            "sets it is outside this process's control")


class TestARecordSurvivesWhatItCarriesThatIsNotAString:
    """The same property, asked of what the record carries that no attribute of it spells: the
    exception. Its text is built from a class, an instance and a traceback, so widening a
    string on the record never reaches it -- which is precisely how it sat outside this file
    until a review drove a stack of sixty thousand characters and got a twenty-nine byte record
    back."""

    @pytest.mark.parametrize("how,make", NOT_STRINGS, ids=[row[0] for row in NOT_STRINGS])
    def test_the_line_is_one_line_inside_the_bound(self, how, make):
        line = _formatted(record=make())
        assert line.count("\n") == 0, f"{how}: the record stopped being one line"
        assert len(line.encode()) <= audit.MAX_RECORD_LEN, (
            f"{how}: the record passed the bound at {len(line.encode())} bytes")

    @pytest.mark.parametrize("how,make", NOT_STRINGS, ids=[row[0] for row in NOT_STRINGS])
    def test_the_record_still_says_when_and_who(self, how, make):
        record = json.loads(_formatted(record=make()))
        missing = [key for key in ("time", *WHO) if key not in record]
        assert not missing, (
            f"a record carrying {how} lost {missing}, so it no longer says when it happened or "
            f"who acted: {str(record)[:200]}")

    @pytest.mark.parametrize("how,make", NOT_STRINGS, ids=[row[0] for row in NOT_STRINGS])
    def test_the_drive_really_reaches_the_formatter(self, how, make):
        assert _formatted(record=make()) != _formatted(), (
            f"{how} changes nothing in the record, so the rows above measure nothing")


# ---------------------------------------------------------------------------
# The default a forgotten declaration inherits.
#
# The review that found this file blind found it blind twice, and the second was this: revert
# the default and the whole suite stays green. The file's own prose names the record that
# would show it -- a field added to the formatter later with no bound line, a thread name a
# library chose, widened to sixty thousand characters -- and the file never builds one.
#
# It cannot build one by driving. Every field the formatter writes today declares its bound
# on the line that sets it, so there is no record any caller or any library can send that
# reaches the default. The default is reached only by a maintainer adding a line, which is
# next year's record, not this year's.
#
# So this section asks the code instead of a record, and says why. It reads the line that
# supplies the default and requires it to be the shortenable side, and it holds the reason
# beside it: of the two ways to be wrong, losing some text beats losing the record. The row
# below it shows what the two sides really do, so the constant names above are not taken on
# trust.
# ---------------------------------------------------------------------------

def _the_default_a_forgotten_declaration_gets() -> str:
    """The second argument of the formatter's own `setdefault` over its own fields, read off
    the module. Returns the name as written, so the assertion names what a maintainer would
    read on that line."""
    source = pathlib.Path(audit.__file__).read_text()
    found = []
    for node in ast.walk(ast.parse(source)):
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and node.func.attr == "setdefault" and len(node.args) == 2):
            target = getattr(node.func.value, "id", "")
            if target == "bounds":
                found.append(ast.unparse(node.args[1]))
    assert len(found) == 1, (
        f"the formatter supplies a default for a forgotten bound in {len(found)} places, so "
        "the one this file holds is not the one a maintainer would meet: " + str(found))
    return found[0]


class TestAForgottenDeclarationLosesTextAndNotTheRecord:
    def test_the_seam_that_supplies_the_default_is_still_there(self):
        """Anti-vacuity, and the whole reason this row reads code rather than a record: a
        refactor that moves the default somewhere else must be noticed here, because no drive
        can notice it."""
        assert _the_default_a_forgotten_declaration_gets()

    def test_a_forgotten_declaration_is_shortened_rather_than_protected(self):
        assert _the_default_a_forgotten_declaration_gets() == "WIRE_SIZED", (
            "a field added to the formatter with no bound line beside it inherits "
            f"{_the_default_a_forgotten_declaration_gets()}, which means never shortened. A "
            "thread name a library chose, or any other field whose width this code does not "
            "choose, then destroys the record it was added to explain. Of the two ways to be "
            "wrong -- losing some text, or losing the record -- this is the fatal one")

    @pytest.mark.parametrize("bound,keeps_the_record", [("WIRE_SIZED", True),
                                                        ("BOUNDED", False)])
    def test_what_the_two_sides_really_do(self, bound, keeps_the_record):
        """So the names above are not taken on trust. An own field whose width came from
        outside is driven at both declarations, through the field that really has one."""
        with_bound = getattr(audit, bound)
        formatter = audit.JsonOneLineFormatter()
        record = _a_record(carrying_everything=False)
        record.name = "W" * ENORMOUS
        import unittest.mock as mock
        real = audit.JsonOneLineFormatter.format

        def formatting(self, given):
            saved = audit.WIRE_SIZED
            try:
                audit.WIRE_SIZED = with_bound
                return real(self, given)
            finally:
                audit.WIRE_SIZED = saved

        with mock.patch.object(audit.JsonOneLineFormatter, "format", formatting):
            line = formatter.format(record)
        whole = json.loads(line)
        survived = all(key in whole for key in ("time", *WHO))
        assert survived is keeps_the_record, (
            f"an own field whose width came from outside, declared {bound}, "
            f"{'kept' if survived else 'destroyed'} the record: {line[:200]!r}. The default "
            "this file holds rests on these two behaving differently")
