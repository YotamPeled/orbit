#!/usr/bin/env python3
"""Regression tests for the agent-runtime's streaming-message tolerance (protocol skew).

Stdlib `unittest` only (no pytest), matching the service's own dependency discipline —
run with `python3 test_agent_runtime.py`. Each test is written to go RED if the fix it
guards is reverted; the revert it guards is named in the test docstring.

Background — the live incident this suite locks down: the Claude Code CLI streams typed
messages to the Python SDK, which parses them. CLI >=2.1.49 emits a `rate_limit_event`
stream message, and the runtime's pinned SDK (claude-agent-sdk 0.1.1) RAISED
`MessageParseError("Unknown message type: rate_limit_event")` from inside the async
generator the runtime drains — so every dispatched task died mid-run without completing.

Two independent fixes, each proven here:
  * the SDK pin was bumped to a build that recognises `rate_limit_event` and skips any
    future unknown type instead of raising  -> `RealSdkParsesRateLimitEvent`
    (runs against the REAL installed SDK; skips when it is not importable);
  * the runtime's message loop tolerates an unknown-type parse failure — logs and lets
    the task complete — while still propagating genuine errors  -> the loop tests below,
    which need no SDK at all (they feed a fake stream).

app.py imports the SDK lazily (inside run_task), so importing it here needs no SDK.
"""
import asyncio
import os
import types
import unittest

# A minimal, self-contained environment: importing app runs its module-level env scrub and
# config reads, none of which need the SDK or a network.
os.environ.setdefault("ORBIT_AGENT_NAME", "test-agent")

import app  # noqa: E402


class FakeMessageParseError(Exception):
    """A stand-in for claude_agent_sdk._errors.MessageParseError.

    The runtime classifies this failure by DUCK TYPE — `type(exc).__name__` plus the
    SDK's stable error text — precisely so the loop does not depend on importing an
    internal SDK class whose path has moved between versions. So the class the test raises
    only has to share the name and carry `.data`, exactly like the real one.
    """
    __name__ = "MessageParseError"

    def __init__(self, message, data=None):
        self.data = data
        super().__init__(message)


# Ensure the raised instance reports the real class name the runtime matches on.
FakeMessageParseError.__qualname__ = "MessageParseError"
FakeMessageParseError.__name__ = "MessageParseError"


def _msg(text=None, usage=None, total_cost_usd=None):
    """A fake SDK message: an object exposing the attributes run_task reads via getattr."""
    m = types.SimpleNamespace()
    if text is not None:
        m.content = [types.SimpleNamespace(text=text)]
    if usage is not None:
        m.usage = usage
    if total_cost_usd is not None:
        m.total_cost_usd = total_cost_usd
    return m


async def _stream(items, raise_exc=None):
    """An async generator that yields `items`, then raises `raise_exc` (as the SDK's own
    generator does when parse_message raises mid-stream, which also closes it)."""
    for item in items:
        yield item
    if raise_exc is not None:
        raise raise_exc


class Recorder:
    """Captures app._log calls so a WARN assertion does not depend on stderr scraping."""

    def __init__(self):
        self.lines = []

    def __call__(self, level, msg, **kw):
        self.lines.append((level, msg, kw))

    def find(self, level, msg):
        return [kw for lv, m, kw in self.lines if lv == level and m == msg]


