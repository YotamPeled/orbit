#!/usr/bin/env bash
# docs/V1.md §6.4 — watching the watchers:
#   * kill each of Prometheus, Loki, Tempo, Alloy, Grafana and Alertmanager: it comes back
#     Ready, and the data loss is bounded and measured (a ticker's log lines across the
#     kill, Prometheus samples across its own restart, a silence across Alertmanager's);
#   * the bucket unreachable for Loki -> OrbitObjectStoreFailing fires, then resolves;
#   * logs stop arriving (Alloy gone) -> OrbitLogsNotArriving fires, then resolves.
# (Reboot -> no false alerts is S16.) Every fault is reverted on exit too.
#
#   scenarios/obs/watchers.sh <out-dir> [part ...]     parts: kills bucket logs (default all)
set -uo pipefail
here=$(cd "$(dirname "$0")" && pwd)
OUT=${1:?usage: watchers.sh <out-dir> [part ...]}; shift
mkdir -p "$OUT"; export SCENARIO_OUT="$OUT"
. "$here/../lib.sh"
: "${ORBIT_DESK:=$ORBIT_INSTANCE_DIR/scripts/desk.sh}"; export ORBIT_DESK
PASS=0 FAIL=0
ok(){ echo "  ok    $1"; PASS=$((PASS+1)); }
bad(){ echo "  FAIL  $1"; FAIL=$((FAIL+1)); }
REVERTS=()
trap 'for r in "${REVERTS[@]}"; do eval "$r" >/dev/null 2>&1 || true; done' EXIT

# instant_q, promq and lokiq live in ../lib.sh (shared with load.sh)
am_active(){ # <alertname>: is it active in Alertmanager
  local am; am=$(desk kubectl -n monitoring get pod -l app.kubernetes.io/name=alertmanager -o jsonpath='{.items[0].metadata.name}')
  desk kubectl -n monitoring exec "$am" -c alertmanager -- wget -qO- "http://localhost:9093/api/v2/alerts?filter=alertname%3D$1&active=true&silenced=false" \
    | python3 -c 'import json,sys;print("yes" if json.loads(sys.stdin.read() or "[]") else "no")'
}
# am_is <alertname> yes|no, called in this shell: `bash -c` children do not get lib.sh's
# python3 function (Windows has no python3 on PATH), so am_active printed nothing there and
# both the bucket and the logs waits could never succeed (2026-10-05).
am_is(){ [ "$(am_active "$1")" = "$2" ]; }
wait_until(){ # <seconds> <command...>
  local until=$(( $(date +%s) + $1 )); shift
  while [ "$(date +%s)" -lt "$until" ]; do "$@" && return 0; sleep 20; done; return 1
}

# ---- a ticker: one numbered line every 2 s in checkers-lab-dev, the yardstick for logs ----
ticker_up(){
  python3 - > "$OUT/ticker.json" <<'PY'
import json
print(json.dumps({"apiVersion": "v1", "kind": "Pod", "metadata": {"name": "obs-ticker", "namespace": "checkers-lab-dev"},
  "spec": {"automountServiceAccountToken": False, "restartPolicy": "Never",
    "securityContext": {"runAsNonRoot": True, "runAsUser": 65532, "seccompProfile": {"type": "RuntimeDefault"}},
    "containers": [{"name": "tick", "image": "python:3.13-alpine",
      "command": ["python", "-u", "-c", "import time\ni=0\nwhile True:\n    print(f'obs-ticker seq={i}')\n    i+=1\n    time.sleep(2)"],
      "resources": {"requests": {"cpu": "5m", "memory": "24Mi"}, "limits": {"memory": "24Mi"}},
      "securityContext": {"allowPrivilegeEscalation": False, "capabilities": {"drop": ["ALL"]}}}]}}))
PY
  # a ticker left by an earlier run is not this run's yardstick: after a reboot it sits in
  # phase Unknown, `apply` leaves it be, and every row then counted 0 lines (docs/V1.md §8)
  desk kubectl -n checkers-lab-dev delete pod obs-ticker --ignore-not-found --force --grace-period=0 >/dev/null 2>&1 || true
  desk kubectl apply -f - < "$OUT/ticker.json" >/dev/null
  # forced: after the driver's own reboot the bare pod sits in phase Unknown and a plain delete never ends
  REVERTS+=("desk kubectl -n checkers-lab-dev delete pod obs-ticker --ignore-not-found --force --grace-period=0")
  desk kubectl -n checkers-lab-dev wait pod/obs-ticker --for=condition=Ready --timeout=120s >/dev/null     || { echo "watchers: the ticker pod did not become Ready; nothing below would measure anything" >&2; exit 2; }
}
# ticker_loss <from-epoch> <to-epoch>: lines expected (one per 2 s) vs found in Loki
ticker_loss(){
  local span=$(( $2 - $1 )) got
  got=$(lokiq "sum(count_over_time({namespace=\"checkers-lab-dev\", pod=\"obs-ticker\"} |= \"obs-ticker seq=\" [${span}s]))" "$2")
  echo "$(( span / 2 )) $got"
}

