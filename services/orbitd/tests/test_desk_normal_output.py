"""What the desk writes to the stream a person actually reads.

WHY THIS FILE EXISTS. Outside text reaching an output whose separators were not neutralised
has now happened four times in this component. Round four wrapped the error stream and left
the normal stream bare, on the argument that the normal stream carries machine-readable
documents and listings where line breaks are load-bearing, and on the claim that every
host-derived string the desk echoes goes to the error stream. A reviewer broke the claim by
running the desk: the role printed after signing in, the tool listing, and the summary of a
tool's result all put host-chosen text on the normal stream, raw.

THE DESIGN THIS HOLDS. Safety on the normal stream stops being the exception and becomes the
default. Both streams are rendered; the one thing that is not rendered is a machine-readable
document, and writing one is an explicit, narrow act rather than an assumption. A path that
has never heard of this rule is then safe, and the unsafe path is the one you have to ask
for -- which is the opposite of the arrangement that failed four times.

TWO THINGS THE SAME REVIEW FOUND, HELD HERE TOO. The escape notation was not reversible,
because a backslash passed through unescaped: a host that types the two characters
backslash-n was indistinguishable from a host that sent a real line break. And the length
bound was per write rather than per message, so a host's value split across many writes was
not bounded at all.

Run under pytest from services/orbitd.
"""

from __future__ import annotations

import io
import json
import sys

import pytest
from conftest import MCP_URL
from orbitd.roles import Role
from test_login_address import ISSUER, Recorder, sign_in

pytestmark = pytest.mark.anyio


@pytest.fixture
def xdg(tmp_path, monkeypatch):
    """The desk's own session directory, per test, so a sign-in here touches nothing else."""
    d = tmp_path / "xdg"
    monkeypatch.setenv("XDG_CONFIG_HOME", str(d))
    return d

ESCAPES = "\x1b[2J\x1b[H"
FORGED_LINE = "\nrole: admin"
CONTROL = ("\x1b", "\r", "\x00")


class HostileTools:
    """The real server, with every tool name and description rewritten on the way out.

    The protocol stays exactly what the desk expects; only the text the host chose is
    hostile, which is the only thing a host actually controls.
    """

    def __init__(self, app, text):
        self.app = app
        self.text = text

    def factory(self, headers=None):
        import httpx2

        async def handle(request: httpx2.Request) -> httpx2.Response:
            response = await httpx2.ASGITransport(app=self.app).handle_async_request(request)
            await response.aread()
            body = response.content
            if b'"tools"' not in body:
                return httpx2.Response(response.status_code, content=body,
                                       headers=dict(response.headers))
            text = body.decode("utf-8", "replace")
            head, _, tail = text.partition("{")
            try:
                doc = json.loads("{" + tail)
            except json.JSONDecodeError:
                return httpx2.Response(response.status_code, content=body,
                                       headers=dict(response.headers))
            tools = doc.get("result", {}).get("tools")
            if isinstance(tools, list):
                for tool in tools:
                    tool["description"] = self.text
            patched = (head + json.dumps(doc)).encode()
            headers = {k: v for k, v in response.headers.items()
                       if k.lower() not in ("content-length",)}
            return httpx2.Response(response.status_code, content=patched, headers=headers)

        return httpx2.AsyncClient(transport=httpx2.MockTransport(handle),
                                  base_url=ISSUER, headers=headers or {})


class HostileRole:
    """A token endpoint that answers 200 with a token whose role claim it chose."""

    def __init__(self, app, role):
        self.app = app
        self.role = role

    def factory(self, headers=None):
        import base64

        import httpx2

        def seg(obj):
            return base64.urlsafe_b64encode(json.dumps(obj).encode()).decode().rstrip("=")

        token = ".".join([seg({"alg": "none", "typ": "JWT"}),
                          seg({"sub": "ci-reader", "role": self.role, "exp": 4102444800}),
                          ""])

        async def handle(request: httpx2.Request) -> httpx2.Response:
            if request.url.path.endswith("/token"):
                return httpx2.Response(200, json={"access_token": token, "token_type": "Bearer",
                                                  "expires_in": 3600})
            return await httpx2.ASGITransport(app=self.app).handle_async_request(request)

        return httpx2.AsyncClient(transport=httpx2.MockTransport(handle),
                                  base_url=ISSUER, headers=headers or {})


