#!/usr/bin/env bash
# docs/V1.md §6.5 — retention and storage, read-only:
#   * Prometheus keeps 7 days: a series answers 6 days back and nothing 8 days back;
#   * Loki keeps 14 days and Tempo 72 hours, both in the bucket: the live configuration
#     says so, data inside the window answers and data past it does not, and their
#     object-store writes succeed (request counters, not a guess);
# A box younger than a window cannot show that window's two ends, and this says so instead
# of guessing. Until 2026-10-11 it asked for fixed ages (6 and 8 days, 13 and 15, 2 and 4):
# on the test box rebuilt three days earlier the "kept" rows failed for data that could not
# exist, and the "expired" rows PASSED for the same reason, which proves nothing
# (docs/V1.md §8). Now, from the node's own age:
#   * kept: the store answers at the oldest moment it can be asked about, the window less
#     a day or, on a younger box, the box's age less three hours;
#   * expired: judged only on a box older than the window plus a day; otherwise the row is
#     printed as "not judged" with the day it can be, and counts as neither.
#
#   scenarios/obs/retention.sh <out-dir>
# Restarts losing no recent data is §6.4's (scenarios/obs/watchers.sh).
set -uo pipefail
here=$(cd "$(dirname "$0")" && pwd)
OUT=${1:?usage: retention.sh <out-dir>}; mkdir -p "$OUT"
export SCENARIO_OUT="$OUT"
. "$here/../lib.sh"
: "${ORBIT_DESK:=$ORBIT_INSTANCE_DIR/scripts/desk.sh}"; export ORBIT_DESK
PASS=0 FAIL=0
SKIP=0
ok(){ echo "  ok    $1"; PASS=$((PASS+1)); }
bad(){ echo "  FAIL  $1"; FAIL=$((FAIL+1)); }
skip(){ echo "  --    $1"; SKIP=$((SKIP+1)); }
now=$(date -u +%s); day=86400
born=$(desk kubectl get nodes -o jsonpath='{.items[0].metadata.creationTimestamp}' | tr -d '\r' | tail -1)
born_s=$(date -u -d "$born" +%s 2>/dev/null) || { echo "retention: the node's age could not be read ('$born')"; exit 2; }
age=$(( now - born_s ))
# kept_back <window days>: how far back the "kept" question reaches, in seconds
kept_back(){ local w=$(( ($1 - 1) * day )) a=$(( age - 3*3600 )); [ "$a" -lt "$w" ] && echo "$a" || echo "$w"; }
# can_expire <window days>: true when the box is old enough for data to have expired
can_expire(){ [ "$age" -gt $(( ($1 + 1) * day )) ]; }
judged_on(){ date -u -d "@$(( born_s + ($1 + 1) * day ))" +%Y-%m-%d; }
ago(){ awk -v s="$1" 'BEGIN{printf "%.1f days", s/86400}'; }
PB=$(kept_back 7); LB=$(kept_back 14); TB=$(kept_back 3)
echo "the node was made $born: $(ago "$age") old; kept is asked $(ago "$PB") back of Prometheus, $(ago "$LB") of Loki, $(ago "$TB") of Tempo"

# One desk session: port-forwards to Prometheus, Loki and Tempo, then every query, so
# the answers come back as "<key> <value>" lines.
"$ORBIT_DESK" bash -s > "$OUT/answers.txt" 2>"$OUT/desk.err" <<EOF
kubectl -n monitoring port-forward svc/kube-prometheus-stack-prometheus 19090:9090 >/dev/null 2>&1 &
kubectl -n loki port-forward svc/loki 13100:3100 >/dev/null 2>&1 &
kubectl -n tempo port-forward svc/tempo 13200:3200 >/dev/null 2>&1 &
# each forward answers before anything is asked through it (a fixed pause once was too short)
for u in 19090/-/ready 13100/ready 13200/ready; do for n in 1 2 3 4 5 6 7 8 9 10 11 12 13 14 15 16 17 18 19 20; do curl -s -o /dev/null "http://127.0.0.1:\$u" && break; sleep 3; done; done
q(){ curl -s -G "http://127.0.0.1:19090/api/v1/query" --data-urlencode "query=\$2" --data-urlencode "time=\$3" \
  | python3 -c 'import json,sys;d=json.load(sys.stdin);print(sys.argv[1], round(sum(float(r["value"][1]) for r in d["data"]["result"]), 3))' "\$1"; }
q prom_6d   'count(up)' $(( now - PB ))
q prom_8d   'count(up)' $(( now - 8*day ))
q loki_s3_ok   'sum(rate(loki_s3_request_duration_seconds_count{status_code=~"2.."}[1h]))' $now
q tempo_s3_ok  'sum(rate(tempodb_backend_request_duration_seconds_count{status_code=~"2.."}[1h]))' $now
echo "prom_retention \$(kubectl -n monitoring get prometheus -o jsonpath='{.items[0].spec.retention}')"
ll(){ curl -s -G "http://127.0.0.1:13100/loki/api/v1/query_range" --data-urlencode 'query=count_over_time({namespace="traefik"}[1h])' \
  --data-urlencode "start=\$2" --data-urlencode "end=\$3" --data-urlencode "step=3600" \
  | python3 -c 'import json,sys;d=json.load(sys.stdin);print(sys.argv[1], sum(len(r["values"]) for r in d.get("data",{}).get("result",[])))' "\$1"; }
