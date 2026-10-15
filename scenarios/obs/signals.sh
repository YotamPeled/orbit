#!/usr/bin/env bash
# docs/V1.md §6.1 — signals end to end, each with a time bound:
#   * metrics carry the catalog's names (project, env, component) in Prometheus;
#   * a unique marker line reaches Loki within 2 minutes, found BY those labels;
#   * one trace runs bot -> Traefik -> api -> engine -> Postgres in Tempo;
#   * trace -> logs: Loki holds lines carrying that trace's id;
#   * metric -> trace: an exemplar on the span-metrics latency histogram names a trace Tempo has.
#
#   scenarios/obs/signals.sh <out-dir>
set -uo pipefail
here=$(cd "$(dirname "$0")" && pwd)
OUT=${1:?usage: signals.sh <out-dir>}; mkdir -p "$OUT"; export SCENARIO_OUT="$OUT"
. "$here/../lib.sh"
: "${ORBIT_DESK:=$ORBIT_INSTANCE_DIR/scripts/desk.sh}"; export ORBIT_DESK
PASS=0 FAIL=0
ok(){ echo "  ok    $1"; PASS=$((PASS+1)); }
bad(){ echo "  FAIL  $1"; FAIL=$((FAIL+1)); }
MARK="orbit-signal-$(date -u +%s)-$RANDOM"
trap 'desk kubectl -n checkers-lab-dev delete pod obs-signal --ignore-not-found --wait=false >/dev/null 2>&1' EXIT

# the marker: a pod wearing the labels the service chart stamps, printing one unique line
python3 - "$MARK" > "$OUT/marker.json" <<'PY'
import json, sys
print(json.dumps({"apiVersion": "v1", "kind": "Pod", "metadata": {"name": "obs-signal", "namespace": "checkers-lab-dev",
  "labels": {"app.kubernetes.io/instance": "checkers-lab", "app.kubernetes.io/part-of": "checkers-lab",
             "app.kubernetes.io/name": "signal-marker", "orbit.local/env": "dev"}},
  "spec": {"automountServiceAccountToken": False, "restartPolicy": "Never",
    "securityContext": {"runAsNonRoot": True, "runAsUser": 65532, "seccompProfile": {"type": "RuntimeDefault"}},
    "containers": [{"name": "marker", "image": "python:3.13-alpine",
      "command": ["python", "-u", "-c", f"import time; print('{sys.argv[1]}'); time.sleep(300)"],
      "resources": {"requests": {"cpu": "5m", "memory": "24Mi"}, "limits": {"memory": "24Mi"}},
      "securityContext": {"allowPrivilegeEscalation": False, "capabilities": {"drop": ["ALL"]}}}]}}))
PY
desk kubectl apply -f - < "$OUT/marker.json" >/dev/null
SENT=$(date -u +%s)

"$ORBIT_DESK" bash -s > "$OUT/answers.txt" 2> "$OUT/desk.err" <<EOF
kubectl -n monitoring port-forward svc/kube-prometheus-stack-prometheus 19090:9090 >/dev/null 2>&1 &
kubectl -n loki port-forward svc/loki 13100:3100 >/dev/null 2>&1 &
kubectl -n tempo port-forward svc/tempo 13200:3200 >/dev/null 2>&1 &
sleep 4
MARK="$MARK" SENT="$SENT" python3 - <<'PY'
import json, os, time, urllib.error, urllib.parse, urllib.request
def get(url, **q):
    # a port-forward started a moment ago can still refuse: retried for a minute, then raised
    for attempt in range(12):
        try:
            return json.load(urllib.request.urlopen(url + ("?" + urllib.parse.urlencode(q) if q else ""), timeout=60))
        except urllib.error.URLError as e:
            if attempt == 11 or not isinstance(e.reason, ConnectionRefusedError):
                raise
            time.sleep(5)
P, L, T = "http://127.0.0.1:19090", "http://127.0.0.1:13100", "http://127.0.0.1:13200"
# 1. catalog names on series
r = get(P + "/api/v1/query", query='count by (project, env, component) (up{project="checkers-lab"})')["data"]["result"]
print("metric_labels", ";".join(sorted(f'{x["metric"].get("project")}/{x["metric"].get("env")}/{x["metric"].get("component")}' for x in r)) or "none")
# 2. the marker, by its catalog labels, within 120 s of being written
mark, sent, found_at = os.environ["MARK"], int(os.environ["SENT"]), None
while time.time() < sent + 120 and found_at is None:
    d = get(L + "/loki/api/v1/query_range", query='{project="checkers-lab", env="dev", component="signal-marker"} |= "%s"' % mark,
            start=str((sent - 60) * 10**9), end=str(int(time.time() + 5) * 10**9), limit="5")
    if any(s["values"] for s in d["data"]["result"]):
        found_at = int(time.time())
    else:
        time.sleep(5)
