#!/usr/bin/env bash
# Bump a component's deployed tag and land it on main, safely against concurrent bumps.
#
# Lives in a script rather than inside the workflow YAML so that the retry can be tested
# by actually racing two of them, which is the only way anyone was ever going to find out
# it was broken. It was: the first project to build three images from one push made the
# three dispatch jobs race, the old `git pull --rebase` retry hit a conflict,
# left unmerged files behind, and every subsequent attempt failed on those. Two of the
# three tags landed and the third was silently dropped — a partial deploy, which reads as
# success everywhere except the one component that did not move.
#
# The fix is not a better merge. It is to RECOMPUTE the bump on top of whatever main is
# now: the edit is derived entirely from the payload, so re-deriving it on a newer base is
# always correct and cannot conflict.
#
# Env: IMAGE TAG BRANCH REPO (the dispatch payload), all read by bump-tag.py.
# OUTBOX_DELIVERY_ID is optional. When present, the deployment commit also carries a
# durable per-image receipt so replay can be recognized after later deployments.
# GIT_TRAILERS is optional: newline-separated `Key: value` lines, each appended as
# `git commit --trailer`. ship.sh uses this for the six Orbit-* trailers.
#
# ORBIT_CLONE is how orbitd ships (scripts/ship.sh, with a core of its own): this file is
# then THIS BUILD's copy, run on the fresh clone ORBIT_CLONE names, and after every reset
# the clone's vendored core must be byte for byte this build's (scripts/core-same.py)
# before anything out of the clone runs. Without ORBIT_CLONE (an instance's own CI, the
# desk) the checkout this file sits in is the one bumped, as it always was.
set -uo pipefail
SELF="$(cd "$(dirname "$0")" && pwd)"
. "$SELF/lib-instance.sh" || exit 2
if [ -n "${ORBIT_CLONE:-}" ]; then
  [ -n "${ORBIT_CORE_DIR:-}" ] && [ -f "$ORBIT_CORE_DIR/core-manifest.txt" ] \
    || { echo "ORBIT_CLONE is set without a core to compare the clone with (ORBIT_CORE_DIR)"; exit 1; }
  cd "$ORBIT_CLONE" || exit 1
else
  cd "$SELF/.."
fi

ATTEMPTS="${ATTEMPTS:-5}"
REMOTE="${REMOTE:-origin}"
MAIN="${MAIN:-main}"

git config user.name  "${GIT_AUTHOR_NAME:-orbit-ci}"
git config user.email "${GIT_AUTHOR_EMAIL:-ci@$ORBIT_DOMAIN}"

TRAILER_ARGS=()
if [ -n "${GIT_TRAILERS:-}" ]; then
  while IFS= read -r line || [ -n "$line" ]; do
    [ -z "$line" ] && continue
    TRAILER_ARGS+=(--trailer "$line")
  done <<EOF
$GIT_TRAILERS
EOF
fi

DELIVERY_ID="${OUTBOX_DELIVERY_ID:-}"
RECEIPT_EFFECT=""
if [ -n "$DELIVERY_ID" ]; then
  case "$DELIVERY_ID" in
    *[!A-Za-z0-9-]*|'') echo "refusing invalid outbox delivery id"; exit 1;;
  esac
  [ "${#DELIVERY_ID}" -le 128 ] \
    || { echo "refusing invalid outbox delivery id"; exit 1; }
  # This validates the existing deployment inputs without loading or rewriting catalog
  # state. The resulting key is non-secret and identifies this delivery's image effect.
  RECEIPT_EFFECT=$(python3 scripts/bump-tag.py --receipt-key) || exit 1
fi

receipt_exists() {
  [ -n "$DELIVERY_ID" ] || return 1
  git log "$REMOTE/$MAIN" --format='%B%x00' \
    | RECEIPT_DELIVERY="$DELIVERY_ID" RECEIPT_EFFECT="$RECEIPT_EFFECT" python3 -c '
import os, sys
delivery = ("Orbit-Delivery: " + os.environ["RECEIPT_DELIVERY"]).encode()
effect = ("Orbit-Effect: " + os.environ["RECEIPT_EFFECT"]).encode()
messages = sys.stdin.buffer.read().split(b"\0")
raise SystemExit(0 if any(delivery in m.splitlines() and effect in m.splitlines()
                          for m in messages) else 1)
'
}

