#!/usr/bin/env bash
# Four read-only assertions after a ship. The tag has landed in git; this
# script says whether the cluster is actually running it.
#
#   scripts/assert-deployed.sh <project> <env> <component> <tag> --since <RFC3339|unix>
#
# Prints one `STEP <name> OK|FAIL <detail>` line per assertion and exits 1
# at the first failure with a final line `LANDED NOT DEPLOYED <assertion>`.
# Usage errors print `LANDED NOT DEPLOYED args` last and no STEP line.
#
#   image    every non-terminating pod matching the chart labels has the
#            container named after the component carrying the expected
#            reference ${ORBIT_REGISTRY}/${project}-${component}:${tag}
#            exactly, or the digest form of that repo whose imageID names
#            the same repo. Pod spec, never a Deployment or Argo status.
#            Active old-ReplicaSet pods are in the set; terminating pods
#            are not. Empty set fails.
#   rollout  observedGeneration == generation; updatedReplicas ==
#            readyReplicas == replicas == availableReplicas; no OOMKilled
#            lastState; no CrashLoopBackOff / ImagePullBackOff / ErrImagePull
#            waiting.
#   ready    snapshot the set (name → Ready, restartCount), wait one
#            readinessProbe.periodSeconds (10s when absent, said in the STEP
#            detail), snapshot again: same names, Ready in both, restartCount
#            unchanged, no pod added or removed.
#   log      --since is required (the ship time). A Loki query_range line
#            from a pod in the set whose creationTimestamp >= since, queried
#            {namespace="<ns>", pod=~"<project>-<component>-.*"} from since
#            to now, direction=forward, limit=1000 paging by last timestamp;
#            polled every 5s until since+60s (or 60s from now if since is
#            older). kind: web also needs HTTP 200 from the first public host.
#
# Loki: ORBIT_LOKI_URL (default http://loki.loki.svc:3100). query_range is
# ${ORBIT_LOKI_URL}/loki/api/v1/query_range.
# Web GET: ORBIT_WEB_AUTH_FILE (optional) is a file of one line user:password
# sent as HTTP basic auth via curl -K - (never -u; never in argv or logs).
#
# Read-only against the cluster: get only. Namespace is <project>-<env>.
# Chart labels (charts/service/templates/_helpers.tpl service.labels /
# service.selectorLabels): app.kubernetes.io/name=<component>,
# app.kubernetes.io/instance=<project>. Catalog via catalog/projects.yaml.
set -euo pipefail

. "$(dirname "$0")/lib-instance.sh" || exit 2

usage() {
  echo "usage: $0 <project> <env> <component> <tag> --since <RFC3339|unix>" >&2
  echo "LANDED NOT DEPLOYED args"
  exit 1
}

SINCE_ARG=""
POSITIONAL=()
while [ $# -gt 0 ]; do
  case "$1" in
    --since)
      [ $# -ge 2 ] || usage
      SINCE_ARG="$2"
      shift 2
      ;;
    --since=*)
      SINCE_ARG="${1#--since=}"
      shift
      ;;
    -*)
      usage
      ;;
    *)
      POSITIONAL+=("$1")
      shift
      ;;
  esac
done
[ "${#POSITIONAL[@]}" -eq 4 ] || usage
PROJECT="${POSITIONAL[0]}" ENV_NAME="${POSITIONAL[1]}" COMPONENT="${POSITIONAL[2]}" TAG="${POSITIONAL[3]}"
case "$PROJECT$ENV_NAME$COMPONENT$TAG" in
  *[!A-Za-z0-9._-]*|'') usage ;;
esac
[ -n "$SINCE_ARG" ] || usage

NS="${PROJECT}-${ENV_NAME}"
DEPLOY="${PROJECT}-${COMPONENT}"
SEL="app.kubernetes.io/name=${COMPONENT},app.kubernetes.io/instance=${PROJECT}"
LOKI_URL="${ORBIT_LOKI_URL:-http://loki.loki.svc:3100}"
CATALOG="${ORBIT_CATALOG:-$_ORBIT_ROOT/catalog/projects.yaml}"
# The reference the pods run is the catalog component's image plus the tag (what the chart
# renders and what ship.sh shipped); the registry composition is the fallback for a catalog
# that does not carry the component. Resolved below, after the catalog is read.
EXPECTED=""

