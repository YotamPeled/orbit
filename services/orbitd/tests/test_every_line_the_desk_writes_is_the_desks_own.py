"""Every line the desk puts in front of an operator, held to one property.

WHY THIS FILE EXISTS. Two rounds repaired the sentences the desk builds out of a host's text.
Round one gave the desk's own separators an escape, so a host could not write a part of the
frame. Round two gave every framed value a bound, so a long value could not push the desk's
own last part -- the thing to do next -- off the line. Both repairs were applied one call site
at a time, and a review then found four more sentences that had never been given either: the
one the sign-in writes when the token endpoint refuses, the notice the desk writes when it
refuses a host's resource address, the listing that prints a host-chosen tool name, and the
refusal sentence's own tool name, which its sibling bounds and it does not.

The same review found the bound itself measured in the wrong units. A value is bounded at
four hundred characters as the FRAME writes them, and then rendered for the screen, where one
character the host chose can take four. Three values of four hundred become four thousand
eight hundred characters against a line the guard cuts at two thousand -- so a host that
answers in control characters still takes the desk's last part off all three repaired
sentences. Every case holding that bound used letters, one screen character each, so none of
them could see it.

The front's rule is that a defect class found twice means the design is wrong. This is the
third round, and the class is the same both times: a rule applied at a list of call sites, and
a measurement taken in the wrong units. So this file holds neither a list of sites nor a
number of characters.

WHAT THIS FILE HOLDS. One property, asked once per ROUTE -- a way a host or an operator
decides part of a line the desk writes:

    whatever reaches the operator, the desk's own last part is still on the screen, and the
    desk's own parts appear exactly as often as when the host said something ordinary.

Both halves are derived, not written down. Each route is driven twice: once with a host whose
values are an ordinary marker, and once with the same values made hostile. The desk's own
parts are then whatever the ordinary line contains that the marker does not -- so no wording
is pinned, and a repair may rewrite every sentence it likes.

Hostility comes in three kinds, and the third is the one nothing could see: a value carrying
the desk's own separators, a long value of ordinary letters, and a long value of characters
the screen widens.

ONE VOCABULARY, NOT ONE PER SENTENCE. The marks below are gathered in one place and every
route is held against all of them, not against the ones its own sentence happens to use. A
reader looking at a line cannot tell which of the desk's sentences it is, so a host that
writes the summary's labels into a refusal is writing in the desk's voice just the same. That
is what "one writer" means here, and it is the thing that makes a list of call sites
unnecessary: there is one vocabulary, it is escaped wherever host text goes, and no site
chooses.

Run under pytest from services/orbitd.
"""

from __future__ import annotations

import ast
import inspect
import pathlib
import re
import json

import pytest
from conftest import MCP_URL
from orbitd import client
from orbitd.roles import Role
from test_login_address import ISSUER, Recorder, sign_in

pytestmark = pytest.mark.anyio

#: What an ordinary host puts in every field it controls, so the desk's own words are
#: whatever is left over.
MARKER = "HOSTSAIDTHIS"

#: Every separator any of the desk's sentences uses, gathered so a forgery can carry all of
#: them at once. A repair that changes the marks changes this line with it; what it may not
#: do is let a host write one.
FORGERY = (MARKER + " x): forged; next: run something else "
           "(verified by the surface, not a claim) code=F what_failed=G, token holds admin")

#: The three kinds of hostility, and what each is for.
HOSTILITIES = (
    ("a value carrying the desk's own separators", FORGERY),
    ("a long value of ordinary letters", MARKER + "P" * 2500),
    ("a long value of characters the screen widens", MARKER + "\x01" * 2500),
)


@pytest.fixture
def xdg(tmp_path, monkeypatch):
    d = tmp_path / "xdg"
    monkeypatch.setenv("XDG_CONFIG_HOME", str(d))
    return d


# --------------------------------------------------------------------- the hosts

class TokenEndpointRefuses:
    """A host whose token endpoint refuses with text of its own."""

    def __init__(self, app, value):
        self.app, self.value = app, value

    def factory(self, headers=None):
        import httpx2

        async def handle(request):
            if request.url.path.endswith("/token"):
                return httpx2.Response(400, json={"error": "invalid_target",
                                                  "error_description": self.value})
            return await httpx2.ASGITransport(app=self.app).handle_async_request(request)

        return httpx2.AsyncClient(transport=httpx2.MockTransport(handle),
                                  base_url=ISSUER, headers=headers or {})


