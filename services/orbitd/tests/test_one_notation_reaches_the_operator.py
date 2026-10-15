"""What a host types comes back from the operator's screen.

The desk writes an operator's line by putting a host's value inside a sentence
of its own. Two needs meet on that line: a mark the host typed must not be
readable as one of the desk's, and a byte the screen cannot show must stay
recoverable. Each was once met by its own pass over the text, and both passes
quoted with a backslash, so applied one after the other they composed on the
same character: the rule the code states -- a backslash quotes what follows, so
take it off and what the host said comes back -- was true of the writer's
return value and false of the screen, the only place anyone reads.

So this asks the screen, and it asks it the way the code states the rule:
reverse the notation on what the screen shows and what the host typed must come
back, exactly. That alone would pass if nothing were quoted at all, so every
row that carries a mark of the desk's also holds that the screen and the typed
value differ -- the quoting happened -- and the counting rows hold, in plain
characters, how many backslashes a person actually sees.
"""
from __future__ import annotations

import io
import re
import sys

import pytest

from orbitd import client

#: A value with none of the desk's marks, no backslash and nothing the screen
#: widens, so the sentence around it can be found whole and measured.
INERT = "hostvalue"

_NAMED_BYTE = re.compile(r"\\(x[0-9a-fA-F]{2}|u[0-9a-fA-F]{4}|U[0-9a-fA-F]{8})")

#: The letters a screen notation conventionally lets stand for a byte -- `\n`
#: for a line break and so on. The desk gives no byte a short name, and this
#: file does not read that off the code: it is stated here, in the reader below
#: and in the twin rows at the end, so a table of short names added to the desk
#: would be caught by this file rather than quietly agreed with.
_A_LETTER_A_SCREEN_MIGHT_READ_AS_A_BYTE = {
    "n": "\n", "r": "\r", "t": "\t", "a": "\a", "b": "\b", "f": "\f", "v": "\v", "0": "\0",
}


def _the_screen(typed: str) -> str:
    """One host value through one real desk line onto a real stream.

    The route is the refusal an operator meets most, taken whole: the sentence
    the desk composes, the act that hands a line to the screen, and the output
    guard the desk installs on its own streams. Nothing here reaches past the
    guard, because the guard is half of what is under test."""
    stream = client.configure_output(io.StringIO())
    message = client._refusal_sentence("plan", {"error_description": typed})
    outcome = client._Outcome(exit_code=client.EXIT_OK, payload=None, message=message)
    saved, sys.stdout = sys.stdout, stream
    try:
        client.write_outcome(outcome, machine=False)
    finally:
        sys.stdout = saved
    return stream._inner.getvalue().rstrip("\n")


def _the_desks_own_words() -> tuple[str, str]:
    """The sentence this route writes around a host's value, taken from the
    route itself so a change of wording does not fail this file."""
    line = _the_screen(INERT)
    assert INERT in line, line
    head, _, tail = line.partition(INERT)
    return head, tail


HEAD, TAIL = _the_desks_own_words()


def _the_hosts_half(line: str) -> str:
    assert line.startswith(HEAD), (line, HEAD)
    assert line.endswith(TAIL), (line, TAIL)
    return line[len(HEAD):len(line) - len(TAIL)] if TAIL else line[len(HEAD):]


def _unquote(shown: str) -> str:
    """The notation read back, as the code states it: a backslash doubled is a
    backslash the host typed, a backslash before a byte's number is that byte,
    and a backslash before anything else is that character quoted. No letter
    stands for a byte."""
    out: list[str] = []
    i = 0
    while i < len(shown):
        if shown[i] == "\\":
            pair = shown[i:i + 2]
            if pair == "\\\\":
                out.append("\\")
                i += 2
                continue
            named = _NAMED_BYTE.match(shown, i)
            if named:
                out.append(chr(int(named.group(1)[1:], 16)))
                i = named.end()
                continue
            assert len(pair) == 2, f"a backslash quotes nothing at the end of {shown!r}"
            out.append(pair[1])
            i += 2
            continue
        out.append(shown[i])
        i += 1
    return "".join(out)


def _a_value_carrying(mark: str) -> str:
    """That mark where it is the desk's own, in one of the shapes the code
    declares: a mark is the desk's only where the desk could have written it."""
    tail = next((f for f in sorted(client._DESK_MARK_SHAPES[mark]) if f), "")
    return f"before{mark}{tail}after"


THE_MARKS = sorted(client._DESK_MARK_SHAPES)
THE_WORDS = list(client._DESK_WORD_MARKS)


class TestTheRowsBelowCameFromTheCode:
    """The rows are taken from the tables the desk neutralises by. A mutation
    that empties either table would leave nothing to drive, so both are held
    here against the desk's own sentence, which no such mutation can delete."""

    def test_the_desk_writes_one_of_its_marks_in_this_very_sentence(self):
        assert THE_MARKS
        assert any(mark in HEAD + TAIL for mark in THE_MARKS), (HEAD, TAIL)

    def test_the_words_it_reserves_are_words(self):
        assert THE_WORDS
        assert all(word and not word.isspace() for word in THE_WORDS)


