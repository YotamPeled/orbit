"""Did anything reach the operator that the writer did not produce?

This node has now lost five contracts to the same failure: each one could see the leak it
was written for and went blind to the next. A scan for callers of one private helper saw
one door. A scan for two spellings of writing saw those two. A scan that worked out which
functions decide an exit code matched exactly one function -- the entry point, which was on
its own allowlist -- so the set it asserted to be empty was empty by construction, and it
had found four real exits one commit earlier.

Every one of those failures is the same mistake: asking what the source looks like. The
answer keeps depending on how the code happens to be spelled, and the spelling keeps
changing -- a variable instead of a literal, an await in front of a call, an outcome object
instead of a code.

So this asks the question the invariant is actually about, and asks it of the running
program. Everything a command puts in front of an operator is captured at the file
descriptors -- below the desk's own stream wrappers, below sys.__stderr__, below anything a
future repair might reach for -- and split in two: what arrived while the writer was
running, and what arrived at any other time. The second must be empty. A second exit is
then visible whatever it is made of, because the operator saw its text.
"""

from __future__ import annotations

import io
import json
import sys

import httpx2
import pytest
from conftest import ISSUER, Role

pytestmark = pytest.mark.anyio

BAD_CREDENTIALS = json.dumps({"client_id": "nobody", "client_secret": "wrong"})


class Watch:
    """Splits everything that reaches the descriptors into what the writer produced and
    what something else did."""

    def __init__(self, capfd, monkeypatch):
        from orbitd import client as client_mod

        self.capfd = capfd
        self.outside: list[str] = []
        self.inside: list[str] = []
        real = client_mod.write_outcome

        def wrapper(outcome, *, machine):
            self.outside.append(self._drain())
            try:
                return real(outcome, machine=machine)
            finally:
                self.inside.append(self._drain())

        monkeypatch.setattr(client_mod, "write_outcome", wrapper)

    def _drain(self) -> str:
        for stream in (sys.stdout, sys.stderr, sys.__stdout__, sys.__stderr__):
            try:
                stream.flush()
            except (ValueError, AttributeError):
                pass
        captured = self.capfd.readouterr()
        return captured.out + captured.err

    def leaked(self) -> str:
        self.outside.append(self._drain())
        return "".join(self.outside)

    def written(self) -> str:
        return "".join(self.inside)


@pytest.fixture
def desk(app, state, tmp_path, monkeypatch, capfd):
    """A desk pointed at the real server, with a watch on everything it writes."""
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))

    def make(headers=None):
        return httpx2.AsyncClient(transport=httpx2.ASGITransport(app=app),
                                  base_url=ISSUER, headers=headers or {})

    class Desk:
        factory = staticmethod(make)
        url = ISSUER + "/mcp"

        def __init__(self):
            self.watch = None

        async def run(self, argv, stdin=""):
            from orbitd import client as client_mod

            return await client_mod.async_main(argv, stdin=io.StringIO(stdin),
                                               http_client_factory=make)

        async def sign_in(self, name="a-reader", role=Role.viewer):
            secret = state.store.add_principal(name, role, 3600)
            creds = json.dumps({"client_id": name, "client_secret": secret})
            code = await self.run(["login", "--url", self.url], stdin=creds)
            assert code == 0, "the fixture could not sign in"
            capfd.readouterr()

        def watching(self):
            self.watch = Watch(capfd, monkeypatch)
            return self.watch

    return Desk()


# Every command this round brought into the shape, and a failure each one really has.
# The platform and secrets command groups are deliberately not here: they live in sibling
# modules, have exit codes of their own, and are a node of their own. This file is about
# the three commands the round changed.
FAILURES = (
    ("call", ["call", "orbit_not_a_tool_here"], "a tool the surface does not offer"),
    ("call", ["call", "orbit_auth_revoke", "--arg", "jti=Q1w2E3r4T5y6U7i8"],
     "a tool above the caller's role"),
    ("call", ["call", "orbit_project_list", "--json", "{not json"], "arguments that are not JSON"),
    ("call", ["call", "orbit_project_list", "--url", "http://127.0.0.1:9/mcp"],
     "no stored session for that surface"),
    ("tools", ["tools", "--url", "http://127.0.0.1:9/mcp"], "no stored session for that surface"),
    ("tools", ["tools"], "a session whose token no longer works"),
    ("login", ["login", "--url", "http://127.0.0.1:9/mcp"], "a surface that is not there"),
    ("login", ["login", "--url", ISSUER + "/mcp"], "credentials the surface refuses"),
)


