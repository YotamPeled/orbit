#!/usr/bin/env bash
# Replace or remove one project's catalog entry on the instance repo's main as the GitHub App.
#
#   CATALOG_ENTRY_FILE=<path> scripts/catalog-commit.sh set <project>
#   CATALOG_ENTRY_FILE=<path> scripts/catalog-commit.sh check <project>
#   scripts/catalog-commit.sh remove <project>
#
# In a fresh clone of main: splices the one entry in catalog/projects.yaml
# (scripts/catalog-edit.py: every other line of the file stays as it was, and the edit
# is refused unless the file then parses to exactly the old catalog with that one entry
# changed), regenerates clusters/ the way `make sync` does (scripts/catalog-sync.py),
# and lands the result as ONE commit carrying the ledger trailers. `set` reads the new
# entry (YAML or JSON) from CATALOG_ENTRY_FILE.
#
# The generator is the judge: an entry it refuses (an unknown field, a database with
# the databases feature off, a host outside the instance's domains) fails the `render`
# step with the generator's own sentence and nothing is pushed. orbitd's commit gate
# then runs its whole chain on the landed commit before anything is applied.
# `remove` also deletes, in the same commit, the project's sealed secret files and every
# monitors/<name>.yaml whose `project` it is (each printed as `MONITOR-REMOVED <name>`).
#
# THE GATE'S CHECKS, BEFORE THE PUSH. With ORBIT_CATALOG_PRECHECK=1 a `set` also runs
# the commit gate's own chain (`orbit check all`) on the edited clone and pushes only
# what passes: a failing check fails the `check` step with the check's sentence and
# nothing is pushed. That is for a caller at the desk. orbitd does not set it: it asks
# the same chain the same question in its own process before it runs this script
# (orbitd/tools_operator/catalog.py, precheck_entry), because its memory limit has no
# room for a second interpreter. Without it, what the generator accepts but the gate
# refuses (more memory than the node has, two components on one path, a secret nobody
# declared) landed on main first and was refused there, and a refused main blocks every
# deploy of every project until another commit passes (QA of v1.0.0, F26).
# `check` is `set` without the commit: the edit, the generator and the gate's chain run
# on the clone, it prints `CHECKED <project>` (or `UNCHANGED <project>`), pushes nothing.
#
# WHOSE SCRIPTS RUN. As scripts/features-commit.sh: where this build carries its own
# copy of orbit-core (ORBIT_CORE_DIR), the clone's vendored paths are first replaced by
# that copy, so the editor and the generator that run are this build's, never a file
# the instance repository carries; a clone pinned to another core is refused.
#
# An edit that changes nothing commits nothing: exit 0, COMMIT of the current main.
# Like ship.sh, monitor-commit.sh and features-commit.sh: every argument check runs
# before a token is minted, the token travels only in http.extraHeader, tracing is
# forced off, failure output is scrubbed, and a push rejected by a concurrent landing
# retries by recomputing the edit on the new main, up to 5 attempts.
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

[ "$#" -eq 2 ] || { echo "usage: catalog-commit.sh set|check|remove <project>" >&2; fail args; }
VERB="$1"
PROJECT="$2"
[[ "$PROJECT" =~ ^[a-z][a-z0-9-]{0,62}$ ]] || fail args "the project is not a project id (the value is not repeated)"
case "$VERB" in
  set|check)
    [ -n "${CATALOG_ENTRY_FILE:-}" ] || fail args "CATALOG_ENTRY_FILE is not set"
    [ -f "$CATALOG_ENTRY_FILE" ] && [ -r "$CATALOG_ENTRY_FILE" ] || fail args "CATALOG_ENTRY_FILE is not readable"
    [ ! -L "$CATALOG_ENTRY_FILE" ] || fail args "CATALOG_ENTRY_FILE is not a regular file"
    ;;
  remove) ;;
  *) echo "usage: catalog-commit.sh set|check|remove <project>" >&2; fail args;;
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
ACTION="${ORBIT_ACTION:-catalog-$VERB}"
ROLE="${ORBIT_ROLE:-operator}"
[[ "$ROLE" =~ ^[a-z]{1,20}$ ]] || fail args "the role is not a role name"

