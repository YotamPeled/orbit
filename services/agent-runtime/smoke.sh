#!/usr/bin/env bash
# Container smoke test for the Orbit agent-runtime (SDK) image — Phase D.1.
#
# Runs the REAL image in containers, not the module in-process: nearly every property
# this guards is a property of the built artifact, and several were previously inert in
# a way unit tests on app.py would not have caught.
#
# What it holds down, each red if reverted:
#   * CALLER auth on every POST (absent/wrong bearer, and unconfigured = refuse);
#   * MODEL auth subscription-only, including the routing vars that silently redirect
#     the credential (ANTHROPIC_BASE_URL et al);
#   * no tracebacks leaked to callers;
#   * the SDK argv actually carries the security posture — proven by stubbing `claude`
#     on PATH and reading the argv it was invoked with, NOT by grepping our source;
#   * skills mount loaded, contained (no symlink escape), and discoverable;
#   * budget brakes: per-request timeout and bounded concurrency;
#   * A2A validation: content-type, unknown method, replayed id, non-text parts;
#   * OTel emission observed at a real collector.
#
# Runs WITHOUT any Anthropic credential: "fails closed" is itself the assertion. The
# stubbed-CLI stages use a FAKE token and never reach Anthropic. A live model call
# happens only when CLAUDE_CODE_OAUTH_TOKEN is present (optional last stage).
#
#   usage: services/agent-runtime/smoke.sh [image-tag]
#   env:   ORBIT_SMOKE_NO_BUILD=1   reuse an existing tag instead of building
#          DOCKER=podman            use podman instead of docker
set -uo pipefail

IMG="${1:-orbit-agent-runtime:smoke}"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
RUN="orbit-smoke-$$"
NET="${RUN}-net"
WORK="$(mktemp -d)"
DOCKER="${DOCKER:-docker}"
CURL_IMG="${CURL_IMG:-curlimages/curl:8.11.1}"
TOKEN="smoke-caller-token-$$"
PASSED=0
FAILED=0
CONTAINERS=()

pass() { printf '  \033[32mPASS\033[0m %s\n' "$1"; PASSED=$((PASSED + 1)); }
fail() { printf '  \033[31mFAIL\033[0m %s\n' "$1"; FAILED=$((FAILED + 1)); }
note() { printf '\n== %s\n' "$1"; }
ok()   { if [ "$1" = "0" ]; then pass "$2"; else fail "$2"; fi; }

cleanup() {
  [ "${#CONTAINERS[@]}" -gt 0 ] && $DOCKER rm -f "${CONTAINERS[@]}" >/dev/null 2>&1
  $DOCKER network rm "$NET" >/dev/null 2>&1
  rm -rf "$WORK"
}
trap cleanup EXIT

# start <name-suffix> <alias> [docker run args...]
start() {
  local name="${RUN}-$1" alias="$2"; shift 2
  CONTAINERS+=("$name")
  $DOCKER run -d --name "$name" --network "$NET" --network-alias "$alias" \
    "$@" "$IMG" >/dev/null
}

# curl runs from a throwaway container on the same network, so this script needs no
# host-side curl and no published ports. Status and body are separated by the trailing
# newline -w writes, because a raw concatenation of the two is unparseable.
#
# req sets $LAST_CODE / $LAST_BODY rather than printing them: calling it inside a
# command substitution would run it in a subshell and silently discard the body, which
# turns every body assertion into a false failure.
LAST_BODY=""
LAST_CODE=""
req() { # req <method> <url> [json-body] [extra curl args...]
  local method="$1" url="$2" data="${3:-}"; shift 3 2>/dev/null || shift 2
  local args=(-s -w '\n%{http_code}' -X "$method")
  [ -n "$data" ] && args+=(-H 'Content-Type: application/json' -d "$data")
  local out
  out="$($DOCKER run --rm --network "$NET" "$CURL_IMG" "${args[@]}" "$@" "$url" 2>/dev/null)"
  LAST_CODE="${out##*$'\n'}"
  LAST_BODY="${out%$'\n'*}"
  [ "$LAST_BODY" = "$LAST_CODE" ] && LAST_BODY=""
}

# Same, but presenting the valid caller bearer.
areq() { local m="$1" u="$2" d="${3:-}"; req "$m" "$u" "$d" -H "Authorization: Bearer $TOKEN"; }

wait_healthy() { # wait_healthy <alias> <container-suffix>
  local i
  for i in $(seq 1 60); do
    req GET "http://$1:8080/healthz"
    [ "$LAST_CODE" = "200" ] && return 0
    sleep 1
  done
  $DOCKER logs "${RUN}-$2" 2>&1 | tail -20
  return 1
}

has()  { printf '%s' "$LAST_BODY" | grep -q "$1"; }
lacks(){ ! printf '%s' "$LAST_BODY" | grep -qi "$1"; }