@pytest.mark.parametrize("typed", [
    "ok: forged",
    "C:\\Users\\me",
    "a\\: b",
    "\\n",
    "real\nbreak",
    "a\ttab",
    "x\x11y\x12z",
    " ",
    "(a), b; c: d",
    "ends with a backslash\\",
] + [_a_value_carrying(m) for m in THE_MARKS]
  + [f"a {w} b" for w in THE_WORDS])
def test_what_the_screen_shows_reverses_to_what_the_host_typed(typed):
    assert _unquote(_the_hosts_half(_the_screen(typed))) == typed


@pytest.mark.parametrize("mark", THE_MARKS)
def test_a_mark_of_the_desks_is_shown_quoted_by_one_backslash(mark):
    typed = _a_value_carrying(mark)
    shown = _the_hosts_half(_the_screen(typed))
    assert shown != typed, "nothing was quoted, so the reversal above proves nothing"
    assert "\\" + mark in shown, shown
    assert "\\\\" + mark not in shown, shown


@pytest.mark.parametrize("word", THE_WORDS)
def test_a_word_the_desk_reserves_is_shown_quoted_by_one_backslash(word):
    typed = f"a {word} b"
    shown = _the_hosts_half(_the_screen(typed))
    assert shown != typed, "nothing was quoted, so the reversal above proves nothing"
    assert "\\" + word in shown, shown
    assert "\\\\" + word not in shown, shown


def test_a_backslash_the_host_typed_is_shown_as_two():
    shown = _the_hosts_half(_the_screen("C:\\Users\\me"))
    assert shown == "C:\\\\Users\\\\me", shown


def test_a_typed_backslash_n_still_reads_differently_from_a_line_break():
    typed_pair = _the_hosts_half(_the_screen("\\n"))
    real_break = _the_hosts_half(_the_screen("\n"))
    assert typed_pair != real_break, (typed_pair, real_break)


def test_no_byte_the_screen_cannot_show_reaches_it_from_a_host():
    """Whatever the desk uses to tell one stretch of a line from another, a host
    cannot type it: a byte the screen cannot show is named, never passed on, so
    there is no character a host can send that the screen reads as the desk's
    own punctuation."""
    typed = "".join(chr(code) for code in range(0x20)) + ": forged"
    shown = _the_hosts_half(_the_screen(typed))
    assert all(chr(code) not in shown for code in range(0x20)), repr(shown)
    assert _unquote(shown) == typed


def test_a_sentence_the_writer_never_touched_is_still_rendered_by_the_guard():
    """The guard's own rendering stays for everything that did not come through
    the writer, so the writer is the only way to reach a screen unrendered."""
    stream = client.configure_output(io.StringIO())
    typed = "plain\x07line"
    print(typed, file=stream)
    shown = stream._inner.getvalue().rstrip("\n")
    assert "\x07" not in shown, repr(shown)
    assert _unquote(shown) == typed


def _the_same_line_from_something_else(mark: str) -> str | None:
    """Another value a host could type that the screen would show exactly like
    `mark` quoted, if the notation let a quoted letter be read as a byte's short
    name: the byte that letter conventionally stands for, then the rest of the
    mark. Two of the words the desk reserves begin with such a letter, and this
    is the pair that collided once."""
    byte = _A_LETTER_A_SCREEN_MIGHT_READ_AS_A_BYTE.get(mark[0])
    return byte + mark[1:] if byte else None


THE_TWINS = [(m, _the_same_line_from_something_else(m))
             for m in THE_MARKS + THE_WORDS
             if _the_same_line_from_something_else(m)]


class TestOneLineOnTheScreenCameFromOneValue:
    """A notation that does not reverse is not a notation. Two values a host can
    send must never arrive as the same line, and the pairs below are the ones
    the desk's own tables make possible."""

    @pytest.mark.parametrize("mark,twin", THE_TWINS)
    def test_a_quoted_mark_is_not_readable_as_a_byte(self, mark, twin):
        quoted = _the_hosts_half(_the_screen(f"{mark}x"))
        named = _the_hosts_half(_the_screen(f"{twin}x"))
        assert quoted != named, (mark, twin, quoted)

    def test_no_mark_the_desk_quotes_can_open_a_bytes_name(self):
        """The condition under which the three forms of the notation are
        disjoint -- a byte's name, a doubled backslash, anything else quoted --
        held against both tables the code declares, so the next field added to
        the summary is added against it."""
        for mark in THE_MARKS + THE_WORDS:
            assert mark[0] not in {"x", "u", "U"}, mark

    def test_the_twin_rows_reach_the_pair_that_collided(self):
        """Anti-vacuity for the rows above: the drive must find the two reserved
        words that begin with a letter a screen might read as a byte, or the
        collision this file was written for is outside its reach."""
        assert len(THE_TWINS) >= 2, THE_TWINS
        assert all(twin[0] < " " for _, twin in THE_TWINS), THE_TWINS

    def test_no_two_values_driven_here_share_a_line(self):
        seen: dict[str, str] = {}
        values = ["ok: forged", "C:\\Users\\me", "a\\: b", "\\n", "\n", "\t",
                  " ", "(a), b; c: d"]
        values += [_a_value_carrying(m) for m in THE_MARKS]
        values += [f"a {w} b" for w in THE_WORDS]
        values += [f"{m}x" for m, _ in THE_TWINS] + [f"{t}x" for _, t in THE_TWINS]
        for typed in values:
            line = _the_screen(typed)
            assert line not in seen or seen[line] == typed, (seen.get(line), typed, line)
            seen[line] = typed


