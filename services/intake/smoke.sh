#!/usr/bin/env bash
# Container smoke test for the Orbit intake (event router) image — Phase D.3.
#
# Runs the REAL image in containers, against a REAL mock Kubernetes API over TLS and a
# REAL mock agent, because nearly every property here is a property of the built artifact
# and of what actually goes out on the wire. A unit test on app.py would pass with the
# signature check reordered after the parse, or with the dedup claim taken after dispatch.
#
# What it holds down, each red if reverted:
#   * GitHub HMAC verified over the RAW body, BEFORE parsing — missing, malformed and
#     wrong signatures are one indistinguishable 401, and a valid one is accepted;
#   * Alertmanager and a2a bearer auth, constant-time, and UNCONFIGURED = refuse (503);
#   * dedup: the second delivery of one event dispatches nothing and answers deduped:true;
#   * unknown route / unknown agent / unknown path = 404, fail closed;
#   * the task text is BOUNDED and control characters are stripped;
#   * no credential substring ever appears in a log line or in an emitted span;
#   * a routing config with an unknown key CRASHES at startup — it does not warn and serve;
#   * a Job created for a job-dispatch agent carries ORBIT_AGENT_ONESHOT=1, the fixed
#     image, restartPolicy Never, and NO mounted ServiceAccount token;
#   * the Kubernetes API is reached over verified TLS: an untrusted cert is a failed
#     dispatch and no Job, never a token handed to an unverified peer.
#
# Runs with NO real credentials of any kind: every token here is a smoke-local string.
#
#   usage: services/intake/smoke.sh [image-tag]
#   env:   ORBIT_SMOKE_NO_BUILD=1   reuse an existing tag instead of building
#          DOCKER=podman            use podman instead of docker
set -uo pipefail

IMG="${1:-orbit-intake:smoke}"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
RUN="orbit-intake-smoke-$$"
NET="${RUN}-net"
WORK="$(mktemp -d)"
DOCKER="${DOCKER:-docker}"
CURL_IMG="${CURL_IMG:-curlimages/curl:8.11.1}"
PY_IMG="${PY_IMG:-python:3.13-slim}"
# Distinctive, greppable, and deliberately unlike each other: the leak assertions search
# every log line and every emitted span for each of these strings by name.
AM_TOKEN="SMOKECANARY-alertmanager-$$"
GH_SECRET="SMOKECANARY-githubsecret-$$"
A2A_TOKEN="SMOKECANARY-a2atoken-$$"
AGENT_TOKEN="SMOKECANARY-agentbearer-$$"
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

LAST_BODY=""
LAST_CODE=""
req() { # req <method> <url> [json-body] [extra curl args...]
  local method="$1" url="$2" data="${3:-}"; shift 3 2>/dev/null || shift 2
  local args=(-s -w '\n%{http_code}' -X "$method")
  [ -n "$data" ] && args+=(-H 'Content-Type: application/json' --data-raw "$data")
  local out
  out="$($DOCKER run --rm --network "$NET" "$CURL_IMG" "${args[@]}" "$@" "$url" 2>/dev/null)"
  LAST_CODE="${out##*$'\n'}"
  LAST_BODY="${out%$'\n'*}"
  [ "$LAST_BODY" = "$LAST_CODE" ] && LAST_BODY=""
}
has()  { printf '%s' "$LAST_BODY" | grep -q "$1"; }
lacks(){ ! printf '%s' "$LAST_BODY" | grep -qi "$1"; }

wait_healthy() { # wait_healthy <alias>
  local i
  for i in $(seq 1 60); do
    req GET "http://$1:8080/healthz"
    [ "$LAST_CODE" = "200" ] && return 0
    sleep 1
  done
  return 1
}

# ---------------------------------------------------------------- 0. build the image
note "0. the image builds"
if [ "${ORBIT_SMOKE_NO_BUILD:-0}" = "1" ]; then
  echo "  (skipped, ORBIT_SMOKE_NO_BUILD=1)"
elif $DOCKER build --pull --no-cache -t "$IMG" "$HERE" >"$WORK/build.log" 2>&1; then
  pass "$DOCKER build $IMG"
else
  tail -40 "$WORK/build.log"
  fail "$DOCKER build $IMG"
  exit 1
fi

$DOCKER network create "$NET" >/dev/null || { echo "cannot create network"; exit 1; }

# --------------------------------------------------------- 1. the mocks intake talks to
# A mock OTLP collector, a mock warm agent, and a mock Kubernetes API served over TLS with
# a self-signed cert. All three RECORD what they were sent, so every assertion below is
# made at the receiving end rather than against a log line intake could print without ever
# opening a socket.
mkdir -p "$WORK/out" "$WORK/tls"
chmod 777 "$WORK/out"

cat >"$WORK/recorder.py" <<'PY'
# One recorder, three roles, chosen by $ROLE. Writes every request body it is POSTed to
# /out/$ROLE.log, one JSON line per delivery, so the assertions read what went on the wire.
import json, os, ssl, sys, time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
ROLE = os.environ["ROLE"]
LOG = "/out/%s.log" % ROLE
class H(BaseHTTPRequestHandler):
    def do_POST(self):
        # A SLOW agent: sleep PAST the caller's read timeout before responding, so intake's
        # warm dispatch times out while the "run" is still in flight (finding #1).
        if os.environ.get("SLEEP"):
            time.sleep(float(os.environ["SLEEP"]))
        n = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(n).decode("utf-8", "replace")
        with open(LOG, "a", encoding="utf-8") as fh:
            fh.write(json.dumps({"path": self.path, "auth": self.headers.get("Authorization", ""),
                                 "body": body}) + "\n")
        # The Kubernetes API answers 409 when a Job of that name already exists; the
        # AlreadyExists file lets a stage turn that on to exercise the apiserver-side
        # dedup layer.
        if ROLE == "k8s" and os.path.exists("/out/conflict"):
            self.send_response(409)
        else:
            self.send_response(201 if ROLE == "k8s" else 200)
        self.send_header("Content-Length", "2"); self.end_headers(); self.wfile.write(b"{}")
    def log_message(self, *a): pass
addr = ("0.0.0.0", int(os.environ.get("PORT", "8080")))
srv = ThreadingHTTPServer(addr, H)
if os.environ.get("TLS"):
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain("/tls/%s.crt" % os.environ["TLS"], "/tls/%s.key" % os.environ["TLS"])
    srv.socket = ctx.wrap_socket(srv.socket, server_side=True)
print("recorder up: " + ROLE, file=sys.stderr, flush=True)
srv.serve_forever()
PY

# Two self-signed certs for the SAME mock: one intake will be given the CA for (trusted),
# one it will not (untrusted). That pair is what makes "TLS is actually verified" provable
# rather than asserted — with verification off, BOTH would succeed.
gen_cert() { # gen_cert <name> <CN>
  openssl req -x509 -newkey rsa:2048 -nodes -days 2 \
    -keyout "$WORK/tls/$1.key" -out "$WORK/tls/$1.crt" -subj "/CN=$2" \
    -addext "subjectAltName=DNS:$2" >/dev/null 2>&1
}
gen_cert k8s k8s
gen_cert rogue k8s
# A stand-in for the projected ServiceAccount token. A plain single-line string, because
# that is what a real one is — and because a value with a newline in it would be rejected
# by urllib as an invalid header long before any TLS decision was made, which would make
# the TLS assertions below pass for the wrong reason.
printf 'smoke-serviceaccount-token' > "$WORK/tls/satoken"
chmod -R a+rX "$WORK/tls"