# ---------------------------------------------------------------- 0. build the image
note "0. the image builds"
if [ "${ORBIT_SMOKE_NO_BUILD:-0}" = "1" ]; then
  echo "  (skipped, ORBIT_SMOKE_NO_BUILD=1)"
elif $DOCKER build -t "$IMG" "$HERE" >"$WORK/build.log" 2>&1; then
  pass "$DOCKER build $IMG"
else
  tail -40 "$WORK/build.log"
  fail "$DOCKER build $IMG"
  exit 1
fi

$DOCKER network create "$NET" >/dev/null || { echo "cannot create network"; exit 1; }

# ------------------------------------------------- 1. a mock OTLP collector to observe
# Not a mock of our own code: a real HTTP server that records what it is POSTed, so
# "OTel emission was attempted" is proven at the receiving end, not by a log line the
# runtime could print without ever opening a socket.
cat >"$WORK/collector.py" <<'PY'
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
class H(BaseHTTPRequestHandler):
    def do_POST(self):
        n = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(n).decode("utf-8", "replace")
        print("OTLP-RECEIVED " + self.path + " " + body, flush=True)
        self.send_response(200); self.send_header("Content-Length", "2")
        self.end_headers(); self.wfile.write(b"{}")
    def log_message(self, *a): pass
print("collector up", file=sys.stderr, flush=True)
ThreadingHTTPServer(("0.0.0.0", 4318), H).serve_forever()
PY

# ---------------------------------------------- 2. skills: one real, one symlink escape
mkdir -p "$WORK/skills/orbit-smoke-skill"
cat >"$WORK/skills/orbit-smoke-skill/SKILL.md" <<'MD'
---
name: orbit-smoke-skill
description: A dummy skill mounted by smoke.sh to prove the skills mount is discoverable.
---
This skill exists only so the smoke test can assert discovery of a mounted directory.
MD
# The attack: a mounted skill whose SKILL.md points outside the mount. Its contents
# must never reach /skills or the agent card.
printf 'TOP-SECRET-CANARY-VALUE\n' >"$WORK/secret.txt"
mkdir -p "$WORK/skills/evil-skill"
ln -s /home/orbit/secret/secret.txt "$WORK/skills/evil-skill/SKILL.md"

# The stub CLI: records the argv the SDK invoked it with, so the security posture is
# asserted from the OUTSIDE. A source grep would pass even with the option deleted.
mkdir -p "$WORK/stub" "$WORK/out"
# Python, not sh, because the modern SDK (>=0.1.40, the floor that carries the
# rate_limit_event fix) always drives the CLI in STREAMING mode: it opens with an
# `initialize` control_request on stdin and BLOCKS until the CLI answers with a matching
# control_response. A print-and-exit stub hangs that handshake (Control request timeout:
# initialize) and no task ever completes. So the stub speaks the minimum of that protocol
# — read the initialize request, reply success, then emit the result — which is also why
# it must parse a line of JSON, awkward in sh. It still records argv/env for the security
# assertions, which read them from the OUTSIDE.
cat >"$WORK/stub/claude" <<'PY'
#!/usr/bin/env python3
import json, os, sys, time

if len(sys.argv) > 1 and sys.argv[1] == "-v":
    print("2.1.250 (Claude Code)"); sys.exit(0)

# Record the argv and env the SDK invoked us with (best-effort: /out is unmounted in the
# stages that only need a live CLI, and those never read these files).
try:
    with open("/out/argv.txt", "w") as fh:
        fh.writelines(a + "\n" for a in sys.argv[1:])
    with open("/out/env.txt", "w") as fh:
        fh.writelines(f"{k}={v}\n" for k, v in os.environ.items())
except OSError:
    pass

def emit(obj):
    sys.stdout.write(json.dumps(obj) + "\n"); sys.stdout.flush()

# Answer the streaming control protocol: reply success to the first control_request
# (initialize), echoing its request_id so the SDK's initialize() unblocks.
while True:
    line = sys.stdin.readline()
    if not line:
        break                                  # EOF before initialize; nothing to answer
    line = line.strip()
    if not line:
        continue
    try:
        msg = json.loads(line)
    except ValueError:
        continue
    if msg.get("type") == "control_request":
        emit({"type": "control_response",
              "response": {"subtype": "success", "request_id": msg.get("request_id")}})
        break

# Optional dwell so the runtime's per-request timeout and concurrency cap have something
# to bite on (the slot stays held / the wall clock trips) before any result is emitted.
if os.environ.get("STUB_SLEEP"):
    time.sleep(float(os.environ["STUB_SLEEP"]))

