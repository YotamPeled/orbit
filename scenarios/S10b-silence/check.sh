#!/usr/bin/env bash
# Pass: Alertmanager holds the alert suppressed by a silence, and the silence was made
# through orbitd by the agent.
export S10_MONITOR=s10b-api-errors
. "$(dirname "$0")/../S10-common.sh"
PASS=0 FAIL=0
ok(){ echo "  ok    $1"; PASS=$((PASS+1)); }
bad(){ echo "  FAIL  $1"; FAIL=$((FAIL+1)); }
st=$(s10_am_state)
case "$st" in *suppressed:silenced*) ok "Alertmanager holds $S10_MONITOR suppressed by a silence ($st)";; *) bad "Alertmanager state of $S10_MONITOR: $st";; esac
called orbit_monitor_silence && ok "the agent silenced it through orbitd (orbit_monitor_silence)" || bad "no orbit_monitor_silence call in the transcript"
echo "$PASS passed, $FAIL failed"
[ "$FAIL" -eq 0 ]
