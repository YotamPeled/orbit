#!/usr/bin/env bash
# Run the operating-scenario suite (docs/V1.md §5) one scenario at a time and print the
# results table.
#
#   scenarios/run-all.sh <out-dir> [<scenario-dir-name> ...]
#
# With no names, every scenario directory runs in the order below: the ones that only
# read first, the ones that change the world after, S16 (a reboot) last. They run one at
# a time because they share one cluster, one instance repository and one deploy lock.
# S15 needs S15_RELEASE (a core release newer than the instance's) and is skipped
# without it. Environment: as scenarios/run.sh (ORBIT_INSTANCE_DIR,
# ORBIT_SCENARIO_MCP_DIR, CHECKERS_LAB_DIR, ACCEPTANCE_PYTHON, SCENARIO_MODEL).
set -uo pipefail
here=$(cd "$(dirname "$0")" && pwd)
out=${1:?usage: run-all.sh <out-dir> [scenario ...]}; shift
ORDER=(S03-nonexistent-tag S11-guardrails S12-network-isolation S06a-oom S06b-5xx S06c-slow
       S06d-errorlogs S06e-memory-at-limit S08b-missing-secret S04-crashloop-rollback S14-drift S13-tls S10a-monitor-create
       S10b-silence S08a-secret-add-rotate S09-scale-resources-domain S22-size-from-advice S02-build-ship-promote
       S18-deploy-speed S05-zero-downtime S07-bad-migration-restore S17-remove-project
       S17b-remove-unprovable-backup S01-onboard S20-tool-of-a-feature-that-is-off S19-feature-off-on
       S15-core-upgrade S16-resilience)
[ "$#" -gt 0 ] && ORDER=("$@")
mkdir -p "$out"
for s in "${ORDER[@]}"; do
  if [ "$s" = S15-core-upgrade ] && [ -z "${S15_RELEASE:-}" ]; then
    printf '%s\t%s\t%s\n' "$s" SKIPPED "S15_RELEASE not set" >> "$out/results.tsv"; continue
  fi
  bash "$here/run.sh" "$here/$s" "$out" >> "$out/run.log" 2>&1 \
    || grep -q "^$s"$'\t' "$out/results.tsv" 2>/dev/null \
    || printf '%s\t%s\t%s\n' "$s" ERROR "setup or harness failed (see $out/run.log)" >> "$out/results.tsv"
done
echo "== results ($out/results.tsv)"
cut -f1,2 "$out/results.tsv" | column -t
! cut -f2 "$out/results.tsv" | grep -qvE '^(PASS|SKIPPED)$'
