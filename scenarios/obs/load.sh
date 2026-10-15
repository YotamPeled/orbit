#!/usr/bin/env bash
# docs/V1.md §6.6 — load:
#   * a log burst (300 000 lines from one pod in a minute) is ingested, or refused WITH an
#     alert (OrbitLogsDiscarded) — never silently dropped;
#   * a label explosion (a target exposing 8 000 series, over the service chart's 5 000
#     sample limit) is caught: the target is not scraped and OrbitScrapeSampleLimitHit fires;
#   * the stack's own memory during the burst stays within its 2 GiB budget (measured).
#
#   scenarios/obs/load.sh <out-dir>
set -uo pipefail
here=$(cd "$(dirname "$0")" && pwd)
OUT=${1:?usage: load.sh <out-dir>}; mkdir -p "$OUT"; export SCENARIO_OUT="$OUT"
. "$here/../lib.sh"
: "${ORBIT_DESK:=$ORBIT_INSTANCE_DIR/scripts/desk.sh}"; export ORBIT_DESK
PASS=0 FAIL=0
ok(){ echo "  ok    $1"; PASS=$((PASS+1)); }
bad(){ echo "  FAIL  $1"; FAIL=$((FAIL+1)); }
NS=checkers-lab-dev
trap 'desk kubectl -n '"$NS"' delete pod obs-burst --ignore-not-found --wait=false >/dev/null 2>&1; desk kubectl -n '"$NS"' delete deploy,svc,servicemonitor obs-explode --ignore-not-found --wait=false >/dev/null 2>&1' EXIT
mq(){ "$ORBIT_DESK" bash -c "orbit call orbit_metrics_query --json '$(python3 -c 'import json,sys;print(json.dumps({"query":sys.argv[1]}))' "$1")'" 2>/dev/null \
  | python3 -c 'import json,sys;t=sys.stdin.read();d=json.loads(t[t.index("{"):]);print(round(sum(float(p[-1]) for x in d.get("items") or [] for p in x.get("values") or []),3))'; }
am_active(){
  local am; am=$(desk kubectl -n monitoring get pod -l app.kubernetes.io/name=alertmanager -o jsonpath='{.items[0].metadata.name}')
  desk kubectl -n monitoring exec "$am" -c alertmanager -- wget -qO- "http://localhost:9093/api/v2/alerts?filter=alertname%3D$1&active=true&silenced=false" \
    | python3 -c 'import json,sys;print("yes" if json.loads(sys.stdin.read() or "[]") else "no")'
}
restricted(){ # <name> <command-json> -> a restricted, quota-fitting Pod spec fragment
  python3 - "$1" "$2" <<'PY'
import json, sys
name, cmd = sys.argv[1], json.loads(sys.argv[2])
print(json.dumps({"automountServiceAccountToken": False, "restartPolicy": "Never",
  "securityContext": {"runAsNonRoot": True, "runAsUser": 65532, "seccompProfile": {"type": "RuntimeDefault"}},
  "containers": [{"name": name, "image": "python:3.13-alpine", "command": cmd,
    "ports": [{"name": "metrics", "containerPort": 8080}],
    "resources": {"requests": {"cpu": "50m", "memory": "48Mi"}, "limits": {"memory": "48Mi"}},
    "securityContext": {"allowPrivilegeEscalation": False, "capabilities": {"drop": ["ALL"]}}}]}))
PY
}

# ---- the log burst -------------------------------------------------------------------------
disc0=$(mq 'sum(loki_discarded_samples_total) or vector(0)')
t0=$(date -u +%s)
spec=$(restricted burst '["python","-u","-c","import sys\nfor i in range(300000):\n    sys.stdout.write(f\"obs-burst seq={i} padding-padding-padding-padding\\n\")\nimport time; time.sleep(600)"]')
python3 -c 'import json,sys;print(json.dumps({"apiVersion":"v1","kind":"Pod","metadata":{"name":"obs-burst","namespace":sys.argv[1]},"spec":json.loads(sys.argv[2])}))' "$NS" "$spec" > "$OUT/burst.json"
desk kubectl apply -f - < "$OUT/burst.json" >/dev/null
sleep 240
got=$(lokiq "sum(count_over_time({namespace=\"$NS\", pod=\"obs-burst\"} |= \"obs-burst seq=\" [10m]))" "$(date -u +%s)")
case $got in unreadable|error) bad "the burst's line count could not be read from Loki ($got)"; got=0;; *) got=${got%.*};; esac
disc=$(python3 -c "print(round($(mq 'sum(loki_discarded_samples_total) or vector(0)') - $disc0))")
echo "  (burst: 300000 lines written, $got in Loki, $disc discarded by Loki)"
if [ "${got:-0}" -ge 297000 ]; then
  ok "the log burst was ingested ($got of 300000 lines in Loki)"
