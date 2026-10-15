"""Signing in with the address a person was actually handed.

A person is given a host — "orbit is at orbit.example.com" — not a full interface
address. Handed that host, the sign-in is refused with a four hundred and the desk prints
the number alone, so the reader is told nothing they can act on. Handed the same host with
the interface path appended, it succeeds.

Both halves of the fix are already on the server and thrown away by the desk. The host
serves a resource document naming the exact address it issues tokens for, and the refusal
itself carries a description saying so in words. The desk fetches the authorization-server
document from that same origin and never the resource document, and on a failure it prints
the status code without the body.

These checks drive the real client against the real server over the same in-process
transport the rest of the desk suite uses. They say nothing about how the repair is made:
following the resource document, or reading the description back, or both.
"""

from __future__ import annotations

import io
import json

import httpx2
import pytest
from conftest import ISSUER, MCP_URL

from orbitd.client import EXIT_OK, async_main, canonical_mcp_url
from orbitd.roles import Role

pytestmark = pytest.mark.anyio

CREDENTIAL = "this-secret-must-never-appear-on-stdio"


@pytest.fixture
def xdg(tmp_path, monkeypatch):
    d = tmp_path / "xdg"
    monkeypatch.setenv("XDG_CONFIG_HOME", str(d))
    return d


@pytest.fixture
def factory(app):
    def make(headers=None):
        return httpx2.AsyncClient(transport=httpx2.ASGITransport(app=app),
                                  base_url=ISSUER, headers=headers or {})
    return make


async def sign_in(factory, url, name, secret):
    stdin = io.StringIO(json.dumps({"client_id": name, "client_secret": secret}))
    return await async_main(["login", "--url", url], stdin=stdin, http_client_factory=factory)


# ------------------------------------------------------- the shape of the problem

def test_the_address_a_person_is_handed_is_not_the_one_the_server_issues_for():
    """Anti-vacuity: if the bare host already canonicalised to the interface address there
    would be nothing here to fix, and every check below would pass for the wrong reason."""
    assert canonical_mcp_url(ISSUER) != MCP_URL


async def test_the_server_names_the_right_address_where_the_desk_can_read_it(factory):
    """The other half of the anti-vacuity guard: the repair is possible without guessing,
    because the host publishes the address in a document at a well-known place."""
    async with factory() as http:
        r = await http.get(ISSUER + "/.well-known/oauth-protected-resource")
    assert r.status_code == 200, r.text
    assert r.json().get("resource") == MCP_URL


# ------------------------------------------------------------- what must become true

async def test_signing_in_with_the_bare_host_works(state, factory, xdg):
    """The whole point. A reader hands the desk what a person told them and gets in."""
    secret = state.store.add_principal("ci-reader", Role.viewer, 3600)
    assert await sign_in(factory, ISSUER, "ci-reader", secret) == EXIT_OK


async def test_the_session_is_filed_under_the_address_the_server_named(state, factory, xdg, capsys):
    """Signing in with the host must leave the desk able to CALL. A session filed under the
    typed address would send the reader straight back to a sign-in on the next command."""
    secret = state.store.add_principal("ci-reader", Role.viewer, 3600)
    assert await sign_in(factory, ISSUER, "ci-reader", secret) == EXIT_OK
    capsys.readouterr()
    rc = await async_main(["tools"], stdin=io.StringIO(""), http_client_factory=factory)
    out, err = capsys.readouterr()
    assert rc == EXIT_OK, err
    assert "orbit_platform_health" in out


async def test_the_full_interface_address_still_works(state, factory, xdg):
    """The address the documentation shows must not stop working."""
    secret = state.store.add_principal("ci-reader", Role.viewer, 3600)
    assert await sign_in(factory, MCP_URL, "ci-reader", secret) == EXIT_OK


# --------------------------------------- and when it is still refused, say why

