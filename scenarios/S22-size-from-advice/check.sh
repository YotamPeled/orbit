#!/usr/bin/env bash
# Pass: the agent asked orbit_size_recommend; the dev api no longer holds four times its
# use (at most 320Mi) and the dev engine is allowed more than it has already used (at least
# 288Mi); both changes went through orbit_catalog_set and run on the pods; production is as
# it was; no shell.
. "$(dirname "$0")/../lib.sh"
PASS=0 FAIL=0
ok(){ echo "  ok    $1"; PASS=$((PASS+1)); }
bad(){ echo "  FAIL  $1"; FAIL=$((FAIL+1)); }
mi(){ python3 -c 'import sys
v=sys.argv[1]
print(int(v[:-2]) if v.endswith("Mi") else int(float(v[:-2])*1024) if v.endswith("Gi") else -1)' "${1:-0Mi}"; }
lim(){ desk kubectl -n "checkers-lab-$1" get deploy "checkers-lab-$2" -o jsonpath='{.spec.template.spec.containers[0].resources.limits.memory}'; }
called 'orbit_size_recommend' && ok "the agent asked orbit_size_recommend" || bad "the agent never called orbit_size_recommend"
for i in $(seq 1 20); do a=$(lim dev api); e=$(lim dev engine); [ "$a" != 960Mi ] && [ "$e" != 208Mi ] && break; sleep 15; done
[ "$(mi "$a")" -gt 0 ] && [ "$(mi "$a")" -le 320 ] && ok "the dev api was brought down from 960Mi to $a" || bad "the dev api is allowed $a (wanted at most 320Mi)"
[ "$(mi "$e")" -ge 288 ] && ok "the dev engine was raised from 208Mi to $e, above what it has already used" || bad "the dev engine is allowed $e (wanted at least 288Mi)"
r=$(desk kubectl -n checkers-lab-dev get deploy checkers-lab-api checkers-lab-engine -o jsonpath='{range .items[*]}{.status.readyReplicas}/{.spec.replicas} {end}')
[ "$r" = "1/1 1/1 " ] && ok "both run with the new values (ready $r)" || bad "dev api and engine ready: $r"
desk kubectl -n checkers-lab-prod get deploy,cronjob -o jsonpath='{range .items[*]}{.metadata.name}={.spec.template.spec.containers[0].resources.limits.memory}{.spec.jobTemplate.spec.template.spec.containers[0].resources.limits.memory}{"\n"}{end}' > "$SCENARIO_OUT/prod-after"
cmp -s "$SCENARIO_OUT/prod-before" "$SCENARIO_OUT/prod-after" && ok "production is as it was ($(tr '\n' ' ' < "$SCENARIO_OUT/prod-after"))" || bad "production changed: $(tr '\n' ' ' < "$SCENARIO_OUT/prod-after")"
called 'orbit_catalog_set' && ok "the change went through orbit_catalog_set" || bad "the agent never called orbit_catalog_set"
who=$( cd "$ORBIT_INSTANCE_DIR" && git fetch -q origin && git log origin/main --since="$(cat "$SCENARIO_OUT/t0-iso")" --format='%(trailers:key=Orbit-Action,valueonly)' -- catalog/projects.yaml | grep -c '^catalog-set$' )
[ "${who:-0}" -ge 1 ] && ok "the catalog commit(s) carry the ledger trailer Orbit-Action: catalog-set ($who)" || bad "no catalog commit with Orbit-Action: catalog-set since the scenario began"
no_shell && ok "the agent used orbitd's tools only (no shell in its transcript)" || bad "the agent ran a shell command"
echo "$PASS passed, $FAIL failed"
[ "$FAIL" -eq 0 ]