start_recorder() { # start_recorder <alias> <role> [TLS cert name]
  local name="${RUN}-$1"
  CONTAINERS+=("$name")
  $DOCKER run -d --name "$name" --network "$NET" --network-alias "$1" \
    -e ROLE="$2" -e TLS="${3:-}" \
    -v "$WORK/recorder.py:/r.py:ro,z" -v "$WORK/out:/out:z" -v "$WORK/tls:/tls:ro,z" \
    "$PY_IMG" python3 -u /r.py >/dev/null
}
start_recorder collector otlp
start_recorder agent agent
start_recorder k8s k8s k8s

for _ in $(seq 1 60); do
  req POST http://collector:8080/v1/traces '{"probe":true}'
  [ "$LAST_CODE" = "200" ] && break
  sleep 1
done
[ "$LAST_CODE" = "200" ] || { echo "mock collector never came up"; exit 1; }
rm -f "$WORK/out/otlp.log"   # discard the readiness probe

# --------------------------------------------------------------- 2. the routing config
# One warm agent (dispatch: service) and one cold one (dispatch: job) — the two halves of
# the router — plus every route kind.
cat >"$WORK/routes.json" <<'JSON'
{
 "version": 1,
 "agents": {
  "warm": {"dispatch": "service", "url": "http://agent:8080/",
           "tokenEnv": "INTAKE_AGENT_TOKEN_WARM"},
  "slow": {"dispatch": "service", "url": "http://slowagent:8080/",
           "tokenEnv": "INTAKE_AGENT_TOKEN_SLOW"},
  "cjob": {"dispatch": "job", "job": {
     "namespace": "orbit-agents-prod",
     "image": "ghcr.io/example-core/orbit-agent-runtime:main-0000000",
     "serviceAccountName": "orbit-agents",
     "uid": 10001, "memory": "512Mi",
     "activeDeadlineSeconds": 420, "backoffLimit": 0, "ttlSecondsAfterFinished": 3600,
     "env": {"ORBIT_AGENT_NAME": "cjob"}}},
  "cold": {"dispatch": "job", "job": {
     "namespace": "orbit-agents-prod",
     "image": "ghcr.io/example-core/orbit-agent-runtime:main-0000000",
     "serviceAccountName": "orbit-agents",
     "uid": 10001, "memory": "512Mi",
     "activeDeadlineSeconds": 420, "backoffLimit": 0, "ttlSecondsAfterFinished": 3600,
     "imagePullSecret": "ghcr-pull",
     "env": {"ORBIT_AGENT_NAME": "cold", "ORBIT_AGENT_PROMPT": "You are cold."},
     "secretEnv": {"ORBIT_AGENT_TOKEN": {"secret": "agent-cold-token",
                                         "key": "AGENT_COLD_TOKEN"},
                   "CLAUDE_CODE_OAUTH_TOKEN": {"secret": "orbit-claude-code",
                                               "key": "CLAUDE_CODE_OAUTH_TOKEN"}}}}
 },
 "routes": {
  "alert": [{"agent": "warm", "match": {"severity": "critical", "status": "firing"}},
            {"agent": "cold", "match": {"severity": "page"}}],
  "github": [{"agent": "warm", "repo": "acme/widgets", "events": ["push"]}],
  "a2a": [{"agent": "warm"}, {"agent": "slow"}, {"agent": "cjob"}]
 }
}
JSON
chmod -R a+rX "$WORK"

start_intake() { # start_intake <alias> [extra docker args...]
  local alias="$1"; shift
  local name="${RUN}-$alias"
  CONTAINERS+=("$name")
  $DOCKER run -d --name "$name" --network "$NET" --network-alias "$alias" \
    -v "$WORK/routes.json:/etc/orbit/intake/routes.json:ro,z" \
    -v "$WORK/tls:/tls:ro,z" \
    "$@" "$IMG" >/dev/null
}

start_intake intake \
  -e INTAKE_ALERTMANAGER_TOKEN="$AM_TOKEN" \
  -e INTAKE_GITHUB_SECRET="$GH_SECRET" \
  -e INTAKE_A2A_TOKEN="$A2A_TOKEN" \
  -e INTAKE_AGENT_TOKEN_WARM="$AGENT_TOKEN" \
  -e INTAKE_K8S_API=https://k8s:8080 \
  -e INTAKE_K8S_CA_FILE=/tls/k8s.crt \
  -e INTAKE_K8S_TOKEN_FILE=/tls/satoken \
  -e OTEL_EXPORTER_OTLP_ENDPOINT=http://collector:8080 \
  -e OTEL_SERVICE_NAME=orbit-smoke-intake

note "1. the router comes up"
wait_healthy intake; ok $? "container serves /healthz 200"
$DOCKER logs "${RUN}-intake" >"$WORK/intake.log" 2>&1
grep -q '"msg": "intake starting"' "$WORK/intake.log"
ok $? "startup line logged with the compiled route counts"
req GET http://intake:8080/routes
[ "$LAST_CODE" = "404" ]
ok $? "there is no unauthenticated endpoint that dumps the routing table (got $LAST_CODE)"
# The generator renders this component with readOnlyRootFilesystem: true. Asserted on the
# real image, because that setting is a runtime property of the process — a stray write at
# import would CrashLoopBackOff in the cluster and nowhere else.
start_intake ro --read-only \
  -e INTAKE_ALERTMANAGER_TOKEN="$AM_TOKEN" -e INTAKE_AGENT_TOKEN_WARM="$AGENT_TOKEN"
wait_healthy ro
ok $? "the router runs with a READ-ONLY root filesystem (as the chart renders it)"

# ----------------------------------------------------- 3. GitHub HMAC (the load-bearing one)
note "2. the GitHub signature is verified over the raw body, before anything parses it"
GH_BODY='{"repository":{"full_name":"acme/widgets"},"ref":"refs/heads/main"}'
SIG="sha256=$(printf '%s' "$GH_BODY" | openssl dgst -sha256 -hmac "$GH_SECRET" | awk '{print $NF}')"

req POST http://intake:8080/webhook/github "$GH_BODY" -H 'X-GitHub-Event: push'
{ [ "$LAST_CODE" = "401" ] && has 'bad_signature'; }
ok $? "UNSIGNED delivery is 401 bad_signature (got $LAST_CODE)"
req POST http://intake:8080/webhook/github "$GH_BODY" -H 'X-GitHub-Event: push' \
  -H 'X-Hub-Signature-256: sha256=deadbeef'
[ "$LAST_CODE" = "401" ]; ok $? "MALFORMED signature is 401 (got $LAST_CODE)"
req POST http://intake:8080/webhook/github "$GH_BODY" -H 'X-GitHub-Event: push' \
  -H "X-Hub-Signature-256: sha256=$(printf 'a%.0s' $(seq 1 64))"
