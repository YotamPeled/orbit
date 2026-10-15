#!/usr/bin/env bash
# Pass: zero failed requests across the whole run, the never-ready variant served no
# request (its pods' request counter in Prometheus), prod api is back on the tag it ran
# before, the ledger shows the variant shipped and rolled back, and a restart was made.
. "$(dirname "$0")/../lib.sh"
touch "$SCENARIO_OUT/stop"
if [ "$(cat "$SCENARIO_OUT/traffic-from" 2>/dev/null)" = node ]; then
  ssh -o BatchMode=yes "$ORBIT_NODE_SSH" 'touch /tmp/s05-stop; sleep 3; cat /tmp/s05-traffic.log' > "$SCENARIO_OUT/traffic.log.new" \
    && mv "$SCENARIO_OUT/traffic.log.new" "$SCENARIO_OUT/traffic.log"
else
  sleep 3
fi
PASS=0 FAIL=0
ok(){ echo "  ok    $1"; PASS=$((PASS+1)); }
bad(){ echo "  FAIL  $1"; FAIL=$((FAIL+1)); }
V=$(cat "$SCENARIO_OUT/variant"); before=$(cat "$SCENARIO_OUT/before")
total=$(wc -l < "$SCENARIO_OUT/traffic.log")
failed=$(awk '$2 != "200"' "$SCENARIO_OUT/traffic.log" | wc -l)
codes=$(awk '$2 != "200" {print $2}' "$SCENARIO_OUT/traffic.log" | sort | uniq -c | tr '\n' ' ')
[ "$total" -gt 100 ] && [ "$failed" -eq 0 ] && ok "0 failed of $total requests under the ship, rollback and restart" \
  || bad "$failed failed of $total requests ($codes)"
now=$(image_of prod api)
[ "$now" = "$before" ] && ok "prod api is back on ${before##*:}" || bad "prod api runs ${now##*:}, not ${before##*:}"
# The variant's ReplicaSet outlives the rollback (revisionHistoryLimit); its pods are
# <rs>-<suffix>. Their request counter for routed requests only (a controller label):
# kubelet probes and the scrape carry none, and a never-ready pod still answers its probes.
rs=$(desk kubectl -n checkers-lab-prod get rs -o jsonpath="{range .items[*]}{.metadata.name} {.spec.template.spec.containers[0].image}{\"\n\"}{end}" | awk -v v=":$V" 'index($2, v) {print $1}' | head -1)
if [ -z "$rs" ]; then
  bad "no ReplicaSet ran the variant $V (was it shipped?)"
else
  t0=$(cat "$SCENARIO_OUT/t0"); win=$(( ($(date +%s) - t0) / 60 + 5 ))
  q="sum(increase(http_requests_received_total{namespace=\"checkers-lab-prod\",pod=~\"$rs-.*\",controller!=\"\"}[${win}m]))"
  served=$("$ORBIT_DESK" bash -c "orbit call orbit_metrics_query --json '$(python3 -c 'import json,sys;print(json.dumps({"query":sys.argv[1]}))' "$q")'" 2>/dev/null \
    | python3 -c 'import json,sys;t=sys.stdin.read();d=json.loads(t[t.index("{"):]);s=d.get("items") or [];v=[float(p[-1]) for x in s for p in (x.get("values") or []) if p];print(round(max(v),1) if v else 0)')
  awk -v s="$served" 'BEGIN{exit !(s+0 == 0)}' && ok "the never-ready variant ($rs) served no request" || bad "the never-ready variant ($rs) served $served requests"
fi
ledger=$("$ORBIT_DESK" bash -c "orbit call orbit_ledger_get --json '{\"project\":\"checkers-lab\",\"env\":\"prod\",\"limit\":10}'" 2>/dev/null)
python3 -c '
import json, sys
t = sys.stdin.read(); d = json.loads(t[t.index("{"):])
apis = [(i["at"], (i.get("tags") or {}).get("checkers-lab/prod/api")) for i in d.get("items") or []]
apis.sort()
v, b = sys.argv[1], sys.argv[2]
shipped = [a for a, tag in apis if tag == v]
back = [a for a, tag in apis if tag == b and shipped and a > shipped[0]]
sys.exit(0 if shipped and back else 1)' "$V" "${before##*:}" <<<"$ledger" \
  && ok "the ledger shows the variant shipped, then the previous tag back" || bad "the ledger does not show ship then rollback"
called orbit_workload_restart && ok "the agent restarted the api through orbitd" || bad "no restart in the transcript"
echo "$PASS passed, $FAIL failed"
[ "$FAIL" -eq 0 ]
