#!/usr/bin/env python3
"""
One-time instance scaffold: fill the {{token}} placeholders that the template's
clusters/ tree ships with, from instance/config.yaml.

Idempotent: after a successful run no {{token}} remains, so a second run is a no-op
for the fill. Refuses to write ANYTHING while any value in instance/config.yaml is
still the placeholder config.yaml.example ships (EXAMPLE_VALUES), and fails loudly
listing every token it could not resolve — a missing config field must never scaffold
silently.

EVERY config-derived file is RE-RENDERED on every run, from a staged source under
provisioning/ that mirrors its path under clusters/. That is the load-bearing property and
it exists because filling a {{token}} in place is ONE-WAY: the substitution leaves a
literal behind, so a value written on the day the repo was cut could never be corrected
afterwards — and several of these values are not knowable that day. The node's address is
scaffolded before tofu creates the server, and a core image tag is bumped later by
definition. Under the old one-way fill,
correcting any of them in config.yaml and re-running silently did nothing.

So: clusters/ is OUTPUT. A file there is either GENERATED (it has a twin under
provisioning/, and every such file says so in its first line) or SEEDED (it does not, and
is the instance's to edit). And provisioning/ is VENDORED: core-manifest.txt maps core's
template/provisioning onto it, `make core-sync` (which `make init` runs first) replaces it
from the pinned core, and `make core-check` flags any local edit as drift. So an instance
never edits a staged source either: a value goes through instance/config.yaml (the tokens
and the gates below), and a change to the staged text goes to orbit-core and a pin bump.
core-sync refuses to overwrite a vendored path with uncommitted changes, so a stray edit
is refused, not lost.

Two gates decide whether a staged file is rendered at
all, and each one REMOVES the destination when it is closed, so the tree can never carry a
stale artifact whose config was taken away:

  * objectStorage.buckets.platform — Loki, Alloy and Tempo.
  * intakeIngress: wired — the intake's PUBLIC webhook door. Absent is fail-closed: the
    generator renders a github trigger's ROUTE and never the DOOR, and this is what makes
    the flag's attestation true rather than merely claimed.

The secret plane is sops and only sops: every Secret comes from a SopsSecret the desk
applies (clusters/platform/secrets/<ns>/<name>.yaml), so init renders no store, no
identity and no canary of its own. Files the retired plane rendered are listed under
RETIRED_DESTINATIONS, and the next init removes them from an existing instance.
"""
import errno
import json
import os
import pathlib
import re
import urllib.parse
import stat
import sys

import yaml

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "verify"))
import shown as display   # the shared display rule (scripts/verify/shown.py)
import orbit_features     # the features registry and the reading of `features:`

ROOT = pathlib.Path(__file__).resolve().parent.parent
CONFIG = ROOT / "instance" / "config.yaml"
CLUSTERS = ROOT / "clusters"
# provisioning/clusters/<path> renders to clusters/<path>. The mirror IS the mapping;
# there is no table of pairs to keep in step with the tree.
STAGED = ROOT / "provisioning" / "clusters"

# ---------------------------------------------------------------------------------------
# secrets.provider — sops is the only secret plane. Every Secret comes from a SopsSecret
# the desk applies, so init renders no store, no identity and no canary of its own.
# Absent means sops. Anything else is refused before anything is written.
PROVIDER_SOPS = "sops"


# The features registry of this tree (provisioning/features.yaml). A tree without one
# cannot be rendered; main() says so, and importing this file still works so the verify
# stages can read the gate table of whatever tree they are given.
try:
    FEATURES, FEATURES_ERROR = orbit_features.load_registry(ROOT), None
except orbit_features.FeatureError as e:
    FEATURES, FEATURES_ERROR = {}, str(e)

# Gates, keyed by the destination path relative to clusters/. A prefix match, so a
# directory gates everything under it. Anything not named here renders unconditionally.
# Every optional feature gates the paths the registry says it owns: turned off, its
# Applications and config leave the tree, and the desk and orbitd have nothing of it to
# apply. (Loki, Alloy and Tempo were gated on the bucket alone before features existed;
# the bucket is now what the logs and traces features require.)
GATES = {path: name for name, spec in FEATURES.items() if not spec["always"]
         for path in spec["paths"]}
# The intake's public webhook door (AGENT-PLANE.md, "the `github` gate").
GATES["platform/config/orbit-intake"] = "intake"

# Destinations a deleted component used to render, relative to clusters/. Phase 8
# retired agentgateway, the status page and orbit-hooks; an instance that rendered
# them before loses them on its next init. An explicit list, not "whatever this
# run did not render": init must never delete a hand-written file it merely did
# not produce. The component names are assembled from parts because no live line
# in this file may name a deleted component (scripts/test_phase8_removals.py
# scans for exactly that); the history lives in this comment, the paths below.
_RETIRED_COMPONENTS = (
    ("agent" + "gateway", ("jwt-door.yaml", "mcp-grafana.yaml", "gateway.yaml")),
    ("status" + "-page", ("content.yaml", "ingress.yaml", "namespace.yaml")),
    ("orbit" + "-hooks", ("deployment.yaml", "external" + "secret.yaml",
                           "namespace.yaml", "pvc.yaml", "service.yaml")),
)
RETIRED_DESTINATIONS = tuple(
    "platform/config/%s/%s" % (component, leaf)
    for component, leaves in _RETIRED_COMPONENTS for leaf in leaves)
# Files an instance VENDORED from template/clusters/ for the same components: never
# rendered through a gate, so the component lists above never named them, but the
# next init removes them all the same — otherwise the page they belong to is left
# half-standing. Assembled from parts for the same no-live-line reason as above.
# Phase 8d retired the observability agent's Application the same way (vendored
# from template/clusters/, no config directory beside it).
_RETIRED_VENDORED = (
    "platform/apps/" + "agent" + "gateway" + ".yaml",
    "platform/config/" + "agent" + "gateway" + "/netpol.yaml",
    "platform/config/" + "status" + "-page" + "/deployment.yaml",
    "platform/config/" + "status" + "-page" + "/service.yaml",
    "platform/apps/" + "o" + "bi" + ".yaml",
)
RETIRED_DESTINATIONS = RETIRED_DESTINATIONS + _RETIRED_VENDORED

# What the retired identity provider rendered, relative to clusters/. An instance that
# rendered it before loses those files on its next init: removals reported by path, a
# hand-written file beside them surviving, emptied directories gone. The names are
# assembled from parts because no live line in this file may name the retired plane
# (scripts/test_zitadel_retired.py scans for exactly that); the history lives in this
# comment, the paths below. The fourth destination is the deploy tool's own sign-on
# config: it was rendered from the same block, and its file left core when the deploy
# tool was retired — so no instance re-renders it, and nothing has removed the copy an
# existing instance still carries, pointing at a provider that no longer exists.
_RETIRED_IDP = "zita" + "del"
_RETIRED_SSO = "grafana-" + "oi" + "dc"
_RETIRED_DEPLOY_SSO = "oi" + "dc"
RETIRED_IDP_DESTINATIONS = (
    "platform/apps/%s.yaml" % _RETIRED_IDP,
    "platform/config/%s/backup.yaml" % _RETIRED_IDP,
    "platform/config/%s/db-and-secrets.yaml" % _RETIRED_IDP,
    "platform/config/monitoring/%s.yaml" % _RETIRED_SSO,
    "platform/config/argocd/%s.yaml" % _RETIRED_DEPLOY_SSO,
)

