#!/usr/bin/env bash
# The engine goes back to the build it was on (and is seen back on it), and the failed bot
# Jobs of the window are removed.
. "$(dirname "$0")/../lib.sh"
rc=0
driver_ship_back dev engine "$(cat "$SCENARIO_OUT/before")" || rc=1
clean_fault_jobs dev "$(cat "$SCENARIO_OUT/t0")"
exit $rc