ll loki_13d $(( (now - LB) * 1000000000 )) $(( (now - LB + 3*3600) * 1000000000 ))
ll loki_15d $(( (now - 15*day) * 1000000000 )) $(( (now - 15*day + 3*3600) * 1000000000 ))
echo "loki_config \$(curl -s http://127.0.0.1:13100/config | grep -E '^\s*(retention_period|retention_enabled|object_store):' | tr -s ' ' | tr '\n' ' ')"
tt(){ curl -s -G "http://127.0.0.1:13200/api/search" --data-urlencode "start=\$2" --data-urlencode "end=\$3" --data-urlencode "limit=5" \
  | python3 -c 'import json,sys;d=json.load(sys.stdin);print(sys.argv[1], len(d.get("traces") or []))' "\$1"; }
tt tempo_2d $(( now - TB )) $(( now - TB + 6*3600 ))
tt tempo_4d $(( now - 4*day )) $(( now - 4*day + 6*3600 ))
echo "tempo_config \$(curl -s http://127.0.0.1:13200/status/config | grep -E '^\s*(block_retention|backend):' | tr -s ' ' | tr '\n' ' ')"
EOF
a(){ awk -v k="$1" '$1 == k {$1=""; sub(/^ /,""); print}' "$OUT/answers.txt"; }
cat "$OUT/answers.txt"
[ "$(a prom_retention)" = 7d ] && ok "Prometheus spec.retention is 7d" || bad "Prometheus spec.retention is '$(a prom_retention)'"
awk -v v="$(a prom_6d)" 'BEGIN{exit !(v+0 > 0)}' && ok "Prometheus answers $(ago "$PB") back (count(up) = $(a prom_6d))" || bad "Prometheus has nothing $(ago "$PB") back"
if can_expire 7; then
  awk -v v="$(a prom_8d)" 'BEGIN{exit !(v+0 == 0)}' && ok "Prometheus answers nothing 8 days back (expired)" || bad "Prometheus still answers 8 days back ($(a prom_8d) series)"
else skip "Prometheus expiry not judged: the box is $(ago "$age") old and the window is 7 days (from $(judged_on 7))"; fi
case "$(a loki_config)" in *"retention_period: 2w"*|*"retention_period: 336h"*|*"retention_period: 14d"*) ok "Loki: $(a loki_config)";; *) bad "Loki config: $(a loki_config)";; esac
[ "$(a loki_13d)" -gt 0 ] 2>/dev/null && ok "Loki answers $(ago "$LB") back ($(a loki_13d) points)" || bad "Loki has nothing $(ago "$LB") back"
if can_expire 14; then
  [ "$(a loki_15d)" = 0 ] && ok "Loki answers nothing 15 days back (expired)" || bad "Loki still answers 15 days back ($(a loki_15d) points)"
else skip "Loki expiry not judged: the box is $(ago "$age") old and the window is 14 days (from $(judged_on 14))"; fi
awk -v v="$(a loki_s3_ok)" 'BEGIN{exit !(v+0 > 0)}' && ok "Loki writes to the bucket ($(a loki_s3_ok) successful object-store requests/s)" || bad "Loki shows no successful object-store requests"
case "$(a tempo_config)" in *"block_retention: 72h"*) ok "Tempo: $(a tempo_config)";; *) bad "Tempo config: $(a tempo_config)";; esac
[ "$(a tempo_2d)" -gt 0 ] 2>/dev/null && ok "Tempo finds traces $(ago "$TB") back ($(a tempo_2d))" || bad "Tempo finds no trace $(ago "$TB") back"
if can_expire 3; then
  [ "$(a tempo_4d)" = 0 ] && ok "Tempo finds nothing 4 days back (expired)" || bad "Tempo still finds traces 4 days back ($(a tempo_4d))"
else skip "Tempo expiry not judged: the box is $(ago "$age") old and the window is 3 days (from $(judged_on 3))"; fi
awk -v v="$(a tempo_s3_ok)" 'BEGIN{exit !(v+0 > 0)}' && ok "Tempo writes to the bucket ($(a tempo_s3_ok) successful backend requests/s)" || bad "Tempo shows no successful backend requests"
if [ "$SKIP" -gt 0 ]; then echo "retention: $PASS passed, $FAIL failed, $SKIP not judged (the box is younger than the window)"
else echo "retention: $PASS passed, $FAIL failed"; fi
[ "$FAIL" -eq 0 ]
