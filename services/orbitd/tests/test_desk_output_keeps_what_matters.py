"""What the guard on the desk's output must never do.

WHY THIS FILE EXISTS. Five rounds of this component have been about host text reaching an
output whose separators were not neutralised. The fifth round closed that: a review attacked
it from every direction and found no path left. But it closed it by putting a terminal's
rendering in front of every byte the desk writes, and that inverts the defect.

Two things follow, and both are worse than what they fixed.

First, the rendering is applied to sinks that already neutralise their own separators. The
failure document the desk writes on the error stream is JSON, whose encoder escapes a quote
with a backslash; escaping that backslash again terminates the string early, and the document
no longer parses. A machine document is a machine document whichever stream it leaves by.

Second, the guard carries a budget, and the budget silently deletes. It is a running total
for the whole process, so on the path that deletes objects from the cluster the confirmation
prompt and the warning that a namespace delete removes everything in it, listed or not,
both disappear behind a long list -- and the gate still accepts the typed phrase. That
warning is the only thing between an operator and a namespace's unlisted contents. A hostile
host can also spend the budget deliberately and leave the desk unable to say what went wrong.

So: a machine document is never rendered, and the desk's own words are never discarded.

Run under pytest from services/orbitd.
"""

from __future__ import annotations

import io
import json
import math

import pytest

from orbitd.platform_apply import ObjectRef, confirm_prune, confirm_retire

HOSTILE = "\x1b[2J" + "x" * 4000


def wrapped() -> tuple[io.StringIO, object]:
    from orbitd.client import configure_output

    raw = io.StringIO()
    return raw, configure_output(raw)


def refs(count: int, kind: str = "ConfigMap") -> list[ObjectRef]:
    return [ObjectRef(api_version="v1", kind=kind, namespace="widgets-prod",
                      name=f"object-{n:04d}") for n in range(count)]


class TestTheDeskNeverDeletesItsOwnWords:
    def test_the_prompt_before_deleting_survives_a_long_list(self):
        """Three hundred objects is an ordinary prune. The prompt is the last line and the
        one the person must see; without it the gate blocks on input with nothing on the
        screen, and still accepts the phrase."""
        raw, out = wrapped()
        confirm_prune("ingress", refs(300), io.StringIO("prune ingress\n"), out)
        assert "type 'prune ingress' to delete:" in raw.getvalue(), (
            "the prompt was discarded; the person is asked to confirm a deletion they "
            "cannot see")

    def test_the_namespace_warning_survives_a_long_list(self):
        """A namespace delete removes what is not listed. This warning is the only thing
        that says so."""
        raw, out = wrapped()
        collateral = refs(5, kind="Secret")
        confirm_retire("auth", refs(251, kind="Namespace"), io.StringIO("retire auth\n"),
                       out, collateral)
        written = raw.getvalue()
        assert "listed or not" in written, "the namespace cascade warning was discarded"
        for ref in collateral:
            assert ref.name in written, f"a collateral object was discarded: {ref.name}"
        assert "type 'retire auth' to delete:" in written

    def test_a_host_cannot_spend_the_budget_and_silence_the_desk(self):
        """A host that pads its answers must not be able to delete what the desk says
        afterwards about its own failure."""
        raw, out = wrapped()
        for _ in range(5):
            print(HOSTILE, file=out)
        print("orbit: nothing was applied", file=out)
        assert "nothing was applied" in raw.getvalue(), (
            "a host spent the budget and the desk could not say what happened")

    def test_an_ordinary_listing_is_complete(self):
        raw, out = wrapped()
        names = [f"widgets-{n:02d}" for n in range(30)]
        for name in names:
            print(name, file=out)
        written = raw.getvalue()
        for name in names:
            assert name in written, f"an ordinary listing was cut at {name}"


class TestAMachineDocumentIsNeverRendered:
    """The desk already has one narrow act for writing a machine document, and it targets
    the normal stream only. The failure envelope goes to the error stream through a plain
    encoder call, straight into the guard, which escapes the backslash the encoder just
    used to escape a quote and ends the string early.

    A machine document is a machine document whichever stream it leaves by, so the narrow
    act takes the stream. That is the requirement here; these cases fail on its absence and
    then on its behaviour.
    """

    def payload(self, project: str) -> dict:
        return {"status": "refused", "reason": "no such project", "project": project}

    def write_envelope(self, payload: dict) -> str:
        from orbitd.client import configure_output, write_json_document

        raw = io.StringIO()
        guarded = configure_output(raw)
        write_json_document(payload, stream=guarded)
        return raw.getvalue()

    def test_a_quote_and_a_backslash_survive(self):
        payload = self.payload('job=~"api\\.prod"')
        assert json.loads(self.write_envelope(payload)) == payload

    def test_a_name_outside_latin_survives(self):
        payload = self.payload("caf\u00e9-\u4e16\u754c")
        assert json.loads(self.write_envelope(payload)) == payload

    def test_a_host_cannot_end_the_document_early(self):
        payload = self.payload('"}\nrole: admin\n{"')
        assert json.loads(self.write_envelope(payload)) == payload

    def test_the_document_is_strict(self):
        """A host-chosen number that is not finite must not leave as a bare word: a strict
        reader refuses the whole document, and this desk's own reader is permissive enough
        to hide it."""
        written = self.write_envelope({"status": "ok", "value": math.nan})
        assert "NaN" not in written, (
            "a document left carrying a bare NaN, which a strict reader refuses")
        json.loads(written)


