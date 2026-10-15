"""Orbit agent runtime — the SDK image every agent runs on (AGENT-PLANE.md).

It speaks the small A2A HTTP contract (an agent card + a task endpoint) and runs the
task on the Claude Agent SDK, authenticating with the org's subscription setup-token
(CLAUDE_CODE_OAUTH_TOKEN) — never an API key. This image is the whole brain: kagent was
dropped 2026-08-27, so nothing else supplies a model layer.

Deliberately standard-library HTTP, like the hooks receiver: the SDK is the only heavy
dependency, and a small dependency surface on a pod that holds a subscription token is
the point.

THE THREAT MODEL, because it drives most of what follows. This pod holds a credential
that bills the org and a model that will run tools. Anything that can POST to it can
spend the org's money and reach whatever the model can reach. So:

  * CALLER auth is separate from MODEL auth. A bearer token, mounted by the platform,
    gates every POST. Absent config = refuse, never run open.
  * The model gets NO built-in tools by default and never `bypassPermissions`. The
    agent's real tool surface arrives later through agentgateway's MCP bundles, which
    is where AGENT-PLANE.md's write policy is actually enforced. Until then the
    built-in Bash/Write/Edit surface is denied outright.
  * The CLI subprocess inherits os.environ, and the CLI honours several vars that
    redirect model traffic or swap the credential (ANTHROPIC_BASE_URL and friends).
    They are stripped from this process at import, before anything can spawn.
  * Errors never return a traceback to a caller; the caller gets {code, message}.

Every task is one OTel span so the console's run history is real.
"""
import hmac
import json
import os
import signal
import sys
import threading
import time
import traceback
import urllib.request
from collections import OrderedDict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# --------------------------------------------------------------- environment hygiene
# The CLI subprocess inherits this process's environment, and every one of these either
# swaps the credential or redirects where model traffic goes — ANTHROPIC_BASE_URL alone
# is enough to send the token and every prompt to an attacker's host. They are stripped
# HERE, at import, because ClaudeAgentOptions.env only *merges over* os.environ
# ({**os.environ, **options.env}) and therefore cannot delete an inherited variable.
# Removing them from this process is the only thing that actually guarantees absence.
DENIED_ENV = (
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_AUTH_TOKEN",
    "ANTHROPIC_BASE_URL",
    "ANTHROPIC_CUSTOM_HEADERS",
    "ANTHROPIC_DEFAULT_HEADERS",
    "CLAUDE_CODE_USE_BEDROCK",
    "CLAUDE_CODE_USE_VERTEX",
    "ANTHROPIC_BEDROCK_BASE_URL",
    "ANTHROPIC_VERTEX_BASE_URL",
    "AWS_BEARER_TOKEN_BEDROCK",
    # Finding #3: swapping the credential is not the only way to redirect the model call.
    # A proxy var reroutes the HTTPS to the model (and with it the subscription token and
    # every prompt); a CA-bundle override or NODE_TLS_REJECT_UNAUTHORIZED lets that proxy's
    # certificate validate; NODE_OPTIONS injects `--require <module>` into the CLI's own
    # Node process; CLAUDE_CONFIG_DIR repoints the CLI at attacker-chosen config. None of
    # these has any legitimate value inside this pod, so all are stripped before spawn — the
    # SAME set the intake refuses on a Job env (services/intake/app.py allowlist), so both
    # layers agree. HOME and CLAUDE_CODE_OAUTH_TOKEN are deliberately NOT here: this process
    # needs both (HOME for the SDK config dir, the token as the sanctioned credential); the
    # intake forbids them only as config-supplied Job LITERALS, which is a different axis.
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "ALL_PROXY",
    "NO_PROXY",
    "SSL_CERT_FILE",
    "SSL_CERT_DIR",
    "REQUESTS_CA_BUNDLE",
    "CURL_CA_BUNDLE",
    "NODE_EXTRA_CA_CERTS",
    "NODE_OPTIONS",
    "NODE_TLS_REJECT_UNAUTHORIZED",
    "CLAUDE_CONFIG_DIR",
)


def _scrub_environment() -> list:
    """Strip and remember the denied vars. Must run before anything spawns a process.

    Matched CASE-INSENSITIVELY: the CLI's Node/undici honours a lowercase `http_proxy` as
    readily as HTTP_PROXY, so an exact-name pop would leave the lowercase spelling behind.
    (Unreachable through the intake today — its ENV_RE demands uppercase on a closed schema
    — but this layer must not depend on that.)"""
    denied_upper = {name.upper() for name in DENIED_ENV}
    seized = []
    for name in list(os.environ):
        if name.upper() in denied_upper:
            os.environ.pop(name, None)
            seized.append(name)
    return seized


