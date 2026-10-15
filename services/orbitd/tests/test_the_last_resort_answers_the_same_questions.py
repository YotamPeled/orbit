"""The record a reader gets when the fitting itself has failed.

WHY THIS FILE EXISTS. Under everything else there is a floor: a function that builds a record
which fits no matter what the rounds above miscounted. It is reached when the arithmetic
surprises, which is to say on the day something is wrong -- and that is the day a reader most
needs the record to answer the same questions as any other.

It does not read the same declarations as the rest of the module. Every other route now asks
each field for the bound declared where it is set; the floor keeps a literal tuple of the five
names it believes say who acted, and a width of its own for them. That is the third instance on
this front of a rule written as a list of names, and this one sits where a record lands when
everything else has already failed: a name added to the identity set anywhere else is silently
absent exactly then.

Nothing is red here today, because the tuple currently matches the set. This file exists so
that the next name is a red row on the day it is added, rather than a record that quietly stops
saying who acted on the day the fitting breaks.

HOW IT IS DRIVEN, AND WHY THAT IS HONEST. The floor is unreachable by ordinary means -- by
construction, it runs only when the fitter is wrong. So the fitter is made wrong: its rounds are
replaced with a step that does nothing, which is precisely the condition the floor is documented
to survive. Everything else is real -- the same logging call, the same formatter, the same
record.

Where the names come from is the same answer as the contract beside this one: the logging call's
own signature, not the module's tuple. A contract that reads the list it is checking cannot see
a name missing from that list.

Run under pytest from services/orbitd.
"""

from __future__ import annotations

import ast
import inspect
import json
import logging
import pathlib

import pytest

from orbitd import audit


def _what_the_caller_is_asked_for() -> tuple[str, ...]:
    signature = inspect.signature(audit.log_audit)
    names = tuple(name for name, parameter in signature.parameters.items()
                  if parameter.kind is inspect.Parameter.KEYWORD_ONLY)
    assert len(names) >= 4, f"the logging call asks for {names}, which says almost nothing"
    return names


WHO_ACTED = _what_the_caller_is_asked_for()

#: Values a reader would want back: ordinary, short, nothing remarkable about any of them.
def _the_verdicts_this_code_writes() -> tuple[str, ...]:
    """Every decision word this surface really records, read off the surface.

    The review that found this file blind found it blind for one reason: every value it drove
    was shorter than the truncation mark, and the two questions it holds against each other --
    what the cutter decides, and what the floor decides -- can only differ on a value longer
    than that. A word invented here is a word chosen by the same hand that chose the
    assertion. These are chosen by the code that writes them, so the longest verdict this
    surface can record is driven the day it is added."""
    found: set[str] = set()
    for path in sorted(pathlib.Path(audit.__file__).parent.rglob("*.py")):
        tree = ast.parse(path.read_text())
        # A decision word is not always written at the call. Some are module constants and
        # some are a constant with a reason appended, and the longest decision this surface
        # records is one of those -- so a reader that sees only the literal at the call misses
        # exactly the words most likely to be lost.
        constants: dict[str, str] = {}
        for node in ast.walk(tree):
            if isinstance(node, ast.Assign) and isinstance(node.value, ast.Constant) \
                    and isinstance(node.value.value, str):
                for target in node.targets:
                    if isinstance(target, ast.Name):
                        constants[target.id] = node.value.value

        def said(node) -> str | None:
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                return node.value
            if isinstance(node, ast.Name):
                return constants.get(node.id)
            if isinstance(node, ast.JoinedStr):
                out = ""
                for part in node.values:
                    if isinstance(part, ast.Constant):
                        out += str(part.value)
                    else:
                        inner = said(part.value)
                        # What the reason says is the caller's; what stands in front of it is
                        # this code's, and that prefix is the decision a reader must keep.
                        out += inner if inner is not None else "a reason from the caller"
                return out
            return None

        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            for keyword in node.keywords:
                if keyword.arg == "verdict":
                    word = said(keyword.value)
                    if word:
                        found.add(word)
    assert found, "no verdict literal was found, so the drive below is invented after all"
    return tuple(sorted(found, key=lambda word: (-len(word), word)))


VERDICTS = _the_verdicts_this_code_writes()

#: How much of a decision a reader must get back even when the rest was cut: enough to tell
#: one decision from another. The shortest decision this surface writes sets it, because a
#: reader who can tell that one apart can tell any of them apart.
VERDICT_BEGINNING = min(VERDICTS, key=len)

#: The longest decision this code can record. The one most likely to be lost, and the one a
#: drive of short invented words can never reach.
THE_LONGEST_VERDICT = VERDICTS[0]

SAID = {"actor": "an-operator", "verdict": THE_LONGEST_VERDICT, "role": "operator",
        "tool": "orbit_project_list", "resource": "a-project", "needs": "admin"}


#: The sentence a real route writes: the deployment record, whose project, environment,
#: component and revision reach a reader through this sentence and nothing else, because the
#: floor carries the formatter's own fields, who acted and the decision, and drops the rest.
A_SENTENCE_A_ROUTE_WRITES = "deploy orbit_deploy demo/prod api v42: DEPLOYED"


