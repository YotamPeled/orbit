#!/usr/bin/env bash
# The dev engine is shipped a variant that holds memory outside its managed heap until the
# container's working set is 93% of its 256Mi limit, and stays there alive (checkers-lab's
# FILL_MEMORY_PERCENT). Until 2026-10-08 this scenario drove traffic at the plain engine
# and waited for the alert, on the belief that its heap would fill the container; measured
# on the rebuilt box, .NET refuses allocations with the container at 56% of its limit, and
# the alert rightly never fired (docs/V1.md §8). When OrbitWorkloadMemoryAtLimit is firing,
# a viewer agent is asked what is wrong.
set -e
. "$(dirname "$0")/../lib.sh"
date -u +%Y-%m-%dT%H:%M:%SZ > "$SCENARIO_OUT/t0"
am(){ desk kubectl -n monitoring exec alertmanager-kube-prometheus-stack-alertmanager-0 -c alertmanager -- amtool --alertmanager.url=http://localhost:9093 alert query alertname=OrbitWorkloadMemoryAtLimit 2>/dev/null | grep -c 'checkers-lab-dev\|engine' || true; }
image_of dev engine > "$SCENARIO_OUT/before"
if on_a_variant "$(cat "$SCENARIO_OUT/before")"; then
  echo "dev engine is on a fault variant ($(cat "$SCENARIO_OUT/before")): an earlier fault scenario was not torn down"; exit 2
fi
# the variant's name says nothing about what it does: the tag is in the deploy history the
# agent reads, and a workload that sits at its limit does not announce it
tag=$(build_variant engine b "FILL_MEMORY_PERCENT=93")
echo "$tag" > "$SCENARIO_OUT/variant"
echo "variant $tag; before $(cat "$SCENARIO_OUT/before")"
echo "driver ship: $(driver_ship dev engine "$tag")"
fault_is_planted dev engine "$tag" || { echo "driver ship (again): $(driver_ship dev engine "$tag")"; fault_is_planted dev engine "$tag"; }
fired=0
for round in $(seq 1 60); do
  sleep 30
  [ "$(am)" -ge 1 ] && { fired=1; break; }
done
[ "$fired" = 1 ] || { echo "OrbitWorkloadMemoryAtLimit did not fire for the dev engine within 30 minutes of it sitting at 93% of its limit"; exit 2; }
echo "OrbitWorkloadMemoryAtLimit is firing for the dev engine $(( round / 2 )) min after the variant was shipped"