DENIED_PRESENT = _scrub_environment()

AGENT_NAME = os.environ.get("ORBIT_AGENT_NAME", "orbit-agent")
AGENT_PROMPT = os.environ.get("ORBIT_AGENT_PROMPT", "You are an Orbit platform agent.")
# Sonnet 4.6 is the default for every agent — cheapest of the capable tier and still
# strong (the owner, 2026-08-27). A catalog `agent:` may override per-agent (Haiku for
# cheap sweeps, Opus for hard investigations); this is the floor everything inherits.
MODEL = os.environ.get("ORBIT_AGENT_MODEL", "claude-sonnet-4-6")
# The task text for a SCHEDULED (one-shot / CronJob) run. AGENT_PROMPT above is the
# agent's standing duty (the system prompt); a scheduled run still needs a task to act on,
# and — unlike a deployment agent — there is no caller to supply one. So the one-shot
# entrypoint uses a fixed scheduled-run instruction, overridable per-pod via
# ORBIT_AGENT_TASK. That override is POD CONFIG, never caller-reachable: one-shot has no
# socket, and the value is read here at import, not from any request. The generator does
# not set ORBIT_AGENT_TASK today, so a scheduled agent runs the default below.
SCHEDULED_TASK = (os.environ.get("ORBIT_AGENT_TASK")
                  or "Perform your scheduled task now, following your system instructions.")
# One-shot mode is selected ONCE, here, from the environment — never from a request. When
# set, __main__ runs exactly one task and exits; the HTTP server is never started. The
# generator sets ORBIT_AGENT_ONESHOT=1 in the CronJob pod spec (scripts/catalog-sync.py);
# it is a pod env var, not a field any caller controls, and reading it a single time at
# import means no in-flight request can toggle the entrypoint's behaviour.
# Truthy ONLY for an explicit on-value: `bool(os.environ.get(...))` is True for the string
# "0" or "false", so a pod that sets ORBIT_AGENT_ONESHOT=0 to DISABLE one-shot would have
# been silently flipped into it. Compare against an explicit set instead.
ONESHOT = os.environ.get("ORBIT_AGENT_ONESHOT", "").strip().lower() in {"1", "true", "yes", "on"}
PORT = int(os.environ.get("PORT", "8080"))
OTLP = os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT")  # set by the platform; optional
HOME = os.environ.get("HOME", "/home/orbit")
# Where the core skills library is mounted, as one directory per skill. `user` scope
# (i.e. $HOME/.claude/skills) rather than a project tree, because an agent pod has no
# checkout — the skills ARE its working knowledge.
SKILLS_DIR = os.environ.get("ORBIT_SKILLS_DIR", os.path.join(HOME, ".claude", "skills"))
# The model runs here — NOT in $HOME, which is the SDK's own config directory. If a
# tool is ever allowed, its blast radius should not start inside ~/.claude.
WORKDIR = os.environ.get("ORBIT_AGENT_WORKDIR", os.path.join(HOME, "work"))

# ------------------------------------------------------------------- budget / brakes
# AGENT-PLANE.md lists per-agent budget caps under "Brakes (product features, not
# afterthoughts)". Spend caps proper need the console; these are the floor that stops
# one request from running forever or one pod from spawning unbounded CLI processes.
MAX_TURNS = int(os.environ.get("ORBIT_AGENT_MAX_TURNS", "12"))
TASK_TIMEOUT_S = float(os.environ.get("ORBIT_AGENT_TIMEOUT_S", "300"))
MAX_CONCURRENCY = int(os.environ.get("ORBIT_AGENT_MAX_CONCURRENCY", "2"))
_SLOTS = threading.BoundedSemaphore(MAX_CONCURRENCY)

# Built-in tools. Empty by default and deliberately so: the agent's sanctioned tool
# surface is agentgateway's MCP bundles (AGENT-PLANE.md, "Tool bundles"), where the
# per-tool rules and the ask-gates live. The SDK's own built-ins bypass all of that, so
# they are denied outright rather than left to a permission mode.
#
# The list is the FILESYSTEM and NETWORK read/write surface, not just the obvious
# writers. This pod holds a 1-year subscription token in /proc/self/environ and the
# system prompt is up to 8KB of catalog-controlled text — so "read a file, fetch a URL"
# is a complete exfiltration primitive on its own, and Read/Glob/Grep/WebFetch/WebSearch
# are as dangerous here as Bash. Containment must not rest on an SDK default nothing
# asserts: everything that can touch the filesystem or the network is named. When the
# gateway bundles land, the agent's real tools arrive as MCP servers (mcp__*), which are
# unaffected by this list.
ALLOWED_TOOLS = [t.strip() for t in
                 os.environ.get("ORBIT_AGENT_ALLOWED_TOOLS", "").split(",") if t.strip()]