class LoopTolerance(unittest.TestCase):
    """The runtime's message loop (_consume_messages) — the belt-and-braces layer."""

    def setUp(self):
        self._real_log = app._log
        self.log = Recorder()
        app._log = self.log

    def tearDown(self):
        app._log = self._real_log

    def _drain(self, items, raise_exc=None):
        chunks, usage = [], {}
        asyncio.run(app._consume_messages(_stream(items, raise_exc), chunks, usage))
        return chunks, usage

    def test_unknown_stream_message_is_tolerated(self):
        """REVERT GUARD: drop the try/except in _consume_messages and this reddens.

        A rate_limit_event surfacing as an unknown-type MessageParseError mid-stream must
        NOT abort the task: the text gathered before it is kept, the run completes, and
        the skip is logged at WARN naming the type."""
        err = FakeMessageParseError("Unknown message type: rate_limit_event",
                                    data={"type": "rate_limit_event"})
        chunks, _ = self._drain([_msg(text="partial answer")], raise_exc=err)
        self.assertEqual("".join(chunks), "partial answer")
        warns = self.log.find("WARN", "skipping unrecognized SDK stream message")
        self.assertTrue(warns, "the tolerated skip must be logged, not silently swallowed")
        self.assertEqual(warns[0].get("message_type"), "rate_limit_event")

    def test_type_recovered_from_message_text_when_no_data(self):
        """Some SDK builds attach no `.data`; the type is then read from the error text."""
        err = FakeMessageParseError("Unknown message type: some_future_event")
        self._drain([], raise_exc=err)
        warns = self.log.find("WARN", "skipping unrecognized SDK stream message")
        self.assertEqual(warns[0].get("message_type"), "some_future_event")

    def test_missing_required_field_still_propagates(self):
        """REVERT GUARD: a KNOWN message missing a required field is a genuine
        malformation, not a skew — tolerance must NOT swallow it. If the guard were
        widened to catch every MessageParseError, this would stop raising."""
        err = FakeMessageParseError("Missing required field in result message: 'usage'",
                                    data={"type": "result"})
        with self.assertRaises(FakeMessageParseError):
            self._drain([_msg(text="x")], raise_exc=err)

    def test_missing_type_field_still_propagates(self):
        """A payload with no `type` at all is malformed, not an unknown type."""
        err = FakeMessageParseError("Message missing 'type' field", data={})
        with self.assertRaises(FakeMessageParseError):
            self._drain([], raise_exc=err)

    def test_genuine_exception_still_propagates(self):
        """A non-parse failure (a tool or transport error) must fail the task."""
        with self.assertRaises(RuntimeError):
            self._drain([_msg(text="x")], raise_exc=RuntimeError("tool blew up"))

    def test_usage_and_cost_are_collected(self):
        """The happy path still threads usage/spend onto the run for the OTel span."""
        _, usage = self._drain([
            _msg(text="hi", usage={"input_tokens": 3}),
            _msg(total_cost_usd=0.02),
        ])
        self.assertEqual(usage.get("input_tokens"), 3)
        self.assertEqual(usage.get("total_cost_usd"), 0.02)


class Classifier(unittest.TestCase):
    """The unknown-type discriminator in isolation — the line between skew and error."""

    def test_unknown_type_from_data(self):
        err = FakeMessageParseError("Unknown message type: rate_limit_event",
                                    data={"type": "rate_limit_event"})
        self.assertEqual(app._unknown_stream_message_type(err), "rate_limit_event")

    def test_missing_field_is_not_unknown_type(self):
        err = FakeMessageParseError("Missing required field in result message: 'x'")
        self.assertIsNone(app._unknown_stream_message_type(err))

    def test_non_parse_exception_is_not_unknown_type(self):
        self.assertIsNone(app._unknown_stream_message_type(ValueError("nope")))


def _real_sdk_parse_message():
    """Return the REAL installed SDK's parse_message, or None if the SDK is absent.

    Deliberately does not go through app's lazy import, so this stays a direct probe of
    the pinned dependency."""
    try:
        from claude_agent_sdk._internal.message_parser import parse_message
        return parse_message
    except Exception:
        return None


class RealSdkParsesRateLimitEvent(unittest.TestCase):
    """Proves the PIN BUMP against the actually-installed SDK.

    RED when reverted: with claude-agent-sdk 0.1.1 installed, parse_message RAISES
    MessageParseError on rate_limit_event and this test fails. GREEN on the pinned build
    (>=0.1.40), which recognises it. Skips (does not fail) when the SDK is not importable,
    so the suite still runs on a bare checkout — the in-image smoke stage is the CI gate
    for that case."""

    def setUp(self):
        self.parse = _real_sdk_parse_message()
        if self.parse is None:
            self.skipTest("claude-agent-sdk not importable in this environment")

    def test_rate_limit_event_does_not_raise(self):
        msg = self.parse({
            "type": "rate_limit_event",
            "uuid": "u-1",
            "session_id": "s-1",
            "rate_limit_info": {"status": "allowed"},
        })
        self.assertIsNotNone(msg)

    def test_future_unknown_type_is_skipped_not_raised(self):
        # The forward-compat contract the bump buys: an unmodelled type parses to None.
        self.assertIsNone(self.parse({"type": "some_future_event_2099", "x": 1}))


if __name__ == "__main__":
    unittest.main(verbosity=2)
