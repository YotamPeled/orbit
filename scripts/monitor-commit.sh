#!/usr/bin/env bash
# Land a monitor file on the instance repo's main as the GitHub App.
#
#   MONITOR_FILE=<path> scripts/monitor-commit.sh upsert <name>
#   scripts/monitor-commit.sh delete <name>
#
# <name> is a DNS label. Upsert writes MONITOR_FILE's exact bytes to
# monitors/<name>.yaml (its top-level `name` must equal <name>); delete removes
# that path, and a missing file fails. Content identical to main commits
# nothing: exit 0, print COMMIT of the current main.
#
# Like ship.sh, every argument check runs before a token is minted, the token
# travels only in http.extraHeader (never argv), tracing is forced off, and
# failure output is scrubbed. A push rejected by a concurrent landing retries
# by recomputing the edit on the new main (fetch + reset, never merge or
# rebase), up to 5 attempts.
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

[ "$#" -eq 2 ] || { echo "usage: monitor-commit.sh <upsert|delete> <name>" >&2; fail args; }
OP="$1"
NAME="$2"
case "$OP" in
  upsert|delete) ;;
  *) echo "usage: monitor-commit.sh <upsert|delete> <name>" >&2; fail args;;
esac
[[ "$NAME" =~ ^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?$ ]] || fail args "monitor name is not a DNS label (the value is not repeated)"
if [ "$OP" = "upsert" ]; then
  [ -n "${MONITOR_FILE:-}" ] || fail args "MONITOR_FILE is not set"
  [ -f "$MONITOR_FILE" ] && [ -r "$MONITOR_FILE" ] || fail args "MONITOR_FILE is not readable"
  [ ! -L "$MONITOR_FILE" ] || fail args "MONITOR_FILE is not a regular file"
  BODY_NAME="$(python3 - "$MONITOR_FILE" <<'PY'
import sys, yaml
ALLOWED = {"name", "project", "env", "kind", "query", "target", "module",
           "condition", "severity", "notify", "summary", "runbook"}
try:
    doc = yaml.safe_load(open(sys.argv[1]))
except Exception:
    sys.exit("monitor file is not YAML")
if not isinstance(doc, dict):
    sys.exit("monitor file is not a monitor file")
if not set(doc.keys()) <= ALLOWED:
    sys.exit("monitor file has unexpected keys")
name = doc.get("name")
if not isinstance(name, str) or not name:
    sys.exit("monitor file has no top-level name")
print(name)
PY
)" || fail args "MONITOR_FILE is not a monitor file"
  # the body's name is file text: the row says it differs, never what it says
  [ "$BODY_NAME" = "$NAME" ] || fail args "monitor file's top-level name is not '${NAME}' (the file's value is not repeated)"
fi
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
ACTION="${ORBIT_ACTION:-monitor-${OP}}"
DEST="monitors/${NAME}.yaml"

git -C "$CLONE" config user.name "${GIT_AUTHOR_NAME:-$ACTOR}"
git -C "$CLONE" config user.email "${GIT_AUTHOR_EMAIL:-orbit-monitor@invalid}"

ATTEMPTS=5
i=1
while [ "$i" -le "$ATTEMPTS" ]; do
  git -C "$CLONE" fetch --quiet origin "${MAIN}" || fail "$OP" "fetch failed"
  git -C "$CLONE" reset --hard --quiet "origin/${MAIN}" || fail "$OP" "reset failed"
  if [ -L "$CLONE/monitors" ] || [ -L "$CLONE/$DEST" ]; then
    fail "$OP" "refusing symlinked monitor path"
  fi
  RESOLVED="$(realpath -m "$CLONE/$DEST")"
  MONITORS_DIR="$(realpath -m "$CLONE/monitors")"
  case "$RESOLVED" in
    "$MONITORS_DIR/"*) ;;
    *) fail "$OP" "refusing monitor path outside monitors" ;;
  esac
  if [ -e "$CLONE/$DEST" ] && [ ! -f "$CLONE/$DEST" ]; then
    fail "$OP" "refusing non-regular monitor file: ${DEST}"
  fi
  if [ -e "$CLONE/monitors" ] && [ ! -d "$CLONE/monitors" ]; then
    fail "$OP" "refusing non-directory monitors path"
  fi
  if [ "$OP" = "upsert" ]; then
    mkdir -p "$CLONE/monitors"
    cp "$MONITOR_FILE" "$CLONE/$DEST"
    git -C "$CLONE" add -- "$DEST"
    if git -C "$CLONE" diff --cached --quiet; then
      SHA="$(git -C "$CLONE" rev-parse "origin/${MAIN}")"
      ok "$OP"
      printf 'COMMIT %s\n' "$SHA"
      exit 0
    fi
  else
    [ -e "$CLONE/$DEST" ] || fail "$OP" "no such monitor: ${NAME}"
    git -C "$CLONE" rm -q -- "$DEST"
  fi
  git -C "$CLONE" commit -q -m "monitor ${OP} ${NAME}" \
    --trailer "Orbit-Action: ${ACTION}" \
    --trailer "Orbit-Actor: ${ACTOR}" \
    --trailer "Orbit-Role: operator" || fail "$OP" "commit failed"
  if PUSH_OUT="$(git -C "$CLONE" push --quiet origin "HEAD:${MAIN}" 2>&1)"; then
    SHA="$(git -C "$CLONE" rev-parse HEAD)"
    ok "$OP"
    printf 'COMMIT %s\n' "$SHA"
    exit 0
  fi
  scrub_text <<<"$PUSH_OUT" >&2
  echo "push rejected (attempt ${i} of ${ATTEMPTS}) - recomputing on the new main" >&2
  i=$((i + 1))
done
fail "$OP" "could not push after ${ATTEMPTS} attempts"