step_ok()   { echo "STEP $1 OK $2"; }
step_fail() {
  echo "STEP $1 FAIL $2"
  echo "LANDED NOT DEPLOYED $1"
  exit 1
}

# kubectl get -o json into KUBE_JSON. Never run this (or step_fail) inside
# command substitution: the STEP/LANDED lines must reach stdout.
KUBE_JSON=""
kube_get() {
  local assertion="$1"; shift
  local rc=0
  KUBE_JSON=$(kubectl -n "$NS" --request-timeout=30s "$@" -o json) || rc=$?
  if [ "$rc" -ne 0 ]; then
    step_fail "$assertion" "kubectl $* failed in $NS"
  fi
}

PY_OUT=""
run_py() {
  local assertion="$1"; shift
  local rc=0
  PY_OUT=$("$@") || rc=$?
  if [ "$rc" -ne 0 ]; then
    step_fail "$assertion" "parser failed"
  fi
}

# stdin: pod list JSON. argv: tag mode component [expected].
# The pod set is every item whose metadata.deletionTimestamp is empty.
# kubectl already applied the chart selector labels.
py_pods() {
  python3 -c '
import json, sys
from datetime import datetime
tag, mode = sys.argv[1], sys.argv[2]
component = sys.argv[3] if len(sys.argv) > 3 else ""
expected = sys.argv[4] if len(sys.argv) > 4 else ""
d = json.load(sys.stdin)
items = d.get("items") or []

def selected():
    out = []
    for p in items:
        meta = p.get("metadata") or {}
        if meta.get("deletionTimestamp"):
            continue
        out.append(p)
    return out

def statuses(p):
    st = p.get("status") or {}
    return (st.get("containerStatuses") or []) + (st.get("initContainerStatuses") or [])

def created_unix(p):
    ts = ((p.get("metadata") or {}).get("creationTimestamp") or "").strip()
    if not ts:
        return 0
    if ts.endswith("Z"):
        ts = ts[:-1] + "+00:00"
    try:
        return int(datetime.fromisoformat(ts).timestamp())
    except (TypeError, ValueError):
        return 0

def is_ready(p):
    conds = (p.get("status") or {}).get("conditions") or []
    return next((c.get("status") == "True" for c in conds if c.get("type") == "Ready"), False)

def restart_count(p):
    n = 0
    for cs in statuses(p):
        try:
            n += int(cs.get("restartCount") or 0)
        except (TypeError, ValueError):
            pass
    return n

def repo_of(ref):
    s = (ref or "").strip()
    if "://" in s:
        s = s.split("://", 1)[1]
    if "@" in s:
        return s.split("@", 1)[0]
    slash = s.rfind("/")
    colon = s.rfind(":")
    if colon > slash:
        return s[:colon]
    return s

def image_matches(img, want, image_id):
    if img == want:
        return True
    exp_repo = repo_of(want)
    if not exp_repo:
        return False
    prefix = exp_repo + "@sha256:"
    if not img.startswith(prefix):
        return False
    digest = img[len(prefix):]
    if not digest:
        return False
    for ch in digest:
        if ch not in "0123456789abcdefABCDEF":
            return False
    return repo_of(image_id) == exp_repo

pods = selected()

if mode == "image":
    if not pods:
        print("NONE")
        raise SystemExit(0)
    bad = []
    for p in pods:
        name = (p.get("metadata") or {}).get("name") or "?"
        cons = (p.get("spec") or {}).get("containers") or []
        named = [c for c in cons if (c.get("name") or "") == component]
        if not named:
            bad.append("%s=" % name)
            continue
        by_name = {}
        for cs in ((p.get("status") or {}).get("containerStatuses") or []):
            by_name[cs.get("name") or ""] = cs
        for c in named:
            img = c.get("image") or ""
            image_id = (by_name.get(c.get("name") or "") or {}).get("imageID") or ""
            if not image_matches(img, expected, image_id):
                bad.append("%s=%s" % (name, img))
    if bad:
        print("BAD " + ";".join(bad))
    else:
        print("OK %d" % len(pods))

elif mode == "oom":
    hits = []
    for p in pods:
        name = (p.get("metadata") or {}).get("name") or "?"
        for cs in statuses(p):
            reason = ((cs.get("lastState") or {}).get("terminated") or {}).get("reason")
            if reason == "OOMKilled":
                hits.append("%s:%s" % (name, cs.get("name") or "?"))
    print("OOM " + ",".join(hits) if hits else "OK")

elif mode == "waiting":
    hits = []
    for p in pods:
        name = (p.get("metadata") or {}).get("name") or "?"
        for cs in statuses(p):
            reason = ((cs.get("state") or {}).get("waiting") or {}).get("reason")
            if reason in ("CrashLoopBackOff", "ImagePullBackOff", "ErrImagePull"):
                hits.append("%s:%s:%s" % (name, cs.get("name") or "?", reason))
    print("WAIT " + ",".join(hits) if hits else "OK")

elif mode == "period":
    seen_probe = False
    periods = []
    for p in pods:
        for c in (p.get("spec") or {}).get("containers") or []:
            if component and (c.get("name") or "") != component:
                continue
            probe = c.get("readinessProbe")
            if not probe:
                continue
            seen_probe = True
            if "periodSeconds" in probe:
                try:
                    periods.append(int(probe["periodSeconds"]))
                except (TypeError, ValueError):
                    pass
    if not seen_probe:
        print("NONE")
    else:
        print(periods[0] if periods else 10)

elif mode == "snapshot":
    if not pods:
        print("NONE")
        raise SystemExit(0)
    for p in pods:
        name = (p.get("metadata") or {}).get("name") or ""
        if not name:
            continue
        print("%s %d %d %d" % (name, 1 if is_ready(p) else 0, restart_count(p), created_unix(p)))
' "$1" "$2" "${3-}" "${4-}"
}

