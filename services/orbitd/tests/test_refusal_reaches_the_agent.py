"""A refusal has to reach the agent as a refusal.

The operator skill tells an agent that a failed call arrives as a result with
``isError`` set, carrying ``code``, ``what_failed``, ``evidence``, ``next_action`` and
``retryable``, and that the way to recover is to follow ``next_action``. That is exactly
what this surface builds -- at the handler tier, in the protocol, with a 200 -- and no
agent ever receives it.

There are two gates. The one behind answers in the protocol. The one in front answers with
an HTTP 403 before the protocol is reached, and which gate a caller meets is decided by a
routing header that every client library sets. So the gate every agent actually reaches is
the one that cannot say anything useful: the session is torn down with a transport error
carrying no code, no next action, and nothing left to retry on. An agent that mistypes a
tool name loses its session instead of correcting itself, and the phase the release ends
with is a session holding only the skills.

The refusals are not authentication failures. A caller whose token is fine but whose role
is too low, and a caller who named a tool that does not exist, both have a working session;
there is nothing about their credential to fix. Authentication is different and stays where
it is: a caller with no token or a bad one is refused before the protocol, because there is
no session to answer in.
"""

from __future__ import annotations

import json

import pytest
from conftest import ISSUER, Role, mint, sdk_client

pytestmark = pytest.mark.anyio

DOCUMENTED_FIELDS = ("code", "what_failed", "evidence", "next_action", "retryable")


def _envelope(result):
    """The failure envelope out of a tool result, however the SDK hands it over."""
    structured = getattr(result, "structuredContent", None) or getattr(
        result, "structured_content", None)
    if isinstance(structured, dict) and "code" in structured:
        return structured
    for item in result.content or []:
        text = getattr(item, "text", None)
        if not text:
            continue
        try:
            document = json.loads(text)
        except ValueError:
            continue
        if isinstance(document, dict) and "code" in document:
            return document
    raise AssertionError("no failure envelope in the result: " + repr(result)[:400])


class TestARefusalArrivesAsAResult:
    """Driven the way an agent drives it: through the client library, which is what sets
    the header that picks the gate. A case that posts the body by hand reaches the other
    gate and cannot see any of this."""

    async def test_a_name_the_surface_does_not_offer(self, state, http):
        async with sdk_client(http, mint(state, Role.operator)) as client:
            result = await client.call_tool("orbit_not_a_tool_here", {})
        assert result.is_error, "a name that does not exist was not refused"
        envelope = _envelope(result)
        assert envelope["code"] == "UNKNOWN_TOOL", envelope
        assert "tools/list" in envelope["next_action"], envelope

    async def test_a_call_above_the_callers_role(self, state, http):
        async with sdk_client(http, mint(state, Role.viewer)) as client:
            result = await client.call_tool("orbit_auth_revoke",
                                            {"jti": "Q1w2E3r4T5y6U7i8"})
        assert result.is_error, "a call above the caller's role was not refused"
        envelope = _envelope(result)
        assert envelope["code"] == "FORBIDDEN", envelope
        assert envelope["evidence"]["required_role"] == "admin", envelope
        assert envelope["evidence"]["held_role"] == "viewer", envelope

    @pytest.mark.parametrize("name,arguments,role", [
        ("orbit_not_a_tool_here", {}, Role.operator),
        ("orbit_auth_revoke", {"jti": "Q1w2E3r4T5y6U7i8"}, Role.viewer),
    ])
    async def test_the_refusal_carries_what_the_skill_promises(self, state, http, name,
                                                               arguments, role):
        async with sdk_client(http, mint(state, role)) as client:
            result = await client.call_tool(name, arguments)
        envelope = _envelope(result)
        missing = [f for f in DOCUMENTED_FIELDS if f not in envelope]
        assert not missing, (
            f"the refusal is missing {missing}, which the operator skill tells an agent to "
            f"read: {envelope}")
        assert envelope["retryable"] is False, (
            "a refusal that will not change on a retry says it is retryable, so an agent "
            "told to retry once will burn its retry: " + json.dumps(envelope))


class TestTheSessionSurvivesARefusal:
    """The operator-visible point. Being told what went wrong is only useful while there is
    still a session to act on it in. Following ``next_action`` means making another call."""

    async def test_the_agent_can_still_ask_what_is_offered(self, state, http):
        async with sdk_client(http, mint(state, Role.operator)) as client:
            await client.call_tool("orbit_not_a_tool_here", {})
            listed = await client.list_tools()
        assert listed.tools, (
            "the refusal ended the session, so the next action it names cannot be taken")

    async def test_the_agent_can_still_make_a_call_it_is_allowed(self, state, http):
        async with sdk_client(http, mint(state, Role.viewer)) as client:
            await client.call_tool("orbit_auth_revoke", {"jti": "Q1w2E3r4T5y6U7i8"})
            result = await client.call_tool("orbit_project_list", {})
        assert not result.is_error, (
            "one refusal cost the caller every call after it: " + repr(result)[:300])


class TestAuthenticationStaysWhereItIs:
    """Anti-vacuity, and the line the repair must not cross. Moving every refusal into the
    protocol would pass everything above and hand an unauthenticated caller a session. A
    caller with no usable token has nothing to be told in the protocol, because there is no
    session to tell it in."""

    async def test_a_bad_token_is_refused_before_the_protocol(self, http):
        async with http("not-a-token") as h:
            response = await h.post(
                "/mcp",
                json={"jsonrpc": "2.0", "id": 41, "method": "tools/list"},
                headers={"Accept": "application/json, text/event-stream",
                         "MCP-Protocol-Version": "2025-06-18"})
        assert response.status_code == 401, (
            "a caller with an unusable token was let into the protocol: "
            f"{response.status_code} {response.text[:200]}")

    async def test_no_token_at_all_is_refused_before_the_protocol(self, http):
        async with http(None) as h:
            response = await h.post(
                "/mcp",
                json={"jsonrpc": "2.0", "id": 42, "method": "tools/list"},
                headers={"Accept": "application/json, text/event-stream",
                         "MCP-Protocol-Version": "2025-06-18"})
        assert response.status_code == 401, (
            "a caller with no token was let into the protocol: "
            f"{response.status_code} {response.text[:200]}")