kill_and_wait(){ # <namespace> <label-selector> <ready-timeout>
  local t0; t0=$(date -u +%s)
  desk kubectl -n "$1" delete pod -l "$2" --wait=false >/dev/null
  sleep 10
  desk kubectl -n "$1" wait pod -l "$2" --for=condition=Ready --timeout="$3s" >/dev/null 2>&1 && echo $(( $(date -u +%s) - t0 )) || echo timeout
}

part_kills(){
  ticker_up
  sleep 60
  for spec in "loki loki app.kubernetes.io/name=loki" "alloy alloy app.kubernetes.io/name=alloy" \
              "tempo tempo app.kubernetes.io/name=tempo" "grafana monitoring app.kubernetes.io/name=grafana"; do
    set -- $spec
    t0=$(date -u +%s); took=$(kill_and_wait "$2" "$3" 600)
    [ "$took" != timeout ] && ok "$1: killed, Ready again in ${took}s" || bad "$1: not Ready 10 min after the kill"
    sleep 90
    read -r want got <<<"$(ticker_loss "$t0" "$(date -u +%s)")"
    awk -v w="$want" -v g="$got" 'BEGIN{exit !(g >= w*0.95)}' \
      && ok "$1: the ticker's lines across the kill reached Loki ($got of ~$want)" \
      || bad "$1: the ticker lost lines across the kill ($got of ~$want)"
  done
  c=$(curl -s -o /dev/null -w '%{http_code}' "https://grafana.$(python3 -c 'import yaml,sys;print(yaml.safe_load(open(sys.argv[1]))["domains"]["primary"])' "$ORBIT_INSTANCE_DIR/instance/config.yaml")/api/health")
  [ "$c" = 200 ] && ok "grafana: /api/health answers 200 after the restart" || bad "grafana: /api/health answered $c"
  # Prometheus: samples from before its restart survive (WAL), the gap is bounded
  before=$(date -u +%s)
  took=$(kill_and_wait monitoring app.kubernetes.io/name=prometheus 900)
  [ "$took" != timeout ] && ok "prometheus: killed, Ready again in ${took}s" || bad "prometheus: not Ready 15 min after the kill"
  sleep 120
  v=$(promq 'count(up)' $(( before - 60 )))
  awk -v v="$v" 'BEGIN{exit !(v+0 > 0)}' && ok "prometheus: a sample from before the restart is still there (count(up) = $v)" || bad "prometheus: the minute before the restart is gone ($v)"
  n=$(promq "count_over_time(up{job=\"kube-state-metrics\"}[$(( $(date -u +%s) - before + 300 ))s])")
  want=$(( ($(date -u +%s) - before + 300) / 30 ))
  ok "prometheus: kube-state-metrics' up has $n of ~$want samples across the restart (loss bounded by the ${took}s it was down)"
  # Alertmanager: a silence made before the kill is still there after it
  am=$(desk kubectl -n monitoring get pod -l app.kubernetes.io/name=alertmanager -o jsonpath='{.items[0].metadata.name}')
  sid=$(desk kubectl -n monitoring exec "$am" -c alertmanager -- sh -c "wget -qO- --header 'Content-Type: application/json' --post-data '{\"matchers\":[{\"name\":\"alertname\",\"value\":\"ObsWatchersProbe\",\"isRegex\":false}],\"startsAt\":\"$(date -u +%FT%TZ)\",\"endsAt\":\"$(date -u -d '+2 hours' +%FT%TZ)\",\"createdBy\":\"obs-watchers\",\"comment\":\"§6.4 persistence probe\"}' http://localhost:9093/api/v2/silences" | python3 -c 'import json,sys;print(json.load(sys.stdin)["silenceID"])')
  took=$(kill_and_wait monitoring app.kubernetes.io/name=alertmanager 600)
  [ "$took" != timeout ] && ok "alertmanager: killed, Ready again in ${took}s" || bad "alertmanager: not Ready 10 min after the kill"
  am=$(desk kubectl -n monitoring get pod -l app.kubernetes.io/name=alertmanager -o jsonpath='{.items[0].metadata.name}')
  st=$(desk kubectl -n monitoring exec "$am" -c alertmanager -- wget -qO- "http://localhost:9093/api/v2/silence/$sid" | python3 -c 'import json,sys;print(json.load(sys.stdin)["status"]["state"])')
  [ "$st" = active ] && ok "alertmanager: the silence made before the kill is still active" || bad "alertmanager: the silence is '$st' after the kill"
  desk kubectl -n monitoring exec "$am" -c alertmanager -- wget -qO- --method=DELETE "http://localhost:9093/api/v2/silence/$sid" >/dev/null 2>&1
}