def assert_one_safe_line(out: str, where: str) -> None:
    for char in CONTROL:
        assert char not in out, f"{where}: a raw {char!r} reached the stream a person reads"
    assert "role: admin" not in out.split("\n", 1)[-1] or out.count("role:") <= 1, (
        f"{where}: a host wrote a line of its own: {out!r}")


class TestTheNormalStreamIsRenderedByDefault:
    async def test_the_role_a_host_claims_cannot_paint_the_screen(self, app, state, xdg, capsys):
        """The desk prints the role after signing in, and the role is a claim the host
        chose. It is read without verifying a signature, so a host that answers the token
        exchange at all decides this text."""
        secret = state.store.add_principal("ci-reader", Role.viewer, 3600)
        await sign_in(HostileRole(app, ESCAPES + FORGED_LINE + "x" * 5000).factory,
                      MCP_URL, "ci-reader", secret)
        out, _ = capsys.readouterr()
        assert_one_safe_line(out, "the role after signing in")
        assert len(out) < 4096, f"a host wrote {len(out)} characters to the normal stream"

    async def test_a_tool_description_cannot_paint_the_screen(self, app, state, xdg, capsys):
        from orbitd.client import async_main

        secret = state.store.add_principal("ci-reader", Role.viewer, 3600)
        hostile = HostileTools(app, "does a thing" + ESCAPES + FORGED_LINE)
        await sign_in(hostile.factory, MCP_URL, "an-admin", secret)
        capsys.readouterr()
        await async_main(["tools"], http_client_factory=hostile.factory)
        out, _ = capsys.readouterr()
        for char in CONTROL:
            assert char not in out, f"a raw {char!r} reached the stream a person reads"
        assert "\nrole: admin" not in out, "a host wrote a line of its own into the listing"

    async def test_a_listing_is_still_worth_reading(self, app, state, xdg, capsys):
        """The guard that matters most: a renderer that mangled ordinary text would pass
        every check above and make the desk useless."""
        from orbitd.client import async_main

        secret = state.store.add_principal("ci-reader", Role.viewer, 3600)
        plain = Recorder(app)
        await sign_in(plain.factory, MCP_URL, "ci-reader", secret)
        capsys.readouterr()
        await async_main(["tools"], http_client_factory=plain.factory)
        out, _ = capsys.readouterr()
        assert "orbit_deploy_status" in out
        for line in out.splitlines():
            if line.startswith("orbit_"):
                assert "\\" not in line, f"an ordinary listing line was mangled: {line!r}"


class TestMachineOutputStaysMachineReadable:
    async def test_a_document_survives_a_hostile_value_exactly(self, app, state, xdg, capsys):
        """The one thing that is not rendered. Its encoder neutralises separators by
        construction, and the wrapper must not touch it or it stops parsing."""
        from orbitd.client import async_main

        secret = state.store.add_principal("ci-reader", Role.operator, 3600)
        plain = Recorder(app)
        await sign_in(plain.factory, MCP_URL, "ci-reader", secret)
        capsys.readouterr()
        await async_main(["call", "orbit_project_list", "--json", "{}"],
                         http_client_factory=plain.factory)
        out, _ = capsys.readouterr()
        json.loads(out)


class TestTheEscapeNotationIsReversible:
    def test_a_typed_backslash_is_not_a_line_break(self):
        """A host that types the two characters backslash-n must not be indistinguishable
        from a host that sent a real line break."""
        from orbitd.client import configure_output

        a, b = io.StringIO(), io.StringIO()
        configure_output(a).write("first\\nsecond")
        configure_output(b).write("first\nsecond")
        assert a.getvalue() != b.getvalue(), (
            "a typed backslash-n and a real line break render identically, so no reader "
            "can tell what the host sent")

    def test_a_typed_escape_notation_is_not_an_escape_byte(self):
        from orbitd.client import configure_output

        a, b = io.StringIO(), io.StringIO()
        configure_output(a).write("before\\x1bafter")
        configure_output(b).write("before\x1bafter")
        assert a.getvalue() != b.getvalue()


