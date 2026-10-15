#!/usr/bin/env bash
# Traces goes back on, and the scenario waits until it is ready again.
here=$(cd "$(dirname "$0")" && pwd)
. "$here/../lib.sh"
"$ORBIT_DESK" orbit features enable traces --yes 2>&1 | tr -d '\r' | tail -4
for i in $(seq 1 40); do
  row=$(feature_row traces)
  [ "$row" = "declared=true applied=true ready=true" ] && break
  sleep 15
done
echo "traces after: $(feature_row traces)"