git -C "$CLONE" config user.name "${GIT_AUTHOR_NAME:-$ACTOR}"
git -C "$CLONE" config user.email "${GIT_AUTHOR_EMAIL:-orbit-catalog@invalid}"

ATTEMPTS=5
i=1
while [ "$i" -le "$ATTEMPTS" ]; do
  git -C "$CLONE" fetch --quiet origin "${MAIN}" || fail edit "fetch failed"
  git -C "$CLONE" reset --hard --quiet "origin/${MAIN}" || fail edit "reset failed"
  git -C "$CLONE" clean -fdq || fail edit "clean failed"
  for path in catalog catalog/projects.yaml catalog/tags.yaml clusters clusters/apps scripts scripts/verify; do
    [ ! -L "$CLONE/$path" ] || fail edit "refusing a symlinked path in the instance repo: ${path}"
  done
  [ -f "$CLONE/catalog/projects.yaml" ] || fail edit "the instance repo has no catalog/projects.yaml"
  if [ -n "${ORBIT_CORE_DIR:-}" ] && [ -f "${ORBIT_CORE_DIR}/core-manifest.txt" ]; then
    PINNED="$(sed -n 's/^ref:[[:space:]]*//p' "$CLONE/orbit-core.lock" 2>/dev/null | head -n 1)"
    [[ "$PINNED" =~ ^[A-Za-z0-9._-]{1,64}$ ]] || fail edit "the instance repo's orbit-core.lock names no ref"
    [ "$PINNED" = "${ORBITD_VERSION:-}" ] || fail edit "the instance pins orbit-core ${PINNED} and this build is ${ORBITD_VERSION:-unversioned}: apply the platform first (orbit platform apply --app platform-config), then change the catalog"
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
  [ -f "$CLONE/scripts/catalog-edit.py" ] || fail edit "the instance repo's core has no scripts/catalog-edit.py: it predates catalog edits through orbitd (upgrade it first)"
  # The edit. Its refusal is one sentence about the catalog; it never prints a value.
  if [ "$VERB" = set ] || [ "$VERB" = check ]; then
    EDIT_OUT="$(cd "$CLONE" && python3 scripts/catalog-edit.py set "$PROJECT" < "$CATALOG_ENTRY_FILE" 2>&1)" \
      || fail edit "$(scrub_text <<<"$EDIT_OUT" | tail -n 1)"
  else
    EDIT_OUT="$(cd "$CLONE" && python3 scripts/catalog-edit.py remove "$PROJECT" 2>&1)" \
      || fail edit "$(scrub_text <<<"$EDIT_OUT" | tail -n 1)"
  fi
  # Regenerate exactly as `make sync` does. ORBIT_INSTANCE_CONFIG is unset for it: the
  # generator reads the clone's own config, not the one this script was pointed at.
  if ! RENDER_OUT="$(cd "$CLONE" && env -u ORBIT_INSTANCE_CONFIG python3 scripts/catalog-sync.py 2>&1)"; then
    fail render "$(scrub_text <<<"$RENDER_OUT" | tail -n 3)"
  fi
  if [ "$VERB" = remove ]; then
    # The project's sealed Secrets go with it. They are files nobody generates (a person
    # or an agent sealed them into clusters/platform/secrets/<namespace>/), so the
    # generator leaves them, and `make verify` then asked the cluster for Secrets of
    # namespaces that were gone (docs/V1.md §8). The namespaces are exactly the pairs
    # whose generated Application this removal just deleted: no name is guessed.
    while IFS= read -r gone; do
      pair="${gone#clusters/apps/}"; pair="${pair%.yaml}"
      case "$pair" in "${PROJECT}"-*) ;; *) continue;; esac
      [[ "$pair" =~ ^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?$ ]] || continue
      sealed="clusters/platform/secrets/${pair}"
      [ ! -L "$CLONE/$sealed" ] || fail edit "refusing a symlinked path in the instance repo: ${sealed}"
      [ -d "$CLONE/$sealed" ] && rm -rf "${CLONE:?}/${sealed}"
    done < <(git -C "$CLONE" ls-files --deleted -- clusters/apps)
    # The project's monitors go with it too. A monitor file names its project; left in
    # the repository after the project is gone it is a file the monitors unit refuses
    # for ever, and its rule and probe stayed in the cluster: two minutes after a
    # removal the removed project's uptime monitor paged the chat (QA of v1.0.0, F40).
    # orbitd deletes the objects (the `monitors` step of orbit_project_remove).
    [ ! -L "$CLONE/monitors" ] || fail edit "refusing a symlinked path in the instance repo: monitors"
    if [ -d "$CLONE/monitors" ]; then
      GONE_MONITORS="$(python3 - "$CLONE/monitors" "$PROJECT" <<'PY'
