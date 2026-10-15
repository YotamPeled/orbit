#!/usr/bin/env bash
# Stop the traffic and put the pre-scenario tag back if the agent left the variant running.
. "$(dirname "$0")/../lib.sh"
touch "$SCENARIO_OUT/stop"
[ "$(cat "$SCENARIO_OUT/traffic-from" 2>/dev/null)" = node ] && ssh -o BatchMode=yes "$ORBIT_NODE_SSH" 'touch /tmp/s05-stop' || true
b=$(cat "$SCENARIO_OUT/before")
rc=0
case "$(image_of prod api)" in *notready*) driver_ship_back prod api "$b" || rc=1;; *) echo "api already off the variant";; esac
clean_fault_jobs prod "$(cat "$SCENARIO_OUT/t0-iso")"
exit $rc
