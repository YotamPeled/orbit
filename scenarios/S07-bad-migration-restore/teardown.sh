#!/usr/bin/env bash
# Put the pre-scenario tag back if the agent left the destructive one running, and put the
# games back if they are not: restoring them is the agent's job and the check has already
# judged it; production is not left without its history because an agent failed, or because
# the setup stopped after the destructive ship. The restore is the platform's own tool, from
# the backup the setup made with it.
. "$(dirname "$0")/common.sh"
rc=0
case "$(image_of prod api)" in *badmig*) b=$(cat "$SCENARIO_OUT/before"); driver_ship_back prod api "$b" || rc=1;; *) echo "api already off the variant";; esac
before=$(cat "$SCENARIO_OUT/games-before" 2>/dev/null || echo 0); now=$(games)
pb=$(cat "$SCENARIO_OUT/product-backup" 2>/dev/null || true)
if [ -n "$pb" ] && [ "${now:-0}" -lt "${before:-0}" ]; then
  echo "prod holds ${now:-0} games, $before before the scenario: restoring $pb"
  echo "driver restore: $(driver_db orbit_db_restore prod "$pb")"
  now=$(games)
  [ "${now:-0}" -ge "$before" ] && echo "prod games are back ($now)" \
    || { echo "TEARDOWN FAILED: prod holds ${now:-0} games, $before before the scenario; the verified copies are $pb (the platform's) and $SCENARIO_OUT/backup.sql"; rc=1; }
else
  echo "prod games: ${now:-0} now, $before before the scenario"
fi
clean_fault_jobs prod "$(cat "$SCENARIO_OUT/t0-iso")"
exit $rc
