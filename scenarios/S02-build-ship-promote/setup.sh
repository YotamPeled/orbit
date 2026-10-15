#!/usr/bin/env bash
# A new commit on checkers-lab's main, landed the way a developer's change lands (a pull
# request, merged), so the agent has an unbuilt commit to build, ship to dev and promote.
set -e
. "$(dirname "$0")/../lib.sh"
cd "${CHECKERS_LAB_DIR:?CHECKERS_LAB_DIR is not set}"
git fetch -q && git checkout -q main && git pull -q --ff-only
[ -z "$(git status --porcelain)" ] || { echo "checkers-lab tree is not clean"; exit 2; }
br="scenario/s02-$(date -u +%H%M%S)"
git checkout -q -b "$br"
printf 'S02 build-ship-promote run at %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" > scenario-stamp.txt
git add scenario-stamp.txt
git -c user.name="Orbit scenario" -c user.email=scenario@example.invalid commit -q -m "scenario S02: stamp a new commit to build and ship"
git push -q -u origin "$br" 2>/dev/null
gh pr create --fill --body "Scenario S02 setup: a new commit to build, ship to dev and promote to prod." >/dev/null
gh pr merge --merge --delete-branch >/dev/null
git checkout -q main && git pull -q --ff-only
tag="main-$(git rev-parse --short=7 HEAD)"
echo "$tag" > "$SCENARIO_OUT/tag"
echo "new commit on main; its tag will be $tag"
cat > "$SCENARIO_OUT/prompt.txt" <<P
I merged a change to checkers-lab's main branch. Build it and ship it to the dev
environment, all three components; once it is healthy there, promote the same build to
production. You have my go for production. The checkers-lab source is at
$CHECKERS_LAB_DIR and the instance checkout at $ORBIT_INSTANCE_DIR.
P
