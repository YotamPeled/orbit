#!/usr/bin/env bash
# Stage 2 — trust + edge. Re-runnable; every later stage runs this first.
#
# Deliberately asserts identity through the cluster API rather than by fingerprinting
# HTTP responses: a Traefik 404, a Cloudflare 404 and a Firebase 404 are
# indistinguishable from outside, so "404 means Traefik answered" is a false green
# waiting to happen.
set -uo pipefail
# the python blocks below import the shared display rule (scripts/verify/shown.py)
export PYTHONPATH="$(cd "$(dirname "$0")" && pwd)${PYTHONPATH:+:$PYTHONPATH}"
PASS=0; FAIL=0
ok()   { echo "  ok    $1"; PASS=$((PASS+1)); }
bad()  { echo "  FAIL  $1"; FAIL=$((FAIL+1)); }
check(){ if eval "$2" >/dev/null 2>&1; then ok "$1"; else bad "$1"; fi; }

. "$(dirname "$0")/../lib-instance.sh" || exit 2

export KUBECONFIG="${KUBECONFIG:-$HOME/.kube/config-${ORBIT_INSTANCE_NAME}-platform}"

# Stage 2 is live-cluster head to toe. Pre-boot (no kubeconfig FILE — a declaration
# that bootstrap never ran, never a reachability probe), the whole stage keys off the
# stage8 way: visibly, with a zero count. A bootstrapped instance that lost its
# cluster still fails loudly — its kubeconfig exists.
# KUBECONFIG is a LIST (colon-separated) the way kubectl reads it: pre-boot means no
# entry is a readable non-empty file; an entry that exists but is empty or unreadable is
# refused rather than read as pre-boot.
kubeconfig_present(){ local f any=1; set -f; IFS=: ; for f in $1; do IFS=$' \t\n'   # set -f: the list is split, never glob-expanded
    if [ -s "$f" ] && [ -r "$f" ]; then any=0
    elif [ -e "$f" ]; then echo "kubeconfig $f exists but is empty or unreadable — neither a cluster nor pre-boot; fix or remove it" >&2; exit 2; fi
  done; IFS=$' \t\n'; set +f; return $any; }
if ! kubeconfig_present "$KUBECONFIG"; then
  echo "stage2: 0 passed, 0 failed (pre-boot — no kubeconfig at $KUBECONFIG; bootstrap arms this stage)"
  exit 0
fi
IP="$ORBIT_NODE_IP"
# The platform wildcard Certificate/Secret are named after the primary domain with
# dots swapped for dashes (wildcard-<domain-dashed>[-tls]) — same convention the
# generator uses when it renders clusters/platform/config/cert-manager/.
WILDCARD="wildcard-${ORBIT_DOMAIN//./-}"

echo "== node =="
# configz is the kubelet's whole configuration (staticPodURLHeader can carry a bearer
# token): it goes to a private file, never a shell variable a traced shell prints, and
# only two yes/no facts come back out
CFGF=$(mktemp)
kubectl get --raw /api/v1/nodes/"$ORBIT_NODE_NAME"/proxy/configz > "$CFGF" 2>/dev/null
grep -q '"memory.available"' "$CFGF" && ok "kubelet has a memory eviction threshold" \
  || bad "kubelet has NO memory eviction threshold (kernel OOM is the only backstop)"
grep -q '"systemReserved"' "$CFGF" && ok "system/kube reserved set" || bad "no reservations"
rm -f "$CFGF"
# stderr closed: a kubeconfig's credential plugin writes there, and what it writes is its own
# The two quantities are the cluster's text: they go to a scratch file, never to a shell
# value or an argument, and are read back only once they have the shape of a quantity.
# Compared as QUANTITIES (Ki/Mi/Gi..., k/M/G..., m, plain), not as strings: 100Gi and
# 10Gi differ as text and the wrong way round as numbers.
MEMQ=$(mktemp)
# the configured node, by name (the loader held it to a label's shape): on a cluster
# with more than one node, items[0] was whichever node sorted first (round 25)
kubectl get node "$ORBIT_NODE_NAME" -o jsonpath='{.status.allocatable.memory}{"\n"}{.status.capacity.memory}{"\n"}' > "$MEMQ" 2>/dev/null
if MEMROW=$(python3 - "$MEMQ" <<'PY'
import re, sys
U = {"":1,"m":1e-3,"k":1e3,"M":1e6,"G":1e9,"T":1e12,"P":1e15,"E":1e18,"Ki":2**10,"Mi":2**20,"Gi":2**30,"Ti":2**40,"Pi":2**50,"Ei":2**60}
lines = open(sys.argv[1]).read().split("\n")
def q(s):
    m = re.fullmatch(r"([0-9]+(?:\.[0-9]+)?)(m|k|M|G|T|P|E|Ki|Mi|Gi|Ti|Pi|Ei)?", s.strip())
    if not m: sys.exit(2)
    return m.group(0), float(m.group(1)) * U[m.group(2) or ""]
if len(lines) < 2: sys.exit(2)
(a, av), (c, cv) = q(lines[0]), q(lines[1])
print(f"allocatable ({a}) < capacity ({c})")
sys.exit(0 if av < cv else 1)
PY
); then
  ok "$MEMROW"