[ "$LAST_CODE" = "401" ]; ok $? "WRONG signature of the right shape is 401 (got $LAST_CODE)"
lacks 'sha256='; ok $? "the refusal does not echo the expected signature back"
# The attack the ordering exists to stop: a body that is signed correctly for DIFFERENT
# bytes. Verifying a re-serialized payload would accept this.
req POST http://intake:8080/webhook/github '{"repository":{"full_name":"acme/widgets"},"ref":"refs/heads/evil"}' \
  -H 'X-GitHub-Event: push' -H "X-Hub-Signature-256: $SIG"
[ "$LAST_CODE" = "401" ]
ok $? "a signature valid for OTHER bytes is refused — the raw body is what is verified (got $LAST_CODE)"
# ...and an UNPARSEABLE body with a valid signature proves the signature ran FIRST: an
# implementation that parsed first would answer 400, not 401/2xx.
BAD_BODY='{not json'
BAD_SIG="sha256=$(printf '%s' "$BAD_BODY" | openssl dgst -sha256 -hmac "$GH_SECRET" | awk '{print $NF}')"
req POST http://intake:8080/webhook/github "$BAD_BODY" -H 'X-GitHub-Event: push' \
  -H "X-Hub-Signature-256: $BAD_SIG"
[ "$LAST_CODE" = "400" ]
ok $? "a signed-but-unparseable body reaches the parser (400), an unsigned one never does (got $LAST_CODE)"
# And the positive: a correctly signed delivery on a routed repo+event is accepted.
req POST http://intake:8080/webhook/github "$GH_BODY" -H 'X-GitHub-Event: push' \
  -H "X-Hub-Signature-256: $SIG" -H 'X-GitHub-Delivery: gh-delivery-1'
{ [ "$LAST_CODE" = "202" ] && has '"warm"'; }
ok $? "a VALID signature on a routed repo/event is accepted 202 (got $LAST_CODE)"
# Routing is per repo AND per event: neither alone opens the path.
req POST http://intake:8080/webhook/github '{"repository":{"full_name":"acme/other"}}' \
  -H 'X-GitHub-Event: push' \
  -H "X-Hub-Signature-256: sha256=$(printf '%s' '{"repository":{"full_name":"acme/other"}}' | openssl dgst -sha256 -hmac "$GH_SECRET" | awk '{print $NF}')"
{ [ "$LAST_CODE" = "404" ] && has 'no_route'; }
ok $? "a signed delivery for an UNROUTED repo is refused 404 no_route (got $LAST_CODE)"
req POST http://intake:8080/webhook/github "$GH_BODY" -H 'X-GitHub-Event: issues' \
  -H "$(printf 'X-Hub-Signature-256: %s' "$SIG")"
{ [ "$LAST_CODE" = "404" ] && has 'no_route'; }
ok $? "a signed delivery for an UNROUTED event type is refused 404 (got $LAST_CODE)"

# ------------------------------------------------------------ 4. bearer sources
note "3. bearer auth on the Alertmanager and a2a paths"
AM_BODY='{"status":"firing","groupKey":"g1","groupLabels":{"severity":"critical"},"commonAnnotations":{"summary":"disk full"},"alerts":[{"labels":{"alertname":"DiskFull","severity":"critical"},"annotations":{"summary":"nvme0 at 97%"}}]}'
req POST http://intake:8080/webhook/alertmanager "$AM_BODY"
{ [ "$LAST_CODE" = "401" ] && has 'unauthenticated'; }
ok $? "Alertmanager POST with NO bearer is 401 (got $LAST_CODE)"
req POST http://intake:8080/webhook/alertmanager "$AM_BODY" -H 'Authorization: Bearer wrong'
[ "$LAST_CODE" = "401" ]; ok $? "Alertmanager POST with a WRONG bearer is 401 (got $LAST_CODE)"
lacks 'SMOKECANARY'; ok $? "the refusal body leaks no part of the expected token"
req POST http://intake:8080/a2a/warm '{"text":"hi"}'
[ "$LAST_CODE" = "401" ]; ok $? "a2a POST with no bearer is 401 (got $LAST_CODE)"
req POST http://intake:8080/a2a/warm '{"text":"hi"}' -H "Authorization: Bearer $AM_TOKEN"
[ "$LAST_CODE" = "401" ]
ok $? "the a2a path does not accept the ALERTMANAGER token — the credentials are separate (got $LAST_CODE)"
# Unconfigured = refuse. A router deployed without a secret must be inert, not open.
start_intake noauth -e INTAKE_K8S_API=https://k8s:8080 -e INTAKE_K8S_CA_FILE=/tls/k8s.crt
wait_healthy noauth
req POST http://noauth:8080/webhook/alertmanager "$AM_BODY"
{ [ "$LAST_CODE" = "503" ] && has 'source_auth_unconfigured'; }
ok $? "no INTAKE_ALERTMANAGER_TOKEN configured -> 503 refuse, never open (got $LAST_CODE)"
req POST http://noauth:8080/webhook/github "$GH_BODY" -H 'X-GitHub-Event: push' \
  -H "X-Hub-Signature-256: $SIG"
{ [ "$LAST_CODE" = "503" ] && has 'source_auth_unconfigured'; }
ok $? "no INTAKE_GITHUB_SECRET configured -> 503 refuse, never open (got $LAST_CODE)"
req POST http://noauth:8080/a2a/warm '{"text":"x"}'
[ "$LAST_CODE" = "503" ]; ok $? "no INTAKE_A2A_TOKEN configured -> 503 refuse (got $LAST_CODE)"

# ------------------------------------------------------ 5. routing refusals fail closed
note "4. an unmatched or unknown route is refused, never guessed"
req POST http://intake:8080/webhook/alertmanager \
  '{"status":"firing","groupKey":"g9","groupLabels":{"severity":"info"}}' \
  -H "Authorization: Bearer $AM_TOKEN"
{ [ "$LAST_CODE" = "404" ] && has 'no_route'; }
ok $? "an alert matching no trigger is 404 no_route (got $LAST_CODE)"
# A matcher is ALL-of: one satisfied label must not route.
req POST http://intake:8080/webhook/alertmanager \
  '{"status":"resolved","groupKey":"g8","groupLabels":{"severity":"critical"}}' \
  -H "Authorization: Bearer $AM_TOKEN"
{ [ "$LAST_CODE" = "404" ] || { [ "$LAST_CODE" = "202" ] && ! has '"warm"'; }; }
ok $? "a matcher is ALL-of: status=resolved does not satisfy status=firing (got $LAST_CODE)"
req POST http://intake:8080/a2a/nosuchagent '{"text":"x"}' -H "Authorization: Bearer $A2A_TOKEN"
{ [ "$LAST_CODE" = "404" ] && has 'no_route'; }
ok $? "a2a to an agent with no a2a trigger is 404 (got $LAST_CODE)"
req POST http://intake:8080/a2a/cold '{"text":"x"}' -H "Authorization: Bearer $A2A_TOKEN"
[ "$LAST_CODE" = "404" ]
ok $? "a2a to a known agent that declares NO a2a trigger is 404 (got $LAST_CODE)"
req POST http://intake:8080/a2a/../../etc/passwd '{"text":"x"}' -H "Authorization: Bearer $A2A_TOKEN"
[ "$LAST_CODE" = "404" ]; ok $? "a traversal-shaped agent name is 404 (got $LAST_CODE)"
req POST http://intake:8080/webhook/anything '{}'
[ "$LAST_CODE" = "404" ]; ok $? "an unknown webhook path is 404 (got $LAST_CODE)"