class TokenEndpointClaimsARole:
    """A host that signs its own token and puts what it likes in the role claim. The desk
    reads that claim without verifying the signature -- it is the host's own token -- so the
    role on the screen is the host's text."""

    def __init__(self, app, value):
        self.app, self.value = app, value

    def factory(self, headers=None):
        import httpx2
        import jwt

        # A fixed expiry, not "an hour from now": the desk prints the token's expiry on the
        # line these tests compare between two sign-ins, and two sign-ins either side of a
        # second's tick printed two different lines (one failure in a whole-suite run on a
        # loaded machine, 2026-10-08; green on its own). 2100-01-01T00:00:00Z.
        token = jwt.encode({"role": self.value, "exp": 4102444800},
                           "a-secret-of-the-hosts", algorithm="HS256")

        async def handle(request):
            if request.url.path.endswith("/token"):
                return httpx2.Response(200, json={"access_token": token,
                                                  "token_type": "Bearer",
                                                  "expires_in": 3600})
            return await httpx2.ASGITransport(app=self.app).handle_async_request(request)

        return httpx2.AsyncClient(transport=httpx2.MockTransport(handle),
                                  base_url=ISSUER, headers=headers or {})


class ListsAToolOfItsOwn:
    """A host that answers the listing with a tool name of its own choosing."""

    def __init__(self, app, value):
        self.app, self.value = app, value

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
            if isinstance(result, dict) and isinstance(result.get("tools"), list) \
                    and result["tools"]:
                result["tools"] = [dict(result["tools"][0], name=self.value)]
                out = {k: v for k, v in response.headers.items()
                       if k.lower() != "content-length"}
                return httpx2.Response(response.status_code,
                                       content=(head + json.dumps(doc)).encode(), headers=out)
            return httpx2.Response(response.status_code, content=response.content,
                                   headers=dict(response.headers))

        return httpx2.AsyncClient(transport=httpx2.MockTransport(handle),
                                  base_url=ISSUER, headers=headers or {})


class ListsAToolBesideTheRealOnes(ListsAToolOfItsOwn):
    """A host that leaves the surface's own tools in the listing and adds one of its own.

    The difference from the class above is the whole reason this class exists, and it is a
    defect of mine that it did not exist sooner. `ListsAToolOfItsOwn` REPLACES the list with a
    single entry, which is the right host for asking what one entry looks like on the screen --
    and the wrong host for asking whether one entry costs the others, because with it there are
    no others. The row below ran against it and its own guard was what noticed: it counted
    lines that were not tools and called them tools, and the day the listing changed shape the
    count fell to one and the row went red instead of passing on nothing."""

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
            if isinstance(result, dict) and isinstance(result.get("tools"), list) \
                    and result["tools"]:
                real = list(result["tools"])
                # First, not last: a host chooses the order too, and an entry
                # appended after everything cannot push anything off a screen
                # that is cut from the end. Measured -- with it last, removing
                # the desk's bound entirely left this row green.
                result["tools"] = [dict(real[0], name=self.value)] + real
                out = {k: v for k, v in response.headers.items()
                       if k.lower() != "content-length"}
                return httpx2.Response(response.status_code,
                                       content=(head + json.dumps(doc)).encode(), headers=out)
            return httpx2.Response(response.status_code, content=response.content,
                                   headers=dict(response.headers))

        return httpx2.AsyncClient(transport=httpx2.MockTransport(handle),
                                  base_url=ISSUER, headers=headers or {})


class AnswersNothingUsable:
    """A host whose answer to a call is not the protocol at all, so nothing above it can make
    sense of the reply. The operator's own tool name is what the desk has left to say."""

    def __init__(self, app):
        self.app = app

    def factory(self, headers=None):
        import httpx2

        async def handle(request: httpx2.Request) -> httpx2.Response:
            response = await httpx2.ASGITransport(app=self.app).handle_async_request(request)
            await response.aread()
            if b'"result"' in response.content and b'"tools"' not in response.content:
                out = {k: v for k, v in response.headers.items()
                       if k.lower() != "content-length"}
                return httpx2.Response(200, content=b"not a protocol answer at all",
                                       headers=out)
            return httpx2.Response(response.status_code, content=response.content,
                                   headers=dict(response.headers))

        return httpx2.AsyncClient(transport=httpx2.MockTransport(handle),
                                  base_url=ISSUER, headers=headers or {})