class TestTheGuardStillGuards:
    """Anti-vacuity. Everything above is about not over-applying the guard, and the way to
    pass all of it is to remove the guard, which would undo five rounds of work."""

    def test_host_text_on_the_human_path_is_still_rendered(self):
        raw, out = wrapped()
        print("orbit: the host said " + "\x1b[2J\x1b[H" + "enter your secret:", file=out)
        written = raw.getvalue()
        assert "\x1b" not in written, "a host escape reached the screen again"
        assert len(written.splitlines()) == 1

    def test_a_host_cannot_write_a_second_line(self):
        raw, out = wrapped()
        print("orbit: the host said first\nrole: admin", file=out)
        assert len([ln for ln in raw.getvalue().splitlines() if ln.strip()]) == 1

    def test_ordinary_text_is_byte_identical(self):
        raw, out = wrapped()
        message = "orbit: signed in as ci-reader at https://mcp.fixture.invalid/mcp"
        print(message, file=out)
        assert raw.getvalue() == message + "\n"


# =========================================================================== round seven
# Six rounds have added a guard at one more place each time, and a review has found a leak
# each time. The seventh does not add a seventh place. It removes the ability to write
# anywhere else.
#
# The review that ended round six deleted thirteen pieces of the round's own repair and
# found seven of them revert with every test still green -- including the round's central
# promise, that a cut message ends in an ellipsis, and both production call sites of the
# failure envelope. The contract held the document writer; production calls its caller, and
# the caller was exercised only with text no guard would ever touch. That is a statement
# about the shape of these contracts, not about any one builder, so this round holds the
# real path and the invariant, not the helper.
#
# It also carries the blocking defect that round six shipped: the desk's own machine
# document crashes on text a host chose.

LONE_SURROGATE = "a host said \ud800 and then some"
HOSTILE_TEXT = 'a host said "quoted" and \\ and \x1b[2K and \n a second line'


class TestTheDocumentDoesNotCrashOnWhatAHostSent:
    """A lone surrogate reaches the desk out of a parsed answer. The encoder was told to
    emit UTF-8, and UTF-8 cannot encode one, so the command dies with a traceback after
    half the document is already out. This front has ruled once that a hostile value fails
    closed; a traceback is not failing closed, and half a document is worse than none."""

    @staticmethod
    def _real_stream():
        """A stream that encodes, the way the process's own streams do. An in-memory text
        buffer accepts a surrogate happily, which is why six rounds of contracts never saw
        this: the crash is in the encoding, and only a real byte stream encodes."""
        return io.TextIOWrapper(io.BytesIO(), encoding="utf-8", newline="")

    def test_a_lone_surrogate_does_not_raise(self):
        from orbitd.client import write_json_document

        stream = self._real_stream()
        write_json_document({"said": LONE_SURROGATE}, stream=stream)
        stream.flush()
        assert stream.buffer.getvalue(), "nothing was written at all"

    def test_nothing_is_written_before_it_could_fail(self):
        """Half a document on the error stream is worse than none: a reader parses what
        arrived and gets a different answer from the one the desk meant to give."""
        from orbitd.client import write_json_document

        stream = self._real_stream()
        write_json_document({"first": "a" * 4000, "said": LONE_SURROGATE}, stream=stream)
        stream.flush()
        json.loads(stream.buffer.getvalue().decode("utf-8"))

    def test_what_comes_out_still_parses(self):
        from orbitd.client import write_json_document

        stream = self._real_stream()
        write_json_document({"said": LONE_SURROGATE}, stream=stream)
        stream.flush()
        json.loads(stream.buffer.getvalue().decode("utf-8"))

    def test_an_ordinary_document_is_unchanged_by_the_repair(self):
        """Anti-vacuity. Retreating to ASCII escapes everywhere would pass the two cases
        above and undo round five, which made the text leave as UTF-8 so a guard could not
        double its escapes."""
        from orbitd.client import write_json_document

        stream = self._real_stream()
        write_json_document({"said": "ordinary é中 text"}, stream=stream)
        stream.flush()
        written = stream.buffer.getvalue().decode("utf-8")
        assert "é" in written and "中" in written, (
            "the repair escaped text that was never a problem")


