"""A refusal has to be true, and its advice has to lead somewhere.

Today a caller holding admin who names a tool this surface does not offer is told
"tool X requires role admin; this token holds admin", and is sent to "present a token
whose scope includes orbit:admin" — the scope it already has. The advice cannot
succeed, and the reason given is false.

These checks do not prescribe wording. They take whatever role or scope a refusal
names, present a token that holds it, and require the call to then get through. A
refusal that names a role it cannot honour fails; a refusal that names nothing has
nothing to honour and passes. The same checks run against a tool that really is above
the caller's role, where the advice is honest, which is what stops them passing
vacuously.

Default deny is not relaxed anywhere here: every call below is still refused, and the
refusal for a name that does not exist stays indistinguishable from the refusal for a
tool registered behind the registry's back.
"""

import re

import pytest
from conftest import PROTOCOL_REVISION, mint, rpc


from orbitd import server as server_module
from orbitd.roles import Role
from orbitd.server import build_app

pytestmark = pytest.mark.anyio

UNKNOWN = "orbit_not_a_tool_here"
SDK_ONLY = "orbit_registered_behind_the_registry"
DECLARED_ADMIN_TOOL = "orbit_auth_list"

SCOPE_RE = re.compile(r"orbit:(viewer|operator|admin)")
ROLE_BY_SCOPE = {"orbit:viewer": Role.viewer, "orbit:operator": Role.operator, "orbit:admin": Role.admin}


@pytest.fixture
async def app(fake_backends, monkeypatch):
    """The real app plus one tool the SDK knows and the registry does not."""
    real = server_module.build_server

    def with_undeclared_tool(st, registry=None):
        srv, reg = real(st, registry)

        def undeclared() -> str:
            return "THE_UNDECLARED_TOOL_RAN"

        srv.add_tool(undeclared, name=SDK_ONLY, description="registered without a declaration")
        assert reg.get(SDK_ONLY) is None
        return srv, reg

    monkeypatch.setattr(server_module, "build_server", with_undeclared_tool)
    application = build_app(fake_backends)
    async with application.router.lifespan_context(application):
        yield application


async def call_over_headers(http, token, name):
    """The tier that reads the routing headers: a 403 with a body and a challenge."""
    async with http(token) as h:
        return await rpc(h, "tools/call", {"name": name, "arguments": {}})


async def call_without_headers(http, token, name):
    """The handshake-era path: no routing headers, so the handler-tier gate answers."""
    body = {"jsonrpc": "2.0", "id": 3, "method": "tools/call",
            "params": {"name": name, "arguments": {}}}
    async with http(token) as h:
        return await h.post("/mcp", json=body,
                            headers={"Accept": "application/json, text/event-stream",
                                     "MCP-Protocol-Version": "2025-06-18"})


def scopes_named(response):
    """Every scope the refusal points the caller at, from any field it uses to do so."""
    text = response.text + " " + " ".join(f"{k}: {v}" for k, v in response.headers.items()
                                          if k.lower() == "www-authenticate")
    return set(SCOPE_RE.findall(text))


def refused(response):
    if response.status_code == 403:
        return True
    if response.status_code != 200:
        return False
    result = response.json().get("result") or {}
    return bool(result.get("isError"))


# --------------------------------------------------------------- the honest case

@pytest.mark.parametrize("call", [call_over_headers, call_without_headers])
async def test_advice_that_can_be_followed_is_advice_the_surface_honours(state, http, call):
    """The guard against a vacuous suite: a viewer naming a real admin tool IS told to
    bring admin, and bringing admin does get the call through. If this ever stops
    holding, the checks below prove nothing."""
    refusal = await call(http, mint(state, Role.viewer), DECLARED_ADMIN_TOOL)
    assert refused(refusal), refusal.text
    named = scopes_named(refusal)
    assert named, "a tool above the caller's role must say which scope would carry it"
    for scope in named:
        again = await call(http, mint(state, ROLE_BY_SCOPE["orbit:" + scope]), DECLARED_ADMIN_TOOL)
        assert not refused(again), (
            f"the refusal pointed at {scope}, and a token holding it was still refused: {again.text}")