# --------------------------------------------------------------------- the routes

async def _seen(capsys) -> str:
    out, err = capsys.readouterr()
    return (out + err).strip()


async def _token_endpoint(app, state, xdg, capsys, value, who):
    secret = state.store.add_principal(who, Role.viewer, 3600)
    capsys.readouterr()
    await sign_in(TokenEndpointRefuses(app, value).factory, MCP_URL, who, secret)
    return await _seen(capsys)


async def _resource_notice(app, state, xdg, capsys, value, who):
    secret = state.store.add_principal(who, Role.viewer, 3600)
    capsys.readouterr()
    await sign_in(Recorder(app, resource=value).factory, ISSUER, who, secret)
    return await _seen(capsys)


async def _the_claimed_role(app, state, xdg, capsys, value, who):
    secret = state.store.add_principal(who, Role.viewer, 3600)
    capsys.readouterr()
    await sign_in(TokenEndpointClaimsARole(app, value).factory, MCP_URL, who, secret)
    return await _seen(capsys)


async def _the_listing(app, state, xdg, capsys, value, who):
    from orbitd.client import async_main

    secret = state.store.add_principal(who, Role.viewer, 3600)
    hostile = ListsAToolOfItsOwn(app, value)
    await sign_in(hostile.factory, MCP_URL, who, secret)
    capsys.readouterr()
    await async_main(["tools"], http_client_factory=hostile.factory)
    return await _seen(capsys)


async def _the_listing_beside_the_real_ones(app, state, xdg, capsys, value, who):
    """The listing with the surface's own tools present and one of the host's beside them."""
    from orbitd.client import async_main

    secret = state.store.add_principal(who, Role.viewer, 3600)
    hostile = ListsAToolBesideTheRealOnes(app, value)
    await sign_in(hostile.factory, MCP_URL, who, secret)
    capsys.readouterr()
    await async_main(["tools"], http_client_factory=hostile.factory)
    return await _seen(capsys)


#: The names this surface really offers all share one prefix, which is how an entry is counted
#: here rather than by where it sits on the screen. A row that counted screen positions is what
#: went wrong the first time: the listing was re-laid-out as a name line and an indented
#: sentence, and the count fell to one.
A_REAL_NAME = re.compile(r"\borbit_[a-z][a-z0-9_]*\b")


async def _a_refusal_envelope(app, state, xdg, capsys, value, who):
    """One of the three sentences the last two rounds repaired, driven here as well. Its
    escape and its bound are in; what this file adds is the third kind of hostility, which
    every case holding that bound was blind to."""
    from orbitd.client import async_main
    from test_the_desks_own_words_are_its_own import HostileEnvelope

    secret = state.store.add_principal(who, Role.viewer, 3600)
    hostile = HostileEnvelope(app, {"code": value, "what_failed": "something",
                                    "next_action": "try again"})
    await sign_in(hostile.factory, MCP_URL, who, secret)
    capsys.readouterr()
    await async_main(["call", "orbit_auth_revoke", "--arg=jti=Q1w2E3r4T5y6U7i8"],
                     http_client_factory=hostile.factory)
    return await _seen(capsys)


async def _a_refused_call(app, state, xdg, capsys, value, who):
    """The name is the operator's own, and the sentence that comes back frames it."""
    import httpx2
    from orbitd.client import async_main

    secret = state.store.add_principal(who, Role.viewer, 3600)

    def make(headers=None):
        return httpx2.AsyncClient(transport=httpx2.ASGITransport(app=app),
                                  base_url=ISSUER, headers=headers or {})

    await sign_in(make, MCP_URL, who, secret)
    capsys.readouterr()
    await async_main(["call", value], http_client_factory=make)
    return await _seen(capsys)


async def _an_answer_that_fits_nothing(app, state, xdg, capsys, value, who):
    from orbitd.client import async_main

    secret = state.store.add_principal(who, Role.viewer, 3600)
    hostile = AnswersNothingUsable(app)
    await sign_in(hostile.factory, MCP_URL, who, secret)
    capsys.readouterr()
    await async_main(["call", value], http_client_factory=hostile.factory)
    return await _seen(capsys)


