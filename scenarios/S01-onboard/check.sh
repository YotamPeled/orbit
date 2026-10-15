#!/usr/bin/env bash
# Pass: main's catalog has checkers-s1 with three components and a database, and the
# generated tree agrees with it (make check); both environments' api, engine and database
# are ready; the prod door answers 200 and the dev door 401 without credentials and 200
# with them; the bot's CronJob carries a tag in both; the production database archives to
# the bucket; verify stage2 (the doors) passes.
. "$(dirname "$0")/../lib.sh"
PASS=0 FAIL=0
ok(){ echo "  ok    $1"; PASS=$((PASS+1)); }
bad(){ echo "  FAIL  $1"; FAIL=$((FAIL+1)); }
P=$(cat "$SCENARIO_OUT/project" 2>/dev/null || echo checkers-s1)
( cd "$ORBIT_INSTANCE_DIR" && git checkout -q main && git pull -q --ff-only )
shape=$(python3 - "$ORBIT_INSTANCE_DIR/catalog/projects.yaml" "$P" <<'PY'
import sys, yaml
text = open(sys.argv[1], encoding="utf-8").read()
cat = yaml.safe_load(text.split("\n# ── a worked project")[0]) or {}
p = next((x for x in cat.get("projects") or [] if x.get("id") == sys.argv[2]), None)
if not p:
    print("missing"); sys.exit()
comps = sorted(c.get("name") for c in p.get("components") or [])
print(f"components={','.join(comps)} database={'yes' if p.get('database') else 'no'} "
      f"envs={','.join(sorted((p.get('environments') or {}).keys()))}")
PY
)
case "$shape" in *"components=api,bot,engine database=yes envs=dev,prod"*) ok "the catalog declares $P: $shape";; *) bad "the catalog entry for $P: $shape";; esac
desk make check | grep -q 'catalog and manifests agree' && ok "make check: catalog and manifests agree" || bad "make check failed"
for env in dev prod; do
  ns="$P-$env"
  r=$(desk kubectl -n "$ns" get deploy -o jsonpath='{range .items[*]}{.metadata.name}={.status.readyReplicas}/{.spec.replicas} {end}')
  db=$(desk kubectl -n "$ns" get clusters.postgresql.cnpg.io "$P-db" -o jsonpath='{.status.readyInstances}/{.spec.instances}')
  bot=$(desk kubectl -n "$ns" get cronjob "$P-bot" -o jsonpath='{.spec.jobTemplate.spec.template.spec.containers[0].image}')
  grep -q "$P-api=1/1" <<<"$r" && grep -q "$P-engine=1/1" <<<"$r" && [ "$db" = "1/1" ] && [ "${bot##*:}" != "$bot" ] \
    && ok "$env: api and engine ready, database 1/1, bot on ${bot##*:}" || bad "$env: deployments [$r] database $db bot ${bot##*:}"
done
# The production database archives to the bucket. Until 2026-10-09 this scenario passed
# with a database that had archived nothing in its whole life: the name had been used and
# removed before, and barman refuses a path that holds another database's files (§8).
arch=""
for i in $(seq 1 18); do
  arch=$(desk kubectl -n "$P-prod" get clusters.postgresql.cnpg.io "$P-db" -o jsonpath='{range .status.conditions[?(@.type=="ContinuousArchiving")]}{.status} {.reason}{end}')
  case "$arch" in True*) break;; esac; sleep 10
done
dest=$(desk kubectl -n "$P-prod" get objectstores.barmancloud.cnpg.io "$P-db-store" -o jsonpath='{.spec.configuration.destinationPath}')
case "$arch" in True*) ok "prod: the database archives to the bucket (ContinuousArchiving $arch, ${dest#s3://*/})";; *) bad "prod: the database does not archive (ContinuousArchiving '${arch:-no condition}', ${dest#s3://*/})";; esac
p=$(door -w '%{http_code}' "https://$P.$ORBIT_SCENARIO_DOMAIN/api/healthz")
d0=$(door -w '%{http_code}' "https://$P.$ORBIT_SCENARIO_DEV_DOMAIN/api/healthz")
cfg=$(mktemp); printf 'user = "orbit:%s"\n' "$(cat "$ORBIT_SCENARIO_MCP_DIR/dev-basic-auth-password")" > "$cfg"
d1=$(door -w '%{http_code}' -K "$cfg" "https://$P.$ORBIT_SCENARIO_DEV_DOMAIN/api/healthz"); rm -f "$cfg"
[ "$p $d0 $d1" = "200 401 200" ] && ok "doors: prod 200, dev 401 without credentials, 200 with them" || bad "doors: prod $p, dev $d0 / $d1 (want 200, 401 / 200)"
desk bash scripts/verify/stage2.sh > "$SCENARIO_OUT/stage2.log" 2>&1
grep -q "^stage2: [0-9]* passed, 0 failed" "$SCENARIO_OUT/stage2.log" && grep -q "$P" "$SCENARIO_OUT/stage2.log" \
  && ok "verify $(grep '^stage2:' "$SCENARIO_OUT/stage2.log") — including $P's doors" || bad "verify $(grep '^stage2:' "$SCENARIO_OUT/stage2.log")"
echo "$PASS passed, $FAIL failed"
[ "$FAIL" -eq 0 ]
