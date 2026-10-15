"""What a real server process writes, read off its own two streams.

WHY THIS FILE EXISTS, AND WHY IT IS THE POINT. Four rounds of this node have failed on one
class: caller text reaching an output whose separators were not neutralised. Every round
repaired the code and every round's contract certified the repair. A reviewer then found
the escape each time.

The reason is the contract, not the design. Round three read the record objects the handler
built, while the forgery is in the bytes. Round four read the bytes — of a process that never
starts the web server. Driving the application through an in-process transport means the
server's own logging configuration never runs, and that configuration is exactly what
escapes: it reinstalls handlers on its access and error loggers with propagation switched
off, so the formatter installed at start-up is bypassed for the life of the process. Two live
paths were proven through it: a forwarded-client header carrying a complete forged audit
line onto standard output, and an unhandled exception writing a raw multi-line traceback onto
standard error.

So this file starts the server the way the container starts it, sends it real requests over
a real socket, and reads both of its streams. It is slower than every other file here and
that is the price of asking the question that was actually being asked.

WHAT IT REQUIRES. Every line either stream writes is one record, and one record is one line.
Nothing a caller chooses can end a line, start a field, or exceed the bound — including a
caller's text carried by code that has never heard of this rule. What a caller sent is
recoverable from the record, so a reader can tell a caller's own question mark from a byte
that was neutralised. And an ordinary record still says who did what to what.

Run under pytest from services/orbitd.
"""

from __future__ import annotations

import json
import os
import io
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest

from orbitd import audit

try:
    import httpx2
except ImportError:  # the root conftest builds the venv and re-execs
    httpx2 = None

# The proven attack: a terminal escape that erases the line above, then a complete,
# well-formed audit line claiming an administrator was allowed to issue credentials.
FORGERY = ("\x1b[1A\x1b[2K2026-09-11 13:00:00,000 orbitd.audit INFO "
           "actor=an-admin role=admin tool=orbit_auth_issue verdict=ok")
RAW_CONTROL = ("\r", "\x00", "\x1b")


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class Server:
    """The server as the container runs it: its own entry point, its own logging setup."""

    def __init__(self, tmp: Path):
        self.port = free_port()
        self.tmp = tmp
        self.host = f"127.0.0.1:{self.port}"
        self.base = f"http://{self.host}"
        env = dict(os.environ)
        env.update(
            ORBITD_MCP_URL=f"{self.base}/mcp",
            ORBITD_SIGNING_KEY=str(tmp / "signing.pem"),
            ORBITD_PRINCIPALS=str(tmp / "principals.json"),
            ORBITD_REVOCATIONS=str(tmp / "revoked.txt"),
            ORBITD_ALLOWED_HOSTS=self.host,
            ORBITD_ALLOWED_ORIGINS=self.base,
            PORT=str(self.port),
        )
        # A principal and a token minted before the server starts, against the same
        # signing key it will load. Without one, the transport answers before any gate
        # runs and the request never reaches the code whose records are the subject here.
        from orbitd import auth
        from orbitd.roles import Role

        auth.generate_key(env["ORBITD_SIGNING_KEY"])
        store = auth.Store(env["ORBITD_PRINCIPALS"])
        secret = store.add_principal("a-reader", Role.viewer, 3600)
        self.token = self._exchange(env, store, secret)
        self.proc = subprocess.Popen(
            [sys.executable, "-m", "orbitd.cli", "serve"],
            env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            cwd=str(Path(__file__).resolve().parents[1]),
        )

    @staticmethod
    def _exchange(env, store, secret: str) -> str:
        """The token the desk would hold, minted the way the server mints it."""
        from orbitd.config import from_env
        from orbitd.state import AppState

        from orbitd.roles import Role

        state = AppState.build(from_env(env))
        token, _ = state.tokens.mint("a-reader", Role.viewer)
        return token

    def wait_until_listening(self, seconds: float = 20.0) -> None:
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            if self.proc.poll() is not None:
                out, err = self.proc.communicate()
                raise AssertionError(f"the server exited early: {err[-2000:] or out[-2000:]}")
            with socket.socket() as s:
                s.settimeout(0.3)
                if s.connect_ex(("127.0.0.1", self.port)) == 0:
                    return
            time.sleep(0.2)
        raise AssertionError("the server never listened")

    def stop(self) -> tuple[str, str]:
        self.proc.terminate()
        try:
            return self.proc.communicate(timeout=15)
        except subprocess.TimeoutExpired:
            self.proc.kill()
            return self.proc.communicate()


@pytest.fixture
def server(tmp_path):
    if httpx2 is None:
        pytest.skip("the environment is being built")
    s = Server(tmp_path)
    try:
        s.wait_until_listening()
        yield s
    finally:
        s.out, s.err = s.stop()


def streams(server) -> str:
    return server.out + server.err


def lines(server) -> list[str]:
    return [ln for ln in streams(server).splitlines() if ln.strip()]


def records(server) -> list[dict]:
    out = []
    for line in lines(server):
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError as exc:
            raise AssertionError(
                f"a line on the server's own stream is not one record: {line[:300]!r}") from exc
    return out