else
  bad "allocatable is not below capacity (or the two are not readable quantities): nothing reserved for the OS"
fi
rm -f "$MEMQ"
# The key path and the address are quoted words: a path from the workstation's pointer
# file is one argument to ssh whatever it holds. What ssh prints is the node's text: it
# lands in a private file and becomes a shell value only as a count (digits), else the
# word unreadable — a traced shell prints every word it expands, so the raw output is
# never one of them (round 21).
SWAPF=$(mktemp)
ssh -o BatchMode=yes -i "$ORBIT_SSH_KEY" "root@$IP" "swapon --show --noheadings | wc -l" > "$SWAPF" 2>/dev/null
SWAPN=$(python3 -c 'import re, sys; v = open(sys.argv[1], errors="replace").read().strip(); print(v if re.fullmatch(r"[0-9]{1,6}", v) else "unreadable")' "$SWAPF" 2>/dev/null || echo unreadable); rm -f "$SWAPF"
check "swap is off" '[ "$SWAPN" = "0" ]'
# Two rows, because there are two ways to lose this and they fail at different times.
# The running value is what starves containerd shims and Alloy TODAY (silent EMFILE,
# reported as "too many open files"); the sysctl.d file is what survives a reboot or a
# node rebuild. A hand-run `sysctl -w` passes the first and fails the second, which is
# exactly the state worth catching. >= so a future raise is not a regression.
# What ssh prints is the node's to print: the value is used and shown only once it is
# a number, and anything else is reported as unreadable, never repeated.
# ...and never held in a shell variable before that check: a traced shell prints an
# assignment, so the node's output goes to a private file and only a number comes back.
INSTF=$(mktemp)
ssh -o BatchMode=yes -i "$ORBIT_SSH_KEY" "root@$IP" 'sysctl -n fs.inotify.max_user_instances' > "$INSTF" 2>/dev/null
INST=$(python3 -c 'import sys; v = open(sys.argv[1], errors="replace").read().strip(); print(v if v.isdigit() and len(v) <= 12 else "")' "$INSTF" 2>/dev/null)
rm -f "$INSTF"
case "$INST" in *[!0-9]*|"") INST=;; esac
if [ -n "$INST" ] && [ "$INST" -ge 512 ]; then
  ok "inotify max_user_instances = $INST (>=512; kubelet, shims and Alloy share the uid-0 budget)"
else
  bad "inotify max_user_instances = ${INST:-unreadable} (<512: shims and Alloy silently starve on EMFILE)"