py_deploy() {
  python3 -c '
import json, sys
d = json.load(sys.stdin)
st = d.get("status") or {}
meta = d.get("metadata") or {}
def n(src, key):
    v = src.get(key)
    if v is None:
        return None
    try:
        return int(v)
    except (TypeError, ValueError):
        return None
gen, obs = n(meta, "generation"), n(st, "observedGeneration")
u, r, t, a = n(st, "updatedReplicas"), n(st, "readyReplicas"), n(st, "replicas"), n(st, "availableReplicas")
if gen is None or obs is None:
    print("GENERATION %s %s" % (obs, gen))
elif obs != gen:
    print("GENERATION %s %s" % (obs, gen))
elif u is None or r is None or t is None or a is None:
    print("MISSING %s %s %s %s" % (u, r, t, a))
elif u == r == t == a:
    print("OK %d" % t)
else:
    print("MISMATCH %s %s %s %s" % (u, r, t, a))
'
}

py_catalog() {
  python3 - "$1" "$2" "$3" "$4" "$5" "$6" <<'PY'
import re, sys, yaml
path, pid, env, comp, domain, dev = sys.argv[1:7]
try:
    catalog = yaml.safe_load(open(path)) or {}
except Exception as e:
    print("ERR")
    print("")
    # the parser's message quotes the file; the exception TYPE is the diagnostic
    print("READERR %s" % type(e).__name__)
    raise SystemExit(0)
if not isinstance(catalog, dict):
    print("ERR")
    print("")
    print("READERR catalog is not a mapping")
    raise SystemExit(0)
kind, hosts, found, image = "web", [], False, ""
for p in catalog.get("projects") or []:
    if not isinstance(p, dict) or p.get("id") != pid:
        continue
    found = True
    for c in p.get("components") or []:
        if isinstance(c, dict) and c.get("name") == comp:
            kind = c.get("kind") or "web"
            # the reference the chart renders (charts/service/templates/_helpers.tpl) is the
            # catalog's image plus the tag: shown only in an image reference's shape
            img = c.get("image")
            if isinstance(img, str) and re.fullmatch(r"[a-z0-9][a-z0-9._/-]{0,255}", img) and "@" not in img:
                image = img
            break
    envcfg = (p.get("environments") or {}).get(env) or {}
    if not isinstance(envcfg, dict):
        envcfg = {}
    if envcfg.get("expose") is False:
        hosts = []
    else:
        hosts = list(envcfg.get("hostnames") or [])
        if not hosts:
            base = domain if env == "prod" else dev
            hosts = ["%s.%s" % (pid, base)]
        hosts += [c["host"] for c in (envcfg.get("customDomains") or [])
                  if isinstance(c, dict) and c.get("host")]
        seen, out = set(), []
        for h in hosts:
            h = str(h).lower().rstrip(".")
            if h and h not in seen:
                seen.add(h)
                out.append(h)
        hosts = out
    break
print(kind)
print(hosts[0] if hosts else "")
print("FOUND" if found else "MISSING")
print(image)
PY
}

