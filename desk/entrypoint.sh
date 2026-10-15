#!/usr/bin/env bash
# orbit-desk entrypoint. Prepares the container the way a Linux workstation would be,
# then runs the command it was given (scripts/desk.sh passes it through).
#
#  1. The node ssh key arrives read-only at /run/desk/node-key (bind mounts from Windows
#     carry 0777, which ssh refuses); it is copied to ~/.ssh with 0600.
#  2. The instance checkout is mounted at /instance and owned by another uid: git is told
#     it is safe.
#  3. If the instance has a node and a kubeconfig exists, the ssh tunnel to the node's API
#     is opened on 127.0.0.1:6443 — the address the desk kubeconfig names. The API port
#     is firewalled from the internet (stage2 checks it stays that way), so the tunnel
#     is the only way kubectl reaches it. ORBIT_DESK_TUNNEL=0 skips it (pre-boot).
set -euo pipefail
mkdir -p "$HOME/.ssh" && chmod 700 "$HOME/.ssh"
if [ -r /run/desk/node-key ]; then
  install -m 600 /run/desk/node-key "$HOME/.ssh/node-key"
  export ORBIT_SSH_KEY="$HOME/.ssh/node-key"
fi
# known_hosts lives in the credentials directory, so the node's host key is trusted on
# first use once per workstation, not once per container.
if [ -n "${ORBIT_CONFIG_DIR:-}" ] && [ -d "$ORBIT_CONFIG_DIR" ]; then
  touch "$ORBIT_CONFIG_DIR/known_hosts"; ln -sf "$ORBIT_CONFIG_DIR/known_hosts" "$HOME/.ssh/known_hosts"
fi
git config --global --add safe.directory '*'
# GH_TOKEN (passed by name from the operator's `gh`) answers git's https prompts for
# github.com. The helper reads it from the environment at use time; it is never written
# to a file or a remote URL.
if [ -n "${GH_TOKEN:-}" ]; then
  git config --global credential.https://github.com.helper     '!f() { echo username=x-access-token; echo "password=$GH_TOKEN"; }; f'
fi

INSTANCE=${ORBIT_DESK_INSTANCE:-/instance}
if [ "${ORBIT_DESK_TUNNEL:-1}" = 1 ] && [ -f "$INSTANCE/instance/config.yaml" ] \
   && [ -n "${KUBECONFIG:-}" ] && [ -s "${KUBECONFIG%%:*}" ] && [ -n "${ORBIT_SSH_KEY:-}" ]; then
  ip=$(python3 - "$INSTANCE/instance/config.yaml" <<'PY'
import sys
import yaml
c = yaml.safe_load(open(sys.argv[1], encoding="utf-8")) or {}
nodes = c.get("nodes") or [{}]
print(nodes[0].get("ip", ""))
PY
)
  if [ -n "$ip" ]; then
    tunnel() {
      ssh -o BatchMode=yes -o StrictHostKeyChecking=accept-new -o ExitOnForwardFailure=yes \
          -o ConnectTimeout=15 \
          -o ServerAliveInterval=30 -i "$ORBIT_SSH_KEY" -fN -L 127.0.0.1:6443:127.0.0.1:6443 "root@$ip"
    }
    # Asked up to three times before it is a stop. One connection that the operator
    # machine's link dropped ("Connection refused" from a node whose sshd refused nobody,
    # seen from behind a desktop's NAT) ended the whole command before it began, and
    # whatever was running it read "no answer" as an answer (docs/V1.md §8, 2026-10-08).
    opened=0
    for try in 1 2 3; do
      if tunnel 2>/tmp/desk-tunnel.err; then opened=1; break; fi
      [ "$try" = 3 ] || sleep "${ORBIT_DESK_TUNNEL_RETRY_S:-5}"
    done
    if [ "$opened" != 1 ]; then
      cat /tmp/desk-tunnel.err >&2 2>/dev/null || true
      echo "desk: the ssh tunnel to $ip:6443 did not open (three tries)" >&2; exit 2
    fi
    # The tunnel is kept open for as long as the command runs. Measured (docs/V1.md §8,
    # 2026-10-06): one dropped ssh connection in the middle of `make verify` left every
    # later row asking a cluster that "did not answer", twelve minutes of red for a
    # platform that was healthy. A tunnel found closed is opened again and said so once.
    (
      while sleep "${ORBIT_DESK_TUNNEL_CHECK_S:-5}"; do
        if ! (exec 3<>/dev/tcp/127.0.0.1/6443) 2>/dev/null; then
          tunnel 2>/dev/null && echo "desk: the ssh tunnel to $ip:6443 had closed and was opened again" >&2 || true
        fi
      done
    ) &
  fi
fi
exec "$@"