async def test_a_refusal_the_server_explained_reaches_the_person(state, factory, xdg, capsys):
    """Whatever address the desk ends up asking for, if the server refuses it in words,
    those words are what the reader needs. The credential here is good and the address is
    not, which is the refusal that today prints a bare status number."""
    secret = state.store.add_principal("ci-reader", Role.viewer, 3600)
    await sign_in(factory, ISSUER + "/not-the-interface", "ci-reader", secret)
    out, err = capsys.readouterr()
    blob = out + err
    assert MCP_URL in blob, (
        "the server said which address it issues tokens for and the desk did not pass it on: "
        + blob)


async def test_no_credential_is_printed_on_any_of_these_paths(state, factory, xdg, capsys):
    secret = state.store.add_principal("ci-reader", Role.viewer, 3600)
    await sign_in(factory, ISSUER, "ci-reader", secret)
    await sign_in(factory, ISSUER + "/not-the-interface", "ci-reader", CREDENTIAL)
    out, err = capsys.readouterr()
    assert CREDENTIAL not in out + err
    assert secret not in out + err


# ----------------------------------------------------------------- round two
# A review asked the one question this change turns on -- can the credential end up
# somewhere the person never typed -- and answered it by building the transport: a host
# whose resource document names another origin had the secret posted to that origin's token
# endpoint. The first round of this contract never asked where the credential went, only
# whether the sign-in succeeded. It asks now.

ELSEWHERE = "https://somewhere-else.invalid"


class Recorder:
    """A transport that answers for two hosts: the one a person typed, which is the real
    server, and every other host, which records what it was sent and answers plausibly."""

    def __init__(self, app, *, resource=None, token_endpoint=None):
        self.app = app
        self.resource = resource
        self.token_endpoint = token_endpoint
        self.elsewhere = []
        self.sent = []

    def factory(self, headers=None):
        import httpx2

        async def handle(request: httpx2.Request) -> httpx2.Response:
            url = str(request.url)
            host = request.url.host
            self.sent.append((url, len(request.content)))
            if host != "mcp.fixture.invalid":
                self.elsewhere.append((url, request.content.decode("utf-8", "replace")))
                return httpx2.Response(200, json={"access_token": "x", "token_type": "Bearer"})
            if url.endswith("/.well-known/oauth-protected-resource") and self.resource is not None:
                return httpx2.Response(200, json={"resource": self.resource})
            if url.endswith("/.well-known/oauth-authorization-server") and self.token_endpoint is not None:
                return httpx2.Response(200, json=self.token_endpoint)
            return await httpx2.ASGITransport(app=self.app).handle_async_request(request)

        return httpx2.AsyncClient(transport=httpx2.MockTransport(handle),
                                  base_url=ISSUER, headers=headers or {})

    def saw_secret(self, secret):
        return [u for u, body in self.elsewhere if secret in body]


async def test_the_recorder_really_does_catch_a_credential_sent_elsewhere(app, state, xdg):
    """Anti-vacuity, and it must not be written in terms of the defect: once the desk pins
    the origin, no sign-in can reach another host, so a guard that drives the desk would
    fail for the right reason and look like a regression. Post to the other host directly
    instead. If the recorder cannot see a credential handed straight to it, it cannot prove
    one is withheld."""
    secret = state.store.add_principal("ci-reader", Role.viewer, 3600)
    rec = Recorder(app)
    async with rec.factory() as h:
        await h.post(ELSEWHERE + "/token",
                     data={"grant_type": "client_credentials", "client_id": "ci-reader",
                           "client_secret": secret})
    assert rec.saw_secret(secret), "the recorder saw nothing; every check below would be empty"


async def test_a_resource_document_naming_another_host_does_not_get_the_credential(
        app, state, xdg):
    """The review's blocker, proved by running the real sign-in: a host whose document
    names another origin had the secret posted to that origin."""
    secret = state.store.add_principal("ci-reader", Role.viewer, 3600)
    rec = Recorder(app, resource=ELSEWHERE + "/mcp")
    await sign_in(rec.factory, ISSUER, "ci-reader", secret)
    assert not rec.saw_secret(secret), (
        "the credential was posted to a host the person never typed: " + str(rec.saw_secret(secret)))