DISALLOWED_TOOLS = [
    "Bash", "BashOutput", "KillShell",       # shell
    "Write", "Edit", "MultiEdit", "NotebookEdit",  # filesystem writes
    "Read", "Glob", "Grep", "LS", "NotebookRead",  # filesystem reads/lists (token on disk)
    "WebFetch", "WebSearch",                 # network egress with attacker-chosen URL
    "Task", "SlashCommand",                  # sub-agents / command expansion
]

MAX_FIELD = 200  # cap on any string this service echoes back or publishes


def _log(level: str, msg: str, **kw) -> None:
    print(json.dumps({"level": level, "msg": msg, **kw}), file=sys.stderr, flush=True)


# ------------------------------------------------------------------- caller identity

def _read_caller_token() -> str:
    """The bearer every POST must present. Platform-mounted; a file is preferred."""
    path = os.environ.get("ORBIT_AGENT_TOKEN_FILE")
    if path:
        try:
            with open(path, "r", encoding="utf-8") as fh:
                return fh.read().strip()
        except OSError as exc:
            _log("ERROR", "caller token file unreadable", path=path, error=str(exc))
            return ""
    return os.environ.get("ORBIT_AGENT_TOKEN", "").strip()


CALLER_TOKEN = _read_caller_token()


class HttpError(Exception):
    """An error with an HTTP status and a stable machine-readable code."""

    def __init__(self, status: int, code: str, message: str, rpc_code: int = -32000):
        super().__init__(message)
        self.status = status
        self.code = code
        self.rpc_code = rpc_code


def authenticate(headers) -> None:
    """Gate every POST on the caller's bearer. Fails closed when unconfigured.

    An unconfigured token used to mean "no check", which made every in-cluster peer an
    operator of this agent. Absent config is now a refusal: a misconfigured deploy is
    inert, not open.
    """
    if not CALLER_TOKEN:
        raise HttpError(503, "caller_auth_unconfigured",
                        "ORBIT_AGENT_TOKEN/_FILE is not configured — refusing to serve "
                        "tasks rather than accept unauthenticated callers.")
    header = headers.get("Authorization", "")
    presented = header[7:] if header.startswith("Bearer ") else ""
    # Constant-time: a timing oracle on a shared cluster network is a real one.
    if not hmac.compare_digest(presented.encode(), CALLER_TOKEN.encode()):
        raise HttpError(401, "unauthenticated",
                        "a valid Bearer credential is required", -32001)


# --------------------------------------------------------------------- skills mount

def _contained(path: str) -> bool:
    """True when `path` really lives under SKILLS_DIR — symlinks resolved.

    Without this a mounted skill can point its SKILL.md at /var/run/secrets/... and
    this service will publish the contents on the agent card.
    """
    root = os.path.realpath(SKILLS_DIR)
    real = os.path.realpath(path)
    return real == root or real.startswith(root + os.sep)


def _clip(value: str) -> str:
    return " ".join(str(value).split())[:MAX_FIELD]


def _skill_meta(skill_md: str) -> dict:
    """Read `name`/`description` out of a SKILL.md YAML frontmatter block.

    A short parser rather than a pyyaml dependency: frontmatter here is two flat scalar
    keys, and this image's dependency surface is a security property. Only the
    frontmatter is ever read — never the body — and only from a contained path.
    """
    meta = {}
    if not _contained(skill_md):
        return meta
    try:
        with open(skill_md, "r", encoding="utf-8", errors="replace") as fh:
            head = fh.read(8192).split("\n")
    except OSError:
        return meta
    if not head or head[0].strip() != "---":
        return meta
    for line in head[1:]:
        if line.strip() == "---":
            break
        if ":" in line and not line.startswith((" ", "\t", "#")):
            key, _, value = line.partition(":")
            meta[key.strip()] = _clip(value.strip().strip("'\""))
    return meta


