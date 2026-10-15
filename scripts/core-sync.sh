#!/usr/bin/env bash
# Vendor orbit-core into an instance repo at the version pinned in orbit-core.lock.
#
# The delivery model (docs/PRODUCT.md, DEVELOPMENT.md): core ships as tagged releases;
# an instance repo pins exactly one and commits the vendored copy, so the working tree
# is self-contained, Argo and CI read files at unchanged paths, and a core upgrade is
# a one-line pin bump whose re-vendor diff IS the review surface. Never edit a
# vendored file in the instance repo — the drift check (core-check) will catch it,
# and the edit belongs in orbit-core.
#
# Usage, from the instance repo root:
#   scripts/core-sync.sh          # sync to the pin in orbit-core.lock
#   scripts/core-sync.sh --check  # verify vendored files match the pin (no writes)
set -euo pipefail

LOCK="orbit-core.lock"
[ -f "$LOCK" ] || { echo "no $LOCK in $(pwd) — run from the instance repo root"; exit 1; }
CORE_URL=$(sed -n 's/^url: //p' "$LOCK")
CORE_REF=$(sed -n 's/^ref: //p' "$LOCK")
[ -n "$CORE_URL" ] && [ -n "$CORE_REF" ] || { echo "$LOCK must carry url: and ref: lines"; exit 1; }

TMP=$(mktemp -d); trap 'rm -rf "$TMP"' EXIT
git clone -q --depth 1 --branch "$CORE_REF" "$CORE_URL" "$TMP/core" 2>/dev/null \
  || { git clone -q "$CORE_URL" "$TMP/core" && git -C "$TMP/core" checkout -q "$CORE_REF"; }

MODE="${1:-sync}"
RC=0
# Both ends of every copy are resolved against these two roots and must land beneath one
# of them. Physical paths (`pwd -P`), because the containment test below compares strings
# and a symlinked component would make a path that escapes look like one that does not.
CORE_ROOT=$(cd "$TMP/core" && pwd -P)
INST_ROOT=$(pwd -P)

