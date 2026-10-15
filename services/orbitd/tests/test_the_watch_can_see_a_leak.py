"""Whether the watch beside this one can see the thing it is watching for.

WHY THIS FILE EXISTS. This node's recurring failure is a contract blind to its own subject,
and a review found it again in the very contract written to end it. The child-process watch
runs the real entry point and splits what it wrote into what the one writer produced and what
something else did. It does that by swapping the two module-level names `sys.stdout` and
`sys.stderr` for two files. Anything written below those names never reaches the files at
all, so it is not in what the writer produced, not in what leaked, and the case passes.

Proved on the newest repair's own function: a second way out spelled `sys.stderr` turns two
rows red, and the identical leak spelled `sys.__stderr__` or written straight to file
descriptor 2 leaves the whole suite green. The in-process sibling already states the
invariant is asked "at the file descriptors -- below the desk's own stream wrappers, below
sys.__stderr__, below anything a future repair might reach for". The child contract, which
exists precisely because the harness was the thing being measured, is the weaker of the two.

WHAT THIS FILE HOLDS. Not the desk: the watch. Each row plants a leak in the child by one
ordinary spelling and requires the watch to report it. A watch that cannot be made to fail is
not evidence that nothing leaked; it is evidence that nothing was looked at.

Run under pytest from services/orbitd.
"""

from __future__ import annotations

import pytest

import test_desk_outside_pytest as watched

MARK = "A-SECOND-WAY-OUT-THE-WATCH-MUST-SEE"

# Three ordinary ways a future repair reaches an output, all of them below the two names the
# watch currently swaps. The first is the one it can already see; it is here as the control.
SPELLINGS = (
    ("the stream name the watch swaps", f"sys.stderr.write({MARK!r}); sys.stderr.flush()"),
    ("the interpreter's own saved stream",
     f"sys.__stderr__.write({MARK!r}); sys.__stderr__.flush()"),
    ("the file descriptor itself", f"import os; os.write(2, {MARK!r}.encode())"),
)

# The leak is planted after the streams are in place and before the desk runs, so it is a
# genuine second way out during the command rather than noise around it.
ANCHOR = "code = 99"


@pytest.mark.parametrize("how,leak", SPELLINGS, ids=[h for h, _ in SPELLINGS])
class TestTheWatchSeesALeakHoweverItIsSpelled:
    def _leaky(self, monkeypatch, leak):
        assert ANCHOR in watched.CHILD, (
            "the child program changed shape and this file no longer knows where to plant "
            "a leak, so it is measuring nothing")
        monkeypatch.setattr(watched, "CHILD",
                            watched.CHILD.replace(ANCHOR, leak + "\n" + ANCHOR, 1))

    def test_the_watch_reports_it(self, tmp_path, monkeypatch, how, leak):
        self._leaky(monkeypatch, leak)
        _, _, leaked = watched.run(["call"], tmp_path)
        assert MARK in leaked, (
            f"a second way out written by {how} is invisible to the watch, so every row "
            "beside it passes whether or not the desk has one: "
            + repr(leaked[:300]))

    def test_the_watch_does_not_call_it_the_writers_work(self, tmp_path, monkeypatch,
                                                         how, leak):
        """The other half. A watch that swept the leak into what the writer produced would
        satisfy the case above by counting it as legitimate output."""
        self._leaky(monkeypatch, leak)
        _, inside, _ = watched.run(["call"], tmp_path)
        assert MARK not in inside, (
            f"a second way out written by {how} was counted as the one writer's own work")


def test_the_watch_is_quiet_when_nothing_leaks(tmp_path):
    """Anti-vacuity for all six rows above: a watch that reported a leak on every run would
    satisfy them and be worthless. Unplanted, the same command must come back clean."""
    _, inside, leaked = watched.run(["call"], tmp_path)
    assert inside.strip(), "the command wrote nothing, so this case measures nothing"
    assert not leaked.strip(), (
        "the watch reports a leak with nothing planted: " + repr(leaked[:300]))