class TestTheBoundIsOnTheMessage:
    def test_many_small_writes_of_one_value_are_bounded(self):
        """The bound was per write, so a host's value split across writes was not bounded."""
        from orbitd.client import configure_output

        stream = io.StringIO()
        wrapped = configure_output(stream)
        for _ in range(50):
            wrapped.write("y" * 2000)
        assert len(stream.getvalue()) < 16_384, (
            f"a host pushed {len(stream.getvalue())} characters through in small pieces")

    def test_a_write_reports_what_it_was_given(self):
        """A file object's write returns the count its caller handed it; anything else
        breaks a caller that checks."""
        from orbitd.client import configure_output

        wrapped = configure_output(io.StringIO())
        text = "an ordinary message"
        assert wrapped.write(text) == len(text)


# --------------------------------------------------------------------------- round seven
# A review deleted both production call sites of the failure envelope -- reverting them to
# what stood before round six -- and every test still passed. The contract held the
# document writer; production calls its caller, and the caller was exercised only with text
# no guard would ever touch. These drive the real command and read what actually left the
# error stream.


class TestTheRealFailureEnvelope:
    async def test_a_genuine_refusal_reaches_the_error_stream(self, app, state, xdg, capsys):
        """Anti-vacuity for this whole class, and the thing to establish first: the real
        server refusing a real call is what puts a document on the error stream at all."""
        from orbitd.client import async_main

        secret = state.store.add_principal("a-reader", Role.viewer, 3600)
        plain = Recorder(app)
        await sign_in(plain.factory, MCP_URL, "a-reader", secret)
        capsys.readouterr()
        code = await async_main(["call", "orbit_auth_revoke", "--json", '{"jti": "Q1w2E3r4T5y6U7i8"}'],
                                http_client_factory=plain.factory)
        out, err = capsys.readouterr()
        assert code != 0, "a refused call reported success"
        assert err.strip(), "a refusal put nothing on the error stream: " + repr(out[:200])
        json.loads(err)

    async def test_a_refusal_tells_the_operator_what_the_surface_said(
            self, app, state, xdg, capsys):
        """The surface refuses in full: a code, a sentence naming the role the tool needs
        and the role the token holds, and what to do next. The desk reads only the
        structured half, which a protocol-level refusal leaves empty, so the operator is
        handed an empty object and an exit code. Everything the surface said is thrown away
        at the last step of its journey."""
        from orbitd.client import async_main

        secret = state.store.add_principal("a-reader", Role.viewer, 3600)
        plain = Recorder(app)
        await sign_in(plain.factory, MCP_URL, "a-reader", secret)
        capsys.readouterr()
        # The refusal quotes the tool name back, and the caller chose the name: this is
        # how outside bytes get into a document the desk writes. If the envelope stops
        # going through the document writer, the rendering mangles them or splits the line.
        # A real tool this token may not call. The name must be one the surface offers:
        # appending anything to it makes it a different, unknown tool, and "no such tool"
        # is a different refusal from "not your role" -- conflating them is what made this
        # case read the wrong refusal.
        await async_main(["call", "orbit_auth_revoke",
                          "--json", json.dumps({"jti": "Q1w2E3r4T5y6U7i8" + ESCAPES})],
                         http_client_factory=plain.factory)
        _, err = capsys.readouterr()
        import base64

        document = json.loads(err)
        said = json.dumps(document)
        assert said != "{}", "the operator was told nothing at all about the refusal"
        assert "admin" in said and "viewer" in said, (
            "the refusal does not say which role the tool needs and which the token holds")

    async def test_an_unknown_tool_refusal_keeps_the_name_the_caller_chose(
            self, app, state, xdg, capsys):
        """The other refusal, and the one that carries outside bytes. A name the surface
        does not offer is refused as unknown -- not as the wrong role, which would be the
        surface guessing what a made-up tool would need -- and the name comes back so a
        reader can see what was actually asked for, and it arrives with nothing raw in it.

        The mechanism changed under this case and the property did not. The refusal used to
        travel in an authorization header, which admits no raw bytes, so the name came back
        base64-encoded. It now travels in the protocol as a document, which admits any byte,
        and the name comes back escaped instead. Either way a reader recovers exactly what
        was asked for and no control byte reaches the stream, so that is what this asks."""
        from orbitd.client import async_main

        secret = state.store.add_principal("a-reader", Role.viewer, 3600)
        plain = Recorder(app)
        await sign_in(plain.factory, MCP_URL, "a-reader", secret)
        capsys.readouterr()
        await async_main(["call", "orbit_not_a_tool" + ESCAPES + "\nrole: admin",
                          "--json", "{}"],
                         http_client_factory=plain.factory)
        _, err = capsys.readouterr()
        document = json.loads(err)
        assert document.get("code", "").lower() == "unknown_tool", (
            "a name the surface does not offer was refused as something else, which is the "
            "surface guessing what a made-up tool would need: " + repr(err[:300]))
        recovered = " ".join(str(v) for v in document.values())
        assert ESCAPES in recovered, (
            "the refusal lost the bytes the caller chose, so a reader cannot see what was "
            "actually asked for: " + repr(err[:300]))
        body = err.rstrip("\n")  # the last newline is the writer's terminator, not the caller's
        for raw in {c for c in ESCAPES + "\n" if ord(c) < 0x20}:
            assert raw not in body, (
                f"a raw control byte {raw!r} the caller chose reached the operator's "
                "stream, where a terminal acts on it: " + repr(err[:300]))
        assert err.count("\n") == 1, (
            "the refusal stopped being one line, so a reader taking a line at a time is "
            "handed half a document: " + repr(err[:300]))

    async def test_that_refusal_is_still_one_line(self, app, state, xdg, capsys):
        """It stays a machine document on the error stream: one line, whatever it carries."""
        from orbitd.client import async_main

        secret = state.store.add_principal("a-reader", Role.viewer, 3600)
        plain = Recorder(app)
        await sign_in(plain.factory, MCP_URL, "a-reader", secret)
        capsys.readouterr()
        await async_main(["call", "orbit_auth_revoke" + ESCAPES + "\nrole: admin",
                          "--json", json.dumps({"jti": "Q1w2E3r4T5y6U7i8"})],
                         http_client_factory=plain.factory)
        _, err = capsys.readouterr()
        assert err.count("\n") == 1, (
            "a machine document on the error stream stopped being one line: "
            + repr(err[:200]))

    async def test_an_ordinary_call_still_writes_nothing_to_the_error_stream(
            self, app, state, xdg, capsys):
        """Anti-vacuity: an envelope that wrote the refusal on every call, refused or not,
        would pass the cases above and ruin every ordinary use."""
        from orbitd.client import async_main

        secret = state.store.add_principal("a-reader", Role.viewer, 3600)
        plain = Recorder(app)
        await sign_in(plain.factory, MCP_URL, "a-reader", secret)
        capsys.readouterr()
        code = await async_main(["call", "orbit_project_list", "--json", "{}"],
                                http_client_factory=plain.factory)
        _, err = capsys.readouterr()
        assert code == 0 and not err.strip(), (
            "an ordinary call wrote to the error stream: " + repr(err[:200]))