# --------------------------------------------------------------- 6. dedup
note "5. a redelivered event dispatches once"
DEDUP_BODY='{"status":"firing","groupKey":"dedup-group","groupLabels":{"severity":"critical"},"commonAnnotations":{"summary":"same event"}}'
req POST http://intake:8080/webhook/alertmanager "$DEDUP_BODY" -H "Authorization: Bearer $AM_TOKEN"
FIRST="$LAST_CODE"
sleep 2
BEFORE=$(grep -c . "$WORK/out/agent.log" 2>/dev/null || echo 0)
req POST http://intake:8080/webhook/alertmanager "$DEDUP_BODY" -H "Authorization: Bearer $AM_TOKEN"
{ [ "$LAST_CODE" = "200" ] && has '"deduped": true'; }
ok $? "the second delivery of one alert group answers 200 deduped:true (first=$FIRST, replay=$LAST_CODE)"
sleep 2
AFTER=$(grep -c . "$WORK/out/agent.log" 2>/dev/null || echo 0)
[ "$BEFORE" = "$AFTER" ]
ok $? "and dispatched NOTHING — the agent saw no second task (before=$BEFORE after=$AFTER)"
# GitHub dedups on X-GitHub-Delivery, which is the id GitHub retries under.
req POST http://intake:8080/webhook/github "$GH_BODY" -H 'X-GitHub-Event: push' \
  -H "X-Hub-Signature-256: $SIG" -H 'X-GitHub-Delivery: gh-delivery-1'
{ [ "$LAST_CODE" = "200" ] && has '"deduped": true'; }
ok $? "a GitHub redelivery of the same X-GitHub-Delivery is deduped (got $LAST_CODE)"
# a2a dedups only on a caller-supplied key; without one, two calls are two tasks.
req POST http://intake:8080/a2a/warm '{"text":"idem"}' -H "Authorization: Bearer $A2A_TOKEN" \
  -H 'Idempotency-Key: idem-1'
req POST http://intake:8080/a2a/warm '{"text":"idem"}' -H "Authorization: Bearer $A2A_TOKEN" \
  -H 'Idempotency-Key: idem-1'
{ [ "$LAST_CODE" = "200" ] && has '"deduped": true'; }
ok $? "an a2a call repeating its Idempotency-Key is deduped (got $LAST_CODE)"

# ------------------------------------------------- 7. what actually reached the agent
note "6. the shaped task: bounded, scrubbed, and carrying no header or credential"
sleep 2
python3 - "$WORK/out/agent.log" "$AGENT_TOKEN" <<'PY'
import json, sys
rows = [json.loads(l) for l in open(sys.argv[1]) if l.strip()]
if not rows:
    print("FAIL no task ever reached the mock agent"); sys.exit(0)
body = json.loads(rows[0]["body"])
print("ok" if rows[0]["auth"] == "Bearer " + sys.argv[2] else "FAIL",
      "intake presents the TARGET agent's own bearer, not the caller's")
print("ok" if isinstance(body.get("text"), str) and body["text"] else "FAIL",
      "the agent receives an A2A task body with text")
print("ok" if set(body) == {"text"} else "FAIL",
      "the task body carries ONLY text — no headers, no raw payload (%s)" % sorted(body))
PY
python3 - "$WORK/out/agent.log" <<'PY' > "$WORK/agentcheck"
import json, sys
rows = [json.loads(l) for l in open(sys.argv[1]) if l.strip()]
out = []
def check(label, cond): out.append(("ok " if cond else "FAIL ") + label)
texts = [json.loads(r["body"])["text"] for r in rows]
check("no task text carries a raw newline injected from a label value",
      all("EVIL-INJECTED-LINE" not in t for t in texts))
print("\n".join(out))
PY
while IFS= read -r line; do
  case "$line" in "ok "*) pass "${line#ok }";; "FAIL "*) fail "${line#FAIL }";; esac
done < "$WORK/agentcheck"

# The bound, proven on the wire. A 200KB summary must not become a 200KB prompt.
BIG=$(printf 'A%.0s' $(seq 1 60000))
req POST http://intake:8080/webhook/alertmanager \
  "{\"status\":\"firing\",\"groupKey\":\"big\",\"groupLabels\":{\"severity\":\"critical\"},\"commonAnnotations\":{\"summary\":\"$BIG\"}}" \
  -H "Authorization: Bearer $AM_TOKEN"
sleep 2
python3 - "$WORK/out/agent.log" <<'PY' > "$WORK/boundcheck"
import json, sys
rows = [json.loads(l) for l in open(sys.argv[1]) if l.strip()]
longest = max((len(json.loads(r["body"])["text"]) for r in rows), default=0)
print(("ok " if longest <= 4096 else "FAIL ")
      + "the task text is bounded at MAX_TASK (longest seen: %d chars)" % longest)
PY
while IFS= read -r line; do
  case "$line" in "ok "*) pass "${line#ok }";; "FAIL "*) fail "${line#FAIL }";; esac
done < "$WORK/boundcheck"

# Control characters in a label value must not forge a line of the report.
req POST http://intake:8080/webhook/alertmanager \
  '{"status":"firing","groupKey":"inject","groupLabels":{"severity":"critical","team":"a\nEVIL-INJECTED-LINE: yes"},"commonAnnotations":{"summary":"x"}}' \
  -H "Authorization: Bearer $AM_TOKEN"
sleep 2
if grep -q 'EVIL-INJECTED-LINE' "$WORK/out/agent.log" 2>/dev/null; then
  # It may legitimately appear as inline text; what must NOT happen is a new line.
  python3 - "$WORK/out/agent.log" <<'PY' > "$WORK/injcheck"
import json, sys
rows = [json.loads(l) for l in open(sys.argv[1]) if l.strip()]
bad = [t for t in (json.loads(r["body"])["text"] for r in rows)
       if any(line.startswith("EVIL-INJECTED-LINE") for line in t.split("\n"))]
print(("FAIL " if bad else "ok ") + "a newline in a label value cannot forge a new line of the task report")
PY
  while IFS= read -r line; do
    case "$line" in "ok "*) pass "${line#ok }";; "FAIL "*) fail "${line#FAIL }";; esac
  done < "$WORK/injcheck"
else
  pass "a newline in a label value cannot forge a new line of the task report"
fi

# ------------------------------------------------------------------ 8. Job dispatch
note "7. a job-dispatch agent gets a one-shot Job, and only that"
req POST http://intake:8080/webhook/alertmanager \
  '{"status":"firing","groupKey":"page-1","groupLabels":{"severity":"page"},"commonAnnotations":{"summary":"paged"}}' \
  -H "Authorization: Bearer $AM_TOKEN"
[ "$LAST_CODE" = "202" ]; ok $? "an alert routed to a job-dispatch agent is accepted (got $LAST_CODE)"
sleep 3
if [ -s "$WORK/out/k8s.log" ]; then
  python3 - "$WORK/out/k8s.log" <<'PY' > "$WORK/jobcheck"