fi
# the persisted file is the node's text: a private file, and the EFFECTIVE assignment
# (not a comment, exactly this key) must set at least 512 — `grep -q max_user_instances`
# matched a commented-out line (round 24)
PERSF=$(mktemp)
ssh -o BatchMode=yes -i "$ORBIT_SSH_KEY" "root@$IP" "cat /etc/sysctl.d/90-orbit-inotify.conf" > "$PERSF" 2>/dev/null
PERSN=$(python3 -c 'import re, sys
vals = [int(m.group(1)) for l in open(sys.argv[1], errors="replace") for m in [re.fullmatch(r"\s*fs\.inotify\.max_user_instances\s*=\s*([0-9]{1,12})\s*", l.rstrip("\n"))] if m]
print(vals[-1] if vals else "unset")' "$PERSF" 2>/dev/null || echo unset); rm -f "$PERSF"
check "the inotify raise is persisted, not just live" '[ "$PERSN" != unset ] && [ "$PERSN" -ge 512 ]'
# ssh takes keys only: sshd's own effective configuration, not a file that may be shadowed.
# A rebuilt server comes from the provider with a root password and password sign-in on
# (docs/V1.md §8, 2026-10-08); bootstrap-node.sh turns it off, and this row is what says
# so afterwards. The node's text goes to a private file; only yes/no comes back.
SSHDF=$(mktemp)
ssh -o BatchMode=yes -i "$ORBIT_SSH_KEY" "root@$IP" 'sshd -T 2>/dev/null' > "$SSHDF" 2>/dev/null
SSHDA=$(python3 -c 'import re, sys
vals = [m.group(1) for l in open(sys.argv[1], errors="replace") for m in [re.fullmatch(r"passwordauthentication (yes|no)", l.strip())] if m]
print(vals[0] if vals else "unreadable")' "$SSHDF" 2>/dev/null || echo unreadable); rm -f "$SSHDF"
case "$SSHDA" in
  no) ok "ssh on the node takes keys only (passwordauthentication no)" ;;
  yes) bad "ssh on the node takes PASSWORDS (passwordauthentication yes) — run scripts/bootstrap-node.sh, which turns it off" ;;
  *) bad "sshd's effective configuration on the node could not be read — this check proved nothing" ;;
esac

# The cluster's DNS reaches every resolver it asks. CoreDNS forwards to the kubelet's
# resolv.conf; a resolver it cannot reach (an IPv6 one, from a pod network with no IPv6
# route: the rebuilt test server, docs/V1.md §8) fails some lookups and not others, and
# what fails is whatever asked just then: a ship's registry check, a backup's bucket.
# Counted from CoreDNS's own log over ten minutes; the log's text is not repeated.
DNSF=$(mktemp)
kubectl -n kube-system logs deploy/coredns --since=10m > "$DNSF" 2>/dev/null; DNSRC=$?
DNSN=$(grep -c 'network is unreachable' "$DNSF" 2>/dev/null || true); rm -f "$DNSF"
case "$DNSN" in ''|*[!0-9]*) DNSN=unreadable;; esac
if [ "$DNSRC" -ne 0 ] || [ "$DNSN" = unreadable ]; then
  bad "the cluster DNS's log could not be read — this check proved nothing"
elif [ "$DNSN" -eq 0 ]; then
  ok "the cluster DNS reached every resolver it asked in the last 10 minutes"
else
  bad "the cluster DNS could not reach a resolver $DNSN time(s) in the last 10 minutes (network is unreachable) — run scripts/bootstrap-node.sh, which gives the kubelet the node's IPv4 resolvers"
fi

echo "== platform apps =="
# The desk applies each platform app and stores its inventory as a ConfigMap in the
# daemon's namespace; the inventory's existence is the record that the app was applied
# (the rows below prove the components run). Existence only: nothing in the ConfigMap is read.
for a in sops-secrets-operator cert-manager traefik platform-config; do
  kubectl -n orbit get configmap "orbit-platform-inventory-$a" >/dev/null 2>&1 \
    && ok "app $a applied (stored inventory present)" || bad "app $a has no stored inventory — the desk has not applied it"
done

