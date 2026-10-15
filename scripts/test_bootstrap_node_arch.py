"""The node bootstrap on an Arch (Omarchy / Asahi) node, proven in a fake-node sandbox.

Every `<<'REMOTE'` block of scripts/bootstrap-node.sh is run, in order, as root inside a
user namespace where /etc and /usr/local are private directories and PATH holds only the
fakes below plus a fixed set of real userland tools. No real package manager, no real
iptables, no network. The fakes log every call; the tests read the logs and the private
/etc afterwards. Two node layouts: Arch (pacman, zram-generator device + fstab swapfile,
no apt-get) and Debian (apt-get, fstab swap only, no pacman).
"""
import os, pathlib, re, shutil, stat, subprocess, sys, tempfile, textwrap, unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
BOOTSTRAP = ROOT / "scripts" / "bootstrap-node.sh"
STAGE3 = ROOT / "scripts" / "verify" / "stage3.sh"
LIB = ROOT / "scripts" / "lib-instance.sh"

REAL_TOOLS = ("bash sh grep sed awk cat chmod mkdir rm cp mv ls wc tr head tail cut sort uniq tee "
              "printf echo env date seq sleep timeout mktemp basename dirname readlink id uname touch "
              "python3 xargs find hostname stat ln true false test mount").split()


def remote_blocks(text: str):
    blocks, cur = [], None
    for line in text.splitlines():
        if cur is None:
            if line.rstrip().endswith("<<'REMOTE'"):
                cur = []
        elif line.strip() == "REMOTE":
            blocks.append("\n".join(cur) + "\n"); cur = None
        else:
            cur.append(line)
    return blocks


FAKE_IPTABLES = r'''#!/usr/bin/env python3
import os, sys
name = os.path.basename(sys.argv[0]); args = sys.argv[1:]
log = os.environ["FAKE_LOG_DIR"] + "/" + name + ".log"
open(log, "a").write(" ".join(args) + "\n")
a = list(args)
while "-w" in a:
    i = a.index("-w"); del a[i:i + 2]
if "-L" in a:
    sys.exit(0)
def spec(op):
    i = a.index(op); rest = a[i + 1:]
    if op == "-I" and len(rest) > 1 and rest[1].isdigit():
        rest = [rest[0]] + rest[2:]
    return " ".join(a[:i] + rest)
seen = os.environ["FAKE_LOG_DIR"] + "/" + name + ".rules"
if "-I" in a or "-A" in a:
    open(seen, "a").write(spec("-I" if "-I" in a else "-A") + "\n"); sys.exit(0)
if "-C" in a:
    want = spec("-C")
    have = open(seen).read().splitlines() if os.path.exists(seen) else []
    sys.exit(0 if want in have else 1)
sys.exit(0)
'''