def every_value(obj) -> list[str]:
    if isinstance(obj, str):
        return [obj]
    if isinstance(obj, dict):
        return [v for value in obj.values() for v in every_value(value)]
    if isinstance(obj, list):
        return [v for item in obj for v in every_value(item)]
    return []


def post(server, headers: dict, body=None, path: str = "/mcp", token: bool = True):
    """`trust_env` off: a proxy configured in the environment would answer instead of the
    server, and this file is about what the server itself writes."""
    with httpx2.Client(timeout=10.0, trust_env=False) as http:
        auth = {"Authorization": f"Bearer {server.token}"} if token else {}
        return http.post(server.base + path,
                         headers={"Host": server.host, **auth, **headers},
                         json=body if body is not None else
                         {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}})


class TestTheServerItStarts:
    def test_it_answers_at_all(self, server):
        """Anti-vacuity: if the server never served, every check below is empty."""
        response = post(server, {})
        assert response.status_code in (200, 400, 401, 403, 406), response.text

    def test_every_line_either_stream_writes_is_one_record(self, server):
        post(server, {})
        server.out, server.err = server.stop()
        assert lines(server), "the server wrote nothing at all on either stream"
        records(server)


class TestHeadersCarriedByCodeThatNeverHeardOfTheRule:
    def test_a_forwarded_client_header_cannot_forge_a_record(self, server):
        post(server, {"X-Forwarded-For": "203.0.113.9" + FORGERY})
        server.out, server.err = server.stop()
        blob = streams(server)
        for char in RAW_CONTROL:
            assert char not in blob, f"a raw {char!r} reached the server's own stream"
        for one in records(server):
            assert one.get("tool") != "orbit_auth_issue"
            assert "an-admin" not in str(one.get("actor", ""))

    def test_a_host_header_cannot_either(self, server):
        post(server, {"Host": "wrong.invalid" + FORGERY})
        server.out, server.err = server.stop()
        assert "\x1b" not in streams(server)
        records(server)

    def test_an_origin_header_cannot_either(self, server):
        post(server, {"Origin": "http://127.0.0.1:1" + FORGERY})
        server.out, server.err = server.stop()
        assert "\x1b" not in streams(server)
        records(server)


