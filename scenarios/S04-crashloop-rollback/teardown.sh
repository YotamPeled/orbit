#!/usr/bin/env bash
# The crash window makes the dev bot's runs fail; those failed Jobs are this scenario's debris.
. "$(dirname "$0")/../lib.sh"
clean_fault_jobs dev "$(cat "$SCENARIO_OUT/t0-iso")"