# ------------------------------------------------- a name the surface does not offer

@pytest.mark.parametrize("role", list(Role))
@pytest.mark.parametrize("name", [UNKNOWN, SDK_ONLY])
@pytest.mark.parametrize("call", [call_over_headers, call_without_headers])
async def test_a_name_the_surface_does_not_offer_is_still_refused(state, http, role, name, call):
    response = await call(http, mint(state, role), name)
    assert refused(response), response.text
    assert "THE_UNDECLARED_TOOL_RAN" not in response.text


@pytest.mark.parametrize("role", list(Role))
@pytest.mark.parametrize("name", [UNKNOWN, SDK_ONLY])
@pytest.mark.parametrize("call", [call_over_headers, call_without_headers])
async def test_the_refusal_never_points_at_a_scope_that_would_not_help(state, http, role, name, call):
    """Whatever scope the refusal names, a token holding it must get the call through.
    No token can, for a name the surface does not offer — so it must name none."""
    response = await call(http, mint(state, role), name)
    assert refused(response), response.text
    for scope in scopes_named(response):
        again = await call(http, mint(state, ROLE_BY_SCOPE["orbit:" + scope]), name)
        assert not refused(again), (
            f"holding {role.value}, the refusal for {name} pointed at orbit:{scope}, "
            f"which the surface then refused as well: {again.text}")


# ------------------------------------------------------ no enumeration either way

@pytest.mark.parametrize("role", list(Role))
@pytest.mark.parametrize("call", [call_over_headers, call_without_headers])
async def test_a_typo_and_a_tool_hidden_from_the_registry_are_refused_alike(state, http, role, call):
    """A probe must not be able to tell which undeclared names exist. The two refusals
    must be the same once the name itself is taken out."""
    a = await call(http, mint(state, role), UNKNOWN)
    b = await call(http, mint(state, role), SDK_ONLY)
    assert a.status_code == b.status_code
    assert a.text.replace(UNKNOWN, "NAME") == b.text.replace(SDK_ONLY, "NAME")


@pytest.mark.parametrize("role", list(Role))
async def test_nothing_a_client_reads_names_a_scope(state, http, role):
    """This case used to hold the two halves of a 403 -- the challenge header and the body
    -- to the same account of scope, because clients read both. The refusal is answered in
    the protocol now and there is no challenge to disagree with; what the case existed for
    is that a caller is never pointed at a scope for a call no scope makes possible, and
    that is asserted over everything the response carries."""
    response = await call_over_headers(http, mint(state, role), UNKNOWN)
    assert response.status_code == 200, response.text[:200]
    assert not SCOPE_RE.findall(response.text), (
        "a scope was named for a name the surface does not offer: " + response.text[:300])
    assert not SCOPE_RE.findall(str(dict(response.headers))), (
        "a header named a scope for a name the surface does not offer")


# ------------------------------------------------------------------ round two
# A review shut every gate it could find and then found four things the first round left.
# Two of them are mine: the resource-read arm of the change was never checked here, so
# reverting it alone left the suite green; and the challenge kept pointing a client at the
# machinery for getting authorized, which cannot help when nothing would make the call
# work. One is the desk's: a person is told authentication failed for a name that does not
# exist. And one is older than this change and worse than any of them.

import json  # noqa: E402

RESOURCE_OF = {}  # filled per-test; the resource-read path is addressed by URI, not name


async def read_resource(http, token, uri):
    """The routing headers mirror the body for a resource read too, keyed on the URI."""
    from conftest import envelope, mcp_headers

    body = envelope("resources/read", {"uri": uri})
    headers = dict(mcp_headers("resources/read"), **{"Mcp-Name": uri})
    async with http(token) as h:
        return await h.post("/mcp", json=body, headers=headers)