FAKE_SYSTEMCTL = r'''#!/usr/bin/env bash
# logs every call; `start <unit>` runs the unit's ExecStart the way systemd would, so a
# hook installed as a oneshot really executes (the metadata block and the 6443 rule)
echo "$*" >> "$FAKE_LOG_DIR/systemctl.log"
case " $* " in
  *" is-active --quiet k3s "*) exit 1 ;;
  *" is-active k3s "*) echo active; exit 0 ;;
  *" show "*) echo 12345; exit 0 ;;
esac
if [ "$1" = start ] || [ "$1" = restart ]; then
  for u in "${@:2}"; do
    f="/etc/systemd/system/$u"; [ -f "$f" ] || continue
    cmd=$(sed -n 's/^ExecStart=//p' "$f" | head -1); [ -n "$cmd" ] && bash -c "$cmd" || true
  done
fi
exit 0
'''
FAKE_SYSCTL = r'''#!/usr/bin/env bash
echo "$*" >> "$FAKE_LOG_DIR/sysctl.log"
case " $* " in *" -n "*) echo 512 ;; esac
exit 0
'''
FAKE_SSHD = r'''#!/usr/bin/env bash
# sshd as the node has it: `-t` accepts the configuration; `-T` prints the effective one,
# where the FIRST value met wins: a drop-in read before the provider's (which says yes)
echo "$*" >> "$FAKE_LOG_DIR/sshd.log"
case "$1" in
  -t) exit 0 ;;
  -T) v=$(cat /etc/ssh/sshd_config.d/*.conf 2>/dev/null | awk 'tolower($1) == "passwordauthentication" {print tolower($2); exit}')
      echo "port 22"; echo "passwordauthentication ${v:-yes}" ;;
esac
exit 0
'''
FAKE_SWAPON = r'''#!/usr/bin/env bash
cat "$FAKE_SWAP_STATE"
'''
FAKE_SWAPOFF = r'''#!/usr/bin/env bash
echo "$*" >> "$FAKE_LOG_DIR/swapoff.log"; : > "$FAKE_SWAP_STATE"
'''
FAKE_PKG = r'''#!/usr/bin/env bash
echo "$*" >> "$FAKE_LOG_DIR/$(basename "$0").log"
exit 0
'''
FAKE_CURL = r'''#!/usr/bin/env bash
echo "$*" >> "$FAKE_LOG_DIR/curl.log"
case "$*" in *get.k3s.io*) printf '%s\n' 'echo "$INSTALL_K3S_EXEC" > "$FAKE_LOG_DIR/k3s-exec.log"' ;; esac
exit 0
'''
FAKE_K3S = r'''#!/usr/bin/env bash
case "$1" in --version) echo "k3s version v1.37.0+k3s1 (fake)";; kubectl) echo "node-1 Ready control-plane 1m v1.37.0+k3s1";; esac
exit 0
'''
FAKE_IP = r'''#!/usr/bin/env bash
echo "$*" >> "$FAKE_LOG_DIR/ip.log"
# Two defaults (ethernet + wifi, different metrics) and a v6 pair: closing only
# the first v4 `dev` would leave 6443 open on wlan0 and on [v6].
if [[ " $* " == *" -6 "* ]]; then
  printf '%s\n' \
    "default via fe80::1 dev eth0 proto ra metric 1024" \
    "default via fe80::1 dev wlan0 proto ra metric 2048"
else
  printf '%s\n' \
    "default via 192.168.1.1 dev eth0 proto dhcp src 192.168.1.50 metric 100" \
    "default via 192.168.1.1 dev wlan0 proto dhcp src 192.168.1.51 metric 600"
fi
exit 0
'''