import json, sys
rows = [json.loads(l) for l in open(sys.argv[1]) if l.strip()]
out = []
def check(label, cond): out.append(("ok " if cond else "FAIL ") + label)
check("intake POSTed to the namespaced batch/v1 jobs endpoint",
      rows[0]["path"] == "/apis/batch/v1/namespaces/orbit-agents-prod/jobs")
job = json.loads(rows[0]["body"])
check("the created object is a batch/v1 Job",
      job.get("kind") == "Job" and job.get("apiVersion") == "batch/v1")
spec = job["spec"]["template"]["spec"]
c = spec["containers"][0]
env = {e["name"]: e for e in c["env"]}
# THE switch. Without it the pod starts the HTTP server, the Job never completes, and the
# event is billed as a run that did nothing.
check("the Job carries ORBIT_AGENT_ONESHOT=1",
      env.get("ORBIT_AGENT_ONESHOT", {}).get("value") == "1")
check("the Job runs the FIXED image from the routing config, not anything from the event",
      c["image"] == "ghcr.io/example-core/orbit-agent-runtime:main-0000000")
check("the shaped task rides in ORBIT_AGENT_TASK",
      isinstance(env.get("ORBIT_AGENT_TASK", {}).get("value"), str)
      and "paged" in env["ORBIT_AGENT_TASK"]["value"])
check("the task text on the Job is bounded too",
      len(env.get("ORBIT_AGENT_TASK", {}).get("value", "")) <= 4096)
# Credentials on a Job are POINTERS the kubelet resolves; intake never holds these values.
tok = env.get("ORBIT_AGENT_TOKEN", {})
check("the agent bearer is a secretKeyRef, never a literal intake put on the wire",
      "value" not in tok
      and tok.get("valueFrom", {}).get("secretKeyRef", {}).get("name") == "agent-cold-token")
mod = env.get("CLAUDE_CODE_OAUTH_TOKEN", {})
check("the subscription token is a secretKeyRef, never a literal", "value" not in mod)
check("no credential-routing variable is set on the Job",
      not ({"ANTHROPIC_API_KEY", "ANTHROPIC_BASE_URL", "ANTHROPIC_AUTH_TOKEN",
            "CLAUDE_CODE_USE_BEDROCK", "CLAUDE_CODE_USE_VERTEX"} & set(env)))
check("restartPolicy is Never — a failed event run is not a hot-loop that re-spends",
      spec.get("restartPolicy") == "Never")
check("the Job is not retried (backoffLimit 0): a retry re-runs a task that may have acted",
      job["spec"].get("backoffLimit") == 0)
check("the Job has a wall-clock deadline", isinstance(job["spec"].get("activeDeadlineSeconds"), int))
# Launching something is not delegating to it: the run gets no API token of its own.
check("the Job pod mounts NO ServiceAccount token",
      spec.get("automountServiceAccountToken") is False)
check("the Job runs under the plane's ordinary ServiceAccount, not the router's",
      spec.get("serviceAccountName") == "orbit-agents")
check("the Job pod is non-root with the runtime image's uid",
      spec["securityContext"]["runAsNonRoot"] is True
      and spec["securityContext"]["runAsUser"] == 10001)
check("the container drops all capabilities and cannot escalate",
      c["securityContext"]["allowPrivilegeEscalation"] is False
      and c["securityContext"]["capabilities"]["drop"] == ["ALL"])
print("\n".join(out))
PY
  while IFS= read -r line; do
    case "$line" in "ok "*) pass "${line#ok }";; "FAIL "*) fail "${line#FAIL }";; esac
  done < "$WORK/jobcheck"
else
  fail "intake never called the mock Kubernetes API — no Job body to inspect"
  $DOCKER logs "${RUN}-intake" 2>&1 | tail -10
fi

# The apiserver-side dedup layer: a Job whose name already exists comes back 409, and that
# is a SUPPRESSED duplicate rather than an error. This is what survives an intake restart.
: >"$WORK/out/conflict"
req POST http://intake:8080/webhook/alertmanager \
  '{"status":"firing","groupKey":"page-2","groupLabels":{"severity":"page"},"commonAnnotations":{"summary":"paged again"}}' \
  -H "Authorization: Bearer $AM_TOKEN"
sleep 3
$DOCKER logs "${RUN}-intake" 2>&1 | grep -q 'deduped_by_apiserver'
ok $? "a 409 AlreadyExists from the API server is recorded as a suppressed duplicate"
rm -f "$WORK/out/conflict"

# ---------------------------------------------------- 9. TLS to the Kubernetes API
note "8. the Kubernetes API is reached over VERIFIED TLS"
start_recorder rogue k8s rogue
start_intake untrusted \
  -e INTAKE_ALERTMANAGER_TOKEN="$AM_TOKEN" \
  -e INTAKE_K8S_API=https://k8s:8080 \
  -e INTAKE_K8S_CA_FILE=/tls/rogue-not-a-file \
  -e INTAKE_K8S_TOKEN_FILE=/tls/satoken
wait_healthy untrusted
BEFORE=$(grep -c . "$WORK/out/k8s.log" 2>/dev/null || echo 0)
req POST http://untrusted:8080/webhook/alertmanager \
  '{"status":"firing","groupKey":"tls-1","groupLabels":{"severity":"page"},"commonAnnotations":{"summary":"tls"}}' \
  -H "Authorization: Bearer $AM_TOKEN"
sleep 3
AFTER=$(grep -c . "$WORK/out/k8s.log" 2>/dev/null || echo 0)
[ "$BEFORE" = "$AFTER" ]
ok $? "an API server whose certificate does not verify gets NO request — and no token (before=$BEFORE after=$AFTER)"
$DOCKER logs "${RUN}-untrusted" 2>&1 | grep -q 'job_create_failed'
ok $? "the failed dispatch is logged as a failure, not swallowed"

# ------------------------------------------- 10. a bad routing config is a startup crash
note "9. a routing config defect crashes at startup — it never warns and serves"
crashes() { # crashes <label> <json>
  printf '%s' "$2" > "$WORK/bad.json"
  chmod a+r "$WORK/bad.json"
  timeout 30 $DOCKER run --rm -v "$WORK/bad.json:/etc/orbit/intake/routes.json:ro,z" \
    "$IMG" >"$WORK/bad.log" 2>&1
  RC=$?
  # The intended fail-closed startup-refusal code is EXACTLY 2 (app.py _boot_config and the
  # non-https K8S guard both sys.exit(2)). A merely-nonzero exit could be an infra/permission
  # error masquerading as a config rejection, hiding a regression where the code should have
  # refused-at-startup but errored for an unrelated reason. Assert the exact code.
  [ "$RC" -eq 2 ] \
    && pass "$1 (exit $RC)" || fail "$1 (exit $RC — want the fail-closed startup-refusal code 2; 0 served, 124 hung)"
}
crashes "an UNKNOWN key in the routing config crashes at startup" \
  '{"version":1,"agents":{"a":{"dispatch":"service","url":"http://a/","tokenEnv":"T","surprise":1}},"routes":{"a2a":[{"agent":"a"}]}}'
crashes "an unknown key at the ROOT of the routing config crashes" \
  '{"version":1,"agents":{"a":{"dispatch":"service","url":"http://a/","tokenEnv":"T"}},"routes":{},"extra":true}'
crashes "an unknown trigger kind under routes crashes" \
  '{"version":1,"agents":{"a":{"dispatch":"service","url":"http://a/","tokenEnv":"T"}},"routes":{"kafka":[]}}'