#: A route is a way a host, or the operator's own command, decides part of a line the desk
#: writes. Each is driven twice: once ordinary, once hostile.
#:
#: They divide by whose text the value is, because that decides what "measuring something"
#: means. On a HOST route the host's words must reach the operator at all -- a desk that
#: stopped echoing would pass every other row here and take the reason for this node with
#: it. On an OPERATOR route the value is the name the person typed, and what must reach them
#: is which call this was: a sentence that cannot even name the call has told them nothing
#: they did not already know.
class RefusesWithARole:
    """A host that refuses at the transport, naming a role it requires and a role it says the
    token holds. This is the branch that writes the desk's role clauses, and until this route
    existed nothing here drove it: a reviewer deleted one of the two clauses from the desk's
    registry and the whole suite stayed green."""

    def __init__(self, app, value):
        self.app, self.value = app, value

    def factory(self, headers=None):
        import httpx2

        async def handle(request: httpx2.Request) -> httpx2.Response:
            if request.url.path.endswith("/mcp"):
                body = json.dumps({"error": "forbidden", "required_role": self.value,
                                   "held_role": self.value})
                return httpx2.Response(403, content=body.encode(),
                                       headers={"content-type": "application/json"})
            response = await httpx2.ASGITransport(app=self.app).handle_async_request(request)
            await response.aread()
            return httpx2.Response(response.status_code, content=response.content,
                                   headers=dict(response.headers))

        return httpx2.AsyncClient(transport=httpx2.MockTransport(handle))


async def _the_role_clauses(app, state, xdg, capsys, value, who):
    """The desk's own role clauses, driven by a host that refuses naming both roles."""
    from orbitd.client import async_main

    secret = state.store.add_principal(who, Role.viewer, 3600)
    plain = RefusesWithARole(app, "viewer")
    await sign_in(plain.factory, MCP_URL, who, secret)
    capsys.readouterr()
    hostile = RefusesWithARole(app, value)
    await async_main(["call", "orbit_project_list"], http_client_factory=hostile.factory)
    return await _seen(capsys)


HOST_ROUTES = (
    ("the sentence for a refusal the surface explained", _a_refusal_envelope),
    ("the role clauses the desk writes when a host refuses", _the_role_clauses),
    ("the sign-in when the token endpoint refuses", _token_endpoint),
    ("the notice when the desk refuses a host's address", _resource_notice),
    ("the role the sign-in reports from the host's own token", _the_claimed_role),
    ("the listing of the tools a host offers", _the_listing),
)

OPERATOR_ROUTES = (
    ("the sentence for a call the surface refused", _a_refused_call),
    ("the sentence for an answer nothing above could read", _an_answer_that_fits_nothing),
)

ROUTES = HOST_ROUTES + OPERATOR_ROUTES

#: Routes whose line is host text from end to end. The listing prints a name the host chose
#: followed by a sentence the host wrote, so there is no part of the desk's own after the
#: value for a host to push off -- what a host can take from the operator there is the OTHER
#: entries, which is asked on its own below.
NO_TAIL_OF_ITS_OWN = ("the listing of the tools a host offers",)


def _who(kind: str, index: int, how: str = "") -> str:
    """A principal name of this drive's own, so two drives never collide in the store."""
    return f"{kind}{index}{abs(hash(how)) % 1000}"


def _the_desks_own_tail(ordinary: str) -> str:
    """What the desk itself wrote after the last thing the host said. Derived from the
    ordinary drive, so no wording is pinned here and a repair may rewrite every sentence."""
    if MARKER not in ordinary:
        return ""
    return ordinary.rsplit(MARKER, 1)[1]


class TestTheMeasurementIsWorthComparingAgainst:
    @pytest.mark.parametrize("index", range(len(HOST_ROUTES)),
                             ids=[row[0] for row in HOST_ROUTES])
    async def test_what_the_host_said_reaches_the_operator(self, app, state, xdg, capsys,
                                                           index):
        """Anti-vacuity: a desk that stopped echoing what a host says would pass every other
        row in this file and undo the reason the node exists."""
        what, route = HOST_ROUTES[index]
        seen = await route(app, state, xdg, capsys, MARKER, _who("m", index))
        assert MARKER in seen, (
            f"{what}: what the host said never reached the operator, so this route measures "
            f"nothing: {seen[:200]!r}")

    @pytest.mark.parametrize("index", range(len(ROUTES)), ids=[row[0] for row in ROUTES])
    async def test_the_desk_says_something_of_its_own_on_this_line(self, app, state, xdg,
                                                                   capsys, index):
        what, route = ROUTES[index]
        seen = await route(app, state, xdg, capsys, MARKER, _who("n", index))
        assert seen.strip() and seen.strip() != MARKER, (
            f"{what}: the desk wrote nothing of its own, so the operator reads the host and "
            f"nothing else: {seen[:200]!r}")