async def test_a_token_endpoint_naming_another_host_does_not_get_the_credential(
        app, state, xdg):
    """The same hole through the other document the desk already fetched before this
    change. The value was validated not at all."""
    secret = state.store.add_principal("ci-reader", Role.viewer, 3600)
    rec = Recorder(app, token_endpoint={"token_endpoint": ELSEWHERE + "/token"})
    await sign_in(rec.factory, MCP_URL, "ci-reader", secret)
    assert not rec.saw_secret(secret), (
        "the credential was posted to a host the person never typed: " + str(rec.saw_secret(secret)))


async def test_a_document_that_is_not_an_object_does_not_crash_the_sign_in(app, state, xdg, capsys):
    """A list where an object belongs threw a traceback onto the reader's terminal. The
    other document's reader already falls back; this one must too."""
    secret = state.store.add_principal("ci-reader", Role.viewer, 3600)
    rec = Recorder(app, token_endpoint=["not", "an", "object"])
    code = await sign_in(rec.factory, MCP_URL, "ci-reader", secret)
    out, err = capsys.readouterr()
    assert "Traceback" not in out + err, out + err
    assert code == EXIT_OK, (
        "a broken document must fall back to the address the person typed: " + out + err)


async def test_an_enormous_resource_string_is_not_taken_as_an_address(app, state, xdg):
    """Unbounded text from a host became the address the desk signed in to."""
    secret = state.store.add_principal("ci-reader", Role.viewer, 3600)
    rec = Recorder(app, resource="https://mcp.fixture.invalid/" + "a" * 3_000_000)
    await sign_in(rec.factory, ISSUER, "ci-reader", secret)
    oversized = [(url, size) for url, size in rec.sent if len(url) > 10_000 or size > 100_000]
    assert not oversized, (
        "a three-megabyte string from a host became the address the desk signed in to: "
        + str([(u[:60] + "...", n) for u, n in oversized]))


# ---------------------------------------------------------------- round three
# The pin holds against twenty-six lookalike origins. It has a hole in front of it: the
# token endpoint carried in the credential blob is returned before any check runs, so the
# one source an operator is TOLD to use is the one source nothing pins. Bootstrap prints
# that blob, the issuing verb returns the same shape, and the documentation says to pipe it
# into the sign-in. No hostility is needed for it to bite: a blob copied from one
# environment sends another environment's secret to the first.
#
# And the refusal this node added to stop a person being told nothing now prints the host's
# own text raw and unbounded, in the one command that reads a credential from standard
# input. That is the third place on this front where remote text reached an output without
# that output's separators neutralised.

BLOB_ELSEWHERE = "https://blob-says-elsewhere.invalid/token"


async def sign_in_with_hint(factory, url, name, secret, token_endpoint):
    """The blob a person is told to paste: it carries a token endpoint of its own."""
    stdin = io.StringIO(json.dumps({"client_id": name, "client_secret": secret,
                                    "token_endpoint": token_endpoint}))
    return await async_main(["login", "--url", url], stdin=stdin, http_client_factory=factory)


async def test_the_blob_really_does_carry_a_token_endpoint(state, factory, xdg):
    """Anti-vacuity: the blob's own field must be honoured at all, or the check below is
    about nothing. Pointed at the right endpoint, the sign-in works."""
    secret = state.store.add_principal("ci-reader", Role.viewer, 3600)
    assert await sign_in_with_hint(factory, MCP_URL, "ci-reader", secret,
                                   ISSUER + "/token") == EXIT_OK


async def test_a_token_endpoint_in_the_pasted_blob_does_not_get_the_credential(
        app, state, xdg):
    """The source an operator is told to use is the source nothing pins."""
    secret = state.store.add_principal("ci-reader", Role.viewer, 3600)
    rec = Recorder(app)
    await sign_in_with_hint(rec.factory, MCP_URL, "ci-reader", secret, BLOB_ELSEWHERE)
    assert not rec.saw_secret(secret), (
        "a token endpoint pasted with the credential sent it to a host the person never "
        "typed: " + str(rec.saw_secret(secret)))