# A COMPLETE, valid ResultMessage for the installed SDK: duration_ms AND duration_api_ms
# are both required or the parser raises MessageParseError. The argv-only stages never
# parse this (they read argv.txt); the one-shot success stage runs the task to completion.
emit({"type": "result", "subtype": "success", "is_error": False,
      "duration_ms": 1, "duration_api_ms": 1, "num_turns": 1,
      "session_id": "stub", "result": "STUB-OK", "total_cost_usd": 0})
PY
chmod +x "$WORK/stub/claude"
# mktemp -d is 0700 and the image runs as uid 10001; the `z` flag is the SELinux half of
# the same story (Fedora/RHEL). Without both, mounts read as empty and assertions fail
# for the wrong reason. /out must be writable by the container user.
chmod -R a+rX "$WORK"; chmod 777 "$WORK/out"
STUB_PATH=/opt/stub:/usr/local/bin:/usr/local/sbin:/usr/sbin:/usr/bin:/sbin:/bin

CONTAINERS+=("${RUN}-collector")
$DOCKER run -d --name "${RUN}-collector" --network "$NET" \
  --network-alias collector -v "$WORK/collector.py:/c.py:ro,z" \
  python:3.13-slim python3 -u /c.py >/dev/null

# The agent emits its `agent.start` span the instant it boots. If the collector is not
# yet listening, that POST hits connection-refused and is (correctly) swallowed — and
# the boot-span assertion then fails, but for a race, not a regression. This bit the CI
# runner where the local machine got lucky. So gate the agent on the collector being
# ready: probe until the POST endpoint answers. The probe body carries no span name, so
# it cannot satisfy any later assertion.
for _ in $(seq 1 60); do
  req POST http://collector:4318/v1/traces '{"probe":true}'
  [ "$LAST_CODE" = "200" ] && break
  sleep 1
done
[ "$LAST_CODE" = "200" ] || { echo "mock collector never came up"; $DOCKER logs "${RUN}-collector" 2>&1 | tail; exit 1; }

# ----------------------------------------------------------------- 3. start the agent
# Deliberately NO CLAUDE_CODE_OAUTH_TOKEN: the point is that it comes up and fails
# closed on the task path rather than crash-looping or silently proceeding.
start agent agent \
  -e ORBIT_AGENT_NAME=smoke-agent \
  -e ORBIT_AGENT_TOKEN="$TOKEN" \
  -e OTEL_EXPORTER_OTLP_ENDPOINT=http://collector:4318 \
  -e OTEL_SERVICE_NAME=orbit-smoke-agent \
  -v "$WORK/skills:/home/orbit/.claude/skills:ro,z" \
  -v "$WORK/secret.txt:/home/orbit/secret/secret.txt:ro,z"

note "1. the entrypoint comes up"
wait_healthy agent agent; ok $? "container serves /healthz 200"
$DOCKER logs "${RUN}-agent" >"$WORK/agent.log" 2>&1
grep -q '"msg": "agent-runtime starting"' "$WORK/agent.log"
ok $? "startup line logged with agent/model/skills/brakes"

# --------------------------------------------------- 4. A2A protocol handshake answers
note "2. the A2A shim answers its protocol handshake"
req GET http://agent:8080/.well-known/agent-card.json
[ "$LAST_CODE" = "200" ]; ok $? "agent card served on /.well-known/agent-card.json"
for field in '"protocolVersion"' '"name"' '"skills"' '"defaultInputModes"' '"url"'; do
  has "$field"; ok $? "card carries $field"
done
has '"securitySchemes"'; ok $? "card declares its bearer security scheme"
req GET http://agent:8080/.well-known/agent.json
[ "$LAST_CODE" = "200" ]; ok $? "legacy /.well-known/agent.json also served"

# ------------------------------------------------------------ 5. CALLER auth (S1a)
note "3. caller auth gates every POST"
req POST http://agent:8080/ '{"text":"who are you"}'
{ [ "$LAST_CODE" = "401" ] && has 'unauthenticated'; }
ok $? "POST / with NO bearer is 401 code=unauthenticated (got $LAST_CODE, body=${LAST_BODY:0:60})"
req POST http://agent:8080/tasks '{"text":"x"}' -H "Authorization: Bearer wrong-token"
[ "$LAST_CODE" = "401" ]; ok $? "POST /tasks with a WRONG bearer is 401 (got $LAST_CODE)"
req POST http://agent:8080/a2a '{"text":"x"}'
[ "$LAST_CODE" = "401" ]; ok $? "POST /a2a with no bearer is 401 (got $LAST_CODE)"
req GET http://agent:8080/skills
[ "$LAST_CODE" = "401" ]; ok $? "GET /skills requires the bearer too (got $LAST_CODE)"
# Unconfigured caller auth must REFUSE, not run open.
start noauth noauth -e ORBIT_AGENT_NAME=noauth
wait_healthy noauth noauth
req POST http://noauth:8080/ '{"text":"x"}'
{ [ "$LAST_CODE" = "503" ] && has 'caller_auth_unconfigured'; }
ok $? "no caller token configured -> 503 refuse, never open (got $LAST_CODE)"
# Content-Type enforcement.
req POST http://agent:8080/ "" -H "Authorization: Bearer $TOKEN" \
  -H 'Content-Type: text/plain' --data-raw '{"text":"x"}'
