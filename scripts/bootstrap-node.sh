#!/usr/bin/env bash
#
# Bootstraps the node into a working k3s cluster and pulls a kubeconfig back.
#
# Why this is a script and not cloud-init: the server was adopted, not created by
# OpenTofu, so cloud-init already ran and will never run again. Anything that has
# to happen *on* the node has to be re-runnable against a live box. So this is
# idempotent — run it as many times as you like.
#
# Everything above the node (Argo CD, Traefik, cert-manager, the apps) is GitOps
# and lives in clusters/. This script's job ends at "kubectl works".
#
# Usage:  scripts/bootstrap-node.sh
set -euo pipefail

. "$(dirname "$0")/lib-instance.sh" || exit 2

HOST="${HOST:-$ORBIT_NODE_IP}"
SSH_KEY="${SSH_KEY:-$ORBIT_SSH_KEY}"
KUBECONFIG_OUT="${KUBECONFIG_OUT:-$HOME/.kube/config-${ORBIT_INSTANCE_NAME}-platform}"
TLS_SANS="${TLS_SANS:-$HOST,k8s.$ORBIT_DOMAIN}"

# accept-new, not strict: this script's first contact is with a server created
# seconds ago whose host key CANNOT be known yet — BatchMode+strict can never
# succeed on a fresh org (found by the re-acceptance run). Changed keys on a KNOWN
# host still refuse, which is the half of the protection that matters.
ssh_cmd() { ssh -o BatchMode=yes -o StrictHostKeyChecking=accept-new -o ConnectTimeout=15 -i "$SSH_KEY" "root@$HOST" "$@"; }

echo "==> [1/4] node prerequisites"
ssh_cmd 'bash -eu -s' <<'REMOTE'
# kubelet refuses to start with swap enabled unless explicitly told otherwise.
# Commenting fstab is not enough on a node that also has zram-generator: that
# device is created at boot from [zramN] and is not an fstab line. Persist both.
SWAP_NOW=$(swapon --show --noheadings || true)
if [ -n "$SWAP_NOW" ]; then
  swapoff -a
  echo "    swap disabled"
else
  echo "    swap already off"
fi
# Keep the first backup of the original fstab. sed -i.bak on every run would
# replace it with the already-commented file.
if [ ! -e /etc/fstab.bak ]; then
  cp -a /etc/fstab /etc/fstab.bak