crashes "a route naming an agent the config does not describe crashes" \
  '{"version":1,"agents":{"a":{"dispatch":"service","url":"http://a/","tokenEnv":"T"}},"routes":{"a2a":[{"agent":"ghost"}]}}'
crashes "an EMPTY alert matcher crashes (it would route every alert on the platform)" \
  '{"version":1,"agents":{"a":{"dispatch":"service","url":"http://a/","tokenEnv":"T"}},"routes":{"alert":[{"agent":"a","match":{}}]}}'
# The config is trusted to name an env var on a pod that holds the subscription token.
crashes "a routing config setting ANTHROPIC_BASE_URL on a Job crashes" \
  '{"version":1,"agents":{"a":{"dispatch":"job","job":{"namespace":"n","image":"r/i:t","serviceAccountName":"s","uid":1,"memory":"1Mi","activeDeadlineSeconds":10,"env":{"ANTHROPIC_BASE_URL":"http://evil"}}}},"routes":{"a2a":[{"agent":"a"}]}}'
crashes "a routing config overriding ORBIT_AGENT_TASK on a Job crashes" \
  '{"version":1,"agents":{"a":{"dispatch":"job","job":{"namespace":"n","image":"r/i:t","serviceAccountName":"s","uid":1,"memory":"1Mi","activeDeadlineSeconds":10,"env":{"ORBIT_AGENT_TASK":"do evil"}}}},"routes":{"a2a":[{"agent":"a"}]}}'
crashes "a Job image with no tag crashes (an untagged ref pulls :latest, not the pin)" \
  '{"version":1,"agents":{"a":{"dispatch":"job","job":{"namespace":"n","image":"r/i","serviceAccountName":"s","uid":1,"memory":"1Mi","activeDeadlineSeconds":10}}},"routes":{"a2a":[{"agent":"a"}]}}'
crashes "uid 0 in a Job spec crashes" \
  '{"version":1,"agents":{"a":{"dispatch":"job","job":{"namespace":"n","image":"r/i:t","serviceAccountName":"s","uid":0,"memory":"1Mi","activeDeadlineSeconds":10}}},"routes":{"a2a":[{"agent":"a"}]}}'
crashes "a ConfigMap item path escaping its mount crashes" \
  '{"version":1,"agents":{"a":{"dispatch":"job","job":{"namespace":"n","image":"r/i:t","serviceAccountName":"s","uid":1,"memory":"1Mi","activeDeadlineSeconds":10,"volumes":[{"name":"v","mountPath":"/m","configMap":{"name":"cm","items":[{"key":"k","path":"../escape"}]}}]}}},"routes":{"a2a":[{"agent":"a"}]}}'
crashes "an absent routing config crashes rather than serving an empty route table" \
  ''
# And the control: the SAME harness must accept a good config, or every row above passes
# because the image is simply broken.
printf '%s' '{"version":1,"agents":{"a":{"dispatch":"service","url":"http://a/","tokenEnv":"T"}},"routes":{"a2a":[{"agent":"a"}]}}' > "$WORK/good.json"
chmod a+r "$WORK/good.json"
timeout 8 $DOCKER run --rm -v "$WORK/good.json:/etc/orbit/intake/routes.json:ro,z" "$IMG" \
  >/dev/null 2>&1
[ "$?" -eq 124 ]
ok $? "a VALID routing config starts and stays serving (so the crashes above are real)"

# ------------------------------------------------- 11. no credential ever reaches a log
note "10. no credential appears in any log line or emitted span"
$DOCKER logs "${RUN}-intake" >"$WORK/intake.log" 2>&1
$DOCKER logs "${RUN}-noauth" >>"$WORK/intake.log" 2>&1
$DOCKER logs "${RUN}-untrusted" >>"$WORK/intake.log" 2>&1
$DOCKER logs "${RUN}-ro" >>"$WORK/intake.log" 2>&1
LEAK=0
for secret in "$AM_TOKEN" "$GH_SECRET" "$A2A_TOKEN" "$AGENT_TOKEN"; do
  if grep -qF "$secret" "$WORK/intake.log"; then LEAK=1; echo "    leaked in logs: ${secret%%-*}..."; fi
  if [ -f "$WORK/out/otlp.log" ] && grep -qF "$secret" "$WORK/out/otlp.log"; then
    LEAK=1; echo "    leaked in spans: ${secret%%-*}..."
  fi
done
[ "$LEAK" = "0" ]
ok $? "no intake credential and no agent bearer appears in a log line or a span"
# The generic canary too: any SMOKECANARY substring anywhere is a leak.
! grep -q 'SMOKECANARY' "$WORK/intake.log"
ok $? "no credential-shaped smoke canary appears anywhere in the router's logs"
[ ! -f "$WORK/out/otlp.log" ] || ! grep -q 'SMOKECANARY' "$WORK/out/otlp.log"
ok $? "no credential-shaped smoke canary appears in any emitted span"
# The Authorization header intake PRESENTS is not the one it was given.
! grep -qF "$A2A_TOKEN" "$WORK/out/agent.log" 2>/dev/null
ok $? "the caller's a2a token is never forwarded to the agent"

# ------------------------------------------------------- 12. spans are real, and narrow
note "11. OTel emission is real, observed, and carries no payload"
if [ -f "$WORK/out/otlp.log" ]; then
  grep -q 'intake.start' "$WORK/out/otlp.log"
  ok $? "a startup span is emitted at boot"
  grep -q 'intake.event' "$WORK/out/otlp.log"
  ok $? "a per-event span is emitted"
  grep -q 'orbit.intake.dispatch' "$WORK/out/otlp.log"
  ok $? "the span records the dispatch mode"
  grep -q 'orbit.intake.deduped' "$WORK/out/otlp.log"
  ok $? "the span records the dedup verdict"
  grep -q 'orbit-smoke-intake' "$WORK/out/otlp.log"
  ok $? "resource identity comes from OTEL_SERVICE_NAME (platform-set)"
  # A span must never carry the shaped task: it is attacker-influenced text, stored and
  # rendered in a UI. And an unauthenticated caller must not be able to drive collector
  # traffic at all.
  ! grep -q 'disk full' "$WORK/out/otlp.log"
  ok $? "the shaped task text is NOT put on a span"
  ! grep -q 'unauthenticated' "$WORK/out/otlp.log"
  ok $? "a rejected unauthenticated POST emits no span (no outbound amplification)"
else
  fail "the mock collector received nothing at all"
fi
# A tracing outage must not take the router down, and must not be swallowed.
start_intake nootlp -e INTAKE_ALERTMANAGER_TOKEN="$AM_TOKEN" \
  -e OTEL_EXPORTER_OTLP_ENDPOINT=http://nowhere.invalid:4318
wait_healthy nootlp; NOOTLP_UP=$?
ok $NOOTLP_UP "a tracing outage does not take the router down"
$DOCKER logs "${RUN}-nootlp" 2>&1 | grep -q '"msg": "otel emit failed"'
ok $? "an unreachable collector logs 'otel emit failed' — no silent swallow"

# =====================================================================================
# Phase D.3 adversarial-review fixes. Each stage below is RED if its fix is reverted.
# =====================================================================================

