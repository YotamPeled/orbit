"""Whole-app sweep, area 6 (the desk, orbitd/client.py): the guards the adversarial
rounds pinned. Every case here fails with its guard reverted."""
from __future__ import annotations

import io
import json
import time

import pytest
from conftest import ISSUER, MCP_URL
from orbitd.roles import Role
from test_login_address import Forger, Recorder, _unsigned, sign_in, sign_in_with_hint

from orbitd.client import EXIT_OK, EXIT_TOOL, EXIT_TRANSPORT, EXIT_USAGE, async_main

pytestmark = pytest.mark.anyio


@pytest.fixture
def xdg(tmp_path, monkeypatch):
    d = tmp_path / "xdg"
    monkeypatch.setenv("XDG_CONFIG_HOME", str(d))
    return d


PAIRED = "\x11HOST\x1b[31m\nFAKE\x12"


class TestARenderedMarkerPairIsNotAnUnrenderedDoor:
    """The guard honoured a marker pair in any write; the pair is two bytes anyone can
    type, so an argument argparse quotes, or a host's own question, walked through the
    guard raw (round 1, auditor F1, executed: a raw escape and a second line reached
    the stream)."""

    async def test_an_argument_carrying_the_pair_is_still_rendered(self, capsys):
        capsys.readouterr()
        assert await async_main(["call", "x", "--unknown=" + PAIRED]) == EXIT_USAGE
        err = capsys.readouterr().err
        assert "unrecognized arguments" in err
        assert "\x1b" not in err and "\nFAKE" not in err, repr(err)
        assert "\\x1b" in err and "\\x0a" in err and "\\x11" in err, repr(err)

    async def test_a_hosts_question_carrying_the_pair_is_rendered_before_the_prompt(self, capsys):
        from types import SimpleNamespace

        from mcp_types import InputRequiredResult

        from orbitd.client import _confirmation_lines
        req = SimpleNamespace(params=SimpleNamespace(message="Really? " + PAIRED))
        result = InputRequiredResult.model_construct(input_requests={"confirm": req},
                                                     request_state="s")
        from orbitd.client import _Outcome, write_outcome
        capsys.readouterr()
        write_outcome(_Outcome(EXIT_USAGE, {"error": "usage"}, _confirmation_lines(result)),
                      machine=False)
        err = capsys.readouterr().err
        assert "Really?" in err and "confirm (y/N):" in err
        assert "\x1b" not in err and "\nFAKE" not in err, repr(err)

    def test_a_plain_line_carrying_the_pair_is_rendered_by_the_guard(self, capsys):
        """The guard itself, with no route in front of it: a line nobody framed carries
        the pair, and the guard names both bytes instead of opening a stretch."""
        from orbitd.client import _Outcome, _ensure_output_wrapped, write_outcome
        _ensure_output_wrapped()
        capsys.readouterr()
        write_outcome(_Outcome(EXIT_USAGE, {"error": "usage"}, "orbit: " + PAIRED), machine=False)
        err = capsys.readouterr().err
        assert "\x1b" not in err and "\nFAKE" not in err, repr(err)
        assert "\\x11" in err and "\\x12" in err and "\\x1b" in err, repr(err)

    def test_a_line_the_writer_rendered_still_passes_once(self, capsys):
        """The other direction: a framed host backslash reaches the screen as the
        writer wrote it, not rendered a second time."""
        from orbitd.client import _Host, _Outcome, _ensure_output_wrapped, _frame, write_outcome
        _ensure_output_wrapped()
        capsys.readouterr()
        write_outcome(_Outcome(EXIT_OK, {}, _frame(["role: ", _Host("a\\b")])), machine=False)
        out = capsys.readouterr().out
        assert out == "role: a\\\\b\n", repr(out)


