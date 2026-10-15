#!/usr/bin/env bash
# Switch one platform feature on the instance repo's main as the GitHub App.
#
#   scripts/features-commit.sh <feature> <on|off>
#
# In a fresh clone of main: rewrites the `features:` block of instance/config.yaml
# (scripts/verify/orbit_features.py, the one reading of that block), re-renders the tree
# the way an operator would (`scripts/instance-init.py`, then `scripts/catalog-sync.py`),
# and lands the result as ONE commit carrying the ledger trailers.
#
# WHOSE SCRIPTS RUN. Where this build carries its own copy of orbit-core (ORBIT_CORE_DIR:
# the orbitd and desk images do), the clone's vendored paths are first replaced by that
# copy, so the scaffolder and the generator that run are this build's and never a file the
# instance repository happens to carry under scripts/ (orbitd holds the GitHub App key; a
# commit must not be able to run code beside it). That only renders the right tree when
# the instance pins this build's core, so a clone pinned to another ref is refused.
# Without such a copy (a bare checkout of core) the clone's own files are used. A switch that changes
# nothing commits nothing: exit 0, COMMIT of the current main. A switch the config cannot
# take (an unknown feature, a feature turned on without its bucket, one turned off while
# another needs it) is refused with the reading's own sentence and nothing is pushed.
#
# Like ship.sh and monitor-commit.sh: every argument check runs before a token is minted,
# the token travels only in http.extraHeader, tracing is forced off, failure output is
# scrubbed, and a push rejected by a concurrent landing retries by recomputing the edit
# on the new main (fetch + reset, never merge or rebase), up to 5 attempts.
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"

# shellcheck disable=SC2329,SC2034 # trap-run; blanked here, read by the sourced helper
cleanup() {
  TOKEN=""
  AUTH_HEADER=""
  BASIC=""
  [ -n "${CLONE:-}" ] && rm -rf "$CLONE"
}
trap cleanup EXIT

fail() {
  echo "STEP $1 FAIL"
  if [ "$#" -ge 2 ]; then
    printf '%s\n' "$2"
  fi
  exit 1
}
ok() { echo "STEP $1 OK"; }

# shellcheck source-path=SCRIPTDIR
# shellcheck source=lib-app-clone.sh
. "$HERE/lib-app-clone.sh"

[ "$#" -eq 2 ] || { echo "usage: features-commit.sh <feature> <on|off>" >&2; fail args; }
FEATURE="$1"
STATE="$2"
[[ "$FEATURE" =~ ^[a-z][a-z0-9-]{0,39}$ ]] || fail args "the feature is not a feature name (the value is not repeated)"
case "$STATE" in
  on|off) ;;
  *) echo "usage: features-commit.sh <feature> <on|off>" >&2; fail args;;
esac
ok args

export ORBIT_INSTANCE_CONFIG="${ORBIT_INSTANCE_CONFIG:-$HERE/../instance/config.yaml}"
[ -f "$ORBIT_INSTANCE_CONFIG" ] || fail config
# shellcheck source-path=SCRIPTDIR
# shellcheck source=lib-instance.sh
. "$HERE/lib-instance.sh" || exit 2
[ -n "${ORBIT_REPO_URL:-}" ] || fail config
ok config

app_disable_tracing

app_mint_token

app_clone_repo

ACTOR="${ORBIT_ACTOR:-${USER:-operator}}"
ACTION="${ORBIT_ACTION:-features-set}"

git -C "$CLONE" config user.name "${GIT_AUTHOR_NAME:-$ACTOR}"
git -C "$CLONE" config user.email "${GIT_AUTHOR_EMAIL:-orbit-features@invalid}"

ATTEMPTS=5
i=1
while [ "$i" -le "$ATTEMPTS" ]; do
  git -C "$CLONE" fetch --quiet origin "${MAIN}" || fail edit "fetch failed"
  git -C "$CLONE" reset --hard --quiet "origin/${MAIN}" || fail edit "reset failed"
  git -C "$CLONE" clean -fdq || fail edit "clean failed"
  for path in instance instance/config.yaml provisioning provisioning/features.yaml scripts scripts/verify; do
    [ ! -L "$CLONE/$path" ] || fail edit "refusing a symlinked path in the instance repo: ${path}"
  done
  [ -f "$CLONE/instance/config.yaml" ] || fail edit "the instance repo has no instance/config.yaml"
  [ -f "$CLONE/provisioning/features.yaml" ] || fail edit "the instance repo has no provisioning/features.yaml: its core predates features (upgrade it first)"
  if [ -n "${ORBIT_CORE_DIR:-}" ] && [ -f "${ORBIT_CORE_DIR}/core-manifest.txt" ]; then
    PINNED="$(sed -n 's/^ref:[[:space:]]*//p' "$CLONE/orbit-core.lock" 2>/dev/null | head -n 1)"
    [[ "$PINNED" =~ ^[A-Za-z0-9._-]{1,64}$ ]] || fail edit "the instance repo's orbit-core.lock names no ref"
    [ "$PINNED" = "${ORBITD_VERSION:-}" ] || fail edit "the instance pins orbit-core ${PINNED} and this build is ${ORBITD_VERSION:-unversioned}: apply the platform first (orbit platform apply --app platform-config), then switch the feature"
    python3 - "$ORBIT_CORE_DIR" "$CLONE" <<'PY' || fail edit "this build's copy of orbit-core could not be laid over the clone"