# ============================================================== the desk has one way out
# Three reviews in a row found the same thing on this node: a contract that could not see
# its own subject. The last one proved it exactly -- four cases named for the failure
# envelope never reached the failure envelope once, because every one of them drove a
# refusal that leaves by a different exit, and the stream looked the same either way.
#
# That is not three careless contracts. One command has eight ways to write its outcome:
# five sentences, three documents, and the sign-in failure has its own path beside them. A
# contract inspects a stream, so it cannot tell which of the eight ran, and a repair can
# move between them without any case noticing. The front's rule is that the same class
# found twice means the design is wrong, so this round changes the design rather than
# writing a fourth contract against the same shape.
#
# One way out. The command decides an outcome and one writer renders it: a machine document
# when the caller asked for machine output, a sentence otherwise. Then there is one place to
# hold, one place to guard, and a case that asks for the failure document always reaches the
# failure document, because there is nowhere else for it to come from.


FAILURES = (
    # (argv, what goes wrong, a word the document must carry)
    (["call", "orbit_auth_revoke", "--json", '{"jti": "Q1w2E3r4T5y6U7i8"}'], "the role", "role"),
    (["call", "orbit_not_a_tool_at_all", "--json", "{}"], "no such tool", "tool"),
    (["call", "orbit_project_list", "--json", "{not json"], "bad arguments", "json"),
)


