"""The record the process actually writes, not the record object it built.

WHY THIS FILE EXISTS. Three rounds of this node put caller text into an output without
that output's separators neutralised: a tool name forging authentication-challenge
parameters, then a tool name forging a second audit line, then — with a rendering step
added at both gates — a request header forging one, because the header is logged by
vendored transport code that has never heard of that rendering step. The third round's
contract could not see it: it read the record objects the handler built
(`caplog.records`), and the forgery is in the bytes the process writes. So the subject
here is the stream.

THE DESIGN THIS HOLDS. Rendering moves from the call sites to the sink. The process
installs one formatter for the whole root logger, and every record — orbitd's own, the
vendored transport's, a library nobody has read — is emitted as exactly one line of JSON.
A separator inside caller text is then impossible by construction rather than by every
author remembering, which is what failed three times. It also ends two smaller defects
the same way: a reader can tell a caller's own question mark from a neutralised control
byte, because the encoding is reversible; and a collector or a viewer that is not a
terminal is safe for the same reason a JSON body is.

WHAT IS STILL FORBIDDEN. A record that spans two lines. A caller who can choose a field
name. A single request that can make the record unbounded.

Run under pytest from services/orbitd.
"""

from __future__ import annotations

import io
import json
import logging

import pytest
from conftest import mint, rpc

from orbitd.roles import Role

pytestmark = pytest.mark.anyio

# One request header carrying a terminal escape that erases the line above it, then a
# complete, well-formed audit line claiming an administrator was allowed to issue
# credentials. This is the proven attack, verbatim in shape.
FORGERY = ("\x1b[1A\x1b[2K2026-09-11 13:00:00,000 orbitd.audit INFO "
           "actor=an-admin role=admin tool=orbit_auth_issue verdict=ok")
CONTROL_BYTES = ("\n", "\r", "\x00", "\x1b")


def configure(stream) -> None:
    """Install the process's own logging setup onto a stream we can read.

    Resolved at call time: if there is no single place that configures the process's
    logging, this contract FAILS rather than testing a stand-in, because 'there is one
    place' is the entire claim of this round.
    """
    from orbitd import audit

    fn = getattr(audit, "configure_logging", None)
    assert fn is not None, (
        "orbitd.audit has no configure_logging; the rendering is still at the call sites, "
        "which is what let a vendored library's own log line carry the forgery")
    fn(stream)


@pytest.fixture
def log_stream():
    """The bytes the process would write, with the real configuration in front of them."""
    root = logging.getLogger()
    saved = (list(root.handlers), root.level)
    stream = io.StringIO()
    configure(stream)
    yield stream
    root.handlers[:] = saved[0]
    root.setLevel(saved[1])


def lines(stream) -> list[str]:
    return [line for line in stream.getvalue().splitlines() if line.strip()]


def records(stream) -> list[dict]:
    out = []
    for line in lines(stream):
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError as exc:  # a line that is not a record is the defect
            raise AssertionError(f"the stream carries a line that is not one record: {line[:200]!r}") from exc
    return out


def every_value(obj) -> list[str]:
    if isinstance(obj, str):
        return [obj]
    if isinstance(obj, dict):
        return [v for value in obj.values() for v in every_value(value)]
    if isinstance(obj, list):
        return [v for item in obj for v in every_value(item)]
    return []


class TestAnyLoggerAtAll:
    """The sink covers code that has never heard of this module. That is the whole point."""

    def test_a_library_that_interpolates_caller_text_still_writes_one_line(self, log_stream):
        logging.getLogger("mcp.server.transport_security").warning(
            "Invalid Origin header: %s" % ("http://127.0.0.1:8732" + FORGERY))
        assert len(lines(log_stream)) == 1, (
            "a vendored library's own log line split into two; a caller chooses that text")

    def test_the_forgery_is_not_in_the_bytes_that_are_written(self, log_stream):
        logging.getLogger("some.library.nobody.has.read").warning(
            "rejected %s" % ("host" + FORGERY))
        written = log_stream.getvalue()
        for char in CONTROL_BYTES[1:]:
            assert char not in written, f"a raw {char!r} reached the stream"
        assert len(lines(log_stream)) == 1

    def test_percent_arguments_are_covered_too(self, log_stream):
        logging.getLogger("some.library").warning("rejected %s", "host" + FORGERY)
        assert len(lines(log_stream)) == 1

    def test_a_traceback_does_not_become_several_records(self, log_stream):
        try:
            raise ValueError("a host said " + FORGERY)
        except ValueError:
            logging.getLogger("orbitd.applier").exception("apply failed")
        assert len(lines(log_stream)) == 1, (
            "an exception spread over several lines, each of which can be forged")

    def test_what_the_caller_sent_is_recoverable(self, log_stream):
        """A reader must be able to tell a caller's own question mark from a byte that
        was neutralised. An encoding that is reversible does that; a substitution that
        maps many characters onto one does not."""
        logging.getLogger("some.library").warning("rejected %s", "a?b" + FORGERY)
        found = [v for v in every_value(records(log_stream)[0]) if "a?b" in v]
        assert found, "the caller's text is not in the record at all"
        assert FORGERY in found[0], (
            "the record does not carry what the caller actually sent, so a reader cannot "
            "tell a neutralised byte from a question mark the caller typed")

    def test_the_record_is_bounded(self, log_stream):
        logging.getLogger("some.library").warning("rejected %s", "x" * 4_000_000)
        assert len(log_stream.getvalue()) < 16_384, (
            "one log call wrote a record large enough to drown the trace around it")

    def test_a_caller_cannot_choose_a_field_name(self, log_stream):
        logging.getLogger("some.library").warning(
            "rejected %s", '", "verdict": "ok", "actor": "an-admin')
        one = records(log_stream)[0]
        assert one.get("verdict") != "ok"
        assert one.get("actor") != "an-admin"

    def test_an_ordinary_line_is_still_worth_reading(self, log_stream):
        """Anti-vacuity: a formatter that dropped everything would pass everything above."""
        logging.getLogger("orbitd.audit").info(
            "actor=%s role=%s tool=%s verdict=%s", "someone", "viewer", "orbit_deploy_status", "ok")
        text = " ".join(every_value(records(log_stream)[0]))
        for word in ("someone", "viewer", "orbit_deploy_status", "ok"):
            assert word in text, f"the record no longer says {word}"
        assert "orbitd.audit" in " ".join(every_value(records(log_stream)[0]))