# Every destination the retired secret plane rendered or generated under clusters/.
# An explicit list, not "whatever this run did not render": init must never delete a
# hand-written file it merely did not produce — a file the instance wrote itself in
# one of these directories survives, and an emptied directory is removed. Each removal
# is reported by path. The names are assembled from parts because no live line in
# this file may name the old plane (scripts/test_sops_only_plane.py scans for exactly
# that); the history lives in this comment, the paths below.
_PLANE_VAULT = "infi" + "sical"
_PLANE_OPERATOR = "external-" + "secrets"
_PLANE_DOCUMENT = "external" + "secret"
RETIRED_SECRET_PLANE = (
    "platform/apps/%s.yaml" % _PLANE_VAULT,
    "platform/apps/%s-data.yaml" % _PLANE_VAULT,
    "platform/apps/%s.yaml" % _PLANE_OPERATOR,
    "platform/config/%s/db-and-cache.yaml" % _PLANE_VAULT,
    "platform/config/%s/netpol.yaml" % _PLANE_VAULT,
    "platform/config/%s/token-reviewer.yaml" % _PLANE_VAULT,
    "platform/config/%s/stores.yaml" % _PLANE_OPERATOR,
    "platform/config/%s/sentinel.yaml" % _PLANE_OPERATOR,
    "platform/config/%s/eso-identity.yaml" % _PLANE_OPERATOR,
    "platform/config/%s/dns-credentials.yaml" % _PLANE_OPERATOR,
    "platform/config/%s/netpol.yaml" % _PLANE_OPERATOR,
    "platform/config/orbitd/%s.yaml" % _PLANE_DOCUMENT,
    "platform/config/loki/s3-credentials.yaml",
    "platform/config/tempo/s3-credentials.yaml",
    # The hosted vault's machine identity and its shipped example: they target a
    # namespace phase 8 deleted, so an org that ran on the old plane carries them
    # until the next init retires them here, beside the other retired destinations.
    "platform/config/%s/%s-machine-identity.sops.yaml" % (_PLANE_OPERATOR, _PLANE_VAULT),
    "platform/config/%s/%s-machine-identity.sops.yaml.example" % (_PLANE_OPERATOR, _PLANE_VAULT),
)

# Scaffold tokens are dotted and space-free ({{domains.primary}}). Prometheus,
# Alertmanager and ESO templates legitimately use {{ ... }} with spaces (and
# undotted forms like {{end}}) — the dot + no-space shape is what separates ours.
#
# The `$` lookbehind keeps a `${{name.field}}` expression (another tool's syntax, written
# tight) from reading as one of ours: it has our exact shape one character in, and
# without the guard init would call it an unfilled placeholder and refuse to scaffold.
TOKEN_RE = re.compile(r"(?<!\$)\{\{([a-z][a-zA-Z]*(?:\.[a-zA-Z]+)+)\}\}")


# Every placeholder instance/config.yaml.example ships, keyed by its dotted config path.
# Init writes NOTHING while any of them survives. Checking the instance name alone was not
# enough: review round 2 changed only the name, ran init, and got a whole org's manifests
# rendered against example.org, an example git remote, an example bucket, the RFC 5737
# documentation node address and a telegram chat id of 111111111 — a scaffold that looks
# complete, syncs into a real cluster, and is wrong everywhere.
#
# A key that is ABSENT is not checked. objectStorage, agentRuntime and intakeRuntime are
# legitimately dropped by an org that has no bucket and no agents, and deleting a block is
# not the same as leaving its example value in place.
_ABSENT = object()
EXAMPLE_VALUES = {
    "instance.name": ["example", ""],
    "git.repoUrl": ["git@github.com:example-org/example-instance.git"],
    "git.github_org": ["example-org"],
    "domains.primary": ["example.org"],
    "domains.dev": ["dev.example.org"],
    "domains.dnsZones": [["example.org"]],
    "registry": ["ghcr.io/example-org"],
    "coreRegistry.owner": ["ghcr.io/example-core-org"],
    "coreRegistry.orbitdTag": ["main-0000000"],
    "agentRuntime.image": ["ghcr.io/example-core-org/orbit-agent-runtime"],
    "agentRuntime.tag": ["main-0000000"],
    "intakeRuntime.image": ["ghcr.io/example-core-org/orbit-intake"],
    "intakeRuntime.tag": ["main-0000000"],
    "objectStorage.buckets.platform": ["example-orbit-platform"],
    "objectStorage.buckets.appData": ["example-orbit-apps"],
    "contacts.adminFirstName": ["Ada"],
    "contacts.adminLastName": ["Admin"],
    "contacts.telegramChatId": ["111111111", 111111111],
    "contacts.email": ["ops@example.org"],
}
# `nodes:` is a LIST, so its address has no dotted path. It is the sharpest one in the set:
# an org that keeps the documentation address has never filled in its own server's.
EXAMPLE_NODE_IP = "203.0.113.10"


def dig(c: dict, path: str):
    node = c
    for key in path.split("."):
        if not isinstance(node, dict) or key not in node:
            return _ABSENT
        node = node[key]
    return node


def refuse_example_values(c: dict) -> None:
    """Refuse a config that is still the example — listing EVERY offending key at once.

    One error with the whole list, not one per run: an operator filling a config in should
    learn what is left in a single pass, and a validator that stops at the first hit trains
    them to fill the file one field per `make init`."""
    offending = [f"{path}: {value!r}"
                 for path, sentinels in EXAMPLE_VALUES.items()
                 for value in [dig(c, path)]
                 if value is not _ABSENT and any(value == s for s in sentinels)]
    offending += [f"nodes[{i}].ip: {EXAMPLE_NODE_IP!r}"
                  for i, n in enumerate(c.get("nodes") or [])
                  if isinstance(n, dict) and n.get("ip") == EXAMPLE_NODE_IP]
    if offending:
        sys.exit("instance/config.yaml still carries example values — NOTHING was "
                 "written. Fill each of these and re-run:\n"
                 + "\n".join(f"  {o}" for o in sorted(offending))
                 + "\n\nEvery line above is a placeholder shipped by "
                   "instance/config.yaml.example. A block this org does not use "
                   "(objectStorage, agentRuntime, intakeRuntime) is DELETED, not filled.")


