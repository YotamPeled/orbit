"""One failure, two readers, and the rule that the form may differ and the content may not.

WHY THIS FILE EXISTS. The node before this one gave the desk a single way out: a command
decides an outcome and one writer renders it, a document when the caller asked for machine
output and a sentence otherwise. That repaired which path writes. A review then drove the
repaired desk and found the other half had gone wrong in the same move: the sentence builder
reads fields the failure envelope does not carry, so a person who asks for a tool that does
not exist and a person refused for want of a role are handed the same six words. Only the
exit code still tells them apart, and a person does not read exit codes.

The same review found the mirror of it. A caller that asked for a document is never told
about a refusal the desk itself made, because the writer keeps the desk's own notices in the
sentence and renders only the payload.

WHAT THIS FILE HOLDS, AND WHY IN THIS SHAPE. Not the spelling of either form -- three rounds
on this node were defeated by contracts that asked how a claim was spelled. The property is
that the two forms of one failure are about the same thing. So every row here drives the SAME
failure twice, once each way, and compares what the two callers were handed against each
other. A repair can then reword anything it likes and stay green, and cannot drop what one
reader is told without the other reader's copy noticing.

Run under pytest from services/orbitd.
"""

from __future__ import annotations

import io
import json
import re

import pytest
from conftest import MCP_URL
from orbitd.roles import Role
from test_login_address import Recorder, sign_in

pytestmark = pytest.mark.anyio

# Each row: a name, the argv without the machine flag, and what a reader must be able to
# tell this failure apart from. The rows are deliberately two failures of the same command.
FAILURES = (
    ("a tool that does not exist", "orbit_not_a_tool_here", {}),
    ("a call above the caller's role", "orbit_auth_revoke", {"jti": "Q1w2E3r4T5y6U7i8"}),
)


@pytest.fixture
def xdg(tmp_path, monkeypatch):
    """The desk's own session directory, per test, so a sign-in here touches nothing else."""
    d = tmp_path / "xdg"
    monkeypatch.setenv("XDG_CONFIG_HOME", str(d))
    return d

# Words every failure carries, which therefore say nothing about WHICH failure happened.
EMPTY = re.compile(r"\b(orbit|failed|error|failure|call|the|a|an|is|was|to|of|with|"
                   r"status|code|for|and|not)\b", re.I)


def _telling(text: str, tool: str = "", args: dict | None = None) -> set[str]:
    """The words in a piece of text that could tell one failure from another.

    The caller's own call is removed first, and that subtraction is the whole point of this
    file. A sentence that echoes the tool name back differs from the sentence for another
    tool, and says nothing at all: the reader typed that name. What is left after the words
    every failure carries and the words the caller supplied are taken away is the only part
    that is about which failure happened."""
    words = set(re.findall(r"[A-Za-z_][A-Za-z0-9_]{2,}", text.lower()))
    known = set(re.findall(r"[A-Za-z_][A-Za-z0-9_]{2,}", tool.lower()))
    for key, value in (args or {}).items():
        known |= set(re.findall(r"[A-Za-z_][A-Za-z0-9_]{2,}", f"{key} {value}".lower()))
    return {w for w in words - known if not EMPTY.fullmatch(w)}


async def _desk(app, state, xdg, capsys):
    """One signed-in desk that can be driven more than once. A reader signs in once and
    then makes both calls, which is also how a person meets these two failures."""
    from orbitd.client import async_main

    secret = state.store.add_principal("a-reader", Role.viewer, 3600)
    plain = Recorder(app)
    await sign_in(plain.factory, MCP_URL, "a-reader", secret)

    async def drive(tool, args, *, machine):
        """The same call, differing in nothing but what the caller asked to hold."""
        argv = ["call", tool, "--json", json.dumps(args)] if machine else \
            ["call", tool] + [f"--arg={k}={v}" for k, v in args.items()]
        capsys.readouterr()
        code = await async_main(argv, http_client_factory=plain.factory)
        out, err = capsys.readouterr()
        return code, out, err

    return drive


