#!/usr/bin/env bash
# Block until orbitd has applied a commit of the instance repo to one project environment.
#
#   scripts/desk.sh scripts/wait-applied.sh <project> <env> [<commit>] [--timeout <s>]
#
# A catalog change lands as a merge; orbitd's checkout follows the repo within about a
# minute and its reconcile loop applies the pair on its next tick. An agent cannot sleep
# while that happens — its host refuses a bare sleep, and a session that ends its turn
# "to wait" never comes back (S09, S10 on the V1 test box, docs/V1.md §8) — so this is the
# wait: it polls orbit_deploy_status until the reconcile loop's last tick ran on a checkout
# that contains <commit> (default: this checkout's HEAD, after `git pull`), with no failed
# object, then prints APPLIED and exits 0. A tick that failed, a loop that is off for the
# pair, or the timeout (default 600 s) end it with the reason and a non-zero exit.
set -uo pipefail
P=${1:-}; E=${2:-}; shift 2 2>/dev/null || true
C=""; T=600
while [ "$#" -gt 0 ]; do
  case "$1" in --timeout) T=${2:-600}; shift 2;; *) C=$1; shift;; esac
done
case "$P$E" in ""|*[!a-z0-9-]*) echo "usage: wait-applied.sh <project> <env> [<commit>] [--timeout <s>]"; exit 2;; esac
[ -n "$C" ] || C=$(git rev-parse HEAD)
case "$C" in *[!0-9a-f]*|"") echo "not a commit: $C"; exit 2;; esac
until=$(( $(date +%s) + T ))
last=""
while [ "$(date +%s)" -lt "$until" ]; do
  st=$(orbit call orbit_deploy_status --json "{\"project\":\"$P\",\"env\":\"$E\"}" 2>/dev/null | python3 -c '
import json, sys
t = sys.stdin.read()
try:
    d = json.loads(t[t.index("{"):])
except ValueError:
    print("error -"); sys.exit()
r = d.get("reconcile")
if r is None:
    print("off -"); sys.exit()
if str(r.get("mode", "")).startswith("off"):
    print("pair-off -"); sys.exit()
print("tick", (r.get("source") or {}).get("commit") or "-", r.get("last_run"), r.get("applied", 0), r.get("failed", 0))')
  set -- $st
  case "$1" in
    off) echo "orbitd's reconcile loop is off (ORBITD_APPLY=off): nothing applies this commit"; exit 2;;
    pair-off) echo "$P/$E is not in ORBITD_APPLY_PAIRS: orbitd does not apply this pair"; exit 2;;
    tick)
      src=$2 at=$3 applied=$4 failed=$5
      if [ "$src" != - ]; then
        git cat-file -e "$src^{commit}" 2>/dev/null || git fetch -q origin 2>/dev/null
        if git merge-base --is-ancestor "$C" "$src" 2>/dev/null; then
          if [ "$failed" = 0 ]; then
            echo "APPLIED: $P/$E carries ${C:0:12} (orbitd tick at $at on ${src:0:12}, $applied object(s) applied)"; exit 0
          fi
          echo "orbitd's tick at $at on ${src:0:12} FAILED on $failed object(s): read orbit_deploy_status and orbit-diagnose"; exit 1
        fi
      fi
      [ "$src $at" != "$last" ] && echo "waiting: orbitd's last tick for $P/$E ran at $at on ${src:0:12}, not yet on ${C:0:12}"
      last="$src $at";;
    *) ;;
  esac
  sleep 15
done
echo "TIMEOUT after ${T}s: orbitd has not applied ${C:0:12} to $P/$E (last tick: ${last:-none})"
exit 1
