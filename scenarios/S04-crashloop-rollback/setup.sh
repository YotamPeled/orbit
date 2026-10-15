#!/usr/bin/env bash
# S4 setup: a version that crash-loops — the api built with CRASH_ON_START baked in, tagged
# <branch>-<sha7>-crash, from checkers-lab's clean, pushed main (pushed to the registry).
# Records the tags running now so the check can prove dev was rolled back and prod untouched.
set -euo pipefail
. "$(dirname "$0")/../lib.sh"
date -u +%Y-%m-%dT%H:%M:%SZ > "$SCENARIO_OUT/t0-iso"   # the fault window starts (teardown cleans its debris)
LAB=${CHECKERS_LAB_DIR:?CHECKERS_LAB_DIR is not set}
cd "$LAB" && git fetch -q && git checkout -q main && git pull -q --ff-only
[ -z "$(git status --porcelain)" ] || { echo "checkers-lab tree is not clean"; exit 2; }
tag="main-$(git rev-parse --short=7 HEAD)-crash"
docker buildx build --platform linux/amd64 --build-arg APP=api --build-arg FAULTS="CRASH_ON_START=1" \
  -t "ghcr.io/$ORBIT_SCENARIO_ORG/checkers-lab-api:$tag" --push . >/dev/null
echo "$tag" > "$SCENARIO_OUT/crash-tag"
for e in dev prod; do
  desk kubectl -n "checkers-lab-$e" get deploy checkers-lab-api -o jsonpath='{.spec.template.spec.containers[0].image}' \
    | tail -1 > "$SCENARIO_OUT/before-$e"
done
sed "s/CRASH_TAG/$tag/" "$(dirname "$0")/prompt.template" > "$SCENARIO_OUT/prompt.txt"
echo "crash variant $tag built and pushed; before: dev=$(cat "$SCENARIO_OUT/before-dev") prod=$(cat "$SCENARIO_OUT/before-prod")"