class TestASentenceNamesTheCallItIsAbout:
    """The property the fourth sentence was repaired to meet, asked of every sentence a
    person gets when their own call goes wrong. The name is the one thing the desk knows at
    that moment that the operator's own command line does not already tell them -- and an
    operator running several calls cannot tell which one a nameless sentence is about."""

    @pytest.mark.parametrize("index", range(len(OPERATOR_ROUTES)),
                             ids=[row[0] for row in OPERATOR_ROUTES])
    async def test_the_line_names_the_call(self, app, state, xdg, capsys, index):
        what, route = OPERATOR_ROUTES[index]
        seen = await route(app, state, xdg, capsys, MARKER, _who("c", index))
        assert MARKER in seen, (
            f"{what}: the sentence does not say which call it is about, so an operator "
            f"running several cannot tell which one this was: {seen[:200]!r}")


class TestTheDesksOwnLastPartIsStillOnTheScreen:
    """The half the bound was for, asked in the units the operator's screen counts."""

    @pytest.mark.parametrize("how,value", HOSTILITIES, ids=[row[0] for row in HOSTILITIES])
    @pytest.mark.parametrize("index", range(len(ROUTES)), ids=[row[0] for row in ROUTES])
    async def test_the_tail_survives(self, app, state, xdg, capsys, index, how, value):
        what, route = ROUTES[index]
        if what in NO_TAIL_OF_ITS_OWN:
            pytest.skip("this line is the host's text from end to end; the property that "
                        "matters on it is asked in the class below")
        ordinary = await route(app, state, xdg, capsys, MARKER,
                               _who("t", index, how) + "a")
        tail = _the_desks_own_tail(ordinary).strip()
        if not tail:
            pytest.skip("this route writes nothing of its own after the host's value")
        seen = await route(app, state, xdg, capsys, value, _who("t", index, how) + "b")
        assert tail in seen, (
            f"{what}, given {how}: the desk's own last part -- {tail[:60]!r} -- is not on "
            f"the operator's screen, so they are left with the host's words and no idea "
            f"what to do next. What landed ({len(seen)} characters): {seen[:160]!r}")


class TestAHostCannotWriteAPartOfTheDesksLine:
    """The half the escape was for, asked of every route rather than of three sentences."""

    @pytest.mark.parametrize("index", range(len(ROUTES)), ids=[row[0] for row in ROUTES])
    async def test_no_mark_of_the_desks_is_added(self, app, state, xdg, capsys, index):
        what, route = ROUTES[index]
        ordinary = await route(app, state, xdg, capsys, MARKER, _who("f", index) + "a")
        forged = await route(app, state, xdg, capsys, FORGERY, _who("f", index) + "b")
        offences = []
        for mark in ("; next: ", "(verified by the surface, not a claim)", "code=",
                     "what_failed=", "token holds "):
            before = ordinary.count(mark)
            after = _unescaped(forged, mark)
            if after > before:
                offences.append(f"{mark!r}: {before} when the host is ordinary, {after} now")
        assert not offences, (
            f"{what}: a host wrote a part of the desk's own line, so the operator reads the "
            f"host's instruction in the desk's voice: {offences}. What landed: "
            f"{forged[:200]!r}")


def _unescaped(text: str, mark: str) -> int:
    """How many times `mark` appears written by the desk rather than by a host. A host's own
    copy leaves with a backslash in front of the separator it starts with, which is how the
    desk's notation says "the host typed this"."""
    count = 0
    start = 0
    while True:
        at = text.find(mark, start)
        if at < 0:
            return count
        if at == 0 or text[at - 1] != "\\":
            count += 1
        start = at + 1