async def test_the_typed_host_is_the_one_contacted(app, state, xdg):
    """Not only must the secret not go elsewhere; the sign-in must actually happen where
    the person said, rather than failing silently in a way that looks the same."""
    secret = state.store.add_principal("ci-reader", Role.viewer, 3600)
    rec = Recorder(app)
    await sign_in_with_hint(rec.factory, MCP_URL, "ci-reader", secret, BLOB_ELSEWHERE)
    assert any("mcp.fixture.invalid" in url for url, _ in rec.sent), (
        "the typed host was never contacted at all")


# ------------------------------------------- what the host is allowed to put on the screen

SCREEN_ATTACK = ("\x1b[2J\x1b[H" + "enter your client secret: " + "x" * 200_000)


class Hostile:
    """A token endpoint that answers a refusal carrying whatever text it likes."""

    def __init__(self, app, body):
        self.app = app
        self.body = body

    def factory(self, headers=None):
        import httpx2

        async def handle(request: httpx2.Request) -> httpx2.Response:
            if request.url.path.endswith("/token"):
                return httpx2.Response(400, json={"error": "invalid_target",
                                                  "error_description": self.body})
            return await httpx2.ASGITransport(app=self.app).handle_async_request(request)

        return httpx2.AsyncClient(transport=httpx2.MockTransport(handle),
                                  base_url=ISSUER, headers=headers or {})


async def test_a_host_cannot_paint_the_readers_screen(app, state, xdg, capsys):
    """The sign-in is the one command that reads a credential from standard input. A host
    that can clear the screen and print a prompt is asking the person for their secret."""
    secret = state.store.add_principal("ci-reader", Role.viewer, 3600)
    await sign_in(Hostile(app, SCREEN_ATTACK).factory, MCP_URL, "ci-reader", secret)
    out, err = capsys.readouterr()
    blob = out + err
    assert "\x1b" not in blob, "a host wrote a terminal escape onto the reader's screen"
    assert len(blob) < 4096, f"a host wrote {len(blob)} characters onto the reader's screen"


async def test_a_host_cannot_write_a_second_line_of_its_own(app, state, xdg, capsys):
    secret = state.store.add_principal("ci-reader", Role.viewer, 3600)
    await sign_in(Hostile(app, "first\nsecond line the host chose").factory,
                  MCP_URL, "ci-reader", secret)
    out, err = capsys.readouterr()
    assert "\n" not in (out + err).strip(), (
        "a host wrote more than one line onto the reader's screen: " + repr(out + err))


async def test_what_the_server_said_still_reaches_the_person(app, state, xdg, capsys):
    """The guard: a repair that simply stopped echoing would pass both checks above and
    undo the whole reason this node exists."""
    secret = state.store.add_principal("ci-reader", Role.viewer, 3600)
    await sign_in(Hostile(app, "this server issues tokens only for https://x.invalid/mcp").factory,
                  MCP_URL, "ci-reader", secret)
    out, err = capsys.readouterr()
    assert "https://x.invalid/mcp" in out + err, (
        "the server explained the refusal and the person was told nothing: " + out + err)


# --------------------------------------------------- and when the desk itself refuses

async def test_the_reader_is_told_when_the_desk_refuses_an_address(app, state, xdg, capsys):
    """A refusal the desk makes on the person's behalf must say so. Otherwise they get the
    bare status number this whole node was opened on, from a decision the desk made."""
    secret = state.store.add_principal("ci-reader", Role.viewer, 3600)
    rec = Recorder(app, resource=ELSEWHERE + "/mcp")
    await sign_in(rec.factory, ISSUER, "ci-reader", secret)
    out, err = capsys.readouterr()
    blob = (out + err).lower()
    assert "somewhere-else.invalid" in blob or "origin" in blob or "refus" in blob, (
        "the desk refused an address on the person's behalf and told them nothing: "
        + out + err)


