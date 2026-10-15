"""`orbit` — an MCP client. Every `call` is one `tools/call`; `tools` is `tools/list`.

Login talks to the co-hosted token endpoint (`client_credentials`); tool traffic
goes through the official SDK over Streamable HTTP. No second code path, no
hand-rolled JSON-RPC.
"""

from __future__ import annotations

import argparse
import contextlib
import contextvars
import io
import json
import logging
import math
import os
import re
import sys
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, TextIO
from urllib.parse import urlparse

import anyio
import httpx2
import jwt
from mcp import Client
from mcp.client.streamable_http import streamable_http_client
from mcp.shared.exceptions import MCPError
from mcp_types import ElicitResult, InputRequiredResult

from .roles import role_of_scopes

EXIT_OK = 0
EXIT_TOOL = 1
EXIT_USAGE = 2
EXIT_AUTH = 3
EXIT_TRANSPORT = 4

HttpClientFactory = Callable[[Mapping[str, str]], httpx2.AsyncClient]

_BUILTINS = {"login", "tools", "call", "platform", "secrets", "onboard", "features", "check"}

log = logging.getLogger(__name__)


class _Said(Exception):
    """A failure the desk itself diagnosed, carrying its sentence as writer
    segments: the desk's own words plain, the outside value (a typed address,
    a path, an argument) as host text. str() is the plain sentence."""

    def __init__(self, *segments: Any) -> None:
        self.segments = list(segments)
        super().__init__("".join(seg.text if isinstance(seg, _Host) else str(seg)
                                 for seg in segments))


class _SaidValue(_Said, ValueError):
    """A value the desk refused."""


class _SaidNotLoggedIn(_Said, FileNotFoundError):
    """No session to use."""


def _desk_sentence(e: BaseException) -> "_Rendered":
    """The `orbit: ...` line for a failure the desk caught: its own segments
    when it wrote them, else the exception's text as one host value."""
    if isinstance(e, _Said):
        return _frame(["orbit: ", *e.segments])
    return _frame(["orbit: ", _Host(str(e))])


class _AskedAgain(Exception):
    """The host answered a confirmed call with a second request for input.
    The tool did not run; a result read off that answer would report a call
    that never happened."""


class _AuthCapture:
    """Keeps the HTTP 401/403 body the SDK otherwise folds into INTERNAL_ERROR."""

    def __init__(self) -> None:
        self.status: int | None = None
        self.body: dict[str, Any] | None = None
        self.www: str = ""

    async def on_response(self, response: httpx2.Response) -> None:
        if response.status_code not in (401, 403):
            return
        self.status = response.status_code
        self.www = response.headers.get("www-authenticate", "")
        await response.aread()
        try:
            parsed = json.loads(response.content)
        except (json.JSONDecodeError, UnicodeDecodeError, ValueError):
            parsed = None
        self.body = parsed if isinstance(parsed, dict) else None


def config_dir() -> Path:
    xdg = os.environ.get("XDG_CONFIG_HOME")
    base = Path(xdg) if xdg else Path.home() / ".config"
    return base / "orbit"


def host_of(url: str) -> str:
    host = urlparse(url).hostname
    if not host:
        raise _SaidValue("URL has no host: ", _Host(repr(url)))
    return host


def session_path(url: str) -> Path:
    """One file per origin: the host, and the port when one was typed, so two
    surfaces on one hostname (two port-forwards on localhost) never share a
    token. A default-port address keeps the file name it always had."""
    parsed = urlparse(url)
    name = host_of(url)
    default = {"https": 443, "http": 80}.get(parsed.scheme)
    if parsed.port is not None and parsed.port != default:
        name = f"{name}_{parsed.port}"
    return config_dir() / f"{name}.json"


def canonical_mcp_url(url: str) -> str:
    parsed = urlparse(url.strip())
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise _SaidValue("not an absolute http(s) URL: ", _Host(repr(url)))
    path = parsed.path.rstrip("/") or "/"
    return f"{parsed.scheme}://{parsed.netloc}{path}"


def origin_of(url: str) -> str:
    parsed = urlparse(url)
    return f"{parsed.scheme}://{parsed.netloc}"


_MAX_DISCOVERED_URL_LEN = 2048


def _normalized_port(parsed: Any) -> int | None:
    try:
        if parsed.port is not None:
            return int(parsed.port)
    except ValueError:
        return None
    if (parsed.scheme or "").lower() == "https":
        return 443
    if (parsed.scheme or "").lower() == "http":
        return 80
    return None


def _same_origin(a: str, b: str) -> bool:
    try:
        pa, pb = urlparse(a), urlparse(b)
        hosts = ((pa.hostname or "").lower(), (pb.hostname or "").lower())
        ports = (_normalized_port(pa), _normalized_port(pb))
    except ValueError:
        # A malformed address (e.g. broken IPv6) is never the typed origin.
        return False
    if (pa.scheme or "").lower() not in ("http", "https"):
        return False
    if (pb.scheme or "").lower() not in ("http", "https"):
        return False
    if not hosts[0] or not hosts[1]:
        return False
    return (
        pa.scheme.lower() == pb.scheme.lower()
        and hosts[0] == hosts[1]
        and ports[0] == ports[1]
    )


def _accept_discovered(value: Any, base_url: str) -> str | None:
    """The one rule for every address a host's document names: it must be a
    short absolute http(s) URL on the origin the person typed. Anything else
    is refused and the caller falls back to the typed address."""
    if not isinstance(value, str):
        return None
    if len(value) > _MAX_DISCOVERED_URL_LEN:
        return None
    text = value.strip()
    if any(c <= " " or c == "\x7f" for c in text):
        # Raw whitespace/controls never belong in an address; httpx would
        # refuse them at POST time with a traceback. Fail closed instead.
        return None
    try:
        parsed = urlparse(text)
    except ValueError:
        return None
    if parsed.scheme.lower() not in ("http", "https") or not parsed.netloc:
        return None
    if not _same_origin(text, base_url):
        return None
    return text


_MAX_OUTPUT_TEXT_LEN = 2000


def _escape_output_char(ch: str) -> str:
    """One character rendered for the reader's screen, in the notation whose
    three forms are exhaustive and disjoint:

    - a backslash then `x`, `u` or `U` and that many hex digits names a byte,
      and it is the only way a byte is named -- no byte has a short name,
      because a backslash and one letter is also what a neutralised mark
      looks like when the mark begins with that letter, and two of the words
      the desk reserves do;
    - a backslash then a backslash is a backslash the host typed;
    - a backslash then anything else is that character quoted -- a mark of the
      desk's the host typed and the desk neutralised, below.

    Printable text passes through untouched, so what the host said stays
    recoverable instead of collapsing onto one mark, and reading the notation
    back left to right can only reach one answer."""
    if ch == "\\":
        return "\\\\"
    if ch.isprintable():
        return ch
    code = ord(ch)
    if code < 0x100:
        return f"\\x{code:02x}"
    if code < 0x10000:
        return f"\\u{code:04x}"
    return f"\\U{code:08x}"


#: The two characters that tell this rendering a stretch of a write was
#: already rendered for the screen by the writer below, which renders a host's
#: text and the desk's marks in one pass. Rendering such a stretch a second
#: time would escape that one pass's own backslashes, and what the operator
#: reads would be the writer's notation seen through this one: a neutralised
#: mark as two backslashes, a backslash the host really typed as four. One
#: notation, applied once, at the point where text becomes screen.
#:
#: A host cannot open a stretch of its own: these are not printable, so the
#: writer renders them out of a host's text as an escape naming the byte,
#: exactly like any other character the screen cannot show. The desk's own
#: parts are literals in this program.
_RENDERED_OPEN = "\x11"
_RENDERED_CLOSE = "\x12"

#: Whether the write under way carries a line the writer rendered. The fact is
#: out of band, set by the one act that prints a _Rendered line and by nothing
#: else, so the pair above is honoured only inside such a line -- where every
#: host character was already rendered and the pair cannot occur -- and a pair
#: carried by anything else (an argument argparse quotes, a host's question)
#: is two bytes the screen cannot show, named like any other.
_printing_rendered: contextvars.ContextVar[bool] = contextvars.ContextVar(
    "orbit_printing_rendered", default=False)


class _Rendered(str):
    """A line the writer below has already rendered for the operator's screen.

    The fact travels as a type rather than in the characters, because the same
    line is also handed to a machine reader inside a failure document, where
    a screen's markers have no meaning: an encoder writes the string, and a
    string is what it gets. The markers are put on where the line is printed
    and nowhere else, so only the one act that hands a line to the guard
    claims it was rendered."""


def _for_the_screen(line: Any) -> Any:
    """One line on its way to the operator's screen, told apart for the guard.

    A line the writer rendered leaves marked, so the guard hands it to the
    reader as the writer wrote it. Anything else -- a sentence built without
    the writer, whatever it interpolated -- leaves as it is and the guard
    renders it character by character, which is what makes the writer the only
    way to reach a screen unrendered."""
    if isinstance(line, _Rendered) and line:
        return _RENDERED_OPEN + str(line) + _RENDERED_CLOSE
    return line


