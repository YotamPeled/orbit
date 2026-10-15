#!/usr/bin/env bash
# Pass: main pins the release (lock and both image tags) and the vendored files match it,
# orbitd reports it, `make verify` passes every stage, no prod request failed during the
# upgrade, and checkers-lab's images did not change.
. "$(dirname "$0")/../lib.sh"
touch "$SCENARIO_OUT/stop"; sleep 3
PASS=0 FAIL=0
ok(){ echo "  ok    $1"; PASS=$((PASS+1)); }
bad(){ echo "  FAIL  $1"; FAIL=$((FAIL+1)); }
R=$(cat "$SCENARIO_OUT/release")
( cd "$ORBIT_INSTANCE_DIR" && git checkout -q main && git pull -q --ff-only )
lock=$(sed -n 's/^ref: //p' "$ORBIT_INSTANCE_DIR/orbit-core.lock")
tags=$(grep -E '^\s*(orbitdTag|deskTag):' "$ORBIT_INSTANCE_DIR/instance/config.yaml" | grep -c "\"\?$R\"\?")
[ "$lock" = "$R" ] && [ "$tags" -eq 2 ] && ok "main pins $R (lock, orbitdTag, deskTag)" || bad "main pins lock=$lock, $tags of 2 image tags at $R"
desk make core-check | grep -q "vendored core matches $R" && ok "the vendored files match $R" || bad "core-check does not report $R"
hz=$(curl -s "https://orbit.$ORBIT_SCENARIO_DOMAIN/healthz")
grep -q "\"version\":\"$R\"" <<<"$hz" && ok "orbitd reports $R ($hz)" || bad "orbitd reports $hz"
desk make verify > "$SCENARIO_OUT/verify.log" 2>&1
grep -q '^verify: all stages passed' "$SCENARIO_OUT/verify.log" \
  && ok "make verify: all stages passed ($(grep -E '^stage[0-9a-z-]*: ' "$SCENARIO_OUT/verify.log" | tr '\n' ' '))" \
  || bad "make verify did not pass ($(grep -E '^stage[0-9a-z-]*: |FAIL' "$SCENARIO_OUT/verify.log" | head -8 | tr '\n' ' '))"
total=$(wc -l < "$SCENARIO_OUT/traffic.log"); failed=$(awk '$2 != "200"' "$SCENARIO_OUT/traffic.log" | wc -l)
[ "$total" -gt 100 ] && [ "$failed" -eq 0 ] && ok "0 failed of $total prod requests during the upgrade" || bad "$failed failed of $total prod requests"
for c in api engine; do image_of prod "$c"; done > "$SCENARIO_OUT/images-after"
cmp -s "$SCENARIO_OUT/images-before" "$SCENARIO_OUT/images-after" && ok "checkers-lab's prod images did not change" || bad "checkers-lab's prod images changed"
echo "$PASS passed, $FAIL failed"
[ "$FAIL" -eq 0 ]
