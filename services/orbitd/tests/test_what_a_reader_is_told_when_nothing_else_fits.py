"""The fourth sentence the desk writes, and the only one built from nothing the host sent.

WHY THIS FILE EXISTS. Three of the desk's sentences are built out of text a surface supplied,
and all three have been held against a hostile surface. This is the fourth, and it is the one
that runs when the desk could not make sense of what came back at all -- when a surface
answers in a shape the tool does not promise, or a transport fails in a way nothing above
anticipated.

Today that sentence is the name of a Python class:

    orbit: unexpected failure: ExceptionGroup

The operator is told an internal type name and nothing else. Not which tool, not that the
answer came from the surface rather than from their own command, not what to do next. It is
reachable without any hostility: the desk checks a successful answer field by field against
the shape the tool declares, and an answer that does not fit ends here.

WHAT THIS FILE HOLDS, and how it avoids pinning a wording. Two halves, both derived rather
than written down.

The negative half is a registry. Every exception class name reachable in this process -- the
built-in hierarchy, plus the exception classes of the libraries the desk imports -- is a name
the operator must never be shown. That set is enumerable, the search is literal, and a repair
may say anything it likes as long as it says none of those. What this cannot hold is a
sentence that invents its own jargon without using a class name; that is written down as a
gap below.

The positive half is what the desk KNOWS at that moment and the host does not: which tool was
called. A sentence that cannot even name the tool has told the operator nothing they did not
already know.

Run under pytest from services/orbitd.
"""

from __future__ import annotations

import builtins
import json
import re

import pytest
from conftest import MCP_URL
from orbitd.roles import Role
from test_login_address import ISSUER, sign_in

pytestmark = pytest.mark.anyio

# What this file cannot hold, so a reader does not have to rediscover it:
#
#   - a sentence that invents its own jargon without using any class name. "orbit: internal
#     inconsistency in the response envelope" says as little as the class name did and this
#     file would pass it. There is no registry to subtract that from; it is the same shape
#     the document rule gives up and for the same reason.
#   - whether the next action it offers is the RIGHT one. That a next action exists is
#     holdable; whether it helps is a question about meaning.


def _every_exception_name() -> frozenset:
    """Every exception class name this process can reach: the built-in hierarchy, and the
    exception classes of the libraries the desk imports. Derived, not listed, so a library
    that grows a new one is covered the day it appears."""
    names = {name for name, thing in vars(builtins).items()
             if isinstance(thing, type) and issubclass(thing, BaseException)}
    import httpx2
    import mcp_types
    import pydantic
    for module in (httpx2, mcp_types, pydantic):
        for name, thing in vars(module).items():
            if isinstance(thing, type) and issubclass(thing, BaseException):
                names.add(name)
    # Single-word names too common in ordinary prose to search for. Each is a hole, and each
    # is here because a sentence may legitimately contain it: "the surface's answer did not
    # match" must not be an offence merely because a class is called `Match`.
    return frozenset(n for n in names if n not in {"Warning", "Error", "Exception"})


@pytest.fixture
def xdg(tmp_path, monkeypatch):
    d = tmp_path / "xdg"
    monkeypatch.setenv("XDG_CONFIG_HOME", str(d))
    return d