def _the_floor(monkeypatch, said: dict | None = None, message: str = A_SENTENCE_A_ROUTE_WRITES,
               **extra) -> str:
    """One record written while the fitter's rounds achieve nothing, which is the condition the
    floor exists to survive. `said` replaces what the caller said about who acted; `message`
    is the sentence the route wrote; `extra` is what the caller sent besides."""
    said = said or {}
    monkeypatch.setattr(audit, "_shorten_planned", lambda *args, **kwargs: False)
    monkeypatch.setattr(audit, "_drop_planned", lambda *args, **kwargs: False)
    logger = logging.getLogger("orbitd.audit.floor")
    logger.setLevel(logging.INFO)
    logger.propagate = False
    written: list[str] = []

    class Capture(logging.Handler):
        def emit(self, record):
            _carrying_everything(record)
            written.append(self.format(record))

    handler = Capture()
    handler.setFormatter(audit.JsonOneLineFormatter())
    logger.handlers = [handler]
    try:
        audit.log_audit(logger, message, **{**SAID, **said}, **extra)
    finally:
        logger.handlers = []
    assert len(written) == 1
    return written[0]


def _an_exception():
    try:
        raise ValueError("something went wrong")
    except ValueError as error:
        return type(error), error, error.__traceback__


def _carrying_everything(record: logging.LogRecord) -> logging.LogRecord:
    """The record as the formatter really meets it: with an exception and a stack, the two
    fields the formatter writes as its own only when they are there. A drive without them
    holds nothing about them, which is how a floor that dropped both passed a row written to
    hold every own field."""
    record.exc_info = _an_exception()
    record.stack_info = "a stack"
    return record


#: Enough weight that no record could be written without the fitter having done something.
TOO_MUCH = {"payload": "w" * 20_000}


class TestTheDriveReallyReachesTheFloor:
    def test_without_the_floor_there_would_be_no_record(self, monkeypatch):
        """If a record this size came out fitted, the fitter is still working and nothing below
        is about the floor at all."""
        line = _the_floor(monkeypatch, **TOO_MUCH)
        assert "w" * 1_000 not in line, (
            "the oversized value came through whole, so the rounds this drive disabled are not "
            "the ones that do the fitting and this file measures the wrong thing")

    def test_what_the_floor_produces_is_a_record(self, monkeypatch):
        line = _the_floor(monkeypatch, **TOO_MUCH)
        assert line.count("\n") == 0
        assert len(line.encode()) <= audit.MAX_RECORD_LEN
        json.loads(line)


#: A field the formatter declares as its own and hands on with no value of its own yet.
_DECLARED_WITHOUT_A_VALUE = object()


def _what_the_formatter_declares_as_its_own() -> dict:
    """The fields the formatter declares as its own, with the values it wrote for them, read
    at the one place it hands them on: the bound per key it passes to the fitting. Not an
    example line -- a line shows only the fields that record happened to carry, and a row
    built on one held the level and the logger and let the exception and the stack go. The
    record driven here carries everything the formatter can read, and the row below says so."""
    seen: dict = {}
    real_fit = audit._fit

    def spy(payload, own=None):
        # Every key the formatter declares, whether or not this record carries a value
        # for it yet: the reserved field the fitting writes into is declared with no
        # value, and a spy that read only values let the floor stop writing it.
        seen.update({key: payload.get(key, _DECLARED_WITHOUT_A_VALUE) for key in (own or {})})
        return real_fit(payload, own)

    record = _carrying_everything(logging.LogRecord(
        "orbitd.audit.floor", logging.INFO, "p.py", 1, A_SENTENCE_A_ROUTE_WRITES, None, None))
    setattr(record, audit._SEALED_AUDIT_ATTR, dict(SAID))
    audit._fit = spy
    try:
        audit.JsonOneLineFormatter().format(record)
    finally:
        audit._fit = real_fit
    own = {key: value for key, value in seen.items() if key != "message"}
    assert {"exception", "stack"} <= set(own), (
        f"the drive did not reach the fields the formatter writes only sometimes: {sorted(own)}")
    assert audit.TRUNCATED_FIELD in own, (
        f"the drive did not see the reserved field the fitting writes into: {sorted(own)}")
    return own


THE_OWN_FIELDS = _what_the_formatter_declares_as_its_own()


