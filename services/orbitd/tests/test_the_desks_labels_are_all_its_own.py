"""Every label the summary writes, and whether a host can write one too.

WHY THIS FILE EXISTS. The desk's summary frames a tool's answer as the tool's name, a colon,
the status, and then one `key=value` part for each field the answer carried. Those labels are
the desk's own words, so a host's text that contains one has to leave neutralised -- otherwise
the host is writing a part of the desk's sentence.

The desk knows its own marks from a list, and that list is a hand-written copy of the keys the
summary prints. A review added one carried field to the summary the way an ordinary change
would, left the list alone, and an operator read:

    orbit_project_list: ok detail=fine detail=the host wrote this

The second `detail=` is the host's. All hundred and forty-four rows of the desk's contracts
stayed green while that was true, because every one of them drives a label somebody remembered
to add to both places.

WHAT THIS FILE ASKS. Not that the two lists agree -- a contract that reads the lists it is
checking is the same defect one level up. It asks the summary what labels it writes, by giving
it an answer and reading the line that comes back, and then asks of each one: if a host sends
this label inside a value, does it reach the operator in the desk's voice or the host's.

So the day a field is added to the summary and nowhere else, the label is discovered here and
its row goes red.

Run under pytest from services/orbitd.
"""

from __future__ import annotations

import re

from orbitd import client

#: A tool's answer carrying a status and, with it, everything a summary might be asked to
#: carry. Keys the summary does not print simply do not appear in the line, so this over-asks
#: on purpose: the labels come from the answer, not from this list.
AN_ANSWER = {
    "status": "ok", "version": "1", "code": "E1", "what_failed": "a step", "detail": "fine",
    "reason": "because", "hint": "try again", "component": "api", "env": "dev",
}
TOOL = "orbit_project_list"

#: What a label looks like once written: letters, then the equals sign the desk puts after it.
A_LABEL = re.compile(r"(?<![\\\w])([a-z][a-z_]*=)")


def _labels_the_summary_writes() -> list:
    """Every label the summary really prints, read off a line it really wrote."""
    line = client._summary(TOOL, dict(AN_ANSWER))
    labels = sorted(set(A_LABEL.findall(line)))
    assert labels, f"the summary wrote no labelled part at all: {line!r}"
    return labels


LABELS = _labels_the_summary_writes()


class TestTheMeasurementIsWorthComparingAgainst:
    def test_the_summary_writes_more_than_one_label(self):
        assert len(LABELS) > 1, (
            f"only {LABELS} came back, so this file drives almost nothing; either the summary "
            "changed shape or the reading above is wrong")

    def test_an_ordinary_value_reaches_the_operator_whole(self):
        line = client._summary(TOOL, {"status": "ok", "what_failed": "a plain step"})
        assert "a plain step" in line, (
            f"an ordinary value does not survive the summary at all: {line!r}")


class TestAHostCannotWriteALabelOfTheDesks:
    def test_every_label_the_summary_writes_is_one_the_desk_knows_is_its_own(self):
        """The property, asked of each label the summary prints: a host value containing it
        must not reach the operator looking like another part of the desk's sentence.

        Driven one label at a time, and reported together, so a reader sees every label that
        is unprotected rather than the first."""
        unprotected = []
        for label in LABELS:
            forged = f"{label}the host wrote this"
            line = client._summary(TOOL, {"status": "ok", "what_failed": forged})
            # The desk's own copy of the label is written unescaped; the host's copy must not
            # be. So after the desk's part, no further unescaped label may appear.
            after = line.split("what_failed=", 1)[-1]
            if A_LABEL.search(after):
                unprotected.append(label)
        assert not unprotected, (
            f"a host that sends {unprotected} inside a value writes a part of the desk's own "
            "sentence: the operator cannot tell which half of the line the desk said. The "
            "labels the summary writes and the marks the desk protects are two lists, and "
            "these are the ones that fell out of step")