# ---- finding #1: a warm run that outlives the read timeout is ACCEPTED, not failed, and
# its dedup claim is KEPT so the source's re-send cannot double-spend ------------------
note "12. a slow warm run past the dispatch timeout is accepted, claim kept (finding #1)"
$DOCKER run -d --name "${RUN}-slowagent" --network "$NET" --network-alias slowagent \
  -e ROLE=slowagent -e SLEEP=6 \
  -v "$WORK/recorder.py:/r.py:ro,z" -v "$WORK/out:/out:z" \
  "$PY_IMG" python3 -u /r.py >/dev/null && CONTAINERS+=("${RUN}-slowagent")
# A dedicated router with a 3s read timeout: the slow agent answers at 6s, so intake gives
# up on the read while the "run" is still in flight — the exact shape of a real warm run
# whose DISPATCH_TIMEOUT_S was shorter than the agent's task timeout.
start_intake slowintake \
  -e INTAKE_A2A_TOKEN="$A2A_TOKEN" \
  -e INTAKE_AGENT_TOKEN_SLOW="$AGENT_TOKEN" \
  -e INTAKE_DISPATCH_TIMEOUT_S=3 \
  -e INTAKE_K8S_API=https://k8s:8080 -e INTAKE_K8S_CA_FILE=/tls/k8s.crt \
  -e INTAKE_K8S_TOKEN_FILE=/tls/satoken \
  -e OTEL_EXPORTER_OTLP_ENDPOINT=http://collector:8080
wait_healthy slowintake
rm -f "$WORK/out/slowagent.log"
req POST http://slowintake:8080/a2a/slow '{"text":"slow one"}' \
  -H "Authorization: Bearer $A2A_TOKEN" -H 'Idempotency-Key: slow-1'
[ "$LAST_CODE" = "202" ]; ok $? "the slow a2a dispatch is accepted 202 (got $LAST_CODE)"
sleep 9
$DOCKER logs "${RUN}-slowintake" >"$WORK/slow.log" 2>&1
{ grep -q '"outcome": "task_accepted"' "$WORK/slow.log" \
    && ! grep -qE '"msg": "dispatch (failed|crashed)' "$WORK/slow.log"; }
ok $? "a read timeout on a warm run is outcome=task_accepted, NOT a failure"
GOT1=$(grep -c . "$WORK/out/slowagent.log" 2>/dev/null || echo 0)
[ "$GOT1" = "1" ]; ok $? "the slow agent received the task exactly once (got $GOT1)"
req POST http://slowintake:8080/a2a/slow '{"text":"slow one"}' \
  -H "Authorization: Bearer $A2A_TOKEN" -H 'Idempotency-Key: slow-1'
{ [ "$LAST_CODE" = "200" ] && has '"deduped": true'; }
ok $? "a redelivery inside DEDUP_TTL is still suppressed — the claim was kept (got $LAST_CODE)"
sleep 9
GOT2=$(grep -c . "$WORK/out/slowagent.log" 2>/dev/null || echo 0)
[ "$GOT2" = "1" ]; ok $? "the slow agent STILL received exactly one task, no double-spend (got $GOT2)"

# ---- finding #2: two DIFFERENT keyless a2a tasks to a JOB agent must produce two DISTINCT
# Jobs — a constant name would 409 the 2nd against the 1st's lingering Job -------------
note "13. two different keyless a2a tasks to a job agent get two DISTINCT Jobs (finding #2)"
rm -f "$WORK/out/conflict"
K8S_BEFORE=$(grep -c . "$WORK/out/k8s.log" 2>/dev/null || echo 0)
req POST http://intake:8080/a2a/cjob '{"text":"keyless one"}' -H "Authorization: Bearer $A2A_TOKEN"
req POST http://intake:8080/a2a/cjob '{"text":"keyless two"}' -H "Authorization: Bearer $A2A_TOKEN"
sleep 3
python3 - "$WORK/out/k8s.log" "$K8S_BEFORE" <<'PY' > "$WORK/keyless"
import json, sys
rows = [json.loads(l) for l in open(sys.argv[1]) if l.strip()][int(sys.argv[2]):]
names = [b.get("metadata", {}).get("name")
         for b in (json.loads(r["body"]) for r in rows)
         if b.get("metadata", {}).get("labels", {}).get("orbit.local/agent") == "cjob"]
out = []
def check(label, cond): out.append(("ok " if cond else "FAIL ") + label)
check("two keyless a2a tasks created two Jobs (neither swallowed)", len(names) == 2)
check("the two keyless Jobs have DISTINCT names (a constant name would collide/409)",
      len(set(names)) == 2)
print("\n".join(out))
PY
while IFS= read -r line; do
  case "$line" in "ok "*) pass "${line#ok }";; "FAIL "*) fail "${line#FAIL }";; esac
done < "$WORK/keyless"

# ---- finding #3 + NEW-2: the Job env is an ALLOWLIST and a Job volume may mount only the
# skills dir — a dangerous env name or a code-shadowing mountPath crashes at startup ---
note "14. Job env allowlist and mount allowlist crash at startup (findings #3, NEW-2)"
crashes "a Job env setting HTTPS_PROXY is refused at startup (finding #3 allowlist)" \
  '{"version":1,"agents":{"a":{"dispatch":"job","job":{"namespace":"n","image":"r/i:t","serviceAccountName":"s","uid":1,"memory":"1Mi","activeDeadlineSeconds":10,"env":{"HTTPS_PROXY":"http://evil"}}}},"routes":{"a2a":[{"agent":"a"}]}}'
crashes "a Job env setting NODE_OPTIONS is refused at startup (finding #3 allowlist)" \
  '{"version":1,"agents":{"a":{"dispatch":"job","job":{"namespace":"n","image":"r/i:t","serviceAccountName":"s","uid":1,"memory":"1Mi","activeDeadlineSeconds":10,"env":{"NODE_OPTIONS":"--require /x"}}}},"routes":{"a2a":[{"agent":"a"}]}}'
crashes "a Job env setting CLAUDE_CODE_OAUTH_TOKEN as a LITERAL is refused (finding #3)" \
  '{"version":1,"agents":{"a":{"dispatch":"job","job":{"namespace":"n","image":"r/i:t","serviceAccountName":"s","uid":1,"memory":"1Mi","activeDeadlineSeconds":10,"env":{"CLAUDE_CODE_OAUTH_TOKEN":"literal"}}}},"routes":{"a2a":[{"agent":"a"}]}}'
crashes "a Job volume mounting /app (the runtime's own code) is refused (NEW-2)" \
  '{"version":1,"agents":{"a":{"dispatch":"job","job":{"namespace":"n","image":"r/i:t","serviceAccountName":"s","uid":1,"memory":"1Mi","activeDeadlineSeconds":10,"volumes":[{"name":"v","mountPath":"/app","configMap":{"name":"cm","items":[{"key":"k","path":"p"}]}}]}}},"routes":{"a2a":[{"agent":"a"}]}}'