elif [ "${disc:-0}" -gt 0 ]; then
  fired=no; for i in $(seq 1 45); do [ "$(am_active OrbitLogsDiscarded)" = yes ] && { fired=yes; break; }; sleep 20; done
  [ "$fired" = yes ] && ok "the burst was refused WITH an alert: $disc lines discarded, OrbitLogsDiscarded fired ($got lines kept)" \
    || bad "Loki discarded $disc lines and OrbitLogsDiscarded did not fire in 15 min"
else
  bad "lines went missing with no discard recorded ($got of 300000 in Loki, 0 discarded): a silent drop"
fi
rss=$(mq "max_over_time(sum(container_memory_rss{container!=\"\",namespace=~\"monitoring|loki|tempo|alloy|traefik|cert-manager|cnpg-system|orbit|kube-system\"})[$(( $(date -u +%s) - t0 + 60 ))s:30s])")
python3 -c "import sys; sys.exit(0 if $rss < 2147483648 else 1)" \
  && ok "the stack's peak memory during the burst: $(python3 -c "print(round($rss/1048576))") MiB RSS, within the 2 GiB budget" \
  || bad "the stack's peak memory during the burst: $(python3 -c "print(round($rss/1048576))") MiB RSS, over the 2 GiB budget"
# the burst pod is gone before the next part starts: it holds the namespace's pod quota, and
# the exploding target once never started behind it (the alert then had nothing to fire on)
desk kubectl -n "$NS" delete pod obs-burst --wait=true --timeout=120s >/dev/null

# ---- the label explosion ---------------------------------------------------------------------
cat > "$OUT/explode.py" <<'PY'
from http.server import BaseHTTPRequestHandler, HTTPServer
BODY = "".join(f'obs_explode{{series="{i}"}} 1\n' for i in range(8000)).encode()
class H(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200); self.send_header("Content-Type", "text/plain"); self.end_headers(); self.wfile.write(BODY)
    def log_message(self, *a): pass
HTTPServer(("", 8080), H).serve_forever()
PY
spec=$(restricted explode "[\"python\",\"-c\",$(python3 -c 'import json,sys;print(json.dumps(open(sys.argv[1]).read()))' "$OUT/explode.py")]")
python3 - "$NS" "$spec" > "$OUT/explode.json" <<'PY'
import json, sys
ns, spec = sys.argv[1], json.loads(sys.argv[2])
spec["restartPolicy"] = "Always"
labels = {"app.kubernetes.io/name": "obs-explode"}
docs = [
  {"apiVersion": "apps/v1", "kind": "Deployment", "metadata": {"name": "obs-explode", "namespace": ns},
   "spec": {"replicas": 1, "selector": {"matchLabels": labels}, "template": {"metadata": {"labels": labels}, "spec": spec}}},
  {"apiVersion": "v1", "kind": "Service", "metadata": {"name": "obs-explode", "namespace": ns, "labels": labels},
   "spec": {"selector": labels, "ports": [{"name": "metrics", "port": 8080, "targetPort": 8080}]}},
  {"apiVersion": "monitoring.coreos.com/v1", "kind": "ServiceMonitor", "metadata": {"name": "obs-explode", "namespace": ns, "labels": labels},
   "spec": {"selector": {"matchLabels": labels}, "endpoints": [{"port": "metrics", "interval": "30s"}], "sampleLimit": 5000}}]
print(json.dumps({"apiVersion": "v1", "kind": "List", "items": docs}))
PY
desk kubectl apply -f - < "$OUT/explode.json" >/dev/null
if ! desk kubectl -n "$NS" rollout status deploy/obs-explode --timeout=180s >/dev/null; then
  bad "the exploding target never ran, so nothing below was tested: $(desk kubectl -n "$NS" get events --field-selector reason=FailedCreate -o jsonpath='{.items[-1:].message}' | cut -c1-200)"
  echo "load: $PASS passed, $FAIL failed"; exit 1
fi
fired=no; for i in $(seq 1 60); do [ "$(am_active OrbitScrapeSampleLimitHit)" = yes ] && { fired=yes; break; }; sleep 20; done
[ "$fired" = yes ] && ok "a target with 8000 series (limit 5000) is refused at the scrape and OrbitScrapeSampleLimitHit fired" \
  || bad "OrbitScrapeSampleLimitHit did not fire in 20 min"
series=$(mq 'count(obs_explode) or vector(0)')
[ "$series" = 0 ] || [ "$series" = 0.0 ] && ok "none of the 8000 exploding series entered Prometheus" || bad "$series exploding series entered Prometheus"
desk kubectl -n "$NS" delete deploy,svc,servicemonitor obs-explode --wait=false >/dev/null
echo "load: $PASS passed, $FAIL failed"
[ "$FAIL" -eq 0 ]