async def _break_the_session(desk):
    """Leave a stored session in place with a token the surface will not accept."""
    from orbitd.client import canonical_mcp_url, session_path

    path = session_path(canonical_mcp_url(desk.url))
    record = json.loads(path.read_text())
    record["token"] = "not-a-token"
    path.write_text(json.dumps(record))


@pytest.mark.parametrize("command,argv,what", FAILURES,
                         ids=[f"{c}: {w}" for c, _, w in FAILURES])
class TestNothingReachesTheOperatorExceptThroughTheWriter:
    """The invariant, asked of the running program rather than of the source."""

    async def test_the_failure_is_written_only_by_the_writer(self, desk, command, argv,
                                                             what):
        await desk.sign_in()
        if what == "a session whose token no longer works":
            await _break_the_session(desk)
        watch = desk.watching()
        code = await desk.run(argv, stdin=BAD_CREDENTIALS if command == "login" else "")
        leaked = watch.leaked()
        assert code != 0, f"{what} reported success"
        assert not leaked.strip(), (
            f"{what} put text in front of the operator that the one writer did not "
            f"produce, so its form was decided by which failure happened: {leaked[:400]!r}")

    async def test_the_writer_actually_wrote_something(self, desk, command, argv, what):
        """Anti-vacuity, and the failure mode that made two earlier cases meaningless: a
        command that wrote nothing at all would satisfy the case above."""
        await desk.sign_in()
        if what == "a session whose token no longer works":
            await _break_the_session(desk)
        watch = desk.watching()
        await desk.run(argv, stdin=BAD_CREDENTIALS if command == "login" else "")
        watch.leaked()
        assert watch.written().strip(), (
            f"{what} left the operator with nothing at all")


class TestTheFormFollowsWhatTheCallerAskedFor:
    """The other half of the writer's rule, held by its effect rather than by a flag being
    present in a help listing. A review found the flag was advertised on two commands and
    nothing drove either of them, so a desk that accepted the option and discarded it was
    fully green."""

    @pytest.mark.parametrize("argv", [
        ["tools", "--url", "http://127.0.0.1:9/mcp"],
        ["call", "orbit_not_a_tool_here"],
    ], ids=["tools", "call"])
    async def test_asking_for_machine_output_gets_a_document(self, desk, capfd, argv):
        await desk.sign_in()
        capfd.readouterr()
        await desk.run(argv + ["--json"] if argv[0] == "tools" else argv + ["--json", "{}"])
        captured = capfd.readouterr()
        stream = (captured.err or captured.out).strip()
        assert stream, "the command asked for machine output and wrote nothing"
        json.loads(stream.splitlines()[-1])

    @pytest.mark.parametrize("argv", [
        ["tools", "--url", "http://127.0.0.1:9/mcp"],
        ["call", "orbit_not_a_tool_here", "--arg", "a=b"],
    ], ids=["tools", "call"])
    async def test_not_asking_gets_a_sentence(self, desk, capfd, argv):
        """Anti-vacuity: a desk that answered with a document whatever was asked would pass
        the case above and hand a person a document to read."""
        await desk.sign_in()
        capfd.readouterr()
        await desk.run(argv)
        captured = capfd.readouterr()
        stream = (captured.err or captured.out).strip()
        assert stream, "the command wrote nothing at all"
        try:
            json.loads(stream)
        except ValueError:
            return
        raise AssertionError(
            "a caller who did not ask for machine output was handed one: " + stream[:300])


class TestAMachineCallerIsNotToldLess:
    """A review found the inversion: when the surface refuses with no JSON body and a
    challenge that names no scope, the machine document is the empty object, while the
    person reading a sentence is told the status and the challenge. The caller that asked
    for the parseable answer is the one that cannot tell what happened."""

    async def test_the_document_says_at_least_what_the_sentence_says(self, desk, capfd,
                                                                     monkeypatch):
        from orbitd import client as client_mod

        def refusing(headers=None):
            async def handle(request):
                return httpx2.Response(401, content=b"not json at all",
                                       headers={"www-authenticate": "Bearer"})

            return httpx2.AsyncClient(transport=httpx2.MockTransport(handle),
                                      base_url=ISSUER)

        await desk.sign_in()
        capfd.readouterr()
        await client_mod.async_main(["call", "orbit_project_list"], stdin=io.StringIO(""),
                                    http_client_factory=refusing)
        sentence = (capfd.readouterr().err or "").strip()
        await client_mod.async_main(["call", "orbit_project_list", "--json", "{}"],
                                    stdin=io.StringIO(""), http_client_factory=refusing)
        document = (capfd.readouterr().err or "").strip()
        assert sentence, "the human form said nothing either"
        assert document not in ("{}", ""), (
            "the machine caller was handed an empty document while the person reading a "
            f"sentence was told {sentence[:200]!r}")