# The CRITICAL the first NEW-2 cut missed: a '..'-traversal mountPath that starts under the
# skills prefix but resolves (runc/normpath) to /app — must be refused BEFORE apply time.
crashes "a Job mountPath traversing OUT of the skills dir to /app is refused (NEW-2 traversal)" \
  '{"version":1,"agents":{"a":{"dispatch":"job","job":{"namespace":"n","image":"r/i:t","serviceAccountName":"s","uid":1,"memory":"1Mi","activeDeadlineSeconds":10,"volumes":[{"name":"v","mountPath":"/home/orbit/.claude/skills/../../../../app","configMap":{"name":"cm","items":[{"key":"k","path":"p"}]}}]}}},"routes":{"a2a":[{"agent":"a"}]}}'
crashes "a Job mountPath with a bare '..' segment is refused (NEW-2 traversal)" \
  '{"version":1,"agents":{"a":{"dispatch":"job","job":{"namespace":"n","image":"r/i:t","serviceAccountName":"s","uid":1,"memory":"1Mi","activeDeadlineSeconds":10,"volumes":[{"name":"v","mountPath":"/home/orbit/.claude/skills/..","configMap":{"name":"cm","items":[{"key":"k","path":"p"}]}}]}}},"routes":{"a2a":[{"agent":"a"}]}}'
# The LOW: a config-set OTLP exporter endpoint would redirect the agent Job's telemetry to
# an attacker collector — only the two OTEL identity names are allowlisted on a Job env.
crashes "a Job env setting OTEL_EXPORTER_OTLP_ENDPOINT is refused (telemetry redirect)" \
  '{"version":1,"agents":{"a":{"dispatch":"job","job":{"namespace":"n","image":"r/i:t","serviceAccountName":"s","uid":1,"memory":"1Mi","activeDeadlineSeconds":10,"env":{"OTEL_EXPORTER_OTLP_ENDPOINT":"http://evil:4318"}}}},"routes":{"a2a":[{"agent":"a"}]}}'
# The control: a Job that sets ONLY allowlisted names and mounts under the skills dir starts.
printf '%s' '{"version":1,"agents":{"a":{"dispatch":"job","job":{"namespace":"n","image":"r/i:t","serviceAccountName":"s","uid":1,"memory":"1Mi","activeDeadlineSeconds":10,"env":{"ORBIT_AGENT_NAME":"a","OTEL_SERVICE_NAME":"x"},"volumes":[{"name":"v","mountPath":"/home/orbit/.claude/skills","configMap":{"name":"cm","items":[{"key":"k","path":"k/SKILL.md"}]}}]}}},"routes":{"a2a":[{"agent":"a"}]}}' > "$WORK/good2.json"
chmod a+r "$WORK/good2.json"
timeout 8 $DOCKER run --rm -v "$WORK/good2.json:/etc/orbit/intake/routes.json:ro,z" "$IMG" >/dev/null 2>&1
[ "$?" -eq 124 ]
ok $? "an allowlisted-env, skills-mounted Job config starts and serves (so the crashes are real)"

# ---- finding #4: a non-https Kubernetes API base is refused at STARTUP (the SA token
# would otherwise ride cleartext), and an https base is verified against ONLY the mounted
# cluster CA at CALL time ---------------------------------------------------------------
note "15. a non-https Kubernetes API base is refused at startup (finding #4)"
timeout 20 $DOCKER run --rm \
  -v "$WORK/routes.json:/etc/orbit/intake/routes.json:ro,z" \
  -e INTAKE_K8S_API=http://k8s:8080 "$IMG" >"$WORK/httpapi.log" 2>&1
RC=$?
{ [ "$RC" -ne 0 ] && [ "$RC" -ne 124 ]; }
ok $? "http:// INTAKE_K8S_API exits non-zero at startup (rc=$RC — 0 served, 124 hung)"
grep -q 'must be https' "$WORK/httpapi.log"
ok $? "the startup refusal names the cleartext-token reason"

note "16. an API cert that does not chain to the mounted cluster CA is refused at call time"
start_intake wrongca \
  -e INTAKE_ALERTMANAGER_TOKEN="$AM_TOKEN" \
  -e INTAKE_K8S_API=https://k8s:8080 \
  -e INTAKE_K8S_CA_FILE=/tls/rogue.crt \
  -e INTAKE_K8S_TOKEN_FILE=/tls/satoken
wait_healthy wrongca
WBEFORE=$(grep -c . "$WORK/out/k8s.log" 2>/dev/null || echo 0)
req POST http://wrongca:8080/webhook/alertmanager \
  '{"status":"firing","groupKey":"wrongca-1","groupLabels":{"severity":"page"},"commonAnnotations":{"summary":"x"}}' \
  -H "Authorization: Bearer $AM_TOKEN"
sleep 3
WAFTER=$(grep -c . "$WORK/out/k8s.log" 2>/dev/null || echo 0)
[ "$WBEFORE" = "$WAFTER" ]
ok $? "the API server (cert not signed by the mounted CA) gets NO request and no token (before=$WBEFORE after=$WAFTER)"
$DOCKER logs "${RUN}-wrongca" 2>&1 | grep -q 'job_create_failed'
ok $? "the failed dispatch is logged, not swallowed"

# ---- finding #5: unknown HTTP methods must not disclose the Python/BaseHTTP version -----
note "17. an unknown HTTP method discloses no server version banner (finding #5)"
BANNER=$($DOCKER run --rm --network "$NET" "$CURL_IMG" -s -i -X PUT \
  http://intake:8080/webhook/github 2>/dev/null)
if printf '%s' "$BANNER" | grep -i '^Server:' | grep -qiE 'Python/|BaseHTTP'; then
  fail "the error-page Server header leaks the Python/BaseHTTP version"
else
  pass "the error-page Server header discloses no Python/BaseHTTP version"
fi

# ---- NEW-4: a stale-Job 409 outside the in-memory dedup window is a non-ok WARN, not a
# green success (a genuine re-fire may have been silently dropped) ---------------------
note "18. a stale-Job 409 is surfaced as a WARN, not billed a green success (NEW-4)"
: >"$WORK/out/conflict"
req POST http://intake:8080/webhook/alertmanager \
  '{"status":"firing","groupKey":"warn-409","groupLabels":{"severity":"page"},"commonAnnotations":{"summary":"stale"}}' \
  -H "Authorization: Bearer $AM_TOKEN"
sleep 3
$DOCKER logs "${RUN}-intake" >"$WORK/intake.log" 2>&1
{ grep -q '"level": "WARN"' "$WORK/intake.log" \
    && grep 'deduped_by_apiserver' "$WORK/intake.log" | grep -q 'may have been suppressed'; }
ok $? "a 409 outside the in-memory dedup window logs a WARN, not a silent success (NEW-4)"
rm -f "$WORK/out/conflict"

# ---- NEW-3: the dispatch-outcome counter is exposed for Prometheus, carrying no secret --
note "19. the dispatch-outcome metric is exposed and leaks no credential (NEW-3)"
req GET http://intake:8080/metrics
{ [ "$LAST_CODE" = "200" ] && has 'orbit_intake_dispatch_total'; }
ok $? "GET /metrics exposes the dispatch-outcome counter (got $LAST_CODE)"
has 'outcome="deduped"'; ok $? "the counter records the deduped outcome seen in the dedup tests"
lacks 'SMOKECANARY'; ok $? "no credential-shaped canary appears in /metrics"

note "result"
echo "  $PASSED passed, $FAILED failed"
[ "$FAILED" -eq 0 ] || exit 1
exit 0
