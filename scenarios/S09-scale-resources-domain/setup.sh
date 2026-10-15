#!/usr/bin/env bash
# Three catalog changes for an operator agent: scale, a per-environment override, a new
# domain. The starting point is recorded so the teardown can undo what the agent landed.
set -e
. "$(dirname "$0")/../lib.sh"
( cd "$ORBIT_INSTANCE_DIR" && git checkout -q main && git pull -q --ff-only && git rev-parse HEAD ) > "$SCENARIO_OUT/base"
date -u +%Y-%m-%dT%H:%M:%SZ > "$SCENARIO_OUT/t0-iso"
if grep -q 'checkers-lab-s9' "$ORBIT_INSTANCE_DIR/catalog/projects.yaml"; then
  echo "the catalog already mentions checkers-lab-s9 — a previous run was not torn down"; exit 2
fi
cat > "$SCENARIO_OUT/prompt.txt" <<P
Three changes to the checkers-lab project, please: run two replicas of the api in
production; give the api 384Mi of memory in the dev environment only (production keeps what
it has); and serve production on the additional hostname checkers-lab-s9.$ORBIT_SCENARIO_DOMAIN
as well. Prove each one is live. You have my go for production.
P