echo "== secrets =="
check "SopsSecret decrypted to a real Secret" 'kubectl -n cert-manager get secret cloudflare-api-token'
# The sealed file is checked STRUCTURALLY: it exists, reads as YAML, and every value
# under data:/stringData: is a whole sops envelope. A missing or unreadable file is
# red (nothing was checked), and a base64 or bare token is plaintext whatever its
# letters. Values are never printed.
SOPSF=clusters/platform/secrets/cert-manager/cloudflare-api-token.yaml
if SOPSROW=$(python3 - "$SOPSF" <<'PY'
import re, sys, yaml
ENC = re.compile(r"ENC\[AES256_GCM,data:[A-Za-z0-9+/=_.\-]+,iv:[A-Za-z0-9+/=_.\-]+,tag:[A-Za-z0-9+/=_.\-]+,type:(str|int|float|bool|comment)\]")
class StrictLoader(yaml.SafeLoader):
    """A mapping that repeats a key is refused: a YAML reader keeps the LAST value, so a
    plaintext first occurrence would hide behind a sealed second one."""
    def construct_mapping(self, node, deep=False):
        seen = set()
        for k_node, _ in node.value:
            k = self.construct_object(k_node, deep=True)
            try:
                dup = k in seen
            except TypeError:
                raise yaml.constructor.ConstructorError(None, None, "an unhashable mapping key", k_node.start_mark)
            if dup:
                raise yaml.constructor.ConstructorError(None, None, "a duplicate mapping key", k_node.start_mark)
            seen.add(k)
        return super().construct_mapping(node, deep)
try:
    docs = [d for d in yaml.load_all(open(sys.argv[1], encoding="utf-8"), Loader=StrictLoader) if d is not None]
except (OSError, ValueError, yaml.YAMLError) as exc:
    print(f"the sealed cert-manager token file cannot be read as YAML ({type(exc).__name__}; a repeated key counts) — UNCHECKED, not clean"); sys.exit(1)
n, plain = 0, 0
def walk(node):
    global n, plain
    if isinstance(node, dict):
        for k, v in node.items():
            if k in ("data", "stringData"):
                if not isinstance(v, dict): plain += 1; continue
                for vv in v.values():
                    n += 1
                    if not isinstance(vv, str) or not ENC.fullmatch(vv): plain += 1
            else: walk(v)
    elif isinstance(node, list):
        for x in node: walk(x)
for d in docs: walk(d)
if plain: print(f"the sealed cert-manager token file holds {plain} value(s) under data:/stringData: that are not a sops envelope — plaintext in git"); sys.exit(1)
if not n: print("the sealed cert-manager token file carries no data:/stringData: values at all — nothing sealed, nothing checked"); sys.exit(1)
print(f"the sealed cert-manager token file holds only sops envelopes ({n} value(s))")
PY
); then ok "$SOPSROW"; else bad "$SOPSROW"; fi
# git's listing is text: a private file, NUL-delimited (a quoted name would hide its
# suffix), read WHOLE by python — never a pipe that `grep -q` closes at the first match,
# after which git dies on SIGPIPE unnoticed (round 22); a listing git could not take
# is a count of "unreadable", and the row is RED
KEYF=$(mktemp); KEYN=unreadable
if git ls-files -z > "$KEYF" 2>/dev/null; then
  KEYN=$(python3 -c 'import re, sys; print(sum(1 for n in open(sys.argv[1], "rb").read().split(b"\0") if n and re.search(rb"age\.key|deploy-key$|\.tfstate", n)))' "$KEYF" 2>/dev/null || echo unreadable)
fi; rm -f "$KEYF"
check "no key material tracked in git" '[ "$KEYN" = "0" ]'

echo "== certificates =="
for i in letsencrypt-dns letsencrypt-http; do
  check "ClusterIssuer $i ready" \
    '[ "$(kubectl get clusterissuer "$i" -o jsonpath="{.status.conditions[?(@.type==\"Ready\")].status}" 2>/dev/null | awk '"'"'NR==1 && /^True$/{print "True"}'"'"')" = "True" ]'
done
check "wildcard Certificate ready" \
  '[ "$(kubectl -n traefik get certificate "$WILDCARD" -o jsonpath="{.status.conditions[?(@.type==\"Ready\")].status}")" = "True" ]'
# The issued names as a browser would see them: read from the leaf the edge serves
# in the handshake below, never from beside the certificate. An annotation on the
# Secret is metadata any operator can edit, so it stays green against a certificate
# replaced without touching it — which is the substitution this row exists to catch.
# No Secret is read anywhere in this section.
# The SAN list is the server's text: a private file (never a shell variable a traced
# shell prints); only the three EXPECTED names are looked up in it, nothing is read out.
SANF=$(mktemp)
{ printf ','; echo | timeout 15 openssl s_client -connect "$IP:443" -servername "probe.$ORBIT_DOMAIN" 2>/dev/null \
  | openssl x509 -noout -ext subjectAltName 2>/dev/null | grep -o 'DNS:[^,[:space:]]*' 2>/dev/null | tr '\n' ','; } > "$SANF"
for d in "$ORBIT_DOMAIN" "*.$ORBIT_DOMAIN" "*.$ORBIT_DEV_DOMAIN"; do
  # Comma-boundary match, not substring: "*.example.com" must not satisfy the
  # apex row. Only the DNS: tokens are extracted and joined onto one
  # comma-separated line — that join is what makes the comma-boundary match
  # work, since openssl wraps a long SAN list across several lines.
  grep -qF ",DNS:$d," "$SANF" && ok "SAN $d present" || bad "SAN $d MISSING"
