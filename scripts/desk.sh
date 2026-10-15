#!/usr/bin/env bash
# Run a desk command inside the orbit-desk image, so the operator machine needs only
# Docker and git. From the instance repo root:
#
#   scripts/desk.sh make verify
#   scripts/desk.sh kubectl get pods -A
#   scripts/desk.sh                      # an interactive shell
#
# Mounted: this checkout at /instance, the credentials directory
# (~/.config/<name>-platform), ~/.kube, and ~/.config/orbit (the `orbit` client's
# stored login, so one `orbit login` serves every later desk command). The node ssh key named by
# <config dir>/ssh-key-path (or $ORBIT_SSH_KEY) is mounted read-only; the image's
# entrypoint copies it with safe permissions and opens the API tunnel.
#
# The image is ORBIT_DESK_IMAGE, default ghcr.io/yotampeled/orbit-desk:<desk tag in
# instance/config.yaml coreRegistry.deskTag, else "latest">.
set -euo pipefail
root="$(cd "$(dirname "$0")/.." && pwd)"
cfg="$root/instance/config.yaml"
[ -f "$cfg" ] || { echo "desk: no instance/config.yaml under $root" >&2; exit 2; }
name=$(sed -n '/^instance:/,/^[^ ]/s/^  name: *\([a-z0-9-]*\).*/\1/p' "$cfg" | head -1)
[ -n "$name" ] || { echo "desk: instance.name not found in instance/config.yaml" >&2; exit 2; }
desk_tag=$(sed -n 's/^  deskTag: *"\{0,1\}\([A-Za-z0-9._-]*\)"\{0,1\}.*/\1/p' "$cfg" | head -1)
image="${ORBIT_DESK_IMAGE:-ghcr.io/yotampeled/orbit-desk:${desk_tag:-latest}}"

confdir="${ORBIT_CONFIG_DIR:-$HOME/.config/$name-platform}"
mkdir -p "$confdir" "$HOME/.kube" "$HOME/.config/orbit"
key="${ORBIT_SSH_KEY:-}"
if [ -z "$key" ] && [ -s "$confdir/ssh-key-path" ]; then key=$(head -1 "$confdir/ssh-key-path"); fi
key="${key/#\~/$HOME}"

# Docker Desktop on Windows wants C:/... paths and Git Bash would rewrite /instance.
hostpath(){ if command -v cygpath >/dev/null 2>&1; then cygpath -m "$1"; else printf '%s' "$1"; fi; }
export MSYS_NO_PATHCONV=1

args=(run --rm -i)
[ -t 0 ] && [ -t 1 ] && args+=(-t)
args+=(-v "$(hostpath "$root"):/instance"
       -v "$(hostpath "$confdir"):/root/.config/$name-platform"
       -v "$(hostpath "$HOME/.kube"):/root/.kube"
       -v "$(hostpath "$HOME/.config/orbit"):/root/.config/orbit"
       -e "ORBIT_CONFIG_DIR=/root/.config/$name-platform"
       -e "ORBIT_HOST_CONFIG_DIR=$confdir"
       -e "KUBECONFIG=/root/.kube/config-$name-platform"
       -e "ORBIT_DESK_TUNNEL=${ORBIT_DESK_TUNNEL:-1}")
# The sops private key, when this workstation holds one (docs/ONBOARDING.md §2): the
# desk's `orbit secrets copy|set|verify` open sealed files with it. A path, never a value.
[ -s "$confdir/age.key" ] && args+=(-e "SOPS_AGE_KEY_FILE=/root/.config/$name-platform/age.key")
if [ -n "$key" ]; then
  [ -r "$key" ] || { echo "desk: the node ssh key named by ssh-key-path is not readable" >&2; exit 2; }
  args+=(-v "$(hostpath "$key"):/run/desk/node-key:ro")
fi
# GitHub access for git inside the desk (core-sync clones the private core): the token
# travels as an environment variable named on the command line, never as a value.
if [ -z "${GH_TOKEN:-}" ] && command -v gh >/dev/null 2>&1; then
  GH_TOKEN=$(gh auth token 2>/dev/null || true); export GH_TOKEN
fi
[ -n "${GH_TOKEN:-}" ] && args+=(-e GH_TOKEN)
# A port a desk command serves to this machine's browser (`orbit onboard run`'s GitHub App
# flow on 8765): published on loopback only, never on the network.
for p in ${ORBIT_DESK_PORTS:-}; do
  case "$p" in *[!0-9]*|"") echo "desk: ORBIT_DESK_PORTS holds a non-port ($p)" >&2; exit 2;; esac
  args+=(-p "127.0.0.1:$p:$p")
done
[ $# -eq 0 ] && set -- bash
exec docker "${args[@]}" "$image" "$@"