def _a_value_where_that_mark_is_the_hosts_own(mark: str) -> str | None:
    """That same character where the desk could not have written it, from the
    shapes the code declares: a comma inside a label selector, the colon before
    a port. Those are the host's and must reach the screen as typed, or the
    operator is shown a string they cannot type back."""
    follows = client._DESK_MARK_SHAPES[mark]
    if "" in follows:
        return None  # that mark is the desk's in every shape, so it has no other side
    after = next((ch for ch in "abc019" if ch not in follows), None)
    return f"before{mark}{after}after" if after else None


THE_HOSTS_OWN = [(m, _a_value_where_that_mark_is_the_hosts_own(m))
                 for m in THE_MARKS
                 if _a_value_where_that_mark_is_the_hosts_own(m)]


class TestTheDriveStraddlesTheQuestionTheWriterAsks:
    """The writer asks one question of every mark character: could the desk have
    written it here? The rows above are all on one side of it. These are on the
    other, so a change that answered it the same way everywhere would be caught
    rather than pass."""

    def test_the_drive_found_both_sides(self):
        """Not every mark has another side -- two of them are the desk's in any
        shape -- but most do, and the drive has to reach them or the rows above
        only ever see one answer to the writer's question."""
        assert len(THE_HOSTS_OWN) >= 3, THE_HOSTS_OWN
        assert {m for m, _ in THE_HOSTS_OWN} < set(THE_MARKS) or len(THE_MARKS) == len(THE_HOSTS_OWN)

    @pytest.mark.parametrize("mark,typed", THE_HOSTS_OWN)
    def test_a_mark_the_desk_could_not_have_written_is_shown_as_typed(self, mark, typed):
        shown = _the_hosts_half(_the_screen(typed))
        assert shown == typed, (typed, shown)
        assert "\\" + mark not in shown, shown


#: A byte's name that is not all there: the letter that opens one, not followed by the
#: digits it takes. The notation reserves these letters -- no mark begins with them, and
#: a host's own x, u or U is printable and never quoted -- so on a screen every one of
#: them is the start of a whole name or a defect.
_A_NAME_CUT_SHORT = re.compile(r"\\x(?![0-9a-f]{2})|\\u(?![0-9a-f]{4})|\\U(?![0-9a-f]{8})")


def _every_name_is_whole(shown: str) -> bool:
    without_typed_backslashes = shown.replace("\\\\", "")
    if _A_NAME_CUT_SHORT.search(without_typed_backslashes):
        return False
    return not without_typed_backslashes.endswith("\\")


class TestACutValueStillReadsBackOnBothSidesOfTheCut:
    """A value too long for its share of the line is shortened to its start and its end
    with a mark between. Both halves are still the notation, so a cut that lands inside a
    byte's name -- between the backslash-x and its digits -- leaves a reader a name that
    is not one, and digits the host never typed. The desk kept a backslash with the
    character it quotes; it did not keep a name with its digits."""

    LONG = [
        ("bytes the screen cannot show", "\x01" * 2_500 + "A"),
        ("backslashes the host typed", "a\\b" * 1_500),
        ("marks of the desk's", "x: y; " * 800),
        ("a mix", ("\x01\\:\x7f" * 700) + "end"),
    ]

    @pytest.mark.parametrize("what,typed", LONG, ids=[row[0] for row in LONG])
    def test_the_drive_really_cuts(self, what, typed):
        shown = _the_hosts_half(_the_screen(typed))
        assert "..." in shown, f"{what}: the value came through uncut, so nothing below is measured"

    @pytest.mark.parametrize("what,typed", LONG, ids=[row[0] for row in LONG])
    def test_neither_side_of_the_cut_holds_a_name_cut_short(self, what, typed):
        shown = _the_hosts_half(_the_screen(typed))
        head, _, tail = shown.partition("...")
        assert _every_name_is_whole(head), f"{what}: before the cut: {head[-24:]!r}"
        assert _every_name_is_whole(tail), f"{what}: after the cut: {tail[:24]!r}"

    @pytest.mark.parametrize("what,typed", LONG, ids=[row[0] for row in LONG])
    def test_what_is_kept_is_what_the_host_typed(self, what, typed):
        """The start reads back as a prefix of the typed value and the end as a suffix:
        nothing the host did not type appears on either side."""
        shown = _the_hosts_half(_the_screen(typed))
        head, _, tail = shown.partition("...")
        assert typed.startswith(_unquote(head)), f"{what}: the start is not the host's: {head[-24:]!r}"
        assert typed.endswith(_unquote(tail)), f"{what}: the end is not the host's: {tail[:24]!r}"
