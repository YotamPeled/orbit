#!/usr/bin/env bash
# Undo what the agent landed through one revert pull request; orbitd re-applies the pairs
# from git (and prunes the extra hostname's route) on its next tick, and the desk applies
# the services app in case a namespace-level object changed.
. "$(dirname "$0")/../lib.sh"
echo "revert: $(revert_since "$(cat "$SCENARIO_OUT/base")" s09-teardown "scenario S09 teardown: undo scale, override and hostname")"
"$ORBIT_DESK" bash -c 'echo "prune services" | orbit platform apply /instance --app services --prune --kubeconfig "$KUBECONFIG" > /dev/null' 2>&1 | grep -v '^Warning' | tail -2