class AnswerOffTheDeclaredShape:
    """The real server, with the payload of a successful answer replaced by one that does not
    fit what the tool promises. Nothing hostile is required -- a surface with a bug, or one a
    version ahead, produces exactly this."""

    def __init__(self, app):
        self.app = app

    def factory(self, headers=None):
        import httpx2

        async def handle(request: httpx2.Request) -> httpx2.Response:
            response = await httpx2.ASGITransport(app=self.app).handle_async_request(request)
            await response.aread()
            text = response.content.decode("utf-8", "replace")
            head, _, tail = text.partition("{")
            try:
                doc = json.loads("{" + tail)
            except json.JSONDecodeError:
                return httpx2.Response(response.status_code, content=response.content,
                                       headers=dict(response.headers))
            result = doc.get("result")
            if isinstance(result, dict) and "content" in result and not result.get("isError"):
                off = {"this_field": "is not in the shape the tool promises"}
                result["structuredContent"] = off
                result["content"] = [{"type": "text", "text": json.dumps(off)}]
                out = {k: v for k, v in response.headers.items()
                       if k.lower() != "content-length"}
                return httpx2.Response(response.status_code,
                                       content=(head + json.dumps(doc)).encode(), headers=out)
            return httpx2.Response(response.status_code, content=response.content,
                                   headers=dict(response.headers))

        return httpx2.AsyncClient(transport=httpx2.MockTransport(handle),
                                  base_url=ISSUER, headers=headers or {})


TOOL = "orbit_project_list"


async def _what_the_operator_reads(app, state, xdg, capsys, name="n-reader"):
    from orbitd.client import async_main

    secret = state.store.add_principal(name, Role.viewer, 3600)
    surface = AnswerOffTheDeclaredShape(app)
    await sign_in(surface.factory, MCP_URL, name, secret)
    capsys.readouterr()
    code = await async_main(["call", TOOL], http_client_factory=surface.factory)
    out, err = capsys.readouterr()
    return (out + err).strip(), code


class TestTheMeasurementIsWorthComparingAgainst:
    async def test_this_route_is_reached_at_all(self, app, state, xdg, capsys):
        """Anti-vacuity: an answer off the declared shape really ends here, and really fails.
        If the desk accepted it, everything below would measure nothing."""
        told, code = await _what_the_operator_reads(app, state, xdg, capsys, name="o-reader")
        assert code != 0, f"the call succeeded, so this is not the route: {told!r}"
        assert told, "the operator was told nothing at all"

    def test_the_registry_is_not_empty(self):
        """And the registry really holds names, or the negative half below is vacuous."""
        names = _every_exception_name()
        assert len(names) > 50, f"only {len(names)} exception names found; the walk is broken"
        assert "ExceptionGroup" in names, "the name this file was written for is not in the set"


class TestTheOperatorIsNotShownAnInternalTypeName:
    async def test_no_exception_class_name_reaches_the_operator(self, app, state, xdg,
                                                                capsys):
        told, _ = await _what_the_operator_reads(app, state, xdg, capsys, name="p-reader")
        shown = sorted(name for name in _every_exception_name()
                       if re.search(rf"\b{re.escape(name)}\b", told))
        assert not shown, (
            f"the operator is shown the name of an internal type, which tells them nothing "
            f"they can act on and nothing about their own call: {shown} in {told!r}")

    async def test_the_sentence_names_the_tool_that_was_called(self, app, state, xdg, capsys):
        """The positive half: the one thing the desk knows here that the operator's own
        command does not already tell them is which call this was."""
        told, _ = await _what_the_operator_reads(app, state, xdg, capsys, name="q-reader")
        assert TOOL in told, (
            f"the sentence does not say which call failed, so an operator running several "
            f"cannot tell which one this was: {told!r}")


# --------------------------------------------- the same question, asked of the sign-in

# WHY THIS SECTION EXISTS. The rows above drive one route -- a call whose answer does not fit
# the declared shape -- and the repair they held was written for that route. A review then went
# to the real entry point, with no pytest in the process, turned the host off, and read:
#
#     orbit: transport: ConnectError
#
# A class name and nothing to do next, on the failure an operator meets more often than any
# other. A second one, when the token endpoint answers with something unusable, reads
# `orbit: bad token response: KeyError`.
#
# So the drives below are not code paths. They are the ordinary ways the world breaks before
# the desk has an answer at all: the host is not there, and the host answers with something
# that is not a token. A route the desk grows later is not covered by this list -- what is
# covered is that these two, which every operator meets, are held to what the call route is
# held to.