py_loki() {
  python3 -c '
import json, sys
meta, since = {}, int(sys.argv[2]) if sys.argv[2] else 0
for line in (sys.argv[1].split("\n") if sys.argv[1] else []):
    parts = line.split()
    if len(parts) >= 2:
        try:
            meta[parts[0]] = int(parts[1])
        except ValueError:
            pass
try:
    d = json.load(sys.stdin)
except Exception:
    print("QUERYERR")
    raise SystemExit(0)
if d.get("status") != "success":
    print("QUERYERR")
    raise SystemExit(0)
n_match = 0
n_values = 0
last_ts = 0
for s in (d.get("data") or {}).get("result") or []:
    pod = (s.get("stream") or {}).get("pod") or ""
    created = meta.get(pod)
    for pair in s.get("values") or []:
        n_values += 1
        try:
            ts = int(pair[0])
        except (TypeError, ValueError, IndexError):
            ts = 0
        if ts > last_ts:
            last_ts = ts
        if created is None:
            continue
        if created < since:
            continue
        n_match += 1
if n_match:
    print("MATCH %d" % n_match)
else:
    nxt = str(last_ts + 1) if last_ts else ""
    print("NONE %d %s" % (n_values, nxt))
' "$1" "$2"
}

py_ready_cmp() {
  python3 -c '
import sys
def load(raw):
    dest = {}
    for line in (raw or "").splitlines():
        parts = line.split()
        if len(parts) >= 3:
            try:
                dest[parts[0]] = (int(parts[1]), int(parts[2]))
            except ValueError:
                pass
    return dest
old, new = load(sys.argv[1]), load(sys.argv[2])
if not old:
    print("NONE")
    raise SystemExit(0)
for name in old:
    if name not in new:
        print("GONE %s" % name)
        raise SystemExit(0)
for name in new:
    if name not in old:
        print("ADDED %s" % name)
        raise SystemExit(0)
for name, (ready, rst) in old.items():
    nready, nrst = new[name]
    if ready != 1 or nready != 1:
        print("UNREADY %s" % name)
        raise SystemExit(0)
    if rst != nrst:
        print("RESTARTS %s %d->%d" % (name, rst, nrst))
        raise SystemExit(0)
print("OK %d" % len(old))
' "$1" "$2"
}

since_unix() {
  local v="$1"
  [ -n "$v" ] || return 1
  case "$v" in
    *[!0-9]*) date -u -d "$v" +%s ;;
    *)        printf '%s\n' "$v" ;;
  esac
}

rc=0
SINCE_UNIX=$(since_unix "$SINCE_ARG") || rc=$?
[ "$rc" -eq 0 ] && [ -n "$SINCE_UNIX" ] || usage
case "$SINCE_UNIX" in
  ''|*[!0-9]*) usage ;;
esac

# ── (a) image ────────────────────────────────────────────────────────────────
rc=0
CAT_IMAGE=$(py_catalog "$CATALOG" "$PROJECT" "$ENV_NAME" "$COMPONENT" \
  "${ORBIT_DOMAIN:-}" "${ORBIT_DEV_DOMAIN:-}" | sed -n '4p') || rc=$?
[ "$rc" -eq 0 ] || CAT_IMAGE=""
if [ -n "$CAT_IMAGE" ]; then
  EXPECTED="${CAT_IMAGE}:${TAG}"
else
  EXPECTED="${ORBIT_REGISTRY}/${PROJECT}-${COMPONENT}:${TAG}"
fi

kube_get image get pods -l "$SEL"
PODS="$KUBE_JSON"
run_py image py_pods "$TAG" image "$COMPONENT" "$EXPECTED" <<<"$PODS"
case "$PY_OUT" in
  OK\ *) step_ok image "${PY_OUT#OK } pod(s) $EXPECTED" ;;
  NONE)  step_fail image "empty pod set for $SEL in $NS" ;;
  BAD\ *) step_fail image "${PY_OUT#BAD }" ;;
  *)     step_fail image "image check returned: $PY_OUT" ;;
esac

