# S6 (diagnose) shared steps, sourced by each S06x scenario. A fault variant is shipped to
# dev by the DRIVER, traffic is driven through the dev door, then a fresh VIEWER agent is
# asked what is wrong. The check wants the cause named with evidence the agent read.
# Variables each scenario sets: S6_COMP S6_NAME S6_FAULTS S6_WAIT (seconds of symptom)
. "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

s6_setup(){
  set -e
  date -u +%Y-%m-%dT%H:%M:%SZ > "$SCENARIO_OUT/t0"
  image_of dev "$S6_COMP" > "$SCENARIO_OUT/before"
  [ -s "$SCENARIO_OUT/before" ] || { echo "could not read the image dev $S6_COMP runs (the cluster did not answer three times)"; exit 2; }
  # A fault variant's tag is the build's tag with the fault's name after it. Starting from
  # one means an earlier fault scenario was not torn down, and this one's teardown would
  # "restore" the fault (the rebuilt box, 2026-10-08: S06c recorded S06b's variant).
  if on_a_variant "$(cat "$SCENARIO_OUT/before")"; then
    echo "dev $S6_COMP is on a fault variant ($(cat "$SCENARIO_OUT/before")): an earlier fault scenario was not torn down"; exit 2
  fi
  tag=$(build_variant "$S6_COMP" "$S6_NAME" "$S6_FAULTS")
  echo "$tag" > "$SCENARIO_OUT/variant"
  echo "variant $tag; before $(cat "$SCENARIO_OUT/before")"
  echo "driver ship: $(driver_ship dev "$S6_COMP" "$tag")"   # a failed verdict is expected for some faults
  fault_is_planted dev "$S6_COMP" "$tag" || { echo "driver ship (again): $(driver_ship dev "$S6_COMP" "$tag")"; fault_is_planted dev "$S6_COMP" "$tag"; }
  dev_traffic "$S6_WAIT"
  echo "drove ${S6_WAIT}s of dev traffic"
}

s6_teardown(){
  before=$(cat "$SCENARIO_OUT/before")
  local rc=0
  driver_ship_back dev "$S6_COMP" "$before" || rc=1
  clean_fault_jobs dev "$(cat "$SCENARIO_OUT/t0")"
  return $rc
}

# s6_check <cause-regex> <evidence-regex>
s6_check(){
  local PASS=0 FAIL=0
  ok(){ echo "  ok    $1"; PASS=$((PASS+1)); }
  bad(){ echo "  FAIL  $1"; FAIL=$((FAIL+1)); }
  answer=$(sed -n '/## final answer/,$p' "$SCENARIO_TRANSCRIPT")
  grep -qiE "$1" <<<"$answer" && ok "the answer names the cause (/$1/)" || bad "the answer does not name the cause (/$1/)"
  grep -qiE "$2" <<<"$answer" && ok "the answer cites evidence (/$2/)" || bad "the answer cites no evidence (/$2/)"
  called 'orbit_[a-z_]+' && ok "the agent read the platform through orbitd" || bad "no orbitd tool call in the transcript"
  echo "$(basename "$(dirname "$SCENARIO_TRANSCRIPT")" | sed 's/^agent$//'): $PASS passed, $FAIL failed"
  [ "$FAIL" -eq 0 ]
}