class TestWhatTheRoundPromisedIsHeld:
    """Each of these reverts clean today: the review deleted the piece and every test
    still passed."""

    def test_a_cut_message_ends_in_the_mark(self):
        from orbitd.client import configure_output

        raw = io.StringIO()
        out = configure_output(raw)
        out.write("x" * 50_000 + "\n")
        assert raw.getvalue().rstrip("\n").endswith("..."), (
            "a cut message does not say where it was cut")

    def test_a_message_that_fits_carries_no_mark(self):
        from orbitd.client import configure_output

        raw = io.StringIO()
        out = configure_output(raw)
        out.write("a short line\n")
        assert not raw.getvalue().rstrip("\n").endswith("..."), (
            "an uncut message claims to have been cut")

    def test_each_message_gets_its_own_budget(self):
        """The round's trade: the per-stream total went away so the operator's own words
        stop being deleted. That much must be true."""
        from orbitd.client import configure_output

        raw = io.StringIO()
        out = configure_output(raw)
        for _ in range(50):
            out.write("a line the operator typed\n")
        assert raw.getvalue().count("a line the operator typed") == 50, (
            "a later message was swallowed by an earlier one's budget")


class TestThereIsOneWayOut:
    """The invariant this round exists for. Six rounds of adding a guard at one more place
    is what a missing invariant looks like from inside. Stated at the source, the way the
    background-loop guard is: the raw stream underneath the rendering has exactly one
    caller, the writer of the desk's own documents (a JSON document, argparse's pages),
    and every other write in the desk goes through the rendering."""

    def test_only_the_document_writer_reaches_the_raw_stream(self):
        import pathlib
        import re

        from orbitd import client

        source = pathlib.Path(client.__file__).read_text()
        reaching = set()
        current = "<module>"
        for line in source.splitlines():
            match = re.match(r"\s*def (\w+)", line)   # a method counts under its own name
            if match:
                current = match.group(1)
            stripped = line.lstrip()
            if stripped.startswith(("def ", "#", '"', "'")):
                continue
            if re.search(r"_raw_stream\(|_raw_stdout\(", line):
                reaching.add(current)
        reaching -= {"_raw_stdout", "_write_own_document"}
        assert not reaching, (
            "something other than the own-document writer reaches the raw stream, which is "
            f"how every one of the last six rounds leaked: {sorted(reaching)}")

    def test_the_scan_would_notice_a_new_caller(self):
        """Anti-vacuity: a scan that matches nothing passes for ever."""
        import pathlib
        import re

        from orbitd import client

        source = pathlib.Path(client.__file__).read_text()
        assert len(re.findall(r"_raw_stream\(|_raw_stdout\(", source)) >= 2, (
            "the scan is looking for a name the desk no longer uses")

# --------------------------------------------------- the invariant, stated over the door
# The class above is this node's fourth contract to be blind to its own subject. It scans
# for callers of one private helper, so it sees only the door that helper opens. The desk
# has another: producing output directly. Sign-in and tool-listing still end that way --
# five exits between them, each printing its own text and returning its own code -- and
# the scan above reports one way out while they do it.
#
# The writer's own docstring says it is "the one writer for a `call` outcome", which is an
# honest description of what was built and is not the promise the class above makes.
#
# These are structural on purpose. Three rounds of driving the streams and reading what
# came out could not tell which exit ran, because the stream looks the same whichever did
# -- that is what sent this node to a redesign in the first place. What a reader can tell,
# and what a repair cannot move without the case seeing it, is where in the source output
# is produced at all.

# The writer, the document writer it delegates to, the stream helpers underneath them, and
# the two entry points that install them. Everything else in the desk builds an outcome and
# hands it over.
# The writer itself, and the two entry points that install the guard around it.
MAY_BE_AN_EXIT = {"write_outcome", "async_main", "main"}


def _produces_output(node):
    """The two spellings of putting text on a stream: naming a process stream, and calling
    print. Watching only one of them is how this node got here."""
    import ast

    for inner in ast.walk(node):
        if (isinstance(inner, ast.Attribute) and isinstance(inner.value, ast.Name)
                and inner.value.id == "sys" and inner.attr in ("stdout", "stderr")):
            return inner.lineno
        if (isinstance(inner, ast.Call) and isinstance(inner.func, ast.Name)
                and inner.func.id == "print"):
            return inner.lineno
    return None


