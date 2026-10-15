# Sourced by verify stages and operational scripts. Exports the instance's identity
# from instance/config.yaml so no script carries an instance string of its own
# (extraction, 2026-08-25). Values can be overridden from the environment, which is
# what lets a sandboxed fixture point the same scripts at a fictional instance.
_ORBIT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# Workstation path overrides from the environment are validated BEFORE any expansion: a
# traced shell prints every export and every argument, and a stage prints KUBECONFIG in
# its pre-boot line, so a value that is not an absolute path is refused by NAME here and
# never reaches an export, an argv or a row (round 23; ORBIT_INSTANCE_CONFIG too, round 24 —
# it is expanded on the very next line). KUBECONFIG is a colon-separated
# list the way kubectl reads it; each entry must be an absolute path. Read from the
# environment inside the program, never on its command line.
python3 - <<'PY' || { echo "a workstation path override did not load" >&2; return 2 2>/dev/null || exit 2; }
import os, re, sys
PATH = re.compile(r"/[^\x00-\x1f\x7f:]{0,4095}")
for name, many in (("ORBIT_INSTANCE_CONFIG", False), ("ORBIT_CONFIG_DIR", False), ("KUBECONFIG", True)):
    v = os.environ.get(name)
    if not v:
        continue
    for i, p in enumerate(v.split(":") if many else [v], 1):
        if not PATH.fullmatch(p):
            sys.exit(f"{name}{' entry #%d' % i if many else ''} is not an absolute filesystem path "
                     f"(one line, no control characters, no colon, 4096 at most; the content is not repeated)")
PY
_ORBIT_INSTANCE="${ORBIT_INSTANCE_CONFIG:-$_ORBIT_ROOT/instance/config.yaml}"

# Each value is emitted as a single-quoted shell word (shlex.quote), so a config value is
# data to the shell and never code: `$(...)`, backticks and `$VAR` inside it stay literal.
# An environment override wins, and is checked and canonicalised like the file's value;
# the loader exports the effective value outright.
# Values that a stage hands to a URL, an address, a path or a program are checked for
# SHAPE here, once, before any stage sees them: a domain is a DNS name, an address is an
# IP address, the instance name is a label. A value of the wrong shape stops the sourcing
# script with the FIELD named, never the value (a wrong value may be a pasted credential).
# The check covers an environment override too: an override is data of the same kind.
# The loader's output is captured first and evaluated second: a Python that dies
# (a config that is not UTF-8, say) leaves no refusal to evaluate, and an eval of
# nothing succeeds — so the process status is the gate, not the text.
_ORBIT_LOADED="$(python3 - "$_ORBIT_INSTANCE" <<'PY'
import ipaddress, os, re, shlex, sys, yaml
DNS = re.compile(r"(?i)[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?(\.[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?)+")
LABEL = re.compile(r"[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?")
# The repository URL reaches `git clone` with the minted GitHub App credential attached,
# so it is held to the two GitHub forms and nothing else: no userinfo, no other origin.
OWNER = re.compile(r"[A-Za-z0-9]([A-Za-z0-9-]{0,37}[A-Za-z0-9])?")
REPO = re.compile(r"(git@github\.com:|https://github\.com/)[A-Za-z0-9]([A-Za-z0-9-]{0,37}[A-Za-z0-9])?/[A-Za-z0-9._-]{1,100}")
REGISTRY = re.compile(r"ghcr\.io/[a-z0-9]([a-z0-9-]{0,37}[a-z0-9])?")
PROVIDER = re.compile(r"hetzner|owned")
def refuse(why):
    print(f"echo {shlex.quote('instance/config.yaml: ' + why)} >&2; return 2 2>/dev/null || exit 2")
    sys.exit(0)
try:
    c = yaml.safe_load(open(sys.argv[1], encoding="utf-8"))
except (OSError, ValueError, yaml.YAMLError) as exc:   # ValueError: not UTF-8
    refuse(f"cannot be read as YAML ({type(exc).__name__})")