def discover_skills() -> list:
    """Every mounted skill: a contained directory under SKILLS_DIR holding a SKILL.md."""
    found = []
    try:
        entries = sorted(os.listdir(SKILLS_DIR))
    except OSError:
        return found
    for entry in entries:
        skill_dir = os.path.join(SKILLS_DIR, entry)
        skill_md = os.path.join(skill_dir, "SKILL.md")
        if not (_contained(skill_dir) and _contained(skill_md)):
            _log("WARN", "skill skipped: escapes the skills mount", skill=_clip(entry))
            continue
        if not os.path.isfile(skill_md):
            continue
        meta = _skill_meta(skill_md)
        found.append({
            "id": _clip(entry),
            "name": meta.get("name") or _clip(entry),
            "description": meta.get("description") or f"mounted skill: {_clip(entry)}",
            "tags": ["skill"],
        })
    return found


# ---------------------------------------------------------------------------- A2A

RPC_METHODS = ("message/send",)


def _card() -> dict:
    # The A2A agent card: who I am and where to send work. Minimal but real. Mounted
    # skills are advertised here, so `curl /.well-known/agent-card.json` is the honest
    # answer to "what can this agent actually do" — no second source of truth. It stays
    # unauthenticated because it IS the discovery contract; nothing secret goes on it,
    # which is what the containment check above is protecting.
    return {
        "protocolVersion": "0.3.0",
        "name": AGENT_NAME,
        "description": "Orbit platform agent on the Claude Agent SDK",
        "version": "1",
        "preferredTransport": "JSONRPC",
        "capabilities": {"streaming": False},
        "defaultInputModes": ["text"],
        "defaultOutputModes": ["text"],
        "securitySchemes": {"bearer": {"type": "http", "scheme": "bearer"}},
        "security": [{"bearer": []}],
        "skills": [{"id": "task", "name": "task", "tags": ["core"],
                    "description": "Run one agent task"}] + discover_skills(),
        "url": f"http://localhost:{PORT}/",
    }


def task_text(body: dict) -> str:
    """Pull the prompt out of an A2A `message/send` — or a plain probe shape.

    Non-text parts are REJECTED rather than concatenated: a file part flattened into
    the prompt is a quiet way to feed an agent something it was never asked to read.
    """
    if isinstance(body.get("text"), str) and body["text"]:
        return body["text"]
    message = ((body.get("params") or {}).get("message") or {})
    if isinstance(message.get("text"), str) and message["text"]:
        return message["text"]
    parts = message.get("parts") or []
    if not isinstance(parts, list):
        raise HttpError(400, "bad_request", "params.message.parts must be a list",
                        -32602)
    chunks = []
    for part in parts:
        if not isinstance(part, dict):
            raise HttpError(400, "bad_request", "each part must be an object", -32602)
        kind = part.get("kind") or part.get("type") or "text"
        if kind != "text":
            raise HttpError(400, "unsupported_part_kind",
                            f"part kind {_clip(kind)!r} is not supported; this agent "
                            "accepts text parts only", -32602)
        chunks.append(part.get("text") or "")
    return "".join(chunks)


class _SeenIds:
    """Bounded replay guard for JSON-RPC request ids.

    A retried webhook must not silently buy a second model run. Bounded so an attacker
    cannot grow it without limit.
    """

    def __init__(self, limit: int = 1024):
        self._ids = OrderedDict()
        self._limit = limit
        self._lock = threading.Lock()

    def claim(self, rpc_id) -> bool:
        key = json.dumps(rpc_id, sort_keys=True)[:MAX_FIELD]
        with self._lock:
            if key in self._ids:
                return False
            self._ids[key] = True
            while len(self._ids) > self._limit:
                self._ids.popitem(last=False)
            return True


SEEN = _SeenIds()


# --------------------------------------------------------------------------- OTel

def _resource_attributes() -> dict:
    """Identity is set BY THE PLATFORM, never by the app (OBSERVABILITY.md dec. 5).

    So the generator's OTEL_SERVICE_NAME / OTEL_RESOURCE_ATTRIBUTES win; the agent name
    is only the fallback for a runtime started outside the generator, and always rides
    along as `orbit.agent` so run history can group by agent regardless of service.name.
    """
    attrs = {}
    for pair in os.environ.get("OTEL_RESOURCE_ATTRIBUTES", "").split(","):
        if "=" in pair:
            key, _, value = pair.partition("=")
            attrs[key.strip()] = value.strip()
    attrs["service.name"] = (os.environ.get("OTEL_SERVICE_NAME")
                             or attrs.get("service.name") or AGENT_NAME)
    attrs["orbit.agent"] = AGENT_NAME
    attrs["orbit.agent.model"] = MODEL
    return attrs


