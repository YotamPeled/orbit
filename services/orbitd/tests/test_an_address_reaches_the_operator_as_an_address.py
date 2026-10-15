"""What an operator can copy off the screen.

WHY THIS FILE EXISTS. The desk neutralises its own marks in anything a host sent, and a colon
is one of them. The rule has one exception, for the colon in `://`, which was written so that
addresses stay readable. It does not cover the colon before a port, so an address reaches the
operator like this:

    orbit_project_list: failed what_failed=http://127.0.0.1\\:43289/mcp

Two characters that are not in the address, in the middle of the one thing on the line an
operator is meant to act on. It was found while repairing the sign-in's sentences: the sentence
that most needs to name an address -- the one telling an operator to check it -- could not
print one, so it now tells them to check the address without showing it.

WHAT THIS FILE ASKS. That an address a host sent comes back as the address. Nothing about how
the escaping works: the row takes an address, sends it as an ordinary host value through a real
desk line, and asks whether what the operator reads is the string they would have to type.

The addresses driven are the shapes this desk really meets: the ones it is configured with, the
ones a host reports about itself. Each is written here as an operator would type it.

Run under pytest from services/orbitd.
"""

from __future__ import annotations

import re

import pytest

from orbitd import client

ADDRESSES = (
    ("a host and a port", "http://127.0.0.1:43289/mcp"),
    ("a name and a port", "https://orbit.example.com:8443/mcp"),
    ("a name with no port", "https://orbit.example.com/mcp"),
    ("a bare host and port, as a person types it", "127.0.0.1:43289"),
)


def _what_the_operator_reads(value: str) -> str:
    """One real desk line carrying the value as a host's own text."""
    return client._summary("orbit_project_list", {"status": "failed", "what_failed": value})


class TestTheMeasurementIsWorthComparingAgainst:
    def test_an_ordinary_value_survives_whole(self):
        line = _what_the_operator_reads("a plain step")
        assert "a plain step" in line, f"the drive does not carry a value at all: {line!r}"


class TestAnAddressComesBackAsTheAddress:
    @pytest.mark.parametrize("how,address", ADDRESSES, ids=[row[0] for row in ADDRESSES])
    def test_the_operator_can_copy_it(self, how, address):
        line = _what_the_operator_reads(address)
        assert address in line, (
            f"{how}: the operator reads {line.split('what_failed=')[-1]!r} where the address "
            f"is {address!r}. What is on the screen is not what they would type, and this is "
            "the line that exists to tell them which address to check")


UNNEUTRALISED = re.compile(r"(?<!\\): ")


class TestTheDesksOwnSeparatorIsStillTheDesks:
    """The row that stops the rows above being answered by giving the colon away. The desk's
    separator is a colon followed by a space -- that is what divides its sentence into parts --
    and a host that types one must still leave it neutralised. An address's colon is followed
    by a digit or a slash, never a space, so both rules can hold at once."""

    def test_a_host_cannot_write_the_desks_separator(self):
        forged = "ok: the host wrote this part"
        line = client._summary("orbit_project_list", {"status": "failed",
                                                      "what_failed": forged})
        after = line.split("what_failed=", 1)[-1]
        assert not UNNEUTRALISED.search(after), (
            f"a host wrote the desk's own separator into the line: {after!r}. The operator "
            "cannot tell which parts of the sentence the desk said")

    def test_the_desk_still_uses_it_itself(self):
        """Anti-vacuity: if the desk stopped writing the separator, the row above would pass
        on a line with nothing to forge."""
        line = client._summary("orbit_project_list", {"status": "ok"})
        assert UNNEUTRALISED.search(line), (
            f"the desk's own sentence no longer uses its separator: {line!r}")

    def test_a_host_cannot_end_a_part_with_the_desks_separator(self):
        """The same forgery split across the join: the writer puts a space between parts, so
        a host value that ends in a colon writes the separator with the desk's own space."""
        line = client._summary("orbit_project_list", {"status": "failed:", "version": "1"})
        after = line.split("orbit_project_list: ", 1)[-1]
        assert not UNNEUTRALISED.search(after), (
            f"a host ended its part with the desk's separator: {after!r}")
