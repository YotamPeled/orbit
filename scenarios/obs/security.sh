#!/usr/bin/env bash
# docs/V1.md §6.7 — security of the observability plane:
#   * anonymous Grafana is refused;
#   * a tenant cannot reach the backends' write doors (Loki push, Prometheus, Alertmanager);
#   * no secret's value appears in the logs (every container log on the node) or in the
#     text of any alert Alertmanager holds.
# The secret scan reads every live Secret through the desk and searches a distinctive
# piece (up to 24 characters from the middle) of each credential value of 12+ characters
# (identifiers kept in Secrets — a database's name, user and host, an image — are not)
# in the node's container logs, the files Alloy ships to Loki. Only names
# and counts leave the desk: never a value, never the piece searched for.
#
#   scenarios/obs/security.sh <out-dir>
set -uo pipefail
here=$(cd "$(dirname "$0")" && pwd)
OUT=${1:?usage: security.sh <out-dir>}; mkdir -p "$OUT"
export SCENARIO_OUT="$OUT"
. "$here/../lib.sh"
: "${ORBIT_DESK:=$ORBIT_INSTANCE_DIR/scripts/desk.sh}"; export ORBIT_DESK
. "$here/../S12-network-isolation/probe.sh"
PASS=0 FAIL=0
ok(){ echo "  ok    $1"; PASS=$((PASS+1)); }
bad(){ echo "  FAIL  $1"; FAIL=$((FAIL+1)); }
DOMAIN=$(python3 -c 'import yaml,sys;print(yaml.safe_load(open(sys.argv[1]))["domains"]["primary"])' "$ORBIT_INSTANCE_DIR/instance/config.yaml")

c=$(curl -s -o /dev/null -w '%{http_code}' "https://grafana.$DOMAIN/api/dashboards/home")
[ "$c" = 401 ] && ok "anonymous Grafana is refused (401)" || bad "anonymous Grafana answered $c"

blocked(){ case "$1" in *:28|*:7) return 0;; *) return 1;; esac; }
for t in "loki-push http://loki.loki.svc:3100/loki/api/v1/push" \
         "prometheus http://kube-prometheus-stack-prometheus.monitoring.svc:9090/api/v1/write" \
         "alertmanager http://kube-prometheus-stack-alertmanager.monitoring.svc:9093/api/v2/silences"; do
  r=$(probe checkers-lab-dev '{}' "${t#* }")
  blocked "$r" && ok "a tenant pod cannot reach ${t%% *} ($r)" || bad "a tenant pod reached ${t%% *} ($r)"
done

# The scan never sends a secret anywhere it could be recorded. Each piece reaches grep on
# the node over ssh STDIN (`grep -F -f -`): it is on no command line, in no query and in no
# log. An earlier version filtered Loki with the pieces, and Loki logs every query's text
# into its own namespace: the scan itself wrote the pieces into the log store (docs/V1.md
# §8). The node's /var/log/pods files are what Alloy ships to Loki.
"$ORBIT_DESK" bash -s > "$OUT/scan.txt" 2> "$OUT/scan.err" <<'EOF'
. /instance/scripts/lib-instance.sh >/dev/null 2>&1
am=$(kubectl -n monitoring get pod -l app.kubernetes.io/name=alertmanager -o jsonpath='{.items[0].metadata.name}')
kubectl -n monitoring exec "$am" -c alertmanager -- wget -qO- "http://localhost:9093/api/v2/alerts?active=true&silenced=true&inhibited=true" > /tmp/alerts.json
kubectl get secrets -A -o json > /tmp/secrets.json
NODE_IP="$ORBIT_NODE_IP" SSH_KEY="$ORBIT_SSH_KEY" python3 - <<'PY'
import base64, json, os, subprocess
secrets = json.load(open("/tmp/secrets.json"))["items"]
alerts = open("/tmp/alerts.json", encoding="utf-8", errors="replace").read()
SKIP_TYPES = {"helm.sh/release.v1"}           # gzipped chart state, not a credential
SKIP_KEYS = {"ca.crt", "tls.crt",             # certificates are public by design
             # identifiers stored beside a credential (CloudNativePG's -app Secret):
             # logs naming them are expected; the URIs beside them embed the password
             # and ARE scanned
             "dbname", "user", "username", "host", "port"}
pieces = []
for s in secrets:
    if s.get("type") in SKIP_TYPES:
        continue
    for k, v in (s.get("data") or {}).items():
        if k in SKIP_KEYS or k.upper().endswith("_IMAGE"):
            continue
        try:
            val = base64.b64decode(v).decode("utf-8").strip()
        except Exception:
            continue
        if len(val) < 12 or not val.isprintable():
            continue
        mid = max(0, len(val) // 2 - 12)
        pieces.append((f'{s["metadata"]["namespace"]}/{s["metadata"]["name"]}:{k}', val[mid:mid + 24]))
ssh = ["ssh", "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=accept-new", "-i", os.environ["SSH_KEY"],
       "root@" + os.environ["NODE_IP"], "grep -rlF -f - /var/log/pods 2>/dev/null | head -5; exit 0"]
in_logs, in_alerts, errors = [], [], 0
for name, piece in pieces:
    if piece in alerts:
        in_alerts.append(name)
    r = subprocess.run(ssh, input=(piece + "\n").encode(), capture_output=True, timeout=180)
    if r.returncode != 0:
        errors += 1
        continue
    files = [f for f in r.stdout.decode().split() if f]
    if files:
        # a pod log path names namespace_pod_uid/container: identity only, never content
        in_logs.append(f"{name} in {', '.join(sorted({f.split('/')[4] for f in files if f.count('/') > 4}))}")
print("checked", len(pieces))
print("errors", errors)
print("in_logs", "; ".join(in_logs) or "none")
print("in_alerts", "; ".join(in_alerts) or "none")
PY
EOF
a(){ awk -v k="$1" '$1 == k {$1=""; sub(/^ /,""); print}' "$OUT/scan.txt"; }
n=$(a checked); e=$(a errors)
[ "${n:-0}" -gt 0 ] && [ "${e:-1}" = 0 ] && ok "scanned $n secret values against every container log on the node and every alert's text (0 errors)" \
  || bad "the secret scan did not run cleanly ($n values, $e errors; see $OUT/scan.err)"
[ "$(a in_logs)" = none ] && ok "no secret value appears in any container log" || bad "secret values found in logs: $(a in_logs)"
[ "$(a in_alerts)" = none ] && ok "no secret value appears in any alert's text" || bad "secret values found in alerts: $(a in_alerts)"
echo "security: $PASS passed, $FAIL failed"
[ "$FAIL" -eq 0 ]