done
rm -f "$SANF"

echo "== edge =="
# The critical one: a hostname with no Ingress must still get the real certificate.
# Traefik's self-signed default against a one-year HSTS pin is an unrecoverable
# browser error at any cutover, so this is the check that makes cutovers survivable.
# The issuer line is the server's text: a private file (never a shell variable a traced
# shell prints), and not repeated in the row.
# A "real" certificate is one the system trust store VERIFIES for that hostname, not one
# whose issuer line merely says Let's Encrypt (any self-signed cert can say so); the
# chain verdict is openssl's own `Verify return code` line, read from the private file.
LEAFF=$(mktemp); CHAINF=$(mktemp)
echo | timeout 15 openssl s_client -connect "$IP:443" -servername "probe.$ORBIT_DOMAIN" -verify_hostname "probe.$ORBIT_DOMAIN" > "$CHAINF" 2>/dev/null
openssl x509 -noout -issuer < "$CHAINF" > "$LEAFF" 2>/dev/null
if grep -q "Let's Encrypt" "$LEAFF" && grep -q '^Verify return code: 0 (ok)$' "$CHAINF"; then
  ok "unrouted host served a real LE cert that VERIFIES for its name (TLSStore default works)"
elif grep -q "Let's Encrypt" "$LEAFF"; then
  bad "unrouted host served a certificate that names Let's Encrypt as issuer but does NOT verify for probe.$ORBIT_DOMAIN (chain, hostname or validity — openssl's verdict is not repeated) — not a real LE certificate"
else
  bad "unrouted host served $([ -s "$LEAFF" ] && echo "a certificate whose issuer is not Let's Encrypt" || echo "no readable certificate") (expected Let's Encrypt)"
fi
rm -f "$LEAFF" "$CHAINF"
# readyReplicas is read into a private file and compared as an integer: `grep -q 1`
# matched 11 and missed 2, and the API's text was a value in a traced comparison (round 23)
TRF=$(mktemp); kubectl -n traefik get deploy traefik -o jsonpath="{.status.readyReplicas}" > "$TRF" 2>/dev/null
TRN=$(python3 -c 'import re, sys; v = open(sys.argv[1], errors="replace").read().strip(); print(v if re.fullmatch(r"[0-9]{1,6}", v) else "unreadable")' "$TRF" 2>/dev/null || echo unreadable); rm -f "$TRF"
check "traefik pod running" '[ "$TRN" != unreadable ] && [ "$TRN" -gt 0 ]'
# Assert the behaviour (a PERMANENT redirect to the https form of the SAME host), not
# a specific status code: Traefik answers 301 to GET and 308 to HEAD here, and both are
# correct. 302/307 are temporary (a client may keep speaking http), and a Location on
# another host is a redirect away from the surface, not to its https form.
# The response headers are the edge's text (a Set-Cookie among them is a session
# credential): they land in a private file, never in a shell variable a traced shell
# would print, and a python reader hands the shell ONE validated fact.
RHDR=$(mktemp)
# a total deadline: a node that accepts the connection and never finishes the response
# would otherwise hold this row (and the stage) open for good (round 24)
curl -s --max-time 15 -D "$RHDR" -o /dev/null --resolve "probe.$ORBIT_DOMAIN:80:$IP" "http://probe.$ORBIT_DOMAIN" >/dev/null 2>&1
REDIRV=$(python3 - "$RHDR" "probe.$ORBIT_DOMAIN" <<'PY'
import re, sys
try:
    lines = open(sys.argv[1], encoding="utf-8", errors="replace").read().replace("\r", "").splitlines()
except OSError:
    lines = []
host = re.escape(sys.argv[2])
status = re.match(r"HTTP/[0-9.]+ ([0-9]{3})", lines[0] if lines else "")
code = status.group(1) if status else "unreadable"
same = any(re.fullmatch(r"location:\s*https://" + host + r"(:443)?(/.*)?", l, re.I) for l in lines[1:])
print("PERMANENT-SAME-HOST" if code in ("301", "308") and same else code)
PY
)
rm -f "$RHDR"
if [ "$REDIRV" = PERMANENT-SAME-HOST ]; then
  ok "http permanently redirects to https on the same host"
else
  bad "http did not permanently redirect to https on the same host (status $REDIRV; the Location is not repeated)"