def _emit_span(name: str, ok: bool, detail: str, duration_ns: int = 0,
               extra: dict = None) -> None:
    # One OTLP span per task over plain HTTP — no SDK, no batching. A best-effort
    # sink: a tracing outage must never fail a task, so nothing here raises. It does
    # LOG though — a silent `except: pass` makes a misrouted collector undetectable.
    if not OTLP:
        return
    try:
        end = time.time_ns()
        attributes = [{"key": "orbit.detail", "value": {"stringValue": detail[:MAX_FIELD]}}]
        for key, value in (extra or {}).items():
            attributes.append({"key": key,
                               "value": {"stringValue": str(value)[:MAX_FIELD]}})
        span = {
            "resourceSpans": [{
                "resource": {"attributes": [
                    {"key": k, "value": {"stringValue": v}}
                    for k, v in _resource_attributes().items()]},
                "scopeSpans": [{"spans": [{
                    "traceId": os.urandom(16).hex(), "spanId": os.urandom(8).hex(),
                    "name": name, "kind": 1,
                    "startTimeUnixNano": str(end - duration_ns),
                    "endTimeUnixNano": str(end),
                    "status": {"code": 1 if ok else 2},
                    "attributes": attributes,
                }]}],
            }],
        }
        req = urllib.request.Request(
            OTLP.rstrip("/") + "/v1/traces",
            data=json.dumps(span).encode(),
            headers={"Content-Type": "application/json"}, method="POST")
        urllib.request.urlopen(req, timeout=5).read()
    except Exception as exc:
        _log("WARN", "otel emit failed", span=name,
             endpoint=OTLP, error=f"{type(exc).__name__}: {exc}")


# --------------------------------------------------------------------- model auth

def check_credentials() -> None:
    """The subscription-only gate. Runs before every task, and never falls through.

    AGENT-PLANE.md 'Model auth': the setup-token is the ONLY sanctioned pattern. Any of
    the denied vars would either bill through an unsanctioned path or point the model
    and its credential at another host, so their presence is a refusal, not a fallback.
    They were already stripped at import; this reports what was found.
    """
    if DENIED_PRESENT:
        raise HttpError(
            401, "api_key_not_permitted",
            "refusing to run: " + ", ".join(DENIED_PRESENT) + " was set in this pod. "
            "The agent plane is subscription-only (AGENT-PLANE.md, Model auth) — mount "
            "CLAUDE_CODE_OAUTH_TOKEN from the instance's setup-token secret and remove "
            "the credential/routing overrides.", -32001)
    if not os.environ.get("CLAUDE_CODE_OAUTH_TOKEN"):
        raise HttpError(
            401, "no_subscription_token",
            "CLAUDE_CODE_OAUTH_TOKEN is not set — the agent has no subscription "
            "credential (see ONBOARDING). Refusing to run the task.",
            -32001)


# ------------------------------------------------------------------------ the run

def _sdk_options():
    """Build ClaudeAgentOptions — the security posture of the run lives here.

    SEAM (SDK 0.1.1): there is no `skills=` option. Skills are discovered from the
    settings sources, and `setting_sources` defaults to None — meaning NO filesystem
    settings at all. So a mounted skills directory is INERT unless `user` scope is
    named here. Do not drop it: the mount point would still exist and silently do
    nothing.

    `env` only merges over os.environ and cannot delete, so the empty strings below are
    belt-and-braces (a Node program reads them as falsy); the real guarantee is the
    import-time scrub above.
    """
    from claude_agent_sdk import ClaudeAgentOptions
    return ClaudeAgentOptions(
        system_prompt=AGENT_PROMPT,
        model=MODEL,
        # NOT bypassPermissions. That auto-approves every built-in tool, which would
        # route around the whole tool-bundle/ask-gate design in AGENT-PLANE.md.
        permission_mode="default",
        allowed_tools=ALLOWED_TOOLS,
        disallowed_tools=DISALLOWED_TOOLS,
        max_turns=MAX_TURNS,
        setting_sources=["user"],
        cwd=WORKDIR,
        env={name: "" for name in DENIED_ENV},
    )