class FakeNode:
    def __init__(self, layout: str):
        self.layout = layout
        self.tmp = pathlib.Path(tempfile.mkdtemp(prefix=f"fake-node-{layout}-"))
        self.bin = self.tmp / "bin"; self.etc = self.tmp / "etc"; self.usr_local = self.tmp / "usr-local"
        self.logs = self.tmp / "logs"
        for d in (self.bin, self.etc / "sysctl.d", self.etc / "systemd" / "system", self.usr_local / "sbin", self.logs):
            d.mkdir(parents=True)
        for t in REAL_TOOLS:
            p = shutil.which(t)
            if p and not (self.bin / t).exists():
                # the RESOLVED path: on Debian/Ubuntu awk (and others) is a chain through
                # /etc/alternatives, which the node's private /etc below does not carry, so
                # a link to /usr/bin/awk dangles inside the sandbox ("awk: not found")
                os.symlink(os.path.realpath(p), self.bin / t)
        fakes = {"iptables": FAKE_IPTABLES, "ip6tables": FAKE_IPTABLES, "systemctl": FAKE_SYSTEMCTL,
                 "sysctl": FAKE_SYSCTL, "sshd": FAKE_SSHD, "swapon": FAKE_SWAPON, "swapoff": FAKE_SWAPOFF, "curl": FAKE_CURL,
                 "k3s": FAKE_K3S, "ip": FAKE_IP}
        if layout == "arch":
            fakes["pacman"] = FAKE_PKG
        elif layout == "debian":
            fakes["apt-get"] = FAKE_PKG
        for name, body in fakes.items():
            f = self.bin / name; f.write_text(body); f.chmod(0o755)
        for name in ("passwd", "group", "nsswitch.conf", "hosts", "ld.so.cache"):
            src = pathlib.Path("/etc") / name
            if src.exists():
                shutil.copy(src, self.etc / name)
        swap_state = self.tmp / "swap-state"
        # The node's /usr/lib/systemd and /run/systemd are ALWAYS the fixture's, never the
        # host's: bootstrap globs both for zram-generator config, so a host that runs zram
        # itself (the author's workstation) would otherwise steer the Debian fixture down
        # the Arch branch, and a host without the vendor directory could not build the
        # Arch fixture at all (review of PR #189, both reviewers).
        self.usr_lib_systemd = self.tmp / "usr-lib-systemd"; (self.usr_lib_systemd / "zram-generator.conf.d").mkdir(parents=True)
        self.run_systemd = self.tmp / "run-systemd"; self.run_systemd.mkdir()
        self.zram_confd = self.usr_lib_systemd / "zram-generator.conf.d"
        # the node's resolvers, as each layout has them: a provider's image lists IPv6
        # resolvers first (read on the rebuilt test server); a home node may list only a
        # loopback stub and a link-local one, which is "no IPv4 resolver"
        if layout == "debian":
            (self.run_systemd / "resolve").mkdir()
            (self.run_systemd / "resolve" / "resolv.conf").write_text(
                "nameserver 2a01:4ff:ff00::add:2\nnameserver 2a01:4ff:ff00::add:1\nnameserver 185.12.64.1\nnameserver 185.12.64.2\n")
        else:
            (self.etc / "resolv.conf").write_text("nameserver 127.0.0.53\nnameserver fe80::1%eth0\noptions edns0\n")
        if layout == "arch":
            (self.etc / "fstab").write_text("UUID=root / ext4 defaults 0 1\n/swap/swapfile none swap defaults,pri=0 0 0\n")
            # Vendor drop-in, not /etc/systemd/zram-generator.conf: that is the
            # layout a real Arch home node ships (conf.d under /usr/lib).
            (self.zram_confd / "90-zram.conf").write_text("[zram0]\nzram-size = ram\ncompression-algorithm = zstd\n")
            swap_state.write_text("/swap/swapfile file 31G 0B 0\n/dev/zram0 partition 31G 4.7G 100\n")
        else:
            (self.etc / "fstab").write_text("UUID=root / ext4 defaults 0 1\n/swapfile none swap sw 0 0\n")
            swap_state.write_text("/swapfile file 4G 0B -2\n")
        self.swap_state = swap_state

    def run_blocks(self):
        blocks = remote_blocks(BOOTSTRAP.read_text())
        assert blocks, "bootstrap-node.sh has no <<'REMOTE' blocks"
        results = []
        for i, block in enumerate(blocks):
            (self.tmp / f"block{i}.sh").write_text(block)
            mounts = (f"mount --bind {self.etc} /etc && mount --bind {self.usr_local} /usr/local"
                      f" && mount --bind {self.usr_lib_systemd} /usr/lib/systemd"
                      f" && mount --bind {self.run_systemd} /run/systemd")
            inner = f"{mounts} && exec bash -eu -s < {self.tmp}/block{i}.sh"
            env = {"PATH": str(self.bin), "HOME": str(self.tmp), "FAKE_LOG_DIR": str(self.logs),
                   "FAKE_SWAP_STATE": str(self.swap_state), "TLS_SANS": "192.168.1.50,k8s.example.test",
                   "LANG": "C"}
            proc = subprocess.run([shutil.which("unshare"), "-Urm", "--propagation", "private", shutil.which("bash"), "-c", inner],
                                  env=env, capture_output=True, text=True, timeout=120)
            results.append(proc)
            if proc.returncode != 0:
                break
        return results

    def log(self, name: str) -> str:
        p = self.logs / f"{name}.log"
        return p.read_text() if p.exists() else ""

    def cleanup(self):
        shutil.rmtree(self.tmp, ignore_errors=True)