class TestOneEntryCannotCostTheOthers:
    """The listing is host text from end to end, so nothing of the desk's is on it to lose.
    What an operator loses there is the rest of the listing: a name long enough to spend the
    line takes every other tool off the screen, and an agent that reads the listing to decide
    what to call is left with the one entry the host chose.

    WHAT HOLDS THIS, RE-MEASURED AFTER A REVIEW FOUND MY FIRST ANSWER WRONG. Two things hold
    it, independently, and either alone is enough -- which is why naming one of them was worse
    than naming none:

      - each entry is framed on its own, so there is no shared line for one entry to spend;
      - the writer divides what is left of the line between the host values on it, so even on
        a shared line one value cannot take another's room.

    Measured, each mutation alone and then together: framing the whole list as one line leaves
    all thirty-nine rows in this file green. Giving every value the whole line instead of a
    share turns four rows red -- the tail rows on the two-value sentences, not these. Only
    with both gone do these three go red. The first version of this note said the framing held
    it and the cut did not; the mutation behind that claim removed both at once, and I read
    one cause into a result with two. A measurement quoted in a file is a claim the next
    reader will act on, so it is stated here as it was actually taken.

    The host puts its entry FIRST and leaves the surface's own tools in place: appended last it
    could not push anything off a screen cut from the end, and the row passed with the desk's
    cut removed entirely."""

    @pytest.mark.parametrize("how,value", HOSTILITIES, ids=[row[0] for row in HOSTILITIES])
    async def test_the_other_tools_are_still_listed(self, app, state, xdg, capsys, how,
                                                    value):
        ordinary = await _the_listing_beside_the_real_ones(app, state, xdg, capsys, MARKER,
                                                           _who("l", 0, how) + "a")
        others = sorted(set(A_REAL_NAME.findall(ordinary)))
        assert len(others) > 3, (
            f"only {len(others)} other tools were listed, so this case measures nothing")
        seen = await _the_listing_beside_the_real_ones(app, state, xdg, capsys, value,
                                                       _who("l", 0, how) + "b")
        missing = [name for name in others if name not in seen]
        assert not missing, (
            f"given {how}, a host-chosen name took {len(missing)} of the {len(others)} other "
            f"tools off the operator's screen, so the listing they read is the one the host "
            f"chose: {missing[:4]}")


# ---------------------------------------------------------------------------
# Which characters are the desk's marks at all.
#
# Three rounds circled this and a review settled it. The colon was escaped everywhere, so an
# address reached the operator with a backslash before its port; narrowing it left the comma
# and the semicolon doing the same thing; and the first contract written against the question
# had two blind spots a reviewer found in one sitting. Both are worth writing down, because
# they are the same mistake:
#
#   - Its rows were parametrized over the very table it was holding. Delete the semicolon from
#     that table and the rows did not go red, they stopped existing, and the suite passed at a
#     lower count.
#   - It read the desk's segments off the module by walking the calls to the writer. A segment
#     built in a list of its own and passed by name is invisible that way, and the role clauses
#     are built exactly like that -- so half the desk's vocabulary was never asked about, and
#     the runtime guard beside it drove only the same route table, so the two halves shared one
#     blind spot instead of covering each other.
#
# So the desk's vocabulary is read from two sources that fail differently, and the rows below
# ask the union. The running desk is one: every segment the writer is really handed while each
# route above is driven, which sees a segment however it was built but only on a route this
# file drives. The module is the other: every string in a function that writes a line and that
# is not marked as a host's, which sees a segment on a route nobody drives but only where it
# is written as text. The role clauses are invisible to the second and reached by the first;
# a sentence on an undriven route is the other way round.
#
# From the recorded segments, every character the desk writes that is not a letter, a digit or
# a space must be declared in the shape table or stand in the list below with a reason. A mark
# the desk writes and does not declare is one a host can forge; a character in ordinary prose
# is not a mark, and saying which is which is a decision, so a new one turns this red rather
# than passing quietly.
# ---------------------------------------------------------------------------

#: Punctuation the desk writes inside ordinary words rather than as a separator, with why each
#: is not a mark. A character here is one a host cannot use to imitate the desk's frame,
#: because the desk never writes it where a part of its sentence begins or ends.
NOT_A_MARK = {
    "-": "inside a word the desk writes, as in sign-in",
    "'": "an apostrophe inside a word the desk writes, as in the host's answer",
    "_": "inside a field name the desk writes, as in expires_at",
    ".": "a full stop inside a sentence the desk writes",
    "=": "the tail of a label, and the whole label is a word mark the desk already owns",
    "/": "inside a path or an address the desk writes",
}


