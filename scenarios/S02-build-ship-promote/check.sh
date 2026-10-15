#!/usr/bin/env bash
# Pass: every component runs the new commit's tag in dev and in prod, the ledger shows dev
# took it before prod, and the post-ship assertions passed (a DEPLOYED verdict).
. "$(dirname "$0")/../lib.sh"
T=$(cat "$SCENARIO_OUT/tag")
PASS=0 FAIL=0
ok(){ echo "  ok    $1"; PASS=$((PASS+1)); }
bad(){ echo "  FAIL  $1"; FAIL=$((FAIL+1)); }
for env in dev prod; do
  for c in api engine; do
    img=$(image_of "$env" "$c")
    [ "${img##*:}" = "$T" ] && ok "$env $c runs $T" || bad "$env $c runs ${img##*:}, not $T"
  done
  img=$(desk kubectl -n "checkers-lab-$env" get cronjob checkers-lab-bot -o jsonpath='{.spec.jobTemplate.spec.template.spec.containers[0].image}' | tail -1)
  [ "${img##*:}" = "$T" ] && ok "$env bot's CronJob carries $T" || bad "$env bot's CronJob carries ${img##*:}, not $T"
done
first_at(){ # <env>: when the ledger first shows every component of <env> on $T
  "$ORBIT_DESK" bash -c "orbit call orbit_ledger_get --json '{\"project\":\"checkers-lab\",\"env\":\"$1\",\"limit\":20}'" 2>/dev/null \
    | python3 -c '
import json, sys
t = sys.stdin.read(); d = json.loads(t[t.index("{"):])
hits = [i["at"] for i in d.get("items") or [] if set((i.get("tags") or {}).values()) == {sys.argv[1]}]
print(min(hits) if hits else "")' "$T"
}
dev_at=$(first_at dev); prod_at=$(first_at prod)
[ -n "$dev_at" ] && [ -n "$prod_at" ] && [[ "$dev_at" < "$prod_at" ]] \
  && ok "the ledger shows dev took $T ($dev_at) before prod ($prod_at)" \
  || bad "the ledger order is wrong or incomplete (dev '$dev_at', prod '$prod_at')"
grep -q 'DEPLOYED' "$SCENARIO_TRANSCRIPT" && ok "a ship reported a DEPLOYED verdict" || bad "no DEPLOYED verdict in the transcript"
echo "$PASS passed, $FAIL failed"
[ "$FAIL" -eq 0 ]
