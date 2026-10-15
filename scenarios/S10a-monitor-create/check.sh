#!/usr/bin/env bash
# Pass: the monitor is in git, it fires in Alertmanager within 10 minutes of the agent's
# answer, its own alert reached the human channel (the delivery recorder names it),
# Alertmanager sent Telegram notifications since the start with none failing, and
# the agent created it through orbitd.
. "$(dirname "$0")/../S10-common.sh"
PASS=0 FAIL=0
ok(){ echo "  ok    $1"; PASS=$((PASS+1)); }
bad(){ echo "  FAIL  $1"; FAIL=$((FAIL+1)); }
( cd "$ORBIT_INSTANCE_DIR" && git checkout -q main && git pull -q --ff-only )
[ -f "$ORBIT_INSTANCE_DIR/monitors/$S10_MONITOR.yaml" ] && ok "monitors/$S10_MONITOR.yaml is in git" || bad "no monitors/$S10_MONITOR.yaml in git"
called orbit_monitor_upsert && ok "the agent created it through orbitd (orbit_monitor_upsert)" || bad "no orbit_monitor_upsert call in the transcript"
if s10_wait_firing 600; then ok "Alertmanager holds $S10_MONITOR firing ($(s10_am_state))"; else bad "$S10_MONITOR did not fire within 10 minutes ($(s10_am_state))"; fi
if d=$(s10_delivered 420); then ok "this monitor's alert reached the human channel (delivery recorder lines naming $S10_MONITOR: $d)"
else bad "the delivery recorder has no line naming $S10_MONITOR after 7 minutes: the monitor fired and nobody was messaged"; fi
read -r sent failed <<<"$(s10_telegram)"
awk -v s="$sent" -v f="$failed" 'BEGIN{exit !(s > 0 && f == 0)}' && ok "Alertmanager sent $sent Telegram notification(s) since the start, $failed failed" \
  || bad "Telegram notifications since the start: sent $sent, failed $failed"
echo "$PASS passed, $FAIL failed"
[ "$FAIL" -eq 0 ]
