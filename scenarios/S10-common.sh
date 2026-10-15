# S10 (monitors) shared steps, sourced by S10a (create) and S10b (silence). A headless
# agent cannot wait for an alert (a foreground sleep is refused by its host), so the
# waiting is the driver's: S10a's agent creates the monitor and the check waits for it to
# fire; S10b's setup makes it fire and the agent silences it.
. "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
: "${S10_MONITOR:=s10-api-errors}"   # S10b sets its own

# s10_fault_up: the dev api answers 5xx for 60% of requests, under constant dev traffic
s10_fault_up(){
  date -u +%s > "$SCENARIO_OUT/t0"
  date -u +%Y-%m-%dT%H:%M:%SZ > "$SCENARIO_OUT/t0-iso"
  image_of dev api > "$SCENARIO_OUT/before"
  if on_a_variant "$(cat "$SCENARIO_OUT/before")"; then
    echo "dev api is on a fault variant ($(cat "$SCENARIO_OUT/before")): an earlier fault scenario was not torn down"; exit 2
  fi
  local tag; tag=$(build_variant api errors "ERROR_RATE=0.6")
  echo "driver ship dev: $(driver_ship dev api "$tag")"
  fault_is_planted dev api "$tag" || { echo "driver ship dev (again): $(driver_ship dev api "$tag")"; fault_is_planted dev api "$tag" || exit 2; }
  local here; here=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
  nohup bash -c ". '$here/lib.sh'; until [ -e '$SCENARIO_OUT/stop' ]; do dev_traffic 60; done" > /dev/null 2>&1 &
  sleep 5
}

# s10_am_state: the monitor's alert states in Alertmanager, e.g. "active" or
# "suppressed:silenced", comma-joined; "none" when Alertmanager holds no such alert
s10_am_state(){
  local am
  am=$(desk kubectl -n monitoring get pod -l app.kubernetes.io/name=alertmanager -o jsonpath='{.items[0].metadata.name}')
  desk kubectl -n monitoring exec "$am" -c alertmanager -- wget -qO- \
      "http://localhost:9093/api/v2/alerts?filter=alertname%3D$S10_MONITOR&silenced=true&active=true&inhibited=true" \
    | python3 -c '
import json, sys
a = json.loads(sys.stdin.read() or "[]")
print(",".join(sorted({x["status"]["state"] + (":silenced" if x["status"].get("silencedBy") else "") for x in a})) or "none")'
}

# s10_wait_firing <seconds>: until Alertmanager holds the alert active (fired, not silenced)
s10_wait_firing(){
  local until=$(( $(date +%s) + $1 ))
  while [ "$(date +%s)" -lt "$until" ]; do
    case "$(s10_am_state)" in *active*) return 0;; esac
    sleep 20
  done
  return 1
}

# s10_telegram: "<sent> <failed>" Telegram notifications since t0
s10_telegram(){
  local t0 win
  t0=$(cat "$SCENARIO_OUT/t0"); win=$(( ($(date +%s) - t0) / 60 + 2 ))
  mq(){ "$ORBIT_DESK" bash -c "orbit call orbit_metrics_query --json '$(python3 -c 'import json,sys;print(json.dumps({"query":sys.argv[1]}))' "$1")'" 2>/dev/null \
    | python3 -c 'import json,sys;t=sys.stdin.read();d=json.loads(t[t.index("{"):]);v=[float(p[-1]) for x in d.get("items") or [] for p in x.get("values") or []];print(round(sum(v),1))'; }
  echo "$(mq "sum(increase(alertmanager_notifications_total{integration=\"telegram\"}[${win}m]))") $(mq "sum(increase(alertmanager_notifications_failed_total{integration=\"telegram\"}[${win}m]))")"
}

# s10_teardown: stop the traffic, delete the monitor through orbitd, put the dev api back
s10_teardown(){
  touch "$SCENARIO_OUT/stop"
  "$ORBIT_DESK" bash -c "orbit call orbit_monitor_delete --yes --json '{\"name\":\"$S10_MONITOR\"}'" 2>/dev/null | tr -d '\r' | grep -o '"status": *"[a-z_]*"' | head -1
  local b rc=0; b=$(cat "$SCENARIO_OUT/before")
  driver_ship_back dev api "$b" || rc=1
  clean_fault_jobs dev "$(cat "$SCENARIO_OUT/t0-iso")"
  # The delete must really have removed the rule. It once did not (docs/V1.md §8): the
  # ship back above lands 15 s after the delete's commit, the delete's job failed nine
  # minutes later, and the rule stayed in the cluster for two days, unseen.
  local i left=1
  for i in $(seq 1 18); do
    left=$(desk kubectl -n monitoring get prometheusrule "orbit-monitor-$S10_MONITOR" -o name --ignore-not-found | grep -c . || true)
    [ "$left" = 0 ] && break
    sleep 10
  done
  [ "$left" = 0 ] && echo "the monitor's rule is gone from the cluster" \
    || { echo "LEFT BEHIND: PrometheusRule orbit-monitor-$S10_MONITOR is still in the cluster 3 minutes after its delete"; return 1; }
  [ "$rc" = 0 ] || { echo "TEARDOWN FAILED: the dev api was not put back (above)"; return 1; }
}

# s10_delivered <seconds>: this monitor's own alert reached the human channel. The receiver
# that holds the Telegram notifier also holds the delivery recorder, whose line names the
# alert; the notification counters alone cannot tell this alert from any other that was
# sent meanwhile (they let a monitor that messaged nobody pass, docs/V1.md §8). Prints the
# number of recorder lines; fails when there is none in time.
s10_delivered(){
  local n=0 i
  for i in $(seq 1 $(( ${1:-420} / 20 ))); do
    n=$(lokiq "sum(count_over_time({namespace=\"monitoring\", pod=~\"alert-recorder.*\"} |= \"$S10_MONITOR\" [30m]))" "$(date +%s)")
    case "$n" in ""|0|0.0|error|unreadable) sleep 20;; *) echo "$n"; return 0;; esac
  done
  echo "${n:-0}"; return 1
}