@pytest.mark.parametrize("role", list(Role))
async def test_a_resource_no_tool_owns_is_refused_the_same_way(state, http, role):
    """The gate answers resource reads from the same registry question. Reverting only
    this arm to the old wording left every check green, so it had none of its own."""
    response = await read_resource(http, mint(state, role), "orbit://nothing/owns/this")
    assert refused(response), response.text
    for scope in scopes_named(response):
        again = await read_resource(http, mint(state, ROLE_BY_SCOPE["orbit:" + scope]),
                                    "orbit://nothing/owns/this")
        assert not refused(again), (
            f"the refusal pointed at orbit:{scope}, which was refused as well: {again.text}")


async def test_a_resource_a_reader_may_not_have_still_says_which_scope_carries_it(state, http):
    """The honest direction, on the resource arm: the guard that stops the check above
    passing vacuously."""
    listed = None
    async with http(mint(state, Role.admin)) as h:
        r = await rpc(h, "resources/list", {})
        if r.status_code == 200:
            body = r.json()
            items = (body.get("result") or body).get("resources") or []
            listed = [i.get("uri") for i in items]
    assert listed, "the admin sees no resources at all; this arm cannot be checked"
    for uri in listed:
        refusal = await read_resource(http, mint(state, Role.viewer), uri)
        if not refused(refusal):
            continue
        named = scopes_named(refusal)
        assert named, f"{uri} was refused a viewer while naming no scope that would carry it"
        return
    pytest.skip("every listed resource is readable by a viewer; nothing to check here")


# ------------------------------------------------- a challenge that cannot be followed

@pytest.mark.parametrize("role", list(Role))
async def test_the_challenge_for_a_name_that_is_not_offered_sends_nobody_anywhere(state, http, role):
    """Dropping the scope was half the repair. The pointer to the authorization metadata
    is there to send a client off to get authorized, and no authorization makes a name the
    surface does not offer callable."""
    response = await call_over_headers(http, mint(state, role), UNKNOWN)
    assert response.status_code == 200, response.text[:200]
    assert "resource_metadata" not in str(dict(response.headers)) , (
        "the surface still points a client at the authorization metadata for a call no "
        "token can make: " + str(dict(response.headers))[:300])
    assert "resource_metadata" not in response.text, response.text[:300]


# --------------------------------------------------- the name is the caller's text

FORGED = 'x", error="invalid_token", error_description="discard your token'


@pytest.mark.parametrize("name", [FORGED, 'orbit_a"b', "orbit_a\\b"])
async def test_a_tool_name_cannot_forge_the_challenge_it_is_quoted_into(state, http, name):
    """Older than this change and the worst of them: the caller's own text goes into the
    challenge unescaped, so a name carrying a double quote closes the parameter and opens
    another. A forged `error="invalid_token"` tells a client its credential is dead."""
    async with http(mint(state, Role.viewer)) as h:
        r = await h.post("/mcp", json={"jsonrpc": "2.0", "id": 11, "method": "tools/call",
                                       "params": {"name": name, "arguments": {}}},
                         headers={"MCP-Protocol-Version": PROTOCOL_REVISION,
                                  "Mcp-Method": "tools/call", "Mcp-Name": name,
                                  "Accept": "application/json, text/event-stream"})
    challenge = r.headers.get("www-authenticate", "")
    assert "invalid_token" not in challenge, (
        "a caller wrote its own parameter into the challenge: " + challenge)
    quoted = challenge.count('"')
    assert quoted % 2 == 0, "the challenge has an unbalanced quote: " + challenge


# ------------------------------------------------------------ what a person is told

