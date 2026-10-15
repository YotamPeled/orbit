#!/usr/bin/env bash
# The driver makes a monitor fire (the dev api's 5xx problem plus the monitor itself,
# created through orbitd as an operator would), then the agent is asked to silence it.
# Its own name, not S10a's: S10a's alert can still be active in Alertmanager for minutes
# after S10a deletes its monitor, and once passed for this one (docs/V1.md §8).
set -e
export S10_MONITOR=s10b-api-errors
. "$(dirname "$0")/../S10-common.sh"
s10_fault_up
body='{"name":"s10b-api-errors","project":"checkers-lab","env":"dev","kind":"metric","severity":"warn","summary":"Dev api 5xx ratio above 10% over 2m","query":"sum(rate(http_requests_received_total{namespace=\"checkers-lab-dev\",service=\"checkers-lab-api\",code=~\"5..\"}[2m])) / sum(rate(http_requests_received_total{namespace=\"checkers-lab-dev\",service=\"checkers-lab-api\"}[2m]))","condition":{"comparison":">","threshold":0.1,"for":"1m"}}'
# the body reaches the desk as a file in its mounted config directory
printf '%s' "{\"monitor\": $body}" > "$ORBIT_SCENARIO_MCP_DIR/s10b-upsert.json"
# the monitors lock may still be held by S10a's delete job: a busy answer is retried
for try in 1 2 3 4 5 6; do
  out=$("$ORBIT_DESK" bash -c 'orbit call orbit_monitor_upsert --yes --json "$(cat "$ORBIT_CONFIG_DIR/s10b-upsert.json")"' 2>&1 | tr -d '\r')
  job=$(python3 -c 'import json,sys;t=sys.stdin.read();d=json.loads(t[t.index("{"):]);print((d.get("evidence") or {}).get("job_id",""))' <<<"$out" 2>/dev/null)
  [ -n "$job" ] && break
  grep -qi busy <<<"$out" || break
  sleep 30
done
rm -f "$ORBIT_SCENARIO_MCP_DIR/s10b-upsert.json"
[ -n "$job" ] || { echo "the monitor upsert did not start a job: $(head -c 300 <<<"$out")"; exit 2; }
st=""
for i in 1 2 3 4 5; do
  # an answer that is not JSON (the call failed on the way) is asked again, and shown
  raw=$("$ORBIT_DESK" bash -c "orbit call orbit_job_wait --json '{\"job_id\":\"$job\",\"timeout_s\":120}'" 2>&1 | tr -d '\r' || true)
  st=$(python3 -c 'import json,sys;t=sys.stdin.read();print(json.loads(t[t.index("{"):]).get("status",""))' <<<"$raw" 2>/dev/null || true)
  [ -n "$st" ] || echo "orbit_job_wait gave no answer (try $i): $(head -c 200 <<<"$raw")"
  case "$st" in completed|failed|cancelled) break;; esac
done
[ "$st" = completed ] || { echo "the monitor upsert job ended '$st'"; exit 2; }
echo "monitor $S10_MONITOR created (job $job completed)"
s10_wait_firing 900 || { echo "the monitor did not fire within 15 minutes ($(s10_am_state))"; exit 2; }
echo "firing: $(s10_am_state)"
cat > "$SCENARIO_OUT/prompt.txt" <<P
The monitor s10b-api-errors on checkers-lab's dev environment is firing and paging the team
while we work on the cause. Silence it for one hour with the reason "S10 test". Change
nothing else.
P
