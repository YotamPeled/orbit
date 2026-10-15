#!/usr/bin/env bash
# Pass: the prod api runs 2 ready replicas; the dev api requests 384Mi while prod keeps
# 256Mi; the new hostname serves the prod api over https; and a hand over-ask beyond the
# catalog (kubectl scale to 12) is refused by the namespace quota while the declared pods
# keep serving, then put back by the reconcile loop.
. "$(dirname "$0")/../lib.sh"
PASS=0 FAIL=0
ok(){ echo "  ok    $1"; PASS=$((PASS+1)); }
bad(){ echo "  FAIL  $1"; FAIL=$((FAIL+1)); }
spec(){ desk kubectl -n "checkers-lab-$1" get deploy checkers-lab-api -o jsonpath="$2"; }
r=$(spec prod '{.spec.replicas} {.status.readyReplicas}')
[ "$r" = "2 2" ] && ok "prod api: 2 replicas, 2 ready" || bad "prod api replicas/ready: $r"
dm=$(spec dev '{.spec.template.spec.containers[0].resources.requests.memory}')
pm=$(spec prod '{.spec.template.spec.containers[0].resources.requests.memory}')
[ "$dm" = 384Mi ] && [ "$pm" = 256Mi ] && ok "dev api requests 384Mi, prod keeps 256Mi" || bad "dev api requests $dm, prod $pm"
c=$(curl -s -o /dev/null -w '%{http_code}' https://checkers-lab-s9.$ORBIT_SCENARIO_DOMAIN/api/healthz)
[ "$c" = 200 ] && ok "https://checkers-lab-s9.$ORBIT_SCENARIO_DOMAIN/api/healthz answers 200" || bad "the new hostname answered $c"
# the over-ask: a hand scale far beyond what the catalog declared
desk kubectl -n checkers-lab-prod scale deploy/checkers-lab-api --replicas=12 >/dev/null
sleep 20
refused=$(desk kubectl -n checkers-lab-prod get events --field-selector reason=FailedCreate -o jsonpath='{range .items[*]}{.message}{"\n"}{end}' | grep -c 'exceeded quota: orbit-quota')
running=$(spec prod '{.status.readyReplicas}')
p=$(curl -s -o /dev/null -w '%{http_code}' https://checkers-lab.$ORBIT_SCENARIO_DOMAIN/api/healthz)
[ "$refused" -gt 0 ] && [ "${running:-0}" -ge 2 ] && [ "$p" = 200 ] \
  && ok "a hand scale to 12 is refused by orbit-quota ($refused refusal(s)); $running declared pods keep serving (prod 200)" \
  || bad "over-ask: $refused quota refusal(s), $running ready, prod $p"
back=""
for i in $(seq 1 16); do
  [ "$(spec prod '{.spec.replicas}')" = 2 ] && { back=yes; break; }
  sleep 25
done
[ -n "$back" ] && ok "the reconcile loop put the prod api back to the declared 2 replicas" || bad "prod api replicas still $(spec prod '{.spec.replicas}')"
called 'orbit_catalog_set' && ok "the catalog was changed with orbit_catalog_set" || bad "the agent never called orbit_catalog_set"
called 'orbit_deploy_wait' && ok "the agent waited with orbit_deploy_wait" || bad "the agent never called orbit_deploy_wait"
who=$( cd "$ORBIT_INSTANCE_DIR" && git fetch -q origin && git log origin/main --since="$(cat "$SCENARIO_OUT/t0-iso")" --format='%(trailers:key=Orbit-Action,valueonly)' -- catalog/projects.yaml | grep -c '^catalog-set$' )
[ "${who:-0}" -ge 1 ] && ok "the catalog commit(s) carry the ledger trailer Orbit-Action: catalog-set ($who)" || bad "no catalog commit with Orbit-Action: catalog-set since the scenario began"
no_shell && ok "the agent used orbitd's tools only (no shell in its transcript)" || bad "the agent ran a shell command"
echo "$PASS passed, $FAIL failed"
[ "$FAIL" -eq 0 ]
