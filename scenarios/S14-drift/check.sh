#!/usr/bin/env bash
# Pass: the drift report taken right after the hand edit names it, the live Deployment no
# longer carries it (within two reconcile periods), the api is serving, and the agent's
# answer names the field that was changed.
. "$(dirname "$0")/../lib.sh"
PASS=0 FAIL=0
ok(){ echo "  ok    $1"; PASS=$((PASS+1)); }
bad(){ echo "  FAIL  $1"; FAIL=$((FAIL+1)); }
EDIT='hand edit: spec.template.spec.containers[name=api].env[name=HAND_EDIT] (by kubectl-set)'
t0=$(cat "$SCENARIO_OUT/t0")
reverted_at=$(grep -oE '20[0-9T:-]+Z Deployment checkers-lab-prod/checkers-lab-api: reverted a hand edit: [^"]*' \
  "$SCENARIO_OUT/drift-status.json" 2>/dev/null | grep -F "reverted a $EDIT" | head -1 | cut -d' ' -f1)
if grep -qF "would revert a $EDIT" "$SCENARIO_OUT/drift.json"; then
  ok "the drift report names the hand edit (containers[name=api].env[name=HAND_EDIT], by kubectl-set)"
elif [ -n "$reverted_at" ] && [[ ! "$reverted_at" < "$t0" ]]; then
  ok "the reconcile loop reverted the hand edit first and recorded it ($reverted_at, edit at $t0)"
else
  bad "the drift report does not name the hand edit"
fi
gone=""
for i in $(seq 1 16); do
  v=$(desk kubectl -n checkers-lab-prod get deploy checkers-lab-api -o jsonpath='{.spec.template.spec.containers[0].env[?(@.name=="HAND_EDIT")].value}')
  [ -z "$v" ] && { gone=yes; break; }
  sleep 25
done
[ -n "$gone" ] && ok "the live prod api no longer carries HAND_EDIT" || bad "HAND_EDIT is still on the live prod api"
desk kubectl -n checkers-lab-prod rollout status deploy/checkers-lab-api --timeout=300s >/dev/null \
  && ok "the prod api rollout is complete" || bad "the prod api rollout did not complete"
answer=$(sed -n '/## final answer/,$p' "$SCENARIO_TRANSCRIPT")
grep -q 'HAND_EDIT' <<<"$answer" && ok "the agent's answer names HAND_EDIT" || bad "the agent's answer does not name the changed field"
echo "$PASS passed, $FAIL failed"
[ "$FAIL" -eq 0 ]