def _render_for_screen(text: str) -> str:
    """One write rendered for the reader's screen: character by character,
    except for a stretch the writer marked as rendered already, which goes
    through as it was written and loses its markers.

    A marker opens a stretch only inside a write the one printing act marked
    as rendered (_printing_rendered); anywhere else, paired or not, it is
    rendered like any other character the screen cannot show, so text that
    never came through the writer cannot reach the screen unrendered by
    carrying a pair."""
    out: list[str] = []
    honour = _printing_rendered.get()
    i, n = 0, len(text)
    while i < n:
        ch = text[i]
        if honour and ch == _RENDERED_OPEN:
            end = text.find(_RENDERED_CLOSE, i + 1)
            if end >= 0:
                out.append(text[i + 1:end])
                i = end + 1
                continue
        out.append(_escape_output_char(ch))
        i += 1
    return "".join(out)


class _SafeOutput:
    """The desk's own output. Every write through it leaves with this
    output's separators neutralised (no control characters, no second line)
    and each message's length bounded, so a hostile host cannot paint the
    terminal — including through a print that never heard of this rule.

    It cannot stop a host filling the screen. The bound was once a running
    total per stream, which did stop that, and which also deleted the
    operator's own prompts and warnings once a hostile listing had spent the
    stream's budget. That is the worse failure of the two: a host that wastes
    the operator's scrollback is a nuisance, and a desk that swallows the
    question it is asking is a trap. So the total is gone deliberately, and a
    host that returns many messages can write as much as it likes.

    Only one trailing newline is ever preserved, the print terminator; every
    other line break is escaped. Ordinary one-line text passes through
    character for character. The length bound is on the message (terminator
    to terminator), not on the stream and not on the write: a value split
    across many small writes still fills the same message budget, while one
    message never spends another's — a prompt, warning, or failure line
    after a long list is still shown. A message cut by the bound ends in
    "..." where it was cut, never silent. write returns the count its caller
    handed it, even when the stream kept less, so callers that check keep
    working. All other attributes proxy the wrapped stream.
    """

    def __init__(self, inner: TextIO) -> None:
        self._inner = inner
        self._msg_len = 0
        self._cut = False

    def write(self, text: str) -> int:
        if not isinstance(text, str) or not text:
            return self._inner.write(text)
        given = len(text)
        terminator = ""
        if text.endswith("\n"):
            text, terminator = text[:-1], "\n"
        if text:
            rendered = _render_for_screen(text)
            if not self._cut:
                room = _MAX_OUTPUT_TEXT_LEN - self._msg_len
                if room <= 0:
                    self._inner.write("...")
                    self._cut = True
                elif len(rendered) > room:
                    self._inner.write(rendered[:room].rstrip() + "...")
                    self._msg_len += room
                    self._cut = True
                else:
                    self._inner.write(rendered)
                    self._msg_len += len(rendered)
        if terminator:
            self._inner.write(terminator)
            self._msg_len = 0
            self._cut = False
        return given

    def writelines(self, lines: Any) -> None:
        for line in lines:
            self.write(line)

    def flush(self) -> None:
        self._inner.flush()

    def __getattr__(self, name: str) -> Any:
        return getattr(self.__dict__["_inner"], name)


def configure_output(stream: TextIO) -> TextIO:
    """Wrap the desk's own output in the rendering above. Installed on both
    streams when the desk starts; wrapping an already-wrapped stream returns
    it unchanged so a second install cannot double-escape."""
    if isinstance(stream, _SafeOutput):
        return stream
    return _SafeOutput(stream)


def _ensure_output_wrapped() -> None:
    """Render by default on both streams, even for a command function called
    directly: a print that never heard of this rule is then safe, and the
    unsafe path is the one an author has to ask for (write_json_document)."""
    if not isinstance(sys.stdout, _SafeOutput):
        sys.stdout = configure_output(sys.stdout)
    if not isinstance(sys.stderr, _SafeOutput):
        sys.stderr = configure_output(sys.stderr)


def _ensure_logging_configured() -> None:
    """Take the desk's own warnings where the command line starts.

    A repair moved a diagnostic from printing to logging, which removes it
    from the operator's stream only if something is configured to take it.
    Nothing is: the command line installs no handler, so the standard
    library's last resort writes every warning straight at the operator —
    and the repair holds only under pytest, whose plugin installs a root
    handler. One do-nothing handler on the desk's own logger, so a warning
    goes where warnings go and the operator's stream holds only what the
    writer wrote."""
    desk = logging.getLogger("orbitd")
    if not any(isinstance(h, logging.NullHandler) for h in desk.handlers):
        desk.addHandler(logging.NullHandler())


def _raw_stream(stream: TextIO) -> TextIO:
    """The stream underneath the rendering, for the one thing that is not
    rendered. Only write_json_document should reach for this."""
    if isinstance(stream, _SafeOutput):
        inner = stream.__dict__.get("_inner")
        if inner is not None:
            return inner
    return stream


def _raw_stdout() -> TextIO:
    """The normal stream underneath the rendering. Only write_json_document
    should reach for this."""
    return _raw_stream(sys.stdout)