[ "$LAST_CODE" = "415" ]; ok $? "non-JSON Content-Type is 415 (got $LAST_CODE)"

# ------------------------------------------------------- 6. MODEL auth fails closed
note "4. model auth fails closed for an AUTHENTICATED caller"
areq POST http://agent:8080/ '{"text":"who are you"}'
if [ "$LAST_CODE" = "200" ]; then
  fail "task returned 200 WITHOUT a token — silent pass-through"
elif [ "$LAST_CODE" = "401" ]; then
  pass "authenticated caller, no model token -> 401"
else
  fail "task refused with $LAST_CODE (expected 401)"
fi
has 'no_subscription_token'; ok $? "refusal carries code=no_subscription_token"
has 'CLAUDE_CODE_OAUTH_TOKEN'; ok $? "refusal names the missing credential"
lacks 'Traceback'; ok $? "refusal body leaks no traceback"
lacks '/app/app.py'; ok $? "refusal body leaks no source paths"

areq POST http://agent:8080/a2a \
  '{"jsonrpc":"2.0","id":7,"method":"message/send","params":{"message":{"role":"user","parts":[{"kind":"text","text":"hi"}]}}}'
{ [ "$LAST_CODE" = "401" ] && has '"jsonrpc"' && has 'no_subscription_token'; }
ok $? "JSON-RPC message/send refused 401 with an rpc error (got $LAST_CODE)"

# Every credential/routing override must be a refusal, not a fallback (S2).
# ANTHROPIC_BASE_URL alone would send the token and every prompt to another host.
# The proxy/CA/NODE names (finding #3) join the list: each redirects the model call or
# runs code in the CLI's own process, and each is refused the same way — the SAME set the
# intake refuses on a Job env, so both plane layers agree.
for var in ANTHROPIC_API_KEY ANTHROPIC_AUTH_TOKEN ANTHROPIC_BASE_URL \
           CLAUDE_CODE_USE_BEDROCK CLAUDE_CODE_USE_VERTEX ANTHROPIC_CUSTOM_HEADERS \
           HTTPS_PROXY NODE_OPTIONS NODE_TLS_REJECT_UNAUTHORIZED CLAUDE_CONFIG_DIR; do
  alias="deny$(echo "$var" | tr 'A-Z_' 'a-z-')"
  start "$alias" "$alias" -e ORBIT_AGENT_TOKEN="$TOKEN" -e "$var=hostile-value" \
    -e CLAUDE_CODE_OAUTH_TOKEN=fake-token-never-used
  wait_healthy "$alias" "$alias"
  areq POST "http://$alias:8080/" '{"text":"hi"}'
  { [ "$LAST_CODE" = "401" ] && has 'api_key_not_permitted' && has "$var"; }
  ok $? "$var present -> 401 api_key_not_permitted (got $LAST_CODE)"
done
# ...and it is gone from the process, so the CLI subprocess cannot inherit it. This is
# the load-bearing half: ClaudeAgentOptions.env only MERGES over os.environ and cannot
# delete an inherited variable.
$DOCKER run --rm -e ANTHROPIC_BASE_URL=http://evil.invalid --entrypoint python3 "$IMG" -c \
  'import sys; sys.path.insert(0,"/app"); import os, app; sys.exit(0 if "ANTHROPIC_BASE_URL" not in os.environ and "ANTHROPIC_BASE_URL" in app.DENIED_PRESENT else 1)'
ok $? "denied vars are stripped from os.environ at import (subprocess cannot inherit)"

# ------------------------------------------------------------ 7. skills mount is real
note "5. the skills mount is loaded, contained and discoverable"
areq GET http://agent:8080/skills
has '/home/orbit/.claude/skills'; ok $? "skills mount point is the documented path"
has 'orbit-smoke-skill'; ok $? "mounted dummy skill discovered"
has 'prove the skills mount is discoverable'
ok $? "SKILL.md frontmatter parsed (description read from the file)"
# S4: the symlink-escape skill must be skipped and its target never published.
lacks 'TOP-SECRET-CANARY-VALUE'
ok $? "symlinked SKILL.md pointing outside the mount is NOT published"
lacks 'evil-skill'; ok $? "the escaping skill is skipped entirely"
grep -q 'skill skipped: escapes the skills mount' "$WORK/agent.log" \
  || $DOCKER logs "${RUN}-agent" 2>&1 | grep -q 'skill skipped: escapes the skills mount'
