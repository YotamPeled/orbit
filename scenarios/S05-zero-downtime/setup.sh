#!/usr/bin/env bash
# Build an api variant that never becomes ready (FAIL_READINESS: /readyz 503 for ever,
# /healthz 200), start constant traffic at the prod door, then hand the agent a ship of
# that variant, its rollback and a restart, all in production, under that traffic.
set -e
here=$(cd "$(dirname "$0")" && pwd)
. "$here/../lib.sh"
date -u +%Y-%m-%dT%H:%M:%SZ > "$SCENARIO_OUT/t0-iso"   # the fault window starts (teardown cleans its debris)
image_of prod api > "$SCENARIO_OUT/before"
tag=$(build_variant api notready "FAIL_READINESS=1")
echo "$tag" > "$SCENARIO_OUT/variant"
echo "variant $tag; prod api before: $(cat "$SCENARIO_OUT/before")"
date -u +%s > "$SCENARIO_OUT/t0"
: > "$SCENARIO_OUT/traffic.log"
# Where the requests are made from. "None may fail" is judged request by request, and a
# request that never left the operator machine is not the platform's downtime: measured on
# 2026-10-08, that machine's link drops a connection now and then (a control address at the
# same moments), and five such requests of 764 failed a run in which the platform dropped
# none. With the node's ssh alias (ORBIT_NODE_SSH) the loop runs ON the node, against the
# same public name: through the node's public address, Traefik and the Service, with no
# internet in between. Without the alias it runs here, as before.
URL="https://checkers-lab.$ORBIT_SCENARIO_DOMAIN/api/getwinners"
if [ -n "${ORBIT_NODE_SSH:-}" ]; then
  ssh -o BatchMode=yes "$ORBIT_NODE_SSH" "cat > /tmp/s05-traffic.sh; rm -f /tmp/s05-stop /tmp/s05-traffic.log; : > /tmp/s05-traffic.log; nohup bash /tmp/s05-traffic.sh '$URL' /tmp/s05-traffic.log /tmp/s05-stop > /dev/null 2>&1 &" < "$here/traffic.sh"
  echo "node" > "$SCENARIO_OUT/traffic-from"
  sleep 10
  ssh -o BatchMode=yes "$ORBIT_NODE_SSH" 'cat /tmp/s05-traffic.log' > "$SCENARIO_OUT/traffic.log"
else
  nohup bash "$here/traffic.sh" "$URL" "$SCENARIO_OUT/traffic.log" "$SCENARIO_OUT/stop" > /dev/null 2>&1 &
  echo "here" > "$SCENARIO_OUT/traffic-from"
  sleep 10
fi
n=$(wc -l < "$SCENARIO_OUT/traffic.log")
[ "$n" -gt 5 ] || { echo "traffic did not start ($n requests in 10 s)"; touch "$SCENARIO_OUT/stop"; exit 2; }
echo "traffic running from $(cat "$SCENARIO_OUT/traffic-from"): $n requests in the first 10 s"
cat > "$SCENARIO_OUT/prompt.txt" <<P
Ship tag $tag of the checkers-lab api to production. If it does not come up healthy, roll
it back. Then restart the production api so it runs on fresh pods. Players are on the site
the whole time: none of their requests may fail. You have my go for production.
P