def _sanitize_json_numbers(value: Any) -> Any:
    """Strict documents never carry a bare non-finite number: a strict reader
    refuses the whole document over one NaN/Infinity, and this desk's own
    reader is permissive enough to hide it. Host-chosen floats that are not
    finite become null before the encoder runs."""
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, dict):
        return {key: _sanitize_json_numbers(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_sanitize_json_numbers(item) for item in value]
    return value


_LONE_SURROGATE_RE = re.compile("[\ud800-\udfff]")


def write_json_document(payload: Any, *, sort_keys: bool = False,
                        stream: TextIO | None = None,
                        indent: int | None = 2) -> None:
    """The explicit, narrow act for a machine-readable document: its encoder
    neutralises separators by construction, so it needs no rendering and must
    not be given any — a wrapper that escaped it would stop it parsing.

    A machine document is a machine document whichever stream it leaves by,
    so this takes the stream (default the normal stream) and writes to the
    raw underneath a guard. Strict: non-finite numbers leave as null, never
    a bare NaN/Infinity word; text leaves as UTF-8, never backslash-u
    escapes a guard could double. The failure envelope on the error stream
    stays single-line (indent=None); documents on the normal stream stay
    pretty (indent=2).

    A host can send a lone surrogate, which survives parsing but UTF-8
    cannot encode, so the encoder would raise mid-stream leaving half a
    document behind. The unencodable character is rendered as U+FFFD
    instead — ordinary non-Latin text still leaves as UTF-8 — and the whole
    document is built and proven to encode before a single byte is written,
    so the stream holds a parseable document whatever a host sent."""
    body = _sanitize_json_numbers(payload if payload is not None else {})
    text = json.dumps(body, indent=indent, sort_keys=sort_keys, default=str,
                      allow_nan=False, ensure_ascii=False)
    text = _LONE_SURROGATE_RE.sub("\ufffd", text) + "\n"
    text.encode("utf-8")
    _write_own_document(text, stream)


def _write_own_document(text: str, stream: TextIO | None = None) -> None:
    """The one way to the raw stream. A document the desk composed itself — a
    JSON document, whose encoder neutralises separators, or argparse's usage
    and help, built from the desk's own strings — needs no rendering and must
    not be given any. Everything else the desk writes goes through the
    rendering; the contract that holds this counts the callers of the raw
    stream and allows exactly this one."""
    raw = _raw_stdout() if stream is None else _raw_stream(stream)
    try:
        raw.write(text)
        raw.flush()
    except BrokenPipeError:
        _quiet_closed_stream(raw)


def _pin_token_endpoint(candidate: Any, mcp_url: str) -> str:
    """The chokepoint: every candidate token endpoint passes _accept_discovered
    here, immediately before the post, so no source — the pasted hint, the
    authorization-server document, or one added later — can go round the pin.
    A refused candidate is reported in one sentence naming the typed origin and
    the refused one; the desk falls back to the typed origin's endpoint.
    The sentence is a diagnostic, not the command's report, so it goes to the
    log — never to the operator's stream and never through the outcome
    writer, which renders an outcome and returns an exit code."""
    accepted = _accept_discovered(candidate, mcp_url)
    if accepted is not None:
        return accepted
    origin = origin_of(mcp_url)
    fallback = origin + "/token"
    log.warning("orbit: refusing token endpoint %s outside typed origin %s; using %s",
                candidate, origin, fallback)
    return _accept_discovered(fallback, mcp_url) or fallback


def _iso(ts: int) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat(timespec="seconds")


def _write_session(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(path.parent, 0o700)
    tmp = path.with_suffix(path.suffix + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(payload, f, indent=1, sort_keys=True)
        f.write("\n")
    os.replace(tmp, path)
    os.chmod(path, 0o600)


def load_session(url: str | None) -> dict[str, Any]:
    if url:
        path = session_path(canonical_mcp_url(url))
        if not path.is_file():
            raise _SaidNotLoggedIn("not logged in for ", _Host(host_of(canonical_mcp_url(url))),
                                   "; run orbit login")
    else:
        files = sorted(config_dir().glob("*.json"))
        if not files:
            raise _SaidNotLoggedIn("not logged in; run orbit login --url https://<host>/mcp")
        if len(files) > 1:
            hosts = ", ".join(p.stem for p in files)
            raise _SaidNotLoggedIn("several sessions stored (", _Host(hosts), "); pass --url")
        path = files[0]
    with open(path) as f:
        try:
            data = json.load(f)
        except json.JSONDecodeError as e:
            raise _SaidValue("session file ", _Host(str(path)),
                             " is not a stored orbit login") from e
    if not isinstance(data, dict) or "url" not in data or "token" not in data:
        raise _SaidValue("session file ", _Host(str(path)), " is not a stored orbit login")
    if url and not _same_origin(str(data["url"]), canonical_mcp_url(url)):
        # The typed address is not repeated (it may carry userinfo); the host
        # names the file, which is what the operator has to act on.
        raise _SaidValue("the stored session for ", _Host(host_of(canonical_mcp_url(url))),
                         " is for another origin (scheme or port); run orbit login for the "
                         "address you typed")
    return data


def _credentials(stdin: TextIO) -> tuple[str, str, str | None]:
    """client_id, client_secret, optional token_endpoint from bootstrap JSON."""
    env_id = os.environ.get("ORBIT_CLIENT_ID")
    env_file = os.environ.get("ORBIT_CLIENT_SECRET_FILE")
    if env_id and env_file:
        try:
            secret = Path(env_file).read_text().rstrip("\n")
        except OSError as e:
            raise _SaidValue("cannot read ORBIT_CLIENT_SECRET_FILE: ",
                             _Host(str(e.strerror))) from e
        if not env_id.strip() or not secret:
            raise ValueError("ORBIT_CLIENT_ID / ORBIT_CLIENT_SECRET_FILE are empty")
        return env_id.strip(), secret, None
    if env_id or env_file:
        raise ValueError("set both ORBIT_CLIENT_ID and ORBIT_CLIENT_SECRET_FILE, or neither")
    raw = stdin.read()
    try:
        body = json.loads(raw)
    except json.JSONDecodeError as e:
        raise ValueError("credentials on stdin must be JSON with client_id and client_secret") from e
    if not isinstance(body, dict):
        raise ValueError("credentials on stdin must be a JSON object")
    client_id = body.get("client_id")
    secret = body.get("client_secret")
    if not client_id or not secret:
        raise ValueError("credentials JSON needs client_id and client_secret")
    endpoint = body.get("token_endpoint")
    return str(client_id), str(secret), str(endpoint) if endpoint else None


# The desk cannot verify the token's signature (the key lives on the surface),
# so whatever it reads out of the token is a claim, not a finding. Every line
# the sign-in prints about such a value carries this word, and the session file
# records the value under a claimed_* key, never a bare one.
CLAIM_MARKER = "claimed"


def _role_from_token(token: str, scope: str | None) -> str:
    claims = jwt.decode(token, options={"verify_signature": False, "verify_aud": False, "verify_exp": False})
    role = claims.get("role")
    if role:
        return str(role)
    held = role_of_scopes(str(scope or claims.get("scope") or "").split())
    return held.value if held else "unknown"


def _expires_at(token: str, expires_in: Any) -> str:
    claims = jwt.decode(token, options={"verify_signature": False, "verify_aud": False, "verify_exp": False})
    if "exp" in claims:
        return _iso(int(claims["exp"]))
    if expires_in is not None:
        return _iso(int(datetime.now(tz=timezone.utc).timestamp()) + int(expires_in))
    raise ValueError("token response has no expiry")


def _first_sentence(text: str) -> str:
    compact = " ".join(text.split())
    if not compact:
        return ""
    head, sep, _ = compact.partition(". ")
    return head.rstrip(".") if sep else compact.rstrip(".")


def _coerce_arg(raw: str) -> Any:
    lowered = raw.lower()
    if lowered == "true":
        return True
    if lowered == "false":
        return False
    if lowered == "null":
        return None
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return raw


def _parse_args_list(items: list[str]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for item in items:
        if "=" not in item:
            raise _SaidValue("--arg needs key=value, not ", _Host(repr(item)))
        key, _, value = item.partition("=")
        if not key:
            raise _SaidValue("--arg needs key=value, not ", _Host(repr(item)))
        out[key] = _coerce_arg(value)
    return out


def _rewrite(argv: list[str]) -> list[str]:
    """`orbit <tool>` is `orbit call <tool>` — one tool call, no second path."""
    for a in argv:
        if a.startswith("-"):
            continue
        if a in _BUILTINS:
            return argv
        idx = argv.index(a)
        return argv[:idx] + ["call"] + argv[idx:]
    return argv


class _DeskParser(argparse.ArgumentParser):
    """The desk's usage and help pages take the raw path; its argument
    errors do not.

    Both streams are wrapped in the rendering for host text, and argparse
    writes its documents through them like any print: a help page then
    reached the operator with every line break as the name of a byte, and
    was unreadable. Usage and help are composed from the desk's own strings
    alone, so they leave by the one raw path a JSON document takes. An
    argument error quotes what was typed, which is anyone's text, so it
    stays under the rendering. Subparsers are built with the parent's
    class, so the overrides cover every command."""

    def print_usage(self, file: Any = None) -> None:
        _write_own_document(self.format_usage(), file if file is not None else sys.stdout)

    def print_help(self, file: Any = None) -> None:
        _write_own_document(self.format_help(), file if file is not None else sys.stdout)


def _parser() -> argparse.ArgumentParser:
    p = _DeskParser(
        prog="orbit",
        description="MCP client for orbitd. Every subcommand is one tool call (or tools/list).",
    )
    sub = p.add_subparsers(dest="command", required=True)

    login = sub.add_parser("login", help="exchange client credentials for a token and store it")
    login.add_argument("--url", required=True, help="MCP URL, e.g. https://mcp.example/mcp")
    login.add_argument("--json", action="store_true",
                       help="print the result as a JSON document instead of sentences")

    tools = sub.add_parser("tools", help="list the tools this token may call")
    tools.add_argument("--url", help="MCP URL; defaults to the stored session")
    tools.add_argument("--json", action="store_true",
                       help="print the result as a JSON document instead of sentences")

    call = sub.add_parser("call", help="call one tool")
    call.add_argument("tool", help="tool name, e.g. orbit_platform_health")
    call.add_argument("--url", help="MCP URL; defaults to the stored session")
    call.add_argument("--arg", action="append", default=[], metavar="KEY=VALUE",
                      help="tool argument; repeatable")
    call.add_argument("--json", nargs="?", const=True, default=False, metavar="ARGS",
                      help="print structuredContent as JSON; optional JSON object of arguments")
    confirm = call.add_mutually_exclusive_group()
    confirm.add_argument("--yes", action="store_true",
                         help="confirm a mutating tool without prompting")
    confirm.add_argument("--no", action="store_true",
                         help="refuse a mutating tool without prompting")

    from .platform import add_parser as add_platform_parser
    add_platform_parser(sub)
    from .secrets import add_parser as add_secrets_parser
    add_secrets_parser(sub)
    from .onboard import add_parser as add_onboard_parser
    add_onboard_parser(sub)
    from .features_cli import add_parser as add_features_parser
    add_features_parser(sub)
    from .checks_cli import add_parser as add_check_parser
    add_check_parser(sub)
    return p


def parse_argv(argv: list[str]) -> argparse.Namespace:
    return _parser().parse_args(_rewrite(argv))


def _attach_capture(client: httpx2.AsyncClient, capture: _AuthCapture) -> None:
    hooks = client.event_hooks.setdefault("response", [])
    hooks.append(capture.on_response)


def _open_http(headers: Mapping[str, str], factory: HttpClientFactory | None,
               url: str, capture: _AuthCapture | None = None) -> httpx2.AsyncClient:
    if factory is not None:
        client = factory(headers)
    else:
        # Reads may block for as long as the longest tool legitimately does:
        # orbit_job_wait holds the request up to 120 s (its own cap), so a 30 s read
        # timeout turned every long wait into a transport failure (docs/V1.md §8).
        client = httpx2.AsyncClient(base_url=origin_of(url), headers=dict(headers),
                                    timeout=httpx2.Timeout(150.0, connect=15.0))
    if capture is not None:
        _attach_capture(client, capture)
    return client


def _auth_body(capture: _AuthCapture) -> dict[str, Any]:
    """The refusal the surface made, with the needed role backfilled from the
    challenge header when the body does not name it.

    One reader for both renderings of a refusal — the sentence the operator
    reads and the document a machine caller parses — so the two can never
    disagree about what the surface said.
    """
    body = dict(capture.body or {})
    if not body.get("required_role") and capture.www:
        match = re.search(r'scope="orbit:(\w+)"', capture.www)
        if match:
            body.setdefault("required_role", match.group(1))
            body.setdefault("error", "insufficient_scope")
    if not body.get("error") and not body.get("error_description"):
        # No refusal to quote: the sentence falls back to the status and the
        # challenge, so the document carries those too — a machine caller
        # must not be told less than the person reading the sentence.
        if capture.status is not None:
            body.setdefault("status", capture.status)
        if capture.www:
            body.setdefault("challenge", capture.www)
    return body


def _auth_outcome(capture: _AuthCapture) -> _Outcome:
    """A refused token or role as an outcome: the refusal quoted verbatim
    as the payload, the sentence for the operator as the message — the
    same refusal the call command renders. The caller hands it to
    write_outcome."""
    body = _auth_body(capture)
    return _Outcome(EXIT_AUTH, body, _auth_sentence(body, capture))


def _walk_exceptions(exc: BaseException):
    if isinstance(exc, BaseExceptionGroup):
        for inner in exc.exceptions:
            yield from _walk_exceptions(inner)
    else:
        yield exc


def _is_not_offered(capture: _AuthCapture) -> bool:
    """A 403 for a name the surface does not offer is not an authentication failure:
    no credential would make the call work, so the caller is told the name is wrong,
    not that their credential failed. Matches the server's not-offered refusal, which
    names no required role — the case where the credential really did fail."""
    body = capture.body or {}
    return (capture.status == 403 and body.get("error") == "unknown_tool"
            and not body.get("required_role"))


@dataclass(frozen=True)
class _Outcome:
    """One command outcome: what happened and the payload describing it.

    The command builds one for every exit — success or failure — and
    write_outcome renders it. Outcomes follow the exit codes the desk
    already reported (ok, tool, usage, auth, transport) and nothing finer:
    the payload and the message already carry the specifics, and a finer
    taxonomy would rebuild the many doors this replaced. A refusal the
    surface or a tool made is quoted verbatim as the payload; a failure
    with no refusal to quote (bad arguments, transport, confirmation)
    carries {"error": kind, "message": sentence}.

    The message is the human text: one sentence, or one string per line
    when the human form is a listing. The builder splits the lines — a
    line break inside one entry is host text for the guard to render,
    never a second line.
    """

    exit_code: int
    payload: Any
    message: str | tuple[str, ...]


def write_outcome(outcome: _Outcome, *, machine: bool) -> int:
    """The one writer for a command outcome.

    The form follows what the caller asked for, never which failure
    happened: a document on the normal stream for success, a one-line
    document on the error stream for failure, when machine output was
    requested; the human text on the same stream the document would have
    used otherwise, one print per line so a listing stays a listing.
    Rendering is untouched: human lines go through the guard and documents
    bypass it on whichever stream they leave by.
    """
    lines = outcome.message if isinstance(outcome.message, tuple) else (outcome.message,)
    stream = sys.stdout if outcome.exit_code == EXIT_OK else sys.stderr
    try:
        if outcome.exit_code == EXIT_OK:
            if machine:
                write_json_document(outcome.payload)
            else:
                for line in lines:
                    _print_line(line, sys.stdout)
        elif machine:
            write_json_document(outcome.payload, stream=sys.stderr, indent=None)
        else:
            for line in lines:
                _print_line(line, sys.stderr)
        # A small outcome sits in the stream's buffer until someone flushes
        # it; flushed here, a closed pipe is found while the guard below is
        # still the one holding it, not at the interpreter's exit.
        stream.flush()
    except BrokenPipeError:
        # The reader went away (`orbit tools | head`). Nothing is owed to it;
        # the exit is still the outcome's, and the interpreter's own flush at
        # exit must not raise a second time on the same closed pipe.
        _quiet_closed_stream(stream)
    return outcome.exit_code


def _quiet_closed_stream(stream: TextIO) -> None:
    """Point the broken stream's descriptor at nothing, so nothing written to
    it later (the exit flush included) raises again."""
    try:
        devnull = os.open(os.devnull, os.O_WRONLY)
        os.dup2(devnull, stream.fileno())
        os.close(devnull)
    except (OSError, ValueError, AttributeError):
        pass


def _print_line(line: Any, stream: TextIO) -> None:
    """The one act that hands a line to the guard, and so the only place that
    says whether the guard may trust the line's markers: it may for a line the
    writer rendered, and for nothing else."""
    mark = _printing_rendered.set(isinstance(line, _Rendered))
    try:
        print(_for_the_screen(line), file=stream)
    finally:
        _printing_rendered.reset(mark)


@dataclass(frozen=True)
class _Host:
    """One host or operator value inside a sentence the desk writes. The desk's
    own parts are plain strings; every value goes through the writer, so a new
    sentence cannot forget the repair -- there is nowhere else to write one."""
    text: str


#: The desk's own marks, in one place, each with the shape the desk writes it
#: in: the characters that may follow it in a part of a sentence the desk wrote
#: itself. That shape IS the rule, and it is asked once rather than kept as a
#: character list with a rule for one of them beside it -- a mark cannot be
#: added here without saying where the desk writes it, so there is no second
#: list for the next mark to be missing from.
#:
#: A host's copy is neutralised where the desk could have written it and
#: nowhere else. The separators the desk writes only before a space -- the
#: comma between two role clauses, the semicolon before what to do next, the
#: colon before a value -- are the host's own everywhere else, so a label
#: selector, a list of names and an address reach the operator as the string
#: they would type. The empty shape means the desk writes the mark at the END
#: of one of its parts, immediately before a value it was handed: whatever
#: follows there is the next part of the line, so a mark with that shape could
#: be the desk's however a host wrote it, and it stays neutralised in every
#: shape. That is the parentheses around a status or a code.
#:
#: Single characters are escaped inline wherever host text goes. A label that
#: is letters up to its last character -- the role clauses, the summary's
#: key=value pairs -- leaves with a backslash in front of its first letter
#: instead, so the value around it stays readable: what the host said must
#: still reach the operator, only not in the desk's voice. Either way the
#: notation reverses: drop a backslash and what the host said comes back.
#:
#: THE CONDITION ON EVERY MARK, single character or word, here and in
#: `_DESK_WORD_MARKS`: no mark the desk neutralises may begin with `x`, `u` or
#: `U`. Those three letters open a byte's name, the only way a byte is named,
#: and a backslash before anything else is a character quoted -- so a mark
#: beginning with one of them would arrive on the screen as a line a host
#: could also have sent as that byte and the rest of the mark, and the
#: operator would have no way to tell which was typed. Today none does. The
#: next mark, and the next field added to the summary, is added against this.
#: What a set says: the characters that may follow the mark for it to be the
#: desk's, with the end of the value always counting. An empty string in a set
#: means the mark is the desk's in every shape -- the brackets, which the desk
#: writes at a segment's end straight before a value it was handed, so a host's
#: bracket is neutralised wherever it stands. The table once read as if the
#: brackets had two shapes; they have one behaviour, all of them.
_DESK_MARK_SHAPES: dict[str, frozenset[str]] = {
    "(": frozenset({"", " "}),
    ")": frozenset({"", ":"}),
    ",": frozenset({" "}),
    ":": frozenset({" "}),
    ";": frozenset({" "}),
}

_DESK_MARK_CHARS = frozenset(_DESK_MARK_SHAPES)

#: The role clauses the desk writes: the field of a refusal each one reports,
#: and the words the desk puts in front of that value. This table is the only
#: copy of those words in the program. Both sentences that report a role build
#: their clauses from it, and the word marks below are read off it, so a clause
#: cannot be written anywhere the escape does not already know about -- and the
#: surface does not write them at all. It carries the two roles as the fields
#: named here and says nothing about them in prose: a second copy of the
#: sentence, arriving over a wire, is host text the desk cannot tell from a host
#: claiming a role, which is what marked the commonest refusal as a forgery.
#:
#: The desk's other word marks are the summary's labels, and they are not
#: written down anywhere: they are derived from the fields the summary prints,
#: and the clause below. All of them meet in `_DESK_WORD_MARKS`, which is
#: assembled beside that summary because that is where the half that moves is
#: decided.
_DESK_ROLE_CLAUSES: tuple[tuple[str, str], ...] = (
    ("required_role", "requires role "),
    ("held_role", "token holds "),
)

_DESK_ROLE_MARKS = tuple(mark for _, mark in _DESK_ROLE_CLAUSES)

#: The words the desk puts between an address it refused and the origin the
#: operator typed, in the one place they are written. A refusal of an address
#: names two of them, and which one the desk went on to use is the whole point
#: of the sentence -- so the clause that tells them apart is the desk's own
#: word, not prose at the site that happens to need it. Without that, a host
#: whose offered address carries the clause writes the sentence a second time
#: and the operator cannot say which origin was accepted.
#:
#: It ends where the desk's own words end, before the space that separates them
#: from the value, so a host's copy leaves with a backslash in front of its
#: first letter and the address around it stays the string the operator would
#: type -- the notation the role clauses already use.
_DESK_ORIGIN_CLAUSE = "outside typed origin "


def _escape_host_text(value: str) -> str:
    """One host value rendered for the operator's screen, in one pass: the
    desk's marks neutralised and the bytes the screen cannot show named, by
    the same notation, so what comes out is what a person reads.

    It is one pass because there is only one backslash to go round. Both needs
    are real -- a mark the host typed must not be readable as the desk's, and a
    byte the screen cannot show must stay recoverable -- and each was once met
    by its own pass, this one and the output guard's. Applied one after the
    other they composed on the same character: a neutralised mark reached the
    screen as two backslashes and a backslash the host really typed as four,
    so the rule that reverses the notation (drop a backslash and what the host
    said comes back) was true of this function's return and false of the only
    place anyone reads. So this renders both here, and the guard leaves what
    the writer produced alone.

    Each mark from the host leaves neutralised: the mark with a backslash in
    front of it, the backslash itself doubled, so a typed backslash-mark stays
    distinguishable from a rendered one. A byte the screen cannot show is named
    by its number and never by a letter, so a neutralised mark and a named byte
    cannot arrive as the same line: a tab a host typed is `\\x09`, and the
    reserved word that begins with `t` is that word behind one backslash.

    A mark is the desk's only where the desk could have written it, which is
    `_DESK_MARK_SHAPES` asked character by character: a comma inside a label
    selector, a semicolon between two names, the colon before a port or in
    `://` are the host's own and leave as they arrived, so what the operator
    is shown is the string they would type. The notation reverses read left to
    right: a backslash always quotes the character after it, so a
    backslash-mark is a host's mark the desk neutralised and never a mark the
    desk rendered."""
    out: list[str] = []
    i, n = 0, len(value)
    while i < n:
        for mark in _DESK_WORD_MARKS:
            if value.startswith(mark, i):
                out.append("\\" + "".join(_escape_output_char(ch) for ch in mark))
                i += len(mark)
                break
        else:
            ch = value[i]
            if _is_the_desks_mark(value, i):
                out.append("\\" + _escape_output_char(ch))
            else:
                out.append(_escape_output_char(ch))
            i += 1
    return "".join(out)


def _is_the_desks_mark(value: str, i: int) -> bool:
    """Whether the character at `value[i]` is one the desk could have written
    here -- the one question `_DESK_MARK_SHAPES` answers.

    The end of a host value is not the end of the line: the writer joins the
    next part of the desk's own on after it, so a mark in the last position is
    in whatever shape that part begins with, and is the desk's."""
    shapes = _DESK_MARK_SHAPES.get(value[i])
    if shapes is None:
        return False
    follows = value[i + 1:i + 2]
    return follows == "" or "" in shapes or follows in shapes


def _screen_width(text: str) -> int:
    """How many characters one of the desk's OWN parts takes on the operator's
    screen: the length the output guard renders it to, counted with the guard's
    own per-character rendering, so the writer and the guard can never disagree
    about where the line is cut.

    A host's value is not measured here. It is rendered for the screen before
    it is cut, by the one pass above, so the characters it is cut in are the
    characters the operator reads and its width is its length."""
    return sum(len(_escape_output_char(ch)) for ch in text)


def _token_boundaries(rendered: str) -> list[int]:
    """Every index at which a token of the notation begins or ends: a plain
    character, a doubled backslash, a backslash and the character it quotes,
    or a backslash and a byte's name with all its digits. A cut at one of these
    leaves both sides whole; a cut anywhere else leaves a reader half a name
    and digits the host never typed."""
    bounds = [0]
    i, n = 0, len(rendered)
    while i < n:
        if rendered[i] == "\\":
            opener = rendered[i + 1:i + 2]
            i += {"x": 4, "u": 6, "U": 10}.get(opener, 2)
        else:
            i += 1
        bounds.append(min(i, n))
    return bounds


def _cut_host_text(rendered: str, share: int) -> str:
    """Shorten one host value, already rendered for the screen, to `share`
    screen characters. It is cut in the characters it is read in: the pass
    above rendered it, so a byte the screen widens was widened before this
    counted, and nothing renders it again afterwards.

    A shortened value keeps its start and its end with "..." where text was
    removed, so both halves stay readable: the start names what the value is,
    and the end carries the template's own words after an echoed value -- a
    refusal names the call before the value and keeps explaining it after.
    Both cuts fall on a boundary of the notation, never inside a token: a cut
    between a backslash and its mark would quote the desk's own mark that
    follows, a cut that dropped the backslash would unquote the host's mark
    that stays, and a cut inside a byte's name -- it once fell there -- left
    a reader the name's opener without its digits on one side and the digits
    as printable text on the other."""
    if len(rendered) <= share:
        return rendered
    if share <= len("..."):
        return "..."[:share]
    bounds = _token_boundaries(rendered)
    wanted = max(len(rendered) - share // 4, 0)
    start = next(b for b in bounds if b >= wanted)
    tail = rendered[start:]
    room = max(share - len("...") - len(tail), 0)
    keep = max(b for b in bounds if b <= room)
    core = rendered[:keep].rstrip()
    return core + "..." + tail


def _frame(segments: list[str | _Host]) -> str:
    """The one writer for every operator-visible line that carries host text.

    The desk's own parts (plain strings) go in whole and are rendered by the
    output guard like any other text; each host value (_Host) is rendered here
    for the screen -- the desk's marks neutralised and the bytes the screen
    cannot show named, in one pass. The line says so by its type, and the one
    act that prints a line marks it, so the guard hands it to the reader as
    written rather than rendering it again. The values share what the desk's
    parts did not spend of the line the guard cuts, with the desk's parts
    reserved first. So what the operator sees always ends with the desk's own
    last part, however wide the host's characters render, and a shortened value
    is always the host's, never the frame."""
    desk_width = sum(_screen_width(seg) for seg in segments if isinstance(seg, str))
    values = [seg for seg in segments if isinstance(seg, _Host)]
    share = max((_MAX_OUTPUT_TEXT_LEN - desk_width) // len(values), 0) if values else 0
    rendered = [_cut_host_text(_escape_host_text(seg.text), share) for seg in values]
    out: list[str] = []
    taken = 0
    for seg in segments:
        if isinstance(seg, _Host):
            out.append(rendered[taken])
            taken += 1
        else:
            out.append(seg)
    return _Rendered("".join(out))


def _role_clauses(body: Mapping[str, Any]) -> list[str | _Host]:
    """The role clauses a refusal carries, as parts of a sentence the desk writes:
    the desk's own words from the one table, each followed by the refusal's value
    for the field beside it, with the desk's comma between them.

    Read in the table's order, which is the order the clauses are spoken in, so
    both sentences that report a role say it the same way and neither has a copy
    of the words of its own.

    A field is taken from the body, or from the body's `evidence` when it keeps
    one: the refused-token challenge names the roles at the top level and a tool
    refusal carries them among its evidence, and this reads a role wherever the
    refusal it was handed puts it."""
    beside = body.get("evidence")
    flat: list[str | _Host] = []
    for field, mark in _DESK_ROLE_CLAUSES:
        value = body.get(field)
        if not value and isinstance(beside, Mapping):
            value = beside.get(field)
        if not value:
            continue
        if flat:
            flat.append(", ")
        flat += [mark, _Host(str(value))]
    return flat


def _auth_sentence(body: Mapping[str, Any], capture: _AuthCapture) -> str:
    """The human sentence for a refused token or role: what the surface said,
    plus which role it needs and which the token holds when those are known.

    The frame is the desk's own -- the parenthesised roles, the comma between
    them, the colon before the challenge -- and every value in it goes through
    the one writer, which neutralises the desk's marks wherever host text goes
    and shortens the host's values before the frame."""
    detail = body.get("error_description") or body.get("error")
    if detail:
        segments: list[str | _Host] = ["orbit: ", _Host(str(detail))]
        roles = _role_clauses(body)
        if roles:
            segments += [" ("] + roles + [")"]
        return _frame(segments)
    if capture.www:
        return _frame(["orbit: auth failed (", _Host(str(capture.status)), "): ",
                       _Host(str(capture.www))])
    return _frame(["orbit: auth failed (", _Host(str(capture.status)), ")"])


def _refusal_sentence(tool: str, payload: Any) -> str:
    """The human sentence for a tool answer reported as a failure.

    The frame is the desk's own: one parenthesised code, one colon part, one
    next-action part. The values -- the operator's tool name and the host's
    three -- all go through the one writer, so a host that types `):` or
    `; next:` is quoting itself, not writing another part.

    A refusal that names the role it needed and the role the token held reports
    it from those fields, in the desk's own clauses, and does not also render the
    refusal's prose: the two say the same thing, and the prose copy is text from
    outside, so the commonest refusal an operator meets arrived with the desk's
    own words neutralised inside it. What the surface said about anything else it
    has no field for is still rendered, as host text, exactly as before."""
    if isinstance(payload, dict):
        code = payload.get("code")
        what = payload.get("what_failed")
        nxt = payload.get("next_action")
        roles = _role_clauses(payload)
        if (roles
                or (isinstance(code, str) and code.strip())
                or (isinstance(what, str) and what.strip())
                or (isinstance(nxt, str) and nxt.strip())):
            segments: list[str | _Host] = ["orbit: ", _Host(tool), " refused"]
            if isinstance(code, str) and code.strip():
                segments += [" (", _Host(code.strip()), ")"]
            if roles:
                segments += [": "] + roles
            elif isinstance(what, str) and what.strip():
                segments += [": ", _Host(what.strip())]
            if isinstance(nxt, str) and nxt.strip():
                segments += ["; next: ", _Host(nxt.strip())]
            return _frame(segments)
        detail = payload.get("error_description") or payload.get("error")
        if detail:
            return _frame(["orbit: ", _Host(tool), " refused: ", _Host(str(detail))])
        if payload.get("status"):
            return _frame(["orbit: ", _Host(tool), " failed with status ",
                           _Host(str(payload["status"]))])
        return _frame(["orbit: ", _Host(tool), " failed"])
    return _frame(["orbit: ", _Host(tool), " failed: ", _Host(str(payload))])


def _call_failure(exc: BaseException, capture: _AuthCapture,
                  tool: str | None = None) -> _Outcome:
    """A transport or auth failure as an outcome.

    The same causes keep the same exits they always reported — only the
    rendering moved to write_outcome. A failure with no outcome to build
    from (no refusal captured, no transport error) leaves as a tool failure
    naming the exception rather than by a traceback past the writer; the
    exit stays the one an unhandled failure always reported.

    The last branch is the answer nothing else fit: the surface answered in
    a shape this desk does not understand. The operator is told which call
    that was and what to do next; the internal type name stays on the debug
    log, where a maintainer can reach it, never on the operator's stream. The
    two branches above it are the same shape for the failures with a name but
    no sentence: a failure inside the protocol, and a transport failure, each
    naming the call the operator made instead of the library class.
    """
    if capture.status in (401, 403):
        if _is_not_offered(capture):
            body = _auth_body(capture)
            return _Outcome(EXIT_TOOL, body, _auth_sentence(body, capture))
        body = _auth_body(capture)
        return _Outcome(EXIT_AUTH, body, _auth_sentence(body, capture))
    kinds = tuple(_walk_exceptions(exc))
    if any(isinstance(e, _AskedAgain) for e in kinds):
        message = _frame(["orbit: ", _Host(str(tool)), " asked for confirmation again "
                          "after it was answered, so the call was not made; next: retry "
                          "the call, and report it if it recurs"])
        return _Outcome(EXIT_TOOL, {"error": "tool", "message": message}, message)
    # The internal type names belong to the maintainer, not the operator.
    # Debug sits below the warning threshold, so the command line's last
    # resort never renders them at the reader.
    if any(isinstance(e, MCPError) for e in kinds):
        found = next(e for e in kinds if isinstance(e, MCPError))
        log.debug("orbit: protocol failure in %s: %s", tool, type(found).__name__)
        if tool is None:
            message = _frame(["orbit: the listing failed inside the protocol; "
                              "next: retry the command"])
        else:
            message = _frame(["orbit: ", _Host(tool),
                              " failed inside the protocol; next: retry the call"])
        return _Outcome(EXIT_TRANSPORT, {"error": "transport", "message": message},
                        message)
    if any(isinstance(e, httpx2.HTTPError) for e in kinds):
        found = next(e for e in kinds if isinstance(e, httpx2.HTTPError))
        log.debug("orbit: transport failure in %s: %s", tool, type(found).__name__)
        if tool is None:
            message = _frame(["orbit: the listing failed on the transport; "
                              "next: retry the command"])
        else:
            message = _frame(["orbit: ", _Host(tool),
                              " failed on the transport; next: retry the call"])
        return _Outcome(EXIT_TRANSPORT, {"error": "transport", "message": message},
                        message)
    log.debug("orbit: unexpected failure in %s: %s", tool, type(exc).__name__)
    # The tool name is the caller's own text, so it goes through the one
    # writer with every other framed value: a name long enough to spend the
    # whole line would push this sentence's own last part -- the thing to do
    # next -- off the end of it.
    if tool is None:
        message = _frame(["orbit: the listing answered in a shape this desk does not "
                          "understand; next: update the desk and retry"])
    else:
        message = _frame(["orbit: ", _Host(tool), " answered in a shape "
                          "this desk does not understand; next: update the desk and retry"])
    return _Outcome(EXIT_TOOL, {"error": "tool", "message": message}, message)


#: What the protocol library says when a tool's arguments fail its schema: the
#: refusal never reaches the tool, so it carries no envelope, only this text.
_VALIDATION_MARK = "validation error"


def _envelope_for_bare_error(tool: str, result: Any) -> dict[str, Any]:
    """The documented failure envelope for an error result that arrived without
    one. The surface's own refusals carry `{code, what_failed, ...}` as their
    structured content; an argument that fails the tool's schema is refused by
    the protocol library before the tool runs, as an error result whose only
    content is a sentence. Left as it was, the desk printed the empty document
    and `failed: None` (the second stranger session, 2026-09-12), which gives an
    operator nothing to act on. The sentence becomes `what_failed`; a schema
    refusal is `invalid_arguments`, a usage error, and anything else keeps the
    tool-failure code."""
    texts = [str(getattr(part, "text", "") or "") for part in (getattr(result, "content", None) or [])]
    text = " ".join(t.strip() for t in texts if t and t.strip()).strip()
    if _VALIDATION_MARK in text.lower():
        return {
            "code": "invalid_arguments",
            "what_failed": text or "the arguments did not match the tool's schema",
            "evidence": {},
            "next_action": "pass the arguments the tool's description names, as one JSON object under --json",
            "retryable": False,
        }
    return {
        "code": "tool_error",
        "what_failed": text or "the surface reported an error without saying what failed",
        "evidence": {},
        "next_action": "read the tool's description and call again; report the text if it recurs",
        "retryable": False,
    }


def _error_exit(payload: Any) -> int:
    """The exit for an error tool result, read from the refusal's code.

    Refusals arrive in the protocol now, as a result marked as an error
    carrying the documented envelope — there is no status left to read.
    FORBIDDEN is an authentication failure as far as the operator is
    concerned; anything else stays a tool failure, notably UNKNOWN_TOOL,
    where no credential would make the call work.
    """
    if isinstance(payload, dict) and payload.get("code") == "FORBIDDEN":
        return EXIT_AUTH
    if isinstance(payload, dict) and payload.get("code") == "invalid_arguments":
        return EXIT_USAGE
    return EXIT_TOOL


#: The fields a working answer carries into the summary, each written as one
#: `key=value` part after the status, in the order an operator reads them.
#: This tuple is the decision itself: the loop below prints these and nothing
#: else, and the marks the desk protects are taken from here rather than kept
#: alongside. Add a field here and the summary writes it and the desk owns it
#: in the same edit -- against the condition on every mark stated at
#: `_DESK_MARK_SHAPES`: a field whose name begins with `x`, `u` or `U` would
#: give the desk a word mark that opens a byte's name, and its label on the
#: screen could not be told from the byte a host typed.
_SUMMARY_FIELDS = ("version", "code", "what_failed")

#: Every word mark of the desk's, assembled from the three places that decide
#: one: the refused-token sentence's role clauses, one label per field the
#: summary writes, and the clause a refused address is named outside. The
#: summary's half is derived because a hand-written copy of it was the defect
#: -- a field added to the loop and forgotten in the copy let a host put that
#: label inside a value, and an operator read the host's half of the line as
#: the desk's own.
_DESK_WORD_MARKS = (_DESK_ROLE_MARKS + tuple(f"{key}=" for key in _SUMMARY_FIELDS)
                    + (_DESK_ORIGIN_CLAUSE,))


def _summary(name: str, data: Any) -> str:
    """The human line for a tool answer that worked.

    The frame is the desk's own: the tool's name, a colon, the status, then
    one `key=value` part per carried field. The values -- the operator's tool
    name and the host's four -- all go through the one writer, so a host that
    types `code=` or `what_failed=` is quoting itself, not writing another
    part. A payload without a status has no frame to forge, so it is rendered
    by the document encoder, whose separators hold by construction."""
    if isinstance(data, dict):
        if "status" in data:
            segments: list[str | _Host] = [_Host(str(data["status"]))]
            for key in _SUMMARY_FIELDS:
                if data.get(key) not in (None, ""):
                    segments += [" ", key + "=", _Host(str(data[key]))]
            return _frame([_Host(name), ": "] + segments)
        return _frame([_Host(name), ": ",
                       _Host(json.dumps(data, separators=(',', ':'), default=str))])
    return _frame([_Host(name), ": ", _Host(str(data))])


def _as_dict(value: Any) -> Any:
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")
    return value


# Whether the in-flight `call` asked for machine output. Read where the
# confirmation question is asked; set by the call command around its
# round-trip. A variable rather than a parameter because the seam between
# the two is six arguments wide and covered by doubles written against it.
_asking_machine: contextvars.ContextVar[bool] = contextvars.ContextVar(
    "orbit_asking_machine", default=False)


def _confirmation_lines(result: InputRequiredResult) -> tuple[str, ...]:
    """The question the operator must answer: the tool's own words first,
    then the desk's prompt. One string per line for the writer to render —
    the conversation stays as it is, but what it writes comes from the
    writer, so a caller who asked for machine output is asked in a form it
    can parse."""
    lines = []
    for req in (result.input_requests or {}).values():
        params = getattr(req, "params", None)
        message = getattr(params, "message", None)
        if message:
            # The tool's words are the host's: rendered by the one writer, so a
            # host cannot paint the screen or forge a desk line at the moment
            # the operator is about to type an answer.
            lines.append(_frame([_Host(str(message))]))
    # Spelled with marks the desk already declares, so a host cannot type this
    # line and be believed.
    lines.append("confirm (y/N):")
    return tuple(lines)


def _resolve_confirm(*, yes: bool, no: bool, stdin: TextIO, machine: bool,
                     question: tuple[str, ...]) -> bool | None:
    """True/False for the confirm form, or None when the caller must pass --yes/--no.

    The None case stays silent here; the call site reports it through
    write_outcome, so it follows what the caller asked for like every other
    `call` failure. The interactive question goes through the writer too,
    so its form follows what the caller asked for rather than which path
    is asking.
    """
    if yes:
        return True
    if no:
        return False
    if stdin.isatty():
        write_outcome(
            _Outcome(EXIT_USAGE, {"error": "usage",
                                  "message": "orbit: confirmation required",
                                  "question": list(question)},
                     question),
            machine=machine)
        line = stdin.readline()
        return line.strip().lower() in {"y", "yes"}
    return None


async def _call_with_confirmation(client: Client, tool: str, arguments: dict[str, Any],
                                  yes: bool, no: bool, stdin: TextIO):
    """First round may be `InputRequiredResult`; retry with `input_responses` + `request_state`.
    Returns None when a non-tty caller omitted `--yes`/`--no`."""
    result = await client.session.call_tool(tool, arguments, allow_input_required=True)
    if not isinstance(result, InputRequiredResult):
        return result
    accepted = _resolve_confirm(yes=yes, no=no, stdin=stdin,
                                machine=_asking_machine.get(),
                                question=_confirmation_lines(result))
    if accepted is None:
        return None
    responses = {"confirm": ElicitResult(action="accept", content={"confirm": accepted})}
    answered = await client.session.call_tool(
        tool, arguments, input_responses=responses, request_state=result.request_state,
    )
    if isinstance(answered, InputRequiredResult):
        raise _AskedAgain(tool)
    return answered


async def _discover_token_endpoint(http: httpx2.AsyncClient, mcp_url: str,
                                   hinted: str | None) -> str:
    """The untrusted candidate token endpoint: the pasted blob's hint first
    (bootstrap tells the operator to pipe it into the sign-in), then the
    authorization-server document, then the typed origin's default. Nothing is
    pinned here; the caller passes the result through _pin_token_endpoint
    immediately before the post."""
    if hinted and hinted.strip():
        return hinted
    origin = origin_of(mcp_url)
    try:
        r = await http.get(origin + "/.well-known/oauth-authorization-server")
        if r.status_code != 200:
            return origin + "/token"
        body = r.json()
    except (httpx2.HTTPError, ValueError):
        return origin + "/token"
    if isinstance(body, dict) and isinstance(body.get("token_endpoint"), str):
        return body["token_endpoint"]
    return origin + "/token"


def _is_bare_host(mcp_url: str) -> bool:
    return urlparse(mcp_url).path in ("", "/")


async def _discover_resource(http: httpx2.AsyncClient, mcp_url: str) -> tuple[str | None, str | None]:
    """RFC 9728: the host names the exact address it issues tokens for.

    Best effort — a host with no such document keeps today's behaviour, so
    only a short absolute http(s) `resource` on the typed origin is ever
    returned; anything else falls back to the address the person typed. A
    refusal the desk makes on the person's behalf is returned beside the
    fallback for the command's outcome, so the reader is told through the
    one writer: printed beside it, it is a second way out, and swallowed,
    it reads as a bare failure from a decision the desk made."""
    try:
        r = await http.get(origin_of(mcp_url) + "/.well-known/oauth-protected-resource")
        if r.status_code != 200:
            return None, None
        body = r.json()
    except (httpx2.HTTPError, ValueError):
        return None, None
    if not isinstance(body, dict):
        return None, None
    raw = body.get("resource")
    accepted = _accept_discovered(raw, mcp_url)
    if accepted is None:
        if isinstance(raw, str) and raw.strip():
            origin = origin_of(mcp_url)
            return None, _frame(["orbit: refusing resource ", _Host(raw),
                                 " " + _DESK_ORIGIN_CLAUSE, _Host(origin),
                                 "; using ", _Host(mcp_url)])
        return None, None
    try:
        return canonical_mcp_url(accepted), None
    except ValueError:
        return None, None


async def cmd_login(url: str, stdin: TextIO, factory: HttpClientFactory | None,
                    machine: bool = False) -> int:
    _ensure_output_wrapped()
    notices: list[str] = []

    def report(outcome: _Outcome) -> int:
        """Hand an outcome over with the desk's own notices first: a refusal
        the desk made on the person's behalf is part of what the writer
        renders, never a second write beside it."""
        if notices:
            lines = outcome.message if isinstance(outcome.message, tuple) else (outcome.message,)
            payload = outcome.payload
            if isinstance(payload, dict):
                payload = {**payload, "notices": list(notices)}
            outcome = _Outcome(outcome.exit_code, payload, (*notices, *lines))
        return write_outcome(outcome, machine=machine)

    try:
        mcp_url = canonical_mcp_url(url)
        client_id, secret, hinted = _credentials(stdin)
    except ValueError as e:
        message = _desk_sentence(e)
        return report(
            _Outcome(EXIT_USAGE, {"error": "usage", "message": message}, message))
    capture = _AuthCapture()
    try:
        async with _open_http({}, factory, mcp_url, capture) as http:
            if _is_bare_host(mcp_url):
                # A person hands the desk a host, not an interface address.
                # The host's resource document names the address the server
                # issues tokens for; an explicit path is used exactly as typed.
                discovered, refused = await _discover_resource(http, mcp_url)
                if refused is not None:
                    notices.append(refused)
                if discovered is not None:
                    mcp_url = discovered
            token_url = _pin_token_endpoint(
                await _discover_token_endpoint(http, mcp_url, hinted), mcp_url)
            response = await http.post(
                token_url,
                data={
                    "grant_type": "client_credentials",
                    "client_id": client_id,
                    "client_secret": secret,
                    "resource": mcp_url,
                },
            )
    except httpx2.HTTPError as e:
        # The same shape the call route keeps: the step that failed, where the
        # failure came from, and what to do next. The internal type name goes to
        # the debug log, below the warning threshold, never to the operator.
        log.debug("orbit: transport failure signing in at %s: %s", mcp_url,
                  type(e).__name__)
        # No address in the sentence: the one writer neutralises the port's
        # colon as a desk mark, so a host and port render unreadably, and the
        # operator typed the address anyway.
        message = _frame(["orbit: the sign-in failed on the transport: the host did not "
                          "answer; next: check the address and that the host is up, "
                          "then retry"])
        return report(
            _Outcome(EXIT_TRANSPORT, {"error": "transport", "message": message},
                     message))
    if response.status_code in (401, 403):
        capture.status = response.status_code
        try:
            capture.body = response.json() if isinstance(response.json(), dict) else None
        except (json.JSONDecodeError, ValueError):
            capture.body = None
        capture.www = response.headers.get("www-authenticate", "")
        return report(_auth_outcome(capture))
    if response.status_code >= 400:
        try:
            refused = response.json()
        except (json.JSONDecodeError, ValueError):
            refused = None
        if isinstance(refused, dict):
            detail = refused.get("error_description") or refused.get("error")
            if detail:
                message = _frame(["orbit: token endpoint returned ",
                                  _Host(str(response.status_code)), ": ",
                                  _Host(str(detail))])
                return report(
                    _Outcome(EXIT_TRANSPORT, {"error": "transport", "message": message},
                             message))
        message = f"orbit: token endpoint returned {response.status_code}"
        return report(
            _Outcome(EXIT_TRANSPORT, {"error": "transport", "message": message},
                     message))
    try:
        body = response.json()
        token = body["access_token"]
        role = _role_from_token(token, body.get("scope"))
        expires_at = _expires_at(token, body.get("expires_in"))
    except (KeyError, TypeError, ValueError, OverflowError, OSError, jwt.PyJWTError,
            json.JSONDecodeError) as e:
        # The host answered, and what came back is not a token this desk can
        # read: no object, no token, an expiry no clock holds, or no expiry. The operator is told that, and what to check; the internal type
        # name stays on the debug log, where the address it came from is too.
        log.debug("orbit: unusable token response from %s: %s", token_url,
                  type(e).__name__)
        message = _frame(["orbit: the sign-in failed on the host's answer: it carried no "
                          "token this desk can use; next: check the address names an Orbit "
                          "host, then retry"])
        return report(
            _Outcome(EXIT_TRANSPORT, {"error": "transport", "message": message},
                     message))
    _write_session(session_path(mcp_url), {
        "url": mcp_url, "token": token,
        "claimed_expires_at": expires_at, "claimed_role": role,
    })
    return report(
        _Outcome(EXIT_OK,
                 {"url": mcp_url, "claimed_role": role,
                  "claimed_expires_at": expires_at},
                 (_frame(["role: ", _Host(role), " (", CLAIM_MARKER, " by token)"]),
                  _frame(["expires_at: ", _Host(expires_at),
                          " (", CLAIM_MARKER, " by token)"]))))


async def _with_mcp(session: dict[str, Any], factory: HttpClientFactory | None,
                    capture: _AuthCapture):
    url = session["url"]
    headers = {"Authorization": f"Bearer {session['token']}"}
    http = _open_http(headers, factory, url, capture)
    return http, streamable_http_client(url, http_client=http)


async def cmd_tools(url: str | None, factory: HttpClientFactory | None,
                    machine: bool = False) -> int:
    _ensure_output_wrapped()
    try:
        session = load_session(url)
    except (FileNotFoundError, ValueError) as e:
        message = _desk_sentence(e)
        return write_outcome(
            _Outcome(EXIT_USAGE, {"error": "usage", "message": message}, message),
            machine=machine)
    except OSError as e:
        message = _desk_sentence(e)
        return write_outcome(
            _Outcome(EXIT_TRANSPORT, {"error": "transport", "message": message},
                     message),
            machine=machine)
    capture = _AuthCapture()
    try:
        http, transport = await _with_mcp(session, factory, capture)
        async with http:
            async with Client(transport) as client:
                listed = await client.list_tools()
    except Exception as e:
        return write_outcome(_call_failure(e, capture), machine=machine)
    lines = []
    for tool in listed.tools:
        # Two lines per entry, each through the one writer: the name first, so
        # a reader taking the first word of each entry still lands on the tool,
        # then the tool's own sentence indented under it. Both halves are the
        # host's text -- a hostile name is what the contract drives, and the
        # sentence beside it is the host's just the same -- so both are the
        # writer's, and one long entry still cannot spend the rest of the list.
        lines.append(_frame([_Host(tool.name)]))
        sentence = _first_sentence(tool.description or "")
        if sentence:
            lines.append(_frame(["  ", _Host(sentence)]))
        # The argument names, required ones first and optional ones in brackets: what
        # `orbit call <tool> --arg NAME=VALUE` takes. A desk user had no way to see them
        # but trial and error (QA of v1.0.0, F8).
        names = _argument_names(_input_schema(tool))
        if names:
            lines.append(_frame(["  arguments: ", _Host(" ".join(names))]))
    return write_outcome(
        _Outcome(EXIT_OK,
                 {"tools": [{"name": tool.name, "description": tool.description or "",
                             # the tool's own JSON Schema for its arguments, as the server sent it
                             "input_schema": _input_schema(tool)}
                            for tool in listed.tools]},
                 tuple(lines)),
        machine=machine)


def _input_schema(tool: Any) -> dict[str, Any]:
    schema = getattr(tool, "input_schema", None)
    if schema is None:
        schema = getattr(tool, "inputSchema", None)
    return schema if isinstance(schema, dict) else {}


def _argument_names(schema: dict[str, Any]) -> list[str]:
    """`project`, `env`, `[dry_run]`: required arguments by name, optional ones bracketed,
    each in the schema's own order."""
    properties = schema.get("properties")
    if not isinstance(properties, dict):
        return []
    required = schema.get("required") if isinstance(schema.get("required"), list) else []
    names = [str(n) for n in properties if isinstance(n, str)]
    return [n for n in names if n in required] + [f"[{n}]" for n in names if n not in required]


async def cmd_call(url: str | None, tool: str, arg_flags: list[str], json_flag: Any,
                   yes: bool, no: bool, stdin: TextIO,
                   factory: HttpClientFactory | None) -> int:
    _ensure_output_wrapped()
    # Whether the caller asked for machine output is known before any value
    # is parsed, so even a bad --json value leaves by the writer as a
    # document rather than falling back to a sentence.
    machine = json_flag is not False
    try:
        session = load_session(url)
        arguments: dict[str, Any] = {}
        if isinstance(json_flag, str):
            parsed = json.loads(json_flag)
            if not isinstance(parsed, dict):
                raise ValueError("--json arguments must be a JSON object")
            arguments.update(parsed)
        arguments.update(_parse_args_list(arg_flags))
    except json.JSONDecodeError:
        message = "orbit: --json value is not valid JSON"
        return write_outcome(
            _Outcome(EXIT_USAGE, {"error": "usage", "message": message}, message),
            machine=machine)
    except (FileNotFoundError, ValueError) as e:
        message = _desk_sentence(e)
        return write_outcome(
            _Outcome(EXIT_USAGE, {"error": "usage", "message": message}, message),
            machine=machine)
    except OSError as e:
        message = _desk_sentence(e)
        return write_outcome(
            _Outcome(EXIT_TRANSPORT, {"error": "transport", "message": message},
                     message),
            machine=machine)
    capture = _AuthCapture()
    mark = _asking_machine.set(machine)
    try:
        http, transport = await _with_mcp(session, factory, capture)
        async with http:
            async with Client(transport) as client:
                result = await _call_with_confirmation(
                    client, tool, arguments, yes, no, stdin)
    except Exception as e:
        return write_outcome(_call_failure(e, capture, tool), machine=machine)
    finally:
        _asking_machine.reset(mark)
    if result is None:
        message = "orbit: confirmation required; pass --yes or --no"
        return write_outcome(
            _Outcome(EXIT_USAGE, {"error": "usage", "message": message}, message),
            machine=machine)
    payload = _as_dict(result.structured_content)
    if isinstance(payload, dict) and (
            payload.get("error") or payload.get("status") in {"refused", "failed"}):
        return write_outcome(
            _Outcome(EXIT_TOOL, payload, _refusal_sentence(tool, payload)),
            machine=machine)
    if result.is_error:
        if not isinstance(payload, dict):
            payload = _envelope_for_bare_error(tool, result)
        return write_outcome(
            _Outcome(_error_exit(payload), payload, _refusal_sentence(tool, payload)),
            machine=machine)
    return write_outcome(
        _Outcome(EXIT_OK, payload, _summary(tool, payload)), machine=machine)


def _writer_owns_usage(argv: list[str]) -> bool:
    """Whether a parse failure on this command line is this node's business:
    the three commands the one writer serves. A bare tool name rewrites to
    `call`, so it counts too. The platform and secrets groups are a node of
    their own with exits of their own; their usage errors keep the parser's
    own rendering."""
    for a in argv:
        if a.startswith("-"):
            continue
        return a in {"login", "tools", "call"} or a not in _BUILTINS
    return True


def _usage_error(argv: list[str], captured: str) -> int:
    """The argument parser's own failure as an outcome, handed to the one
    writer like any other: a missing or bad argument otherwise writes the
    parser's text itself, ignoring what the caller asked for. The parser's
    error line is the message; the full usage block stays out of the
    operator's stream the way every other failure's internals do. Whether
    the caller asked for machine output is read off the raw arguments,
    which is all that survived the failure. The exit stays the parser's."""
    machine = any(a == "--json" or a.startswith("--json=") for a in argv)
    # The parser's error line is the one that says "error:"; everything from it to
    # the end is the message, line breaks included, because the argument it quotes
    # is anyone's text and a line break typed into it must not pick which line the
    # operator sees. The rendering names the break; the usage block before it stays
    # out. (Reading the last non-blank line once let `--x=a\nFAKE` print "FAKE".)
    sentence = ""
    at = captured.find(": error: ")
    if at >= 0:
        start = captured.rfind("\n", 0, at) + 1
        # Up to the marker is the parser's own (the program name), and so is
        # its phrase up to the first colon after it ("unrecognized arguments",
        # "the following arguments are required", "argument --json"); what
        # follows that colon quotes what was typed, which is anyone's text, so
        # it is a host value to the one writer like every other.
        rest = captured[at + len(": error: "):].strip()
        cut = rest.find(": ")
        if cut >= 0:
            sentence = _frame([captured[start:at + len(": error: ")] + rest[:cut + 2],
                               _Host(rest[cut + 2:])])
        else:
            sentence = _frame([captured[start:at + len(": error: ")], _Host(rest)])
    if not sentence:
        sentence = "orbit: bad arguments"
    return write_outcome(
        _Outcome(EXIT_USAGE, {"error": "usage", "message": sentence}, sentence),
        machine=machine)


async def async_main(argv: list[str] | None = None, *, stdin: TextIO | None = None,
                     http_client_factory: HttpClientFactory | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    _ensure_output_wrapped()
    _ensure_logging_configured()
    if _writer_owns_usage(argv):
        try:
            with contextlib.redirect_stderr(io.StringIO()) as captured:
                args = parse_argv(argv)
        except SystemExit as e:
            if e.code is None or e.code == 0:
                return 0
            return _usage_error(argv, captured.getvalue())
    else:
        try:
            args = parse_argv(argv)
        except SystemExit as e:
            return 0 if e.code is None else int(e.code)
    stream = sys.stdin if stdin is None else stdin
    if args.command == "login":
        return await cmd_login(args.url, stream, http_client_factory, args.json)
    if args.command == "tools":
        return await cmd_tools(args.url, http_client_factory, args.json)
    if args.command == "call":
        return await cmd_call(args.url, args.tool, args.arg, args.json,
                              args.yes, args.no, stream, http_client_factory)
    if args.command == "platform":
        from .platform import cmd_plan
        from .platform_apply import cmd_apply, cmd_release_owners, cmd_retire
        if args.platform_command == "plan":
            return cmd_plan(args)
        if args.platform_command == "apply":
            return cmd_apply(args, stdin=stream)
        if args.platform_command == "release-owners":
            return cmd_release_owners(args)
        if args.platform_command == "retire":
            return cmd_retire(args, stdin=stream)
        print("orbit platform: unknown command", file=sys.stderr)
        return EXIT_USAGE
    if args.command == "secrets":
        from .secrets import cmd_secrets
        return cmd_secrets(args)
    if args.command == "onboard":
        from .onboard import cmd_onboard
        return cmd_onboard(args)
    if args.command == "check":
        from .checks_cli import cmd_check
        return cmd_check(args)
    if args.command == "features":
        from .features_cli import THROUGH_ORBITD, cmd_features, tool_call
        if args.features_command in THROUGH_ORBITD:
            tool, flags = tool_call(args)
            return await cmd_call(args.url, tool, flags, args.json, getattr(args, "yes", False),
                                  getattr(args, "no", False), stream, http_client_factory)
        return cmd_features(args)
    print("orbit: unknown command", file=sys.stderr)
    return EXIT_USAGE


def main(argv: list[str] | None = None) -> int:
    try:
        return anyio.run(async_main, list(sys.argv[1:] if argv is None else argv))
    except SystemExit as e:
        return 0 if e.code is None else int(e.code)


if __name__ == "__main__":
    sys.exit(main())
