#!/usr/bin/env bash
# Pass: the new commit's tag runs in dev on every component, and the time from the merge
# to the moment the last dev api pod on that tag became ready is measured and printed
# (the number docs/V1.md §5 S18 records). The number is a measurement, not a threshold.
. "$(dirname "$0")/../lib.sh"
PASS=0 FAIL=0
ok(){ echo "  ok    $1"; PASS=$((PASS+1)); }
bad(){ echo "  FAIL  $1"; FAIL=$((FAIL+1)); }
T=$(cat "$SCENARIO_OUT/tag"); merged=$(cat "$SCENARIO_OUT/merged-at")
for c in api engine; do
  img=$(image_of dev "$c"); [ "${img##*:}" = "$T" ] && ok "dev $c runs $T" || bad "dev $c runs ${img##*:}"
done
img=$(desk kubectl -n checkers-lab-dev get cronjob checkers-lab-bot -o jsonpath='{.spec.jobTemplate.spec.template.spec.containers[0].image}' | tail -1)
[ "${img##*:}" = "$T" ] && ok "dev bot's CronJob carries $T" || bad "dev bot's CronJob carries ${img##*:}"
ready_at=$(desk kubectl -n checkers-lab-dev get pod -l app.kubernetes.io/name=api -o json | python3 -c '
import json, sys
from datetime import datetime, timezone
tag = sys.argv[1]
d = json.load(sys.stdin)
ts = []
for p in d["items"]:
    if not any(c["image"].endswith(":" + tag) for c in p["spec"]["containers"]):
        continue
    for c in (p.get("status") or {}).get("conditions") or []:
        if c["type"] == "Ready" and c["status"] == "True":
            ts.append(datetime.strptime(c["lastTransitionTime"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc).timestamp())
print(int(max(ts)) if ts else "")' "$T")
if [ -n "$ready_at" ] && [ $(( ready_at - merged )) -lt 0 ]; then
  bad "the ready dev api pod on $T predates the merge ($(( ready_at - merged )) s): it is not this commit's rollout"
elif [ -n "$ready_at" ]; then
  s=$(( ready_at - merged ))
  ok "clean commit -> live in dev: $s s (merge at $(date -u -d @"$merged" +%T 2>/dev/null || echo "$merged"), api ready at $(date -u -d @"$ready_at" +%T 2>/dev/null || echo "$ready_at"))"
  echo "$s" > "$SCENARIO_OUT/seconds"
else
  bad "no ready dev api pod on $T to time"
fi
echo "$PASS passed, $FAIL failed"
[ "$FAIL" -eq 0 ]