class TheHostIsNotThere:
    """No server. Not hostile, not misbehaving: switched off, or the wrong address."""

    def factory(self, headers=None):
        import httpx2

        async def handle(request: httpx2.Request):
            raise httpx2.ConnectError("connection refused", request=request)

        return httpx2.AsyncClient(transport=httpx2.MockTransport(handle),
                                  base_url=ISSUER, headers=headers or {})


class TheAnswerIsNotAToken:
    """The real server, with the token endpoint's answer replaced by a document that carries
    no token. A surface a version ahead, or one behind a proxy that rewrote the body."""

    def __init__(self, app):
        self.app = app

    def factory(self, headers=None):
        import httpx2

        async def handle(request: httpx2.Request) -> httpx2.Response:
            response = await httpx2.ASGITransport(app=self.app).handle_async_request(request)
            await response.aread()
            if "token" in str(request.url) and response.status_code == 200:
                return httpx2.Response(200, json={"not_a_token": "at all"})
            return httpx2.Response(response.status_code, content=response.content,
                                   headers={k: v for k, v in response.headers.items()
                                            if k.lower() != "content-length"})

        return httpx2.AsyncClient(transport=httpx2.MockTransport(handle),
                                  base_url=ISSUER, headers=headers or {})


BEFORE_THERE_IS_AN_ANSWER = (
    ("the host is not there", lambda app: TheHostIsNotThere()),
    ("the answer is not a token", lambda app: TheAnswerIsNotAToken(app)),
)


async def _what_the_operator_reads_signing_in(app, state, xdg, capsys, make, name):
    secret = state.store.add_principal(name, Role.viewer, 3600)
    capsys.readouterr()
    code = await sign_in(make(app).factory, MCP_URL, name, secret)
    out, err = capsys.readouterr()
    return (out + err).strip(), code


class TestTheSignInIsHeldToWhatTheCallIsHeldTo:
    @pytest.mark.parametrize("how,make", BEFORE_THERE_IS_AN_ANSWER,
                             ids=[row[0] for row in BEFORE_THERE_IS_AN_ANSWER])
    async def test_the_route_is_reached_and_fails(self, app, state, xdg, capsys, how, make):
        told, code = await _what_the_operator_reads_signing_in(
            app, state, xdg, capsys, make, f"s-{how[4:8]}")
        assert code != 0, f"{how}: the sign-in succeeded, so this is not the route: {told!r}"
        assert told, f"{how}: the operator was told nothing at all"

    @pytest.mark.parametrize("how,make", BEFORE_THERE_IS_AN_ANSWER,
                             ids=[row[0] for row in BEFORE_THERE_IS_AN_ANSWER])
    async def test_no_exception_class_name_reaches_the_operator(self, app, state, xdg, capsys,
                                                                how, make):
        told, _ = await _what_the_operator_reads_signing_in(
            app, state, xdg, capsys, make, f"t-{how[4:8]}")
        shown = sorted(name for name in _every_exception_name()
                       if re.search(rf"\b{re.escape(name)}\b", told))
        assert not shown, (
            f"when {how}, the operator is shown the name of an internal type and nothing they "
            f"can act on: {shown} in {told!r}")

    @pytest.mark.parametrize("how,make", BEFORE_THERE_IS_AN_ANSWER,
                             ids=[row[0] for row in BEFORE_THERE_IS_AN_ANSWER])
    async def test_the_operator_is_told_what_to_do_next(self, app, state, xdg, capsys, how,
                                                        make):
        """The same half the call route already keeps. What counts as a next action is not
        judged here -- only that the sentence offers one, which is the difference between a
        report and a dead end."""
        told, _ = await _what_the_operator_reads_signing_in(
            app, state, xdg, capsys, make, f"u-{how[4:8]}")
        assert "next" in told.lower(), (
            f"when {how}, the operator is told what happened and not what to do about it: "
            f"{told!r}")