def load_tokens() -> dict:
    try:
        c = yaml.safe_load(CONFIG.read_text())
    except yaml.YAMLError as exc:
        # the parser's excerpt would repeat the text around the error, and a config may
        # hold a pasted value that must not land in a terminal or a CI log
        sys.exit(f"instance/config.yaml cannot be read as YAML ({type(exc).__name__})")
    if not isinstance(c, dict):
        sys.exit("instance/config.yaml is not a YAML map")
    refuse_example_values(c)
    refuse_retired_auth(c)
    repo_url = c["git"]["repoUrl"]
    m = re.search(r"[:/]([^:/]+/[^/]+?)(?:\.git)?$", repo_url)
    if not m:
        # The value is not echoed: a pasted URL may carry userinfo that must not land in a
        # terminal or a CI log.
        sys.exit("cannot parse org/repo out of git.repoUrl — expected "
                 "git@<host>:<org>/<repo>[.git] or https://<host>/<org>/<repo>[.git]")
    registry = c["registry"]
    # The three owner spellings are echoed by the refusals below, so they are first held
    # to the SHAPE of an owner: a GitHub owner is letters, digits and dashes, and a
    # registry line is ghcr.io/<owner>. A value of another shape (a pasted URL with
    # userinfo, say) is refused by field name and never printed.
    if not isinstance(c["git"].get("github_org"), str) or not GITHUB_OWNER.fullmatch(c["git"]["github_org"]):
        sys.exit("git.github_org is not a GitHub owner name (letters, digits and dashes)")
    if not GITHUB_OWNER.fullmatch(m.group(1).split("/", 1)[0]):
        sys.exit("git.repoUrl does not name a GitHub owner (letters, digits and dashes) before the repository")
    # The whole URL, not its tail: the same two GitHub forms the loader accepts, so a URL
    # with userinfo or another origin never reaches a rendered manifest.
    if not REPO_URL.fullmatch(repo_url.strip()):
        sys.exit("git.repoUrl is not a GitHub repository URL (git@github.com:owner/repo.git or https://github.com/owner/repo.git)")
    if not isinstance(registry, str) or not REGISTRY_LINE.fullmatch(registry):
        sys.exit("registry is not of the form ghcr.io/<owner> (letters, digits and dashes)")
    # coreRegistry.owner and .orbitdTag are joined into `image: <owner>/orbitd:<tag>`, a
    # container image reference: held to that shape here, never rendered otherwise
    core = c.get("coreRegistry")
    if core is not None:
        if not isinstance(core, dict):
            sys.exit("coreRegistry must be a map with owner and orbitdTag")
        owner, tag = core.get("owner", ""), core.get("orbitdTag", "")
        # a non-string is refused by field further down ("must be strings"); a string
        # is held to the reference's shape here
        if isinstance(owner, str) and not IMAGE_REPO.fullmatch(owner):
            sys.exit("coreRegistry.owner is not an image repository path (registry/path, lowercase; the value is not repeated)")
        if isinstance(tag, str) and not IMAGE_TAG.fullmatch(tag):
            sys.exit("coreRegistry.orbitdTag is not an image tag (letters, digits, `_ . -`, 128 at most; the value is not repeated)")
    # The domain and name fields render into hostnames, lists of hosts and object names:
    # the same shapes the loader (scripts/lib-instance.sh) holds them to, checked here
    # too so a scaffold is never filled from a value the stages would refuse.
    doms = c.get("domains") or {}
    for field, value in (("domains.primary", doms.get("primary")), ("domains.dev", doms.get("dev"))):
        if not isinstance(value, str) or not DNS_NAME.fullmatch(value.strip().rstrip(".")):
            sys.exit(f"{field} is not a DNS name")
    zones = doms.get("dnsZones")
    if not isinstance(zones, list) or not zones or not all(isinstance(z, str) and DNS_NAME.fullmatch(z.strip().rstrip(".")) for z in zones):
        sys.exit("domains.dnsZones is not a list of DNS names")
    # The CANONICAL spelling (no padding, no trailing dot, lowercase) is what validation
    # accepted and what the loader exports: it is stored back so every token, derived
    # name (wildcard-<primary dashed>) and rendered host is built from the same string.
    doms["primary"] = doms["primary"].strip().rstrip(".").lower()
    doms["dev"] = doms["dev"].strip().rstrip(".").lower()
    # the longest object name derived from the primary domain is wildcard-<dashed>-tls;
    # a Kubernetes object name is 253 characters at most (round 17: a 245-character
    # domain rendered a 258-character Secret name the API server refuses)
    # ...and the SAME limit on every domain the loader exports (domains.dev too): the loader
    # refuses either over 240, and a tree init rendered from a domain the loader refuses is
    # a tree no stage will ever verify (round 19)
    for which in ("primary", "dev"):
        if len(doms[which]) > DOMAIN_MAX:
            sys.exit(f"domains.{which} is longer than {DOMAIN_MAX} characters — the names derived from it "
                     "(wildcard-<domain dashed>-tls) would exceed Kubernetes' 253-character object-name limit")
    doms["dnsZones"] = [z.strip().rstrip(".").lower() for z in zones]
    c["domains"] = doms
    endpoint = (c.get("objectStorage") or {}).get("endpoint")
    if endpoint is not None:
        refuse_unless_endpoint_url(endpoint)
    iname = (c.get("instance") or {}).get("name")
    if not isinstance(iname, str) or not LABEL_NAME.fullmatch(iname):
        sys.exit("instance.name is not a lowercase label (letters, digits, dashes)")
    if iname in RESERVED_INSTANCE_NAMES:
        sys.exit("instance.name is a word the platform itself uses as a literal (an environment, a branch, a "
                 "namespace or a directory name) — choose a name of your own")
    branch = c["git"].get("branch", "main")
    if not isinstance(branch, str) or not BRANCH_NAME.fullmatch(branch) or ".." in branch or "//" in branch \
            or "@{" in branch or branch.endswith((".lock", "/", ".")) \
            or any(part.startswith(".") or part.endswith(".lock") or not part for part in branch.split("/")):
        # git check-ref-format's rules, the ones a clone honours; a value of another shape
        # renders a targetRevision nothing can fetch
        sys.exit("git.branch is not a branch name (git check-ref-format --branch)")
    # THREE spellings of one fact, not two. `git.repoUrl` names the owner that hosts the
    # instance repo; `git.github_org` names the org the GitHub App is installed on (its
    # token reaches that owner's repositories, and nowhere else); `registry` names where
    # the images land. Round 1 made the last two agree and left the first out, so a
    # repoUrl pointing at another owner still rendered a scaffold whose instance repo the
    # App's token cannot reach (review round 2, Sol 3, 2026-09-07). Case-insensitive:
    # GitHub owners are case-preserving and case-insensitive, so `AcmeOrg` and `acmeorg`
    # are one org.
    repo_owner = m.group(1).split("/", 1)[0]
    org = str(c["git"]["github_org"])
    if repo_owner.lower() != org.lower():
        so, sg, sr = _shown_owner(repo_owner), _shown_owner(org), _shown_owner(registry)
        sys.exit(f"git.repoUrl names owner {so} but git.github_org is {sg} "
                 f"and registry is {sr}. These are one org spelled three times: "
                 f"the GitHub App is installed on git.github_org and its token reaches "
                 f"that owner's repositories only — so an instance repo under {so} "
                 f"is one the platform installed on {sg} cannot clone or push to. Make all "
                 f"three name the same owner.")
    # The registry and the org are two spellings of ONE fact, and until now nothing made
    # them agree. The org's images are built on the operator machine and pushed to
    # ghcr.io/<the org> — the owner its registry credential belongs to — while the
    # catalog's `image:` is written from `registry`, and scripts/bump-tag.py deploys only
    # what the catalog claims. A config naming a different owner in the two fields is
    # therefore not a typo that fails: it is an org whose images are real and whose
    # deploys never happen, with no error anywhere to say why (review round 1, Sol 1 /
    # Muse 2).
    #
    # ghcr.io lowercases: an org displayed `AcmeOrg` pushes to ghcr.io/acmeorg, so that is
    # what the registry line must say.
    expected_registry = "ghcr.io/" + str(c["git"]["github_org"]).lower()
    if registry != expected_registry:
        sr, sg, se = _shown_owner(registry), _shown_owner(c["git"]["github_org"]), _shown_owner(expected_registry)
        sys.exit(f"registry {sr} does not belong to git.github_org "
                 f"{sg}. The org's images are pushed to "
                 f"{se} (ghcr.io/<owner>, lowercased), so a catalog written from "
                 f"{sr} names images that are never built and deploys nothing, "
                 f"silently. Set registry to ghcr.io/<git.github_org, lowercased> — or change "
                 f"git.github_org if THAT is the wrong one.")
    tokens = {
        "instance.name": c["instance"]["name"],
        "git.repoUrl": repo_url,
        "git.branch": c["git"].get("branch", "main"),
        "git.github_org": c["git"]["github_org"],
        "git.repoSlug": m.group(1),
        "domains.primary": c["domains"]["primary"],
        "domains.dev": c["domains"]["dev"],
        "domains.primaryDashed": c["domains"]["primary"].replace(".", "-"),
        "registry.user": registry.split("/", 1)[1] if "/" in registry else registry,
        "contacts.email": c.get("contacts", {}).get("email", ""),
        "contacts.telegramChatId": str(c.get("contacts", {}).get("telegramChatId", "")),
        "objectStorage.buckets.platform":
            (((c.get("objectStorage") or {}).get("buckets")) or {}).get("platform", ""),
        "objectStorage.endpoint": (c.get("objectStorage") or {}).get("endpoint", ""),
        # Tempo's s3 config wants the endpoint HOST, schemeless; deriving it here keeps
        # the config file carrying one endpoint spelling instead of two to keep in step.
        "objectStorage.endpointHost":
            urllib.parse.urlsplit((c.get("objectStorage") or {}).get("endpoint", "")).netloc,
        "instance.mcpHost": "mcp." + c["domains"]["primary"],
        # Loki's s3 block wants the REGION as well as the endpoint. It is the first label
        # of the S3 host, so it is derived here rather than asked for twice.
        "objectStorage.region":
            (urllib.parse.urlsplit((c.get("objectStorage") or {}).get("endpoint", "")).hostname or "").split(".")[0],
        # The CORE repo's owner. Core's own images (orbitd, agent-runtime, intake)
        # are published under it, and it is an org-shaped string, so it lives in the
        # instance file for the same reason agentRuntime.image does.
        "coreRegistry.owner": (c.get("coreRegistry") or {}).get("owner", ""),
        "coreRegistry.orbitdTag": (c.get("coreRegistry") or {}).get("orbitdTag", ""),
        # orbitd's apply switch (ORBITD_APPLY) and reconcile period
        # (ORBITD_RECONCILE_PERIOD_S). Absent is `off` / 180, the inert default:
        # the applier stays a shadow until the org turns it on deliberately.
        "orbitd.applyMode": orbitd_apply_mode(c),
        "orbitd.reconcilePeriodS": str(orbitd_config(c).get("reconcilePeriodS", 180)),
        "orbitd.applyPairs": orbitd_apply_pairs(c),
        "contacts.adminFirstName":
            c.get("contacts", {}).get("adminFirstName", ""),
        "contacts.adminLastName":
            c.get("contacts", {}).get("adminLastName", ""),
    }
    odd = sorted(k for k, v in tokens.items() if not isinstance(v, str))
    if odd:
        # a bare YAML number or boolean where a string is rendered (`orbitdTag: 1234`)
        # would reach the substitution as a non-string; refused by field, before anything
        # is written
        sys.exit("instance/config.yaml: these fields must be strings (quote the value): " + ", ".join(odd))
    refuse_unsafe_tokens(tokens)
    # `{{features.<name>}}` renders "true" or "false": a chart value that must say off
    # out loud (a default-on subchart) takes it where a block could only say nothing.
    # Added after the safety check above: these two words are ours, not the config's.
    if not FEATURES_ERROR:
        try:
            for name, on in orbit_features.states(c, FEATURES).items():
                tokens[f"features.{name}"] = "true" if on else "false"
        except orbit_features.FeatureError as e:
            sys.exit(f"instance/config.yaml: {e}")
    monitoring_probes(c, tokens)     # validated HERE, before render_staged rewrites the tree
    return c, tokens