class TestEveryFailureLeavesOneMachineDocument:
    """Whatever goes wrong, a caller that asked for machine output gets machine output. A
    reader that must parse a sentence for one kind of failure and a document for another
    has to guess which it is holding, and the desk is the only thing that knows."""

    @pytest.mark.parametrize("argv,what,word", FAILURES)
    async def test_the_failure_is_a_document(self, app, state, xdg, capsys, argv, what, word):
        from orbitd.client import async_main

        secret = state.store.add_principal("a-reader", Role.viewer, 3600)
        plain = Recorder(app)
        await sign_in(plain.factory, MCP_URL, "a-reader", secret)
        capsys.readouterr()
        code = await async_main(argv, http_client_factory=plain.factory)
        out, err = capsys.readouterr()
        assert code != 0, f"{what} reported success"
        assert err.strip(), f"{what} put nothing on the error stream"
        document = json.loads(err)  # the point: it parses, whatever went wrong
        assert word in json.dumps(document).lower(), (
            f"the document for {what} does not say what went wrong: " + repr(err[:300]))

    @pytest.mark.parametrize("argv,what,word", FAILURES)
    async def test_that_document_is_one_line(self, app, state, xdg, capsys, argv, what, word):
        from orbitd.client import async_main

        secret = state.store.add_principal("a-reader", Role.viewer, 3600)
        plain = Recorder(app)
        await sign_in(plain.factory, MCP_URL, "a-reader", secret)
        capsys.readouterr()
        await async_main(argv, http_client_factory=plain.factory)
        _, err = capsys.readouterr()
        assert err.count("\n") == 1, (
            f"the document for {what} is not one line: " + repr(err[:200]))

    async def test_a_sign_in_failure_is_a_document_too(self, app, state, xdg, capsys):
        """The exit that has always been separate. A caller whose token is refused is in
        exactly the position of a caller whose tool is refused."""
        from orbitd.client import async_main

        secret = state.store.add_principal("a-reader", Role.viewer, 3600)
        plain = Recorder(app)
        await sign_in(plain.factory, MCP_URL, "a-reader", secret)
        from orbitd.client import canonical_mcp_url, session_path

        path = session_path(canonical_mcp_url(MCP_URL))
        record = json.loads(path.read_text())
        record["token"] = "not-a-token"
        path.write_text(json.dumps(record))
        capsys.readouterr()
        code = await async_main(["call", "orbit_project_list", "--json", "{}"],
                                http_client_factory=plain.factory)
        _, err = capsys.readouterr()
        assert code != 0
        json.loads(err)

    async def test_an_ordinary_call_still_writes_only_its_answer(self, app, state, xdg, capsys):
        """Anti-vacuity: a desk that wrote a document on every command would pass all of
        the above and ruin the ordinary case."""
        from orbitd.client import async_main

        secret = state.store.add_principal("a-reader", Role.viewer, 3600)
        plain = Recorder(app)
        await sign_in(plain.factory, MCP_URL, "a-reader", secret)
        capsys.readouterr()
        code = await async_main(["call", "orbit_project_list", "--json", "{}"],
                                http_client_factory=plain.factory)
        out, err = capsys.readouterr()
        assert code == 0 and not err.strip(), "an ordinary call wrote to the error stream"
        json.loads(out)


class TestWithoutMachineOutputTheFailureIsASentence:
    """The other half of one exit: the writer decides by what the caller asked for, not by
    which failure happened. A person who did not ask for machine output never gets one."""

    async def test_a_refusal_reads_as_a_sentence(self, app, state, xdg, capsys):
        from orbitd.client import async_main

        secret = state.store.add_principal("a-reader", Role.viewer, 3600)
        plain = Recorder(app)
        await sign_in(plain.factory, MCP_URL, "a-reader", secret)
        capsys.readouterr()
        await async_main(["call", "orbit_auth_revoke",
                          "--arg", "jti=Q1w2E3r4T5y6U7i8"],
                         http_client_factory=plain.factory)
        _, err = capsys.readouterr()
        assert err.strip(), "a refusal said nothing at all"
        try:
            json.loads(err)
        except ValueError:
            return
        raise AssertionError("a caller who did not ask for machine output got one: "
                             + repr(err[:200]))