# ---------------------------------------------------------------------------------------
# Round four. The renderer round three added is not where the text leaves.
# ---------------------------------------------------------------------------------------
#
# Round three added one renderer and routed the refusal the desk parses through it. A review
# then drove the OTHER refusal — the one a host is entitled to make, 401 or 403 — and found
# it prints the host's words and the host's authentication challenge straight to the screen:
# two hundred thousand characters, terminal escapes intact, on a path a hostile typed host
# controls.
#
# That is the fourth time in this front that outside text has reached an output whose
# separators were not neutralised, and the third node it has happened in. Each repair so far
# has been a renderer that every author of every print must remember to call, and each time
# the text left through a print somebody did not change. So the rule moves to where the text
# leaves: the desk renders at its own output, once, and a print that has never heard of this
# is covered because the stream is.


class HostileAuth:
    """A host answering the refusal it is entitled to make, carrying what it likes."""

    def __init__(self, app, body=None, www="", error="invalid_client"):
        self.app = app
        self.body = body
        self.www = www
        self.error = error

    def factory(self, headers=None):
        import httpx2

        async def handle(request: httpx2.Request) -> httpx2.Response:
            if request.url.path.endswith("/token"):
                payload = {"error": self.error} if self.error else {}
                if self.body is not None:
                    payload["error_description"] = self.body
                return httpx2.Response(401, json=payload,
                                       headers={"www-authenticate": self.www} if self.www else {})
            return await httpx2.ASGITransport(app=self.app).handle_async_request(request)

        return httpx2.AsyncClient(transport=httpx2.MockTransport(handle),
                                  base_url=ISSUER, headers=headers or {})


async def test_a_host_cannot_paint_the_screen_through_the_refusal_it_may_make(app, state, xdg, capsys):
    secret = state.store.add_principal("ci-reader", Role.viewer, 3600)
    await sign_in(HostileAuth(app, body=SCREEN_ATTACK).factory, MCP_URL, "ci-reader", secret)
    blob = "".join(capsys.readouterr())
    assert "\x1b" not in blob, "a host wrote a terminal escape onto the reader's screen"
    assert len(blob) < 4096, f"a host wrote {len(blob)} characters onto the reader's screen"


async def test_a_host_cannot_paint_the_screen_through_its_challenge_header(app, state, xdg, capsys):
    """The header is not the body, and round three's renderer never saw it."""
    secret = state.store.add_principal("ci-reader", Role.viewer, 3600)
    await sign_in(HostileAuth(app, www='Bearer realm="' + SCREEN_ATTACK + '"',
                              error=None).factory,
                  MCP_URL, "ci-reader", secret)
    blob = "".join(capsys.readouterr())
    assert "\x1b" not in blob, "a host wrote a terminal escape through its challenge header"
    assert len(blob) < 4096, f"a host wrote {len(blob)} characters through its challenge header"


async def test_a_host_cannot_write_a_second_line_through_either(app, state, xdg, capsys):
    secret = state.store.add_principal("ci-reader", Role.viewer, 3600)
    for hostile in (HostileAuth(app, body="first\nsecond line the host chose"),
                    HostileAuth(app, www='Bearer realm="third\nfourth line"', error=None)):
        await sign_in(hostile.factory, MCP_URL, "ci-reader", secret)
        assert "\n" not in "".join(capsys.readouterr()).strip()


async def test_what_the_host_said_in_that_refusal_still_reaches_the_person(app, state, xdg, capsys):
    """The guard. A repair that stopped echoing would pass everything above and leave the
    person with the bare status number this node was opened on."""
    secret = state.store.add_principal("ci-reader", Role.viewer, 3600)
    await sign_in(HostileAuth(app, body="this token is for another audience").factory,
                  MCP_URL, "ci-reader", secret)
    assert "another audience" in "".join(capsys.readouterr())