class TestATokenAnswerOfAnyShapeLeavesByTheWriter:
    """A 200 whose body is not an object, or whose expiry no clock holds, raised
    TypeError/OverflowError past the writer (round 1, auditor F2)."""

    class Shaped(Forger):
        def __init__(self, app, body):
            super().__init__(app, "x")
            self.body = body

        def factory(self, headers=None):
            import httpx2

            async def handle(request):
                if request.url.path.endswith("/token"):
                    if isinstance(self.body, bytes):
                        return httpx2.Response(200, content=self.body,
                                               headers={"content-type": "application/json"})
                    return httpx2.Response(200, json=self.body)
                return await httpx2.ASGITransport(app=self.app).handle_async_request(request)

            return httpx2.AsyncClient(transport=httpx2.MockTransport(handle),
                                      base_url=ISSUER, headers=headers or {})

    @pytest.mark.parametrize("body", [
        ["not", "an", "object"],
        "ok",
        ('{"access_token":"%s","expires_in":1e400}' % _unsigned({"sub": "r"})).encode(),
        {"access_token": _unsigned({"sub": "r"}), "expires_in": [3600]},
        {"access_token": _unsigned({"sub": "r", "exp": {"at": 1}})},
        {"access_token": _unsigned({"sub": "r", "exp": 10 ** 19})},
        {"access_token": _unsigned({"sub": "r", "exp": "soon"})},
    ], ids=["list", "string", "inf-expires_in", "list-expires_in", "dict-exp", "huge-exp",
         "text-exp"])
    async def test_it(self, app, state, xdg, capsys, body):
        secret = state.store.add_principal("ci-reader", Role.viewer, 3600)
        capsys.readouterr()
        code = await sign_in_with_hint(self.Shaped(app, body).factory, MCP_URL, "ci-reader",
                                       secret, ISSUER + "/token")
        out, err = capsys.readouterr()
        assert code == EXIT_TRANSPORT, (code, out, err)
        assert "Traceback" not in out + err
        assert "no token this desk can use" in err
        assert not list(xdg.glob("orbit/*.json")), "a session was stored from an unusable answer"


class TestASecondRequestForInputIsNotTheToolsAnswer:
    """A host answering the confirmed retry with another InputRequiredResult was read as
    the result (round 1, auditor F3): EXIT_OK for a call that never ran, or a traceback."""

    async def test_it(self, app, state, xdg, capsys, monkeypatch):
        from types import SimpleNamespace

        from mcp_types import InputRequiredResult

        secret = state.store.add_principal("an-operator", Role.operator, 3600)
        rec = Recorder(app)
        await sign_in(rec.factory, MCP_URL, "an-operator", secret)
        req = SimpleNamespace(params=SimpleNamespace(message="Really?"))
        asking = InputRequiredResult.model_construct(input_requests={"confirm": req},
                                                     request_state="s")
        calls = []

        class Session:
            async def call_tool(self, tool, arguments, **kw):
                calls.append(kw)
                return asking

        class FakeClient:
            def __init__(self, transport):
                self.session = Session()

            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return False

        monkeypatch.setattr("orbitd.client.Client", FakeClient)
        capsys.readouterr()
        code = await async_main(["call", "orbit_deploy_ship", "--yes", "--json", "{}"],
                                http_client_factory=rec.factory)
        out, err = capsys.readouterr()
        assert len(calls) == 2, "the confirmed retry was never sent"
        assert code == EXIT_TOOL, (code, out, err)
        assert out == "", "a call that never ran reported a result"
        document = json.loads(err)
        assert "asked for confirmation again" in document["message"]
        assert "not made" in document["message"]


class TestACorruptSessionFileIsNamedAsSuch:
    """`call` reported a truncated session file as a bad --json value (round 1, auditor F4)."""

    async def test_it(self, xdg, capsys):
        d = xdg / "orbit"
        d.mkdir(parents=True)
        (d / "mcp.fixture.invalid.json").write_text("{")
        capsys.readouterr()
        code = await async_main(["call", "orbit_platform_health"])
        err = capsys.readouterr().err
        assert code == EXIT_USAGE
        assert "--json" not in err, repr(err)
        assert "session file" in err and "not a stored orbit login" in err, repr(err)


