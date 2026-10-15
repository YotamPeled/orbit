#!/usr/bin/env bash
# The dev api gets a new secret, and then it is rotated: a job an operator does at the
# desk (orbit secrets mint, a catalog declaration, the desk applies). The starting point
# is recorded so the teardown can undo what the agent landed.
set -e
. "$(dirname "$0")/../lib.sh"
( cd "$ORBIT_INSTANCE_DIR" && git checkout -q main && git pull -q --ff-only && git rev-parse HEAD ) > "$SCENARIO_OUT/base"
if grep -v '^[[:space:]]*#' "$ORBIT_INSTANCE_DIR/catalog/projects.yaml" | grep -q 'API_TOKEN'; then   # the worked example's comments name one
  echo "the catalog already mentions API_TOKEN — a previous run was not torn down"; exit 2
fi
cat > "$SCENARIO_OUT/prompt.txt" <<P
The checkers-lab api in the dev environment needs a new secret: an environment variable
API_TOKEN holding a random credential (any random value will do). Add it, make sure the
running dev api has it, then rotate it to a fresh random value and make sure the running
dev api uses the new value. Do not touch production. The instance checkout is at
$ORBIT_INSTANCE_DIR (run desk commands through its scripts/desk.sh).
P
echo "base $(cat "$SCENARIO_OUT/base")"