class TestAPersonCanTellOneRefusalFromAnother:
    """The defect this file exists for. A person asked for a tool that is not there and a
    person refused for want of a role must not read the same sentence."""

    async def test_two_unlike_refusals_do_not_read_alike(self, app, state, xdg, capsys):
        drive = await _desk(app, state, xdg, capsys)
        said = {}
        for name, tool, args in FAILURES:
            _, _, err = await drive(tool, args, machine=False)
            said[name] = err.strip()
        first, second = (said[n] for n, _, _ in FAILURES)
        assert first and second, "a refusal said nothing at all: " + repr(said)
        apart = {n: _telling(said[n], tool, args) for n, tool, args in FAILURES}
        left, right = (apart[n] for n, _, _ in FAILURES)
        assert left != right, (
            "a missing tool and a missing permission are handed the identical sentence, so "
            "the only thing that tells them apart is an exit code a person never sees: "
            + repr(first))

    @pytest.mark.parametrize("name,tool,args", FAILURES)
    async def test_the_sentence_says_what_went_wrong(self, app, state, xdg, capsys,
                                                     name, tool, args):
        """Anti-vacuity for the case above: two sentences could differ and neither say
        anything. Each must carry at least one word that is about this failure."""
        drive = await _desk(app, state, xdg, capsys)
        _, _, err = await drive(tool, args, machine=False)
        assert _telling(err, tool, args), (
            f"the sentence for {name} is made entirely of words every failure carries, so "
            "it tells a person nothing: " + repr(err[:200]))


class TestAMachineCallerAndAPersonAreToldTheSameThing:
    """The rule the writer states -- the form follows what the caller asked for -- with the
    half nothing held: the CONTENT must not follow it too. Whichever way a reader asked,
    the same failure must reach them."""

    @pytest.mark.parametrize("name,tool,args", FAILURES)
    async def test_the_person_is_told_what_the_document_says(self, app, state, xdg, capsys,
                                                             name, tool, args):
        drive = await _desk(app, state, xdg, capsys)
        _, _, err = await drive(tool, args, machine=False)
        _, _, doc = await drive(tool, args, machine=True)
        document = json.loads(doc)
        both = _telling(json.dumps(document), tool, args) & _telling(err, tool, args)
        assert both, (
            f"for {name} a person and a program are told different things: the document "
            f"says {sorted(_telling(json.dumps(document), tool, args))[:8]} and the "
            f"sentence says {sorted(_telling(err, tool, args))[:8]}")

    @pytest.mark.parametrize("name,tool,args", FAILURES)
    async def test_neither_reader_gets_the_other_ones_form(self, app, state, xdg, capsys,
                                                           name, tool, args):
        """Anti-vacuity: handing both readers the document would pass the case above and
        undo the node this one extends."""
        drive = await _desk(app, state, xdg, capsys)
        _, _, err = await drive(tool, args, machine=False)
        try:
            json.loads(err)
        except ValueError:
            pass
        else:
            raise AssertionError(
                f"a person who did not ask for machine output got one for {name}: "
                + repr(err[:200]))
        _, _, doc = await drive(tool, args, machine=True)
        json.loads(doc)


class ATerminal(io.StringIO):
    """A stream that says it is a terminal, which is the only thing standing between the
    suite and the interactive branch. Every stdin in the suite so far has been an ordinary
    string buffer, whose isatty() is False, so the confirmation question -- one of the four
    exits the node before this one repaired -- was never once executed."""

    def isatty(self) -> bool:
        return True


