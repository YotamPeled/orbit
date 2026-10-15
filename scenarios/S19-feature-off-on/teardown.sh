#!/usr/bin/env bash
# If the agent left traces off, the driver turns it back on the supported way.
here=$(cd "$(dirname "$0")" && pwd)
. "$here/../lib.sh"
row=$(feature_row traces)
case "$row" in
  "declared=true"*) echo "traces is on ($row)";;
  *) echo "traces was left off ($row): turning it on"
     "$ORBIT_DESK" orbit features enable traces --yes 2>&1 | tr -d '\r' | tail -3;;
esac