fi
sed -i -E 's@^([^#].*\sswap\s)@#\1@' /etc/fstab
# zram-generator reads {/etc,/run,/usr/local/lib,/usr/lib}/systemd/zram-generator.conf
# and those same paths' conf.d directories. A vendor drop-in under /usr/lib is the
# usual layout; /etc/systemd/zram-generator.conf often does not exist. Never edit
# the vendor files. Mask the generated unit (wins over a generator rebuild) and
# drop an /etc override that zeros zram0 so the generator itself creates nothing.
need_zram_persist=0
case "$SWAP_NOW" in *zram*) need_zram_persist=1;; esac
for f in \
  /etc/systemd/zram-generator.conf \
  /run/systemd/zram-generator.conf \
  /usr/local/lib/systemd/zram-generator.conf \
  /usr/lib/systemd/zram-generator.conf \
  /etc/systemd/zram-generator.conf.d/*.conf \
  /run/systemd/zram-generator.conf.d/*.conf \
  /usr/local/lib/systemd/zram-generator.conf.d/*.conf \
  /usr/lib/systemd/zram-generator.conf.d/*.conf
do
  [ -f "$f" ] || continue
  need_zram_persist=1
  break
done
if [ "$need_zram_persist" = 1 ]; then
  if [ -f /etc/systemd/zram-generator.conf ]; then
    awk 'BEGIN{s=0} /^\[/{s=($0 ~ /^\[zram0\]/)} !s' /etc/systemd/zram-generator.conf \
      > /etc/systemd/zram-generator.conf.orbit-new
    mv /etc/systemd/zram-generator.conf.orbit-new /etc/systemd/zram-generator.conf
  fi
  mkdir -p /etc/systemd/zram-generator.conf.d
  cat > /etc/systemd/zram-generator.conf.d/99-orbit-zram-off.conf <<'ZRAM'
[zram0]
zram-size = 0
ZRAM
  systemctl mask --now dev-zram0.swap >/dev/null 2>&1 || true
fi

# Which tool the node has, not which string /etc/os-release carries.
if command -v pacman >/dev/null 2>&1; then
  # -S, not -Sy: refreshing the DB without upgrading the rest of the system is a
  # partial upgrade and unsupported. -Syu would also be consistent, but a
  # bootstrap should not upgrade the world; the local DB already matches it.
  pacman -S --noconfirm --needed curl ca-certificates open-iscsi nfs-utils iptables >/dev/null
elif command -v apt-get >/dev/null 2>&1; then
  export DEBIAN_FRONTEND=noninteractive
  apt-get update -qq
  apt-get install -y -qq curl ca-certificates apparmor apparmor-utils open-iscsi nfs-common iptables >/dev/null
else
  echo "    FATAL: no package tool on this node (need pacman or apt-get)" >&2
  exit 1
fi
systemctl enable --now iscsid >/dev/null 2>&1 || true
echo "    packages ok"

# inotify instance headroom for the container runtime and the log shipper.
#
# fs.inotify.max_user_instances caps how many inotify instances a single UID may hold.
# The 128 default dates from 2005 and was sized for a desktop login session. On this
# node every file-watching process the platform runs — the kubelet, every containerd
# shim, Alloy's log tailer — is uid 0, so they all draw on ONE 128-instance budget.
# Measured demand at uid 0 was ~204 against the cap of 128 (2026-08-31).
#
# Over the cap inotify_init1() returns EMFILE, which prints as "too many open files".
# That reads like an FD-rlimit problem and gets chased with ulimit, which is why this
# hid for so long. What it actually caused: Alloy could not create an fsnotify watcher,
# fell back to reopening every log stream on a ~10s loop, and injected "failed to
# create fsnotify watcher: too many open files" into every tenant's log stream —
# ~215k junk lines/day, plus a 10s collection gap on every stream. 27 of the node's 54
# containerd shims were sitting at zero inotify instances for the same reason.
#
# 512 and not more: it clears measured demand with ~2.5x headroom while staying well
# below the 8192 mainstream distributions ship, so this moves toward the ecosystem
# norm rather than past it. An instance costs a small, bounded amount of unswappable
# kernel memory — the cap exists to bound a runaway process, not to size a server, and
# 512 still bounds one.
#
# fs.inotify.max_user_watches is deliberately NOT touched. It is a different resource
# (individual watched paths, not instances), it already sits around 126k here, and
# nothing measured comes near it. No evidence, no change.
cat > /etc/sysctl.d/90-orbit-inotify.conf <<'SYSCTL'
# Managed by scripts/bootstrap-node.sh — see that script for the full reasoning.
# Instances only. max_user_watches is deliberately left at the kernel default.
fs.inotify.max_user_instances = 512
SYSCTL
chmod 644 /etc/sysctl.d/90-orbit-inotify.conf

# `--system`, not `-p <file>`. Applying the one file would prove only that the value is
# settable; replaying the whole /etc/sysctl.d merge in boot order proves that OUR file
# is the one that wins, so the assertion below is a real test of what the node comes up
# with after a reboot rather than of this second.
sysctl -q --system

# Fail loudly if it did not stick. A bootstrap that reports success on a kernel param
# it did not actually change is worse than no bootstrap: the silent-EMFILE failure mode
# above is precisely what a false green here would hand back.
GOT=$(sysctl -n fs.inotify.max_user_instances)
if [ "$GOT" != "512" ]; then
  echo "    FATAL: fs.inotify.max_user_instances is $GOT, expected 512" >&2
  echo "    something later in the sysctl.d merge order overrides it:" >&2
  grep -rl max_user_instances /etc/sysctl.conf /etc/sysctl.d/ 2>/dev/null >&2
  exit 1
fi
echo "    inotify instances: $GOT (persisted, wins the sysctl.d merge)"

# ssh takes keys only.
#
# A server the provider installs with none of its own ssh keys attached (every rebuild is
# one) comes up with a root password set and `PasswordAuthentication yes`: read on the
# rebuilt test server, 2026-10-08, hours after onboarding called it done. Nothing of
# Orbit's signs in with a password, so the node takes none: a password nobody chose is
# not left as a door on port 22.
#
# 00-, so it is read before the provider's own drop-in (sshd keeps the FIRST value it
# meets, and cloud-init writes 50-cloud-init.conf). Checked with `sshd -t` before the
# reload, and asserted from sshd's own effective configuration after it: a bootstrap that
# says "keys only" on a node that still takes passwords would be worse than saying nothing.
mkdir -p /etc/ssh/sshd_config.d
cat > /etc/ssh/sshd_config.d/00-orbit.conf <<'SSHD'
# Managed by scripts/bootstrap-node.sh: ssh takes keys only.
PasswordAuthentication no
KbdInteractiveAuthentication no
SSHD
chmod 644 /etc/ssh/sshd_config.d/00-orbit.conf
sshd -t
systemctl reload ssh 2>/dev/null || systemctl reload sshd 2>/dev/null || true
PWAUTH=$(sshd -T 2>/dev/null | awk '$1 == "passwordauthentication" {print $2}')
if [ "$PWAUTH" != "no" ]; then
  echo "    FATAL: sshd still takes passwords (passwordauthentication ${PWAUTH:-unreadable})" >&2
  echo "    /etc/ssh/sshd_config must Include /etc/ssh/sshd_config.d/*.conf before any other setting" >&2
  exit 1
fi
echo "    ssh: keys only (password sign-in off)"

# Block pods from the cloud metadata service.
#
# This has to live on the node. A Kubernetes NetworkPolicy with
# `ipBlock: 0.0.0.0/0 except 169.254.0.0/16` is accepted and shows up correctly in the
# API, but link-local traffic is routed by the host before it reaches the policy chain,
# so it does not actually block — verified empirically, the metadata service answered 200
# from inside a namespace carrying exactly that policy.
#
# Hetzner's metadata endpoint serves the server's build-time root password, which is a
# valid credential for the web console. Any workload that can read it can log into the
# machine out-of-band.
#
# FORWARD alone is not enough. It covers the normal case — a pod in 10.42.0.0/16 routed
# through the host — but a hostNetwork pod shares the host's stack and its traffic goes out
# via OUTPUT, never touching FORWARD. Platform namespaces (argocd, traefik, kube-system)
# carry no PodSecurity enforcement, and Argo can create a workload in them from a commit,
# so hostNetwork is reachable. Both chains get the rule.
#
# It is also re-asserted on a timer rather than inserted once: the rule goes in at position
# 1, but kube-router rewrites the FORWARD chain on every k3s restart and the rule drifts
# down. It survives today only because kube-router's ACCEPT sits below it — which is luck,
# not design. The timer makes the invariant continuously true instead of true at boot.
#
# The OUTPUT rule costs the host its own metadata access. Nothing here wants it: cloud-init
# ran once at build time and never runs again. The one future consumer is the Hetzner CCM,
# which arrives only if a second node does — at which point this rule needs a source
# exception for it, not removal.
cat > /etc/systemd/system/orbit-block-metadata.service <<'UNIT'
[Unit]
Description=Block the pod network from the cloud metadata service
After=k3s.service
[Service]
Type=oneshot
# NOT RemainAfterExit=yes. A latched oneshot is permanently "active", so every later
# start — including the timer's — is discarded as a no-op, and OnUnitActiveSec is measured
# from an activation that never advances. The timer fired exactly once, then reported
# NextElapseUSecMonotonic=infinity, and the rule it exists to re-assert had already drifted
# off position 1. The script is idempotent, so there is nothing worth latching.
ExecStart=/usr/local/sbin/orbit-block-metadata
[Install]
WantedBy=multi-user.target
UNIT

cat > /usr/local/sbin/orbit-block-metadata <<'HOOK'
#!/bin/sh
# The rule lives in the RAW table, not filter.
#
# raw/PREROUTING is the first hook a packet meets (before conntrack) and raw/OUTPUT the
# first for locally-generated traffic. kube-router only ever writes to filter and nat, so
# nothing reorders these and there is no position to defend. The earlier filter/FORWARD
# version had to sit at position 1 and kept losing that spot on every kube-router resync —
# chasing it with a timer was racing a problem that did not need to exist.
#
# raw/PREROUTING also catches hostNetwork pods, which never traverse FORWARD at all.
set -u
MD=169.254.169.254
IPT="iptables -w 5"
for chain in PREROUTING OUTPUT; do
  $IPT -t raw -C $chain -d $MD -j DROP 2>/dev/null || $IPT -t raw -I $chain 1 -d $MD -j DROP
done
# The v6 metadata address, if the stack is present at all.
if [ -e /proc/net/if_inet6 ]; then
  for chain in PREROUTING OUTPUT; do
    ip6tables -w 5 -t raw -C $chain -d fe80::a9fe:a9fe -j DROP 2>/dev/null \
      || ip6tables -w 5 -t raw -I $chain 1 -d fe80::a9fe:a9fe -j DROP 2>/dev/null || true
  done
fi
# Close the API on every default-route interface, v4 and v6. A node on both
# ethernet and wifi has two defaults (different metrics); closing only the
# first leaves 6443 open on the second. The API listens dual-stack, so v4-only
# would leave [v6]:6443 reachable on a SLAAC LAN. Loopback and the pod network
# are other interfaces, so they keep reaching the API. Re-asserted by the same
# timer as the metadata rules; -C before -I so a second run is a no-op.
IFACES=$( {
  ip -4 route show default 2>/dev/null
  ip -6 route show default 2>/dev/null
} | awk '{for (i=1; i<NF; i++) if ($i=="dev" && $(i+1)!="" && $(i+1)!="lo") print $(i+1)}' | sort -u )
for IFACE in $IFACES; do
  $IPT -C INPUT -i "$IFACE" -p tcp --dport 6443 -j DROP 2>/dev/null \
    || $IPT -I INPUT 1 -i "$IFACE" -p tcp --dport 6443 -j DROP
  if [ -e /proc/net/if_inet6 ]; then
    ip6tables -w 5 -C INPUT -i "$IFACE" -p tcp --dport 6443 -j DROP 2>/dev/null \
      || ip6tables -w 5 -I INPUT 1 -i "$IFACE" -p tcp --dport 6443 -j DROP
  fi
done
HOOK
chmod 700 /usr/local/sbin/orbit-block-metadata

cat > /etc/systemd/system/orbit-block-metadata.timer <<'UNIT'
[Unit]
Description=Re-assert the metadata block after kube-router rewrites the chains
[Timer]
# OnCalendar, not OnBootSec/OnUnitActiveSec. Those are relative to events that a oneshot
# either never advances (activation) or that happened once a day ago (boot), which is how
# the first version reported NextElapseUSecMonotonic=infinity and ran exactly once. A wall
# clock schedule has no such dependency and is trivially assertable.
OnCalendar=*:*:00
AccuracySec=5s
Persistent=true
[Install]
WantedBy=timers.target
UNIT

systemctl daemon-reload
systemctl enable orbit-block-metadata.service >/dev/null 2>&1
# stop before start: daemon-reload does not unlatch a unit that is already active, so a
# unit left over from an older RemainAfterExit=yes definition stays "active" forever and
# swallows every subsequent start.
systemctl stop orbit-block-metadata.service >/dev/null 2>&1
# stderr kept: a unit that fails to start is the reason the assertion below fails
systemctl start orbit-block-metadata.service >/dev/null
systemctl enable --now orbit-block-metadata.timer >/dev/null 2>&1
# Prove the timer will actually fire again. A latched unit reports
# NextElapseUSecMonotonic=infinity, which is how the previous version passed every check
# while never running a second time.
NEXT=$(systemctl show orbit-block-metadata.timer -p NextElapseUSecMonotonic --value)
[ "$NEXT" != "infinity" ] || echo "    WARNING: timer will never fire again"
# Fatal, like the inotify assertion above: a bootstrap that reports Done while pods can
# reach the metadata service (the build-time root password) is worse than no bootstrap.
if iptables -t raw -C PREROUTING -d 169.254.169.254 -j DROP 2>/dev/null \
  && iptables -t raw -C OUTPUT -d 169.254.169.254 -j DROP 2>/dev/null; then
  echo "    metadata blocked in the raw table (no position to defend)"
else
  echo "    FATAL: metadata block not applied (raw-table DROP for 169.254.169.254 missing on PREROUTING or OUTPUT)" >&2
  echo "    check: systemctl status orbit-block-metadata.service; iptables -t raw -S" >&2
  exit 1
fi
IFACES=$( {
  ip -4 route show default 2>/dev/null
  ip -6 route show default 2>/dev/null
} | awk '{for (i=1; i<NF; i++) if ($i=="dev" && $(i+1)!="" && $(i+1)!="lo") print $(i+1)}' | sort -u )
if [ -z "$IFACES" ]; then
  echo "    FATAL: no default-route interface to close tcp/6443 on" >&2
  echo "    check: ip -4 route show default; ip -6 route show default" >&2
  exit 1
fi
for IFACE in $IFACES; do
  if iptables -w 5 -C INPUT -i "$IFACE" -p tcp --dport 6443 -j DROP 2>/dev/null; then
    echo "    api port 6443 closed on $IFACE"
  else
    echo "    FATAL: tcp/6443 not closed on $IFACE (a default-route interface)" >&2
    echo "    check: iptables -S INPUT; ip -4 route show default; ip -6 route show default" >&2
    exit 1
  fi
  if [ -e /proc/net/if_inet6 ]; then
    if ip6tables -w 5 -C INPUT -i "$IFACE" -p tcp --dport 6443 -j DROP 2>/dev/null; then
      echo "    api port 6443 closed on $IFACE (v6)"
    else
      echo "    FATAL: tcp/6443 not closed on $IFACE v6 (a default-route interface)" >&2
      echo "    check: ip6tables -S INPUT; ip -6 route show default" >&2
      exit 1
    fi
  fi
done
REMOTE

echo "==> [2/4] k3s"
ssh_cmd "TLS_SANS='$TLS_SANS' bash -eu -s" <<'REMOTE'
# --disable traefik on purpose. k3s' bundled Traefik is configured through a
# HelmChartConfig dropped on the node's filesystem, which would put ingress
# configuration outside git. Argo CD installs Traefik instead, so every routing
# decision is a commit.
#
# The embedded cloud controller is NOT disabled: k3s hosts the ServiceLB
# (klipper) controller inside it, so --disable-cloud-controller silently breaks
# every LoadBalancer Service. (Adversarial review caught exactly that flag here.)
#
# Kubelet gets real memory guard rails. k3s ships disk-only eviction thresholds
# and reserves nothing for the system, so without these three flags the node's
# only defence against memory pressure is the kernel OOM killer — which does not
# distinguish the rocket-alert listener from a batch job.
# Reservations scale with the node. The original values were sized on a 16GB box;
# on a 4GB test node they consumed half the machine and the platform stack sat
# Pending on "Insufficient memory" with the node 99% requested (fresh-org test,
# 2026-08-27). Small nodes reserve less absolutely — and keep a real eviction
# threshold, because the guard rails are the point.
MEM_KB=$(grep MemTotal /proc/meminfo | awk '{print $2}')
if [ "$MEM_KB" -lt 8000000 ]; then
  RESV="--kubelet-arg=system-reserved=cpu=150m,memory=256Mi \
  --kubelet-arg=kube-reserved=cpu=150m,memory=256Mi \
  --kubelet-arg=eviction-hard=memory.available<300Mi,nodefs.available<10%"
else
  RESV="--kubelet-arg=system-reserved=cpu=250m,memory=768Mi \
  --kubelet-arg=kube-reserved=cpu=250m,memory=512Mi \
  --kubelet-arg=eviction-hard=memory.available<500Mi,nodefs.available<10%"
fi
# The resolvers the cluster's DNS forwards to: the node's own, IPv4 only.
#
# Pods get the kubelet's resolv.conf, and CoreDNS forwards every name outside the cluster
# to the nameservers in it. A provider's image lists IPv6 resolvers first (read on the
# rebuilt test server: two IPv6, then two IPv4), and this cluster's pod network has no
# IPv6 route: whenever CoreDNS picked one of them the lookup failed ("dial udp
# [2a01:...]:53: connect: network is unreachable", 285 times in three hours), and a ship
# failed at "registry unreachable", a backup at the bucket's name, with every one of
# those hosts answering (docs/V1.md §8). The kubelet is given a file of the node's IPv4
# resolvers instead. A node with none (IPv6-only resolvers) keeps the kubelet's default,
# and says so.
RESOLV_SRC=/run/systemd/resolve/resolv.conf
[ -r "$RESOLV_SRC" ] || RESOLV_SRC=/etc/resolv.conf
RESOLV_ARG=""
V4=""
if [ -r "$RESOLV_SRC" ]; then
  V4=$(awk '$1 == "nameserver" && $2 ~ /^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$/ && $2 !~ /^127\./ {print $2}' "$RESOLV_SRC" | head -3)
fi
if [ -n "$V4" ]; then
  mkdir -p /etc/rancher/k3s
  { echo "# Managed by scripts/bootstrap-node.sh: the node's IPv4 resolvers, for the kubelet (pods, CoreDNS)."
    for ns in $V4; do echo "nameserver $ns"; done; } > /etc/rancher/k3s/resolv.conf.new
  if [ "$(cat /etc/rancher/k3s/resolv.conf.new)" != "$(cat /etc/rancher/k3s/resolv.conf 2>/dev/null)" ]; then
    mv /etc/rancher/k3s/resolv.conf.new /etc/rancher/k3s/resolv.conf
    RESOLV_CHANGED=1
  else
    rm -f /etc/rancher/k3s/resolv.conf.new
  fi
  chmod 644 /etc/rancher/k3s/resolv.conf
  RESOLV_ARG="--resolv-conf /etc/rancher/k3s/resolv.conf"
  echo "    cluster DNS forwards to: $(echo $V4 | tr '\n' ' ')"
else
  echo "    WARNING: the node lists no IPv4 resolver; the kubelet keeps its default resolv.conf"
fi

K3S_ARGS="server \
  --disable traefik \
  --write-kubeconfig-mode 600 \
  $RESV \
  $RESOLV_ARG \
  --tls-san ${TLS_SANS//,/ --tls-san }"

# The installer is idempotent and re-running it is the supported way to change
# server args: it rewrites the systemd unit and restarts k3s (brief API blip,
# workloads keep running). Skip only if the unit already matches.
if systemctl is-active --quiet k3s \
   && grep -q "eviction-hard=memory.available<$([ "$MEM_KB" -lt 8000000 ] && echo 300 || echo 500)Mi" /etc/systemd/system/k3s.service \
   && ! grep -q "disable-cloud-controller" /etc/systemd/system/k3s.service \
   && { [ -z "$RESOLV_ARG" ] || grep -q "resolv-conf" /etc/systemd/system/k3s.service; }; then
  echo "    k3s already running with current args: $(k3s --version | head -1)"
else
  curl -sfL https://get.k3s.io | INSTALL_K3S_EXEC="$K3S_ARGS" sh -
  echo "    installed/updated: $(k3s --version | head -1)"
fi

# CoreDNS read the kubelet's resolv.conf when its pod was made: a pod made before the file
# existed (or before it changed) still forwards to the old list, so it is made again.
if [ "${RESOLV_CHANGED:-0}" = 1 ] && k3s kubectl -n kube-system get deploy coredns >/dev/null 2>&1; then
  k3s kubectl -n kube-system rollout restart deploy/coredns >/dev/null 2>&1 \
    && echo "    coredns restarted to read the new resolver list" || true
fi

# The API server listens on 0.0.0.0; block [1/4] DROPs tcp/6443 on the
# default-route interface. Reachability is therefore SSH-tunnel-only.
systemctl is-active k3s
REMOTE

echo "==> [3/4] wait for the node to go Ready"
ssh_cmd 'for i in $(seq 1 60); do
  if k3s kubectl get nodes 2>/dev/null | grep -qw Ready; then k3s kubectl get nodes -o wide; exit 0; fi
  sleep 5
done; echo "node never became Ready"; k3s kubectl get nodes; exit 1'

echo "==> [4/4] fetch kubeconfig -> $KUBECONFIG_OUT"
mkdir -p "$(dirname "$KUBECONFIG_OUT")"
umask 077
# k3s names every cluster/user/context "default", which collides the moment you
# hold a second kubeconfig. Rename via the YAML structure rather than sed, so the
# list form ("- name: default") can't be missed. The server URL already points at
# 127.0.0.1:6443 — that is the tunnel's local end, so it needs no rewriting.
# The program goes in -c, not a heredoc: a heredoc would become stdin and swallow
# the piped kubeconfig.
ssh_cmd 'cat /etc/rancher/k3s/k3s.yaml' | python3 -c '
import sys, re
cfg = sys.stdin.read()
if "clusters:" not in cfg:
    sys.exit("refusing to write: did not receive a kubeconfig")
ctx_name = sys.argv[2]
# Only touch the identifier fields; never the base64 cert blobs.
for pat in (r"(?m)^(\s*-?\s*name:\s*)default\s*$",
            r"(?m)^(\s*cluster:\s*)default\s*$",
            r"(?m)^(\s*user:\s*)default\s*$",
            r"(?m)^(current-context:\s*)default\s*$"):
    cfg = re.sub(pat, r"\1" + ctx_name, cfg)
open(sys.argv[1], "w").write(cfg)
' "$KUBECONFIG_OUT" "${ORBIT_INSTANCE_NAME}-platform"
chmod 600 "$KUBECONFIG_OUT"

cat <<EOF

Done. The API server is firewalled off, so kubectl needs the tunnel:

    ssh -i $SSH_KEY -N -L 6443:127.0.0.1:6443 root@$HOST &
    export KUBECONFIG=$KUBECONFIG_OUT
    kubectl get nodes

EOF