ok $? "the escape is logged, not silently ignored"
req GET http://agent:8080/.well-known/agent-card.json
has 'orbit-smoke-skill'; ok $? "mounted skill advertised on the A2A card"
lacks 'TOP-SECRET-CANARY-VALUE'; ok $? "the card leaks no out-of-mount content either"
$DOCKER run --rm --entrypoint python3 "$IMG" -c \
  'import os,sys; sys.exit(0 if os.path.isdir(os.environ["ORBIT_SKILLS_DIR"]) else 1)'
ok $? "mount point exists in the image with no volume attached"
start noskills noskills -e ORBIT_AGENT_TOKEN="$TOKEN"
wait_healthy noskills noskills
areq GET http://noskills:8080/skills
has '"skills": \[\]'; ok $? "no mount -> empty skill list, no crash"

# ------------------------------------- 8. the SDK argv carries the posture (S7, S1b)
# BEHAVIORAL: stub `claude` on PATH and read the argv the SDK actually invoked. The
# previous version of this check grepped app.py, so it passed even with the option
# deleted — the one seam it was written to guard did not hold.
note "6. the SDK is invoked with the security posture it claims"
start argv argv -e ORBIT_AGENT_TOKEN="$TOKEN" \
  -e CLAUDE_CODE_OAUTH_TOKEN=fake-token-never-used \
  -e PATH="$STUB_PATH" \
  -v "$WORK/stub:/opt/stub:ro,z" -v "$WORK/out:/out:z"
wait_healthy argv argv
areq POST http://argv:8080/ '{"text":"hello"}'
if [ -s "$WORK/out/argv.txt" ]; then
  pass "the SDK invoked the CLI (argv captured)"
  ARGV="$(cat "$WORK/out/argv.txt")"
  # The SDK renders this as ONE fused token `--setting-sources=user` (>=0.1.40) or as two
  # tokens `--setting-sources` `user` (older). Accept either; the property is that user
  # scope is enabled so the mounted skills actually load.
  grep -qx -- '--setting-sources=user' <<<"$ARGV" \
    || { grep -qx -- '--setting-sources' <<<"$ARGV" && grep -qx -- 'user' <<<"$ARGV"; }
  ok $? "argv carries setting-sources=user (skills mount actually loaded)"
  ! grep -qx -- 'bypassPermissions' <<<"$ARGV"
  ok $? "argv does NOT carry bypassPermissions"
  grep -qx -- 'default' <<<"$ARGV" && grep -qx -- '--permission-mode' <<<"$ARGV"
  ok $? "argv carries --permission-mode default"
  grep -qx -- '--disallowedTools' <<<"$ARGV"
  ok $? "argv carries --disallowedTools"
  grep -qE '^Bash,|,Bash$|,Bash,|^Bash$' <<<"$ARGV" && grep -q 'Write' <<<"$ARGV"
  ok $? "Bash and Write are explicitly disallowed"
  # The token lives in /proc/self/environ and the prompt is catalog-controlled, so the
  # filesystem-read and network built-ins are as dangerous as Bash — "read a file, fetch
  # a URL" is a complete exfiltration primitive. Assert the whole surface is named, on
  # the argv the SDK actually invoked, so dropping any one of them from app.py reddens.
  DENY_LINE="$(grep -A1 -x -- '--disallowedTools' <<<"$ARGV" | tail -1)"
  MISSING=""
  for t in Read Glob Grep LS NotebookRead WebFetch WebSearch; do
    grep -qE "(^|,)$t(,|$)" <<<"$DENY_LINE" || MISSING="$MISSING $t"
  done
  [ -z "$MISSING" ]
  ok $? "the filesystem-read and network built-ins are disallowed (missing:${MISSING:- none})"
  grep -qx -- '--max-turns' <<<"$ARGV"
  ok $? "argv carries --max-turns (per-run budget brake)"
  grep -qx -- 'claude-sonnet-4-6' <<<"$ARGV"
  ok $? "argv carries the settled default model claude-sonnet-4-6"
  # cwd must not be the SDK's own config dir.
  grep -q '^PWD=/home/orbit/work$' "$WORK/out/env.txt"
  ok $? "the CLI runs in the dedicated workdir, not \$HOME (~/.claude)"
  # The subprocess env is what an exfiltration would actually ride on.
  ! grep -qE '^(ANTHROPIC_BASE_URL|ANTHROPIC_API_KEY|ANTHROPIC_AUTH_TOKEN)=.+' \
    "$WORK/out/env.txt"
  ok $? "subprocess env carries no populated credential/routing override"
else
  fail "the SDK never invoked the CLI — argv not captured"
  $DOCKER logs "${RUN}-argv" 2>&1 | tail -10
fi