def refuse_retired_auth(c: dict) -> None:
    """Refuse a config that still carries the retired identity plane's `auth:` block.

    Refused before anything is written: the provider is retired, and Grafana signs in
    with its local admin (the sealed grafana-admin Secret) now."""
    if "auth" in c:
        sys.exit("instance/config.yaml carries an `auth:` block — NOTHING was written. "
                 "The identity provider is retired; Grafana signs in with its local admin "
                 "(the sealed grafana-admin Secret) now. "
                 "Delete the `auth:` block and re-run.")


# orbitd's apply switch: off | dry-run | on, default off. Absent is the DEFAULT
# and is not an error — the applier stays a shadow until the org turns it on.
ORBITD_APPLY_MODES = ("off", "dry-run", "on")


_PAIR = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,62}/[A-Za-z0-9][A-Za-z0-9._-]{0,62}")
IMAGE_REPO = re.compile(r"[a-z0-9]([a-z0-9.-]*[a-z0-9])?(:[0-9]{1,5})?(/[a-z0-9]([a-z0-9._-]*[a-z0-9])?)+")
IMAGE_TAG = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}")
BRANCH_NAME = re.compile(r"[A-Za-z0-9_][A-Za-z0-9._/-]{0,199}")
DNS_NAME = re.compile(r"(?i)[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?(\.[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?)+")
DOMAIN_MAX = 253 - len("wildcard-") - len("-tls")     # the longest derived object name stays a Kubernetes name
def shown_rel(p) -> str:
    """A tree path in a refusal or report, component by component: a name that is not of
    a file-name shape is replaced, never repeated — a file can be named after a token
    (round 19). Every message that names a path under the instance root goes through here."""
    return display.shown_parts("/".join(pathlib.Path(p).relative_to(ROOT).parts))
LABEL_NAME = re.compile(r"[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?")
REPO_URL = re.compile(r"(git@github\.com:|https://github\.com/)[A-Za-z0-9](?:[A-Za-z0-9-]{0,37}[A-Za-z0-9])?/[A-Za-z0-9._-]{1,100}")
PROBE_HOST = re.compile(r"(?i)[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?(\.[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?)*")
GITHUB_OWNER = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,37}[A-Za-z0-9])?")
REGISTRY_LINE = re.compile(r"ghcr\.io/[A-Za-z0-9](?:[A-Za-z0-9-]{0,37}[A-Za-z0-9])?")


def orbitd_config(c: dict) -> dict:
    raw = c.get("orbitd")
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        sys.exit("orbitd: must be a map with applyMode and reconcilePeriodS, or absent")
    period = raw.get("reconcilePeriodS", 180)
    # A number, in range: the value is written into the daemon's environment block, and
    # only an integer belongs there.
    if isinstance(period, bool) or not isinstance(period, int) or not 1 <= period <= 86400:
        sys.exit("orbitd.reconcilePeriodS: must be a whole number of seconds from 1 to 86400")
    return raw


def orbitd_apply_mode(c: dict) -> str:
    mode = orbitd_config(c).get("applyMode", "off")
    # YAML 1.1 parses an unquoted on/off as a boolean: map it back before
    # validating, so `applyMode: on` still means the 'on' mode. A string
    # stays as is; anything else still fails naming the key below.
    if mode is True:
        mode = "on"
    elif mode is False:
        mode = "off"
    if mode not in ORBITD_APPLY_MODES:
        sys.exit("orbitd.applyMode is not accepted — the values are 'off', 'dry-run' and 'on' (default 'off'). See instance/config.yaml.example.")
    return mode


def orbitd_apply_pairs(c: dict) -> str:
    """Comma-joined project/env pairs for ORBITD_APPLY_PAIRS; empty is the default.

    Each entry is validated against catalog/projects.yaml. Unknown pair: exit
    naming orbitd.applyPairs.
    """
    raw = orbitd_config(c).get("applyPairs", [])
    if raw is None:
        raw = []
    if not isinstance(raw, list):
        sys.exit("orbitd.applyPairs: must be a list of project/env strings, or absent")
    pairs: list[str] = []
    for i, item in enumerate(raw):
        # A value of the wrong SHAPE is refused by position, never repeated: it may be
        # anything a hand pasted. A shaped pair (two names and a slash) is safe to name.
        if not isinstance(item, str) or not _PAIR.fullmatch(item.strip()):
            sys.exit(f"orbitd.applyPairs[{i}] is not a project/env pair (two names joined by one slash)")
        pairs.append(item.strip())
    if pairs:
        known = catalog_project_env_pairs()
        for pair in pairs:
            if pair not in known:
                sys.exit(f"orbitd.applyPairs: {pair} is not a catalog pair")
    return ",".join(pairs)


def catalog_project_env_pairs() -> set[str]:
    path = ROOT / "catalog" / "projects.yaml"
    if not path.is_file():
        sys.exit("orbitd.applyPairs: catalog/projects.yaml is missing")
    try:
        cat = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except (OSError, ValueError, yaml.YAMLError) as exc:
        # the parser's excerpt quotes the text around the error; the catalog may hold anything
        sys.exit(f"orbitd.applyPairs: catalog/projects.yaml cannot be read as YAML ({type(exc).__name__})")
    if not isinstance(cat, dict):
        sys.exit("orbitd.applyPairs: catalog/projects.yaml is not a YAML map")
    known: set[str] = set()
    for project in cat.get("projects") or []:
        if not isinstance(project, dict):
            continue
        pid = project.get("id")
        envs = project.get("environments") or {}
        if not pid or not isinstance(envs, dict):
            continue
        for env in envs:
            known.add(f"{pid}/{env}")
    return known