import pathlib, sys, yaml
directory, project = pathlib.Path(sys.argv[1]), sys.argv[2]
for path in sorted(directory.glob("*.yaml")):
    if path.is_symlink() or not path.is_file():
        continue
    try:
        body = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, yaml.YAMLError):
        continue
    if isinstance(body, dict) and body.get("project") == project:
        path.unlink()
        print(path.stem)
PY
)" || fail edit "the project's monitor files could not be read"
      while IFS= read -r gone; do
        [ -n "$gone" ] && printf 'MONITOR-REMOVED %s\n' "$gone"
      done <<<"$GONE_MONITORS"
    fi
  fi
  git -C "$CLONE" add -A -- catalog clusters
  [ "$VERB" = remove ] && [ -d "$CLONE/monitors" ] && git -C "$CLONE" add -A -- monitors
  if git -C "$CLONE" diff --cached --quiet; then
    SHA="$(git -C "$CLONE" rev-parse "origin/${MAIN}")"
    ok edit
    printf 'UNCHANGED %s\n' "$PROJECT"
    printf 'COMMIT %s\n' "$SHA"
    exit 0
  fi
  if [ "$VERB" = check ] || { [ "$VERB" = set ] && [ "${ORBIT_CATALOG_PRECHECK:-}" = 1 ]; }; then
    # The commit gate's chain on the tree that would be pushed. Its last line on stderr
    # is `orbit check: <name>: <sentence>`; the checks print no value of a secret.
    command -v "${ORBIT_CHECK_BIN:-orbit}" >/dev/null 2>&1 \
      || fail check "the gate's checks cannot run here (no orbit command): nothing was pushed"
    if ! CHECK_OUT="$(env -u ORBIT_INSTANCE_CONFIG "${ORBIT_CHECK_BIN:-orbit}" check all "$CLONE" 2>&1 >/dev/null)"; then
      CHECK_LINE="$(scrub_text <<<"$CHECK_OUT" | grep . | tail -n 1)"
      fail check "${CHECK_LINE#orbit check: }"
    fi
    ok check
  fi
  if [ "$VERB" = check ]; then
    printf 'CHECKED %s\n' "$PROJECT"
    exit 0
  fi
  git -C "$CLONE" commit -q -m "catalog: ${VERB} ${PROJECT}" \
    --trailer "Orbit-Action: ${ACTION}" \
    --trailer "Orbit-Actor: ${ACTOR}" \
    --trailer "Orbit-Role: ${ROLE}" \
    --trailer "Orbit-Project: ${PROJECT}" || fail edit "commit failed"
  if PUSH_OUT="$(git -C "$CLONE" push --quiet origin "HEAD:${MAIN}" 2>&1)"; then
    SHA="$(git -C "$CLONE" rev-parse HEAD)"
    ok edit
    printf 'COMMIT %s\n' "$SHA"
    exit 0
  fi
  scrub_text <<<"$PUSH_OUT" >&2
  echo "push rejected (attempt ${i} of ${ATTEMPTS}) - recomputing on the new main" >&2
  i=$((i + 1))
done
fail edit "could not push after ${ATTEMPTS} attempts"