class TestTheDeskRendersWhereTextLeaves:
    """The sink, not the call sites. Three renderers at three call sites have now been
    walked round three times by a print that did not use one."""

    def configure(self, stream):
        from orbitd import client as client_mod

        fn = getattr(client_mod, "configure_output", None)
        assert fn is not None, (
            "orbitd.client has no configure_output; the rendering is still at the call "
            "sites, which is what let the refusal path print a host's words unrendered")
        return fn(stream)

    def test_a_print_that_never_heard_of_the_renderer_is_covered(self):
        import io

        stream = io.StringIO()
        wrapped = self.configure(stream)
        print("a host said " + SCREEN_ATTACK[:64], file=wrapped)
        written = stream.getvalue()
        assert "\x1b" not in written, "a plain print put a terminal escape on the screen"
        assert len(written.splitlines()) == 1

    def test_an_ordinary_message_is_unchanged(self):
        """Anti-vacuity: a wrapper that mangled everything would pass the case above."""
        import io

        stream = io.StringIO()
        wrapped = self.configure(stream)
        print("orbit: signed in as ci-reader at https://mcp.fixture.invalid/mcp", file=wrapped)
        assert stream.getvalue().rstrip("\n") == (
            "orbit: signed in as ci-reader at https://mcp.fixture.invalid/mcp")

    def test_what_a_host_sent_is_recoverable(self):
        """A reader must be able to tell a host's own text from a byte that was
        neutralised, so the rendering is reversible rather than a substitution."""
        import io

        stream = io.StringIO()
        wrapped = self.configure(stream)
        print("a host said first\rsecond", file=wrapped)
        written = stream.getvalue()
        assert "\r" not in written
        assert "first" in written and "second" in written
        assert "\\r" in written or "\\x0d" in written or "\\u000d" in written, (
            "the control byte left no trace of what it was: " + repr(written))


# --------------------------------------------------- the role the desk states as a fact
# The desk ends a sign-in by telling the operator two things: when the token expires and
# what role it carries. It reads both by decoding the token with signature checking turned
# off, because the key that would verify it lives on the surface and never on the desk.
#
# So the role is not something the desk knows. It is something the token asserts, and any
# endpoint that answers the sign-in can assert anything. An operator told "admin" by a
# forged token believes they hold powers the surface will refuse, and the belief is the
# damage: nothing in the desk decides on that value, so this is not an escalation, it is
# the desk stating as fact something it cannot establish.
#
# The repair is not to verify the signature, which the desk cannot do. It is to stop
# presenting a claim as a finding. What the desk says about the role must name where the
# role came from, and it must say it the same way whether the claim is true or forged --
# because the desk cannot tell those apart, and a message whose shape changes with the
# truth of the claim would be a verification it is not performing.


def _session_file():
    from orbitd.client import session_path

    return session_path(canonical_mcp_url(MCP_URL))


def claimed_role_marker() -> str:
    """The desk exposes the word it marks a claim with, so this contract pins the
    distinction and not a phrasing. A repair may choose any word it likes."""
    from orbitd import client

    return client.CLAIM_MARKER


def _unsigned(claims: dict) -> str:
    """A token any endpoint can mint: correct shape, no key behind it."""
    import base64

    def part(obj):
        raw = json.dumps(obj, separators=(",", ":")).encode()
        return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()

    return f"{part({'alg': 'none', 'typ': 'JWT'})}.{part(claims)}.forged"


class Forger:
    """A token endpoint that hands back whatever token it likes."""

    def __init__(self, app, token):
        self.app = app
        self.token = token

    def factory(self, headers=None):
        import httpx2

        async def handle(request: httpx2.Request) -> httpx2.Response:
            if request.url.path.endswith("/token"):
                return httpx2.Response(200, json={"access_token": self.token,
                                                  "token_type": "Bearer",
                                                  "expires_in": 3600})
            return await httpx2.ASGITransport(app=self.app).handle_async_request(request)

        return httpx2.AsyncClient(transport=httpx2.MockTransport(handle),
                                  base_url=ISSUER, headers=headers or {})


async def test_a_forged_role_is_not_stated_as_a_finding(app, state, xdg, capsys):
    """The whole defect in one case. A token nobody signed says admin; the desk must not
    tell the operator they are an admin."""
    import time

    forged = _unsigned({"sub": "ci-reader", "role": "admin",
                        "exp": int(time.time()) + 3600})
    secret = state.store.add_principal("ci-reader", Role.viewer, 3600)
    await sign_in_with_hint(Forger(app, forged).factory, MCP_URL, "ci-reader", secret,
                            ISSUER + "/token")
    out, _ = capsys.readouterr()
    role_lines = [line for line in out.splitlines() if "admin" in line]
    assert role_lines, "the sign-in said nothing about the role at all"
    for line in role_lines:
        assert claimed_role_marker() in line, (
            "the desk stated a forged role as a fact: " + repr(line))


