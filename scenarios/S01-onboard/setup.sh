#!/usr/bin/env bash
# Onboard a project from its repository: the checkers-lab code a second time, as project
# checkers-s1, on hostnames the instance's wildcard DNS and certificate already cover. The
# starting point is recorded so the teardown can undo the catalog and remove the project.
# S01_PROJECT names another id (the rebuilt box, docs/V1.md M4, onboards checkers-lab
# itself this way) and S01_KEEP=1 leaves the project in place afterwards.
set -e
. "$(dirname "$0")/../lib.sh"
( cd "$ORBIT_INSTANCE_DIR" && git checkout -q main && git pull -q --ff-only && git rev-parse HEAD ) > "$SCENARIO_OUT/base"
P=${S01_PROJECT:-checkers-s1}
echo "$P" > "$SCENARIO_OUT/project"
if grep -q "id: $P\$" "$ORBIT_INSTANCE_DIR/catalog/projects.yaml"; then
  echo "$P is already in the catalog — a previous run was not torn down"; exit 2
fi
cat > "$SCENARIO_OUT/prompt.txt" <<P
Onboard a new project on Orbit: id $P, from the repository
$ORBIT_SCENARIO_ORG/checkers-lab (the source is checked out at $CHECKERS_LAB_DIR; its README
describes the three components and what they need). It needs its database, a dev and a
production environment, production on $P.$ORBIT_SCENARIO_DOMAIN and dev on
$P.$ORBIT_SCENARIO_DEV_DOMAIN. Build its current main, ship it to both
environments, and prove both are serving. You have my go for production. The instance
checkout is at $ORBIT_INSTANCE_DIR (run desk commands through its scripts/desk.sh).
P
