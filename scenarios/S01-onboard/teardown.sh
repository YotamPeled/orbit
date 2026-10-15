#!/usr/bin/env bash
# Take the project off again the supported way: orbit_project_remove through orbitd (the
# catalog entry, a verified backup of both databases, then the namespaces). With S01_KEEP=1
# the project stays: the rebuilt box keeps the checkers-lab it has just been given.
. "$(dirname "$0")/../lib.sh"
P=$(cat "$SCENARIO_OUT/project" 2>/dev/null || echo checkers-s1)
if [ "${S01_KEEP:-0}" = 1 ]; then echo "S01_KEEP=1: $P stays"; exit 0; fi
out=$("$ORBIT_DESK" bash -c "orbit call orbit_project_remove --yes --json '{\"project\":\"$P\"}'" 2>&1 | tr -d '\r')
job=$(python3 -c 'import json,sys;t=sys.stdin.read();d=json.loads(t[t.index("{"):]);print((d.get("evidence") or {}).get("job_id",""))' <<<"$out" 2>/dev/null)
[ -n "$job" ] || { echo "orbit_project_remove did not start a job: $(head -c 300 <<<"$out")"; exit 1; }
st=""
for i in $(seq 1 12); do
  raw=$("$ORBIT_DESK" bash -c "orbit call orbit_job_wait --json '{\"job_id\":\"$job\",\"timeout_s\":120}'" 2>&1 | tr -d '\r' || true)
  st=$(python3 -c 'import json,sys;t=sys.stdin.read();d=json.loads(t[t.index("{"):]);print(d.get("status",""), ((d.get("result") or {}).get("status") or ""))' <<<"$raw" 2>/dev/null || true)
  case "$st" in completed*|failed*|cancelled*) break;; esac
done
echo "orbit_project_remove $P: $st"
left=$(desk kubectl get ns -l "orbit.local/project=$P" -o name | grep -c . || true)
[ "$st" = "completed applied" ] && [ "$left" = 0 ] && echo "$P is gone (no namespace left)" || { echo "NOT REMOVED: job $st, $left namespace(s) left"; exit 1; }
