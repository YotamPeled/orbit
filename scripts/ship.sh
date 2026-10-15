#!/usr/bin/env bash
# Ship a catalog component's image tag as the GitHub App.
#
#   scripts/ship.sh <project> <env> <component> <tag>
#
# Reads the instance repo URL and registry from instance/config.yaml
# (ORBIT_INSTANCE_CONFIG). Checks the tag exists in the registry, mints a
# short-lived installation token from $ORBIT_CONFIG_DIR, shallow-clones the
# instance repo with the token as http.extraheader (never argv), and runs that
# clone's own scripts/bump-and-push.sh. Rollback is the same command with the
# previous tag. Argo CD still applies the resulting commit.
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

if [ "$#" -ne 4 ]; then
  echo "usage: ship.sh <project> <env> <component> <tag>" >&2
  fail args
fi
PROJECT="$1"
ENV_NAME="$2"
COMPONENT="$3"
TAG="$4"
if ! [[ "$TAG" =~ ^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$ ]]; then
  fail args
fi
# project and env are catalog ids, a component name is a DNS label (the chart truncates
# <project>-<component> to 63): held to those shapes here, before any of them reaches the
# catalog lookup or the bump's environment
for v in "$PROJECT" "$ENV_NAME"; do
  [[ "$v" =~ ^[a-z0-9]([a-z0-9-]{0,38}[a-z0-9])?$ ]] || fail args
done
[[ "$COMPONENT" =~ ^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?$ ]] || fail args
ok args

export ORBIT_INSTANCE_CONFIG="${ORBIT_INSTANCE_CONFIG:-$HERE/../instance/config.yaml}"
[ -f "$ORBIT_INSTANCE_CONFIG" ] || fail config
# shellcheck source-path=SCRIPTDIR
# shellcheck source=lib-instance.sh
. "$HERE/lib-instance.sh" || exit 2
[ -n "${ORBIT_REPO_URL:-}" ] && [ -n "${ORBIT_REGISTRY:-}" ] || fail config
ok config

INSTANCE_ROOT="$(cd "$(dirname "$ORBIT_INSTANCE_CONFIG")/.." && pwd)"
CATALOG="$INSTANCE_ROOT/catalog/projects.yaml"
[ -f "$CATALOG" ] || fail catalog

catalog_env="$(python3 - "$CATALOG" "$PROJECT" "$ENV_NAME" "$COMPONENT" <<'PY'
import re, sys, yaml
IMAGE_RE = re.compile(r"[a-z0-9][a-z0-9._/-]{0,255}")
BRANCH_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._/-]{0,200}")
REPO_RE = re.compile(r"[A-Za-z0-9_.-]{1,100}/[A-Za-z0-9_.-]{1,100}")
DEFAULT_BRANCH = {"prod": "main", "dev": "dev"}
catalog_path, project_id, env_name, component_name = sys.argv[1:5]
cat = yaml.safe_load(open(catalog_path))
project = next((p for p in (cat.get("projects") or []) if p.get("id") == project_id), None)
if project is None:
    sys.exit("unknown project")
component = next((c for c in (project.get("components") or [])
                  if c.get("name") == component_name), None)
if component is None:
    sys.exit("unknown component")
comp_envs = component.get("environments")
if comp_envs is not None and env_name not in comp_envs:
    sys.exit("component is not in that environment")
envcfg = (project.get("environments") or {}).get(env_name)
if envcfg is None:
    sys.exit("unknown environment")
envcfg = envcfg or {}
if env_name != "prod" and not envcfg.get("enabled"):
    sys.exit("environment is not enabled")
image = component.get("image") or ""
branch = envcfg.get("branch", DEFAULT_BRANCH.get(env_name, env_name))
repo = (component.get("repo") or project.get("repo") or "")
if not IMAGE_RE.fullmatch(image):
    sys.exit("refusing image")
if not BRANCH_RE.fullmatch(branch):
    sys.exit("refusing branch")
if not REPO_RE.fullmatch(repo):
    sys.exit("refusing repo")
print(f"export IMAGE={image}")
print(f"export BRANCH={branch}")
print(f"export REPO={repo}")
PY
)" || fail catalog
eval "$catalog_env"
[ -n "${IMAGE:-}" ] && [ -n "${BRANCH:-}" ] && [ -n "${REPO:-}" ] || fail catalog
export IMAGE BRANCH REPO TAG
ok catalog

if DIGEST="$(python3 "$HERE/github-app-token.py" --digest "$IMAGE" "$TAG")"; then
  ok registry
else
  fail registry
fi