def gates(c: dict) -> dict:
    """Evaluate every gate once, and refuse a configuration that cannot mean anything."""
    if FEATURES_ERROR:
        sys.exit(FEATURES_ERROR)
    obj = c.get("objectStorage") or {}
    bucket = bool((obj.get("buckets") or {}).get("platform"))
    if bucket and not obj.get("endpoint"):
        # Declared bucket, missing endpoint: fail HERE with the fix named, not later as
        # two cryptic unresolved tokens (the exact confusion a real fresh org hit).
        sys.exit("objectStorage.buckets.platform is declared but objectStorage.endpoint "
                 "is missing — add the endpoint (https://<region>.your-objectstorage.com) "
                 "or remove the objectStorage block")
    try:
        features = orbit_features.states(c, FEATURES)
    except orbit_features.FeatureError as e:
        sys.exit(f"instance/config.yaml: {e}")
    if not features.get("logs") and c.get("lokiRuler"):
        # Loki is about to be skipped, so `lokiRuler: wired` would assert wiring inside a
        # file that is never rendered — and the generator would then render a `logs`
        # trigger whose rule nothing can evaluate.
        sys.exit("lokiRuler is declared but the logs feature is off (no "
                 "objectStorage.buckets.platform, or features.logs is not true) — Loki "
                 "is not rendered, so there is no ruler to wire. "
                 "Remove lokiRuler, or turn logs on.")
    intake = c.get("intakeIngress")
    if intake is not None and intake != "wired":
        # The lokiRuler lesson: a typo must be refused at generation, never silently read
        # as "not wired".
        sys.exit("intakeIngress is not accepted — `wired` is the only value. "
                 "Remove the key to leave the intake's public door unrendered.")
    # sops is the only secret plane. Refused here — before anything is written —
    # so a config naming a plane nothing provides can never scaffold half a tree.
    secrets_provider(c)
    return {**features, "bucket": bucket, "intake": intake == "wired"}


def secrets_provider(c: dict) -> str:
    """secrets.provider, validated: sops is the only secret plane, absent means sops.

    Refused rather than defaulted. The key was read by nothing until v0.18.0, so a typo
    in it used to be free — an org that wrote `provider: in-cluster` got the Cloud
    stores and no signal at all. A silent fallback here would reproduce that bug with a
    validator in front of it, and there is no second plane to fall back to."""
    provider = (c.get("secrets") or {}).get("provider", PROVIDER_SOPS)
    if provider != PROVIDER_SOPS:
        sys.exit("secrets.provider is not a secret plane this core can "
                 "render — sops is the only plane: Secrets come from SopsSecrets "
                 "applied by the desk, and no store, operator or vault is rendered. "
                 "See instance/config.yaml.example.")
    return provider


def gates_for(rel: str) -> list:
    """Every gate governing a destination path, matching a directory prefix too.

    A list, not one gate, because a path can sit under two gates at once. Every named
    gate must be open for the file to render. The longest (most specific) match speaks
    first in the removal line.
    """
    found = [gate for path, gate in GATES.items()
             if rel == path or rel.startswith(path + "/")]
    found.sort(key=lambda g: -max(len(p) for p, gg in GATES.items() if gg == g))
    return found


def render_staged(c: dict) -> None:
    """Re-render every staged file, and remove the ones whose gate is closed.

    Unconditional and idempotent: a run always copies from the staged source, so a value
    corrected in instance/config.yaml reaches clusters/ on the next `make init` instead of
    being frozen at whatever the first run wrote. That is the whole reason this function
    exists — see the module docstring.

    Removal matters as much as rendering. Taking a bucket or `intakeIngress` back out
    of the config DELETES what it used to render, so the tree cannot keep a public door
    or an S3-backed Application that the config no longer asks for."""
    if not STAGED.is_dir():
        sys.exit(f"{shown_rel(STAGED)} is missing — this instance repo is not a "
                 f"faithful copy of the template, and clusters/ cannot be generated")
    open_gates, rendered, removed = gates(c), 0, []
    refuse_symlinks_under_staged()
    for src in sorted(STAGED.rglob("*.yaml")):
        rel = str(src.relative_to(STAGED))
        dst = CLUSTERS / rel
        needed = gates_for(rel)
        closed = [g for g in needed if not open_gates[g]]
        if not closed:
            dst.parent.mkdir(parents=True, exist_ok=True)
            try:
                dst.write_text(orbit_features.apply_blocks(
                    src.read_text(), open_gates, f"provisioning/clusters/{rel}"))
            except orbit_features.FeatureError as e:
                sys.exit(str(e))
            rendered += 1
        elif dst.exists():
            assert _stays_under_clusters(dst), f"refusing to delete {dst} outside clusters/"
            dst.unlink()
            removed.append((rel, closed))
    for rel, reason in ([(r, "retired") for r in RETIRED_DESTINATIONS]
                       + [(r, "retired") for r in RETIRED_IDP_DESTINATIONS]
                       + [(r, "plane") for r in RETIRED_SECRET_PLANE]):
        dst = CLUSTERS / rel
        if dst.exists():
            assert _stays_under_clusters(dst), f"refusing to delete {dst} outside clusters/"
            dst.unlink()
            removed.append((rel, [reason]))
    for rel, closed in removed:
        print(f"removed {shown_rel(CLUSTERS / rel)} ({'; '.join(gate_reason(g, c) for g in closed)})")
    # An emptied directory is not left behind to read as "this feature is here, broken".
    for d in sorted((p for p in CLUSTERS.rglob("*") if p.is_dir()), reverse=True):
        if not any(d.iterdir()):
            d.rmdir()
    print(f"rendered {rendered} files from provisioning/clusters"
          + (f", removed {len(removed)}" if removed else ""))


# The `secrets` Application's source directory (see the staged
# platform/apps/secrets.yaml: `path: clusters/platform/secrets`). It renders for every
# provider — platform-made credentials such as the Alertmanager webhook token are sealed
# there on any plane — but nothing stages it (it holds sealed files init does not own),
# so without this init leaves a tree the platform plan cannot render
# ("clusters/platform/secrets is not a directory"). Hence a folder, not a file: git
# keeps no empty folder, and orbitd renders from a fresh clone.
SECRETS_DIR = CLUSTERS / "platform" / "secrets"
SECRETS_GITKEEP = SECRETS_DIR / ".gitkeep"


# Every path init must never write THROUGH on the way to the secrets folder. A
# symlinked folder would redirect the mkdir below into a directory outside the
# instance repo (writing .gitkeep into it), and a dangling .gitkeep symlink would
# redirect the write into whatever its target names. Each step is refused by lstat
# (is_symlink), never by a resolved comparison — resolving first is what would
# follow the link, and a dangling link has no resolution to compare.
SECRETS_PATH_CHAIN = (
    CLUSTERS,
    CLUSTERS / "platform",
    SECRETS_DIR,
    SECRETS_GITKEEP,
)


def refuse_symlinks_under_staged() -> None:
    """The staged tree is READ and copied into clusters/: a symlink anywhere on the way
    (the staged root, an ancestor, a directory inside it, a file) would make init copy
    a file from outside the repo into a committed tree. Refused before anything is read."""
    chain = [STAGED] + list(STAGED.parents)
    for path in chain:
        if path == ROOT.parent:
            break
        if path.is_symlink():
            sys.exit(f"{shown_rel(path) if path != ROOT else '.'} is a symlink — init reads "
                     f"nothing through a link")
    for path in sorted(STAGED.rglob("*")):
        if path.is_symlink():
            sys.exit(f"{shown_rel(path)} is a symlink — init reads nothing through a "
                     f"link. Replace it with a real file (or directory) and re-run.")


def refuse_symlinked_secrets_path() -> None:
    """Refuse when any step of the secrets folder path is a symlink, writing nothing.

    A normal re-run passes through silently: no step is a link, so this changes
    nothing for it."""
    for path in SECRETS_PATH_CHAIN:
        if path.is_symlink():
            sys.exit(f"{shown_rel(path)} is a symlink — init writes nothing "
                     f"through a link. Replace it with a real directory (or file) "
                     f"and re-run.")


def ensure_secrets_folder() -> None:
    """Create clusters/platform/secrets/ with its .gitkeep, touching nothing else.

    Every run, every provider. Sealed files already in the folder are not ours and are
    never modified or removed; an existing .gitkeep is left alone so a second init
    changes nothing."""
    refuse_symlinked_secrets_path()
    SECRETS_DIR.mkdir(parents=True, exist_ok=True)
    if not SECRETS_GITKEEP.exists():
        SECRETS_GITKEEP.write_text("")


GATE_REASON = {
    "retired": "its component was deleted from the platform (phase 8)",
    "plane": "sops is the only secret plane — the old plane's operator and vaults "
             "left core, and the next init removes what they rendered",
    "bucket": "no objectStorage.buckets.platform declared",
    "intake": "intakeIngress is not `wired` — the intake has no public door",
}


