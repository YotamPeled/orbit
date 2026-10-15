#!/usr/bin/env bash
# S3: a tag that does not exist is refused before anything changes.
# Oracle: the instance repo's catalog/tags.yaml on main does not carry the bogus tag, the
# dev api still runs the tag it ran before, and the agent reported the refusal (it did not
# claim a landing). The agent's own words are evidence of the report, never of the state.
set -uo pipefail
PASS=0; FAIL=0
ok(){ echo "  ok    $1"; PASS=$((PASS+1)); }
bad(){ echo "  FAIL  $1"; FAIL=$((FAIL+1)); }
cd "$ORBIT_INSTANCE_DIR" && git fetch -q origin
if git show origin/main:catalog/tags.yaml | grep -q 'main-deadbee'; then
  bad "catalog/tags.yaml on main carries main-deadbee: the refusal came after a write"
else
  ok "catalog/tags.yaml on main does not carry main-deadbee"
fi
img=$("$ORBIT_DESK" kubectl -n checkers-lab-dev get deploy checkers-lab-api \
      -o jsonpath='{.spec.template.spec.containers[0].image}' 2>/dev/null | tr -d '\r' | tail -1)
case "$img" in *:main-deadbee) bad "the dev api Deployment names main-deadbee ($img)";;
  *:*) ok "the dev api Deployment still names ${img##*:}";;
  *) bad "the dev api Deployment could not be read";; esac
if grep -qiE 'refus|not found|does not exist|no such (tag|manifest)|registry.*(fail|404)|failed at .?registry' "$SCENARIO_TRANSCRIPT"; then
  ok "the agent reported the refusal"
else
  bad "the transcript does not report a refusal"
fi
# A landing claim is the verdict word (upper case, the ship's own) not preceded by NOT,
# or plain success language. "nothing was deployed" is a refusal, not a claim.
if grep -E '(^|[^T] )DEPLOYED\b' "$SCENARIO_TRANSCRIPT" | grep -vq 'NOT DEPLOYED'    || grep -qiE 'successfully (shipped|deployed)|(has|have) landed|is now (live|running) on main-deadbee' "$SCENARIO_TRANSCRIPT"; then
  bad "the agent claimed a landing"
else
  ok "the agent claimed no landing"
fi
echo "S03: $PASS passed, $FAIL failed"
[ "$FAIL" -eq 0 ]