if not isinstance(c, dict):
    refuse("is not a YAML map")
def field(*keys):
    node = c
    for k in keys:
        node = node.get(k) if isinstance(node, dict) else None
    if node is None:
        refuse(f"{'.'.join(keys)} is missing")
    return node
out = []                                              # printed only when every value passed
def export(name, value, shape=None, what=""):
    # The effective value (an override wins), CANONICAL: surrounding whitespace and a
    # DNS name's trailing dot are dropped before the shape check and the export alike,
    # so what a stage puts in a URL is exactly what passed.
    value = os.environ.get(name, str(value)).strip()
    if shape is DNS:
        value = value.rstrip(".").lower()    # the same canonical form instance-init renders
        if len(value) > 240:                 # instance-init's DOMAIN_MAX: derived object names stay within 253
            refuse(f"{what} is longer than 240 characters (the names derived from it would not be Kubernetes names)")
    if name == "ORBIT_DNS_ZONES":
        value = " ".join(w.rstrip(".").lower() for w in value.split())
    if shape is not None and not shape.fullmatch(value):
        refuse(f"{what} is not {SHAPE_NAME.get(shape, 'a space-separated list of DNS names')}")
    # exported OUTRIGHT, not as `${NAME:-value}`: the override was already applied and
    # canonicalised above, and a padded override must not win back its padding
    out.append(f"export {name}={shlex.quote(value)}")
def ip(value, what):
    value = str(value).strip()                        # canonical, like every export
    # a zone/scope suffix (`fe80::1%eth0`) is accepted by the parser and would ride into
    # ssh and curl argv unshaped: an address here is the plain address, nothing after it
    if "%" in value or "/" in value:
        refuse(f"{what} is not a plain IP address (no zone, scope or prefix suffix)")
    try:
        ipaddress.ip_address(value)
    except ValueError:
        refuse(f"{what} is not an IP address")
    return value
SHAPE_NAME = {DNS: "a DNS name", LABEL: "a lowercase label (letters, digits, dashes)",
              None: "of any shape",
              OWNER: "a GitHub owner name (letters, digits, dashes)",
              REPO: "a GitHub repository URL (git@github.com:owner/repo.git or https://github.com/owner/repo.git)",
              REGISTRY: "of the form ghcr.io/<owner>",
              PROVIDER: "hetzner or owned"}
try:
    export("ORBIT_INSTANCE_NAME", field("instance", "name"), LABEL, "instance.name")
    export("ORBIT_DOMAIN", field("domains", "primary"), DNS, "domains.primary")
    export("ORBIT_DEV_DOMAIN", field("domains", "dev"), DNS, "domains.dev")
    zones = field("domains", "dnsZones")
    if not isinstance(zones, list) or not all(isinstance(z, str) and DNS.fullmatch(z.strip().rstrip(".")) for z in zones):
        refuse("domains.dnsZones is not a list of DNS names")
    # the environment override is applied INSIDE export() and must meet the same shape:
    # a space-separated list of DNS names, each canonical (lowercase, no trailing dot)
    _D = DNS.pattern.replace("(?i)", ""); ZONES = re.compile(rf"{_D}( {_D})*", re.I)
    export("ORBIT_DNS_ZONES", " ".join(z.strip().rstrip(".").lower() for z in zones), ZONES, "ORBIT_DNS_ZONES")
    export("ORBIT_REGISTRY", field("registry"), REGISTRY, "registry")
    export("ORBIT_REPO_URL", field("git", "repoUrl"), REPO, "git.repoUrl")
    export("ORBIT_GITHUB_ORG", field("git", "github_org"), OWNER, "git.github_org")
    nodes = c.get("nodes") or []
    if not isinstance(nodes, list) or not all(isinstance(n, dict) for n in nodes):
        refuse("nodes is not a list of maps")
    # an override in the environment is validated whether or not the file lists a node:
    # with `nodes: []` an unshaped ORBIT_NODE_IP would otherwise ride through untouched
    first = nodes[0] if nodes else {}
    if nodes or "ORBIT_NODE_IP" in os.environ:
        export("ORBIT_NODE_IP", ip(os.environ.get("ORBIT_NODE_IP", first.get("ip", "")), "nodes[0].ip"))
    if nodes or "ORBIT_NODE_NAME" in os.environ:
        export("ORBIT_NODE_NAME", first.get("name", ""), LABEL, "nodes[0].name")
    # THE CONTROL-PLANE NODE, by ROLE and not by position: a verify row that
    # compared against nodes[0] would silently compare against a worker on the day an org
    # grows one.
    cp = [n for n in nodes if n.get("role") == "control-plane"] or nodes[:1]
    if cp or "ORBIT_CONTROL_PLANE_IP" in os.environ:
        export("ORBIT_CONTROL_PLANE_IP", ip(os.environ.get("ORBIT_CONTROL_PLANE_IP", cp[0].get("ip", "") if cp else ""), "the control-plane node's ip"))
    # The control-plane node's provider, optional: absent means the default.
    # An override wins and meets the same shape; anything else is refused by
    # field, never the value.
    if cp or "ORBIT_NODE_PROVIDER" in os.environ:
        _raw_provider = cp[0].get("provider", "hetzner") if cp else "hetzner"
        if _raw_provider is None:
            _raw_provider = "hetzner"
        export("ORBIT_NODE_PROVIDER", _raw_provider, PROVIDER, "the control-plane node's provider")
