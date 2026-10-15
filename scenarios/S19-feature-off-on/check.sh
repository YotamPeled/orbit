#!/usr/bin/env bash
# Pass: the agent switched traces off and on with orbit_features_set; each switch is one
# commit by orbitd carrying its ledger trailers; the feature's Applications were retired
# and then applied again and are ready now; a trace that was in the bucket before is still
# readable; nothing else on the platform differs from git; and the agent had no shell.
. "$(dirname "$0")/../lib.sh"
PASS=0 FAIL=0
ok(){ echo "  ok    $1"; PASS=$((PASS+1)); }
bad(){ echo "  FAIL  $1"; FAIL=$((FAIL+1)); }
T0=$(cat "$SCENARIO_OUT/t0-iso")
n=$(grep -cE '^## call [0-9]+: mcp__orbit__orbit_features_set[[:space:]]*$' "$SCENARIO_TRANSCRIPT")
[ "${n:-0}" -ge 2 ] && ok "the agent called orbit_features_set $n times" || bad "the agent called orbit_features_set ${n:-0} time(s), not twice"
called 'orbit_features_list' && ok "the agent read the state with orbit_features_list" || bad "the agent never called orbit_features_list"
log=$( cd "$ORBIT_INSTANCE_DIR" && git fetch -q origin && git log origin/main --since="$T0" --format='%h %(trailers:key=Orbit-Action,valueonly,separator=%x20) %(trailers:key=Orbit-Feature,valueonly,separator=%x20) %(trailers:key=Orbit-Role,valueonly,separator=%x20)' )
off=$(printf '%s\n' "$log" | grep -c 'features-set traces=off admin')
on=$(printf '%s\n' "$log" | grep -c 'features-set traces=on admin')
[ "$off" = 1 ] && [ "$on" = 1 ] && ok "the ledger holds both changes: one commit traces=off and one traces=on, by an admin through orbitd" \
  || bad "the ledger since $T0 holds $off traces=off and $on traces=on commit(s): $(printf '%s' "$log" | tr '\n' ';')"
other=$(printf '%s\n' "$log" | grep -c . ); [ "$other" = 2 ] && ok "those two are the only commits since the scenario began" || bad "$other commit(s) since the scenario began, not 2"
# the apps went and came back: the tool's own converge reports are in the transcript
for app in tempo opentelemetry-operator; do
  # a converge step in a tool result, however the JSON is spaced: {"action":"retire","app":"tempo",...,"ok":true}
  grep -qE "\"action\": ?\"retire\", ?\"app\": ?\"$app\"[^{}]*\"ok\": ?true" "$SCENARIO_TRANSCRIPT" && r=1 || r=0
  grep -qE "\"action\": ?\"apply\", ?\"app\": ?\"$app\"[^{}]*\"ok\": ?true" "$SCENARIO_TRANSCRIPT" && a=1 || a=0
  [ "$r$a" = 11 ] && ok "$app was retired and applied again (the converge reports)" || bad "$app: retired=$r applied-again=$a in the converge reports"
done
t=$(desk kubectl -n tempo get statefulset tempo -o jsonpath='{.status.readyReplicas}')
o=$(desk kubectl -n opentelemetry-operator get deploy opentelemetry-operator -o jsonpath='{.status.readyReplicas}')
[ "$t" = 1 ] && [ "$o" = 1 ] && ok "tempo and the OpenTelemetry operator are ready now" || bad "ready now: tempo=${t:-0} operator=${o:-0}"
row=$(feature_row traces)
[ "$row" = "declared=true applied=true ready=true" ] && ok "orbit_features_list: traces $row" || bad "orbit_features_list: traces ${row:-(no answer)}"
old=$(cat "$SCENARIO_OUT/old-trace")
c=$(desk kubectl get --raw "/api/v1/namespaces/tempo/services/tempo:3200/proxy/api/traces/$old" | python3 -c 'import json,sys
try: d = json.load(sys.stdin)
except ValueError: d = {}
print(len(d.get("batches") or d.get("resourceSpans") or []))')
[ "${c:-0}" -ge 1 ] && ok "a trace that was in the bucket before the switch is still readable ($old, $c batch(es))" \
  || bad "the trace $old from before the switch is not readable any more"
"$ORBIT_DESK" bash -c 'orbit platform plan /instance --summary --kubeconfig "$KUBECONFIG"' 2>&1 | tr -d '\r' | grep -E '^[a-z0-9-]+ empty=' > "$SCENARIO_OUT/plan.txt"
napps=$(grep -c . "$SCENARIO_OUT/plan.txt"); drift=$(grep -vc 'empty=true' "$SCENARIO_OUT/plan.txt")
[ "$napps" -ge 10 ] && [ "$drift" = 0 ] && ok "nothing else changed: orbit platform plan is empty for all $napps apps" \
  || bad "orbit platform plan: $drift of $napps app(s) differ from git: $(grep -v 'empty=true' "$SCENARIO_OUT/plan.txt" | cut -d' ' -f1 | tr '\n' ' ')"
no_shell && ok "the agent used orbitd's tools only (no shell in its transcript)" || bad "the agent ran a shell command"
echo "$PASS passed, $FAIL failed"
[ "$FAIL" -eq 0 ]