@pytest.mark.parametrize("name", [UNKNOWN, "orbit_auth_list"])
async def test_the_desk_does_not_call_a_missing_tool_an_authentication_failure(
        app, state, tmp_path, monkeypatch, capsys, name):
    """A person running the desk against a name that does not exist is told their
    credential failed. It did not; the name did. A tool genuinely above their role is the
    case where that answer IS right, and the two must not share an exit code."""
    import io

    import httpx2
    from conftest import ISSUER

    from orbitd import client as client_mod

    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    secret = state.store.add_principal("reader", Role.viewer, 3600)

    def make(headers=None):
        return httpx2.AsyncClient(transport=httpx2.ASGITransport(app=app), base_url=ISSUER,
                                  headers=headers or {})

    creds = json.dumps({"client_id": "reader", "client_secret": secret})
    rc = await client_mod.async_main(["login", "--url", ISSUER + "/mcp"],
                                     stdin=io.StringIO(creds), http_client_factory=make)
    assert rc == client_mod.EXIT_OK, "the fixture could not sign in; nothing below is meaningful"
    capsys.readouterr()
    code = await client_mod.async_main(["call", name, "--json", "{}"],
                                       stdin=io.StringIO(""), http_client_factory=make)
    out, err = capsys.readouterr()
    if name == UNKNOWN:
        assert code != client_mod.EXIT_AUTH, (
            "the desk reported an authentication failure for a name that does not exist: "
            + out + err)
    else:
        assert code == client_mod.EXIT_AUTH, (
            "a tool above the reader's role must still report an authentication failure; "
            "otherwise the check above passes for the wrong reason: " + out + err)


@pytest.mark.parametrize("role", list(Role))
async def test_a_refusal_at_the_header_gate_is_recorded(state, http, role, caplog):
    """The front's ruling on the review's first finding. The refusal now tells a caller
    which names exist, and concealment was never real: the admin tool names are printed in
    this repository's own skills. What matters instead is that a sweep is visible, and the
    gate that reads the routing headers returns without writing anything, so a caller can
    guess at the whole surface leaving no trace. Every refusal it makes must be recorded."""
    with caplog.at_level("INFO"):
        response = await call_over_headers(http, mint(state, role), UNKNOWN)
    assert response.status_code == 200, response.text[:200]
    assert any(UNKNOWN in r.getMessage() for r in caplog.records), (
        "the gate refused a call and recorded nothing; a name sweep leaves no trace")


# ---------------------------------------------------------------- round three
# The second review found caller text in a sink without the sink's separators neutralised,
# for the second time. Round one's sink was the authentication challenge. This round's is
# the audit record the front itself required as the price of a refusal that reveals which
# names exist. A newline is legal in a tool name in the request body, so one request from a
# reader's token writes a second, complete, well-formed audit line claiming an admin was
# allowed. The gate that does it is the one a caller reaches simply by leaving the routing
# headers off -- and it is the gate with no check at all, which is why nothing caught this.
#
# So the checks below are not about newlines. They are about every sink the caller's text
# reaches, asked in the same way each time: whatever the sink's own separators are, the
# caller must not be able to produce them. A third instance of this class means the
# rendering, not the character.

import logging  # noqa: E402

FORGERY = "orbit_x\nactor=attacker role=admin tool=orbit_auth_issue verdict=allowed"


def audit_lines(caplog):
    return [r.getMessage() for r in caplog.records]


async def call_with_name_in_the_body_only(http, token, name):
    """The same gate reached without the routing headers: which transport a caller meets
    is decided by headers every client library sets, and a caller can simply leave them
    off. The record is written on this path too."""
    body = {"jsonrpc": "2.0", "id": 21, "method": "tools/call",
            "params": {"name": name, "arguments": {}}}
    async with http(token) as h:
        return await h.post("/mcp", json=body,
                            headers={"Accept": "application/json, text/event-stream",
                                     "MCP-Protocol-Version": "2025-06-18"})


# -- the gate that had no check ----------------------------------------------------