except SystemExit:
    raise
except Exception as exc:                 # noqa: BLE001 — a crash is a refusal, never a traceback
    refuse(f"could not be loaded ({type(exc).__name__})")
print("\n".join(out))
PY
)" || { echo "instance/config.yaml: the loader did not run to completion" >&2; return 2 2>/dev/null || exit 2; }
eval "$_ORBIT_LOADED" || return 2 2>/dev/null || exit 2
unset _ORBIT_LOADED

# Workstation-side credential pointers (never in the cluster, never in this repo's
# config — paths only, per the standing custody rule).
export ORBIT_CONFIG_DIR="${ORBIT_CONFIG_DIR:-$HOME/.config/${ORBIT_INSTANCE_NAME}-platform}"
# The ssh-key POINTER names a path, and a stage puts that path on an ssh command line.
# It is read as data (an override too) and exported only once it has a path's shape: one
# line, absolute, no control characters, 4096 at most. Anything else is refused by NAME,
# never repeated (a pasted key body is the obvious wrong content). A missing pointer
# means the default key, as before.
_ORBIT_SSH_KEY=$(python3 - "$ORBIT_CONFIG_DIR/ssh-key-path" "$HOME/.ssh/id_ed25519" <<'PY'
import os, re, sys
# the override is read from the environment HERE, never put on this command line: a
# traced shell (bash -x) prints arguments, and a pasted key body must not be one
pointer, default, override = sys.argv[1], sys.argv[2], os.environ.get("ORBIT_SSH_KEY", "")
what = "ORBIT_SSH_KEY" if override else "ssh-key-path (under ORBIT_CONFIG_DIR)"
if override:
    value = override
else:
    try:
        with open(pointer, encoding="utf-8") as f:
            value = f.read(4097)
    except FileNotFoundError:
        value = default
    except Exception as exc:                 # noqa: BLE001
        sys.exit(f"{what} cannot be read ({type(exc).__name__})")
value = value.strip()
if not value or len(value) > 4096 or not re.fullmatch(r"/[^\x00-\x1f\x7f]*", value):
    sys.exit(f"{what} is not a filesystem path (one line, absolute, no control characters; the content is not repeated)")
print(value)
PY
) || { echo "the ssh key path did not load" >&2; return 2 2>/dev/null || exit 2; }
export ORBIT_SSH_KEY="$_ORBIT_SSH_KEY"
unset _ORBIT_SSH_KEY
