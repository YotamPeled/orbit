#!/usr/bin/env bash
. "$(dirname "$0")/../S06-common.sh"
s6_check 'memory (limit|ceiling)|at its (memory )?limit|out ?of ?memory|OutOfMemory|heap' 'OrbitWorkloadMemoryAtLimit|working set|256 ?Mi|9[0-9](\.[0-9]+)? ?%|orbit_size_recommend|no_suggestion' || rc=1
called 'orbit_alerts_active' && echo "  ok    the agent read the alert (orbit_alerts_active)" || { echo "  FAIL  the agent never called orbit_alerts_active"; rc=1; }
called 'orbit_size_recommend' && echo "  ok    the agent asked orbit_size_recommend" || { echo "  FAIL  the agent never called orbit_size_recommend"; rc=1; }
grep -qiE 'engine' <<<"$(sed -n '/## final answer/,$p' "$SCENARIO_TRANSCRIPT")" && echo "  ok    the answer names the engine" || { echo "  FAIL  the answer does not name the engine"; rc=1; }
[ "${rc:-0}" = 0 ]
