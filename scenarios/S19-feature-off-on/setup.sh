#!/usr/bin/env bash
# An admin agent is asked to turn the traces feature off and on again. The starting point
# is recorded so the check can prove three things the agent cannot fake: the two switches
# are two commits by orbitd with their ledger words, a trace that was already in the
# bucket before is still readable after, and nothing else on the platform moved.
set -e
here=$(cd "$(dirname "$0")" && pwd)
. "$here/../lib.sh"
date -u +%Y-%m-%dT%H:%M:%SZ > "$SCENARIO_OUT/t0-iso"
( cd "$ORBIT_INSTANCE_DIR" && git checkout -q main && git pull -q --ff-only && git rev-parse HEAD ) > "$SCENARIO_OUT/base"
row=$(feature_row traces); echo "traces before: ${row:-unknown}"
[ "$row" = "declared=true applied=true ready=true" ] || { echo "the traces feature must be on, applied and ready before this scenario"; exit 2; }
desk kubectl -n tempo get statefulset tempo -o jsonpath='{.status.readyReplicas}' | grep -q '^1' \
  || { echo "tempo is not running: the traces feature must be on and ready before this scenario"; exit 2; }
# A trace old enough to have been flushed to the bucket (Tempo cuts blocks within minutes;
# one from 45 minutes to 6 hours ago is in object storage, not in the ingester's memory).
now=$(date +%s)
old=$(desk kubectl get --raw "/api/v1/namespaces/tempo/services/tempo:3200/proxy/api/search?start=$((now - 21600))&end=$((now - 2700))&limit=5" \
  | python3 -c 'import json,sys
try: t = json.load(sys.stdin).get("traces") or []
except ValueError: t = []
print(t[0]["traceID"] if t else "")')
[ -n "$old" ] || { echo "Tempo holds no trace older than 45 minutes: nothing to prove the bucket data with"; exit 2; }
echo "$old" > "$SCENARIO_OUT/old-trace"
echo "a trace from before, to find again afterwards: $old"
# The words until 2026-10-09 were "we are about to run a memory-heavy job ... When that is
# done, turn it back on". An agent read "that" as the job, turned traces off, and rightly
# stopped to ask when the job would end (docs/V1.md §8). What is asked is the same; when
# is now said.
cat > "$SCENARIO_OUT/prompt.txt" <<P
This is a rehearsal for a memory-heavy job we will run on the node another day: turn the
platform's traces feature off and show me it is really off. As soon as you have shown that,
in this same session, turn it back on and show me traces work again. You have my go for
both, and there is nothing to wait for between them.
P