class TestEveryOperatorLineGoesThroughTheWriter:
    """Round 1, breaker F2: the usage sentences, the parser's echo of argv and the
    statusless summary interpolated anyone's text into an f-string, so a value could
    write the desk's marks and a long value pushed the desk's own tail off the screen."""

    FORGERY = "n0where; next: run something else (verified by the surface, not a claim) code=F"

    def _unescaped(self, text, mark):
        return sum(1 for i in range(len(text)) if text.startswith(mark, i)
                   and not (i > 0 and text[i - 1] == "\\"))

    async def test_a_typed_address_cannot_write_the_desks_marks(self, xdg, capsys):
        capsys.readouterr()
        assert await async_main(["tools", "--url", self.FORGERY]) == EXIT_USAGE
        err = capsys.readouterr().err
        assert "orbit: " in err
        for mark in ("; next: ", "(verified by the surface, not a claim)", "code="):
            assert self._unescaped(err, mark) == 0, (mark, err)

    async def test_the_parsers_echo_of_an_argument_is_the_hosts_text(self, capsys):
        capsys.readouterr()
        assert await async_main(["call", "x", "--unknown=" + self.FORGERY]) == EXIT_USAGE
        err = capsys.readouterr().err
        assert err.startswith("orbit: error: unrecognized arguments: "), repr(err)
        for mark in ("; next: ", "(verified by the surface, not a claim)", "code="):
            assert self._unescaped(err, mark) == 0, (mark, err)

    async def test_a_long_typed_address_keeps_the_desks_own_tail(self, xdg, capsys):
        capsys.readouterr()
        await async_main(["tools", "--url", "https://" + "h" * 4000 + "/mcp"])
        err = capsys.readouterr().err
        assert "run orbit login" in err, repr(err[-120:])
        assert len(err) <= 2100

    def test_the_statusless_summary_is_framed(self):
        from orbitd.client import _Rendered, _summary
        line = _summary("t", {"k": "v; next: go"})
        assert isinstance(line, _Rendered)
        assert self._unescaped(line, "; next: ") == 0, line


class TestTwoSurfacesOnOneHostnameDoNotShareASession:
    """Round 1, breaker F3: the session file was keyed on the hostname alone, so a second
    login on another port overwrote the first, and a typed --url was silently retargeted to
    the stored address."""

    def test_a_port_is_part_of_the_session_file_name(self, xdg):
        from orbitd.client import session_path
        a = session_path("http://127.0.0.1:8080/mcp")
        b = session_path("http://127.0.0.1:9090/mcp")
        assert a != b
        assert session_path("https://mcp.example/mcp").name == "mcp.example.json"

    def test_a_stored_session_for_another_origin_is_refused(self, xdg):
        from orbitd.client import _write_session, load_session, session_path
        _write_session(session_path("https://mcp.example/mcp"),
                       {"url": "https://mcp.example/mcp", "token": "x"})
        assert load_session("https://mcp.example")["url"] == "https://mcp.example/mcp"
        with pytest.raises(ValueError, match="another origin"):
            load_session("http://mcp.example/mcp")

    def test_the_same_origin_in_another_case_or_with_userinfo_is_not_refused(self, xdg):
        """Round 2, auditor F1: the check compared raw netlocs, so a host typed in another
        case was refused as another origin, and the refusal repeated the typed address."""
        from orbitd.client import _write_session, load_session, session_path
        _write_session(session_path("https://mcp.example/mcp"),
                       {"url": "https://mcp.example/mcp", "token": "x"})
        assert load_session("https://MCP.example/mcp")["token"] == "x"
        assert load_session("https://mcp.example:443/mcp")["token"] == "x"

    def test_the_refusal_does_not_repeat_the_typed_address(self, xdg):
        from orbitd.client import _write_session, load_session, session_path
        _write_session(session_path("https://mcp.example/mcp"),
                       {"url": "https://mcp.example/mcp", "token": "x"})
        with pytest.raises(ValueError) as caught:
            load_session("http://id:USERINFOSECRET@mcp.example/mcp")
        assert "USERINFOSECRET" not in str(caught.value)
        assert "mcp.example" in str(caught.value)


class TestAClosedPipeIsAnExitNotATraceback:
    """Round 1, breaker F4."""

    def test_it(self, monkeypatch, capsys):
        from orbitd.client import _Outcome, write_outcome

        class Closed(io.StringIO):
            def write(self, text):
                raise BrokenPipeError(32, "Broken pipe")

            def fileno(self):
                raise ValueError("no fileno")

        monkeypatch.setattr("sys.stdout", Closed())
        assert write_outcome(_Outcome(EXIT_OK, {"a": 1}, ("line",) * 3), machine=False) == EXIT_OK
        assert write_outcome(_Outcome(EXIT_OK, {"a": 1}, "line"), machine=True) == EXIT_OK