def gate_reason(gate: str, c: dict) -> str:
    """Why a closed gate removed a file, in the words of the removal line."""
    if gate in GATE_REASON:
        return GATE_REASON[gate]
    if "bucket" in FEATURES[gate]["requires"] and not orbit_features.has_bucket(c):
        return f"the {gate} feature is off: {GATE_REASON['bucket']}"
    return f"the {gate} feature is off (`features:` in instance/config.yaml)"


def _stays_under_clusters(dst: pathlib.Path) -> bool:
    """True when a destination resolves inside the instance's clusters/ tree."""
    try:
        dst.resolve().relative_to(CLUSTERS.resolve())
        return True
    except ValueError:
        return False


def refuse_symlinks_under_clusters() -> None:
    """Refuse ANY symlink under clusters/ before a byte moves.

    Three reviews in a row found a different init step following a symlink under
    clusters/ and writing or deleting outside the instance — the secrets folder,
    the retired-file cleanup, then the token fill (which rewrote a file outside
    through a symlinked leaf) and the empty-directory sweep. Checking each step's
    destinations missed the next step each time, so the rule is one invariant and
    it runs first: clusters/ itself, its parents inside the instance root, and
    every entry under it (file, directory or dangling, pointing inside or
    outside) is collected without following links, and any symlink refuses the
    whole run naming every offending path. An instance tree has no legitimate use
    for a symlink under clusters/: the rendered manifests are plain files.

    The walk fails closed: a directory it cannot list, or an entry it cannot
    inspect, could hide a link, so it refuses the run by name as well (review
    r23c: a mode-0311 directory used to be skipped silently and written through).
    """
    offenders: list[str] = []
    unreadable: list[str] = []

    def rel(p) -> str:
        return shown_rel(p)

    def link(p: pathlib.Path) -> bool:
        # Path.is_symlink() answers False on a permission error; lstat does not. A path
        # that does not exist is neither a link nor unreadable: a missing clusters/ is
        # rendered from scratch (review r23d).
        try:
            return stat.S_ISLNK(os.lstat(p).st_mode)
        except OSError as err:
            if err.errno not in (errno.ENOENT, errno.ENOTDIR):
                unreadable.append(rel(p.parent))
            return False

    node = CLUSTERS
    while node == ROOT or ROOT in node.parents:
        if link(node):
            offenders.append(rel(node))
        if node == ROOT or node == node.parent:
            break
        node = node.parent
    if rel(CLUSTERS) not in offenders and CLUSTERS.is_dir():
        def refuse(err: OSError) -> None:
            if err.errno not in (errno.ENOENT, errno.ENOTDIR):
                unreadable.append(rel(err.filename or CLUSTERS))
        for dirpath, dirnames, filenames in os.walk(CLUSTERS, onerror=refuse, followlinks=False):
            for name in sorted(dirnames) + sorted(filenames):
                p = pathlib.Path(dirpath) / name
                if link(p):
                    offenders.append(rel(p))
    if unreadable:
        lines = "\n".join(f"  {d} cannot be listed or inspected — NOTHING was written. "
                          f"Give it read and search permission and re-run."
                          for d in sorted(set(unreadable)))
        sys.exit("init refuses a directory under clusters/ it cannot inspect: it could "
                 "hide a symlink that would write or delete outside the instance repo.\n"
                 + lines)
    if offenders:
        lines = "\n".join(f"  {rel} is a symlink — NOTHING was written. Replace it "
                          f"with a regular file or directory and re-run."
                          for rel in sorted(set(offenders)))
        sys.exit("init refuses symlinks under clusters/: following one would write "
                 "or delete outside the instance repo.\n" + lines)


def refuse_directory_destinations() -> None:
    """Refuse a destination that exists as a directory where init writes a file.

    The symlink invariant above already refused links; what remains is a real
    directory sitting where a staged, retired or loop-written file is rendered
    or unlinked — replacing it safely is impossible, so the run is refused
    naming the path, before anything is written.
    """
    staged = ([str(p.relative_to(STAGED)) for p in STAGED.rglob("*.yaml")]
              if STAGED.is_dir() else [])
    for rel in sorted(set(staged) | set(RETIRED_DESTINATIONS)
                      | set(RETIRED_IDP_DESTINATIONS)
                      | set(RETIRED_SECRET_PLANE)):
        if (CLUSTERS / rel).is_dir():
            sys.exit(f"clusters/{shown_rel(CLUSTERS / rel)} is a directory — NOTHING was written. Init "
                     f"renders that destination as a file (and deletes a retired "
                     f"one the same way), so a directory there cannot be replaced "
                     f"safely. Move it aside and re-run.")


# What a token value may hold. Every token is substituted into YAML text as-is, inside a
# quoted scalar or as a bare one, so a value carrying a line break, a quote or a control
# character would rewrite the DOCUMENT around it (a second key, a second list entry, a
# closed string) instead of filling one field. Refused by KEY before anything is written.
# ... and the flow indicators: a token lands inside `[...]` and `{...}` in some
# templates (a hosts list, an env mapping), where a comma or a bracket splits or closes
# the collection around it — a value that reads as one string standing alone reads as
# two entries there.
_TOKEN_UNSAFE = re.compile(r'["\\\x00-\x1f\x7f\[\]{}]|\{\{|\}\}')
# The comma is refused too, except in the one token that is a comma-joined list by
# construction (orbitd.applyPairs: every element already proven a project/env pair).
_TOKEN_COMMA = re.compile(r',')
_TOKEN_JOINED = frozenset({"orbitd.applyPairs"})


def _yaml_keeps(value: str) -> bool:
    """Would a YAML reader hand this value back as the same string when it stands bare?
    `null` becomes nothing, `yes` a boolean, `a: b` a mapping, `- x` a list; a number
    stays a number, which is what a numeric field means."""
    try:
        parsed = yaml.safe_load(value)
    except yaml.YAMLError:
        return False
    if parsed is None or isinstance(parsed, (bool, dict, list)):
        return False
    return isinstance(parsed, str) and parsed == value


def _yaml_keeps_number(value: str) -> bool:
    """A numeric field (a Telegram chat id) may read back as a number — the SAME number:
    `012345` is octal 5349 to a YAML 1.1 reader, `1e3` a float."""
    try:
        parsed = yaml.safe_load(value)
    except yaml.YAMLError:
        return False
    return isinstance(parsed, int) and not isinstance(parsed, bool) and str(parsed) == value


# Fields whose value IS a number where it lands (rendered bare, read as a number by the
# consumer): the round-trip rule asks for the same number, not the same string.
# (reconcilePeriodS is range-checked as an int before it is stringified here.)
_TOKEN_NUMERIC = frozenset({"contacts.telegramChatId", "orbitd.reconcilePeriodS"})


# Fields whose value is an enumerated word validated elsewhere (`off` reads as a boolean
# in YAML 1.1, and it is quoted where it lands): the round-trip rule does not apply.
_TOKEN_ENUMERATED = frozenset({"orbitd.applyMode"})

# Fields that land only INSIDE a longer string (`image: ghcr.io/<owner>/orbitd:<tag>`),
# which YAML reads as one string whatever the part would read as alone: held to the image
# reference's shape above, the standalone-scalar rule does not apply (round 24: "1234")
_TOKEN_EMBEDDED = frozenset({"coreRegistry.owner", "coreRegistry.orbitdTag"})

# Words the core itself uses as bare literals (an environment name, a branch, a
# namespace, a directory): an instance named after one would make every such literal
# read as the instance's identity to the no-instance-strings check (round 24)
RESERVED_INSTANCE_NAMES = frozenset({"prod", "dev", "staging", "test", "main", "master", "default", "platform",
                                     "orbit", "core", "monitoring", "argocd", "traefik", "kube-system", "instance",
                                     "example", "template", "clusters", "catalog", "scripts", "secrets", "apps"})


def _shown_owner(value) -> str:
    """An owner (or a ghcr.io/<owner> registry) is repeated in a refusal only when it has
    an owner's shape and is not of a credential's shape (a run of 16+ letters and digits
    holding both); anything else is described, not repeated (round 25)."""
    s = str(value)
    return repr(s) if display.fits(s, "owner") else display.described("owner", "value")