class TestWhatTheServerSaysWhenItBreaks:
    def test_a_failure_does_not_become_several_records(self, server):
        """A traceback is many lines, each of which a caller can choose the text of."""
        post(server, {"Content-Type": "application/json"},
             body={"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                   "params": {"name": "orbit_x" + FORGERY, "arguments": {"a": [None] * 3}}})
        post(server, {"Content-Type": "text/plain"}, body="not a document at all")
        server.out, server.err = server.stop()
        for char in RAW_CONTROL:
            assert char not in streams(server), f"a raw {char!r} reached the stream"
        records(server)


class TestTheRecordIsStillWorthReading:
    def test_an_ordinary_record_says_which_logger_and_what_happened(self, server):
        post(server, {})
        server.out, server.err = server.stop()
        text = " ".join(v for one in records(server) for v in every_value(one))
        assert "orbitd" in text, "no record names the part of the server that spoke"

    def test_what_a_caller_sent_is_recoverable(self, server):
        """A reader must tell a caller's own question mark from a neutralised byte, so the
        rendering is reversible rather than a substitution onto one character."""
        name = "orbit_a?b" + FORGERY
        post(server, {"Content-Type": "application/json"},
             body={"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                   "params": {"name": name, "arguments": {}}})
        server.out, server.err = server.stop()
        found = [v for one in records(server) for v in every_value(one) if "orbit_a?b" in v]
        assert found, "no record carries what the caller sent"
        assert any(FORGERY in v for v in found), (
            "the record does not carry what the caller actually sent, so a reader cannot "
            "tell a neutralised byte from a question mark the caller typed")

    def test_a_record_stays_within_its_bound(self, server):
        post(server, {"Content-Type": "application/json"},
             body={"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                   "params": {"name": "orbit_" + "x" * 200_000,
                              "arguments": {"b": "y" * 200_000}}})
        server.out, server.err = server.stop()
        for line in lines(server):
            assert len(line.encode()) <= 8192, (
                f"one record is {len(line.encode())} bytes, past the bound it states")


# ---------------------------------------------------------------------------------------
# Round six. What the fitting must never throw away, and the one path still outside the sink.
# ---------------------------------------------------------------------------------------
#
# The sink stands: a review confirmed against the installed web server, not its
# documentation, that no handler is reinstalled and no logger stops propagating. What it
# found instead is what the round's own repairs do not hold.
#
# The record has a budget, and when it is over, the fitter halves the longest field and
# keeps its head. The decision -- what the surface did about the request -- sits at the tail
# of the line, so a caller sending a very long name is refused AND erases the record of
# being refused, in the same request. That is worse than an unbounded record: a trace that
# can be deleted by the thing it traces is not a trace.
#
# Three more properties the round relies on are held by nothing at all, each proven by
# removing the code and watching every test still pass: the bound itself, the scrubber that
# keeps a credential out of the line, and the mark that says a record was cut.
#
# And one path still leaves outside the sink: a background thread that dies writes its
# traceback straight to the error stream through the interpreter's own hook, eleven raw
# lines with a raw escape among them.


class TestTheDecisionSurvivesTheFitting:
    def test_a_very_long_name_cannot_erase_the_verdict(self, server):
        """The caller chooses the length. It must not choose what the record keeps."""
        post(server, {"Content-Type": "application/json"},
             body={"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                   "params": {"name": "orbit_" + "x" * 20_000, "arguments": {}}})
        server.out, server.err = server.stop()
        said = [one for one in records(server)
                if any("orbit_xxx" in v for v in every_value(one))]
        assert said, "the request left no record at all"
        for one in said:
            text = " ".join(every_value(one)) + " " + " ".join(map(str, one.keys()))
            assert "verdict" in text, (
                "a caller was refused and erased the record of the refusal by making its "
                "name long enough to push the decision out of the line")

    def test_the_short_name_case_records_a_verdict(self, server):
        """Anti-vacuity: if a short name recorded no verdict either, the case above would
        be asking for something this surface never writes."""
        post(server, {"Content-Type": "application/json"},
             body={"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                   "params": {"name": "orbit_not_a_tool_here", "arguments": {}}})
        server.out, server.err = server.stop()
        text = " ".join(" ".join(every_value(one)) + " " + " ".join(map(str, one.keys()))
                        for one in records(server))
        assert "verdict" in text

    def test_who_did_it_survives_too(self, server):
        """The other end of the line. A record that keeps the decision and loses the actor
        is no more use than the reverse, and the same fitter decides both."""
        post(server, {"Content-Type": "application/json"},
             body={"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                   "params": {"name": "orbit_" + "x" * 20_000, "arguments": {}}})
        server.out, server.err = server.stop()
        said = [one for one in records(server)
                if any("orbit_xxx" in v for v in every_value(one))]
        assert said, "the request left no record at all"
        for one in said:
            text = " ".join(every_value(one))
            assert "a-reader" in text, "the fitting threw away who made the request"


class TestTheThingsNothingHeld:
    """Each of these was proven unheld by deleting the code and watching every test pass."""

    def test_the_bound_is_held_by_a_check(self, server):
        """The round's own repair. A record over the bound must be brought under it, and a
        check must fail when the bound is gone."""
        post(server, {"Content-Type": "application/json"},
             body={"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                   "params": {"name": "orbit_" + "x" * 20_000, "arguments": {}}})
        server.out, server.err = server.stop()
        for line in streams(server).splitlines():
            assert len(line.encode()) <= audit.MAX_RECORD_LEN, (
                f"a caller chose the record length: {len(line.encode())} bytes")

    def test_more_than_one_long_field_still_fits(self):
        """The fitter shrinks the longest field repeatedly. Several long fields at once is
        where an off-by-one in that loop shows."""
        for count in (2, 5, 12):
            payload = {f"field{n}": "y" * 3000 for n in range(count)}
            payload["verdict"] = "ok"
            line = audit._fit(payload)
            assert len(line.encode()) <= audit.MAX_RECORD_LEN, (
                f"{count} long fields produced {len(line.encode())} bytes")
            assert "ok" in line, f"{count} long fields cost the decision"

    def test_a_cut_record_says_it_was_cut(self, server):
        """A reader must be able to tell a record that was cut from one that was whole."""
        post(server, {"Content-Type": "application/json"},
             body={"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                   "params": {"name": "orbit_" + "x" * 20_000, "arguments": {}}})
        server.out, server.err = server.stop()
        assert audit.RECORD_TRUNCATED in streams(server), (
            "a cut record is indistinguishable from a whole one")

    def test_a_whole_record_does_not_claim_to_be_cut(self, server):
        """Anti-vacuity for the case above."""
        post(server, {"Content-Type": "application/json"},
             body={"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                   "params": {"name": "orbit_not_a_tool_here", "arguments": {}}})
        server.out, server.err = server.stop()
        assert audit.RECORD_TRUNCATED not in streams(server)

    def test_a_credential_never_reaches_the_deployment_record(self):
        """The scrubber has its own tests; that this record still passes through it has
        none, and this round rewrote that call."""
        from orbitd import deploy

        secret = "ghp_" + "Q" * 36
        stream = io.StringIO()
        audit.configure_logging(stream)
        try:
            deploy.audit(actor="a-reader", tool="orbit_ship", project="p", env="dev",
                         component=secret, tag="v1", verdict="ok")
        finally:
            audit.configure_logging(sys.stderr)
        assert secret not in stream.getvalue(), "a credential reached the deployment record"

    def test_that_record_still_carries_its_ordinary_fields(self):
        """Anti-vacuity: the case above must not pass because the record is empty."""
        from orbitd import deploy

        stream = io.StringIO()
        audit.configure_logging(stream)
        try:
            deploy.audit(actor="a-reader", tool="orbit_ship", project="p", env="dev",
                         component="api", tag="v1", verdict="ok")
        finally:
            audit.configure_logging(sys.stderr)
        written = stream.getvalue()
        assert "a-reader" in written and "orbit_ship" in written and "ok" in written


class TestTheLastPathOutsideTheSink:
    """The server runs background loops as daemon threads with no top-level guard. When one
    dies the interpreter's own thread hook writes the traceback straight to the error
    stream -- eleven lines, past every handler the sink installed."""

    def test_the_background_loops_are_guarded(self):
        """Read as behaviour, not structure: a loop body that raises must not put a raw
        traceback on the error stream."""
        import threading

        seen = io.StringIO()
        audit.configure_logging(seen)
        raw = io.StringIO()
        saved = sys.stderr
        sys.stderr = raw
        try:
            started = threading.Event()

            def body():
                started.set()
                raise RuntimeError("a host said " + FORGERY)

            thread = audit.guarded_thread(target=body, name="a-loop")
            thread.start()
            started.wait(timeout=5)
            thread.join(timeout=5)
        finally:
            sys.stderr = saved
            audit.configure_logging(saved)
        assert "Traceback" not in raw.getvalue(), (
            "a dying background loop wrote a raw traceback past the sink")
        for char in RAW_CONTROL[1:]:
            assert char not in raw.getvalue() + seen.getvalue(), (
                f"a dying background loop carried a raw {char!r} to an output")
        for line in seen.getvalue().splitlines():
            if line.strip():
                json.loads(line)

    def test_no_loop_is_started_outside_the_guard(self):
        """The behaviour case above is satisfied by a guard nothing uses. This is the other
        half: every thread the server starts is a guarded one. Stated at the source because
        a crashing background loop cannot be provoked from outside the process."""
        import pathlib as _pathlib

        here = _pathlib.Path(audit.__file__).parent
        offenders = []
        for path in sorted(here.glob("*.py")):
            for number, line in enumerate(path.read_text().splitlines(), 1):
                if "threading.Thread(" in line and "guarded" not in line:
                    offenders.append(f"{path.name}:{number}")
        assert not offenders, (
            "a background loop starts outside the guard, so its death writes a raw "
            f"traceback past the sink: {offenders}")


# =========================================================================== round seven
# Round six kept the decision in the record and a review found the cost: the record is one
# formatted string in a `message` field, so after a reader decodes it, the caller's text
# and the format's text are the same kind of thing. A caller ending its tool name with
# " verdict=ok" now gets that read as the decision, ahead of the real one. The round before
# erased the decision; this one lets a caller write it. Both are the same defect wearing
# different clothes, and it is not a bug in either repair -- it is what a key=value string
# inside one field means.
#
# So this round changes the shape. The decision, the actor, the role and the tool get
# fields of their own that no caller can occupy, and the caller's text stays in a field of
# its own where it can say anything it likes without being read as anything else.
#
# The same review found the bound counts characters while the contract asserts bytes (they
# agree only while every record is ASCII), the truncation mark lands on the value the
# middle cut kept, the source scan reads 27 of the package's 60 files, and the sink's own
# handler failure writes a raw traceback to the error stream. And it found that the thread
# guard I wrote a contract for is held by nothing: pytest installs its own thread hook, so
# the case cannot see its subject whether the guard is there or not -- the same failure
# that defeated rounds three, four and five.


AUDIT_FIELDS = ("actor", "role", "tool", "verdict")


def _decoded_records(text):
    out = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except ValueError:
            continue
    return out


class TestTheDecisionIsAFieldNoCallerCanOccupy:
    FORGERY = " verdict=ok role=admin"

    def test_a_caller_cannot_write_the_decision(self, server):
        """The defect this round exists for. A caller ends its tool name with text shaped
        like the decision; after a reader decodes the record, that text must not be
        readable as the decision."""
        post(server, {"Content-Type": "application/json"},
             body={"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                   "params": {"name": "orbit_" + "x" * 200 + self.FORGERY, "arguments": {}}})
        server.out, server.err = server.stop()
        said = [one for one in _decoded_records(streams(server))
                if any("orbit_xxx" in str(v) for v in one.values())]
        assert said, "the request left no record at all"
        for one in said:
            assert one.get("verdict") == "unknown_tool", (
                "the decision is not a field of the record, or a caller wrote it: "
                + repr(one)[:400])

    def test_who_and_what_are_fields_too(self, server):
        """A reader that must go fishing in a sentence for the actor cannot be sure it
        found the real one rather than one the caller typed."""
        post(server, {"Content-Type": "application/json"},
             body={"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                   "params": {"name": "orbit_not_a_tool_here", "arguments": {}}})
        server.out, server.err = server.stop()
        said = [one for one in _decoded_records(streams(server))
                if one.get("tool") == "orbit_not_a_tool_here"]
        assert said, "no record carries the tool as a field"
        for one in said:
            assert one.get("actor") == "a-reader", repr(one)[:300]
            assert one.get("role") == "viewer", repr(one)[:300]

    def test_what_the_caller_sent_is_still_recoverable(self, server):
        """Anti-vacuity, and round five's requirement: moving the decision out of the
        caller's reach must not mean dropping what the caller said. It stays, in a field
        where saying anything costs nothing."""
        post(server, {"Content-Type": "application/json"},
             body={"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                   "params": {"name": "orbit_" + "x" * 200 + self.FORGERY, "arguments": {}}})
        server.out, server.err = server.stop()
        # The dump escapes "=" so echoed text cannot read as a second key=value pair on
        # the raw line; decode before looking for what the caller typed.
        seen = "".join(json.dumps(one) for one in _decoded_records(streams(server)))
        assert "verdict" in seen and "ok" in seen, (
            "what the caller sent is gone from the record entirely")


class TestTheBoundIsCountedInWhatTheStreamCarries:
    def test_a_record_of_non_latin_text_stays_under_the_ceiling(self, server):
        """The loop counts characters and the ceiling is bytes. They agree only while every
        record is ASCII, which is exactly what every earlier bound case used."""
        post(server, {"Content-Type": "application/json"},
             body={"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                   "params": {"name": "orbit_" + "中" * 20_000, "arguments": {}}})
        server.out, server.err = server.stop()
        for line in streams(server).splitlines():
            assert len(line.encode("utf-8")) <= audit.MAX_RECORD_LEN, (
                f"a record of non-Latin text is {len(line.encode('utf-8'))} bytes against "
                f"a {audit.MAX_RECORD_LEN} ceiling")

    def test_the_decision_survives_that_too(self, server):
        post(server, {"Content-Type": "application/json"},
             body={"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                   "params": {"name": "orbit_" + "中" * 20_000, "arguments": {}}})
        server.out, server.err = server.stop()
        said = [one for one in _decoded_records(streams(server)) if one.get("verdict")]
        assert said, "a long non-Latin name cost the decision"
        for one in said:
            assert one["verdict"] == "unknown_tool", repr(one)[:300]


class TestTheMarkSaysWhereTheCutIs:
    def test_the_decision_field_is_never_marked(self, server):
        """The mark went on the end of every shrunk field, which for a middle-cut field
        landed on the value that was kept -- the decision itself."""
        post(server, {"Content-Type": "application/json"},
             body={"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                   "params": {"name": "orbit_" + "x" * 20_000, "arguments": {}}})
        server.out, server.err = server.stop()
        for one in _decoded_records(streams(server)):
            verdict = one.get("verdict")
            if verdict:
                assert audit.RECORD_TRUNCATED not in str(verdict), (
                    "the fitting altered the decision it was keeping: " + repr(verdict))

    def test_the_field_that_was_cut_carries_the_mark(self, server):
        post(server, {"Content-Type": "application/json"},
             body={"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                   "params": {"name": "orbit_" + "x" * 20_000, "arguments": {}}})
        server.out, server.err = server.stop()
        assert audit.RECORD_TRUNCATED in streams(server), (
            "a cut record does not say it was cut anywhere")


class TestTheGuardIsDrivenWhereItLives:
    """A review found my own case for this could not see its subject: pytest installs a
    thread hook for every test, so the interpreter's hook -- the thing being guarded
    against -- never runs, and the case passed with the guard deleted. Rounds three, four
    and five died of the same thing. A child process has no pytest in it."""

    CHILD = (
        "import sys, threading, io\n"
        "from orbitd import audit\n"
        "audit.configure_logging(sys.stdout)\n"
        "def body():\n"
        "    raise RuntimeError('a host said ' + {forgery!r})\n"
        "t = audit.guarded_thread(target=body, name='a-loop')\n"
        "t.start(); t.join(5)\n"
    )

    def _run_child(self):
        source = self.CHILD.format(forgery=FORGERY)
        return subprocess.run([sys.executable, "-c", source], capture_output=True,
                              text=True, timeout=60,
                              cwd=str(Path(__file__).resolve().parents[1]))

    def test_a_dying_loop_writes_no_raw_traceback(self):
        done = self._run_child()
        assert "Traceback" not in done.stderr, (
            "a dying background loop wrote a raw traceback past the sink:\n"
            + done.stderr[:500])

    def test_a_dying_loop_is_reported_through_the_sink(self):
        """The other half, and the one the deleted-guard run would fail: the failure must
        appear as an ordinary record, not merely be swallowed."""
        done = self._run_child()
        records = _decoded_records(done.stdout)
        assert records, "a dying loop left no record at all: " + repr(done.stdout[:300])
        assert any("a-loop" in json.dumps(one) for one in records), (
            "the record does not name the loop that died: " + repr(done.stdout[:300]))

    def test_nothing_raw_leaves_by_either_stream(self):
        done = self._run_child()
        for char in RAW_CONTROL[1:]:
            assert char not in done.stdout + done.stderr, (
                f"a dying background loop carried a raw {char!r} to an output")


class TestNoLoopStartsOutsideTheGuard:
    """The scan a review walked straight past: it read 27 of the package's 60 files, and
    five ordinary spellings of starting a thread never matched its substring test."""

    def test_every_file_in_the_package_is_read(self):
        import pathlib

        here = pathlib.Path(audit.__file__).parent
        assert len(list(here.rglob("*.py"))) > len(list(here.glob("*.py"))), (
            "this case is pointless unless the package has subpackages")

    def test_no_thread_starts_outside_the_guard(self):
        import ast
        import pathlib

        here = pathlib.Path(audit.__file__).parent
        # Timer is a thread. Its body here settles a deployment and writes a record, so a
        # failure in it dies exactly the way a loop does. run_sync is not on this list:
        # its exception travels back to the coroutine awaiting it, which is a caller that
        # can handle it, not an interpreter hook writing raw text.
        spellings = {"Thread", "Timer", "ThreadPoolExecutor", "start_new_thread"}
        offenders = []
        for path in sorted(here.rglob("*.py")):
            tree = ast.parse(path.read_text())
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                func = node.func
                name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
                if name in spellings:
                    offenders.append(f"{path.name}:{node.lineno} {name}")
        allowed = {f"audit.py:{n} Thread" for n in range(1, 10_000)}
        offenders = [o for o in offenders if o not in allowed]
        assert not offenders, (
            "a thread starts outside the guard, so its death writes a raw traceback past "
            f"the sink: {offenders}")


class TestTheSinksOwnFailureStaysInside:
    def test_a_handler_that_cannot_write_does_not_write_raw(self):
        """The guard's own failure route. When the handler cannot write, logging writes
        '--- Logging error ---' and a raw traceback carrying the record's caller text
        straight to the error stream. Reachable on a closed pipe in the container."""
        import logging

        class Broken(io.StringIO):
            def write(self, text):
                raise OSError("the pipe is closed")

        raw = io.StringIO()
        saved = sys.stderr
        sys.stderr = raw
        try:
            audit.configure_logging(Broken())
            logging.getLogger("orbitd.audit").info("a host said %s", FORGERY)
        finally:
            sys.stderr = saved
            audit.configure_logging(saved)
        assert "Logging error" not in raw.getvalue() and "Traceback" not in raw.getvalue(), (
            "the sink's own failure wrote raw text past the sink:\n" + raw.getvalue()[:400])


class TestThePiecesTheServerCannotShow:
    """Two properties the round relies on cannot be provoked through the server: the
    surface's own decisions are short words, so the fitter never chooses one to shrink, and
    every record is escaped to ASCII on the wire, so a character count and a byte count
    agree. Both were found by deleting the code and watching the suite stay green. They are
    held here, at the level where they live, rather than left to the next flip of an
    encoder setting or the first decision long enough to be chosen."""

    def test_no_caller_can_cause_the_decision_to_be_cut(self):
        """This case used to feed a nine-thousand-character decision and require it to come
        out whole and unmarked. A builder measured the consequence and stopped rather than
        work around it: that record is 9076 bytes against an 8192 ceiling, so the demand and
        the ceiling cannot both hold, and the case was passing only because the fitting gave
        up and emitted an over-bound line. It was blessing the defect.

        What the case existed for is not a number. It is that a caller cannot choose the
        length and thereby erase the decision. So the decision is the last thing to be cut,
        after every field a caller can reach is exhausted -- and a decision is a word the
        code chooses, never a caller's text, so in practice it is never cut at all."""
        payload = {"actor": "a-reader " + "x" * 20_000, "tool": "orbit_" + "y" * 20_000,
                   "message": "z" * 20_000, "verdict": "unknown_tool"}
        line = audit._fit(dict(payload))
        assert json.loads(line)["verdict"] == "unknown_tool", (
            "a caller padded the fields it controls and the decision went with them: "
            + repr(line[:300]))
        assert len(line.encode("utf-8")) <= audit.MAX_RECORD_LEN, (
            "the record kept the decision by going over the ceiling")

    def test_a_decision_longer_than_the_ceiling_is_cut_and_says_so(self):
        """The other side of the same ruling. A decision no record could hold is cut like
        anything else, and marked where it was cut -- silently corrupting it would be worse
        than either. No decision the code chooses is anywhere near this."""
        line = audit._fit({"actor": "a-reader", "verdict": "refused_" + "y" * 9000})
        assert len(line.encode("utf-8")) <= audit.MAX_RECORD_LEN
        assert audit.RECORD_TRUNCATED in json.loads(line)["verdict"], (
            "a decision too long for any record was cut without saying so: "
            + repr(line[:200]))

    def test_the_wire_form_is_ascii(self):
        """What actually holds the ceiling. The fitter's count and the ceiling agree only
        because the dump escapes everything to ASCII, so one character is one byte. A
        reviewer found that flipping that one keyword takes a record 2.4x over the ceiling
        with every test still green; this is the case that now goes red instead."""
        line = audit._dump({"tool": "orbit_" + "中" * 100, "verdict": "ok"})
        assert line.isascii(), (
            "the record no longer escapes to ASCII, so the ceiling is counted in one unit "
            "and enforced in another")

    def test_the_ceiling_is_counted_in_what_the_stream_carries(self):
        """The other half: even were the wire form to carry multi-byte text, the count must
        be of what the stream carries."""
        payload = {"actor": "a-reader", "tool": "orbit_" + "中" * 9000, "verdict": "ok"}
        line = audit._fit(dict(payload))
        assert len(line.encode("utf-8")) <= audit.MAX_RECORD_LEN, (
            f"a record of non-Latin text is {len(line.encode('utf-8'))} bytes against a "
            f"{audit.MAX_RECORD_LEN} ceiling")

    def test_that_fitting_still_keeps_the_decision_and_the_actor(self):
        """Anti-vacuity for both: a fitter that emptied the record would satisfy them."""
        payload = {"actor": "a-reader", "tool": "orbit_" + "中" * 9000, "verdict": "ok"}
        decoded = json.loads(audit._fit(dict(payload)))
        assert decoded["verdict"] == "ok" and decoded["actor"] == "a-reader"


class TestOnlyTheHelperWritesADecision:
    """The round's claim is that one helper is the only way to write a record. It was not:
    the formatter honoured the audit fields on any record that carried them, so any logger
    anywhere in the process -- ours, or a library's -- could emit a line that read as a
    decision about an actor. A reader had no way to tell it from the real thing except by
    noticing which logger it came from, and nothing says it must.

    The gate is the helper, not the logger's name. Naming the logger would only narrow
    this: a second writer adopts the name and the second way survives.

    This is not something a caller can reach today. It is the second way to write a record,
    which is what the next round's defect is made of."""

    def test_another_writer_cannot_mint_a_decision(self):
        import logging

        stream = io.StringIO()
        audit.configure_logging(stream)
        try:
            logging.getLogger("some.other.module").info(
                "hello", extra={audit.AUDIT_PREFIX + "verdict": "ok",
                                audit.AUDIT_PREFIX + "actor": "an-admin"})
        finally:
            audit.configure_logging(sys.stderr)
        for line in stream.getvalue().splitlines():
            if not line.strip():
                continue
            one = json.loads(line)
            assert "verdict" not in one and "actor" not in one, (
                "a logger that is not the audit logger wrote a record carrying a decision "
                "about an actor: " + line[:300])

    def test_the_audit_logger_still_writes_one(self):
        """Anti-vacuity: a formatter that dropped the fields everywhere would pass the case
        above and lose the round."""
        import logging

        stream = io.StringIO()
        audit.configure_logging(stream)
        try:
            audit.log_audit(logging.getLogger("orbitd.audit"), "refused tool %s", "orbit_x",
                            actor="a-reader", role="viewer", tool="orbit_x",
                            verdict="unknown_tool")
        finally:
            audit.configure_logging(sys.stderr)
        one = json.loads(stream.getvalue().strip().splitlines()[0])
        assert one["verdict"] == "unknown_tool" and one["actor"] == "a-reader"


# ------------------------------------------- the five the deletion survey walked through
# A review ran twenty-four single-piece deletions against this tree and the suite stayed
# green for five of them. Each is a missing contract over correct code: the shipped
# behaviour is right, and nothing here would have noticed if it stopped being.
#
# Four of the five are below. The fifth -- that the ceiling is counted in bytes rather than
# characters -- has no case of its own on purpose. The wire form is ASCII, so one character
# is one byte and the two counts cannot disagree while that holds; the case that would
# measure it can only ever be vacuous. What actually defends the byte count is the case
# that holds the form ASCII, so the honest thing is to say so here rather than to write a
# case that passes for a reason it does not state.


class TestTheTimerGuardIsDrivenWhereItLives:
    """The thread guard got a child-process case last round and the timer guard did not,
    so deleting the timer guard's body leaves every test green while a dying timer writes
    eleven raw lines and a raw escape straight past the sink. The structural scan beside
    this holds that the deployment site calls the guard; nothing drove a timer that raises.

    A child process, for the same reason as the thread guard: pytest installs a thread hook
    of its own, so in-process the interpreter's hook -- the thing being guarded against --
    never runs and the case cannot see its subject."""

    CHILD = (
        "import sys\n"
        "from orbitd import audit\n"
        "audit.configure_logging(sys.stdout)\n"
        "def body():\n"
        "    raise RuntimeError('a host said ' + {forgery!r})\n"
        "t = audit.guarded_timer(interval=0.01, function=body, name='a-timer')\n"
        "t.start(); t.join(5)\n"
    )

    def _run_child(self):
        return _run_child(self.CHILD.format(forgery=FORGERY))

    def test_a_dying_timer_writes_no_raw_traceback(self):
        done = self._run_child()
        assert "Traceback" not in done.stderr, (
            "a dying timer wrote a raw traceback past the sink:\n" + done.stderr[:500])

    def test_a_dying_timer_is_reported_through_the_sink(self):
        """The half a deleted guard fails: the failure must arrive as an ordinary record,
        not merely be swallowed."""
        done = self._run_child()
        records = _decoded_records(done.stdout)
        assert records, "a dying timer left no record at all: " + repr(done.stdout[:300])
        assert any("a-timer" in json.dumps(one) for one in records), (
            "the record does not name the timer that died: " + repr(done.stdout[:300]))

    def test_nothing_raw_leaves_a_dying_timer_by_either_stream(self):
        done = self._run_child()
        assert done.stdout.strip(), "the child wrote nothing, so this case sees nothing"
        for char in RAW_CONTROL[1:]:
            assert char not in done.stdout + done.stderr, (
                f"a dying timer carried a raw {char!r} to an output")


# Both guards, every time. A review found the first draft of the class below drove the
# thread guard alone, so turning the timer's own report from a failure into a bare line left
# the suite green -- the same finding one step to the side, which is the shape this front
# keeps hitting. The route is a parameter now, so a guard added later is a row here rather
# than a class nobody wrote.
GUARDS = (
    ("a thread", "audit.guarded_thread(target=body, name='a-loop')"),
    ("a timer", "audit.guarded_timer(interval=0.01, function=body, name='a-loop')"),
)

FAILING_CHILD = (
    "import sys\n"
    "from orbitd import audit\n"
    "audit.configure_logging(sys.stdout)\n"
    "def body():\n"
    "    raise RuntimeError('the backend went away')\n"
    "t = {start}\n"
    "t.start(); t.join(5)\n"
)


def _run_child(source):
    """Run a child and prove it ran. A review found two cases here passed on a child that
    exited immediately -- an empty stream has no bad line in it -- so an import failure was
    being reported as the defect the case is named for."""
    done = subprocess.run([sys.executable, "-c", source], capture_output=True,
                          text=True, timeout=60,
                          cwd=str(Path(__file__).resolve().parents[1]))
    assert done.returncode == 0, (
        f"the child did not run, so this case is measuring nothing: {done.stderr[:400]}")
    return done


@pytest.mark.parametrize("what,start", GUARDS, ids=[w for w, _ in GUARDS])
class TestAFailureTravelsWithItsRecord:
    """The module promises an exception travels in a field of the same record, and it is
    what makes a dead background loop diagnosable rather than merely counted. Dropping the
    field left the suite green: the traceback cases only ask that a failure does not become
    several lines, which a record carrying no traceback satisfies better than one that
    does."""

    def _records(self, start):
        return _decoded_records(_run_child(FAILING_CHILD.format(start=start)).stdout)

    def test_the_record_carries_the_failure(self, what, start):
        said = [one for one in self._records(start) if "a-loop" in json.dumps(one)]
        assert said, f"the dying {what} left no record"
        assert any(one.get("exception") for one in said), (
            f"the record says {what} died and not what killed it, so the operator has the "
            "fact and no way to act on it: " + repr(said)[:400])

    def test_the_failure_names_what_was_raised(self, what, start):
        said = [one for one in self._records(start) if one.get("exception")]
        assert said, f"no record from {what} carries a failure at all"
        carried = " ".join(str(one["exception"]) for one in said)
        assert "RuntimeError" in carried and "the backend went away" in carried, (
            "the failure field carries neither the kind nor the message: "
            + repr(carried)[:400])

    def test_it_is_still_one_record(self, what, start):
        """Anti-vacuity: carrying the traceback must not undo the separator property that
        the field exists inside. A raw multi-line traceback would satisfy the two cases
        above and ruin the stream."""
        done = _run_child(FAILING_CHILD.format(start=start))
        assert done.stdout.strip(), f"the {what} child wrote nothing at all"
        for line in done.stdout.splitlines():
            if line.strip():
                json.loads(line)


class TestALibrarysBadCallStillLeavesARecord:
    """The formatter catches a record whose arguments do not fit its format string and
    falls back to naming them. Deleting the guard leaves the suite green, and because the
    handler's failure route is silent by design, the record then disappears with no trace
    anywhere. Any library in the process can reach this by logging with the wrong number of
    arguments."""

    def test_a_record_that_cannot_be_formatted_is_still_written(self):
        import logging

        written = io.StringIO()
        saved = sys.stderr
        try:
            audit.configure_logging(written)
            logging.getLogger("some.library").warning("rejected %s %s", "only-one-arg")
        finally:
            audit.configure_logging(saved)
        records = _decoded_records(written.getvalue())
        assert records, (
            "a library logged with the wrong number of arguments and the record vanished "
            "with no trace, because the handler's failure route is silent: "
            + repr(written.getvalue()[:300]))

    def test_what_the_caller_meant_is_recoverable(self):
        import logging

        written = io.StringIO()
        saved = sys.stderr
        try:
            audit.configure_logging(written)
            logging.getLogger("some.library").warning("rejected %s %s", "only-one-arg")
        finally:
            audit.configure_logging(saved)
        assert "only-one-arg" in written.getvalue(), (
            "the record survived and the argument the caller passed did not: "
            + repr(written.getvalue()[:300]))

    def test_a_value_that_refuses_to_become_text_still_leaves_a_record(self):
        """The other way a caller reaches this, and the one that tells the guard apart from
        a narrow one. Both cases above raise the same kind of error, so narrowing the guard
        to that kind left the suite green while a library logging an object whose text form
        raises wrote nothing at all."""
        import logging

        class Unstringable:
            def __str__(self):
                raise RuntimeError("this value refuses to become text")

        written = io.StringIO()
        saved = sys.stderr
        try:
            audit.configure_logging(written)
            logging.getLogger("some.library").warning("rejected %s", Unstringable())
        finally:
            audit.configure_logging(saved)
        assert _decoded_records(written.getvalue()), (
            "a library logged a value whose text form raises and the record vanished with "
            "no trace: " + repr(written.getvalue()[:300]))

    def test_an_ordinary_record_is_untouched(self):
        """Anti-vacuity: a formatter that gave up on formatting entirely would pass both
        cases above."""
        import logging

        written = io.StringIO()
        saved = sys.stderr
        try:
            audit.configure_logging(written)
            logging.getLogger("some.library").warning("rejected %s", "a-name")
        finally:
            audit.configure_logging(saved)
        assert "rejected a-name" in written.getvalue(), repr(written.getvalue()[:300])