import pathlib, shutil, sys
core, clone = pathlib.Path(sys.argv[1]), pathlib.Path(sys.argv[2])
for line in (core / "core-manifest.txt").read_text(encoding="utf-8").splitlines():
    line = line.strip()
    if not line or line.startswith("#"):
        continue
    src, _, dst = line.partition(" -> ")
    source, target = core / src.strip(), clone / (dst or src).strip()
    if not source.exists():
        continue
    if target.is_symlink() or target.is_file():
        target.unlink()
    elif target.is_dir():
        shutil.rmtree(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    if source.is_dir():
        shutil.copytree(source, target, ignore=shutil.ignore_patterns("__pycache__"))
    else:
        shutil.copy2(source, target)
PY
  fi
  # The edit, by the clone's own copy of the reading: the tree being edited decides what
  # its features are. The sentence a refusal prints is the reading's own.
  if ! EDIT_OUT="$(cd "$CLONE" && PYTHONPATH="$CLONE/scripts/verify" python3 - "$FEATURE" "$STATE" 2>&1 <<'PY'
import sys
import orbit_features as rules
feature, state = sys.argv[1], sys.argv[2]
path = "instance/config.yaml"
try:
    registry = rules.load_registry(".")
    text, want = rules.set_feature(open(path, encoding="utf-8").read(), registry, feature, state == "on")
except rules.FeatureError as e:
    sys.exit(str(e))
with open(path, "w", encoding="utf-8", newline="\n") as fh:
    fh.write(text)
print(" ".join("%s=%s" % (name, "on" if on else "off") for name, on in want.items()))
PY
  )"; then
    fail edit "$(scrub_text <<<"$EDIT_OUT" | tail -n 1)"
  fi
  # Re-render exactly as `make init` and `make sync` do. ORBIT_INSTANCE_CONFIG is unset
  # for them: they read the clone's own config, not the one this script was pointed at.
  if ! RENDER_OUT="$(cd "$CLONE" && env -u ORBIT_INSTANCE_CONFIG python3 scripts/instance-init.py 2>&1)"; then
    fail render "$(scrub_text <<<"$RENDER_OUT" | tail -n 3)"
  fi
  if ! RENDER_OUT="$(cd "$CLONE" && env -u ORBIT_INSTANCE_CONFIG python3 scripts/catalog-sync.py 2>&1)"; then
    fail render "$(scrub_text <<<"$RENDER_OUT" | tail -n 3)"
  fi
  git -C "$CLONE" add -A -- instance/config.yaml clusters
  if git -C "$CLONE" diff --cached --quiet; then
    SHA="$(git -C "$CLONE" rev-parse "origin/${MAIN}")"
    ok edit
    printf 'FEATURES %s\n' "$EDIT_OUT"
    printf 'COMMIT %s\n' "$SHA"
    exit 0
  fi
  git -C "$CLONE" commit -q -m "features: ${FEATURE} ${STATE}" \
    --trailer "Orbit-Action: ${ACTION}" \
    --trailer "Orbit-Actor: ${ACTOR}" \
    --trailer "Orbit-Role: admin" \
    --trailer "Orbit-Feature: ${FEATURE}=${STATE}" || fail edit "commit failed"
  if PUSH_OUT="$(git -C "$CLONE" push --quiet origin "HEAD:${MAIN}" 2>&1)"; then
    SHA="$(git -C "$CLONE" rev-parse HEAD)"
    ok edit
    printf 'FEATURES %s\n' "$EDIT_OUT"
    printf 'COMMIT %s\n' "$SHA"
    exit 0
  fi
  scrub_text <<<"$PUSH_OUT" >&2
  echo "push rejected (attempt ${i} of ${ATTEMPTS}) - recomputing on the new main" >&2
  i=$((i + 1))
done
fail edit "could not push after ${ATTEMPTS} attempts"