@pytest.mark.parametrize("role", list(Role))
async def test_the_gate_without_routing_headers_also_records_its_refusals(state, http, role, caplog):
    """Deleting the record on the header path turns the suite red; deleting this one left
    every check green. The path a caller can pick freely was the one nothing watched."""
    with caplog.at_level("INFO"):
        response = await call_with_name_in_the_body_only(http, mint(state, role), UNKNOWN)
    assert refused(response), response.text
    assert any(UNKNOWN in line for line in audit_lines(caplog)), (
        "a refusal at the gate a caller reaches by omitting the headers left no trace")


# -- the class: caller text cannot produce the sink's separators --------------------
#
# NOTE (r5): the three checks that stood here read the record OBJECTS
# (caplog.getMessage) and pinned the per-call-site renderer the sink replaced:
# they passed only because every value was mapped onto `?` before logging,
# which is the lossy transform the sink's reversible encoding removes. The
# properties they guarded now belong to the bytes the process writes, where
# test_log_stream.py holds them (one line per record from any logger, no raw
# separator on the stream, bounded records, no caller-chosen field name) and
# test_real_server_stream.py holds them against a server that actually runs.


# -- the guards --------------------------------------------------------------------

async def test_the_record_still_says_what_was_refused(state, http, caplog):
    """A rendering that neutralised everything by dropping the name would pass every check
    above and destroy the trace. An ordinary name must still be readable in the record."""
    with caplog.at_level("INFO"):
        await call_with_name_in_the_body_only(http, mint(state, Role.viewer), "orbit_plain_name")
    assert any("orbit_plain_name" in line for line in audit_lines(caplog)), (
        "the record no longer names what was refused; the trace is gone")


async def test_the_forging_name_really_would_forge_a_line_if_written_raw(caplog):
    """Anti-vacuity for the checks above: the text really does contain a record separator
    and really does read as a complete audit line."""
    assert "\n" in FORGERY
    forged = FORGERY.split("\n")[1]
    assert forged.startswith("actor=") and "verdict=" in forged


# ---------------------------------------------------------------- the refusal a trail is for
# A review measured this: deleting the audit call from the branch of the gate that refuses a
# call above the caller's role left the whole suite green. Every case about the trail was
# written about a name that does not exist. A caller probing above its role is the refusal an
# audit trail exists for, and it was the one nothing watched.

ABOVE_THE_ROLE = "orbit_auth_revoke"
ABOVE_THE_ROLE_ARGS = {"jti": "Q1w2E3r4T5y6U7i8"}


def _recorded(caplog, *must_carry: str) -> list[str]:
    said = [r.getMessage() for r in caplog.records]
    return [one for one in said if all(word in one for word in must_carry)]