# --------------------------------------------------------- 9. budget brakes (S5)
note "7. budget brakes bound a run"
rm -f "$WORK/out/argv.txt"
# Two containers, because the two brakes need opposite clocks: the concurrency probe
# needs the first task STILL running when the second arrives, so its timeout must be
# comfortably longer than the gap.
start busy busy -e ORBIT_AGENT_TOKEN="$TOKEN" \
  -e CLAUDE_CODE_OAUTH_TOKEN=fake-token-never-used \
  -e PATH="$STUB_PATH" -e STUB_SLEEP=60 \
  -e ORBIT_AGENT_TIMEOUT_S=45 -e ORBIT_AGENT_MAX_CONCURRENCY=1 \
  -v "$WORK/stub:/opt/stub:ro,z" -v "$WORK/out:/out:z"
wait_healthy busy busy
# Occupy the single slot, then prove the second caller is shed rather than queued.
$DOCKER run -d --rm --name "${RUN}-occupy" --network "$NET" "$CURL_IMG" \
  -s -m 40 -X POST -H "Authorization: Bearer $TOKEN" \
  -H 'Content-Type: application/json' -d '{"text":"occupy"}' \
  http://busy:8080/ >/dev/null 2>&1
sleep 5
areq POST http://busy:8080/ '{"text":"second"}'
{ [ "$LAST_CODE" = "429" ] && has 'too_many_requests'; }
ok $? "concurrency cap sheds the extra task with 429 (got $LAST_CODE)"
$DOCKER rm -f "${RUN}-occupy" >/dev/null 2>&1

start tmo tmo -e ORBIT_AGENT_TOKEN="$TOKEN" \
  -e CLAUDE_CODE_OAUTH_TOKEN=fake-token-never-used \
  -e PATH="$STUB_PATH" -e STUB_SLEEP=60 -e ORBIT_AGENT_TIMEOUT_S=3 \
  -v "$WORK/stub:/opt/stub:ro,z" -v "$WORK/out:/out:z"
wait_healthy tmo tmo
areq POST http://tmo:8080/ '{"text":"timeout me"}'
{ [ "$LAST_CODE" = "504" ] && has 'task_timeout'; }
ok $? "a hung CLI is cut off at ORBIT_AGENT_TIMEOUT_S -> 504 (got $LAST_CODE)"
lacks 'Traceback'; ok $? "timeout body leaks no traceback"

# ------------------------------------------------------- 10. A2A validation (S6)
note "8. A2A request validation"
areq POST http://agent:8080/a2a \
  '{"jsonrpc":"2.0","id":1,"method":"tasks/evil","params":{}}'
{ [ "$LAST_CODE" = "400" ] && has 'method_not_found'; }
ok $? "unknown JSON-RPC method rejected (got $LAST_CODE)"
areq POST http://agent:8080/a2a \
  '{"jsonrpc":"2.0","id":"replay-1","method":"message/send","params":{"message":{"parts":[{"kind":"text","text":"a"}]}}}'
FIRST="$LAST_CODE"
areq POST http://agent:8080/a2a \
  '{"jsonrpc":"2.0","id":"replay-1","method":"message/send","params":{"message":{"parts":[{"kind":"text","text":"a"}]}}}'
{ [ "$LAST_CODE" = "409" ] && has 'duplicate_request'; }
ok $? "a replayed JSON-RPC id is refused, not re-run (first=$FIRST, replay=$LAST_CODE)"
areq POST http://agent:8080/a2a \
  '{"jsonrpc":"2.0","id":2,"method":"message/send","params":{"message":{"parts":[{"kind":"file","file":{"uri":"file:///etc/passwd"}}]}}}'
{ [ "$LAST_CODE" = "400" ] && has 'unsupported_part_kind'; }
ok $? "a non-text part is rejected, not concatenated as text (got $LAST_CODE)"

# --------------------------------------------------- 11. OTel emission is real, observed
note "9. OTel emission is attempted toward the configured endpoint"
$DOCKER logs "${RUN}-collector" >"$WORK/collector.log" 2>&1
# A real span, not the readiness probe: grep for the OTLP resourceSpans envelope the
# agent emits, which {"probe":true} does not carry.
grep -q 'OTLP-RECEIVED /v1/traces .*resourceSpans' "$WORK/collector.log"
ok $? "the mock collector received a real span POST on /v1/traces"
grep -q '"name": "agent.start"' "$WORK/collector.log"
ok $? "startup span emitted at boot (no token needed)"
grep -q '"name": "agent.task"' "$WORK/collector.log"
ok $? "task outcome span emitted (from the failed-auth run)"
grep -q 'orbit.auth.error' "$WORK/collector.log"
ok $? "the model-auth refusal is recorded on the span, not only returned"
grep -q 'orbit-smoke-agent' "$WORK/collector.log"
ok $? "resource identity comes from OTEL_SERVICE_NAME (platform-set, OBSERVABILITY.md)"
grep -q '"key": "orbit.agent"' "$WORK/collector.log"
ok $? "orbit.agent attribute present for per-agent run history"
# Unauthenticated callers must not be able to drive outbound collector traffic.
UNAUTH_SPANS="$(grep -c 'unauthenticated' "$WORK/collector.log")"
[ "$UNAUTH_SPANS" = "0" ]
ok $? "a rejected unauthenticated POST emits no span (no outbound amplification)"
# The negative half of the wiring: a misrouted collector must LOG, never be swallowed.
start nootlp nootlp -e ORBIT_AGENT_TOKEN="$TOKEN" \
  -e OTEL_EXPORTER_OTLP_ENDPOINT=http://nowhere.invalid:4318