print("marker_seconds", (found_at - sent) if found_at else "none")
# 3. a trace rooted at the bot that crosses Traefik, the api, the engine and Postgres
end = int(time.time()); start = end - 3 * 3600
traces = get(T + "/api/search", tags="service.name=checkers-lab-bot", start=str(start), end=str(end), limit="20").get("traces", [])
best = None
for t in traces:
    body = get(T + "/api/traces/" + t["traceID"])
    services, db = set(), False
    for b in body.get("batches", body.get("resourceSpans", [])):
        res = {a["key"]: list(a["value"].values())[0] for a in b.get("resource", {}).get("attributes", [])}
        svc = res.get("service.name", "?"); services.add(svc)
        for ss in b.get("scopeSpans", b.get("instrumentationLibrarySpans", [])):
            for sp in ss.get("spans", []):
                attrs = {a["key"]: list(a["value"].values())[0] for a in sp.get("attributes", [])}
                if attrs.get("db.system") == "postgresql" or attrs.get("db.system.name") == "postgresql":
                    db = True
    score = len(services) + (2 if db else 0)
    if best is None or score > best[0]:
        best = (score, t["traceID"], sorted(services), db)
print("trace", f"{best[1]} services={','.join(best[2])} postgres={best[3]}" if best else "none")
# 4. trace -> logs
if best:
    d = get(L + "/loki/api/v1/query_range", query='{namespace=~"checkers-lab-.*"} |= "%s"' % best[1],
            start=str(start * 10**9), end=str(end * 10**9), limit="20")
    print("trace_logs", sum(len(s["values"]) for s in d["data"]["result"]))
# 5. metric -> trace: an exemplar on the span-metrics latency histogram names a trace Tempo has
ex = get(P + "/api/v1/query_exemplars", query="traces_spanmetrics_latency_bucket", start=str(end - 3600), end=str(end)).get("data", [])
tids = [e["labels"].get("trace_id") or e["labels"].get("traceID") for s in ex for e in s.get("exemplars", [])]
tids = [t for t in tids if t]
hit = None
for tid in tids[-10:]:
    try:
        urllib.request.urlopen(T + "/api/traces/" + tid, timeout=30); hit = tid; break
    except Exception:
        continue
print("exemplar", f"{len(tids)} exemplar(s), Tempo has {hit}" if hit else f"{len(tids)} exemplar(s), none found in Tempo")
PY
EOF
a(){ awk -v k="$1" '$1 == k {$1=""; sub(/^ /,""); print}' "$OUT/answers.txt"; }
cat "$OUT/answers.txt"
case "$(a metric_labels)" in *checkers-lab/prod/api*) ok "series carry the catalog's names: $(a metric_labels)";; *) bad "no series with project/env/component: $(a metric_labels)";; esac
m=$(a marker_seconds)
[ "$m" != none ] && [ -n "$m" ] && ok "the marker line reached Loki in ${m}s, found by {project, env, component}" || bad "the marker line was not in Loki by its catalog labels within 120 s"
case "$(a trace)" in *checkers-lab-api*checkers-lab-bot*checkers-lab-engine*postgres=True*) ok "a trace crosses bot, api, engine and Postgres: $(a trace)";; *) bad "no trace crossing bot, api, engine and Postgres: $(a trace)";; esac
case "$(a trace)" in *traefik*) ok "the same trace has Traefik's span";; *) bad "the trace has no Traefik span: $(a trace)";; esac
awk -v v="$(a trace_logs)" 'BEGIN{exit !(v+0 > 0)}' && ok "trace -> logs: $(a trace_logs) Loki line(s) carry that trace's id" || bad "trace -> logs: no Loki line carries the trace's id"
case "$(a exemplar)" in *"Tempo has "*) ok "metric -> trace: $(a exemplar)";; *) bad "metric -> trace: $(a exemplar)";; esac
echo "signals: $PASS passed, $FAIL failed"
[ "$FAIL" -eq 0 ]