class TestTheQuestionAPersonIsAskedGoesOutTheOneWay:
    """Reverting this exit to a bare print left the whole suite green, because nothing here
    could reach it. The property is the writer's, not the question's wording: whoever asks,
    the form follows what the caller asked to hold."""

    QUESTION = ["Really do the thing?", "It cannot be undone."]

    def _ask(self, *, machine):
        from orbitd.client import _resolve_confirm

        answered = ATerminal("yes\n")
        accepted = _resolve_confirm(yes=False, no=False, stdin=answered,
                                    machine=machine, question=self.QUESTION)
        return accepted

    def test_a_person_at_a_terminal_is_actually_asked(self, capsys):
        capsys.readouterr()
        accepted = self._ask(machine=False)
        out, err = capsys.readouterr()
        assert accepted is True, "an answered question was not read as an answer"
        asked = out + err
        assert any(line in asked for line in self.QUESTION), (
            "a person at a terminal was asked nothing before the thing was done: "
            + repr(asked[:300]))

    def test_the_question_is_not_a_document_for_a_person(self, capsys):
        capsys.readouterr()
        self._ask(machine=False)
        out, err = capsys.readouterr()
        for line in (out + err).splitlines():
            if not line.strip():
                continue
            try:
                json.loads(line)
            except ValueError:
                continue
            raise AssertionError(
                "a person who did not ask for machine output was handed a document to "
                "answer: " + repr(line[:200]))

    def test_a_machine_caller_can_read_the_question_it_is_asked(self, capsys):
        """The other half, and the finding beside it: a caller holding documents must be
        able to read what it is being asked, not merely be told that it was asked."""
        capsys.readouterr()
        self._ask(machine=True)
        out, err = capsys.readouterr()
        documents = []
        for line in (out + err).splitlines():
            try:
                documents.append(json.loads(line))
            except ValueError:
                continue
        assert documents, "a machine caller was asked nothing it could parse"
        carried = json.dumps(documents)
        assert any(line in carried for line in self.QUESTION), (
            "a machine caller is told a question was asked and never told what it was, so "
            "it cannot answer: " + repr(carried[:300]))


class TestAFailureTheDeskHasNoBranchForStillLeavesByTheWriter:
    """Restoring the traceback exit left the whole suite green: no case drives a failure
    that is neither a protocol error, nor a transport error, nor a captured refusal. In a
    real run that path writes a Python traceback at the operator, ignores what the caller
    asked to hold, and takes its exit code from the interpreter."""

    class Unbranched(Exception):
        """A failure of a kind the desk has never heard of, which is the point."""

    async def test_it_does_not_leave_as_a_traceback(self, app, state, xdg, capsys):
        from orbitd.client import async_main

        secret = state.store.add_principal("a-reader", Role.viewer, 3600)
        plain = Recorder(app)
        await sign_in(plain.factory, MCP_URL, "a-reader", secret)

        def broken(headers=None):
            raise self.Unbranched("nothing the desk has a branch for")

        capsys.readouterr()
        try:
            code = await async_main(["call", "orbit_project_list", "--json", "{}"],
                                    http_client_factory=broken)
        except self.Unbranched as exc:
            raise AssertionError(
                "a failure the desk has no branch for left as a traceback past the writer, "
                "so the operator reads a stack trace and the exit code comes from the "
                f"interpreter: {exc}") from None
        out, err = capsys.readouterr()
        assert code != 0, "a failure the desk could not place reported success"
        assert err.strip(), "a failure the desk could not place said nothing at all"

    async def test_it_follows_what_the_caller_asked_to_hold(self, app, state, xdg, capsys):
        """A machine caller gets a document for this failure like it does for every other,
        and a person does not. The exit the writer was built for must not have a hole in it
        at the one failure nobody anticipated."""
        from orbitd.client import async_main

        secret = state.store.add_principal("a-reader", Role.viewer, 3600)
        plain = Recorder(app)
        await sign_in(plain.factory, MCP_URL, "a-reader", secret)

        def broken(headers=None):
            raise self.Unbranched("nothing the desk has a branch for")

        capsys.readouterr()
        await async_main(["call", "orbit_project_list", "--json", "{}"],
                         http_client_factory=broken)
        _, document = capsys.readouterr()
        assert document.strip(), "a machine caller was told nothing"
        json.loads(document)

        capsys.readouterr()
        await async_main(["call", "orbit_project_list", "--arg=a=1"],
                         http_client_factory=broken)
        _, sentence = capsys.readouterr()
        assert sentence.strip(), "a person was told nothing"
        try:
            json.loads(sentence)
        except ValueError:
            return
        raise AssertionError("a person who did not ask for machine output got one: "
                             + repr(sentence[:200]))