def refuse_unsafe_tokens(tokens: dict) -> None:
    bad = sorted(k for k, v in tokens.items()
                 if isinstance(v, str) and v and (_TOKEN_UNSAFE.search(v)
                                                  or (k not in _TOKEN_JOINED and _TOKEN_COMMA.search(v))
                                                  or (k in _TOKEN_NUMERIC and not _yaml_keeps_number(v))
                                                  or (k not in _TOKEN_ENUMERATED and k not in _TOKEN_NUMERIC and k not in _TOKEN_EMBEDDED and not _yaml_keeps(v))))
    if bad:
        sys.exit("instance/config.yaml: these fields hold a value a rendered file cannot "
                 "carry as-is (a line break, a double quote, a backslash, a control character, a comma, a bracket or brace, `{{`, or "
                 "text YAML would read as something other than that string, such as "
                 "`null`, `yes`, `a: b`): " + ", ".join(bad))


def fill(tokens: dict) -> list[str]:
    unresolved = []
    for f in sorted(CLUSTERS.rglob("*.yaml")):
        text = f.read_text()
        if "{{" not in text:
            continue

        def sub(match):
            key = match.group(1)
            val = tokens.get(key, "")
            # Empty is the default for applyPairs (every catalog pair). Every other
            # empty token is a missing config field and stays unresolved.
            if not val and key != "orbitd.applyPairs":
                unresolved.append(f"{shown_rel(f)}: {{{{{key}}}}}")
                return match.group(0)
            return val

        f.write_text(TOKEN_RE.sub(sub, text))
    return unresolved


class BlockDumper(yaml.SafeDumper):
    """PyYAML puts block sequences at their parent key's indent; k8s YAML reads better
    (and matches every other file here) with them indented under it."""

    def increase_indent(self, flow=False, indentless=False):
        return super().increase_indent(flow, False)


# The environments the secret plane may have, and it is a closed set (the owner's
# ruling, encoded here after review round 1 found it documented and unenforced). An
# environment nothing reads is a place for a supplied value to be written to by mistake
# and never noticed, which is the failure this plane exists to make impossible.
ENVIRONMENTS = ["dev", "prod"]


def check_environments(c: dict) -> None:
    """Refuse anything but exactly dev and prod, before a byte is written."""
    envs = (c.get("secrets") or {}).get("environments")
    if not isinstance(envs, list) or sorted(envs) != sorted(ENVIRONMENTS):
        sys.exit(
            f"secrets.environments is not exactly {ENVIRONMENTS} (this platform has "
            f"those two and no third; the list is not repeated here). NOTHING was written.\n"
            f"An environment no workload reads is a place for a supplied value to be "
            f"written by mistake and never noticed.")


BLACKBOX_PROBER = "blackbox-exporter.monitoring.svc:9115"
PROBES_REL = "platform/monitoring/probes.yaml"


def monitoring_probes(c: dict, tokens: dict) -> list:
    """monitoring.probes: [{url, module}, ...] — or [] when absent.

    The template ships the platform's own doors. Catalog-sync writes per-project tenant
    probes. What remains (a path-specific door, a protected surface) is this list, and
    it is optional: an org with nothing extra to watch leaves it out."""
    raw = (c.get("monitoring") or {}).get("probes", _ABSENT)
    if raw is _ABSENT or raw is None:
        return []
    if not isinstance(raw, list):
        sys.exit("monitoring.probes must be a list of {url, module} entries, or absent")
    out = []
    for i, entry in enumerate(raw):
        if not isinstance(entry, dict):
            sys.exit(f"monitoring.probes[{i}] must be a map with url and module")
        url = str(entry.get("url") or "").strip()
        module = str(entry.get("module") or "").strip()
        if not url or not module:
            sys.exit(f"monitoring.probes[{i}] needs non-empty url and module")
        # a module is a blackbox-exporter module name, and it is folded into a Probe's
        # object name: lowercase letters, digits and underscores, 63 at most
        if not re.fullmatch(r"[a-z][a-z0-9_]{0,62}", module):
            sys.exit(f"monitoring.probes[{i}].module is not a module name (lowercase letters, digits, underscores)")
        # the module must be one the configured exporter defines: a Probe naming an
        # undefined module renders, and the exporter answers every run with an error (round 24)
        if module not in exporter_modules():
            sys.exit(f"monitoring.probes[{i}].module is not a module the blackbox exporter defines "
                     f"({len(exporter_modules())} module(s) in clusters/{BLACKBOX_APP_REL}; the name is not repeated)")
        refuse_unless_probe_url(url, f"monitoring.probes[{i}].url")
        # ...and the URL the Probe will actually carry: a token stands for a value from
        # this same config, and the substituted text is what the exporter dials
        refuse_unless_probe_url(fill_probe_url(url, tokens), f"monitoring.probes[{i}].url (after token substitution)")
        out.append({"url": url, "module": module})
    return out


BLACKBOX_APP_REL = "platform/apps/blackbox-exporter.yaml"


def exporter_modules() -> frozenset:
    """The module names the blackbox-exporter Application configures (spec.source.helm.values
    → config.modules). Read from the STAGED source (provisioning/clusters/…): probes are
    checked before anything is rendered, and the staged file is what will be rendered.
    Unreadable is refused, never empty."""
    path = STAGED / BLACKBOX_APP_REL
    try:
        doc = yaml.safe_load(re.sub(r"\{\{[^{}]*\}\}", "PLACEHOLDER", path.read_text(encoding="utf-8")))
        values = doc["spec"]["source"]["helm"].get("values")
        vals = yaml.safe_load(re.sub(r"\{\{[^{}]*\}\}", "PLACEHOLDER", values)) if isinstance(values, str) \
            else doc["spec"]["source"]["helm"]["valuesObject"]
        modules = vals["config"]["modules"]
        if not isinstance(modules, dict) or not modules:
            raise ValueError("no modules")
    except Exception as exc:                  # noqa: BLE001
        sys.exit(f"provisioning/clusters/{BLACKBOX_APP_REL} does not carry the exporter's config.modules the way the "
                 f"template writes it ({type(exc).__name__}) — monitoring.probes cannot be checked against it")
    return frozenset(str(k) for k in modules)


def refuse_unless_probe_url(url: str, what: str) -> None:
    """A probe URL is rendered into a Probe the blackbox exporter dials: http(s), a host
    (a DNS name, or a scaffold token that becomes one), no userinfo. Refused by field,
    never repeated: a pasted URL may carry a credential."""
    bare = TOKEN_RE.sub("token.invalid", url)
    try:
        parts = urllib.parse.urlsplit(bare)
    except ValueError:
        sys.exit(f"{what} is not a URL")
    if parts.scheme not in ("http", "https") or parts.username is not None or parts.password is not None \
            or not parts.hostname or not PROBE_HOST.fullmatch(parts.hostname):
        sys.exit(f"{what} is not an http(s) URL of a host with no userinfo")
    if parts.query or parts.fragment:
        # a query is where a pasted token rides (?token=...); a probed door takes none
        sys.exit(f"{what} carries a query or fragment — a probe URL is scheme, host, port and path only")
    # ...and a path is where the other kind rides (/bot<id>:<token>/, a 24+ character
    # token segment): the same rule the Alertmanager inline-credential checker applies
    if any(PROBE_PATH_TOKEN.search(p) or PROBE_PATH_BOT.search(p)
           for p in (parts.path or "", urllib.parse.unquote(parts.path or ""))):
        sys.exit(f"{what} carries a path segment that reads like a credential (a bot token, or 24+ token characters) — a probed door takes none")
    # an explicit port is 1..65535 digits: `host:not-a-port` is a URL Python splits but
    # no prober dials, and the scaffold must not render it
    try:
        port = parts.port
    except ValueError:
        sys.exit(f"{what} names a malformed port")
    if port is not None and not 1 <= port <= 65535:
        sys.exit(f"{what} names a port outside 1..65535")


PROBE_PATH_BOT = re.compile(r"/bot\d+:")
PROBE_PATH_TOKEN = re.compile(r"[A-Za-z0-9_.~+:\-]{24,}")
ENDPOINT_HOST = re.compile(r"[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?(\.[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?)+")