def _unknown_stream_message_type(exc):
    """If `exc` is the SDK's *unrecognized streaming message type* parse failure, return
    that message type; otherwise None.

    This is the ONE failure a run tolerates: the server emitted a streaming message (the
    live incident was `rate_limit_event`) that this SDK build does not model — a protocol
    skew, not a task error. Every OTHER MessageParseError is a genuine malformation and
    MUST propagate: a KNOWN message missing a required field ("Missing required field in
    ... message"), a non-dict payload, a missing `type`. So does any non-parse error (a
    tool or transport failure). We match by DUCK TYPE, not isinstance: the SDK does not
    export MessageParseError at top level and its internal module path has moved between
    versions, but the error TEXT is stable — only the unknown-type branch says "Unknown
    message type: <t>", never the missing-field branch.
    """
    if type(exc).__name__ != "MessageParseError":
        return None
    text = str(exc)
    if not text.startswith("Unknown message type"):
        return None
    data = getattr(exc, "data", None)
    if isinstance(data, dict) and data.get("type"):
        return str(data["type"])
    return text.partition(":")[2].strip() or "unknown"


async def _consume_messages(messages, chunks: list, usage: dict) -> None:
    """Drain the SDK's streaming messages into `chunks`/`usage`.

    Belt-and-braces for protocol skew. If the stream carries a message type this SDK
    build does not recognise, the SDK raises MessageParseError from inside its async
    generator — which also CLOSES the generator, so it cannot be resumed. The pinned SDK
    SKIPS unknown types natively and keeps streaming (so in practice this never fires),
    but a future type on an SDK that has not caught up would otherwise abort the whole
    task. When it happens we LOG the type at WARN and let the task COMPLETE with whatever
    was gathered so far, rather than 500-ing the run. Genuine errors — a malformed known
    message, a tool/transport failure — are re-raised unchanged and still fail the task.
    """
    try:
        async for message in messages:
            for block in getattr(message, "content", []) or []:
                if getattr(block, "text", None):
                    chunks.append(block.text)
            # The SDK's terminal ResultMessage carries the spend; AGENT-PLANE.md wants
            # tokens per run on the span, not just duration and outcome.
            if getattr(message, "usage", None):
                usage.update(message.usage or {})
            if getattr(message, "total_cost_usd", None) is not None:
                usage["total_cost_usd"] = message.total_cost_usd
    except Exception as exc:
        message_type = _unknown_stream_message_type(exc)
        if message_type is None:
            raise
        _log("WARN", "skipping unrecognized SDK stream message",
             message_type=message_type, sdk_error=str(exc)[:MAX_FIELD])


def run_task(text: str) -> dict:
    """Run one task on the Agent SDK and return {text, usage}."""
    check_credentials()
    # Imported here so the card/health path never depends on the SDK being importable.
    from claude_agent_sdk import query

    options = _sdk_options()
    chunks = []
    usage = {}

    async def _drive():
        await _consume_messages(query(prompt=text, options=options), chunks, usage)

    import asyncio

    async def _bounded():
        # A hung CLI must not pin a slot forever — an agent with no wall clock is an
        # agent with no budget.
        await asyncio.wait_for(_drive(), timeout=TASK_TIMEOUT_S)

    try:
        asyncio.run(_bounded())
    except asyncio.TimeoutError:
        raise HttpError(504, "task_timeout",
                        f"task exceeded ORBIT_AGENT_TIMEOUT_S={TASK_TIMEOUT_S:g}s")
    return {"text": "".join(chunks), "usage": usage}


# ---------------------------------------------------------------------- one-shot run

def run_oneshot() -> int:
    """Run exactly ONE task and return a process exit code (0 = success, non-zero = fail).

    The scheduled-run entrypoint — the CronJob pod (AGENT-PLANE.md, "How a scheduled run
    triggers"). It reuses the EXACT model path the HTTP handler's `_run` uses: `run_task`,
    which calls `check_credentials()` and builds the same `ClaudeAgentOptions` (the tool
    denylist, `permission_mode="default"`, the brakes), plus the same `_emit_span` so a
    scheduled run shows up in run history like any other. The ONLY thing this path does not
    do is `authenticate()` a caller bearer — because in one-shot there is NO socket and NO
    remote caller, so there is nothing to authenticate. Every MODEL-side gate is intact: a
    missing or blank subscription token (or a credential-routing override) makes
    `check_credentials()` raise, which becomes a NON-ZERO exit here — never a silent
    no-op — so the CronJob records a failed run rather than a green one that did nothing.
    """
    started = time.time_ns()
    try:
        result = run_task(SCHEDULED_TASK)
    except HttpError as err:
        # Mirror _run's span taxonomy: an auth/credential refusal is orbit.auth.error,
        # any other refusal orbit.task.error. Fail closed with exit 1.
        _emit_span("agent.task", False, err.code, time.time_ns() - started,
                   {"orbit.auth.error": err.code} if err.status == 401
                   else {"orbit.task.error": err.code})
        _log("ERROR", "scheduled task refused", code=err.code, error=str(err))
        return 1
    except Exception as exc:
        _emit_span("agent.task", False, type(exc).__name__, time.time_ns() - started)
        _log("ERROR", "scheduled task failed", error=f"{type(exc).__name__}: {exc}",
             trace=traceback.format_exc()[-2000:])
        return 1
    usage = result.get("usage") or {}
    _emit_span("agent.task", True, "ok", time.time_ns() - started,
               {f"orbit.usage.{k}": v for k, v in usage.items()})
    _log("INFO", "scheduled task complete",
         chars=len(result.get("text") or ""), usage=usage)
    return 0