async def test_a_real_role_is_stated_the_same_way(app, state, xdg, capsys):
    """The desk cannot tell a true claim from a forged one, so it must not say them
    differently. A message whose shape changes with the truth of the claim would be a
    verification the desk is not performing."""
    secret = state.store.add_principal("ci-reader", Role.viewer, 3600)
    await sign_in_with_hint(Recorder(app).factory, MCP_URL, "ci-reader", secret,
                            ISSUER + "/token")
    out, _ = capsys.readouterr()
    role_lines = [line for line in out.splitlines() if "viewer" in line]
    assert role_lines, "an ordinary sign-in stopped telling the operator the role"
    for line in role_lines:
        assert claimed_role_marker() in line


async def test_the_operator_is_still_told_which_role(app, state, xdg, capsys):
    """Anti-vacuity. Deleting the role from the sign-in entirely would pass the two cases
    above, and would be a worse desk."""
    secret = state.store.add_principal("ci-reader", Role.viewer, 3600)
    await sign_in_with_hint(Recorder(app).factory, MCP_URL, "ci-reader", secret,
                            ISSUER + "/token")
    out, _ = capsys.readouterr()
    assert "viewer" in out, "the sign-in no longer tells the operator anything about role"


async def test_the_expiry_is_a_claim_too(app, state, xdg, capsys):
    """The same decoding reads the expiry, and a forged token can claim any. The desk
    prints it in the same breath as the role, so it carries the same weight to a reader."""
    import time

    far = int(time.time()) + 365 * 24 * 3600
    forged = _unsigned({"sub": "ci-reader", "role": "viewer", "exp": far})
    secret = state.store.add_principal("ci-reader", Role.viewer, 3600)
    await sign_in_with_hint(Forger(app, forged).factory, MCP_URL, "ci-reader", secret,
                            ISSUER + "/token")
    out, _ = capsys.readouterr()
    expiry_lines = [line for line in out.splitlines() if "expires" in line]
    assert expiry_lines, "the sign-in said nothing about expiry"
    for line in expiry_lines:
        assert claimed_role_marker() in line, (
            "the desk stated a forged expiry as a fact: " + repr(line))


async def test_the_stored_session_does_not_present_the_claim_as_established(
        app, state, xdg, capsys):
    """A later session reads the file, not the screen. The file must carry the same
    distinction, or the next reader loses it."""
    import time

    forged = _unsigned({"sub": "ci-reader", "role": "admin",
                        "exp": int(time.time()) + 3600})
    secret = state.store.add_principal("ci-reader", Role.viewer, 3600)
    await sign_in_with_hint(Forger(app, forged).factory, MCP_URL, "ci-reader", secret,
                            ISSUER + "/token")
    record = json.loads(_session_file().read_text())
    assert "role" not in record, (
        "the session file records a bare role, which the next reader takes as established")
    assert any("admin" in str(v) for v in record.values()), (
        "the claim is gone entirely rather than recorded as a claim")


async def test_the_stored_expiry_is_a_claim_in_the_file_too(app, state, xdg, capsys):
    """The screen marks the expiry as a claim; the file must too. A later session reads the
    file, and a bare key beside a marked one teaches the next reader that the marking is
    decoration. A forged token can claim a year."""
    import time

    far = int(time.time()) + 365 * 24 * 3600
    forged = _unsigned({"sub": "ci-reader", "role": "viewer", "exp": far})
    secret = state.store.add_principal("ci-reader", Role.viewer, 3600)
    await sign_in_with_hint(Forger(app, forged).factory, MCP_URL, "ci-reader", secret,
                            ISSUER + "/token")
    record = json.loads(_session_file().read_text())
    assert "expires_at" not in record, (
        "the session file records a bare expiry, which the next reader takes as established")
    assert any(claimed_role_marker() in key for key in record), (
        "the file marks nothing as a claim at all")