wait_healthy nootlp nootlp; NOOTLP_UP=$?
$DOCKER logs "${RUN}-nootlp" >"$WORK/nootlp.log" 2>&1
grep -q '"msg": "otel emit failed"' "$WORK/nootlp.log"
ok $? "unreachable collector logs 'otel emit failed' — no silent swallow"
ok $NOOTLP_UP "a tracing outage does not take the agent down"

# ----------------------------------- 10b. one-shot (scheduled / CronJob) mode
# The CronJob renderer (Phase D.3) runs THIS image with ORBIT_AGENT_ONESHOT=1: the
# container performs ONE task and EXITS with its status, rather than serving. These assert
# the security-critical properties of that path — same model posture, fails closed, and no
# auth bypass on the server — from OUTSIDE, on the real image.
note "10. one-shot scheduled mode: runs one task, exits, same posture, fails closed"

# (a) FAIL CLOSED. ORBIT_AGENT_ONESHOT set, NO Anthropic credential -> check_credentials()
# must refuse and the process EXIT NON-ZERO. `timeout` bounds it, so a regression that
# HANGS (never exits) is caught as rc 124, distinct from a clean non-zero refusal. A
# 0-exit here would be a scheduled agent that silently did nothing on the org's dime.
timeout 90 $DOCKER run --rm -e ORBIT_AGENT_ONESHOT=1 -e ORBIT_AGENT_NAME=oneshot-noauth \
  -e ORBIT_AGENT_TOKEN="$TOKEN" "$IMG" >"$WORK/os-noauth.log" 2>&1
RC=$?
{ [ "$RC" -ne 0 ] && [ "$RC" -ne 124 ]; }
ok $? "one-shot with no subscription token EXITS NON-ZERO (rc=$RC), not 0 and not a hang"
grep -q 'no_subscription_token' "$WORK/os-noauth.log"
ok $? "one-shot fail-closed refusal names the missing credential (check_credentials ran)"
# The import-time credential/routing scrub also fails closed in one-shot: a routing
# override is stripped at import and check_credentials() refuses it.
timeout 90 $DOCKER run --rm -e ORBIT_AGENT_ONESHOT=1 -e ORBIT_AGENT_TOKEN="$TOKEN" \
  -e ANTHROPIC_BASE_URL=http://evil.invalid -e CLAUDE_CODE_OAUTH_TOKEN=fake-token-never-used \
  "$IMG" >"$WORK/os-deny.log" 2>&1
RC=$?
{ [ "$RC" -ne 0 ] && [ "$RC" -ne 124 ] && grep -q 'api_key_not_permitted' "$WORK/os-deny.log"; }
ok $? "one-shot with a routing override EXITS NON-ZERO api_key_not_permitted (rc=$RC)"

# (b) SAME POSTURE, asserted on the argv the SDK actually invoked — dropping a denied tool
# from app.py reddens here exactly as on the server path. Stub CLI + a fake token; the run
# succeeds and the process exits 0 (a scheduled Job that succeeds).
rm -f "$WORK/out/argv.txt"
timeout 90 $DOCKER run --rm -e ORBIT_AGENT_ONESHOT=1 -e ORBIT_AGENT_NAME=oneshot-argv \
  -e ORBIT_AGENT_TOKEN="$TOKEN" -e CLAUDE_CODE_OAUTH_TOKEN=fake-token-never-used \
  -e PATH="$STUB_PATH" \
  -v "$WORK/stub:/opt/stub:ro,z" -v "$WORK/out:/out:z" "$IMG" >"$WORK/os-argv.log" 2>&1