# --------------------------------------------------------------------------- HTTP

class Handler(BaseHTTPRequestHandler):
    timeout = 15  # slow-body DoS guard, same posture as the hooks receiver

    def _send(self, code: int, body: dict) -> None:
        payload = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def _fail(self, rpc_id, err: HttpError) -> None:
        body = {"error": str(err), "code": err.code}
        if rpc_id is not None:
            body = {"jsonrpc": "2.0", "id": rpc_id,
                    "error": {"code": err.rpc_code, "message": str(err),
                              "data": {"code": err.code}}}
        self._send(err.status, body)

    def do_GET(self):
        if self.path == "/healthz":
            self._send(200, {"ok": True})
        elif self.path in ("/.well-known/agent.json", "/.well-known/agent-card.json"):
            self._send(200, _card())
        elif self.path == "/skills":
            # Behind caller auth: it exposes the mount path and the full description
            # set, which is operator detail rather than discovery contract.
            try:
                authenticate(self.headers)
            except HttpError as err:
                self._fail(None, err)
                return
            self._send(200, {"dir": SKILLS_DIR, "skills": discover_skills()})
        else:
            self._send(404, {"error": "not found", "code": "not_found"})

    def do_POST(self):
        rpc_id = None
        try:
            if self.path not in ("/", "/tasks", "/a2a"):
                raise HttpError(404, "not_found", "not found")
            # Caller auth FIRST: nothing below this line should be reachable by an
            # unauthenticated peer, parsing included.
            authenticate(self.headers)
            ctype = (self.headers.get("Content-Type") or "").split(";")[0].strip()
            if ctype != "application/json":
                raise HttpError(415, "unsupported_media_type",
                                "Content-Type must be application/json")
            length = int(self.headers.get("Content-Length") or 0)
            if length <= 0 or length > 1_000_000:
                raise HttpError(400, "bad_content_length", "bad content length")
            try:
                body = json.loads(self.rfile.read(length))
            except Exception:
                raise HttpError(400, "unparseable_request", "unparseable request")
            if not isinstance(body, dict):
                raise HttpError(400, "bad_request", "body must be a JSON object")

            if body.get("jsonrpc"):
                rpc_id = body.get("id")
                method = body.get("method")
                if method not in RPC_METHODS:
                    raise HttpError(400, "method_not_found",
                                    f"unknown method {_clip(method)!r}; this agent "
                                    f"implements {', '.join(RPC_METHODS)}", -32601)
                if rpc_id is not None and not SEEN.claim(rpc_id):
                    raise HttpError(409, "duplicate_request",
                                    "this JSON-RPC id was already handled; a replayed "
                                    "task is not re-run", -32600)

            text = task_text(body)
            if not text:
                raise HttpError(400, "no_task_text", "no task text", -32602)
            self._run(rpc_id, text)
        except HttpError as err:
            if err.status in (401, 403, 503):
                # Logged, not traced: an unauthenticated caller must not be able to
                # drive outbound collector traffic just by hammering this port.
                _log("WARN", "request refused", code=err.code, path=self.path)
            self._fail(rpc_id, err)
        except Exception:
            _log("ERROR", "handler crashed", trace=traceback.format_exc()[-2000:])
            self._fail(rpc_id, HttpError(500, "internal_error", "internal error"))

    def _run(self, rpc_id, text: str) -> None:
        if not _SLOTS.acquire(blocking=False):
            raise HttpError(429, "too_many_requests",
                            f"this agent runs at most {MAX_CONCURRENCY} concurrent "
                            "tasks; retry shortly")
        started = time.time_ns()
        try:
            result = run_task(text)
        except HttpError as err:
            _emit_span("agent.task", False, err.code, time.time_ns() - started,
                       {"orbit.auth.error": err.code} if err.status == 401
                       else {"orbit.task.error": err.code})
            _log("ERROR", "task refused", code=err.code, error=str(err))
            raise
        except Exception as exc:
            # The traceback goes to the log, never to the caller: this endpoint is
            # reachable by peers who have no business seeing our internals.
            _emit_span("agent.task", False, type(exc).__name__,
                       time.time_ns() - started)
            _log("ERROR", "task failed", error=f"{type(exc).__name__}: {exc}",
                 trace=traceback.format_exc()[-2000:])
            raise HttpError(500, "task_failed", "the task failed; see agent logs")
        finally:
            _SLOTS.release()

        usage = result.get("usage") or {}
        _emit_span("agent.task", True, "ok", time.time_ns() - started,
                   {f"orbit.usage.{k}": v for k, v in usage.items()})
        payload = {"result": result["text"], "usage": usage}
        if rpc_id is not None:
            payload = {"jsonrpc": "2.0", "id": rpc_id, "result": {
                "kind": "message", "role": "agent",
                "parts": [{"kind": "text", "text": result["text"]}]}}
        self._send(200, payload)

    def log_message(self, *args):  # keep stdout to structured lines only
        pass


