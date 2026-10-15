#!/usr/bin/env bash
# An operator agent upgrades the instance to a core release (S15_RELEASE, which must be
# tagged and have its images published) while constant traffic hits the prod door.
set -e
here=$(cd "$(dirname "$0")" && pwd)
. "$here/../lib.sh"
R=${S15_RELEASE:?S15_RELEASE is not set (the core release to upgrade to)}
echo "$R" > "$SCENARIO_OUT/release"
( cd "$ORBIT_INSTANCE_DIR" && git checkout -q main && git pull -q --ff-only && sed -n 's/^ref: //p' orbit-core.lock ) > "$SCENARIO_OUT/from"
[ "$(cat "$SCENARIO_OUT/from")" != "$R" ] || { echo "the instance already runs $R"; exit 2; }
for c in api engine; do image_of prod "$c"; done > "$SCENARIO_OUT/images-before"
: > "$SCENARIO_OUT/traffic.log"
nohup bash "$here/../S05-zero-downtime/traffic.sh" "https://checkers-lab.$ORBIT_SCENARIO_DOMAIN/api/getwinners" \
  "$SCENARIO_OUT/traffic.log" "$SCENARIO_OUT/stop" > /dev/null 2>&1 &
sleep 5
echo "upgrade $(cat "$SCENARIO_OUT/from") -> $R under traffic"
cat > "$SCENARIO_OUT/prompt.txt" <<P
Upgrade this Orbit instance from orbit-core $(cat "$SCENARIO_OUT/from") to $R, apply it to
the platform, and prove the platform now runs $R with every verification passing.
checkers-lab's players must not notice. The instance checkout is at $ORBIT_INSTANCE_DIR
(run desk commands through its scripts/desk.sh).
P
