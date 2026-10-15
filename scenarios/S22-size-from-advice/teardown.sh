#!/usr/bin/env bash
# Undo the missizing and whatever the agent landed through one revert pull request; orbitd
# re-applies the pair from git on its next tick.
. "$(dirname "$0")/../lib.sh"
echo "revert: $(revert_since "$(cat "$SCENARIO_OUT/base")" s22-teardown "scenario S22 teardown: checkers-lab dev sizes as they were")"
for i in $(seq 1 24); do
  a=$(desk kubectl -n checkers-lab-dev get deploy checkers-lab-api -o jsonpath='{.spec.template.spec.containers[0].resources.limits.memory}')
  e=$(desk kubectl -n checkers-lab-dev get deploy checkers-lab-engine -o jsonpath='{.spec.template.spec.containers[0].resources.limits.memory}')
  [ "$a" = 256Mi ] && [ "$e" = 256Mi ] && break
  sleep 15
done
if [ "$a" = 256Mi ] && [ "$e" = 256Mi ]; then
  # ...and the PODS are of it. Until 2026-10-09 this stopped at the Deployments' spec, and
  # left dev jammed without saying so: the quota had gone down with the sizes, the old 1Gi
  # pod was still there, every new pod was refused, and three later scenarios failed
  # behind it (docs/V1.md §8).
  if wait_applied checkers-lab-dev checkers-lab-api 300 >/dev/null 2>&1 && wait_applied checkers-lab-dev checkers-lab-engine 300 >/dev/null 2>&1; then
    echo "dev api and engine are back at 256Mi and their pods are of it"; exit 0
  fi
  echo "NOT RESTORED: dev api and engine are declared 256Mi and their rollout did not finish: $(desk kubectl -n checkers-lab-dev get deploy checkers-lab-api checkers-lab-engine -o jsonpath='{range .items[*]}{.metadata.name} {.status.readyReplicas}/{.spec.replicas} updated {.status.updatedReplicas}; {end}')"; exit 1
fi
echo "NOT RESTORED: dev api $a, dev engine $e, six minutes after the revert"; exit 1