class TestTheFloorAnswersTheSameQuestions:
    @pytest.mark.parametrize("field", sorted(THE_OWN_FIELDS))
    def test_every_field_the_formatter_owns_reaches_the_floor(self, monkeypatch, field):
        """Not the timestamp alone: the level, the logger, the exception, the stack and
        whatever else the formatter declares as its own. A floor that drops one of them
        loses something the fitter was told never to touch; until this row nothing noticed
        the level going, and until it read the declaration nothing noticed the exception."""
        record = json.loads(_the_floor(monkeypatch, **TOO_MUCH))
        assert field in record, (
            f"the floor does not carry {field!r}, which the formatter declares as its own. "
            f"Got: {sorted(record)}")
        if field != "time" and THE_OWN_FIELDS[field] is not _DECLARED_WITHOUT_A_VALUE:
            assert record[field] == THE_OWN_FIELDS[field], (
                f"the floor carries {field!r} as {record[field]!r}; the formatter wrote "
                f"{THE_OWN_FIELDS[field]!r}, and it is short enough that nothing was gained "
                "by changing it")

    def test_it_still_says_what_happened(self, monkeypatch):
        """The sentence the route wrote is the only place a deployment record names its
        project, environment, component and revision. The floor once replaced it with the
        bare mark whatever its size, so the record of a deployment that reached the floor said
        who deployed and not what."""
        record = json.loads(_the_floor(monkeypatch, **TOO_MUCH))
        assert record.get("message") == A_SENTENCE_A_ROUTE_WRITES, (
            f"the floor wrote the sentence as {record.get('message')!r}; it is "
            f"{len(A_SENTENCE_A_ROUTE_WRITES)} characters and the line had room for it")

    def test_a_sentence_too_long_for_the_line_is_cut_and_not_erased(self, monkeypatch):
        """The straddle: a sentence the cutter would halve must still begin as the route
        wrote it, because a cut sentence names the deployment and a mark names nothing."""
        long = A_SENTENCE_A_ROUTE_WRITES + " because " + "w" * 20_000
        record = json.loads(_the_floor(monkeypatch, message=long, **TOO_MUCH))
        written = record.get("message", "")
        assert written != audit.RECORD_TRUNCATED and written.startswith(
            A_SENTENCE_A_ROUTE_WRITES), (
            f"the floor wrote the oversized sentence as {written[:80]!r}: cut is fine, "
            "erased is the defect")
        assert len(json.dumps(record).encode()) <= audit.MAX_RECORD_LEN

    def test_it_still_says_when(self, monkeypatch):
        record = json.loads(_the_floor(monkeypatch, **TOO_MUCH))
        assert "time" in record, (
            "the floor lost the timestamp, so a record written on the day the fitting broke "
            "cannot be placed in history")

    @pytest.mark.parametrize("field", WHO_ACTED)
    def test_it_still_says_who_and_what_was_decided(self, monkeypatch, field):
        record = json.loads(_the_floor(monkeypatch, **TOO_MUCH))
        assert field in record, (
            f"the floor does not carry {field!r}, which the logging call asks every caller for: "
            "a name added to what a record says about who acted did not reach the one route a "
            f"record takes when everything else has failed. Got: {str(record)[:200]}")
        assert record[field] == SAID[field], (
            f"the floor carries {field!r} as {record[field]!r} rather than what the caller "
            f"said ({SAID[field]!r}); it is short enough that nothing was gained by cutting it")


class TestTheDriveStraddlesTheDecisionItHolds:
    """The guard the last four rounds were lost for want of. This file holds the floor's
    decision against the cutter's, and the two can only differ on a value the cutter would
    keep and a length question would cut. A drive whose values are all on one side of that
    boundary is green whatever the code does, which is how three rounds passed a floor that
    was erasing a decision this surface really writes."""

    def test_the_verdicts_were_read_off_the_code(self):
        assert len(VERDICTS) >= 5, (
            f"only {len(VERDICTS)} verdict literals were found in this surface, so the drive "
            "is effectively hand-written after all")

    def test_the_drive_reaches_past_the_truncation_mark(self):
        """If every decision this code writes were shorter than the mark, no drive could tell
        the two questions apart, and this file would have to say so rather than pass."""
        assert len(THE_LONGEST_VERDICT) >= len(audit.RECORD_TRUNCATED), (
            f"the longest decision this code records ({THE_LONGEST_VERDICT!r}, "
            f"{len(THE_LONGEST_VERDICT)} characters) is shorter than the truncation mark "
            f"({audit.RECORD_TRUNCATED!r}, {len(audit.RECORD_TRUNCATED)}), so nothing this "
            "file drives can distinguish a floor that keeps a decision from one that replaces "
            "it with the mark")

    @pytest.mark.parametrize("verdict", VERDICTS)
    def test_no_decision_this_code_writes_is_replaced_by_the_mark(self, monkeypatch,
                                                                  verdict):
        """Every decision word, not just the longest: a floor that keeps the short ones and
        loses the long one is exactly the defect, and only the whole set shows it."""
        record = json.loads(_the_floor(monkeypatch, {"verdict": verdict}, **TOO_MUCH))
        written = record.get("verdict")
        kept = audit._cut_string(verdict)
        if kept is None:
            assert written == verdict, (
                f"the floor recorded the decision {verdict!r} as {written!r}, though the "
                "cutter would not have cut it. A reader of this record cannot tell what was "
                "decided, and this is a word the surface really writes")
        else:
            # A long decision may be cut -- it carries a reason, and a reason can be any
            # length. What a reader must still get is the decision's first word whole, and
            # more than a mark: the shortest decision this code writes is four letters, so
            # "begins like a decision" alone would let four letters and a mark through.
            first_word = verdict.split()[0]
            assert written and written != audit.RECORD_TRUNCATED and written.startswith(
                first_word), (
                f"the floor recorded the decision {verdict!r} as {written!r}, losing the "
                "word it begins with. A reader is left with a mark where the decision was")
