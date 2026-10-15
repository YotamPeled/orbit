#!/usr/bin/env bash
# Reboot the node (the test box only: ssh alias `orbit`, ORBIT_NODE_SSH), wait for the API
# and every workload to come back, then hand a viewer agent the question "is everything
# back?". The check waits until 35 minutes after boot before judging alerts, so a reboot's
# page cache has had time to trip a working-set alert if one still measured that.
set -e
. "$(dirname "$0")/../lib.sh"
NODE=${ORBIT_NODE_SSH:-orbit}
ssh "$NODE" 'cat /proc/sys/kernel/random/boot_id' > "$SCENARIO_OUT/boot-before"
# The reboot is made a few seconds after a ten-minute mark, when checkers-lab's bot CronJobs
# (every 10 minutes) have a run in flight: the run the reboot cuts fails, and that failed Job is
# what once kept KubeJobFailed firing for hours (docs/V1.md §8). The worst minute, every time.
while :; do s=$(( $(date +%s) % 600 )); [ "$s" -ge 8 ] && [ "$s" -le 20 ] && break; sleep 2; done
echo "rebooting $(( $(date +%s) % 600 )) s after a ten-minute mark (a bot run is in flight)"
ssh "$NODE" 'systemctl reboot' || true
sleep 60
for i in $(seq 1 60); do
  b=$(ssh -o ConnectTimeout=10 "$NODE" 'cat /proc/sys/kernel/random/boot_id' 2>/dev/null || true)
  [ -n "$b" ] && [ "$b" != "$(cat "$SCENARIO_OUT/boot-before")" ] && break
  sleep 15
done
echo "$b" > "$SCENARIO_OUT/boot-after"
[ "$b" != "$(cat "$SCENARIO_OUT/boot-before")" ] || { echo "the node did not come back with a new boot id"; exit 2; }
ssh "$NODE" 'date -u -d "$(uptime -s)" +%s' > "$SCENARIO_OUT/booted-at"
echo "rebooted: boot id $(cat "$SCENARIO_OUT/boot-before") -> $b at $(date -u -d @"$(cat "$SCENARIO_OUT/booted-at")" +%FT%TZ 2>/dev/null || cat "$SCENARIO_OUT/booted-at")"
for i in $(seq 1 60); do desk kubectl get --raw /readyz 2>/dev/null | grep -q ok && break; sleep 15; done
for i in $(seq 1 60); do
  notready=$(desk kubectl get deploy -A -o jsonpath='{range .items[*]}{.metadata.namespace}/{.metadata.name} {.status.readyReplicas} {.spec.replicas}{"\n"}{end}' \
    | awk '$3 > 0 && $2 != $3' | wc -l)
  [ "$notready" -eq 0 ] && break
  sleep 20
done
echo "deployments not ready after the wait: $notready"
# The agent reaches orbitd through the public door, not the API: a Deployment can be ready
# before Traefik routes to it (batch 2's agent met "Connection closed" and had no tools).
# Wait for the door the agent's MCP server names to answer /healthz three times running.
door=$(python3 -c 'import json,sys;print(json.load(open(sys.argv[1]))["mcpServers"]["orbit"]["url"].rsplit("/mcp",1)[0])'   "${ORBIT_SCENARIO_MCP_DIR:?}/scenario-viewer.mcp.json")
ok=0
for i in $(seq 1 60); do
  if [ "$(curl -s -o /dev/null -m 10 -w '%{http_code}' "$door/healthz")" = 200 ]; then ok=$((ok+1)); else ok=0; fi
  [ "$ok" -ge 3 ] && break
  sleep 10
done
[ "$ok" -ge 3 ] || { echo "orbitd's door $door/healthz did not answer 200 after the reboot"; exit 2; }
echo "orbitd's door answers: $door/healthz 200 (3 in a row)"
cat > "$SCENARIO_OUT/prompt.txt" <<P
The Orbit server was rebooted a few minutes ago. Is everything back to normal — the
platform and the checkers-lab project in both environments? Show me the evidence you based
that on. Do not change anything.
P