if __name__ == "__main__":
    os.makedirs(WORKDIR, exist_ok=True)
    skills = discover_skills()
    if ONESHOT:
        # Scheduled one-shot: run ONE task and EXIT with its status. The HTTP server block
        # below is NEVER reached in this mode — sys.exit() returns first — so there is no
        # listening socket and authenticate() (the caller bearer) has nothing to gate and
        # is correctly absent from this path. The MODEL gate check_credentials() still runs
        # inside run_task(), and the import-time credential/routing scrub (DENIED_PRESENT)
        # has already run, exactly as in server mode — so a missing subscription token or a
        # routing override fails CLOSED with a non-zero exit, not a silent success.
        _log("INFO", "agent-runtime one-shot", agent=AGENT_NAME, model=MODEL,
             skills=[s["id"] for s in skills], workdir=WORKDIR, max_turns=MAX_TURNS,
             timeout_s=TASK_TIMEOUT_S, otlp=OTLP or "(unset)")
        if DENIED_PRESENT:
            _log("ERROR", "denied credential/routing env stripped at boot; the scheduled "
                 "task will refuse until it is removed from the pod", names=DENIED_PRESENT)
        sys.exit(run_oneshot())
    _log("INFO", "agent-runtime starting", agent=AGENT_NAME, model=MODEL, port=PORT,
         skills_dir=SKILLS_DIR, skills=[s["id"] for s in skills],
         workdir=WORKDIR, max_turns=MAX_TURNS, timeout_s=TASK_TIMEOUT_S,
         max_concurrency=MAX_CONCURRENCY, allowed_tools=ALLOWED_TOOLS,
         caller_auth="configured" if CALLER_TOKEN else "MISSING",
         otlp=OTLP or "(unset)")
    if DENIED_PRESENT:
        _log("ERROR", "denied credential/routing env stripped at boot; tasks will "
             "refuse until it is removed from the pod", names=DENIED_PRESENT)
    if not CALLER_TOKEN:
        _log("ERROR", "no caller token configured — every task will be refused 503")
    if not OTLP:
        _log("WARN", "otel disabled", reason="OTEL_EXPORTER_OTLP_ENDPOINT unset")
    # A startup span, not just per-task ones: it proves the OTLP wiring at boot, on a
    # pod that has no token yet and may never receive a task.
    _emit_span("agent.start", True, "boot", 0, {"orbit.skills": ",".join(
        s["id"] for s in skills) or "(none)"})
    # SERVER MODE ONLY — the one-shot path has already sys.exit()ed above, so run_oneshot
    # is entirely unaffected by this. This process is PID 1 with no init, and the kernel
    # delivers SIGTERM/SIGINT to PID 1 ONLY for signals that have a handler installed —
    # with none, the default HTTP server would ignore SIGTERM outright. That is a real
    # operational bug: Kubernetes SIGTERMs a pod on every rollout and, ignored, SIGKILLs it
    # only after the full terminationGracePeriod. Installing a handler makes PID 1 actually
    # receive the signal; it stops serve_forever() and exits 0 promptly.
    server = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)

    def _graceful_stop(signum, _frame):
        _log("INFO", "agent-runtime stopping on signal", signal=signum)
        raise SystemExit(0)

    signal.signal(signal.SIGTERM, _graceful_stop)
    signal.signal(signal.SIGINT, _graceful_stop)
    try:
        server.serve_forever()
    except (KeyboardInterrupt, SystemExit):
        pass
    finally:
        server.server_close()