class TestThroughTheRealApp:
    async def test_a_request_header_cannot_forge_a_record(self, state, http, log_stream):
        """The proven attack: a viewer token and one legal header value."""
        client = http(mint(state, Role.viewer), Origin="http://127.0.0.1:8732" + FORGERY)
        async with client:
            await rpc(client, "tools/list")
        for one in records(log_stream):
            assert one.get("tool") != "orbit_auth_issue"
            assert "an-admin" not in str(one.get("actor", ""))
        assert "\x1b" not in log_stream.getvalue()

    async def test_a_host_header_cannot_either(self, state, http, log_stream):
        client = http(mint(state, Role.viewer), Host="mcp.fixture.invalid" + FORGERY)
        async with client:
            await rpc(client, "tools/list")
        assert "\x1b" not in log_stream.getvalue()
        for line in lines(log_stream):
            assert "verdict=ok" not in line or "orbit_auth_issue" not in line

    async def test_the_refusal_a_caller_is_handed_is_bounded(self, state, http):
        """The record was capped and the response body was not: a two-megabyte tool name
        came back in a four-megabyte refusal."""
        client = http(mint(state, Role.viewer))
        async with client:
            response = await rpc(client, "tools/call",
                                 {"name": "orbit_" + "x" * 2_000_000, "arguments": {}})
        assert len(response.content) < 65_536, (
            f"a refusal returned {len(response.content)} bytes for one tool name")

    async def test_the_same_refusal_is_bounded_at_the_other_gate(self, state, http):
        """There are two gates and this case was measuring one of them twice. The helper
        every test reaches for sets the routing header, which is what picks the gate in
        front; a caller who simply leaves the header off reaches the gate behind it, and
        that is the one that writes the record. A review removed the bound from the gate
        behind and the whole suite stayed green while a two-megabyte name came back in a
        four-megabyte refusal."""
        body = {"jsonrpc": "2.0", "id": 21, "method": "tools/call",
                "params": {"name": "orbit_" + "x" * 2_000_000, "arguments": {}}}
        async with http(mint(state, Role.viewer)) as h:
            response = await h.post(
                "/mcp", json=body,
                headers={"Accept": "application/json, text/event-stream",
                         "MCP-Protocol-Version": "2025-06-18"})
        assert len(response.content) < 65_536, (
            f"the gate a caller reaches by leaving the routing header off returned "
            f"{len(response.content)} bytes for one tool name")

    @pytest.mark.parametrize("method,params,what", [
        ("tools/call", {"name": "orbit_" + "x" * 2_000_000, "arguments": {}}, "a tool name"),
        ("resources/read", {"uri": "orbit://" + "x" * 2_000_000}, "a resource address"),
    ], ids=["the tool route", "the resource route"])
    async def test_every_route_that_echoes_a_caller_bounds_what_it_echoes(
            self, state, http, method, params, what):
        """A review found the bound held on the tool route at both gates and on neither
        resource route -- the same finding one step to the side, which is the shape this
        front keeps hitting. The route is a parameter so an arm added later is a row here
        rather than a case nobody wrote."""
        body = {"jsonrpc": "2.0", "id": 23, "method": method, "params": params}
        async with http(mint(state, Role.viewer)) as h:
            response = await h.post(
                "/mcp", json=body,
                headers={"Accept": "application/json, text/event-stream",
                         "MCP-Protocol-Version": "2025-06-18"})
        assert len(response.content) < 65_536, (
            f"the refusal for {what} of two megabytes came back in "
            f"{len(response.content)} bytes")
        assert "UNKNOWN_TOOL" in response.text, (
            f"the surface did not refuse {what} at all, so the bound beside it is "
            "measuring nothing: " + repr(response.text[:300]))

    async def test_that_gate_is_the_one_being_reached(self, state, http):
        """Anti-vacuity, and the thing the case above cannot tell on its own: a request
        that never arrived would also come back small. The refusal has to be the refusal,
        and it has to name the tool."""
        body = {"jsonrpc": "2.0", "id": 22, "method": "tools/call",
                "params": {"name": "orbit_not_a_tool_here", "arguments": {}}}
        async with http(mint(state, Role.viewer)) as h:
            response = await h.post(
                "/mcp", json=body,
                headers={"Accept": "application/json, text/event-stream",
                         "MCP-Protocol-Version": "2025-06-18"})
        assert "UNKNOWN_TOOL" in response.text, (
            "the gate behind the routing header did not answer this at all, so the bound "
            "beside it is measuring nothing. The tool name alone is not enough to tell: "
            "the library's own fallback reply names it too, and a review found this case "
            "green with that gate's refusal removed entirely: " + repr(response.text[:300]))