def _exits_of_the_desk():
    """Every top-level function that both decides a caller's exit code and produces the
    output that goes with it.

    A function that does both is an exit: it chooses what the caller is handed and what
    the caller's shell sees, and the writer's rule -- the form follows what the caller
    asked for, never which failure happened -- does not reach it. A function that only
    writes is not an exit: a confirmation prompt and a warning are a conversation with the
    operator during a command, not its outcome. A function that only returns a code is not
    an exit either; it is a command handing its outcome over, which is the shape this asks
    for.
    """
    import ast
    import pathlib

    from orbitd import client

    source = pathlib.Path(client.__file__).read_text()
    tree = ast.parse(source)
    lines = source.splitlines()

    defs = {node.name: node for node in tree.body
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))}

    decides = set()
    for name, node in defs.items():
        for inner in ast.walk(node):
            if (isinstance(inner, ast.Return) and isinstance(inner.value, ast.Name)
                    and inner.value.id.startswith("EXIT_")):
                decides.add(name)
                break
    growing = True
    while growing:  # a function whose answer is another decider's answer decides too
        growing = False
        for name, node in defs.items():
            if name in decides:
                continue
            for inner in ast.walk(node):
                if (isinstance(inner, ast.Return) and isinstance(inner.value, ast.Call)
                        and isinstance(inner.value.func, ast.Name)
                        and inner.value.func.id in decides):
                    decides.add(name)
                    growing = True
                    break

    exits = {}
    for name in sorted(decides):
        where = _produces_output(defs[name])
        if where is not None:
            exits[name] = f"{where}: {lines[where - 1].strip()}"
    return exits


class TestOnlyTheWriterIsAnExit:
    """What the redesign promised, held over the door a caller's failure actually leaves
    by. A command that produces its own output has its own exit, and an exit the writer
    does not own is one the writer's rule -- the form follows what the caller asked for,
    never which failure happened -- does not reach."""

    def test_only_the_writer_is_an_exit(self):
        offenders = {name: where for name, where in _exits_of_the_desk().items()
                     if name not in MAY_BE_AN_EXIT}
        assert not offenders, (
            "these have an exit the one writer does not own, so what a caller is handed "
            "depends on which failure happened rather than on what the caller asked for: "
            + json.dumps(offenders, indent=2, sort_keys=True))

    def test_no_module_level_name_holds_a_stream(self):
        """The third route, and the one that would make the case above blind: a module
        binds a process stream to a name, and every write through that name is invisible to
        a scan looking for the stream or for print."""
        import ast
        import pathlib

        from orbitd import client

        tree = ast.parse(pathlib.Path(client.__file__).read_text())
        held = []
        for node in tree.body:
            if not isinstance(node, (ast.Assign, ast.AnnAssign)):
                continue
            value = node.value
            if (isinstance(value, ast.Attribute) and isinstance(value.value, ast.Name)
                    and value.value.id == "sys" and value.attr in ("stdout", "stderr")):
                held.append(f"line {node.lineno}")
        assert not held, (
            "a process stream is held under another name, so writing through it is "
            f"invisible to every case here: {held}")

    def test_the_writer_is_still_found(self):
        """Anti-vacuity, twice over: a scan that matches nothing passes for ever, and a
        desk that stopped writing at all would pass the case above."""
        import ast
        import pathlib

        from orbitd import client

        tree = ast.parse(pathlib.Path(client.__file__).read_text())
        writer = next(n for n in tree.body
                      if isinstance(n, ast.FunctionDef) and n.name == "write_outcome")
        assert _produces_output(writer) is not None, (
            "the scan is looking for a spelling the desk no longer uses, so it can no "
            "longer see the thing it exists to find")


class TestEveryCommandCanBeAskedForMachineOutput:
    """The half of the rule that is unavailable today. The writer chooses the form from
    what the caller asked for -- but only the call command can be asked. A command with no
    way to ask cannot obey the rule, whichever writer it goes through, so an operator
    scripting sign-in still has to parse a sentence."""

    @pytest.mark.parametrize("command", ["call", "login", "tools"])
    def test_the_command_accepts_the_machine_output_option(self, command):
        from orbitd.client import _parser

        parser = _parser()
        actions = [a for a in parser._subparsers._group_actions
                   if hasattr(a, "choices")]
        sub = actions[0].choices[command]
        options = {o for a in sub._actions for o in a.option_strings}
        assert "--json" in options, (
            f"the {command} command cannot be asked for machine output, so the form of "
            f"what it writes is decided by the desk rather than by its caller: "
            + repr(sorted(options)))