def _the_modules_own_strings() -> list[str]:
    """Every string the desk writes in its own voice, read off the module. A string wrapped as
    a host's is not one. Collected from any function that writes a line, not only from the
    writer's own arguments, because a segment built in a list of its own and passed by name --
    which is how the role clauses are built -- is invisible to a reader that walks only the
    call."""
    source = pathlib.Path(client.__file__).read_text()
    tree = ast.parse(source)
    found: list[str] = []

    def strings_of(node) -> list[str]:
        if isinstance(node, ast.Call) and getattr(node.func, "id", "") == "_Host":
            return []
        # A line written to the log is read by whoever keeps this instance, never by the
        # operator at the desk, and its interpolation marks are not the desk's punctuation.
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) \
                and getattr(node.func.value, "id", "").lstrip("_") in ("log", "logger"):
            return []
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            return [node.value]
        if isinstance(node, ast.JoinedStr):
            return ["".join(part.value for part in node.values
                            if isinstance(part, ast.Constant))]
        out: list[str] = []
        for child in ast.iter_child_nodes(node):
            out += strings_of(child)
        return out

    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        writes = any(isinstance(inner, ast.Call) and getattr(inner.func, "id", "") == "_frame"
                     for inner in ast.walk(node))
        if not writes:
            continue
        # A docstring explains the function to a maintainer; it is never put in front of an
        # operator, and its prose carries punctuation the desk does not write.
        body = node.body[1:] if ast.get_docstring(node) is not None else node.body
        for statement in body:
            found += strings_of(statement)
    return found


async def _what_the_running_desk_writes(app, state, xdg, capsys, monkeypatch) -> list[str]:
    """Every segment the writer is really handed, over every route above."""
    segments: list[str] = []
    real = client._frame

    def recording(given):
        segments.extend(part for part in given if isinstance(part, str))
        return real(given)

    monkeypatch.setattr(client, "_frame", recording)
    for index, (_what, route) in enumerate(ROUTES):
        for value in (MARKER, "ordinary"):
            await route(app, state, xdg, capsys, value, _who("v", index, value))
    return segments


async def _what_the_desk_writes_before_a_host_value(app, state, xdg, capsys,
                                                    monkeypatch) -> list[str]:
    """Every segment of the desk's own that is followed, in the same line, by a value from
    outside. That adjacency is what makes a label forgeable: it is the thing a host's own text
    begins right after, so a host that reproduces it has written the desk's introduction to
    its own value."""
    labels: list[str] = []
    real = client._frame

    def recording(given):
        parts = list(given)
        for index, part in enumerate(parts[:-1]):
            if isinstance(part, str) and isinstance(parts[index + 1], client._Host):
                labels.append(part)
        return real(parts)

    monkeypatch.setattr(client, "_frame", recording)
    for index, (_what, route) in enumerate(ROUTES):
        for value in (MARKER, "ordinary"):
            await route(app, state, xdg, capsys, value, _who("b", index, value))
    return labels


async def _both_sources(app, state, xdg, capsys, monkeypatch) -> list[str]:
    running = await _what_the_running_desk_writes(app, state, xdg, capsys, monkeypatch)
    assert running, "no segment was recorded from the running desk, so one source is empty"
    module = _the_modules_own_strings()
    assert len(module) >= 20, (
        f"only {len(module)} of the desk's own strings were read off the module, so the "
        "other source is empty")
    return running + module


