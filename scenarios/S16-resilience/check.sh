#!/usr/bin/env bash
# Pass, judged at least 35 minutes after boot: every Deployment and StatefulSet is ready,
# both checkers-lab doors answer, no alert fires but the always-on ones, the memory budget
# alert never fired since boot, a node bootstrap re-run changes nothing (k3s not
# restarted, nothing installed), and the agent answered from orbitd evidence.
. "$(dirname "$0")/../lib.sh"
PASS=0 FAIL=0
ok(){ echo "  ok    $1"; PASS=$((PASS+1)); }
bad(){ echo "  FAIL  $1"; FAIL=$((FAIL+1)); }
NODE=${ORBIT_NODE_SSH:-orbit}
booted=$(cat "$SCENARIO_OUT/booted-at")
wait_s=$(( booted + 2100 - $(date +%s) ))
[ "$wait_s" -gt 0 ] && { echo "  (waiting ${wait_s}s until 35 minutes after boot)"; sleep "$wait_s"; }
since=$(( ($(date +%s) - booted) / 60 ))
nr=$(desk kubectl get deploy,statefulset -A -o jsonpath='{range .items[*]}{.kind}/{.metadata.namespace}/{.metadata.name} {.status.readyReplicas} {.spec.replicas}{"\n"}{end}' \
  | awk '$3 > 0 && $2 != $3 {print $1}' | tr '\n' ' ')
[ -z "$nr" ] && ok "every Deployment and StatefulSet is ready ${since} min after boot" || bad "not ready: $nr"
# a database is neither a Deployment nor a StatefulSet (its operator owns bare pods): a
# cluster that came back not ready passed the row above unseen (docs/V1.md §8)
dbnr=$(desk kubectl get clusters.postgresql.cnpg.io -A -o jsonpath='{range .items[*]}{.metadata.namespace}/{.metadata.name} {.status.readyInstances} {.spec.instances}{"\n"}{end}' \
  | awk '$2 != $3 {print $1}' | tr '\n' ' ')
[ -z "$dbnr" ] && ok "every database cluster has all its instances ready" || bad "database not ready: $dbnr"
cfg=$(mktemp); printf 'user = "orbit:%s"\n' "$(cat "$ORBIT_SCENARIO_MCP_DIR/dev-basic-auth-password")" > "$cfg"
p=$(curl -s -o /dev/null -w '%{http_code}' https://checkers-lab.$ORBIT_SCENARIO_DOMAIN/api/healthz)
d=$(curl -s -o /dev/null -w '%{http_code}' -K "$cfg" https://checkers-lab.$ORBIT_SCENARIO_DEV_DOMAIN/api/healthz); rm -f "$cfg"
[ "$p $d" = "200 200" ] && ok "both checkers-lab doors answer 200" || bad "doors answered prod $p, dev $d"
am=$(desk kubectl -n monitoring get pod -l app.kubernetes.io/name=alertmanager -o jsonpath='{.items[0].metadata.name}')
firing=$(desk kubectl -n monitoring exec "$am" -c alertmanager -- wget -qO- "http://localhost:9093/api/v2/alerts?active=true&silenced=false&inhibited=false" \
  | python3 -c 'import json,sys;a=json.loads(sys.stdin.read() or "[]");print(" ".join(sorted({x["labels"]["alertname"] for x in a} - {"Watchdog","InfoInhibitor"})))')
[ -z "$firing" ] && ok "no alert fires but Watchdog/InfoInhibitor" || bad "firing: $firing"
mq(){ "$ORBIT_DESK" bash -c "orbit call orbit_metrics_query --json '$(python3 -c 'import json,sys;print(json.dumps({"query":sys.argv[1]}))' "$1")'" 2>/dev/null \
  | python3 -c 'import json,sys;t=sys.stdin.read();d=json.loads(t[t.index("{"):]);print(len(d.get("items") or []))'; }
mem=$(mq "max_over_time(ALERTS{alertname=\"OrbitPlatformMemoryOverBudget\"}[${since}m])")
[ "$mem" = 0 ] && ok "OrbitPlatformMemoryOverBudget never fired (pending or firing) in the ${since} min since boot" \
  || bad "OrbitPlatformMemoryOverBudget had $mem series in the ${since} min since boot"
k3s_before=$(ssh "$NODE" 'systemctl show k3s -p ActiveEnterTimestampMonotonic --value')
desk scripts/bootstrap-node.sh > "$SCENARIO_OUT/bootstrap-rerun.log" 2>&1; rc=$?
k3s_after=$(ssh "$NODE" 'systemctl show k3s -p ActiveEnterTimestampMonotonic --value')
grep -q 'k3s already running with current args' "$SCENARIO_OUT/bootstrap-rerun.log" && [ "$k3s_before" = "$k3s_after" ] && [ "$rc" -eq 0 ] \
  && ok "a node bootstrap re-run changes nothing (exit 0, k3s already running with current args, not restarted)" \
  || bad "the bootstrap re-run: exit $rc, k3s start $k3s_before -> $k3s_after ($(grep -E '==>|installed|restart' "$SCENARIO_OUT/bootstrap-rerun.log" | tail -3 | tr '\n' ' '))"
called 'orbit_[a-z_]+' && ok "the agent read the platform through orbitd" || bad "no orbitd call in the transcript"
echo "$PASS passed, $FAIL failed"
[ "$FAIL" -eq 0 ]
