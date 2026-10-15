#!/usr/bin/env bash
# Pass: the control probe reached its sibling (the probes work), every probe into prod and
# into the platform's internals got no answer, the narrowed component reached its sibling
# but not the internet while an undeclared pod did, and the agent cited NetworkPolicy.
. "$(dirname "$0")/../lib.sh"
PASS=0 FAIL=0
ok(){ echo "  ok    $1"; PASS=$((PASS+1)); }
bad(){ echo "  FAIL  $1"; FAIL=$((FAIL+1)); }
code(){ awk -v k="$1" '$1 == k {split($2, a, ":"); print a[1]}' "$SCENARIO_OUT/probes.txt"; }
# no connection: curl timed out (28) or was refused (7) while the target had a ready
# endpoint (column 3; a target that is down refuses too, and proves nothing); 52/56 is a
# connection to something that is not HTTP
blocked(){ awk -v k="$1" '$1 == k {split($2, a, ":"); exit !((a[2] == 28 || a[2] == 7) && $3 > 0)}' "$SCENARIO_OUT/probes.txt"; }
raw(){ awk -v k="$1" '$1 == k {print $2 ", target endpoints " $3}' "$SCENARIO_OUT/probes.txt"; }
[ "$(code control-sibling)" = 200 ] && ok "control: a dev pod reaches its sibling engine (200)" || bad "control probe got $(code control-sibling) — the probes prove nothing"
for k in dev-to-prod-api dev-to-prod-db; do
  blocked $k && ok "$k: no connection ($(raw $k))" || bad "$k connected ($(raw $k))"
done
for k in to-prometheus to-alertmanager to-grafana to-loki to-orbitd; do
  blocked $k && ok "$k: no connection from a tenant pod ($(raw $k))" || bad "$k connected from a tenant pod ($(raw $k))"
done
[ "$(code narrowed-to-sibling)" = 200 ] && ok "the narrowed engine reaches its sibling api" || bad "narrowed-to-sibling got $(code narrowed-to-sibling)"
blocked narrowed-to-internet && ok "the narrowed engine cannot reach the internet ($(raw narrowed-to-internet)): undeclared egress blocked" || bad "narrowed-to-internet connected ($(raw narrowed-to-internet))"
blocked open-to-internet && bad "an undeclared pod could not reach the internet either ($(raw open-to-internet)) — the narrowing test proves nothing" || ok "an undeclared pod reaches the internet ($(raw open-to-internet)): the block is the narrowing"
answer=$(sed -n '/## final answer/,$p' "$SCENARIO_TRANSCRIPT")
grep -qi 'NetworkPolic' <<<"$answer" && ok "the agent cites NetworkPolicy evidence" || bad "the agent's answer cites no NetworkPolicy"
echo "$PASS passed, $FAIL failed"
[ "$FAIL" -eq 0 ]