class TestTheDesksMarksAreTheOnesItWrites:
    """Not one row here is parametrized over the table it holds. Deleting an entry from that
    table turns a row red; it does not take the row away, which is what the version of this
    section a reviewer found could do -- removing the semicolon dropped its two rows and the
    suite passed at a lower count."""

    async def test_every_mark_the_desk_writes_is_declared_or_excused(self, app, state, xdg,
                                                                     capsys, monkeypatch):
        segments = await _both_sources(app, state, xdg, capsys, monkeypatch)
        written = {char for segment in segments for char in segment
                   if not (char.isalnum() or char.isspace())}
        undeclared = sorted(written - set(client._DESK_MARK_SHAPES) - set(NOT_A_MARK))
        assert not undeclared, (
            f"the desk writes {undeclared} in its own voice and neither declares them as its "
            "marks nor excuses them as ordinary text. A mark it writes and does not declare "
            "is one a host can type and be believed")

    async def test_what_the_desk_declares_it_really_writes(self, app, state, xdg, capsys,
                                                           monkeypatch):
        """The direction the deleted semicolon failed: a declared mark must be one the desk
        really writes, so the table cannot quietly shrink and take its rows with it."""
        segments = await _both_sources(app, state, xdg, capsys, monkeypatch)
        written = {char for segment in segments for char in segment}
        never = sorted(set(client._DESK_MARK_SHAPES) - written)
        assert not never, (
            f"the table declares {never} as the desk's marks, but neither the running desk "
            "nor the module writes them anywhere. A mark nobody writes is a mark nobody is "
            "holding")

    async def test_each_mark_is_written_only_in_the_shape_it_declares(self, app, state, xdg,
                                                                      capsys, monkeypatch):
        """What lets an address through: a mark declared as only-before-a-space is left alone
        everywhere else, so writing it in another shape would leave a forgery unopposed."""
        segments = await _both_sources(app, state, xdg, capsys, monkeypatch)
        wrong: list[str] = []
        for segment in segments:
            for index, char in enumerate(segment):
                # The question is the escape's own, asked of the desk's text as it would
                # be asked of a host's: a mark declared for every shape is the desk's
                # wherever it stands, and this row once re-derived the answer from the
                # table by hand and called a bracket before a letter unopposed when the
                # escape neutralises it there.
                if char in client._DESK_MARK_SHAPES and not client._is_the_desks_mark(
                        segment, index):
                    wrong.append(f"{char!r} followed by {segment[index + 1:index + 2]!r} in "
                                 f"{segment!r}")
        assert not wrong, (
            "the desk writes a mark in a shape it did not declare, so a host writing it the "
            f"same way is left unopposed: {wrong[:6]}")

    async def test_the_two_sources_see_different_things(self, app, state, xdg, capsys,
                                                        monkeypatch):
        """Anti-vacuity, and the reason there are two. If either could be dropped without
        losing anything, the blind spot a reviewer found would be back."""
        running = set(await _what_the_running_desk_writes(app, state, xdg, capsys, monkeypatch))
        module = set(_the_modules_own_strings())
        assert module - running, (
            "the module shows nothing the running routes miss, so reading it adds nothing "
            "and a sentence on an undriven route would be unheld")
        assert running - module, (
            "the running desk shows nothing the module misses, so the recording adds nothing "
            "and a segment built in a list of its own would be unheld")


class TestNothingTheDeskWritesBeforeAHostValueCanBeCopied:
    """The word half of the same question, and the half a reviewer showed was unheld: deleting
    a phrase from the role registry lost nothing, because no row asked what the desk writes.

    A label is forgeable exactly when a host's value begins right after it, so the drive reads
    that adjacency off the running desk rather than off any list. Each label must end in
    something the writer neutralises: a mark character, or a word mark. A label that ends in
    neither is one a host can reproduce and be believed.

    A label of nothing but whitespace is excluded, and only that: the listing indents a host's
    sentence under the name above it, and a host that writes the same spaces has written
    spaces. Nothing is claimed by them, so there is nothing to neutralise."""

    async def test_the_drive_found_labels_to_hold(self, app, state, xdg, capsys, monkeypatch):
        labels = await _what_the_desk_writes_before_a_host_value(app, state, xdg, capsys,
                                                                 monkeypatch)
        assert len(set(labels)) >= 4, (
            f"only {sorted(set(labels))} were found before a value from outside, so the row "
            "below holds almost nothing")

    async def test_every_one_of_them_ends_in_something_the_writer_neutralises(
            self, app, state, xdg, capsys, monkeypatch):
        labels = await _what_the_desk_writes_before_a_host_value(app, state, xdg, capsys,
                                                                 monkeypatch)
        unheld = sorted({label for label in set(labels)
                         if label.strip()
                         and not any(label.endswith(word)
                                     for word in client._DESK_WORD_MARKS)
                         and not any(char in client._DESK_MARK_SHAPES for char in label)})
        assert not unheld, (
            f"the desk writes {unheld} immediately before a value from outside, and a host "
            "that types the same thing is not neutralised: its text then reads as the desk's "
            "own introduction to a value the desk never chose")
