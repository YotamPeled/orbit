#!/usr/bin/env bash
# The dev api has a real 5xx problem; the agent is asked for a monitor on it. The waiting
# for it to fire is the check's (S10-common.sh says why).
set -e
. "$(dirname "$0")/../S10-common.sh"
s10_fault_up
cat > "$SCENARIO_OUT/prompt.txt" <<P
The checkers-lab api in dev keeps answering with server errors. Create a monitor named
s10-api-errors that alerts (warning level) when more than 10% of the dev api's requests
fail with a 5xx over two minutes, and tell me once it is in place. Do not wait for it to
fire. Do not touch production.
P
