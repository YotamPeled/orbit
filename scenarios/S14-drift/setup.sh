#!/usr/bin/env bash
# A person changes production by hand, the way it really happens: `kubectl set env` on
# the prod api. The driver's dry-run reconcile right after it is the drift report this
# scenario checks; the agent is then asked to find the change and put production back.
set -e
. "$(dirname "$0")/../lib.sh"
mark="s14-$(date -u +%H%M%S)"
echo "$mark" > "$SCENARIO_OUT/mark"
date -u +%Y-%m-%dT%H:%M:%SZ > "$SCENARIO_OUT/t0"
desk kubectl -n checkers-lab-prod set env deploy/checkers-lab-api "HAND_EDIT=$mark"
"$ORBIT_DESK" bash -c "orbit call orbit_deploy_reconcile --yes --json '{\"project\":\"checkers-lab\",\"env\":\"prod\",\"dry_run\":true}'" \
  2>/dev/null | tr -d '\r' > "$SCENARIO_OUT/drift.json"
python3 -c '
import json, sys
t = open(sys.argv[1]).read(); d = json.loads(t[t.index("{"):])
print("drift report:", next(s["detail"] for s in d["steps"] if s["name"] == "apply"))' "$SCENARIO_OUT/drift.json"
# The reconcile loop may revert the edit before the dry run above sees it (edit at 03:21:31,
# reverted 03:21:43, dry run after, on 2026-10-05). Its record of the revert is the other
# drift report: keep orbit_deploy_status once it lists the edit, for up to two periods.
: > "$SCENARIO_OUT/drift-status.json"
if ! grep -q 'would revert a hand edit' "$SCENARIO_OUT/drift.json"; then
  for i in $(seq 1 12); do
    "$ORBIT_DESK" bash -c "orbit call orbit_deploy_status --json '{\"project\":\"checkers-lab\",\"env\":\"prod\"}'" \
      2>/dev/null | tr -d '\r' > "$SCENARIO_OUT/drift-status.json"
    grep -q 'HAND_EDIT' "$SCENARIO_OUT/drift-status.json" && break
    sleep 10
  done
fi
cat > "$SCENARIO_OUT/prompt.txt" <<P
Someone changed the checkers-lab production api by hand with kubectl a few minutes ago.
Tell me exactly what differs from what git declares, and make sure production is back to
what git says. You have my go for production.
P