# ── (b) rollout ──────────────────────────────────────────────────────────────
kube_get rollout get deploy "$DEPLOY"
DEP="$KUBE_JSON"
run_py rollout py_deploy <<<"$DEP"
ROLL="$PY_OUT"
case "$ROLL" in
  OK\ *) ;;
  GENERATION\ *) step_fail rollout "observedGeneration!=generation on $DEPLOY ($ROLL)" ;;
  MISSING\ *) step_fail rollout "replica fields missing on $DEPLOY ($ROLL)" ;;
  MISMATCH\ *) step_fail rollout "updatedReplicas/readyReplicas/replicas/availableReplicas $ROLL" ;;
  *) step_fail rollout "rollout check returned: $ROLL" ;;
esac
run_py rollout py_pods "$TAG" oom "$COMPONENT" <<<"$PODS"
case "$PY_OUT" in
  OK) ;;
  OOM\ *) step_fail rollout "OOMKilled lastState: ${PY_OUT#OOM }" ;;
  *) step_fail rollout "OOM check returned: $PY_OUT" ;;
esac
run_py rollout py_pods "$TAG" waiting "$COMPONENT" <<<"$PODS"
case "$PY_OUT" in
  OK) ;;
  WAIT\ *) step_fail rollout "waiting: ${PY_OUT#WAIT }" ;;
  *) step_fail rollout "waiting check returned: $PY_OUT" ;;
esac
step_ok rollout "${ROLL#OK }==${ROLL#OK }==${ROLL#OK }, no OOMKilled/CrashLoopBackOff/ImagePullBackOff/ErrImagePull"

# ── (c) ready ────────────────────────────────────────────────────────────────
run_py ready py_pods "$TAG" snapshot "$COMPONENT" <<<"$PODS"
SNAP1="$PY_OUT"
case "$SNAP1" in
  NONE) step_fail ready "empty pod set to probe" ;;
esac
run_py ready py_pods "$TAG" period "$COMPONENT" <<<"$PODS"
PERIOD_RAW="$PY_OUT"
NO_PROBE=0
case "$PERIOD_RAW" in
  NONE)
    PERIOD=10
    NO_PROBE=1
    ;;
  ''|*[!0-9]*) PERIOD=10 ;;
  *) PERIOD="$PERIOD_RAW" ;;
esac
sleep "$PERIOD"
kube_get ready get pods -l "$SEL"
PODS2="$KUBE_JSON"
run_py ready py_pods "$TAG" snapshot "$COMPONENT" <<<"$PODS2"
SNAP2="$PY_OUT"
run_py ready py_ready_cmp "$SNAP1" "$SNAP2"
case "$PY_OUT" in
  OK\ *)
    if [ "$NO_PROBE" -eq 1 ]; then
      step_ok ready "no readinessProbe, waited ${PERIOD}s, Ready (${PY_OUT#OK } pod(s))"
    else
      step_ok ready "Ready across ${PERIOD}s (${PY_OUT#OK } pod(s))"
    fi
    ;;
  NONE) step_fail ready "empty pod set after ${PERIOD}s" ;;
  GONE\ *) step_fail ready "${PY_OUT#GONE } left the set after ${PERIOD}s" ;;
  ADDED\ *) step_fail ready "${PY_OUT#ADDED } joined the set after ${PERIOD}s" ;;
  UNREADY\ *)
    if [ "$NO_PROBE" -eq 1 ]; then
      step_fail ready "no readinessProbe, waited ${PERIOD}s, Ready lost: ${PY_OUT#UNREADY }"
    else
      step_fail ready "not Ready: ${PY_OUT#UNREADY }"
    fi
    ;;
  RESTARTS\ *) step_fail ready "restartCount ${PY_OUT#RESTARTS }" ;;
  *) step_fail ready "ready check returned: $PY_OUT" ;;
esac

# ── (d) log ──────────────────────────────────────────────────────────────────
POD_META=$(printf '%s\n' "$SNAP2" | awk 'NF >= 4 {print $1, $4}')
[ -n "$POD_META" ] || step_fail log "no pods in the set to query"
QUERY="{namespace=\"${NS}\", pod=~\"${DEPLOY}-.*\"}"