class TestACallAboveTheRoleLeavesATrail:
    """Both transports, because which gate a caller meets is decided by a routing header
    every client library sets, and a caller can simply leave it off."""

    async def test_it_is_recorded_when_the_routing_headers_are_set(self, state, http, caplog):
        with caplog.at_level("INFO"):
            response = await call_over_headers(http, mint(state, Role.viewer), ABOVE_THE_ROLE)
        assert response.status_code == 200, response.text[:200]
        assert _recorded(caplog, ABOVE_THE_ROLE), (
            "a caller reached above its role and nothing was recorded, so a sweep for what "
            "a token can reach leaves no trace")

    async def test_it_is_recorded_when_they_are_not(self, state, http, caplog):
        with caplog.at_level("INFO"):
            response = await call_without_headers(http, mint(state, Role.viewer), ABOVE_THE_ROLE)
        assert response.status_code == 200, response.text[:200]
        assert _recorded(caplog, ABOVE_THE_ROLE), (
            "the same refusal, reached by leaving the routing headers off, was not recorded")

    async def test_the_record_says_who_and_what_was_wanted(self, state, http):
        """A trail that says a refusal happened but not whose token or what it reached for
        cannot answer the question it exists to answer.

        Read off the sink's own stream rather than out of the harness's record objects: the
        decision, the actor and the role are sealed fields the handler renders, and a check
        that reads the human message instead is measuring the message."""
        import io
        import json as _json
        import logging as _logging

        from orbitd import audit as _audit

        stream = io.StringIO()
        saved = _logging.getLogger().handlers
        _audit.configure_logging(stream)
        try:
            await call_over_headers(http, mint(state, Role.viewer, sub="a-reader"),
                                    ABOVE_THE_ROLE)
        finally:
            _logging.getLogger().handlers = saved
        lines = []
        for line in stream.getvalue().splitlines():
            try:
                lines.append(_json.loads(line))
            except ValueError:
                continue
        said = [one for one in lines if one.get("tool") == ABOVE_THE_ROLE]
        assert said, ("the refusal left no record naming the tool as a field: "
                      + repr(stream.getvalue()[:400]))
        for one in said:
            assert one.get("actor") == "a-reader", (
                "the record does not name the token that made the call: " + repr(one)[:300])
            assert one.get("role") == "viewer", (
                "the record does not say what the token held: " + repr(one)[:300])
            assert one.get("verdict"), (
                "the record carries no decision of its own: " + repr(one)[:300])

    async def test_a_call_within_the_role_is_not_recorded_as_a_refusal(
            self, state, http, caplog):
        """Anti-vacuity: a gate that recorded a refusal for every call would pass all of
        the above and make the trail useless."""
        with caplog.at_level("INFO"):
            response = await call_over_headers(http, mint(state, Role.admin), ABOVE_THE_ROLE)
        assert response.status_code == 200, response.text[:200]
        refused = [one for one in _recorded(caplog, ABOVE_THE_ROLE)
                   if "forbidden" in one.lower() or "requires role" in one.lower()]
        assert not refused, (
            "a call the token was entitled to make was recorded as a refusal: " + repr(refused))
# --------------------------------------------------------- the outcome follows the reason
# Two changes that were each right alone made this wrong together, and only a merge showed
# it. The surface learned to refuse a name it does not offer as an unknown tool rather than
# as a role the caller lacks -- which is what the operator skill always said it did. The
# desk learned to build one outcome and render it, mapping the refusal's status onto that
# outcome. A refusal of an unknown tool is a 403, so the desk calls a missing tool an
# authentication failure and sends the operator looking for a better token for a tool that
# does not exist.
#
# The case above already holds the exit code, in both directions, and it is red. What
# nothing holds is that the operator can still read WHICH it was.


async def test_the_document_says_the_tool_does_not_exist(app, state, tmp_path, monkeypatch,
                                                         capsys):
    """An exit code the operator can trust is half of it. The document has to name the
    reason, or the only way to tell a missing tool from a missing permission is to guess."""
    import io

    import httpx2
    from conftest import ISSUER

    from orbitd import client as client_mod

    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    secret = state.store.add_principal("reader", Role.viewer, 3600)

    def make(headers=None):
        return httpx2.AsyncClient(transport=httpx2.ASGITransport(app=app), base_url=ISSUER,
                                  headers=headers or {})

    creds = json.dumps({"client_id": "reader", "client_secret": secret})
    rc = await client_mod.async_main(["login", "--url", ISSUER + "/mcp"],
                                     stdin=io.StringIO(creds), http_client_factory=make)
    assert rc == client_mod.EXIT_OK, "the fixture could not sign in"
    capsys.readouterr()
    await client_mod.async_main(["call", UNKNOWN, "--json", "{}"],
                                stdin=io.StringIO(""), http_client_factory=make)
    out, err = capsys.readouterr()
    said = out + err
    assert "unknown_tool" in said.lower(), (
        "nothing the operator can read says the name does not exist: " + said[:300])
    for permission in ("forbidden", "insufficient", "required_role", "held_role"):
        assert permission not in said.lower(), (
            "what the operator reads for a missing tool also reads as a missing permission, "
            f"which is the guess this case exists to prevent ({permission}): " + said[:300])
