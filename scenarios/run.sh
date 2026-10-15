#!/usr/bin/env bash
# Run one operating scenario (docs/V1.md §5) with a fresh agent and judge it.
#
#   scenarios/run.sh <scenario-dir> [<out-dir>]
#
# A scenario directory holds:
#   prompt.txt   the request, in plain language, exactly as a person would make it
#   role         viewer | operator (default operator): which MCP config the agent gets
#   setup.sh     optional: puts the world in the state the scenario needs (run first)
#   check.sh     the pass check: an independent oracle, never the agent's own claim
#
# The agent is a headless Claude Code session holding ONLY the operator skills and the
# orbit MCP server (scripts/skills-acceptance.sh, --auto-confirm so confirmation rounds
# are answered and recorded; prod confirmations are refused unless the scenario's
# `allow-prod` file exists). The transcript is kept under <out-dir>.
#
# Environment (none of it lives in this repository):
#   ORBIT_INSTANCE_DIR      the instance checkout (its scripts/desk.sh runs kubectl/orbit);
#                           setup.sh and check.sh get its domains and GitHub org from its
#                           instance/config.yaml as ORBIT_SCENARIO_DOMAIN,
#                           ORBIT_SCENARIO_DEV_DOMAIN and ORBIT_SCENARIO_ORG
#   ORBIT_SCENARIO_MCP_DIR  directory holding <role>.mcp.json (bearer configs; under $HOME)
#   SCENARIO_MODEL          the agent's model (default claude-sonnet-5-5)
#   ACCEPTANCE_PYTHON       python with orbitd's SDK, for the confirm proxy
# setup.sh and check.sh get ORBIT_DESK (the desk wrapper), SCENARIO_OUT (this run's
# directory) and SCENARIO_TRANSCRIPT (the rendered transcript) in their environment.
set -uo pipefail
here=$(cd "$(dirname "$0")" && pwd)
core=$(cd "$here/.." && pwd)
dir=$(cd "$1" && pwd) || { echo "run: no scenario dir $1" >&2; exit 2; }
id=$(basename "$dir")
out=${2:-$here/out/$(date -u +%Y%m%dT%H%M%SZ)}
: "${ORBIT_INSTANCE_DIR:?run: ORBIT_INSTANCE_DIR is not set}"
: "${ORBIT_SCENARIO_MCP_DIR:?run: ORBIT_SCENARIO_MCP_DIR is not set}"
export ORBIT_DESK="$ORBIT_INSTANCE_DIR/scripts/desk.sh"
# The instance's own names, read from its config: no scenario carries an instance's domain
# or GitHub org. Each may be set beforehand to point a run elsewhere.
py=python3; "$py" -c 'import yaml' >/dev/null 2>&1 || py=python
names=$("$py" -c '
import sys, yaml
c = yaml.safe_load(open(sys.argv[1], encoding="utf-8"))
print(c["domains"]["primary"], c["domains"]["dev"], c["git"]["github_org"])' "$ORBIT_INSTANCE_DIR/instance/config.yaml") \
  || { echo "run: cannot read domains.primary, domains.dev and git.github_org from the instance config" >&2; exit 2; }
read -r d dd org <<<"$names"
export ORBIT_SCENARIO_DOMAIN="${ORBIT_SCENARIO_DOMAIN:-$d}" ORBIT_SCENARIO_DEV_DOMAIN="${ORBIT_SCENARIO_DEV_DOMAIN:-$dd}" \
  ORBIT_SCENARIO_ORG="${ORBIT_SCENARIO_ORG:-$org}"
role=$(cat "$dir/role" 2>/dev/null || echo operator)
cfg="$ORBIT_SCENARIO_MCP_DIR/scenario-$role.mcp.json"
[ -f "$cfg" ] || { echo "run: no MCP config for role $role at $cfg" >&2; exit 2; }
mkdir -p "$out/$id"
export SCENARIO_OUT="$out/$id"

if [ -x "$dir/setup.sh" ] || [ -f "$dir/setup.sh" ]; then
  echo "== $id: setup"
  if ! bash "$dir/setup.sh" > "$SCENARIO_OUT/setup.log" 2>&1; then
    echo "run: $id setup failed (see $SCENARIO_OUT/setup.log)" >&2; tail -5 "$SCENARIO_OUT/setup.log" >&2
    # A setup that died half way has already changed the world (a fault build shipped, a
    # traffic loop started, a project declared): its teardown runs all the same. Left
    # alone, S10b's dead setup kept the dev api in its fault build and a traffic loop on
    # the operator machine for six hours (docs/V1.md §8).
    if [ -f "$dir/teardown.sh" ]; then
      echo "== $id: teardown (after the failed setup)"
      bash "$dir/teardown.sh" > "$SCENARIO_OUT/teardown.log" 2>&1 || echo "run: $id teardown failed too (see $SCENARIO_OUT/teardown.log)" >&2
    fi
    exit 2
  fi
fi

q="$SCENARIO_OUT/questions.tsv"
prompt="$dir/prompt.txt"
[ -f "$SCENARIO_OUT/prompt.txt" ] && prompt="$SCENARIO_OUT/prompt.txt"   # written by setup.sh
printf '%s\t%s\n' "$id" "$(tr '\n' ' ' < "$prompt")" > "$q"
never=""
[ -f "$dir/allow-prod" ] || never="env=prod"
echo "== $id: agent ($role)"
ACCEPTANCE_MODEL="${SCENARIO_MODEL:-claude-sonnet-5-5}" ACCEPTANCE_QUESTIONS="$q" \
ACCEPTANCE_NEVER_CONFIRM="$never" ACCEPTANCE_EXTRA_TOOLS="$(cat "$dir/tools" 2>/dev/null)" \
  bash "$core/scripts/skills-acceptance.sh" --auto-confirm "$cfg" "$core/skills" "$SCENARIO_OUT/agent" \
  > "$SCENARIO_OUT/harness.log" 2>&1
export SCENARIO_TRANSCRIPT="$SCENARIO_OUT/agent/1-$id.md"
[ -s "$SCENARIO_TRANSCRIPT" ] || { echo "run: $id produced no transcript (see $SCENARIO_OUT/harness.log)" >&2; exit 2; }

echo "== $id: check"
if bash "$dir/check.sh" > "$SCENARIO_OUT/check.log" 2>&1; then verdict=PASS; else verdict=FAIL; fi
cat "$SCENARIO_OUT/check.log"
if [ -f "$dir/teardown.sh" ]; then
  echo "== $id: teardown"
  # a scenario that does not put the world back has not passed: the next one starts from
  # what it left, and what it left is a finding
  bash "$dir/teardown.sh" > "$SCENARIO_OUT/teardown.log" 2>&1 \
    || { echo "run: $id teardown failed (see $SCENARIO_OUT/teardown.log)" >&2; tail -3 "$SCENARIO_OUT/teardown.log" >&2
         echo "  FAIL  teardown: $(tail -1 "$SCENARIO_OUT/teardown.log")" >> "$SCENARIO_OUT/check.log"; verdict=FAIL; }
fi
printf '%s\t%s\t%s\n' "$id" "$verdict" "$SCENARIO_TRANSCRIPT" >> "$out/results.tsv"
echo "$id: $verdict"
[ "$verdict" = PASS ]