HIT=""
DEADLINE=""
found_line=0
while true; do
  NOW=$(date +%s)
  if [ -z "$DEADLINE" ]; then
    DEADLINE=$((SINCE_UNIX + 60))
    if [ "$DEADLINE" -le "$NOW" ]; then
      DEADLINE=$((NOW + 60))
    fi
  fi
  if [ "$NOW" -lt "$SINCE_UNIX" ]; then
    sleep 5
    continue
  fi
  start_ns="${SINCE_UNIX}000000000"
  end_ns="${NOW}000000000"
  while true; do
    rc=0
    LOKI_BODY=$(curl -sS --max-time 25 --get \
      --data-urlencode "query=$QUERY" \
      --data-urlencode "start=$start_ns" \
      --data-urlencode "end=$end_ns" \
      --data-urlencode "limit=1000" \
      --data-urlencode "direction=forward" \
      "${LOKI_URL%/}/loki/api/v1/query_range") || rc=$?
    if [ "$rc" -ne 0 ]; then
      step_fail log "Loki query_range failed for $QUERY"
    fi
    run_py log py_loki "$POD_META" "$SINCE_UNIX" <<<"$LOKI_BODY"
    case "$PY_OUT" in
      QUERYERR|'') step_fail log "Loki query_range failed for $QUERY" ;;
      MATCH\ *)
        HIT=$(printf '%s\n' "$PY_OUT" | awk '{print $2}')
        found_line=1
        break
        ;;
      NONE\ *)
        NVAL=$(printf '%s\n' "$PY_OUT" | awk '{print $2}')
        NEXT=$(printf '%s\n' "$PY_OUT" | awk '{print $3}')
        case "$NVAL" in
          ''|*[!0-9]*) step_fail log "Loki parse returned: $PY_OUT" ;;
        esac
        if [ "$NVAL" -lt 1000 ]; then
          break
        fi
        [ -n "$NEXT" ] || break
        start_ns="$NEXT"
        ;;
      *) step_fail log "Loki parse returned: $PY_OUT" ;;
    esac
  done
  if [ "$found_line" -eq 1 ]; then
    break
  fi
  if [ "$NOW" -ge "$DEADLINE" ]; then
    step_fail log "no Loki line from new pods in 60s ($QUERY)"
  fi
  sleep 5
done

rc=0
KIND_HOST=$(py_catalog "$CATALOG" "$PROJECT" "$ENV_NAME" "$COMPONENT" \
  "${ORBIT_DOMAIN:-}" "${ORBIT_DEV_DOMAIN:-}") || rc=$?
if [ "$rc" -ne 0 ]; then
  step_fail log "catalog read error"
fi
KIND=$(printf '%s\n' "$KIND_HOST" | sed -n '1p')
HOST=$(printf '%s\n' "$KIND_HOST" | sed -n '2p')
FOUND=$(printf '%s\n' "$KIND_HOST" | sed -n '3p')
case "$KIND" in
  ERR) step_fail log "catalog read error${FOUND:+: $FOUND}" ;;
esac
case "$FOUND" in
  READERR*) step_fail log "catalog read error: $FOUND" ;;
esac

if [ "$KIND" = "web" ]; then
  [ -n "$HOST" ] || step_fail log "kind web has no public hostname in the catalog"
  rc=0
  CODE=$(
    {
      if [ -n "${ORBIT_WEB_AUTH_FILE:-}" ]; then
        # curl's config grammar: backslash escapes, a double quote ends the value; the
        # file's bytes are escaped the way the verify stages' curluser() does, and CR/LF
        # are stripped, so a password holding \\ or \" reaches the server intact
        python3 -c 'import sys; s = open(sys.argv[1], "rb").read().decode("utf-8", "surrogateescape").replace("\r", "").replace("\n", ""); sys.stdout.write("user = \"" + s.replace("\\", "\\\\").replace("\"", "\\\"") + "\"\n")' "$ORBIT_WEB_AUTH_FILE"
      fi
    } | curl -sS -o /dev/null -w '%{http_code}' --max-time 15 -K - \
      "https://${HOST}/"
  ) || rc=$?
  if [ "$rc" -ne 0 ] || [ "$CODE" != "200" ]; then
    if [ "$CODE" = "401" ] && [ -z "${ORBIT_WEB_AUTH_FILE:-}" ]; then
      step_fail log "https://${HOST}/ returned 401 (basic auth; set ORBIT_WEB_AUTH_FILE)"
    fi
    step_fail log "https://${HOST}/ returned ${CODE:-000}"
  fi
  step_ok log "Loki ${HIT} stream(s), https://${HOST}/ 200"
else
  step_ok log "Loki ${HIT} stream(s), http skipped (kind $KIND)"
fi