fi
check "6443 still closed from the internet" '! timeout 5 bash -c '"'"'cat < /dev/null > "/dev/tcp/$ORBIT_NODE_IP/6443"'"'"''

echo "== live public surfaces =="
# Catalog-driven: every prod hostname the generator actually renders must answer.
# That is scripts/catalog-sync.py env_hostnames(pid, "prod", ...) with the
# instance's own domains as defaults: an omitted prod `hostnames` defaults to
# `<id>.<primary>`, never to the bare primary apex. docs/ONBOARDING.md records
# why: the primary apex itself has no record; only project hostnames under the
# wildcard are served. An explicit apex in the catalog is still probed (it is a
# hostname the generator renders); an empty catalog has no project surface and
# the section keys off. A surface pinned outside the catalog stays the
# instance's own stage check, not core's.
#
# A function rather than an inline heredoc so the stage can ALSO run it against a
# deliberately malformed catalog below and prove it refuses. It prints one host per
# line and exits non-zero, with a reason on stderr, on any shape the generator would
# not accept — silence is never a valid answer to "what is exposed".
catalog_hosts() { # <catalog path> -> one prod hostname per line
  _CATALOG_SYNC="$(cd "$(dirname "$0")/.." && pwd)/catalog-sync.py"
  python3 - "$1" "$_CATALOG_SYNC" <<'PY'
import importlib.util
import os
import sys
import yaml
cat_path = sys.argv[1]
gen_path = sys.argv[2]
try:
    spec = importlib.util.spec_from_file_location("catalog_sync", gen_path)
    if spec is None or spec.loader is None:
        sys.exit("cannot load generator at %s" % gen_path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
except SystemExit:
    raise
except Exception as e:
    sys.exit("cannot load generator at %s: %s" % (gen_path, e))
domain = os.environ.get("ORBIT_DOMAIN") or ""
dev_domain = os.environ.get("ORBIT_DEV_DOMAIN") or ""
if not domain:
    sys.exit("ORBIT_DOMAIN is not set — lib-instance.sh did not export the primary domain")
defaults = {"domain": domain, "devDomain": dev_domain or domain, "registry": "stub"}
try:
    with open(cat_path) as fh:
        cat = yaml.safe_load(fh)
except Exception as e:
    # The parser's message quotes the document; the type says enough.
    sys.exit("catalog %s did not parse (%s)" % (cat_path, type(e).__name__))
if not isinstance(cat, dict):
    sys.exit("catalog %s is not a mapping" % cat_path)
if "projects" not in cat:
    sys.exit("catalog %s has no 'projects' key" % cat_path)
projects = cat["projects"]
if projects is None:
    sys.exit("catalog %s 'projects' is null" % cat_path)
if not isinstance(projects, list):
    sys.exit("catalog %s 'projects' is not a list" % cat_path)
# Every refusal below names the file, the entry and the field, never the value: a catalog
# entry can carry an env block or a pasted token, and this text lands in a failing row.
hosts = []
for n, p in enumerate(projects, 1):
    if p is None:
        sys.exit("catalog %s has a null project entry (entry %d)" % (cat_path, n))
    if not isinstance(p, dict):
        sys.exit("catalog %s has a non-mapping project entry (entry %d)" % (cat_path, n))
    pid = p.get("id")
    if not isinstance(pid, str) or not pid:
        sys.exit("catalog %s has a project with no id (entry %d)" % (cat_path, n))
    if not mod.ID_RE.fullmatch(pid):
        sys.exit("catalog %s entry %d: the project id is not an id (letters, digits, hyphens)" % (cat_path, n))
    # The same predicate scripts/catalog-sync.py uses to decide whether it RENDERS
    # anything: an externally-hosted or elsewhere-managed project only RESERVES its
    # hostnames (so nobody else can claim them) — this cluster serves no Ingress for
    # it, so asserting that it answers here would assert somebody else's uptime.
    if p.get("hosted", "self") != "self" or p.get("managed_elsewhere"):
        continue
    if "environments" not in p or p["environments"] is None:
        continue
    envs = p["environments"]
    if not isinstance(envs, dict):
        sys.exit("%s: environments is not a mapping" % pid)
    if "prod" not in envs:
        continue
    envcfg = envs["prod"]
    if envcfg is None:
        envcfg = {}
    if not isinstance(envcfg, dict):
        sys.exit("%s: prod environment is not a mapping" % pid)
    raw_hosts = envcfg.get("hostnames")
    if raw_hosts is not None and not isinstance(raw_hosts, list):
        sys.exit("%s: hostnames is not a list" % pid)
    for i, h in enumerate(raw_hosts or [], 1):
        if not isinstance(h, str) or not h.strip():
            sys.exit("%s: hostnames entry %d is not a hostname string" % (pid, i))
    # customDomains is a LIST OF MAPPINGS ({host: ..., dns: ...}), which is the shape
    # scripts/catalog-sync.py has always read (`c["host"]`). Reading it as a list of
    # plain strings is what crashed this enumerator and, because the crash left HOSTS
    # empty, silently keyed the whole section off (found 2026-08-29).
    raw_cd = envcfg.get("customDomains")
    if raw_cd is not None and not isinstance(raw_cd, list):
        sys.exit("%s: customDomains is not a list" % pid)
    for i, c in enumerate(raw_cd or [], 1):
        if not isinstance(c, dict):
            sys.exit("%s: customDomains entry %d must be a mapping with a host: key" % (pid, i))
        h = c.get("host")
        if not isinstance(h, str) or not h.strip():
            sys.exit("%s: customDomains entry %d has no usable host" % (pid, i))
    try:
        phosts = mod.env_hostnames(pid, "prod", envcfg, defaults)
    except SystemExit:
        raise
    except Exception as e:
        sys.exit("%s: could not enumerate prod hostnames (%s)" % (pid, type(e).__name__))
    for i, h in enumerate(phosts, 1):
        if "*" in h:
            sys.exit("%s/prod: hostname %d carries a wildcard, which is not allowed" % (pid, i))
        if not mod.HOST_RE.fullmatch(h):
            sys.exit("%s/prod: hostname %d is not a DNS name" % (pid, i))
    # the same door path the generator's Probe requests (catalog-sync door_path): a
    # project with no route at `/` is checked where it does answer
    dpath = mod.door_path(p.get("components") or [])
    hosts.extend(h + dpath for h in phosts)
# Dedup, keeping declaration order: hosts are already normalised
# (lowercased, trailing dot stripped) by the generator, so equivalent spellings
# are one surface and are probed once.
seen = set()
for h in hosts:
    if h not in seen:
        seen.add(h)
        print(h)
PY
}
# Fail CLOSED. The enumerator's exit status is captured and consulted BEFORE the empty
# case is considered, so "no hostnames declared" is only reachable from a successful,
# empty enumeration — never from a crash. This is the fix for the fail-open above: a
# TypeError used to reach the `ok` branch and report the surface checks as keyed off.
HERR=$(mktemp)
HOSTS=$(catalog_hosts catalog/projects.yaml 2>"$HERR"); HRC=$?
if [ "$HRC" -ne 0 ]; then
  bad "could not enumerate prod hostnames from catalog/projects.yaml (rc=$HRC: $(tr '\n' ' ' < "$HERR" | cut -c1-160)) — catalog surfaces UNRUN, not passed"
  HOSTS=""
elif [ -z "$HOSTS" ]; then
  ok "no exposed prod hostnames declared — surface checks keyed off (arm with the first project)"
fi
if [ -n "$HOSTS" ]; then
  while IFS= read -r h; do
    [ -n "$h" ] || continue
    C=$(curl -s -o /dev/null -w '%{http_code}' --max-time 15 "https://$h")
    case "$C" in 200|30*|401) ok "$h answers ($C)";; *) bad "$h returned $C";; esac
  done <<<"$HOSTS"
fi
rm -f "$HERR"
# The oracle for the oracle: prove the enumerator REFUSES rather than returning nothing.
# Without this, re-introducing the fail-open is invisible — an empty result and a crashed
# result look identical from the outside, which is exactly how this hid.
HBAD=$(mktemp -d)/catalog.yaml
printf 'projects:\n  - id: broken\n    environments:\n      prod:\n        customDomains: [tenant.example.com]\n' > "$HBAD"
if catalog_hosts "$HBAD" >/dev/null 2>&1; then
  bad "the host enumerator ACCEPTED a malformed catalog — a crash here keys the surface checks off instead of failing"
else
  ok "the host enumerator refuses a malformed catalog (surface checks fail closed, not open)"
fi
rm -rf "$(dirname "$HBAD")"

echo
echo "stage2: $PASS passed, $FAIL failed"
exit $((FAIL > 0))
