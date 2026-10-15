#!/usr/bin/env bash
# Undo what the agent landed (catalog declaration, env reference, sealed file) through one
# revert pull request, then let the desk apply the secrets app with prune so the Secret
# goes too. orbitd re-applies the pair from git on its next tick.
. "$(dirname "$0")/../lib.sh"
echo "revert: $(revert_since "$(cat "$SCENARIO_OUT/base")" s08a-teardown "scenario S08a teardown: undo the API_TOKEN secret")"
"$ORBIT_DESK" bash -c 'echo "prune secrets" | orbit platform apply /instance --app secrets --prune --kubeconfig "$KUBECONFIG" > /dev/null' 2>&1 | grep -v '^Warning' | tail -2