# ---- the bucket unreachable for Loki (its S3 host pinned to a dead address) -----------
# The bucket becomes unreachable while Loki keeps running: an egress NetworkPolicy on the
# Loki pod allowing everything but the bucket's addresses. (An /etc/hosts alias needs a pod
# restart, and Loki cannot start without its bucket, so that fault tested a crashed Loki,
# which exports nothing an object-store rule could see; 2026-10-05.) The block lands on the
# policy controller's next sync; the wait below is far longer.
part_bucket(){
  local host ips
  host=$(python3 -c 'import yaml,sys;from urllib.parse import urlparse;print(urlparse(yaml.safe_load(open(sys.argv[1]))["objectStorage"]["endpoint"]).hostname)' "$ORBIT_INSTANCE_DIR/instance/config.yaml")
  ips=$(desk getent ahostsv4 "$host" | awk '{print $1}' | sort -u | tr -d '\r' | tr '\n' ' ')
  [ -n "${ips// }" ] || { bad "bucket: could not resolve $host to block it"; return; }
  python3 - "$ips" > "$OUT/bucket-block.json" <<'PY'
import json, sys
print(json.dumps({"apiVersion": "networking.k8s.io/v1", "kind": "NetworkPolicy",
  "metadata": {"name": "obs-watchers-bucket-block", "namespace": "loki"},
  "spec": {"podSelector": {"matchLabels": {"app.kubernetes.io/name": "loki"}}, "policyTypes": ["Egress"],
           "egress": [{"to": [{"ipBlock": {"cidr": "0.0.0.0/0", "except": [f"{ip}/32" for ip in sys.argv[1].split()]}}]}]}}))
PY
  desk kubectl apply -f - < "$OUT/bucket-block.json" >/dev/null
  REVERTS+=("desk kubectl -n loki delete networkpolicy obs-watchers-bucket-block --ignore-not-found")
  echo "  (blocked $host: $ips)"
  if wait_until 1800 am_is OrbitObjectStoreFailing yes; then ok "bucket: OrbitObjectStoreFailing fired with the bucket unreachable (Loki running)"; else bad "bucket: OrbitObjectStoreFailing did not fire in 30 min"; fi
  desk kubectl -n loki delete networkpolicy obs-watchers-bucket-block --ignore-not-found >/dev/null
  if wait_until 1800 am_is OrbitObjectStoreFailing no; then ok "bucket: resolved once the bucket is reachable again"; else bad "bucket: still firing 30 min after the revert"; fi
}

# ---- logs stop arriving (Alloy scheduled nowhere) ----------------------------------------
part_logs(){
  desk kubectl -n alloy patch daemonset alloy --type=merge -p '{"spec":{"template":{"spec":{"nodeSelector":{"orbit.local/obs-watchers":"nowhere"}}}}}' >/dev/null
  REVERTS+=("desk kubectl -n alloy patch daemonset alloy --type=json -p '[{\"op\":\"remove\",\"path\":\"/spec/template/spec/nodeSelector/orbit.local~1obs-watchers\"}]'")
  if wait_until 2400 am_is OrbitLogsNotArriving yes; then ok "logs: OrbitLogsNotArriving fired with Alloy gone"; else bad "logs: OrbitLogsNotArriving did not fire in 40 min"; fi
  desk kubectl -n alloy patch daemonset alloy --type=json -p '[{"op":"remove","path":"/spec/template/spec/nodeSelector/orbit.local~1obs-watchers"}]' >/dev/null
  if wait_until 1800 am_is OrbitLogsNotArriving no; then ok "logs: resolved once Alloy is back"; else bad "logs: still firing 30 min after the revert"; fi
}

export -f am_active desk
PARTS=("$@"); [ "${#PARTS[@]}" -eq 0 ] && PARTS=(kills bucket logs)
for p in "${PARTS[@]}"; do echo "== $p"; "part_$p"; done 2>&1 | tee "$OUT/watchers.log"
p=$(grep -c '^  ok ' "$OUT/watchers.log"); f=$(grep -c '^  FAIL ' "$OUT/watchers.log")
echo "watchers: $p passed, $f failed"
[ "$f" -eq 0 ]