OSRC=$?
if [ -s "$WORK/out/argv.txt" ]; then
  pass "one-shot invoked the SDK CLI (argv captured)"
  ARGV="$(cat "$WORK/out/argv.txt")"
  grep -qx -- '--permission-mode' <<<"$ARGV" && grep -qx -- 'default' <<<"$ARGV"
  ok $? "one-shot argv carries --permission-mode default"
  ! grep -qx -- 'bypassPermissions' <<<"$ARGV"
  ok $? "one-shot argv does NOT carry bypassPermissions"
  grep -qx -- '--disallowedTools' <<<"$ARGV"
  ok $? "one-shot argv carries --disallowedTools"
  # The WHOLE denylist, same as the server-path assertion: the token is in the pod env and
  # the prompt is catalog-controlled, so a read/fetch built-in is a full exfil primitive.
  DENY_LINE="$(grep -A1 -x -- '--disallowedTools' <<<"$ARGV" | tail -1)"
  MISSING=""
  for t in Bash Write Edit Read Glob Grep LS NotebookRead WebFetch WebSearch; do
    grep -qE "(^|,)$t(,|$)" <<<"$DENY_LINE" || MISSING="$MISSING $t"
  done
  [ -z "$MISSING" ]
  ok $? "one-shot carries the full filesystem/network denylist (missing:${MISSING:- none})"
  grep -qx -- '--max-turns' <<<"$ARGV"
  ok $? "one-shot argv carries --max-turns (the per-run budget brake)"
  grep -qx -- 'claude-sonnet-4-6' <<<"$ARGV"
  ok $? "one-shot argv carries the settled default model"
else
  fail "one-shot never invoked the CLI — argv not captured"
  tail -10 "$WORK/os-argv.log"
fi
[ "$OSRC" -eq 0 ]
ok $? "one-shot with a working (stubbed) task EXITS 0 (success -> Job succeeds; rc=$OSRC)"

# (c) DISJOINT ENTRYPOINTS. The flag is the ONLY selector: the SAME image with
# ORBIT_AGENT_ONESHOT UNSET stays up as a server (so `timeout` must kill it -> rc 124),
# while every one-shot run above returned on its own. One env var read once at import; two
# disjoint entrypoints; nothing a request can toggle. (/healthz-on-server-mode is already
# proven by section 1 on the shared long-running agent.)
timeout 8 $DOCKER run --rm -e ORBIT_AGENT_TOKEN="$TOKEN" -e ORBIT_AGENT_NAME=server-mode \
  "$IMG" >/dev/null 2>&1
[ "$?" -eq 124 ]
ok $? "same image, ORBIT_AGENT_ONESHOT UNSET -> serves and never exits on its own (server mode intact)"

# ------------------------------------------- 11. optional: a live run, only if credentialed
note "11. live model call (only when a real setup token is present)"
if [ -n "${CLAUDE_CODE_OAUTH_TOKEN:-}" ]; then
  OUT="$($DOCKER run --rm --network "$NET" \
    -e CLAUDE_CODE_OAUTH_TOKEN -e ORBIT_AGENT_NAME=smoke-live \
    -e OTEL_EXPORTER_OTLP_ENDPOINT=http://collector:4318 \
    --entrypoint python3 "$IMG" -c \
    'import sys; sys.path.insert(0,"/app"); import app; print(app.run_task("Reply with exactly: ORBIT-OK")["text"])' 2>&1)"
  if printf '%s' "$OUT" | grep -q 'ORBIT-OK'; then
    pass "live task ran on the subscription token"
  else
    fail "live task did not return the expected text"
    printf '%s\n' "$OUT" | tail -10
  fi
else
  echo "  (skipped — CLAUDE_CODE_OAUTH_TOKEN not set; fail-closed is the CI assertion)"
fi

# -------------------------- 12. the pinned SDK speaks the CLI's streaming protocol (skew)
# The live incident this whole change fixes: CLI >=2.1.49 streams a `rate_limit_event`
# message and the old SDK pin (claude-agent-sdk 0.1.1) RAISED MessageParseError on it
# ("Unknown message type"), killing every dispatched task mid-run. Asserted here on the
# REAL image's actually-installed SDK, through the SAME parser the runtime's `async for`
# drives one level down — so reverting the pin to a build that does not model it reddens.
# A pure in-process import: no credential, no CLI spawn, no network.
note "12. the pinned SDK recognises the rate_limit_event stream message (protocol skew)"
$DOCKER run --rm --entrypoint python3 "$IMG" -c '
import sys
from claude_agent_sdk._internal.message_parser import parse_message
# well-formed rate_limit_event for the pinned SDK; the old 0.1.1 parser raised on it.
known = parse_message({"type": "rate_limit_event", "uuid": "u", "session_id": "s",
                       "rate_limit_info": {"status": "allowed"}})
# and a FUTURE unknown type must be SKIPPED (None), never raised (forward-compat).
unknown = parse_message({"type": "orbit_future_event_2099", "x": 1})
sys.exit(0 if (known is not None and unknown is None) else 1)
'
ok $? "in-image SDK parses rate_limit_event and skips unknown types (no MessageParseError)"

note "result"
echo "  $PASSED passed, $FAILED failed"
[ "$FAILED" -eq 0 ] || exit 1
exit 0