# Token minting, cloning and output scrubbing live in the shared helper so
# monitor-commit.sh lands monitors the same way this script lands tags.
# shellcheck source-path=SCRIPTDIR
# shellcheck source=lib-app-clone.sh
. "$HERE/lib-app-clone.sh"

app_disable_tracing

app_mint_token

app_clone_repo

SOURCE_COMMIT="$TAG"
if [[ "$TAG" =~ -([0-9a-fA-F]{7,64})$ ]]; then
  SOURCE_COMMIT="${BASH_REMATCH[1]}"
fi
ACTOR="${ORBIT_ACTOR:-${USER:-operator}}"
ACTION="${ORBIT_ACTION:-ship}"
GIT_TRAILERS="$(printf '%s\n' \
  "Orbit-Action: ${ACTION}" \
  "Orbit-Actor: ${ACTOR}" \
  "Orbit-Role: operator" \
  "Source-Commit: ${SOURCE_COMMIT}" \
  "Image-Digest: ${DIGEST}" \
  "Render-Sha: n/a")"

# Whose bump script runs. With a core of its own (ORBIT_CORE_DIR: the orbitd and desk
# images) this build's copy runs, on the clone, and refuses a clone whose vendored core is
# not this build's: orbitd holds the App key and must never run a file a commit brought.
# A clone pinned to another core is refused first, with what to do. Without such a copy
# (a bare checkout of core) the clone's own script runs, as before.
BUMP="$CLONE/scripts/bump-and-push.sh"
BUMP_CLONE=""
if [ -n "${ORBIT_CORE_DIR:-}" ] && [ -f "${ORBIT_CORE_DIR}/core-manifest.txt" ]; then
  PINNED="$(sed -n 's/^ref:[[:space:]]*//p' "$CLONE/orbit-core.lock" 2>/dev/null | head -n 1)"
  [[ "$PINNED" =~ ^[A-Za-z0-9._-]{1,64}$ ]] || fail bump "bump FAIL rc=1: the instance repo's orbit-core.lock names no ref"
  [ "$PINNED" = "${ORBITD_VERSION:-}" ] || fail bump "bump FAIL rc=1: the instance pins orbit-core ${PINNED} and this build is ${ORBITD_VERSION:-unversioned}: apply the platform first (orbit platform apply --app platform-config), then ship"
  BUMP="$ORBIT_CORE_DIR/scripts/bump-and-push.sh"
  BUMP_CLONE="$CLONE"
fi

set +e
BUMP_OUT="$(
  ORBIT_CLONE="$BUMP_CLONE" \
  GIT_TRAILERS="$GIT_TRAILERS" \
  IMAGE="$IMAGE" TAG="$TAG" BRANCH="$BRANCH" REPO="$REPO" \
  ORBIT_SHIP_PROJECT="$PROJECT" ORBIT_SHIP_ENV="$ENV_NAME" ORBIT_SHIP_COMPONENT="$COMPONENT" \
  GIT_AUTHOR_NAME="${GIT_AUTHOR_NAME:-$ACTOR}" \
  GIT_AUTHOR_EMAIL="${GIT_AUTHOR_EMAIL:-orbit-ship@invalid}" \
  bash "$BUMP" 2>&1
)"
BUMP_RC=$?
set -e
# Scrub the raw token, the Basic blob, and the full Authorization line.
SCRUBBED="$(scrub_text <<<"$BUMP_OUT")"
if [ "$BUMP_RC" -ne 0 ]; then
  printf '%s\n' "$SCRUBBED" >&2
  last="$(printf '%s' "$SCRUBBED" | tail -n1)"
  fail bump "bump FAIL rc=${BUMP_RC}: ${last}"
fi
# The child's own account (which keys moved, or that the tag was already current) is the
# operator's evidence too: it goes to stderr, scrubbed, so stdout stays the STEP/COMMIT contract.
printf '%s\n' "$SCRUBBED" | grep -v -E '^COMMIT [0-9a-fA-F]{7,64}$' >&2 || true
ok bump
# Landed SHA is the clone HEAD. A COMMIT line from the bump script is
# accepted if present but is not required.
SHA="$(git -C "$CLONE" rev-parse HEAD)" || fail bump
COMMIT_LINE="$(printf '%s\n' "$BUMP_OUT" | grep -E '^COMMIT [0-9a-fA-F]{7,64}$' | tail -n1 || true)"
if [ -z "$COMMIT_LINE" ]; then
  COMMIT_LINE="COMMIT ${SHA}"
fi
printf '%s\n' "$COMMIT_LINE"
