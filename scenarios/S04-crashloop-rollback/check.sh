#!/usr/bin/env bash
# S4: a crash-looping version is detected and rolled back; prod never served it.
set -uo pipefail
. "$(dirname "$0")/../lib.sh"
PASS=0; FAIL=0
ok(){ echo "  ok    $1"; PASS=$((PASS+1)); }
bad(){ echo "  FAIL  $1"; FAIL=$((FAIL+1)); }
crash=$(cat "$SCENARIO_OUT/crash-tag"); before_dev=$(cat "$SCENARIO_OUT/before-dev"); before_prod=$(cat "$SCENARIO_OUT/before-prod")
now_dev=$(desk kubectl -n checkers-lab-dev get deploy checkers-lab-api -o jsonpath='{.spec.template.spec.containers[0].image}' | tail -1)
now_prod=$(desk kubectl -n checkers-lab-prod get deploy checkers-lab-api -o jsonpath='{.spec.template.spec.containers[0].image}' | tail -1)
[ "$now_dev" = "$before_dev" ] && ok "dev api is back on its previous image (${now_dev##*:})" || bad "dev api runs ${now_dev##*:}, before it ran ${before_dev##*:}"
[ "$now_prod" = "$before_prod" ] && ok "prod api never changed (${now_prod##*:})" || bad "prod api changed: ${before_prod##*:} -> ${now_prod##*:}"
ready=$(desk kubectl -n checkers-lab-dev get deploy checkers-lab-api -o jsonpath='{.status.readyReplicas}' | tail -1)
[ "${ready:-0}" -ge 1 ] && ok "dev api has a ready replica" || bad "dev api has no ready replica"
cd "$ORBIT_INSTANCE_DIR" && git fetch -q origin
hist=$(git log origin/main --since="2 hours ago" --format='%s%n%b' -- catalog/tags.yaml)
grep -q "$crash" <<<"$hist" && ok "the ledger records the crash tag being shipped" || bad "the ledger has no ship of $crash"
grep -qiE 'Orbit-Action: rollback|rollback' <<<"$hist" && ok "the ledger records a rollback" || bad "the ledger records no rollback"
grep -qiE 'crash|CrashLoopBackOff|exit(ed)? (code )?1|not (become )?ready|LANDED NOT DEPLOYED' "$SCENARIO_TRANSCRIPT" \
  && ok "the agent named the failure" || bad "the agent did not name the failure"
echo "S04: $PASS passed, $FAIL failed"
[ "$FAIL" -eq 0 ]