def failure_text(results):
    return "\n".join(f"--- block {i} rc={r.returncode}\nstdout:\n{r.stdout[-2000:]}\nstderr:\n{r.stderr[-2000:]}"
                     for i, r in enumerate(results))


class ArchNode(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.node = FakeNode("arch"); cls.results = cls.node.run_blocks()

    @classmethod
    def tearDownClass(cls):
        cls.node.cleanup()

    def all_green(self):
        self.assertTrue(all(r.returncode == 0 for r in self.results), failure_text(self.results))

    def test_arch_node_block1_installs_with_pacman_and_disables_zram_swap(self):
        self.all_green()
        pac = self.node.log("pacman")
        self.assertIn("-S", pac, "pacman was never asked to install anything:\n" + pac)
        self.assertIsNone(re.search(r"(^|\s)-Sy(\s|$)", pac),
                          "pacman -Sy refreshes the DB without upgrading the rest of the system:\n" + pac)
        self.assertRegex(pac, r"\biptables\b", "iptables is what the metadata block and the 6443 rule need:\n" + pac)
        self.assertNotRegex(pac, r"(^|\s)iptables-nft(\s|$)",
                            "iptables-nft is a provide, not a package; installing it fails:\n" + pac)
        self.assertEqual("", self.node.log("apt-get"), "apt-get must not be called on an Arch node")
        self.assertEqual("", self.node.swap_state.read_text(), "swapon --show still lists a device after the swap step")
        fstab = (self.node.etc / "fstab").read_text()
        self.assertNotRegex(fstab, r"(?m)^[^#].*\sswap\s", "the fstab swap line is still active:\n" + fstab)
        bak = (self.node.etc / "fstab.bak").read_text()
        self.assertRegex(bak, r"(?m)^[^#].*\sswap\s", "fstab.bak is not the original file:\n" + bak)
        # Vendor drop-in is untouched (we do not edit /usr/lib). Persist is the mask
        # plus an /etc override; an /etc main file was never the layout.
        self.assertFalse((self.node.etc / "systemd" / "zram-generator.conf").exists())
        self.assertIn("[zram0]", (self.node.zram_confd / "90-zram.conf").read_text())
        self.assertIsNotNone(re.search(r"\b(mask|disable)\b.*zram", self.node.log("systemctl")),
                             "zram-generator config lives in a vendor conf.d; the unit must be masked/disabled:\n"
                             + self.node.log("systemctl"))
        override = (self.node.etc / "systemd" / "zram-generator.conf.d" / "99-orbit-zram-off.conf").read_text()
        self.assertIn("[zram0]", override)
        self.assertRegex(override, r"(?m)^zram-size\s*=\s*0\s*$")

    def test_a_second_bootstrap_keeps_the_original_fstab_backup(self):
        self.all_green()
        bak = (self.node.etc / "fstab.bak").read_text()
        self.assertRegex(bak, r"(?m)^[^#].*\sswap\s")
        results = self.node.run_blocks()
        self.assertTrue(all(r.returncode == 0 for r in results), failure_text(results))
        self.assertEqual(bak, (self.node.etc / "fstab.bak").read_text(),
                         "a second run replaced fstab.bak with the already-commented file")

    def test_api_port_6443_is_closed_on_the_default_route_interface_and_persisted(self):
        self.all_green()
        ipt = self.node.log("iptables")
        rules = [l for l in ipt.splitlines() if "6443" in l and re.search(r"-j (DROP|REJECT)", l)]
        self.assertTrue(rules, "no iptables rule drops or rejects tcp/6443:\n" + ipt)
        scoped = [l for l in rules if re.search(r"(^|\s)-i\s+(?!lo\b)\S+", l)]
        self.assertTrue(scoped, "the 6443 rule is unconditional; it must name the default-route interface "
                                "so loopback (the SSH tunnel) and the pod network keep reaching the API:\n" + "\n".join(rules))
        ifaces = {m.group(2) for l in scoped if (m := re.search(r"(^|\s)-i\s+(?!lo\b)(\S+)", l))}
        self.assertTrue({"eth0", "wlan0"} <= ifaces,
                        "6443 must be closed on every default-route interface, not just the first:\n" + "\n".join(rules))
        self.assertTrue(any("-C" in l for l in ipt.splitlines() if "6443" in l), "the 6443 rule is not checked with -C before -I (not idempotent)")
        ip6 = self.node.log("ip6tables")
        rules6 = [l for l in ip6.splitlines() if "6443" in l and re.search(r"-j (DROP|REJECT)", l)]
        self.assertTrue(rules6, "no ip6tables rule drops or rejects tcp/6443; the API listens dual-stack:\n" + ip6)
        ifaces6 = {m.group(2) for l in rules6 if (m := re.search(r"(^|\s)-i\s+(?!lo\b)(\S+)", l))}
        self.assertTrue({"eth0", "wlan0"} <= ifaces6,
                        "6443 must be closed on every default-route interface on v6 too:\n" + "\n".join(rules6))
        persisted = [p for p in list((self.node.usr_local / "sbin").rglob("*")) + list((self.node.etc / "systemd" / "system").rglob("*"))
                     if p.is_file() and "6443" in p.read_text(errors="replace")]
        self.assertTrue(persisted, "no persisted hook or unit mentions 6443: the rule is gone after a reboot")

    def test_k3s_block_installs_with_traefik_disabled_on_arch(self):
        self.all_green()
        exec_line = self.node.log("k3s-exec")
        self.assertIn("--disable traefik", exec_line, "the k3s installer was not run with traefik disabled:\n" + exec_line)
        self.assertIn("--tls-san 192.168.1.50", exec_line)
        # this node lists no IPv4 resolver (a loopback stub and a link-local one): the kubelet
        # keeps its default, the bootstrap says so, and no file is written
        self.assertNotIn("--resolv-conf", exec_line)
        self.assertIn("the node lists no IPv4 resolver", "".join(r.stdout for r in self.results))
        self.assertFalse((self.node.etc / "rancher" / "k3s" / "resolv.conf").exists())


class DebianNode(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.node = FakeNode("debian"); cls.results = cls.node.run_blocks()

    @classmethod
    def tearDownClass(cls):
        cls.node.cleanup()

    def test_debian_node_block1_still_uses_apt_get(self):
        self.assertTrue(all(r.returncode == 0 for r in self.results), failure_text(self.results))
        apt = self.node.log("apt-get")
        self.assertIn("install", apt, "apt-get install was never called on a Debian node:\n" + apt)
        self.assertEqual("", self.node.log("pacman"))
        self.assertEqual("", self.node.swap_state.read_text())

    def test_the_node_is_left_taking_keys_only_on_ssh(self):
        """A rebuilt server comes from the provider with a root password and password
        sign-in on (docs/V1.md §8). The drop-in is read BEFORE the provider's own, which
        says yes; sshd's effective configuration is asked after the reload."""
        self.assertTrue(all(r.returncode == 0 for r in self.results), failure_text(self.results))
        dropin = (self.node.etc / "ssh" / "sshd_config.d" / "00-orbit.conf").read_text()
        self.assertIn("PasswordAuthentication no\n", dropin)
        self.assertIn("KbdInteractiveAuthentication no\n", dropin)
        self.assertLess("00-orbit.conf", "50-cloud-init.conf")
        log = self.node.log("sshd")
        self.assertLess(log.index("-t"), log.index("-T"), "the configuration was not checked before the reload")
        self.assertRegex(self.node.log("systemctl"), r"reload ssh")
        self.assertIn("ssh: keys only (password sign-in off)", self.results[0].stdout)

    def test_the_cluster_dns_is_given_the_nodes_ipv4_resolvers_only(self):
        """CoreDNS forwards to the kubelet's resolv.conf, and a pod network with no IPv6
        route cannot reach an IPv6 resolver: on the rebuilt test server lookups failed 285
        times in three hours and a ship failed at "registry unreachable" (docs/V1.md §8)."""
        self.assertTrue(all(r.returncode == 0 for r in self.results), failure_text(self.results))
        given = (self.node.etc / "rancher" / "k3s" / "resolv.conf").read_text()
        self.assertEqual([l for l in given.splitlines() if l.startswith("nameserver")],
                         ["nameserver 185.12.64.1", "nameserver 185.12.64.2"])
        self.assertIn("--resolv-conf /etc/rancher/k3s/resolv.conf", self.node.log("k3s-exec"))
        out = "".join(r.stdout for r in self.results)
        self.assertIn("cluster DNS forwards to: 185.12.64.1 185.12.64.2", out)
        self.assertIn("coredns restarted to read the new resolver list", out)

    def test_debian_node_without_zram_takes_no_zram_step(self):
        # the fixture, not the host, decides: a Debian node with no zram-generator config
        # anywhere must see no mask and no override written (on a workstation that itself
        # runs zram this failed before the sandbox owned /usr/lib/systemd and /run/systemd)
        self.assertTrue(all(r.returncode == 0 for r in self.results), failure_text(self.results))
        self.assertNotRegex(self.node.log("systemctl"), r"zram", "a zram unit was touched on a node that has none")
        self.assertFalse((self.node.etc / "systemd" / "zram-generator.conf.d").exists(),
                         "a zram override was written on a node that has no zram")


class NeitherPkgTool(unittest.TestCase):
    def test_refuses_a_node_with_neither_pacman_nor_apt_get_naming_both(self):
        node = FakeNode("neither")
        try:
            results = node.run_blocks()
            self.assertTrue(results, "block [1/4] never ran")
            self.assertNotEqual(results[0].returncode, 0, "a node with neither package tool was accepted")
            text = results[0].stdout + results[0].stderr
            self.assertIn("pacman", text)
            self.assertIn("apt-get", text)
        finally:
            node.cleanup()


MINIMAL_CONFIG = textwrap.dedent("""\
    instance:
      name: exampleorg
    git:
      repoUrl: git@github.com:exampleorg/exampleorg-platform.git
      branch: main
      github_org: exampleorg
    domains:
      primary: example.test
      dev: dev.example.test
      dnsZones: [example.test]
    registry: ghcr.io/exampleorg
    nodes:
      - name: org-node-1
        ip: 192.168.1.50
        role: control-plane
        provider: {provider}
    """)


def source_lib(provider: str):
    tmp = pathlib.Path(tempfile.mkdtemp(prefix="lib-instance-"))
    cfg = tmp / "config.yaml"; cfg.write_text(MINIMAL_CONFIG.format(provider=provider))
    env = dict(os.environ, ORBIT_INSTANCE_CONFIG=str(cfg))
    env.pop("ORBIT_NODE_PROVIDER", None)
    proc = subprocess.run(["bash", "-c", f'. "{LIB}" && printf "%s" "${{ORBIT_NODE_PROVIDER-UNSET}}"'],
                          env=env, capture_output=True, text=True, timeout=60)
    shutil.rmtree(tmp, ignore_errors=True)
    return proc


class NodeProvider(unittest.TestCase):
    def test_lib_instance_exports_node_provider(self):
        for value in ("owned", "hetzner"):
            proc = source_lib(value)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertEqual(proc.stdout.strip(), value)

    def test_lib_instance_refuses_an_unknown_provider_naming_the_field(self):
        proc = source_lib("azure")
        self.assertNotEqual(proc.returncode, 0, "an unknown provider was accepted")
        self.assertIn("provider", proc.stderr)
        self.assertNotIn("azure", proc.stderr + proc.stdout, "the refusal repeats the value")


class Stage3OwnedNode(unittest.TestCase):
    def test_stage3_metadata_counter_row_keys_off_for_an_owned_node(self):
        text = STAGE3.read_text()
        at = text.find("metadata DROP counters")
        self.assertGreater(at, 0, "the counters row is gone from stage3")
        window = text[max(0, at - 1500):at + 400]
        self.assertIn("ORBIT_NODE_PROVIDER", window, "the counters row does not consult the node's provider")
        self.assertIn("owned", window, "the counters row has no owned-node branch")


if __name__ == "__main__":
    unittest.main()