# A manifest line is `<path>` or `<path> -> <path>`, and the grammar is enforced, not
# assumed. Added 2026-09-07 (review round 2, Sol blocker / Muse 1-2): the sync arm does
# `rm -rf "$dst"` before it copies, so a destination that escapes the instance repo — one
# `../` or a leading `/` in a line somebody adds later — deletes and rewrites whatever it
# lands on, on every instance that bumps its pin. An empty destination killed the run
# mid-loop under `set -u` instead of reporting; an empty SOURCE copied the whole core
# clone, `.git` and all; `a -> b -> c` split at the first arrow for the source and the
# last for the destination, silently. A bad line is named, counted, and stepped over, so
# one typo does not hide the rest of the manifest; the run still exits non-zero.
path_ok() {  # <path> <what it is> <the line it came from>
  case "$1" in
    '')  echo "manifest line has an empty $2: $3"; return 1;;
    /*)  echo "manifest $2 is an absolute path ($1): $3"; return 1;;
  esac
  case "/$1/" in
    */../*) echo "manifest $2 has a '..' segment ($1): $3"; return 1;;
  esac
  return 0
}
beneath() {  # <absolute path> <root> <what it is> <the line it came from>
  case "$1/" in
    "$2"/?*) return 0;;
    *) echo "manifest $3 resolves outside $2 ($1): $4"; return 1;;
  esac
}

# Destination paths a manifest lists, one per line. A bad line is skipped rather than
# recorded: leftover deletion only ever removes a path the previous (valid) manifest
# listed, never anything a `../` or absolute dest could have named.
manifest_dests() {  # <manifest> <outfile>
  : > "$2"
  [ -f "$1" ] || return 0
  while IFS= read -r line; do
    case "$line" in ''|'#'*) continue;; esac
    rest="$line"; arrows=0
    while [ "$rest" != "${rest#*" -> "}" ]; do rest="${rest#*" -> "}"; arrows=$((arrows + 1)); done
    [ "$arrows" -gt 1 ] && continue
    if [ "$arrows" -eq 1 ]; then
      dst="${line##* -> }"
    else
      dst="$line"
    fi
    path_ok "$dst" "destination" "$line" || continue
    abs_dst=$(realpath -m -- "$INST_ROOT/$dst")
    beneath "$abs_dst" "$INST_ROOT" "destination" "$line" || continue
    printf '%s\n' "$dst" >> "$2"
  done < "$1"
}

# Read the instance's current manifest BEFORE the copy loop overwrites it (the
# manifest vendors itself). Destinations it listed that the pinned core no longer
# ships are leftovers: sync deletes them, --check reports them as drift. A path
# the previous manifest never named is left alone.
manifest_dests "$INST_ROOT/core-manifest.txt" "$TMP/prev-dests"
: > "$TMP/new-dests"

while IFS= read -r line; do
  case "$line" in ''|'#'*) continue;; esac
  # `src -> dest` for the one kind of artifact whose path in core is not its path here:
  # a file that lives under core's template/ (because a NEW org gets it by copying that
  # tree) but must also reach an org that already exists. Everything else is one path,
  # meaning the same on both sides. Today that is template/provisioning -> provisioning.
  rest="$line"; arrows=0
  while [ "$rest" != "${rest#*" -> "}" ]; do rest="${rest#*" -> "}"; arrows=$((arrows + 1)); done
  if [ "$arrows" -gt 1 ]; then
    echo "manifest line carries $arrows ' -> ' separators, and a line maps at most one path: $line"
    RC=1; continue
  fi
  if [ "$arrows" -eq 1 ]; then
    src="${line%% -> *}"; dst="${line##* -> }"
  else
    src="$line"; dst="$line"
  fi
  path_ok "$src" "source" "$line" || { RC=1; continue; }
  path_ok "$dst" "destination" "$line" || { RC=1; continue; }
  # -m: the destination need not exist yet, and neither does a source the manifest got
  # wrong — the point is where the path WOULD land, checked before anything is removed.
  abs_src=$(realpath -m -- "$CORE_ROOT/$src")
  abs_dst=$(realpath -m -- "$INST_ROOT/$dst")
  beneath "$abs_src" "$CORE_ROOT" "source" "$line" || { RC=1; continue; }
  beneath "$abs_dst" "$INST_ROOT" "destination" "$line" || { RC=1; continue; }
  printf '%s\n' "$dst" >> "$TMP/new-dests"
  if [ ! -e "$TMP/core/$src" ]; then echo "manifest names missing core path: $src"; RC=1; continue; fi
  if [ "$MODE" = "--check" ]; then
    diff -rq "$TMP/core/$src" "$dst" >/dev/null 2>&1 \
      || { echo "DRIFT: $dst differs from core@$CORE_REF ($src)"; RC=1; }
  else
    # A vendored path is core's, but an operator who edited it (the staged sources under
    # provisioning/ read as if they were theirs) must not lose the edit to a re-run of
    # `make init`: uncommitted changes under the destination refuse the sync, by path —
    # unless the destination already holds exactly what core would write (a re-run after
    # an uncommitted `make core-sync`: nothing would be lost, so there is nothing to refuse).
    if [ -e "$dst" ] && git -C "$INST_ROOT" status --porcelain --untracked-files=all -- "$dst" 2>/dev/null | grep -q . \
       && ! diff -rq "$TMP/core/$src" "$dst" >/dev/null 2>&1; then
      echo "REFUSED: $dst has uncommitted changes that core-sync would overwrite (it is vendored from core@$CORE_REF); commit or discard them first, and put an instance value in instance/config.yaml or the change in orbit-core"
      RC=1; continue
    fi
    mkdir -p "$(dirname "$dst")"
    rm -rf "${dst:?}"
    cp -r "$TMP/core/$src" "$dst"
  fi
done < "$TMP/core/core-manifest.txt"

# A leftover dest that is a parent or child of a dest the new manifest still lists
# is not independent: deleting the parent would wipe a path just copied, and a child
# of a still-vendored directory is already covered by that directory's rm/cp or diff.
while IFS= read -r dst; do
  [ -n "$dst" ] || continue
  if grep -Fxq -- "$dst" "$TMP/new-dests"; then
    continue
  fi
  overlap=0
  while IFS= read -r n; do
    [ -n "$n" ] || continue
    case "$n" in
      "$dst"/*) overlap=1; break;;
    esac
    case "$dst" in
      "$n"/*) overlap=1; break;;
    esac
  done < "$TMP/new-dests"
  [ "$overlap" = 1 ] && continue
  path_ok "$dst" "destination" "$dst" || { RC=1; continue; }
  abs_dst=$(realpath -m -- "$INST_ROOT/$dst")
  beneath "$abs_dst" "$INST_ROOT" "destination" "$dst" || { RC=1; continue; }
  if [ "$MODE" = "--check" ]; then
    if [ -e "$dst" ]; then
      echo "DRIFT: $dst leftover (dropped from core@$CORE_REF)"
      RC=1
    fi
  else
    rm -rf "${dst:?}"
  fi
done < "$TMP/prev-dests"

if [ "$MODE" = "--check" ]; then
  [ "$RC" = 0 ] && echo "vendored core matches $CORE_REF" || echo "vendored core has drifted — edit orbit-core, not the vendor copy"
else
  echo "vendored core@$CORE_REF"
fi
exit "$RC"