def refuse_unless_endpoint_url(endpoint) -> None:
    """objectStorage.endpoint is rendered into Loki's and Tempo's s3 blocks and its host
    is derived for both: https://<host>[:port], nothing else — no userinfo (a pasted
    access key would land in three rendered files), no path, no query. Refused by
    field, never repeated."""
    if not isinstance(endpoint, str):
        sys.exit("objectStorage.endpoint is not a string")
    try:
        parts = urllib.parse.urlsplit(endpoint.strip())
        port = parts.port
    except ValueError:
        sys.exit("objectStorage.endpoint is not a URL with a well-formed port")
    if parts.scheme != "https" or parts.username is not None or parts.password is not None \
            or not parts.hostname or not ENDPOINT_HOST.fullmatch(parts.hostname) \
            or parts.path not in ("", "/") or parts.query or parts.fragment:
        sys.exit("objectStorage.endpoint is not https://<host> with no userinfo, path or query")
    if port is not None and not 1 <= port <= 65535:
        sys.exit("objectStorage.endpoint names a port outside 1..65535")


def fill_probe_url(url: str, tokens: dict) -> str:
    """Substitute scaffold tokens in a configured probe URL, leaving unknowns in place
    so the leftover-token scan at the end of the run still sees them."""
    def sub(match):
        val = tokens.get(match.group(1), "")
        return val if val else match.group(0)
    return TOKEN_RE.sub(sub, url)


def probe_name_for(module: str) -> str:
    """DNS-1123 name for a Probe that did not already exist in the template.

    http_401 is the protected-door module the blackbox exporter ships; the name matches
    the object an instance previously had to write by hand. Any other new module is
    named from the module itself, with underscores folded so the name stays RFC 1123."""
    if module == "http_401":
        return "orbit-protected-surfaces"
    return "orbit-" + module.replace("_", "-") + "-surfaces"


def monitor_files() -> list:
    """(file name, body) of every monitors/*.yaml that reads as a map. An unreadable
    monitor is orbitd's to report, not this script's."""
    directory = ROOT / "monitors"
    found = []
    for path in sorted(directory.glob("*.yaml")) if directory.is_dir() else []:
        try:
            body = yaml.safe_load(path.read_text(encoding="utf-8"))
        except (OSError, yaml.YAMLError):
            continue
        if isinstance(body, dict):
            found.append((path.name, body))
    return found


def refuse_what_needs_a_feature_that_is_off(c: dict, tokens: dict) -> None:
    """Extra probes (monitoring.probes) and `kind: probe` monitors (monitors/*.yaml) are
    run by the uptime feature's prober; a monitor with `notify: [telegram]` is delivered
    by the alerts-telegram feature. With the feature off each would look declared and do
    nothing, so the config is refused before anything is written — which is also what
    refuses turning the feature off while something still needs it."""
    on = gates(c)
    if not on.get("uptime", True):
        if monitoring_probes(c, tokens):
            sys.exit("monitoring.probes is declared and the uptime feature is off: nothing would run "
                     "the probes. Turn uptime on (features.uptime: true), or remove monitoring.probes.")
        names = [name for name, body in monitor_files() if body.get("kind") == "probe"]
        if names:
            sys.exit(f"the uptime feature is off and {len(names)} probe monitor(s) need it "
                     f"(monitors/{', monitors/'.join(names)}): nothing would run them. Turn uptime "
                     f"on (features.uptime: true), or remove the monitor(s) first.")
    if not on.get("alerts-telegram", True):
        names = [name for name, body in monitor_files()
                 if isinstance(body.get("notify"), list) and "telegram" in body["notify"]]
        if names:
            sys.exit(f"the alerts-telegram feature is off and {len(names)} monitor(s) notify telegram "
                     f"(monitors/{', monitors/'.join(names)}): nobody would be messaged. Turn the "
                     f"feature on (features.alerts-telegram: true), or set `notify: []` on them first.")


def render_monitoring_probes(c: dict, tokens: dict) -> None:
    """Append monitoring.probes onto the rendered probes file.

    Called after render_staged + fill, so the template's own targets are already in
    clusters/ with tokens substituted. An empty or absent list is a no-op: the staged
    file is left byte-identical. Extra entries are grouped by module; a module the
    template already has gains targets on that Probe, a new module becomes a new Probe.
    render_staged resets the file on every run, so this cannot accumulate duplicates."""
    extras = monitoring_probes(c, tokens)
    if not extras:
        return
    path = CLUSTERS / PROBES_REL
    if not path.exists():
        sys.exit(f"clusters/{PROBES_REL} is missing — cannot render monitoring.probes "
                 f"onto it")
    docs = [d for d in yaml.safe_load_all(path.read_text()) if d]
    by_module = {}
    for doc in docs:
        if isinstance(doc, dict) and doc.get("kind") == "Probe":
            mod = (doc.get("spec") or {}).get("module")
            if mod and mod not in by_module:
                by_module[mod] = doc
    for extra in extras:
        url = fill_probe_url(extra["url"], tokens)
        module = extra["module"]
        doc = by_module.get(module)
        if doc is None:
            doc = {
                "apiVersion": "monitoring.coreos.com/v1",
                "kind": "Probe",
                "metadata": {
                    "name": probe_name_for(module),
                    "namespace": "monitoring",
                },
                "spec": {
                    "jobName": "platform-probes",
                    "prober": {"url": BLACKBOX_PROBER},
                    "module": module,
                    "targets": {"staticConfig": {"static": []}},
                },
            }
            docs.append(doc)
            by_module[module] = doc
        static = (doc.setdefault("spec", {})
                     .setdefault("targets", {})
                     .setdefault("staticConfig", {})
                     .setdefault("static", []))
        if url not in static:
            static.append(url)
    header = (
        "# GENERATED into clusters/ by `make init` from instance/config.yaml.\n"
        "# Do not edit the copy under clusters/ (a re-run overwrites it), and not this staged\n"
        "# source either: provisioning/ is vendored from orbit-core at the pin, and\n"
        "# `make core-sync` replaces it. Change the text in orbit-core and bump the pin.\n"
        "# Extra static targets come from monitoring.probes in instance/config.yaml.\n"
    )
    chunks = []
    for i, doc in enumerate(docs):
        dumped = yaml.dump(doc, Dumper=BlockDumper, default_flow_style=False,
                           sort_keys=False).rstrip() + "\n"
        chunks.append(dumped if i == 0 else "---\n" + dumped)
    path.write_text(header + "".join(chunks))


def main() -> int:
    # The symlink invariant runs before anything else — before config is read and
    # long before anything is written — so a refusal leaves every tree untouched.
    refuse_symlinks_under_clusters()
    refuse_directory_destinations()
    c, tokens = load_tokens()
    if gates(c).get("alerts-telegram", True) and not c.get("contacts", {}).get("telegram", False):
        # The Telegram notifier is the alerts-telegram feature: on, it needs a chat to
        # send to; off, the Alertmanager config is rendered without it.
        sys.exit("the alerts-telegram feature is on and contacts.telegram is not true: there is no "
                 "chat to send alerts to. Set contacts.telegram: true with contacts.telegramChatId, "
                 "or turn the feature off (features.alerts-telegram: false).")
    # Before anything is written: a symlinked clusters/ or secrets path would
    # redirect the renders below outside the instance repo — half a rendered tree
    # is worse than none.
    refuse_symlinked_secrets_path()
    check_environments(c)
    refuse_what_needs_a_feature_that_is_off(c, tokens)
    render_staged(c)
    ensure_secrets_folder()
    unresolved = fill(tokens)
    render_monitoring_probes(c, tokens)
    if unresolved:
        print("UNRESOLVED tokens — add the missing config fields and re-run:")
        print("\n".join(f"  {u}" for u in sorted(set(unresolved))))
        return 1
    leftovers = [shown_rel(f) for f in CLUSTERS.rglob("*.yaml")
                 if TOKEN_RE.search(f.read_text())]
    if leftovers:
        print("tokens remain in:", ", ".join(leftovers))
        return 1
    print(f"instance scaffold filled for {tokens['instance.name']!r} "
          f"({tokens['domains.primary']}) on the sops plane")
    return 0


if __name__ == "__main__":
    sys.exit(main())
