#!/usr/bin/env bash
# If the agent left anything, the driver finishes the removal the supported way.
here=$(cd "$(dirname "$0")" && pwd)
. "$here/../lib.sh"
P=s17-scratch
if grep -q "id: $P\b" "$ORBIT_INSTANCE_DIR/catalog/projects.yaml" 2>/dev/null; then
  catalog_edit s17-remove "scenario: remove $P (S17 teardown)" "cat['projects'] = [p for p in cat['projects'] if p['id'] != '$P']"
fi
( cd "$ORBIT_INSTANCE_DIR" && git checkout -q main && git pull -q --ff-only )
"$ORBIT_DESK" scripts/remove-project.sh $P --yes 2>&1 | grep -v '^Warning' | tail -3