for i in $(seq 1 "$ATTEMPTS"); do
  git fetch --quiet "$REMOTE" "$MAIN" || { echo "fetch failed"; exit 1; }
  # Receiver clones are shallow. A receipt may be behind a newer deployment, so the
  # authoritative remote branch must be searched through its complete history.
  if [ "$(git rev-parse --is-shallow-repository)" = true ]; then
    git fetch --quiet --unshallow "$REMOTE" "$MAIN" \
      || { echo "full-history fetch failed"; exit 1; }
  fi
  git reset --hard --quiet "$REMOTE/$MAIN"
  if [ -n "${ORBIT_CLONE:-}" ]; then
    # nothing out of this clone runs unless it is this build's own text
    if ! DIFFERS="$(python3 "$SELF/core-same.py" "$ORBIT_CORE_DIR" .)"; then
      echo "the instance repository's copy of orbit-core is not this build's: ${DIFFERS} (run \`make core-sync\` there and land it; nothing was run from it)"
      exit 1
    fi
  fi

  if receipt_exists; then
    echo "delivery effect already recorded — no deployment change"
    echo "COMMIT $(git rev-parse HEAD)"
    exit 0
  fi

  # An authorization refusal is final. Retrying it would print the same refusal five
  # times and bury the reason in the noise.
  BUMP_NOTE=$(python3 scripts/bump-tag.py) || { printf '%s\n' "$BUMP_NOTE"; exit 1; }
  printf '%s\n' "$BUMP_NOTE"

  if git diff --quiet; then
    if [ -z "$DELIVERY_ID" ]; then
      # an empty diff has two causes and bump-tag said which: nothing claims the branch
      # (it printed that line itself), or the tag written is the tag already deployed
      case "$BUMP_NOTE" in
        *"already current:"*) echo "tag already deployed — nothing to commit";;
        *) echo "no environment claims branch '${BRANCH:-?}' — image built, deployed nowhere";;
      esac
      echo "COMMIT $(git rev-parse HEAD)"
      exit 0
    fi
    # A no-op is still a completed decision. Recording it prevents an old delivery from
    # becoming a rollback if catalog state changes before replay.
    git commit --allow-empty -qm "deploy receipt: ${IMAGE:-?}:${TAG:-?} (${BRANCH:-?})" \
      --trailer "Orbit-Delivery: $DELIVERY_ID" --trailer "Orbit-Effect: $RECEIPT_EFFECT" \
      "${TRAILER_ARGS[@]}" || { echo "commit failed"; exit 1; }
  elif [ -n "$DELIVERY_ID" ]; then
    git commit -aqm "deploy: ${IMAGE:-?}:${TAG:-?} (${BRANCH:-?})" \
      --trailer "Orbit-Delivery: $DELIVERY_ID" --trailer "Orbit-Effect: $RECEIPT_EFFECT" \
      "${TRAILER_ARGS[@]}" || { echo "commit failed"; exit 1; }
  else
    git commit -aqm "deploy: ${IMAGE:-?}:${TAG:-?} (${BRANCH:-?})" \
      "${TRAILER_ARGS[@]}" || { echo "commit failed"; exit 1; }
  fi
  # a commit that did not move HEAD (nothing staged, a hook that swallowed it) must never
  # reach the push: the push of an unchanged HEAD succeeds and would read as a deploy
  [ "$(git rev-parse HEAD)" != "$(git rev-parse "$REMOTE/$MAIN")" ] || { echo "commit made no new commit"; exit 1; }
  if git push --quiet "$REMOTE" "HEAD:$MAIN"; then
    echo "COMMIT $(git rev-parse HEAD)"
    exit 0
  fi
  echo "push rejected (attempt $i) — someone else landed first, recomputing"
  sleep $((i * 2))
done

echo "could not push after $ATTEMPTS attempts"
exit 1