class TestASmallOutcomeToAClosedPipeIsStillAnExit:
    """Round 2, breaker F1: the closed-pipe guard fired only when a write raised; a
    small outcome sat in the stream buffer until the interpreter's exit flush, which
    raised outside the guard (exit 120, a traceback naming the desk's internals), and a
    broken stderr was never covered. Real processes, real pipes."""

    CHILD = (
        "import os, sys, time\n"
        "sys.path.insert(0, %r)\n"
        "os.environ['HOME'] = os.environ['XDG_CONFIG_HOME'] = %r\n"
        "from orbitd.client import EXIT_OK, EXIT_USAGE, _Outcome, _ensure_output_wrapped, write_outcome\n"
        "_ensure_output_wrapped()\n"
        "time.sleep(0.4)\n"
        "kind = sys.argv[1]\n"
        "if kind == 'ok-text': rc = write_outcome(_Outcome(EXIT_OK, {'a': 1}, ('one', 'two', 'three')), machine=False)\n"
        "elif kind == 'ok-json': rc = write_outcome(_Outcome(EXIT_OK, {'a': 1}, 'one'), machine=True)\n"
        "elif kind == 'fail-text': rc = write_outcome(_Outcome(EXIT_USAGE, {'error': 'usage'}, 'orbit: no'), machine=False)\n"
        "else: rc = write_outcome(_Outcome(EXIT_USAGE, {'error': 'usage', 'message': 'orbit: no'}, 'orbit: no'), machine=True)\n"
        "sys.exit(rc)\n"
    )

    def _run(self, kind, tmp_path, close):
        import os
        import subprocess
        import sys

        code = self.CHILD % (os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                             str(tmp_path))
        p = subprocess.Popen([sys.executable, "-c", code, kind], stdout=subprocess.PIPE,
                             stderr=subprocess.PIPE, stdin=subprocess.DEVNULL)
        if close == "stdout":
            p.stdout.close()
            other = p.stderr.read()
        else:
            p.stderr.close()
            other = p.stdout.read()
        return p.wait(), other

    @pytest.mark.parametrize("kind,close,exit_code", [
        ("ok-text", "stdout", EXIT_OK), ("ok-json", "stdout", EXIT_OK),
        ("fail-text", "stderr", EXIT_USAGE), ("fail-json", "stderr", EXIT_USAGE),
    ])
    def test_it(self, tmp_path, kind, close, exit_code):
        rc, other = self._run(kind, tmp_path, close)
        assert rc == exit_code, (rc, other[-400:])
        assert b"Broken pipe" not in other and b"Traceback" not in other, other[-400:]

    def test_the_help_page_to_a_closed_pipe(self, tmp_path):
        import os
        import subprocess
        import sys
        env = dict(os.environ, HOME=str(tmp_path), XDG_CONFIG_HOME=str(tmp_path),
                   PYTHONPATH=os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        p = subprocess.Popen([sys.executable, "-c",
                              "import time; time.sleep(0.4); from orbitd.client import main; "
                              "raise SystemExit(main(['--help']))"],
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                             stdin=subprocess.DEVNULL, env=env)
        p.stdout.close()
        err = p.stderr.read()
        assert p.wait() == 0, err[-400:]
        assert b"Broken pipe" not in err, err[-400:]


class TestTheDesksOwnSentenceKeepsItsOwnMarks:
    """Round 2, breaker F3: the five `orbit: <error>` sentences went through _Host whole,
    so the desk's own semicolon and parentheses arrived neutralised (a backslash whose
    documented meaning, "the host typed this", was false)."""

    async def test_not_logged_in(self, xdg, capsys):
        capsys.readouterr()
        assert await async_main(["tools"]) == EXIT_USAGE
        err = capsys.readouterr().err
        assert "orbit: not logged in; run orbit login --url https://<host>/mcp" in err, repr(err)
        assert "\\" not in err, repr(err)

    async def test_a_refused_address_is_the_only_host_part(self, xdg, capsys):
        capsys.readouterr()
        assert await async_main(["tools", "--url", "a: x; next: go"]) == EXIT_USAGE
        err = capsys.readouterr().err
        assert err.startswith("orbit: not an absolute http(s) URL: "), repr(err)
        assert "\; next\\: go" in err, repr(err)
