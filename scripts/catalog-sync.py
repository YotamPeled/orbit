#!/usr/bin/env python3
"""
catalog/projects.yaml  ->  Argo Applications, namespaces, agents.

Run with `make sync`. Output is committed; CI re-runs this and fails if the tree
differs, so the checked-in manifests can never drift from the catalog.

Deterministic by construction: sorted keys, no timestamps, no randomness. That is what
makes "regenerate and diff" a usable drift check rather than noise.
"""
from __future__ import annotations
import copy
import hashlib
import ipaddress
import json
import math
import os
import pathlib
import re
import sys

import yaml

# The one display rule every diagnostic shares (scripts/verify/shown.py, vendored beside
# this file): a catalog value is repeated in a refusal only when it has the shape the
# field is meant to hold; anything else is described, so a pasted token, a control
# character or a multi-line value never reaches the make-sync console, the CI log, or the
# ship evidence that bump-tag.py folds this output into (area-2 sweep, round 1).
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "verify"))
import shown as display  # noqa: E402
import orbit_features  # noqa: E402


def _s(value, shape="name", what="value"):
    """A catalog value as text for a refusal: itself when it fits `shape`, else a
    description. Numbers, booleans and None carry no text and pass as they are; lists,
    tuples and mappings are shown element by element."""
    if value is None or isinstance(value, (bool, int, float)):
        return repr(value)
    if isinstance(value, (list, tuple)):
        return "[" + ", ".join(_r(v, shape, what) for v in value) + "]"
    if isinstance(value, (set, frozenset)):
        return "[" + ", ".join(_r(v, shape, what) for v in sorted(value, key=str)) + "]"
    if isinstance(value, dict):
        return "{" + ", ".join(f"{_r(k, shape, what)}: {_r(v, shape, what)}"
                               for k, v in value.items()) + "}"
    if display.fits(value, shape):
        return str(value)
    return display.described(shape, what)


def _r(value, shape="name", what="value"):
    """`_s` with repr quoting for a string that fits, so messages keep their shape."""
    if isinstance(value, str) and display.fits(value, shape):
        return repr(value)
    return _s(value, shape, what)

ROOT = pathlib.Path(__file__).resolve().parent.parent
CATALOG = ROOT / "catalog" / "projects.yaml"
TAGS = ROOT / "catalog" / "tags.yaml"
# The archive paths removed projects left in the bucket (written by catalog-edit.py remove).
RETIRED = ROOT / "catalog" / "retired.yaml"
APPS_DIR = ROOT / "clusters" / "apps"
ORBIT_DIR = ROOT / "clusters" / "orbit"
# The one file that makes this instance this instance (extraction, 2026-08-25). The
# generator is core; every org-specific value it emits comes from here, never from code.
INSTANCE = ROOT / "instance" / "config.yaml"

# A project id becomes a filesystem path, a namespace and an Argo Application name.
# Unvalidated, an id of "../platform/apps/evil" writes an Application straight into the
# directory the root app-of-apps reconciles — and the pruner, which globs one directory,
# could never clean it up again.
# fullmatch, not match. Python's `$` also matches just before a trailing newline, so
# `match()` accepts "evil\n" — which then becomes a filename with a literal newline in it
# and an Application/Namespace whose name the API server rejects. The identical bug was
# already fixed in bump-tag.py; the anchors in this pattern were never the protection.
ID_RE = re.compile(r"[a-z0-9]([a-z0-9-]{0,38}[a-z0-9])?")
# Kubernetes namespace names cap at 63 characters, and ours is derived as <id>-<env>.
# Two individually-legal 40-char names compose to 81 and the Namespace is rejected at apply
# time, long after the generator has declared success.
MAX_NS = 63
# Hostnames reach the Ingress verbatim; anything the API server would reject as a DNS1123
# subdomain has to be caught here, not at apply time.
HOST_RE = re.compile(r"[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?(\.[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?)*")

# Where every workload's traces go. One constant, used both by the Instrumentation
# resource the OTel Operator resolves for injected SDKs and by the agent plane, which
# emits its own OTLP over plain HTTP rather than carrying an SDK. Two spellings of one
# endpoint is how half the plane ends up tracing into nothing.
OTLP_ENDPOINT = "http://tempo.tempo.svc:4318"

# Resources the OpenTelemetry operator injects as the
# opentelemetry-auto-instrumentation-<lang> init container. Every language
# block on the generated Instrumentation CR sets the field named in
# INSTRUMENTATION_RESOURCE_FIELD from this so injection does not depend on
# operator defaults; derived_quota charges the same numbers (Kubernetes bills
# max(app container, init container) per pod), so a change here is a change
# to every environment's ceiling.
INSTRUMENTATION_INIT = {
    "memory": "128Mi",
    "memoryLimit": "128Mi",
    "cpu": "50m",
    "cpuLimit": "500m",
}

# OpenTelemetry Operator 0.158.0 (Instrumentation CRD opentelemetry.io/v1alpha1):
# java's init-container field is `resources`; nodejs, python, dotnet, go,
# apacheHttpd and nginx use `resourceRequirements`. A server-side dry-run of
# 0.158.0 refused `.spec.java.resourceRequirements` as undeclared in schema.
INSTRUMENTATION_RESOURCE_FIELD = {
    "java": "resources",
    "nodejs": "resourceRequirements",
    "python": "resourceRequirements",
    "dotnet": "resourceRequirements",
    "go": "resourceRequirements",
    "apacheHttpd": "resourceRequirements",
    "nginx": "resourceRequirements",
}

# Where the generated Grafana dashboards land — the ONE namespace outside a project's own
# that the generator has always written to, named here because the namespace allowlist
# below (guard_namespaces) enumerates every namespace a generated manifest may target.
DASHBOARD_NAMESPACE = "monitoring"

# Every generated tenant Probe identity is digest-backed, even when the readable tuple
# would fit.  Keeping the full 200 bits means shortening can never erase identity.  The
# same stem is used for metadata.name, jobName and the filename; the latter gets only the
# non-identifying .yaml suffix.
PROBE_IDENTITY_PREFIX = "tenant-probe-"
PROBE_DIGEST_HEX_LEN = 50       # 200 bits
KUBERNETES_NAME_MAX = 63
GENERATED_PATH_MAX = 253

BANNER = "# GENERATED by scripts/catalog-sync.py from catalog/projects.yaml — do not edit.\n"


def _sha256_hexdigest(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


class GeneratedIdentityClaims:
    """Render-local registry for generated names and paths.

    A digest is collision-resistant, not collision-proof.  Claims turn a collision into
    a render error that names both full source tuples.  Paths for established output use
    this registry too, so a new Probe can never overwrite an Application or dashboard.
    """
    def __init__(self):
        self._claims: dict[tuple[str, str], tuple[str, ...]] = {}

    def claim(self, identity_kind: str, emitted: str,
              source: tuple[str, ...]) -> None:
        key = (identity_kind, emitted)
        prior = self._claims.get(key)
        if prior is not None and prior != source:
            sys.exit(f"generated identity collision for {identity_kind} {_r(emitted)}: "
                     f"source tuple {_r(prior)} and source tuple {_r(source)} both emit it")
        self._claims[key] = source


def generated_path_identity(relative: str) -> str:
    """Identity-claims kind for a generated path under clusters/."""
    if relative.startswith("clusters/orbit/"):
        return "clusters/orbit path"
    return "clusters/apps path"


def generated_probe_identity(pid: str, env: str, probe_kind: str,
                             claims: GeneratedIdentityClaims,
                             digest_fn=_sha256_hexdigest) -> tuple[str, str]:
    """Return the one canonical (resource/job name, filename) Probe identity.

    JSON encodes the full tuple with boundaries, avoiding concatenation ambiguities such
    as (a, b-c) versus (a-b, c).  `digest_fn` is an explicit production-render seam used
    by the collision regression; normal rendering always uses SHA-256.
    """
    source = (str(pid), str(env), str(probe_kind))
    payload = json.dumps(source, ensure_ascii=True, separators=(",", ":")).encode()
    digest = str(digest_fn(payload))
    if not re.fullmatch(r"[0-9a-f]+", digest) or len(digest) < PROBE_DIGEST_HEX_LEN:
        sys.exit("generated Probe digest must contain at least "
                 f"{PROBE_DIGEST_HEX_LEN} lowercase hexadecimal characters")
    name = PROBE_IDENTITY_PREFIX + digest[:PROBE_DIGEST_HEX_LEN]
    filename = name + ".yaml"
    if len(name) > KUBERNETES_NAME_MAX:
        sys.exit(f"generated Probe name {_r(name)} is {len(name)} characters; the "
                 f"Kubernetes limit is {KUBERNETES_NAME_MAX}")
    if len(filename) > GENERATED_PATH_MAX:
        sys.exit(f"generated Probe path component {_r(filename)} is {len(filename)} "
                 f"characters; the limit is {GENERATED_PATH_MAX}")
    claims.claim("Probe metadata.name in monitoring", name, source)
    claims.claim("Probe spec.jobName", name, source)
    claims.claim("clusters/apps path", f"clusters/apps/{filename}", source)
    return name, filename


# Every key a project entry may carry (the generator reads each of them; nothing else).
PROJECT_KEYS = frozenset({"id", "name", "blurb", "repo", "tags", "hosted", "managed_elsewhere", "showcase",
                          "internal", "url", "database", "secrets", "environments", "components"})
# The chart names its own database backup CronJob `<project>-db-backup`: a component of
# that name beside a database would be the same object twice, which the chart refuses at
# render time, after the commit.
DB_BACKUP_COMPONENT = "db-backup"


def feature_on(inst: dict, name: str) -> bool:
    """Whether an optional platform feature is on for this instance (the registry in
    provisioning/features.yaml read against the config's `features:`). A tree vendored
    before the registry existed has every feature on: that is what it rendered."""
    try:
        registry = orbit_features.load_registry(ROOT)
    except orbit_features.RegistryMissing:
        return True
    except orbit_features.FeatureError as e:
        sys.exit(str(e))
    try:
        return orbit_features.states(inst, registry).get(name, True)
    except orbit_features.FeatureError as e:
        sys.exit(f"instance/config.yaml: {e}")


def load_instance() -> dict:
    """instance/config.yaml, validated for the keys the generator consumes.

    Missing file or missing key dies at generation, the same posture as every other
    config error: an instance without a declared domain set must not silently render
    manifests that point at nothing."""
    if not INSTANCE.exists():
        sys.exit("instance/config.yaml is missing — the generator refuses to guess "
                 "which instance it is generating for")
    inst = yaml.safe_load(INSTANCE.read_text()) or {}
    for path in (("git", "repoUrl"), ("domains", "primary"), ("domains", "dev"),
                 ("domains", "dnsZones"), ("registry",)):
        node = inst
        for key in path:
            if not isinstance(node, dict) or key not in node:
                sys.exit(f"instance/config.yaml is missing {'.'.join(path)}")
            node = node[key]
    return inst


# Loaded LAZILY, on the first call, not at import. This module is imported as a library by
# scripts/verify/am-routes.py, which reuses `_agent_alertname` and DERIVED_ALERT_PREFIX so
# the delivery oracle and the generator cannot drift on what an alert is even called. A
# module-level `load_instance()` made that import sys.exit() from anywhere that is not an
# instance root — so the oracle's `--catalog` mode only ran from one directory, and the
# alternative (copying the prefix into the oracle) is the drift this import exists to
# prevent. Nothing else changes: every consumer below is inside a function, so the first
# real use still dies on a missing or malformed instance/config.yaml exactly as before.
_INST_CACHE: dict | None = None
def instance() -> dict:
    global _INST_CACHE
    if _INST_CACHE is None:
        _INST_CACHE = load_instance()
    return _INST_CACHE


def orbitd_apply_pairs(inst: dict) -> frozenset[str]:
    """project/env pairs whose Application is written under clusters/orbit/, not apps/."""
    raw = (inst.get("orbitd") or {}).get("applyPairs") or []
    if raw in (None, ""):
        return frozenset()
    if not isinstance(raw, list):
        sys.exit("orbitd.applyPairs: must be a list of project/env strings, or absent")
    return frozenset(str(item).strip() for item in raw if str(item).strip())


def secrets_plane(inst: dict) -> str:
    """Which plane supplies chart Secrets: sops, always.

    One key decides the plane: `secrets.provider`. Absent means sops, and `sops`
    says so explicitly. Either way the generator renders no Secret producer and
    marks every declared Secret `from: external`, so the chart declares them and
    creates none — the Secrets come from SopsSecrets applied by the desk.
    Anything else is refused. `secretsPlane` shipped in no release and is refused."""
    if "secretsPlane" in inst:
        sys.exit("instance/config.yaml secretsPlane is withdrawn — it shipped in no "
                 "release. One key decides the secret plane: secrets.provider "
                 "('sops')")
    provider = (inst.get("secrets") or {}).get("provider", "sops")
    if provider == "sops":
        return "sops"
    sys.exit(f"instance/config.yaml secrets.provider {_r(provider)} is invalid — "
             f"sops is the only secret plane")


# The platform's own hostnames under the primary domain (the template's Ingresses:
# Grafana, orbitd, the MCP door, the intake door). No project may name one.
PLATFORM_HOST_LABELS = ("grafana", "intake", "mcp", "orbit")


def registrable(host: str, zones: list[str]) -> str | None:
    """Which managed zone, if any, this hostname sits inside."""
    for z in sorted(zones, key=len, reverse=True):
        if host == z or host.endswith("." + z):
            return z
    return None


def env_hostnames(pid: str, env: str, envcfg: dict, defaults: dict) -> list[str]:
    """
    Normalised and deduplicated, because every caller needs the same answer.

    Normalising at one call site and not the others is how `probeHosts` and the portfolio
    `url` kept emitting "HUB.<Domain>." in raw case after the Ingress had been fixed. Deduplicating
    here because a host repeated across `hostnames` and `customDomains` rendered duplicate
    Ingress rules and a Certificate listing the same dnsName several times.
    """
    # `expose: false` means this environment has no public door at all: no hostname, and
    # so no Ingress, no certificate and no probe. It exists because the default is to
    # invent `<id>.<domain>` for anything without an explicit hostname, and for a service
    # that only its neighbours call — a webhook receiver, a worker's admin port — that
    # default silently publishes an unauthenticated endpoint to the internet. Reachability
    # then rests on nobody guessing the name.
    if envcfg.get("expose") is False:
        if envcfg.get("hostnames") or envcfg.get("customDomains"):
            sys.exit(f"{_r(pid)}/{_r(env)} sets expose: false but also names hostnames — "
                     f"one of the two is a mistake, and guessing which would publish or "
                     f"hide a service against the author's intent")
        return []
    hosts = list(envcfg.get("hostnames") or [])
    # An explicitly empty `hostnames: []` declares no public door — no default is
    # invented for it. That is the shape the agent plane uses (via `expose: false`)
    # and what the dashboard generator reads: with no hostnames the chart renders no
    # Ingress, so a web component there has no Traefik service and gets span-metrics
    # panels instead. The default below applies only when the key is absent.
    if not hosts and "hostnames" not in envcfg:
        base = defaults["domain"] if env == "prod" else defaults["devDomain"]
        hosts = [f"{pid}.{base}"]
    hosts += [c["host"] for c in envcfg.get("customDomains") or []]
    seen, out = set(), []
    for h in hosts:
        h = str(h).lower().rstrip(".")
        if h not in seen:
            seen.add(h)
            out.append(h)
    return out


def tls_groups(pid: str, env: str, hosts: list[str], defaults: dict, zones: list[str]) -> list[dict]:
    """
    Split hosts into: covered by the platform wildcard (no Certificate needed — Traefik's
    TLSStore default serves it), vs needing their own cert, grouped by challenge type.

    An Ingress can only reference a TLS Secret in its own namespace, so anything the
    platform wildcard already covers must NOT be named here.
    """
    platform_zone = defaults["domain"]
    dns_managed, http_only = [], []
    for h in hosts:
        # The platform wildcard covers <x>.<domain> and <x>.<devDomain> and the apex.
        labels = h[: -(len(platform_zone) + 1)].split(".") if h.endswith("." + platform_zone) else None
        if h == platform_zone or (labels is not None and (len(labels) == 1 or labels[-1:] == ["dev"] and len(labels) == 2)):
            continue  # served by the platform default certificate
        (dns_managed if registrable(h, zones) else http_only).append(h)

    groups = []
    if dns_managed:
        groups.append({"secretName": f"{pid}-{env}-dns-tls", "issuer": "letsencrypt-dns",
                       "hosts": sorted(dns_managed), "managed": True})
    if http_only:
        groups.append({"secretName": f"{pid}-{env}-http-tls", "issuer": "letsencrypt-http",
                       "hosts": sorted(http_only), "managed": True})
    return groups


# Capacity knobs and environment variables an environment may overlay on a component.
# Anything else is either a typo that would render nothing, or a shape change (image,
# path, kind) that is not an overlay — those belong on the component itself. Closed so a
# misspelt `memroy:` cannot silently leave prod's reservation in place. `env` merges key
# by key over the component's own env (a null value removes that key in that env only):
# a credential one environment needs and another must not reference — a dev-only secret —
# had no way to be declared (S08a on the V1 test box, docs/V1.md §8).
COMPONENT_OVERRIDE_KEYS = frozenset({"cpu", "drain", "env", "memory", "memoryLimit", "replicas"})
# The keys an environment block may carry. A key outside this set is a typo that would
# otherwise be ignored in silence (`hostname:` for `hostnames:` publishes the default
# name instead of the intended one).
ENV_KEYS = frozenset({"enabled", "branch", "hostnames", "customDomains", "expose", "components"})
# Component keys the generator consumes itself and strips before the chart sees them.
GENERATOR_COMPONENT_KEYS = frozenset({"repo", "environments", "hosts"})
# A component name becomes part of a Kubernetes object name (<project>-<component>), a
# NetworkPolicy name and a tags.yaml key: the chart's own pattern, applied here first.
COMPONENT_NAME_RE = re.compile(r"[a-z0-9]([a-z0-9-]{0,20}[a-z0-9])?")
_JSON_TYPES = {"string": str, "integer": int, "number": (int, float), "boolean": bool,
               "array": list, "object": dict}


def _chart_pattern_matches(pattern: str, value: str) -> bool:
    """helm applies a schema `pattern` with Go's regexp: unanchored search, and `$` is the
    end of the text. Python's `$` also matches just before a trailing newline (the trap
    documented at HOST_RE above), so a `$`-ended pattern is matched to the end of the text."""
    if pattern.endswith("$") and not pattern.endswith("\\$"):
        pattern = pattern[:-1] + r"\Z"
    return re.search(pattern, value) is not None


def _check_against_schema(where: str, key: str, value, prop: dict, skip=frozenset()) -> None:
    """One catalog value against the chart's values.schema.json property for it: type,
    pattern, enum, minimum and maximum, then every entry of an array and every property
    of a mapping (closed where the chart closes it). The chart would refuse the same
    value at render time, on a green commit that has already left an Application broken;
    the generator refuses first, naming the field and the shape it wants. `skip` names
    the properties the generator rewrites before they reach the chart."""
    jtype = prop.get("type")
    if jtype:
        types = [jtype] if isinstance(jtype, str) else list(jtype)
        ok = False
        for t in types:
            py = _JSON_TYPES.get(t)
            if py is None:
                ok = True
            elif t in ("integer", "number") and isinstance(value, bool):
                continue
            elif isinstance(value, py):
                ok = True
        if not ok:
            sys.exit(f"{where}: `{key}` must be of type {'/'.join(types)} (got {_r(value)})")
    if "pattern" in prop and isinstance(value, str) and not _chart_pattern_matches(prop["pattern"], value):
        # a schedule is shown as written when it is words, digits and cron's punctuation
        shape = "schedule" if key.rsplit(".", 1)[-1] == "schedule" else "name"
        sys.exit(f"{where}: `{key}` {_r(value, shape)} does not match the chart's pattern "
                 f"{prop['pattern']}")
    if "enum" in prop and value not in prop["enum"]:
        sys.exit(f"{where}: `{key}` {_r(value)} is not one of {_s(prop['enum'])}")
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if "minimum" in prop and value < prop["minimum"]:
            sys.exit(f"{where}: `{key}` {_r(value)} is below the chart's minimum {prop['minimum']}")
        if "maximum" in prop and value > prop["maximum"]:
            sys.exit(f"{where}: `{key}` {_r(value)} is above the chart's maximum {prop['maximum']}")
    if isinstance(value, list) and isinstance(prop.get("items"), dict):
        for index, item in enumerate(value):
            _check_against_schema(where, f"{key}[{index}]", item, prop["items"])
    if isinstance(value, dict) and isinstance(prop.get("properties"), dict):
        props = prop["properties"]
        if prop.get("additionalProperties") is False:
            unknown = sorted(set(value) - set(props), key=str)
            if unknown:
                sys.exit(f"{where}: `{key}` carries the unknown key {_r(unknown[0], 'key')}. "
                         f"Allowed keys: {sorted(props)}")
        for name in prop.get("required") or []:
            if value.get(name) is None:
                sys.exit(f"{where}: `{key}` must carry `{name}`")
        for name, item in value.items():
            if name in props and name not in skip and item is not None:
                _check_against_schema(where, f"{key}.{name}", item, props[name])


def _check_hostname(where: str, host) -> None:
    """The same test the render pass applies to the merged hostname list, here with the
    key and index in front so the operator can find the line."""
    normal = str(host).lower().rstrip(".")
    if "*" in normal:
        sys.exit(f"{where}: wildcard hostname {_r(host, 'hostname')} is not allowed")
    if not HOST_RE.fullmatch(normal):
        sys.exit(f"{where}: {_r(host, 'hostname')} is not a DNS name")


def _string_list(where: str, key: str, value, shape="name") -> None:
    if not isinstance(value, list):
        sys.exit(f"{where}: `{key}` must be a list (got {_r(value)}); a bare scalar there "
                 f"would be read one character at a time")
    for item in value:
        if not isinstance(item, str) or not item:
            sys.exit(f"{where}: every `{key}` entry must be a non-empty string (got {_r(item, shape)})")


def check_catalog_shape(cat, inst: dict, tags_doc) -> None:
    """Refuse a catalog, tags file or instance config whose SHAPE is wrong, once, before
    the render loop: the right kind of value at every key the generator or the chart
    reads. The grammar checks that follow (ids, hostnames, quantities) assume the shape
    holds; without this pass a scalar where a list belongs was iterated character by
    character into hostnames, a mistyped key was ignored, and a missing key was a Python
    traceback (area-2 sweep, round 1)."""
    if not isinstance(cat, dict):
        sys.exit("catalog/projects.yaml must be a mapping with a `projects:` list")
    projects = cat.get("projects")
    if not isinstance(projects, list):
        sys.exit("catalog/projects.yaml: `projects` must be a list of project mappings")
    if not isinstance(tags_doc, dict) or not isinstance(tags_doc.get("tags", {}) or {}, dict):
        sys.exit("catalog/tags.yaml must be a mapping whose `tags` is a mapping of "
                 "<project>/<env>/<component> → tag")
    for key, value in (tags_doc.get("tags") or {}).items():
        parts = str(key).split("/")
        if (len(parts) != 3 or not ID_RE.fullmatch(parts[0]) or not ID_RE.fullmatch(parts[1])
                or not COMPONENT_NAME_RE.fullmatch(parts[2])):
            sys.exit(f"catalog/tags.yaml: key {_r(key, 'label')} is not <project>/<env>/<component>")
        if not isinstance(value, str) or not TAG_RE.fullmatch(value):
            sys.exit(f"catalog/tags.yaml: {key}: the tag {_r(value)} is not a valid image tag")
    domains = inst.get("domains") or {}
    for key in ("primary", "dev"):
        if not isinstance(domains.get(key), str) or not HOST_RE.fullmatch(domains[key]):
            sys.exit(f"instance/config.yaml domains.{key} must be a DNS name "
                     f"(got {_r(domains.get(key), 'hostname')})")
    _string_list("instance/config.yaml domains", "dnsZones", domains.get("dnsZones"), "hostname")
    for zone in domains["dnsZones"]:
        if not HOST_RE.fullmatch(zone):
            sys.exit(f"instance/config.yaml domains.dnsZones entry {_r(zone, 'hostname')} is not a DNS name")
    _chart_values()  # the chart itself first: its absence is reported as such
    chart_props = _chart_schema()["properties"]
    comp_props = _chart_component_schema()
    comp_required = [k for k in (chart_props["components"].get("items") or {}).get("required") or []
                     if k != "name"]  # name has its own, shaped refusal below
    for key in ("database", "secrets"):
        if not isinstance(chart_props.get(key), dict):
            sys.exit(f"service chart schema declares no `{key}` property")
    for index, p in enumerate(projects):
        where = f"projects[{index}]"
        if not isinstance(p, dict):
            sys.exit(f"{where} must be a mapping (got {_r(p)})")
        pid = p.get("id")
        if not isinstance(pid, str) or not ID_RE.fullmatch(pid):
            sys.exit(f"{where}: `id` must be a catalog id (got {_r(pid)})")
        where = pid
        # A closed key set. A key nothing reads is a typo that changes nothing and says
        # nothing (`replicas:` at the project's top level, `enviroments:`), found the day
        # the thing it was meant to do is missed.
        # (`_extra` is the generator's own channel; a catalog that names it has its own,
        # sharper refusal further down)
        unknown = sorted(str(k) for k in p if k not in PROJECT_KEYS and k != "_extra")
        if unknown:
            sys.exit(f"{where}: unknown key {_r(unknown[0], 'key')} in the project entry. A project "
                     f"may carry: {', '.join(sorted(PROJECT_KEYS))}")
        if "name" in p and not isinstance(p["name"], str):
            sys.exit(f"{where}: `name` must be a string (got {_r(p['name'])})")
        if (p.get("showcase") or p.get("internal")) and not isinstance(p.get("name"), str):
            sys.exit(f"{where}: a showcase or internal project must carry a `name` — the "
                     f"portfolio card is titled by it")
        if p.get("database") is not None:
            if not isinstance(p["database"], dict):
                sys.exit(f"{where}: `database` must be a mapping (got {_r(p['database'])})")
            # parameters are stringified by the generator before they reach the chart and
            # gated by check_database_parameters; everything else is passed through
            _check_against_schema(where, "database", p["database"], chart_props["database"],
                                  skip=frozenset({"parameters"}))
        if p.get("secrets") is not None:
            if not isinstance(p["secrets"], list) or not all(isinstance(x, dict) for x in p["secrets"]):
                sys.exit(f"{where}: `secrets` must be a list of mappings")
            _check_against_schema(where, "secrets", p["secrets"], chart_props["secrets"])
        envs = p.get("environments")
        if envs is not None and not isinstance(envs, dict):
            sys.exit(f"{where}: `environments` must be a mapping of name → settings (got {_r(envs)})")
        for env, envcfg in (envs or {}).items():
            if not isinstance(env, str) or not ID_RE.fullmatch(env):
                sys.exit(f"{where}: environment name {_r(env)} must be a catalog id (YAML reads "
                         f"a bare on/yes/no or a number as something other than a name)")
            ewhere = f"{where}/{_s(env)}"
            if envcfg is None:
                continue
            if not isinstance(envcfg, dict):
                sys.exit(f"{ewhere}: environment settings must be a mapping (got {_r(envcfg)})")
            unknown = sorted(set(envcfg) - ENV_KEYS, key=str)
            if unknown:
                sys.exit(f"{ewhere}: unknown environment key {_r(unknown[0], 'key')}. "
                         f"Allowed keys: {sorted(ENV_KEYS)}")
            if "hostnames" in envcfg and envcfg["hostnames"] is not None:
                _string_list(ewhere, "hostnames", envcfg["hostnames"], "hostname")
                for index, host in enumerate(envcfg["hostnames"]):
                    _check_hostname(f"{ewhere}: hostnames[{index}]", host)
            if envcfg.get("customDomains") is not None:
                if not isinstance(envcfg["customDomains"], list):
                    sys.exit(f"{ewhere}: `customDomains` must be a list of {{host: …}} mappings")
                for index, entry in enumerate(envcfg["customDomains"]):
                    if not isinstance(entry, dict) or not isinstance(entry.get("host"), str):
                        sys.exit(f"{ewhere}: every `customDomains` entry must be a mapping with a "
                                 f"string `host` (got {_r(entry, 'hostname')})")
                    _check_hostname(f"{ewhere}: customDomains[{index}].host", entry["host"])
            overlays = envcfg.get("components")
            if isinstance(overlays, dict):
                # the overlay is merged over the component before the chart sees it, so
                # its values face the same schema (its keys are closed by
                # check_component_overrides)
                for cname, overlay in overlays.items():
                    if not isinstance(overlay, dict):
                        continue
                    for key, value in overlay.items():
                        if key in comp_props and value is not None:
                            _check_against_schema(f"{ewhere}/components/{_s(cname)}", key, value,
                                                  comp_props[key])
            for key in ("enabled", "expose"):
                if key in envcfg and not isinstance(envcfg[key], bool):
                    sys.exit(f"{ewhere}: `{key}` must be true or false (got {_r(envcfg[key])})")
            if "branch" in envcfg and not isinstance(envcfg["branch"], str):
                sys.exit(f"{ewhere}: `branch` must be a string (got {_r(envcfg['branch'])})")
        comps = p.get("components")
        if comps is not None and not isinstance(comps, list):
            sys.exit(f"{where}: `components` must be a list of component mappings (got {_r(comps)})")
        seen_names: set[str] = set()
        for cindex, c in enumerate(comps or []):
            cwhere = f"{where}/components[{cindex}]"
            if not isinstance(c, dict):
                sys.exit(f"{cwhere} must be a mapping (got {_r(c)})")
            name = c.get("name")
            if not isinstance(name, str) or not COMPONENT_NAME_RE.fullmatch(name):
                sys.exit(f"{cwhere}: `name` must match {COMPONENT_NAME_RE.pattern} (got {_r(name)})")
            if name == DB_BACKUP_COMPONENT and p.get("database") is not None:
                sys.exit(f"{cwhere}: the component name `{DB_BACKUP_COMPONENT}` is taken by the project's own "
                         f"database backup job ({pid}-{DB_BACKUP_COMPONENT}): rename the component")
            if name in seen_names:
                sys.exit(f"{where}: two components are named {name!r}")
            seen_names.add(name)
            cwhere = f"{where}/{name}"
            unknown = sorted(set(c) - set(comp_props) - GENERATOR_COMPONENT_KEYS, key=str)
            if unknown:
                sys.exit(f"{cwhere}: unknown component key {_r(unknown[0], 'key')}. Allowed keys: "
                         f"{sorted(set(comp_props) | GENERATOR_COMPONENT_KEYS)}")
            for key in comp_required:
                if c.get(key) is None:
                    sys.exit(f"{cwhere}: a component must carry `{key}` (the chart requires it)")
            for key, value in c.items():
                # egress keeps its purpose-written refusals (check_component_egress) in
                # front of the chart's closed mapping
                if key in comp_props and key != "egress" and value is not None:
                    _check_against_schema(cwhere, key, value, comp_props[key])
            if c.get("hosts") is not None:
                _string_list(cwhere, "hosts", c["hosts"], "hostname")
# Postgres GUC names as CNPG accepts them on spec.postgresql.parameters. The two the
# product sets are max_connections and shared_buffers; the pattern is the gate, not a
# allow-list, so a measured work_mem is expressible without a schema bump.
GUC_NAME_RE = re.compile(r"^[a-z_]+$")
# Same text helm's values.schema.json emits for a key that fails `^[a-z_]+$`
# (patternProperties + additionalProperties: false). The generator must fail
# first, and with this wording, so `make check` and `helm template` name the
# same mistake.
GUC_KEY_MSG = "at '/database/parameters': additional properties '{key}' not allowed"


def components_for(p: dict, env: str) -> list[dict]:
    """The components of project `p` that exist in environment `env`.

    A component may declare `environments: [dev]` to exist only where it is wanted;
    absent, it renders everywhere. This exists for a test instrument: a replay server
    that feeds a dev listener has to live in that listener's OWN namespace, because the
    default-deny NetworkPolicy admits a namespace's own pods and nothing else, and it
    must never be rendered into production. A separate catalog project would have put it
    in a separate namespace, which the policy would then block.

    `environments.<env>.components.<name>` may overlay replicas, cpu, memory,
    memoryLimit, drain and env on that component for that env only; `env` merges key by
    key over the component's env, and a null value removes the key there. An env with no
    overlay returns the catalog component unchanged, so its render stays byte-identical.
    """
    env_overrides = ((p.get("environments") or {}).get(env) or {}).get("components") or {}
    out = []
    for c in (p.get("components") or []):
        envs = c.get("environments")
        if envs is None or env in envs:
            overlay = env_overrides.get(c.get("name")) if isinstance(env_overrides, dict) else None
            if overlay:
                merged = {**c, **{k: v for k, v in overlay.items() if k != "env"}}
                if "env" in overlay:
                    env_vars = {**(c.get("env") or {}), **(overlay.get("env") or {})}
                    env_vars = {k: v for k, v in env_vars.items() if v is not None}
                    if env_vars:
                        merged["env"] = env_vars
                    else:
                        merged.pop("env", None)
                out.append(merged)
            else:
                out.append(c)
    return out


def check_component_environments(p: dict) -> None:
    """Refuse an `environments:` entry that names an environment the project has not
    declared. Unchecked, `environments: [devv]` renders the component into nothing at
    all and no manifest, no sync and no probe reports anything — the component simply
    is not there, which is indistinguishable from having forgotten to add it."""
    declared = set((p.get("environments") or {}).keys())
    for c in (p.get("components") or []):
        envs = c.get("environments")
        if envs is None:
            continue
        if not isinstance(envs, list) or not envs:
            sys.exit(f"{_s(p['id'])}/{_s(c.get('name'))}: `environments` must be a non-empty list")
        unknown = sorted(set(envs) - declared, key=str)
        if unknown:
            sys.exit(f"{_s(p['id'])}/{_s(c.get('name'))}: `environments` names {_s(unknown)}, which "
                     f"{_s(p['id'])} does not declare (has {_s(sorted(declared))})")


def check_component_overrides(p: dict) -> None:
    """Refuse an environment overlay that names an unknown key or a component the
    project does not have. Unchecked, `memroy: 128Mi` leaves prod's reservation in
    place and reports nothing, and `components: {webx: {replicas: 1}}` is a typo
    that looks deployed."""
    names = {c.get("name") for c in (p.get("components") or []) if isinstance(c, dict)}
    for env, envcfg in (p.get("environments") or {}).items():
        overrides = (envcfg or {}).get("components")
        if overrides is None:
            continue
        if not isinstance(overrides, dict):
            sys.exit(f"{_s(p['id'])}/{_s(env)}: environments.{_s(env)}.components must be a mapping "
                     f"of component name → overlay")
        for name, overlay in overrides.items():
            if name not in names:
                sys.exit(f"{_s(p['id'])}/{_s(env)}: {_r(name)} is not a component of {p['id']} "
                         f"(has {sorted(n for n in names if n)})")
            if overlay is None:
                continue
            if not isinstance(overlay, dict):
                sys.exit(f"{_s(p['id'])}/{_s(env)}: component override {_r(name)} must be a mapping")
            unknown = sorted(set(overlay) - COMPONENT_OVERRIDE_KEYS, key=str)
            if unknown:
                sys.exit(f"{_s(p['id'])}/{_s(env)}: component override {_r(name)} declares unknown "
                         f"key {_r(unknown[0])}. Allowed keys: {sorted(COMPONENT_OVERRIDE_KEYS)}")


def check_database_parameters(p: dict) -> None:
    """Refuse a `database.parameters` key the chart would refuse. The message is the
    chart's `fail()` text so `make check` and `helm template` name the same mistake."""
    db = p.get("database")
    if not isinstance(db, dict):
        return
    params = db.get("parameters")
    if not params:
        return
    if not isinstance(params, dict):
        sys.exit(f"{_s(p['id'])}: database.parameters must be a map of GUC name → value")
    for key, value in params.items():
        if not isinstance(key, str) or not GUC_NAME_RE.fullmatch(key):
            sys.exit(f"{_s(p['id'])}: " + GUC_KEY_MSG.format(key=_s(key, "key")))
        if isinstance(value, bool) or value is None:
            sys.exit(f"{_s(p['id'])}: database.parameters[{_r(key)}] must be a string")
        if not isinstance(value, (str, int, float)):
            sys.exit(f"{_s(p['id'])}: database.parameters[{_r(key)}] must be a string")


def retired_archives() -> dict:
    """catalog/retired.yaml: project id -> the highest archive generation a removed project
    of that name wrote under. A removed project's backups and WAL stay in the bucket (the
    removal deletes no data), and barman refuses to archive into a path that holds another
    database's files: a project made again under the name ran, answered, and archived
    nothing for as long as it lived (S01 on the rebuilt test box, 2026-10-09: 0 segments
    archived, 36 failures in 12 minutes, docs/V1.md §8). The file is how the next project
    of that name is given a path of its own without anyone remembering to."""
    if not RETIRED.is_file():
        return {}
    doc = yaml.safe_load(RETIRED.read_text(encoding="utf-8")) or {}
    retired = doc.get("retired") if isinstance(doc, dict) else None
    if not isinstance(retired, dict) or not all(
            isinstance(k, str) and isinstance(v, int) and not isinstance(v, bool) and v >= 1
            for k, v in retired.items()):
        sys.exit("catalog/retired.yaml must be `retired:` with a map of project id -> archive "
                 "generation (an integer, 1 or more); it is written by `catalog-edit.py remove`")
    return retired


def database_values(p: dict, retired: int = 0) -> dict:
    """The project's `database:` block as helm values: parameters stringified so a
    catalog `max_connections: 60` is the string CNPG's field expects, and an empty
    parameters map dropped so the Cluster stays byte-identical to a block that never
    named one. `retired` is the generation a removed project of this name archived under
    (retired_archives): this one archives under the next, or under a later one it names."""
    db = dict(p["database"])
    if retired:
        named = db.get("archiveGeneration")
        if named is None:
            db["archiveGeneration"] = retired + 1
        elif named <= retired:
            sys.exit(f"{_s(p['id'])}: database.archiveGeneration is {_r(named)} and a removed project of "
                     f"this name archived under generation {retired} (catalog/retired.yaml): its files "
                     f"are still in the bucket and the database would archive nothing. Leave the key "
                     f"out (it becomes {retired + 1}) or name a later one.")
    params = db.get("parameters")
    if not params:
        db.pop("parameters", None)
        return db
    db["parameters"] = {k: str(v) for k, v in params.items()}
    return db


def dashboard_uid(pid: str, env: str, digest_fn=_sha256_hexdigest) -> str:
    """Grafana caps a uid at 40 characters. Truncating the readable form made two
    environments of one long project id share a uid, and Grafana then kept whichever the
    sidecar loaded last; a uid that does not fit is digest-backed instead (the title still
    carries the readable pair), and the render claims every uid as an identity so a
    collision is a render error rather than a missing dashboard."""
    readable = f"orbit-red-{pid}-{env}"
    if len(readable) <= 40:
        return readable
    payload = json.dumps([str(pid), str(env)], ensure_ascii=True, separators=(",", ":")).encode()
    return "orbit-red-" + str(digest_fn(payload))[:30]


def red_dashboard(pid: str, env: str, p: dict, hostnames: list, wal: bool, probes: bool = True,
                  digest_fn=_sha256_hexdigest) -> dict:
    """One generated RED dashboard per project+env (OBSERVABILITY.md Decision 6).

    The eBPF floor is deleted (phase 8), so this draws on what is already there:
    a web component WITH a public route gets rate, errors and latency from
    Traefik's own metrics, selected by that component's own service label; a
    worker — and a web component with no public route, which the chart renders
    no Ingress for — appears through the span-metrics generator, selected by the
    OTEL_SERVICE_NAME the chart publishes plus the environment dimension Tempo
    is configured to write. A span panel exists and is empty until the product
    emits spans, because an omitted panel reads as a component nobody deployed.
    A panel pinned to a value that exists only when something is wrong (a 5xx
    code, OOMKilled) falls back to zero on a healthy system, where an empty
    vector would render as "No data" — indistinguishable from a broken query.
    The two health panels need no instrumentation at all. Panels beyond RED
    appear only when the catalog declares the fact they draw (database -> CNPG,
    hostnames -> blackbox). Generated, not hand-drawn: the catalog is the source
    of truth for what deserves a panel, and a project added tomorrow gets its
    dashboard in the same sync that deploys it."""
    ns = f"{pid}-{env}"
    ds = {"type": "prometheus", "uid": "prometheus"}

    def stat(title, expr, x, y, unit="none", w=6, h=4):
        return {"type": "stat", "title": title, "gridPos": {"h": h, "w": w, "x": x, "y": y},
                "datasource": ds, "fieldConfig": {"defaults": {"unit": unit}, "overrides": []},
                "targets": [{"expr": expr, "refId": "A"}]}

    def ts(title, exprs, x, y, unit="reqps", legends=None, w=12, h=8):
        legends = legends or ["__auto"] * len(exprs)
        return {"type": "timeseries", "title": title, "gridPos": {"h": h, "w": w, "x": x, "y": y},
                "datasource": ds, "fieldConfig": {"defaults": {"unit": unit}, "overrides": []},
                "targets": [{"expr": e, "refId": chr(65 + i), "legendFormat": legends[i]}
                            for i, e in enumerate(exprs)]}

    def traefik_service(cname: str) -> str:
        # The label Traefik gives the component's Service: the chart renders it
        # as <project>-<component> truncated to 63 characters like every
        # Kubernetes name (charts/service/templates/_helpers.tpl `service.name`),
        # with its port named `http`, addressed by the Ingress by that name, so
        # Traefik's service is <namespace>-<service>-http@kubernetes. Selected
        # per component: a namespace-wide query cannot tell two components apart.
        svc = f"{pid}-{cname}"[:63].rstrip("-")
        return f"{ns}-{svc}-http@kubernetes"

    def has_route(c: dict) -> bool:
        # Whether the chart renders an Ingress backend for this component: the
        # same predicate templates/ingress.yaml uses (a web component with a
        # port, on an environment that has a hostname covering it — see also
        # public_ingress_hosts). Without a route Traefik never creates the
        # service label, so Traefik panels would select nothing for ever.
        if (c.get("kind") or "web") != "web" or not c.get("port"):
            return False
        if not hostnames:
            return False
        allowed = c.get("hosts")
        return (not allowed) or any(h in hostnames for h in allowed)

    req = "traefik_service_requests_total"
    bkt = "traefik_service_request_duration_seconds_bucket"
    calls = "traces_spanmetrics_calls_total"
    lat = "traces_spanmetrics_latency_bucket"
    # The Prometheus label Tempo writes for the environment dimension configured
    # in template/provisioning/clusters/platform/apps/tempo.yaml: the attribute
    # name with every character a label may not carry replaced by an underscore.
    env_label = "deployment_environment_name"
    webs = [c for c in components_for(p, env) if (c.get("kind") or "web") == "web"]
    routed = [c for c in webs if has_route(c)]
    # No route, no Traefik service — but the component still emits spans, and
    # that is where its RED comes from.
    span_webs = [c for c in webs if not has_route(c)]
    workers = [c for c in components_for(p, env) if (c.get("kind") or "web") == "worker"]

    def span_pair(name: str, title_prefix: str) -> list:
        # Selected by the OTEL_SERVICE_NAME the chart publishes
        # (<project>-<component>), scoped to this environment by the dimension
        # Tempo writes for deployment.environment.name.
        svc_name = f"{pid}-{name}"
        return [ts(f"{title_prefix}{name} calls",
                   [f'sum(rate({calls}{{service="{svc_name}",'
                    f'{env_label}="{env}"}}[5m]))',
                    f'sum(rate({calls}{{service="{svc_name}",'
                    f'{env_label}="{env}",status_code="STATUS_CODE_ERROR"}}[5m]))'],
                   0, 0, legends=["calls", "errors"]),
                ts(f"{title_prefix}{name} latency",
                   [f'histogram_quantile(0.95, sum by (le) '
                    f'(rate({lat}{{service="{svc_name}",'
                    f'{env_label}="{env}"}}[5m])))'],
                   0, 0, unit="s", legends=["p95"])]

    panels = []
    for i, c in enumerate(routed):
        svc = traefik_service(c["name"])
        panels.append(stat(f"Requests/s (5m) — {c['name']}",
                           f'sum(rate({req}{{service="{svc}"}}[5m]))',
                           (3 * i % 4) * 6, (3 * i // 4) * 4, unit="reqps"))
        # clamp_min keeps a quiet service reading 0% rather than NaN, which a stat
        # panel renders as "No data" — indistinguishable from a broken query. The
        # `or vector(0)` on the numerator does the same for a healthy service,
        # which has no 5xx series at all.
        panels.append(stat(f"Errors % (5m) — {c['name']}",
                           f'100 * (sum(rate({req}{{service="{svc}",code=~"5.."}}[5m]))'
                           f' or vector(0))'
                           f' / clamp_min(sum(rate({req}{{service="{svc}"}}[5m])), 1e-9)',
                           ((3 * i + 1) % 4) * 6, ((3 * i + 1) // 4) * 4, unit="percent"))
        panels.append(stat(f"p95 latency (5m) — {c['name']}",
                           f'histogram_quantile(0.95, sum by (le) '
                           f'(rate({bkt}{{service="{svc}"}}[5m])))',
                           ((3 * i + 2) % 4) * 6, ((3 * i + 2) // 4) * 4, unit="s"))
    y = 4 * ((3 * len(routed) + 3) // 4) if routed else 0
    wide = []
    if routed:
        wide.append(ts("Request rate by component",
                       [f'sum(rate({req}{{service="{traefik_service(c["name"])}"}}[5m]))'
                        for c in routed],
                       0, 0, legends=[c["name"] for c in routed]))
        wide.append(ts("5xx rate by component",
                       [f'(sum(rate({req}{{service="{traefik_service(c["name"])}",'
                        f'code=~"5.."}}[5m])) or vector(0))' for c in routed],
                       0, 0, legends=[c["name"] for c in routed]))
        for c in routed:
            svc = traefik_service(c["name"])
            wide.append(ts(f"Latency quantiles — {c['name']}",
                           [f'histogram_quantile({q}, sum by (le) '
                            f'(rate({bkt}{{service="{svc}"}}[5m])))'
                            for q in ("0.5", "0.95", "0.99")],
                           0, 0, unit="s", legends=["p50", "p95", "p99"]))
    for w in workers:
        wide.extend(span_pair(w["name"], "Worker "))
    for c in span_webs:
        wide.extend(span_pair(c["name"], ""))
    wide.append(ts("Container restarts",
                   [f'sum by (pod) '
                    f'(kube_pod_container_status_restarts_total{{namespace="{ns}"}})'],
                   0, 0, unit="none", legends=["{{pod}}"]))
    wide.append(ts("OOM kills",
                   [f'(sum by (pod, container) '
                    f'(kube_pod_container_status_last_terminated_reason{{namespace="{ns}",'
                    f'reason="OOMKilled"}}) or vector(0))'],
                   0, 0, unit="none", legends=["{{pod}}/{{container}}"]))
    for i, panel in enumerate(wide):
        panel["gridPos"] = {"h": 8, "w": 12, "x": (i % 2) * 12, "y": y + (i // 2) * 8}
        panels.append(panel)
    y += 8 * ((len(wide) + 1) // 2)
    if p.get("database"):
        db_exprs = [f'sum(cnpg_backends_total{{namespace="{ns}"}})']
        panels.append(ts("Database backends", db_exprs, 0, y, unit="none", legends=["backends"]))
        if wal:
            panels.append(ts("WAL archive age",
                             [f'cnpg_pg_stat_archiver_seconds_since_last_archival{{namespace="{ns}"}}'],
                             12, y, unit="s", legends=["since last archival"]))
        y += 8
    # only while something probes the doors (the uptime feature): a panel of a series
    # nothing produces reads as an outage
    if hostnames and probes:
        host_re = "|".join(h.replace(".", "\\\\.") for h in hostnames)
        panels.append(ts("Public probe success",
                         [f'probe_success{{instance=~"https?://({host_re})/?.*"}}'],
                         0, y, unit="none", legends=["{{instance}}"]))
    # Explicit ids: a provisioned dashboard keeps its JSON verbatim, so without them
    # panel-addressed APIs (solo render, links) have nothing to address.
    for i, pan in enumerate(panels, start=1):
        pan["id"] = i
    dash = {
        "uid": dashboard_uid(pid, env, digest_fn),
        "title": f"RED — {pid} ({env})",
        "description": "Generated from the catalog by catalog-sync.py — do not hand-edit; the sidecar rewrites it.",
        "tags": ["orbit", "red", pid],
        "timezone": "browser", "editable": True, "schemaVersion": 39, "refresh": "30s",
        "time": {"from": "now-6h", "to": "now"},
        "panels": panels,
    }
    return {"apiVersion": "v1", "kind": "ConfigMap",
            "metadata": {"name": f"grafana-dashboard-red-{pid}-{env}",
                         "namespace": DASHBOARD_NAMESPACE,
                         "labels": {"grafana_dashboard": "1",
                                    "orbit.local/project": pid,
                                    "orbit.local/env": env}},
            "data": {f"red-{pid}-{env}.json": json.dumps(dash, indent=1)}}


# --------------------------------------------------------------- the agent plane
# Phase D.2 (ROADMAP, AGENT-PLANE.md v2). kagent was dropped 2026-08-27, so there is no
# CRD layer and no external control plane: an agent is a catalog entry, and THIS is the
# only thing that turns it into something that runs. Every agent renders as an ordinary
# component of one platform project, on the published runtime image, through the same
# chart / Application / namespace machinery as every tenant — which is why the entry is
# this small and why nothing about an agent is invisible to the checks that already
# exist (no credentials in generated manifests, resource policy, pull secrets, netpol).
AGENTS_PROJECT = "orbit-agents"
AGENTS_ENV = "prod"
# The platform layer's inventories are keyed by this word (orbit platform apply); a tenant
# project with this id would name its objects as if they were platform objects.
PLATFORM_PROJECT = "platform"
# ONE namespace for the whole plane, deliberately, and its NetworkPolicy ingress is
# narrowed below to admit ONLY its own pods (unlike a tenant namespace, which also admits
# traefik/monitoring/cnpg). An intake service (D.3) living in a namespace of
# its own could not reach a single agent through that rule — so agents and the things
# that wake them are neighbours here. The narrowing matters because these pods hold the
# org's subscription token; see the namespace loop.
AGENT_PORT = 8080            # the runtime's EXPOSE, and app.py's PORT default
AGENT_HEALTH = "/healthz"    # unauthenticated liveness; every POST is bearer-gated
# The runtime image's own USER (services/agent-runtime/Dockerfile: uid 10001). The chart
# pins 65532 by default and the SDK writes its session state under $HOME, so without
# this override the pod comes up and the first task dies on a permission error inside
# ~/.claude — which reads as a model failure rather than a uid mismatch.
AGENT_UID = 10001
# Settled with the owner 2026-08-27: Sonnet 4.6 is the floor every agent inherits (cheapest
# of the capable tier). A per-agent `model:` overrides it — Haiku for cheap sweeps, Opus
# for hard investigations.
AGENT_MODEL = "claude-sonnet-4-6"
# Where the runtime discovers skills: one directory per skill, each holding a SKILL.md,
# under the SDK's `user` scope (Dockerfile ORBIT_SKILLS_DIR). A ConfigMap volume with
# per-key item paths reproduces that layout; a flat mount would be silently inert.
AGENT_SKILLS_DIR = "/home/orbit/.claude/skills"
SKILLS_SRC = ROOT / ".claude" / "skills"
# The org's subscription setup-token (AGENT-PLANE.md 'Model auth': the ONLY sanctioned
# credential — never an API key). One Kubernetes Secret for the plane, produced by ESO
# from the environment's Infisical store like every other secret here: git holds the
# pointer, the value is written once in the vault and rotated without a commit.
AGENT_MODEL_SECRET = "orbit-claude-code"
AGENT_MODEL_KEY = "CLAUDE_CODE_OAUTH_TOKEN"

# Component names cap at 22 characters in the chart schema, and an agent IS a component.
AGENT_NAME_RE = re.compile(r"[a-z0-9]([a-z0-9-]{0,20}[a-z0-9])?")
AGENT_MODEL_RE = re.compile(r"[a-z0-9][a-z0-9.-]{0,63}")
SKILL_RE = re.compile(r"[a-z0-9]([a-z0-9-]{0,38}[a-z0-9])?")
IMAGE_RE = re.compile(r"[a-z0-9][a-z0-9._/-]{0,255}")
TAG_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")
# A Kubernetes resource quantity — the chart's own values.schema.json pattern, applied
# HERE too so a bad `memory: 512MB` (the SI form Kubernetes rejects) fails in the
# generator where every other agent key does, not downstream at chart render on a green
# commit that has already left an Application permanently broken.
QUANTITY_RE = re.compile(r"[0-9]+(\.[0-9]+)?(E|P|T|G|M|k|Ei|Pi|Ti|Gi|Mi|Ki)?")
CPU_RE = re.compile(r"[0-9]+(\.[0-9]+)?m?")
# Kubernetes' suffix table, so two quantities written in different units can be ORDERED
# (a memory limit must not sit below its request, and "1Gi" < "512Mi" as strings).
QUANTITY_UNITS = {"": 1, "k": 10**3, "M": 10**6, "G": 10**9, "T": 10**12,
                  "P": 10**15, "E": 10**18, "Ki": 1024, "Mi": 1024**2,
                  "Gi": 1024**3, "Ti": 1024**4, "Pi": 1024**5, "Ei": 1024**6}


def _quantity_bytes(q: str) -> float:
    """A validated Kubernetes quantity as a number of bytes. Callers validate the form
    against QUANTITY_RE first; this only has to order two forms that already parse."""
    m = re.fullmatch(r"([0-9]+(?:\.[0-9]+)?)([A-Za-z]*)", str(q))
    if not m:
        sys.exit(f"internal: {_r(q)} reached _quantity_bytes unvalidated")
    return float(m.group(1)) * QUANTITY_UNITS[m.group(2)]


# The same two messages the service chart fails with. A memory-backed emptyDir is
# charged to the container's cgroup; catching this here means `make check` fails
# before helm does. Double quotes around the name match Go's %q for a DNS label.
SCRATCH_REQUIRES_LIMIT = (
    'component "{name}": scratch requires memoryLimit — a memory-backed emptyDir is '
    "charged to the container's cgroup, and without a limit the volume can grow "
    "until the node is the thing that dies"
)
SCRATCH_TOO_LARGE = (
    'component "{name}": scratch.sizeLimit ({size}) must be smaller than memoryLimit '
    "({limit}) — a memory-backed emptyDir is charged to the container's cgroup; a "
    "sizeLimit at or above the limit is an OOMKill waiting for a real file"
)


def check_component_scratch(comp: dict) -> None:
    """Refuse a memory-backed scratch volume that cannot fit under the cgroup limit."""
    scratch = comp.get("scratch")
    if not scratch:
        return
    name = comp.get("name", "")
    limit = comp.get("memoryLimit")
    if not limit:
        sys.exit(SCRATCH_REQUIRES_LIMIT.format(name=name))
    size = scratch.get("sizeLimit") if isinstance(scratch, dict) else None
    if not size or not QUANTITY_RE.fullmatch(str(size)):
        sys.exit(f'component "{name}": scratch.sizeLimit {_r(size)} is not a Kubernetes '
                 f"quantity (e.g. 512Mi, 1Gi — not 512MB)")
    if not QUANTITY_RE.fullmatch(str(limit)):
        sys.exit(f'component "{name}": memoryLimit {_r(limit)} is not a Kubernetes '
                 f"quantity (e.g. 512Mi, 1Gi — not 512MB)")
    if _quantity_bytes(str(size)) >= _quantity_bytes(str(limit)):
        sys.exit(SCRATCH_TOO_LARGE.format(name=name, size=size, limit=limit))


# Per-component egress, the CIDR half (phase 9). A component may declare the
# infrastructure it needs by name (`egress: {cidrs: [postgres, s3, dns]}`) and
# then reaches those addresses and nothing else. `dns` and `postgres` are
# addresses inside the cluster the core renders for any org; anything else is an
# address on the internet that differs per org and comes from the instance as
# `egressTargets: {<alias>: [<cidr>, ...]}`. The core carries no org's addresses.
EGRESS_LABEL = "orbit.local/egress"
EGRESS_BUILTIN_ALIASES = ("dns", "postgres")
EGRESS_KNOWN_KEYS = frozenset({"cidrs"})
EGRESS_LINK_LOCAL = ipaddress.ip_network("169.254.0.0/16")
EGRESS_LINK_LOCAL6 = ipaddress.ip_network("fe80::/10")
# The whole IPv4-mapped IPv6 space. A v6 range inside it — or containing it —
# is a mapped range whatever its own prefix, and the API server refuses every
# one of them.
EGRESS_MAPPED96 = ipaddress.ip_network("::ffff:0.0.0.0/96")

# The value the service chart stamps on a narrowed pod, keyed by chart
# directory so a test that rebinds ROOT re-reads. Read out of the chart's
# values.yaml with a YAML parser, never scraped out of a template's source
# text and never written here: the pod carries the chart's value, so the
# policy must match the chart's value, and two literals in two files drift.
_EGRESS_VALUE_CACHE: dict[str, str] = {}


def _egress_label_value() -> str:
    """The value the service chart stamps beside EGRESS_LABEL on a narrowed pod.

    Read out of charts/service/values.yaml as `egressLabelValue` — a document
    the chart's own templates render — so the per-component policy matches
    whatever the chart stamps by construction. The previous derivation scraped
    the template's source with a regular expression, to which YAML quoting is
    invisible: an ordinary `orbit.local/egress: "declared"` made the generator
    match on a value carrying the quote characters, and the pod was then
    selected by no policy at all, which under the union rule is unrestricted
    in both directions. A pod carrying the key with any other value is in that
    same position, so that agreement has to be structural, not two literals
    that happen to agree. Every template in the chart that stamps the label
    must render this same declaration; two of them disagreeing is refused.
    Called only for namespaces that narrow something, so a catalog that
    narrows nothing never reads the chart."""
    chart = _service_chart_file("values.yaml").parent
    key = str(chart)
    cached = _EGRESS_VALUE_CACHE.get(key)
    if cached is not None:
        return cached
    declared = _chart_values().get("egressLabelValue")
    if not isinstance(declared, str) or not declared:
        sys.exit(f"service chart {chart / 'values.yaml'} does not declare "
                 f"egressLabelValue — the per-component egress policy matches "
                 f"the label value the chart stamps, so it is read from "
                 f"there, not a second copy here")
    by_value: dict[str, list[str]] = {}
    templates = chart / "templates"
    if templates.is_dir():
        for path in sorted(templates.iterdir()):
            if not path.is_file():
                continue
            for match in re.finditer(r"^\s*orbit\.local/egress:\s*(.+?)\s*$",
                                     path.read_text(), re.M):
                raw = match.group(1).strip()
                if "egressLabelValue" in raw:
                    effective = declared
                else:
                    effective = raw.strip("\"'")
                by_value.setdefault(effective, []).append(path.name)
    if len(by_value) != 1:
        detail = "; ".join(f"{_r(value)} in {sorted(files)}"
                           for value, files in sorted(by_value.items()))
        sys.exit(f"service chart templates stamp {len(by_value)} distinct values "
                 f"for orbit.local/egress ({detail or 'none at all'}) — the "
                 f"per-component policy can match only one")
    value = next(iter(by_value))
    _EGRESS_VALUE_CACHE[key] = value
    return value


def egress_targets(inst: dict) -> dict:
    """The instance's org-specific egress alias table, or {} when undeclared."""
    raw = inst.get("egressTargets")
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        sys.exit("instance/config.yaml egressTargets must be a mapping of alias "
                 f"to a list of CIDRs, not {_r(raw)}")
    # The whole table is validated here, not only the aliases a component
    # happens to name: a bad range under an unused alias would otherwise sit
    # green until someone declares it, and the refusal would then land on
    # whoever declared the alias rather than on whoever wrote the value.
    # Ranges are checked before names, so a table holding both a bad range
    # and a shadowed name reports the bad range with the value named.
    for alias, ranges in raw.items():
        if not isinstance(ranges, list):
            continue
        for value in ranges:
            _check_egress_range("instance/config.yaml", alias, value)
    for alias in raw:
        if alias in EGRESS_BUILTIN_ALIASES:
            sys.exit(f"instance/config.yaml egressTargets[{_r(alias)}] shadows the "
                     f"built-in alias {_r(alias)} — the core renders the built-in and "
                     f"the declared range is silently ignored; rename the instance "
                     f"alias or drop it")
    return raw


def _is_mapped_range(net: ipaddress._BaseNetwork) -> bool:
    """Is any part of this IPv6 range IPv4-mapped?

    Either endpoint carrying a mapped address is one, and so is a range the
    whole mapped /96 sits inside — a /64 over it, say — whose own endpoints
    are native. The API server refuses every one of these, while a strict
    parse accepts them, which is the shape that walked through this check
    twice under two different spellings.
    """
    if net[0].ipv4_mapped is not None or net[-1].ipv4_mapped is not None:
        return True
    return EGRESS_MAPPED96.subnet_of(net)


def _covers_link_local(net: ipaddress._BaseNetwork) -> bool:
    """Does this range reach any address the node's metadata service answers on?

    Same-family overlap covers containing, equalling and sitting inside the
    block; the mapped spelling of the IPv4 block is unmapped back to IPv4 and
    compared there, so it cannot walk through a guard that only knows one
    family.
    """
    if net.version == 4:
        return net.overlaps(EGRESS_LINK_LOCAL)
    if net.overlaps(EGRESS_LINK_LOCAL6):
        return True
    first = net[0].ipv4_mapped
    last = net[-1].ipv4_mapped
    if first is not None and last is not None:
        return any(part.overlaps(EGRESS_LINK_LOCAL)
                   for part in ipaddress.summarize_address_range(first, last))
    return False


def _check_egress_range(where: str, alias: str, value) -> None:
    """Refuse an egress target outside the accepted set, naming the value.

    The accepted set is an allowlist — an IPv4 network, or a native IPv6
    network, canonical, with an explicit prefix length — and everything else
    is refused by not being in it. The thing that decides is the API server's
    `ipBlock.cidr` field, not Python's address library: the two agree on the
    common cases and differ on exactly the shapes an operator writes once a
    year, so asking the library "do you accept this" admits values the
    cluster refuses and silently rewrites others. A shape nobody has thought
    of is then refused by default rather than admitted by default.

    A valid CIDR is still refused for what it reaches: anything covering the
    node's link-local range serves the node's root password from the metadata
    service — in any spelling, including the IPv4-mapped IPv6 one — and
    anything wider than a single provider holds narrows nothing. Every
    refusal names the value and exits cleanly: an unhandled traceback is also
    non-zero and also prints the value, and an operator cannot tell a
    rejected value from a broken generator.
    """
    text = str(value)
    try:
        net = ipaddress.ip_network(text, strict=True)
    except ValueError:
        sys.exit(f"{where}: egressTargets[{_r(alias)}] target {_r(value, 'cidr')} is not a "
                 f"usable CIDR — a NetworkPolicy cannot express a hostname, and "
                 f"the API server refuses a range with host bits set; write the "
                 f"network address with an explicit prefix length")
    if "/" not in text:
        sys.exit(f"{where}: egressTargets[{_r(alias)}] target {_r(value, 'cidr')} is a bare "
                 f"address, not a CIDR — write the prefix length explicitly "
                 f"(e.g. {text}/32); the API server's cidr field refuses an "
                 f"address without one")
    prefix_text = text.rsplit("/", 1)[1]
    if len(prefix_text) > 1 and prefix_text.startswith("0") and prefix_text.isdigit():
        sys.exit(f"{where}: egressTargets[{_r(alias)}] target {_r(value, 'cidr')} has a "
                 f"leading zero in the prefix length — the API server refuses "
                 f"it outright; write {_r(str(net))} instead")
    if str(net) != text:
        sys.exit(f"{where}: egressTargets[{_r(alias)}] target {_r(value, 'cidr')} is not "
                 f"canonical — the cluster reads it as {_r(str(net))}, so the "
                 f"render would grant something other than what was written; "
                 f"write {_r(str(net))} instead")
    # Breadth is a bound on address space, not a prefix-length special case:
    # no target may be wider than a /8 in IPv4 or a /32 in IPv6. Those are
    # the largest blocks any single provider is allocated — a regional
    # registry hands out IPv6 at /32 and no IPv4 holder has more than a /8 —
    # so a real "the model provider's network" fits underneath and "the
    # internet" does not. The refusal says how much the range would grant so
    # the operator can decide what to write instead.
    if (net.version == 4 and net.prefixlen < 8) or (
            net.version == 6 and net.prefixlen < 32):
        sys.exit(f"{where}: egressTargets[{_r(alias)}] target {_r(value, 'cidr')} grants "
                 f"{net.num_addresses:,} addresses — wider than any single "
                 f"provider holds; narrow it to the provider's published range")
    if _covers_link_local(net):
        sys.exit(f"{where}: egressTargets[{_r(alias)}] target {_r(value, 'cidr')} covers the "
                 f"node's link-local range (169.254.0.0/16 or fe80::/10), which "
                 f"serves the node's root password from the metadata service — "
                 f"refused")
    if net.version == 6 and _is_mapped_range(net):
        if net[0].ipv4_mapped is not None or net[-1].ipv4_mapped is not None:
            sys.exit(f"{where}: egressTargets[{_r(alias)}] target {_r(value, 'cidr')} is an "
                     f"IPv4-mapped IPv6 range, which the API server refuses "
                     f"outright — write the IPv4 range it maps instead")
        # A native supernet that merely COVERS the mapped block: refusing it
        # is right and calling it a mapped range is wrong — it is native, and
        # there is no IPv4 range it maps to.
        sys.exit(f"{where}: egressTargets[{_r(alias)}] target {_r(value, 'cidr')} covers "
                 f"the IPv4-mapped block (::ffff:0.0.0.0/96), which the API "
                 f"server refuses inside a range — narrow it to exclude that "
                 f"block")


def check_component_egress(pid: str, env: str, comp: dict, targets: dict,
                         has_database: bool = False) -> list[str] | None:
    """Validate a component's `egress:` block; return its CIDR aliases, or None.

    An alias nobody declared is refused, never rendered empty: an empty egress
    rule set reads as deny-everything, so the component would stop working on the
    next apply, in production, with nothing said.
    """
    egress = comp.get("egress")
    if egress is None:
        return None
    name = comp.get("name", "")
    where = f"{_r(pid)}/{_r(env)} component {_r(name)}"
    if not isinstance(egress, dict):
        sys.exit(f"{where} declares egress as {_r(egress)} — it must be a mapping, "
                 f"e.g. egress: {{cidrs: [postgres, dns]}}")
    if "hosts" in egress:
        sys.exit(f"{where} declares egress.hosts, which needs the shared egress proxy "
                 f"that does not exist yet — declare only egress.cidrs in this phase")
    unknown = sorted(set(egress) - EGRESS_KNOWN_KEYS, key=str)
    if unknown:
        sys.exit(f"{where} declares unknown egress key {_r(unknown[0])} "
                 f"(known: {sorted(EGRESS_KNOWN_KEYS)})")
    cidrs = egress.get("cidrs")
    if (not isinstance(cidrs, list) or not cidrs
            or not all(isinstance(a, str) and a for a in cidrs)):
        sys.exit(f"{where} declares egress.cidrs as {_r(cidrs)} — it must be a non-empty "
                 f"list of alias names; an empty grant reads as deny-everything on apply")
    accepted = list(EGRESS_BUILTIN_ALIASES) + sorted(targets)
    aliases: list[str] = []
    for alias in cidrs:
        if alias in aliases:
            continue
        aliases.append(alias)
        if alias in EGRESS_BUILTIN_ALIASES:
            # `postgres` renders a selector for this tenant's database Cluster,
            # which exists only when the project declares a database. Without
            # one the component leaves the open grant for a rule pointing at a
            # Cluster that is not there — reaching nothing at all, not even
            # DNS, which is exactly the deny-everything outcome this refusal
            # exists to prevent.
            if alias == "postgres" and not has_database:
                sys.exit(f"{where} declares egress alias 'postgres', but project "
                         f"{_r(pid)} declares no database — the alias selects the "
                         f"Cluster the chart would create ({pid}-db), which is "
                         f"not there, so the component would reach nothing at "
                         f"all, not even DNS; narrow on the other aliases instead")
            continue
        if alias not in targets:
            sys.exit(f"{where} declares unknown egress alias {_r(alias)} — accepted: "
                     f"{', '.join(accepted)}")
        ranges = targets[alias]
        if not isinstance(ranges, list) or not ranges:
            sys.exit(f"{where}: instance egressTargets[{_r(alias)}] declares no address "
                     f"ranges — an alias with no ranges renders an empty grant, which "
                     f"reads as deny-everything on apply")
        for value in ranges:
            _check_egress_range(where, alias, value)
    return aliases


def _dns_egress_rule() -> dict:
    """Egress to the cluster resolver: without it a component narrowed to one
    external range cannot resolve the hostname that range exists to reach."""
    return {
        "to": [{"namespaceSelector": {"matchLabels": {
            "kubernetes.io/metadata.name": "kube-system"}}}],
        "ports": [{"protocol": "UDP", "port": 53},
                  {"protocol": "TCP", "port": 53}],
    }


def component_egress_rules(pid: str, aliases: list[str], targets: dict) -> list[dict]:
    """The NetworkPolicy egress rules for one narrowed component, in declared order.

    Narrowing restricts what a component reaches OUTSIDE the cluster; the
    platform's own paths stay without being declared, as selectors rather
    than address blocks, so the only ipBlock ranges in the policy are the
    ones the component declared.
    """
    rules = []
    for alias in aliases:
        if alias == "dns":
            rules.append(_dns_egress_rule())
        elif alias == "postgres":
            # This tenant's own database pods, in this namespace — a pod selector,
            # never an address range. The Cluster is <project>-db (database.yaml).
            rules.append({
                "to": [{"podSelector": {"matchLabels": {
                    "cnpg.io/cluster": f"{pid}-db"}}}],
                "ports": [{"protocol": "TCP", "port": 5432}],
            })
        else:
            # Rendered canonical: validation above admits only canonical networks,
            # so this cannot fail and cannot rewrite, and what reaches
            # `ipBlock.cidr` is then exactly what the operator wrote rather
            # than the author's spelling normalised.
            rules.append({"to": [{"ipBlock": {"cidr": str(ipaddress.ip_network(
                str(value), strict=True))}} for value in targets[alias]]})
    # The platform's own paths, kept without being declared. Declaring an
    # address range and losing name resolution is not a configuration an
    # operator would choose on purpose, so the resolver stays even when the
    # `dns` alias was not named.
    if "dns" not in aliases:
        rules.append(_dns_egress_rule())
    # Its siblings: an empty podSelector selects every pod in the policy's
    # own namespace, which no alias can spell — the first customer's shape is
    # a web component calling a worker beside it.
    rules.append({"to": [{"podSelector": {}}]})
    # Its traces: the platform points a component's tracing at the collector
    # in the tempo namespace (OTLP_ENDPOINT) without the component asking, so
    # narrowing must not silently switch that off. Kept for every narrowed
    # component, the way the resolver is.
    rules.append({
        "to": [{"namespaceSelector": {"matchLabels": {
            "kubernetes.io/metadata.name": "tempo"}}}],
        "ports": [{"protocol": "TCP", "port": 4318}],
    })
    return rules


# Chart defaults are read, never copied: a second 256Mi in this file would drift the
# first time values.yaml changes and the quota would admit (or refuse) the wrong pod.
# Cached per path so a test that rebinds ROOT reloads, and a production run does not.
_CHART_VALUES_CACHE: dict[str, dict] = {}
_DB_CHART_DEFAULTS_CACHE: dict[str, dict] = {}
# The tree this file shipped in. Distinct from ROOT, which a test may rebind to a
# sandbox that does not vendor the chart; the instance repo always does.
_GENERATOR_ROOT = pathlib.Path(__file__).resolve().parents[1]

# The Namespace document that names orbitd's namespace. The generator reads
# metadata.name from this file so the NetworkPolicy admit-list cannot drift
# from the manifest that creates the namespace. Instance copy first, then the
# staged source (core template, or provisioning/ after init).
_ORBITD_NAMESPACE_RELS = (
    pathlib.Path("clusters") / "platform" / "config" / "orbitd" / "namespace.yaml",
    pathlib.Path("provisioning") / "clusters" / "platform" / "config" / "orbitd" / "namespace.yaml",
    pathlib.Path("template") / "provisioning" / "clusters" / "platform" / "config"
    / "orbitd" / "namespace.yaml",
)
_ORBITD_NAMESPACE_CACHE: dict[str, str] = {}


def _orbitd_namespace_file() -> pathlib.Path:
    """Path of the orbitd Namespace document (instance copy or staged source)."""
    tried = []
    for base in (ROOT, _GENERATOR_ROOT):
        for rel in _ORBITD_NAMESPACE_RELS:
            path = base / rel
            tried.append(str(path))
            if path.is_file():
                return path
    sys.exit(
        "orbitd Namespace document not found (looked at " + ", ".join(tried)
        + ") — the non-prod web-check admit-list takes the namespace name from "
        "that document, not a second copy")


def _orbitd_namespace() -> str:
    """metadata.name of the orbitd Namespace. One name, read from its manifest."""
    path = _orbitd_namespace_file()
    key = str(path)
    cached = _ORBITD_NAMESPACE_CACHE.get(key)
    if cached is not None:
        return cached
    name = None
    for doc in yaml.safe_load_all(path.read_text()):
        if isinstance(doc, dict) and doc.get("kind") == "Namespace":
            name = (doc.get("metadata") or {}).get("name")
            if name:
                break
    if not name:
        sys.exit(f"orbitd Namespace document at {path} has no metadata.name")
    name = str(name)
    _ORBITD_NAMESPACE_CACHE[key] = name
    return name


def _web_ports(components: list[dict]) -> list[int]:
    """Declared ports of kind: web components, as the chart's Service publishes them."""
    ports: list[int] = []
    seen: set[int] = set()
    for c in components:
        if (c.get("kind") or "web") != "web":
            continue
        raw = c.get("port")
        if raw is None or raw == "":
            continue
        try:
            port = int(raw)
        except (TypeError, ValueError):
            continue
        if not port or port in seen:
            continue
        seen.add(port)
        ports.append(port)
    ports.sort()
    return ports


def _service_chart_file(rel: str) -> pathlib.Path:
    """A file under charts/service.

    The repository the generator is running for (ROOT) is tried first — an instance
    always vendors the chart there. If that path is absent, fall back to the chart
    shipped next to this generator before failing.
    """
    primary = ROOT / "charts" / "service" / rel
    if primary.is_file():
        return primary
    shipped = _GENERATOR_ROOT / "charts" / "service" / rel
    if shipped.is_file():
        return shipped
    return primary


def _chart_schema() -> dict:
    """charts/service/values.schema.json, whole: the shape check is the chart's own
    schema, not a second copy in the generator."""
    path = _service_chart_file("values.schema.json")
    if not path.is_file():
        sys.exit(f"service chart schema not found at {path} — the component shape check "
                 f"is the chart's own schema, not a second copy in the generator")
    schema = json.loads(path.read_text())
    if not isinstance(schema, dict) or not isinstance(schema.get("properties"), dict):
        sys.exit(f"service chart schema at {path} declares no properties")
    return schema


def _chart_component_schema() -> dict:
    """The `components[]` item properties of the chart schema."""
    schema = _chart_schema()
    path = _service_chart_file("values.schema.json")
    props = (((schema.get("properties") or {}).get("components") or {}).get("items") or {}).get("properties")
    if not isinstance(props, dict) or not props:
        sys.exit(f"service chart schema at {path} declares no components[] properties")
    return props


def _chart_values() -> dict:
    """charts/service/values.yaml — defaultMemory, defaultClass, resourceClasses."""
    path = _service_chart_file("values.yaml")
    key = str(path)
    cached = _CHART_VALUES_CACHE.get(key)
    if cached is None:
        if not path.is_file():
            sys.exit(f"service chart values not found at {path} — namespace quota is "
                     f"derived from the chart's defaults, not a second copy in the "
                     f"generator")
        cached = yaml.safe_load(path.read_text()) or {}
        _CHART_VALUES_CACHE[key] = cached
    return cached


def _database_chart_defaults() -> dict:
    """The Cluster's cpu/memory/instances defaults, as the database template writes them.

    They live in the Helm template (not values.yaml: `database: {}` there), so the
    quota uses the same numbers the operator will request. Parsing the template is
    the alternative to duplicating 256Mi/100m/1 here.
    """
    path = _service_chart_file("templates/database.yaml")
    key = str(path)
    cached = _DB_CHART_DEFAULTS_CACHE.get(key)
    if cached is None:
        if not path.is_file():
            sys.exit(f"service chart database template not found at {path} — quota "
                     f"derivation reads the Cluster's default requests from it")
        text = path.read_text()
        cpu = re.search(r"cpu:\s*\{\{\s*\.cpu\s*\|\s*default\s*\"([^\"]+)\"\s*\}\}", text)
        mem = re.search(r"memory:\s*\{\{\s*\.memory\s*\|\s*default\s*\"([^\"]+)\"\s*\}\}",
                        text)
        inst = re.search(r"(?m)^\s*instances:\s*(\d+)\s*$", text)
        if not (cpu and mem and inst):
            sys.exit("internal: could not read database cpu/memory/instances defaults "
                     "from charts/service/templates/database.yaml")
        cached = {"cpu": cpu.group(1), "memory": mem.group(1),
                  "instances": int(inst.group(1))}
        _DB_CHART_DEFAULTS_CACHE[key] = cached
    return cached


def _cpu_millicores(q: str) -> float:
    """A Kubernetes CPU quantity as millicores. '250m' → 250, '1' → 1000, '0.5' → 500."""
    s = str(q)
    if not CPU_RE.fullmatch(s):
        sys.exit(f"internal: {_r(q)} reached _cpu_millicores unvalidated")
    if s.endswith("m"):
        return float(s[:-1])
    return float(s) * 1000.0


def _format_mi(nbytes: float) -> str:
    """Render a byte count as a whole-Mi quantity, rounding up so the quota is never short."""
    mi = nbytes / (1024 ** 2)
    whole = int(math.ceil(mi - 1e-9))
    return f"{whole}Mi"


def _ceil_cpu_to_half_cores_millis(millis: float) -> int:
    """Round millicores up to the next 0.5 CPU (500m). 250 → 500, 500 → 500, 501 → 1000."""
    step = 500.0
    if millis <= 0:
        return 0
    return int(math.ceil(millis / step) * step)


def _component_resources(comp: dict) -> tuple[int, float, float, float]:
    """(replicas, request bytes, limit bytes, cpu millicores) for one catalog component.

    Missing memory/class/cpu fall through to the chart, matching templates/_helpers.tpl
    `service.resources`. Cron has no replica count in the chart; one running Job is the
    occupancy the quota budgets for. Singleton forces replicas=1 the way the chart does.
    """
    chart = _chart_values()
    replicas = 1 if comp.get("singleton") else int(comp.get("replicas") or 1)
    klass = comp.get("class") or chart["defaultClass"]
    classes = chart.get("resourceClasses") or {}
    if klass not in classes:
        sys.exit(f"component {_r(comp.get('name'))}: class {_r(klass)} is not in the chart's "
                 f"resourceClasses")
    cpu = str(comp["cpu"] if comp.get("cpu") not in (None, "") else classes[klass]["cpu"])
    mem = str(comp["memory"] if comp.get("memory") not in (None, "")
              else chart["defaultMemory"])
    lim = str(comp["memoryLimit"] if comp.get("memoryLimit") not in (None, "") else mem)
    if not QUANTITY_RE.fullmatch(mem):
        sys.exit(f"component {_r(comp.get('name'))}: memory {_r(mem)} is not a Kubernetes "
                 f"quantity")
    if not QUANTITY_RE.fullmatch(lim):
        sys.exit(f"component {_r(comp.get('name'))}: memoryLimit {_r(lim)} is not a "
                 f"Kubernetes quantity")
    if not CPU_RE.fullmatch(cpu):
        sys.exit(f"component {_r(comp.get('name'))}: cpu {_r(cpu)} is not a Kubernetes CPU "
                 f"quantity")
    return replicas, _quantity_bytes(mem), _quantity_bytes(lim), _cpu_millicores(cpu)


def _instrumented(comp: dict) -> bool:
    """True when the service chart will stamp an inject annotation on this pod.

    charts/service/templates/workloads.yaml emits
    instrumentation.opentelemetry.io/inject-<language>: "true" only when
    `language:` is set. Agent-plane components omit it on purpose (they emit
    their own OTLP) and are not charged the init container.
    """
    return bool(comp.get("language"))


def _instrumentation_resources() -> dict:
    """requests/limits written onto every Instrumentation language block."""
    return {
        "limits": {
            "cpu": INSTRUMENTATION_INIT["cpuLimit"],
            "memory": INSTRUMENTATION_INIT["memoryLimit"],
        },
        "requests": {
            "cpu": INSTRUMENTATION_INIT["cpu"],
            "memory": INSTRUMENTATION_INIT["memory"],
        },
    }


def _instrumentation_lang(lang: str, env: list, init_resources: dict) -> dict:
    """One Instrumentation spec.<language> block with the CRD's field for lang."""
    return {"env": env, INSTRUMENTATION_RESOURCE_FIELD[lang]: init_resources}


def _pod_quota(comp: dict, req: float, lim: float, cpu: float) -> tuple[float, float, float]:
    """Per-pod quota after the instrumentation init container, if any.

    Kubernetes charges max(Σ app containers, the init container) for each of
    requests.memory, limits.memory, requests.cpu. Uninstrumented pods keep the
    app container's numbers.
    """
    if not _instrumented(comp):
        return req, lim, cpu
    return (
        max(req, _quantity_bytes(INSTRUMENTATION_INIT["memory"])),
        max(lim, _quantity_bytes(INSTRUMENTATION_INIT["memoryLimit"])),
        max(cpu, _cpu_millicores(INSTRUMENTATION_INIT["cpu"])),
    )


def _wal_sidecar_resources() -> tuple[float, float, float]:
    """The barman-cloud sidecar a WAL-archiving database pod runs beside Postgres, as the
    chart declares it (charts/service/templates/database.yaml instanceSidecarConfiguration):
    (request bytes, limit bytes, cpu millicores). Read from the chart so the quota cannot
    drift from what the pod asks for."""
    text = _service_chart_file("templates/database.yaml").read_text(encoding="utf-8")
    m = re.search(r"instanceSidecarConfiguration:\s*\n\s*resources:\s*\n\s*requests:\s*\n"
                  r"\s*cpu:\s*(\S+)\s*\n\s*memory:\s*(\S+)\s*\n\s*limits:\s*\n\s*memory:\s*(\S+)", text)
    if not m:
        sys.exit("charts/service/templates/database.yaml: the WAL sidecar's resources are not where "
                 "the quota derivation reads them (instanceSidecarConfiguration.resources)")
    cpu, req, lim = m.group(1), m.group(2), m.group(3)
    return _quantity_bytes(req), _quantity_bytes(lim), _cpu_millicores(cpu)


def _db_backup_resources(database: dict) -> tuple[float, float, float]:
    """The chart's pg_dump Job for a database with no bucket (charts/service/templates/
    database.yaml: a `batch` cron component named db-backup, memory `backup.memory` or
    the template's default): (request bytes, limit bytes, cpu millicores). The default
    is read from the template so the quota cannot drift from what the Job asks for."""
    text = _service_chart_file("templates/database.yaml").read_text(encoding="utf-8")
    m = re.search(r'"memory" \(\$backup\.memory \| default "([^"]+)"\)', text)
    if not m:
        sys.exit("charts/service/templates/database.yaml: the db-backup Job's default memory is not "
                 "where the quota derivation reads it (\"memory\" ($backup.memory | default ...))")
    backup = database.get("backup") if isinstance(database.get("backup"), dict) else {}
    mem = str(backup.get("memory") or m.group(1))
    _, req, lim, cpu = _component_resources({"name": "db-backup", "kind": "cron", "class": "batch",
                                             "memory": mem})
    return req, lim, cpu


def derived_quota(components: list[dict], database: dict | None, wal: bool = False) -> dict:
    """ResourceQuota hard map for one project environment.

    Per instrumented pod (catalog `language:` set — the same predicate the
    chart uses for the inject annotation), each of request/limit/cpu is
    max'd with INSTRUMENTATION_INIT, because Kubernetes charges
    max(app container, the injected init container). Uninstrumented pods
    (no `language:`, including the agent plane) keep the app numbers.
    The database is not an instrumented component and is not floored.

    requests.memory = Σ (replicas × per-pod request) + largest non-singleton
                      web per-pod request + the database request if declared.
    limits.memory   = the same sum over per-pod limits (memoryLimit, else the
                      request, then the INIT floor if instrumented).
    requests.cpu    = Σ (replicas × per-pod cpu) + that surge pod's cpu + the
                      database cpu, rounded up to the next 0.5 core. Database
                      cpu is in the sum so the quota that *names* requests.cpu
                      can still admit the Cluster (charts/service/templates/
                      database.yaml: a named quota makes the request compulsory,
                      and a Cluster that does not fit sits at zero instances).
    pods            = Σ replicas + 1 surge + database instances + 2 (jobs/probes),
                      minimum 6.
    persistentvolumeclaims is the existing constant 4.

    Quantities render as Mi / m strings. The surge term is the largest `kind: web`
    component that actually rolls (maxSurge: 1), compared after the INIT floor;
    a singleton Recreate web does not surge and does not contribute.
    """
    req_mem = lim_mem = cpu_m = 0.0
    replicas_sum = 0
    largest_web: tuple[float, float, float] | None = None
    for c in components:
        n, req, lim, cpu = _component_resources(c)
        req, lim, cpu = _pod_quota(c, req, lim, cpu)
        replicas_sum += n
        req_mem += n * req
        lim_mem += n * lim
        cpu_m += n * cpu
        kind = c.get("kind") or "web"
        if kind == "web" and not c.get("singleton"):
            if largest_web is None or req > largest_web[0]:
                largest_web = (req, lim, cpu)
    if largest_web:
        req_mem += largest_web[0]
        lim_mem += largest_web[1]
        cpu_m += largest_web[2]
    db_instances = 0
    if database:
        defaults = _database_chart_defaults()
        db_mem = str(database["memory"] if database.get("memory") not in (None, "")
                     else defaults["memory"])
        db_cpu = str(database["cpu"] if database.get("cpu") not in (None, "")
                     else defaults["cpu"])
        if not QUANTITY_RE.fullmatch(db_mem):
            sys.exit(f"database memory {_r(db_mem)} is not a Kubernetes quantity")
        if not CPU_RE.fullmatch(db_cpu):
            sys.exit(f"database cpu {_r(db_cpu)} is not a Kubernetes CPU quantity")
        db_instances = int(database["instances"] if database.get("instances") not in
                           (None, "") else defaults["instances"])
        req_mem += _quantity_bytes(db_mem)
        lim_mem += _quantity_bytes(db_mem)
        cpu_m += _cpu_millicores(db_cpu)
        if wal:
            # A WAL-archiving database pod also runs the barman-cloud sidecar. Left out,
            # the quota had no room for the rolling-update surge pod once the database
            # was up: checkers-lab-prod's first ship sat at "exceeded quota" until the
            # rollout timed out (docs/V1.md §8).
            s_req, s_lim, s_cpu = _wal_sidecar_resources()
            req_mem += db_instances * s_req
            lim_mem += db_instances * s_lim
            cpu_m += db_instances * s_cpu
        else:
            # Without a bucket the chart's pg_dump runs beside the database: once per
            # deploy (waiting for the server to come up) and nightly. Left out, a new
            # project's per-deploy dump held the room the initdb pod needed, and the
            # database never bootstrapped (s17-scratch-dev, docs/V1.md §8).
            b_req, b_lim, b_cpu = _db_backup_resources(database)
            req_mem += b_req
            lim_mem += b_lim
            cpu_m += b_cpu
    pods = max(6, replicas_sum + 1 + db_instances + 2)
    return {
        "requests.cpu": f"{_ceil_cpu_to_half_cores_millis(cpu_m)}m",
        "requests.memory": _format_mi(req_mem),
        "limits.memory": _format_mi(lim_mem),
        "pods": str(pods),
        "persistentvolumeclaims": "4",
    }


# A CLOSED key set. An unknown key in an agent entry is either a typo that renders
# nothing (and reports nothing), or — the reason this is closed rather than ignored — a
# VALUE someone tried to pass inline: `token: hunter2` in a catalog entry is a credential
# committed in plaintext, and the generated Application would carry it into git.
#
# `image`/`tag` are NOT here, deliberately (dropped 2026-08-28 after review): a per-agent
# image let a catalog entry choose the registry that RECEIVES the mounted subscription
# token — one `image: evil.net/x` + egress to the internet and a single task exfiltrates
# the org credential. The runtime image is an INSTANCE-level pin (agentRuntime in
# instance/config.yaml), one build for the whole plane, never a per-agent choice.
#
# CRON_KEYS (Phase D.3) join the set but only mean something on a `mode: cronjob` agent;
# on a deployment agent they are refused (below), because a schedule on a Deployment is a
# silent no-op that never fires.
CRON_KEYS = {"schedule", "concurrencyPolicy", "startingDeadlineSeconds",
             "backoffLimit", "successfulJobsHistoryLimit", "failedJobsHistoryLimit"}
AGENT_KEYS = {"name", "prompt", "model", "mode", "skills",
              "class", "memory", "memoryLimit", "cpu", "maxTurns", "timeoutSeconds",
              "maxConcurrency",
              "uid", "enabled",
              # Phase D.3 (intake). `triggers` is what makes an agent event-driven;
              # `dispatch` is how the intake wakes it. Both are closed-schema below.
              "triggers", "dispatch"} | CRON_KEYS
AGENT_CLASSES = ("interactive", "batch", "background")
# A cron schedule — five whitespace-separated fields or an @-macro. The SAME pattern the
# chart's values.schema.json already enforces on `schedule`, applied HERE too so a bad
# schedule ('every tuesday', four fields, an injected newline) fails in the generator —
# where every other agent value is validated — rather than rendering a CronJob the API
# server rejects at apply time on an otherwise-green commit. fullmatch anchors both ends,
# so a trailing newline that Python's `$` would tolerate cannot smuggle a second line in.
CRON_RE = re.compile(r"@(hourly|daily|weekly|monthly|yearly)"
                     r"|[-0-9*/,]+( +[-0-9*/,A-Za-z]+){4}")
# concurrencyPolicy is Forbid or Replace, never Allow: each concurrent run holds the org
# subscription token and spends against it, so the plane caps a scheduled agent to one
# running Job (Forbid skips a new run while the old one is alive, Replace supersedes it).
# 'Allow' — unbounded pile-up of token-holding pods — is refused.
CRON_CONCURRENCY = ("Forbid", "Replace")
# A scheduled run's Job-level wall-clock backstop = the agent's own task timeout plus a
# grace for image pull and CLI spawn. It sits UNDER app.py's in-process
# ORBIT_AGENT_TIMEOUT_S: if the process wedges before or after the model call, the Job is
# still killed rather than pinning a slot forever.
ACTIVE_DEADLINE_GRACE_S = 120
# An env var, not a file: a prompt long enough to strain that belongs in a skill, which
# is mounted and versioned rather than pasted into a Deployment.
MAX_PROMPT = 8192

# ------------------------------------------------------------ the intake (Phase D.3)
# "The intake service is the event router, and that is its whole job: authenticate the
# source -> match the event against the catalog's trigger declarations -> shape the
# payload into a task -> launch a Job, or call the warm agent if one exists."
# (AGENT-PLANE.md, "Triggers are first class", settled 2026-08-29.)
#
# The intake renders as an ORDINARY component of the same synthetic plane project, so it
# inherits the plane's namespace, quota, pull secret, NetworkPolicy and OTel identity — and
# it is a NEIGHBOUR of the agents it wakes, which is the whole reason the plane is one
# namespace whose policy admits only its own pods.
INTAKE_COMPONENT = "intake"       # also a RESERVED agent name: both render orbit-agents-intake
INTAKE_PORT = 8080
INTAKE_HEALTH = "/healthz"
INTAKE_UID = 10001                # services/intake/Dockerfile's own USER
# Measured, not guessed (2026-08-31, six incarnations over a 48h window on the reference
# instance): resident set FLAT at 18.1-18.2Mi in every one of them, working set 19.0-26.9Mi,
# page cache under 9Mi. The router holds a routing table and an in-memory dedup set; it
# streams no bodies and keeps no per-request state, so its footprint does not track load.
# Phase RS's rule — peak + ~30%, rounded up the 32/64/96/128... ladder — gives 64Mi from a
# 26.9Mi peak. The former 256Mi was a first-cut guess that reserved 13x the steady state
# forever, and it is a SINGLE number for the whole plane, so the waste scaled with nothing.
# Request == limit here deliberately: unlike an agent, this container has no burst to
# protect, so there is nothing for a split to express.
INTAKE_MEMORY = "64Mi"
INTAKE_ROUTES_DIR = "/etc/orbit/intake"     # services/intake/Dockerfile INTAKE_ROUTES_FILE
INTAKE_ROUTES_KEY = "routes.json"
# The three per-source credentials, one Kubernetes Secret for the router. Pointers like
# every other credential here: git holds the key name, the sops-sealed Secret holds
# the value.
INTAKE_SECRET = "orbit-intake"
INTAKE_SECRET_KEYS = ["INTAKE_A2A_TOKEN", "INTAKE_ALERTMANAGER_TOKEN",
                      "INTAKE_GITHUB_SECRET"]
# A one-shot Job created per event is deliberately NOT retried (backoffLimit 0): a retry
# re-runs a task that may already have had effects and re-spends the subscription, and the
# event sources retry at their own layer (Alertmanager repeats the group, GitHub
# redelivers). The Job object lingers for this long so its logs are readable and so its
# NAME — derived from the event's dedup key — keeps suppressing duplicates across an
# intake restart, which the in-memory dedup set cannot.
INTAKE_JOB_BACKOFF = 0
INTAKE_JOB_TTL_S = 3600
# The router's read timeout for a dispatch (INTAKE_DISPATCH_TIMEOUT_S). A WARM agent runs
# the task SYNCHRONOUSLY, so this MUST be >= the agent's own ORBIT_AGENT_TIMEOUT_S or every
# real warm run times out, is billed failed, AND releases its dedup claim — and the source
# re-sends and double-spends. Rendered as max(agent timeout) + a grace, so it is >= every
# routed agent's timeout; the app.py default is the agent default (300) + this same grace,
# so a hand-run image can never have intake < agent either.
INTAKE_DISPATCH_GRACE_S = 60
# Where the router exposes its dispatch-outcome counter for Prometheus (NEW-3). Same port
# as the app: a k8s-down/agent-down event is lost to HTTP-level metrics (the reply was 202)
# and, for GitHub, lost for good — so the outcome counter is what makes a lost event visible.
INTAKE_METRICS_PATH = "/metrics"
# A closed trigger schema, per type, for exactly the same reason AGENT_KEYS is closed: an
# unknown key here either routes nothing (and reports nothing) or is a value someone tried
# to smuggle into a manifest that is committed in plaintext.
#
# `metric` and `logs` are the two types the generator must ALSO emit a Prometheus/Loki
# RULE for, not just an intake route — a route with no rule behind it is a trigger that
# can never fire (that was why they were deferred). They now render both halves from the
# one catalog block: the alerting rule whose alertname is derived deterministically, plus
# the SAME intake route the `alert` type emits, keyed on that alertname. `cron` is still
# refused by name, because the cron trigger's implementation IS the existing `mode:
# cronjob` rendering (AGENT-PLANE.md) and two spellings of one thing is how a schedule
# ends up declared twice and fired twice.
#
# `name` on a metric/logs trigger is REQUIRED: it is the human-facing identifier and the
# tail of the derived alertname, so two triggers on one agent stay distinct rather than
# rendering the same alert twice. `expr` is the PromQL (metric) / LogQL (logs) condition;
# `for` is how long it must hold; `summary` is the human line carried as the alert's
# annotation, and optional `description` is the second line the Telegram receiver already
# prints (`{{ .Annotations.description }}` — blank on every generated alert until this
# key existed). `severity`/`labels` are optional and become rule labels — `alertname` and
# the reserved provenance labels below are set BY the generator, so a catalog label may
# not shadow them (it would either be a no-op or fight the router's own matcher).
_METRICLOGS_KEYS = {"type", "name", "expr", "for", "summary", "description",
                    "severity", "labels"}
TRIGGER_KEYS = {
    "alert": {"type", "match"},
    "github": {"type", "repo", "events"},
    "a2a": {"type"},
    "metric": set(_METRICLOGS_KEYS),
    "logs": set(_METRICLOGS_KEYS),
}
TRIGGER_DEFERRED = {
    "cron": "the cron trigger is `mode: cronjob` + `schedule:` on this same entry — the "
            "existing CronJob rendering IS its implementation (AGENT-PLANE.md)",
}
MAX_TRIGGERS = 16
# A metric/logs trigger name: same shape as an agent name (a DNS-label fragment), so the
# PascalCase alertname derived from it is a valid Prometheus/Loki alert identifier.
TRIGGER_NAME_RE = re.compile(r"[a-z0-9]([a-z0-9-]{0,40}[a-z0-9])?")
# A Prometheus/Loki duration, VERBATIM from the PrometheusRule CRD's own `for` pattern
# (monitoring.coreos.com/v1, spec.groups[].rules[].for): ORDERED, each unit at most once.
# The looser `([0-9]+(ms|[smhdwy]))+` this replaced accepted `30m1h` and `5m5m5m` — which
# render green, ship, and are then REJECTED by the CRD's admission webhook, so the Argo
# app sits SyncFailed with CI still green. The generator refuses what the cluster will.
PROM_DURATION_RE = re.compile(
    r"0|(([0-9]+)y)?(([0-9]+)w)?(([0-9]+)d)?(([0-9]+)h)?(([0-9]+)m)?(([0-9]+)s)?(([0-9]+)ms)?")
_DURATION_UNITS = {"ms": 0.001, "s": 1, "m": 60, "h": 3600,
                   "d": 86400, "w": 604800, "y": 31536000}
_DURATION_TERM_RE = re.compile(r"([0-9]+)(ms|[smhdwy])")
# The floor on `for:`. A rule that fires on a single scrape blip wakes an agent that spends
# the org's subscription, and the plane's only other brakes are coarse (AM group_interval /
# repeat_interval, the intake's dedup TTL, MAX_INFLIGHT) — see AGENT-PLANE.md, "Runaway
# stance". One minute is the cheapest guard that makes a flap cost one run, not fifty.
# `0s` is the documented explicit opt-out: it is spelled, so it is a decision, not a typo.
MIN_FOR_SECONDS = 60
FOR_IMMEDIATE = "0s"
TRIGGER_SEVERITIES = ("info", "warning", "critical")
# FOUND LIVE (2026-08-29): kube-prometheus-stack ships stock Alertmanager `inhibit_rules`
# (critical>warning|info, warning>info, InfoInhibitor>info) and they apply to the DERIVED
# OrbitAgent* space like any other alert. A metric trigger declared `severity: info` was
# route-matched to the intake and then held `state=suppressed` (inhibitedBy InfoInhibitor)
# — never delivered, never a 404, no failure counter, nothing. Machine work items are not
# human notification noise, so the fix is instance-side: exempt the derived space from the
# stock rules (template/provisioning/clusters/platform/apps/kube-prometheus-stack.yaml).
#
# The generator cannot ENFORCE that: it renders clusters/apps/ from the catalog and never
# reads the platform's Alertmanager config, and the one honest way to answer "does this
# config still inhibit the derived space" is to simulate its matchers — which is
# scripts/verify/am-routes.py, and that module imports THIS one (a dependency the other
# way round would be a cycle). Grepping the config for a matcher string would be exactly
# the "does the file contain a string" check that oracle exists to replace. So enforcement
# lives in the oracle, instance-armed like every other plane row, and the generator says
# so out loud on stderr at every `make sync`.
INHIBITED_SEVERITY = "info"


def _warn_info_severity(agent: str, kind: str, trigger) -> None:
    print(f"WARNING: agent {_r(agent)}: the {_r(kind)} trigger {_r(trigger)} is declared "
          f"severity: {_r(INHIBITED_SEVERITY)}. kube-prometheus-stack's STOCK inhibit_rules "
          f"suppress every info-severity alert behind InfoInhibitor — including this one, "
          f"which would be routed to the intake and then never delivered, silently. Verify "
          f"this instance's Alertmanager exempts the {DERIVED_ALERT_PREFIX}* space from "
          f"inhibition (scripts/verify/am-routes.py asserts it; the AGENT-PLANE.md record, "
          f"'Found live'), or use severity: warning.",
          file=sys.stderr)


MAX_EXPR = 2048
MAX_SUMMARY = 512
MAX_DESCRIPTION = 1024
# Labels the generator OWNS on a metric/logs rule: `severity` has its own catalog field,
# `alertname` is the router's whole matcher, and the two provenance labels below tie every
# generated alert back to the agent+trigger that declared it. A catalog `labels:` entry
# naming any of these is refused rather than silently overridden. Everything `__`-prefixed
# is refused too: that namespace is Prometheus's own (`__name__`, `__address__`,
# `__meta_*`), a rule label there is either dropped or rewrites what the sample IS.
RESERVED_RULE_LABELS = frozenset({"alertname", "severity", "orbit_agent", "orbit_trigger"})
# The subset of the above that ONLY a generated alert ever carries. A hand-written `alert`
# trigger matching one of these is not selecting a platform alert — there are none — it is
# subscribing to some metric/logs trigger's alert a second time.
PROVENANCE_RULE_LABELS = frozenset({"orbit_agent", "orbit_trigger"})
RESERVED_LABEL_PREFIX = "__"
# Prometheus expands Go templates in a rule's ANNOTATIONS and LABELS at alert time — and
# `{{ query "..." }}` inside one executes arbitrary PromQL against the server. A catalog is
# plaintext YAML edited by whoever can open a PR, and the summary is a human line, not a
# program: `{{`/`}}` anywhere in a catalog-supplied annotation or label value is refused.
TEMPLATE_MARKERS = ("{{", "}}")
# The derived-alertname prefix. Reserved: a hand-written `alert` trigger may not match on a
# name in this space (it would shadow, or double-dispatch alongside, a derived route), and
# the one Alertmanager route the plane needs is keyed on exactly this prefix.
DERIVED_ALERT_PREFIX = "OrbitAgent"
# Loki ruler alerting rules ship as a ConfigMap in the loki namespace. The instance mounts
# that ConfigMap straight into the ruler's local rule-store directory (/etc/loki/rules/fake);
# it is NOT picked up by the chart's rules sidecar — that sidecar was disabled by a security
# audit (it drags in a cluster-wide secret-read grant), so the `loki_rule` label below is
# vestigial (harmless — it was the sidecar's old discovery label). The ConfigMap alone does
# NOT make the rule live — see the `lokiRuler` gate below and AGENT-PLANE.md, "`logs` triggers".
LOKI_RULE_NAMESPACE = "loki"
LOKI_RULE_LABEL = "loki_rule"
# The instance declaration that a `logs` trigger may render at all. A `logs` rule is loaded
# and evaluated by the LOKI RULER, and a stock Loki ruler does not load it: `rulerConfig`
# ships no `alertmanager_url` (so a firing rule notifies nothing) and its rule storage is
# the S3 object store (so a rule dropped in the local rule-store dir is read by no one). Both
# are instance-side wiring — on the instance that shipped these, the ruler's local rule-store
# dir is the generated ConfigMap mounted in, storage is set to local, and a netpol admits
# loki->Alertmanager:9093. Without this flag a `logs` trigger would render a LIVE intake
# route in front of a rule that can never fire — the exact anti-pattern that deferred these
# types in the first place — so it is refused at generation instead.
LOKI_RULER_KEY = "lokiRuler"
LOKI_RULER_WIRED = "wired"
# The instance declaration that a `github` trigger may render at all. Same shape as the
# `lokiRuler` gate above and for the same reason: the generator renders the ROUTE, not the
# DOOR. A `github` trigger compiles into the intake's routes.json and nothing else — the
# generator deliberately emits NO Ingress and NO Certificate for the intake (a router that
# holds every warm agent's bearer and can create Jobs must never be published because a
# catalog line said so). So on a stock instance GitHub cannot reach /webhook/github at all:
# the route would be live, the operator would believe the agent is wired, and every
# delivery would fail at the edge with nothing on this side reporting why.
#
# The door is HAND-WRITTEN instance-side, and this flag is the instance asserting it exists
# and has the shape the intake's threat model needs:
#
#   (1) an Ingress on a host that reaches the intake Service, PATH-SCOPED to /webhook/github
#       alone — GET /healthz and /metrics are the intake's only unauthenticated routes and
#       must NOT be published; and
#   (2) a NetworkPolicy admitting the ingress controller's pods to the intake pods' port,
#       pod-narrowed on both sides (the plane's default-deny admits only its own pods).
#
# `wired` is the only accepted value: a typo must not read as wired and must not read as
# absent either (the lokiRuler lesson — a misspelt declaration sat green until the day it
# mattered), so the value is checked on EVERY generation, not only when a trigger exists.
INTAKE_INGRESS_KEY = "intakeIngress"
INTAKE_INGRESS_WIRED = "wired"
# A Prometheus/Alertmanager label name, and an exact-match value. Exact match only in this
# cut: a regex matcher in a generated config is a denial-of-service surface (catastrophic
# backtracking) evaluated on every alert, and nobody has asked for one.
LABEL_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_.]{0,62}")
GITHUB_REPO_RE = re.compile(r"[A-Za-z0-9._-]{1,100}/[A-Za-z0-9._-]{1,100}")
GITHUB_EVENT_RE = re.compile(r"[a-z_]{1,50}")
AGENT_DISPATCH = ("service", "job")

# The agent plane's credentials are the crown jewels: the org's subscription token and
# every agent's caller bearer. A NON-plane project that named one of these keys in its
# own chart Secret would mount the value into its own namespace. The generator refuses
# it at render time — and because every app Secret in the cluster is DECLARED through
# the generator (git is the source of truth, CI regenerates and diffs), that refusal is
# a real control, not just advice.
# Case-insensitive: ESO fetches by exact ASCII name so a lowercase variant is not itself
# exploitable, but a `claude_code_oauth_token` slipping the refusal is a needless gap, and
# refusing more never loosens anything.
RESERVED_SECRET_KEY_RE = re.compile(r"(CLAUDE_CODE_OAUTH_TOKEN|AGENT_[A-Z0-9_]+_TOKEN)$", re.I)
RESERVED_SECRET_NAME_RE = re.compile(r"(orbit-claude-code|agent-.+-token)$", re.I)

# The complete set of Kubernetes kinds the generator is ever allowed to WRITE into
# clusters/apps/. Argo's app-of-apps applies this directory with the app-controller's
# cluster-wide rights, so a manifest that reaches it runs with those rights. Chart
# workloads (Deployment, Service, …) are NOT here: those are rendered by Argo FROM the
# chart at apply time and never written to this directory. Anything the generator is
# about to write whose kind is not on this list is a bug or an injection, and is refused
# before the file is created.
WRITABLE_KINDS = frozenset({
    "Application", "Namespace", "ResourceQuota",
    "Instrumentation", "NetworkPolicy", "ConfigMap",
    # The intake's dispatch-failure alert (NEW-3). A monitoring resource: it can only
    # produce alerts, never grant a right, so it is safe to apply with the app-controller.
    "PrometheusRule",
    # Tenant Probes are built entirely from the generator-normalised hostname list.
    # No catalog field carries a manifest, module, prober address or namespace.
    "Probe",
    # orbitd's ConfigMap grant per tenant namespace (orbit-applier-configmaps, below):
    # a namespaced Role and its RoleBinding, never the Cluster* pair. The applier's
    # ConfigMap authority is namespaced instead of cluster-wide, and a Role that grants
    # rights orbitd does not hold yet cannot be created by orbitd itself (RBAC
    # escalation prevention), so the generator writes it and the app-of-apps applies
    # it. Names are fixed here; no catalog field reaches these two documents.
    "Role", "RoleBinding",
})

# The orbitd applier's ConfigMap rights in a tenant namespace, one Role + RoleBinding per
# namespace. Only configmaps, only create/patch/delete; get/list come from the
# cluster-wide read-only role. The applier itself never applies or prunes these (its
# hard rule: no RBAC object named orbit-*).
APPLIER_CONFIGMAPS_ROLE = "orbit-applier-configmaps"
# The subject is the ServiceAccount the platform template declares in
# template/provisioning/clusters/platform/config/orbitd/rbac.yaml (orbit/orbitd); the
# binding must name where that SA lives, so the pair is a literal here, not a lookup.
APPLIER_SERVICE_ACCOUNT = "orbitd"
APPLIER_SERVICE_ACCOUNT_NAMESPACE = "orbit"


def applier_configmaps_rbac(namespace: str) -> list[dict]:
    return [
        {"apiVersion": "rbac.authorization.k8s.io/v1", "kind": "Role",
         "metadata": {"name": APPLIER_CONFIGMAPS_ROLE, "namespace": namespace},
         "rules": [{"apiGroups": [""], "resources": ["configmaps"],
                    "verbs": ["create", "patch", "delete"]}]},
        {"apiVersion": "rbac.authorization.k8s.io/v1", "kind": "RoleBinding",
         "metadata": {"name": APPLIER_CONFIGMAPS_ROLE, "namespace": namespace},
         "roleRef": {"apiGroup": "rbac.authorization.k8s.io", "kind": "Role",
                     "name": APPLIER_CONFIGMAPS_ROLE},
         "subjects": [{"kind": "ServiceAccount", "name": APPLIER_SERVICE_ACCOUNT,
                       "namespace": APPLIER_SERVICE_ACCOUNT_NAMESPACE}]},
    ]


def _agent_int(agent: dict, name: str, key: str, default: int, lo: int, hi: int) -> int:
    """A bounded integer, or death. `True` is an int in Python and would sail through a
    naive isinstance check straight into a resource limit of 1."""
    value = agent.get(key, default)
    if isinstance(value, bool) or not isinstance(value, int) or not lo <= value <= hi:
        sys.exit(f"agent {_r(name)}: {key} must be an integer between {lo} and {hi} "
                 f"(got {_r(value)})")
    return value


def _pascal(s: str) -> str:
    """A dash/underscore-separated lowercase name to PascalCase — the convention every
    hand-written Orbit alert already follows (OrbitSiteDown, OrbitBackupStale)."""
    return "".join(part.capitalize() for part in re.split(r"[-_]", s) if part)


def _agent_alertname(agent: str, trigger: str) -> str:
    """The deterministic alertname a metric/logs trigger fires under, and the exact value
    the intake route matches on.

    NOT injective by construction — `_pascal` drops the separators, so agent `foo-bar` +
    trigger `baz` and agent `foo` + trigger `bar-baz` both land on OrbitAgentFooBarBaz, and
    `hot-x`/`hot--x`/`err-5`/`err5` collide within one agent. A collision is not cosmetic:
    the intake matches on the alertname alone, so ONE alert would dispatch to BOTH agents
    with the wrong prompt and the wrong bearer. Uniqueness is therefore enforced on the
    DERIVED name, globally across every agent and both trigger types — see
    `_claim_alertname`, which is the only sanctioned caller of this function."""
    return f"{DERIVED_ALERT_PREFIX}{_pascal(agent)}{_pascal(trigger)}"


def _claim_alertname(agent: str, trigger: str, claimed: dict) -> str:
    """Derive the alertname and claim it for this (agent, trigger) pair, or die naming the
    pair that already holds it. `claimed` is ONE dict for the whole plane."""
    alertname = _agent_alertname(agent, trigger)
    prior = claimed.get(alertname)
    if prior is not None:
        sys.exit(f"alertname collision: agent {_r(prior[0])} trigger {_r(prior[1])} and agent "
                 f"{_r(agent)} trigger {_r(trigger)} both derive the alertname {_r(alertname)}. "
                 f"The intake routes on the alertname alone, so one alert would dispatch "
                 f"to both agents — with the other one's prompt and bearer. Rename one "
                 f"(the derivation drops `-` and `_`, so `hot-x` and `hotx` are one name).")
    claimed[alertname] = (agent, trigger)
    return alertname


def _duration_seconds(dur: str) -> float:
    """A CRD-valid duration in seconds. Only ever called on a string that already matched
    PROM_DURATION_RE, so every term is <int><unit>."""
    return sum(int(n) * _DURATION_UNITS[u] for n, u in _DURATION_TERM_RE.findall(dur))


def _no_templating(value: str, name: str, kind: str, where: str) -> None:
    """Refuse Go/Prometheus templating in a catalog-supplied annotation or label value.

    Prometheus expands templates in a firing rule's annotations and labels, and the
    template function `query` runs arbitrary PromQL against the server. The summary is a
    human sentence that ends up in a Telegram message and in an agent's prompt; nothing in
    the catalog needs to be a program, so `{{`/`}}` is refused outright rather than
    escaped — escaping is a rule someone has to remember, a refusal is not."""
    for marker in TEMPLATE_MARKERS:
        if marker in value:
            sys.exit(f"agent {_r(name)}: the {_r(kind)} trigger's {where} contains {_r(marker)}. "
                     f"Prometheus expands Go templates in a rule's annotations and labels "
                     f"at alert time — `{{{{ query \"...\" }}}}` inside one executes PromQL "
                     f"— so catalog text may not carry template syntax. Write it plainly.")


def _logql_shape(expr: str, name: str) -> None:
    """A cheap structural check on a LogQL expression. NOT a parser — there is no local
    LogQL oracle the way promtool is one for PromQL (the verify suite runs `promtool check
    rules` over the rendered PrometheusRule; nothing equivalent exists for a Loki rule
    without pulling in Loki itself), so this catches the shapes that are unambiguously not
    LogQL — unbalanced delimiters, an unterminated string, no stream selector at all —
    and says so honestly rather than implying the expression was validated."""
    depth = {"(": 0, "[": 0, "{": 0}
    closer = {")": "(", "]": "[", "}": "{"}
    quote = ""
    for ch in expr:
        if quote:
            if ch == quote:
                quote = ""
            continue
        if ch in "\"'`":
            quote = ch
        elif ch in depth:
            depth[ch] += 1
        elif ch in closer:
            depth[closer[ch]] -= 1
            if depth[closer[ch]] < 0:
                sys.exit(f"agent {_r(name)}: the `logs` trigger `expr` closes a {_r(ch)} that "
                         f"was never opened — that is not a LogQL query")
    if quote:
        sys.exit(f"agent {_r(name)}: the `logs` trigger `expr` has an unterminated {_r(quote)} "
                 f"string")
    if any(v for v in depth.values()):
        sys.exit(f"agent {_r(name)}: the `logs` trigger `expr` has unbalanced delimiters "
                 f"{ {k: v for k, v in depth.items() if v} } — that is not a LogQL query")
    if "{" not in expr or "}" not in expr:
        sys.exit(f"agent {_r(name)}: the `logs` trigger `expr` contains no log stream "
                 f"selector (`{{...}}`). Every LogQL query selects a stream; without one "
                 f"the Loki ruler rejects the rule and the trigger silently never fires.")


def _metric_logs_trigger(trig: dict, name: str, kind: str, claimed: dict,
                         inst: dict) -> dict:
    """Validate a `metric` or `logs` trigger and normalise it into BOTH halves the plane
    needs from one block: the alerting-rule fields (expr/for/labels/annotations) and the
    intake `match` that reuses the exact routing path an `alert` trigger takes. `kind` is
    'metric' (PromQL) or 'logs' (LogQL); the schema and normal form are identical."""
    ruler = inst.get(LOKI_RULER_KEY)
    if kind == "logs" and ruler != LOKI_RULER_WIRED:
        sys.exit(
            f"agent {_r(name)} declares a `logs` trigger, but instance/config.yaml does not "
            f"say `{LOKI_RULER_KEY}: {LOKI_RULER_WIRED}`. A logs rule is evaluated by the "
            f"LOKI RULER, and a stock ruler loads nothing: (1) `loki.rulerConfig` carries "
            f"no `alertmanager_url`, so a firing rule notifies no one, and (2) its rule "
            f"storage is the S3 object store, so a rule in the ruler's local rule-store dir "
            f"is read by no one (needs `rulerConfig.storage: {{type: local, "
            f"local: {{directory: /etc/loki/rules}}}}` with the generated ConfigMap mounted "
            f"into the per-tenant /etc/loki/rules/fake subdir the ruler expects — or the rule "
            f"uploaded to the ruler's S3 prefix). Rendering it "
            f"now would publish a LIVE intake route in front of a rule that can never fire. "
            f"Wire the ruler, then declare `{LOKI_RULER_KEY}: {LOKI_RULER_WIRED}` "
            f"(the AGENT-PLANE.md record, '`logs` triggers').")
    tname = trig.get("name")
    if not isinstance(tname, str) or not TRIGGER_NAME_RE.fullmatch(tname):
        sys.exit(f"agent {_r(name)}: a {_r(kind)} trigger needs a `name` (a DNS-label "
                 f"fragment) — it identifies the trigger and becomes the tail of the "
                 f"alertname the intake routes on (got {_r(tname)})")
    # Uniqueness is claimed on the DERIVED alertname, not the raw name: that is the value
    # the rule fires under and the intake routes on, and the derivation is lossy.
    alertname = _claim_alertname(name, tname, claimed)
    expr = trig.get("expr")
    lang = "PromQL" if kind == "metric" else "LogQL"
    if not isinstance(expr, str) or not expr.strip() or len(expr) > MAX_EXPR:
        sys.exit(f"agent {_r(name)}: a {_r(kind)} trigger needs a non-empty `expr` (the {lang} "
                 f"condition, at most {MAX_EXPR} chars)")
    if "\n" in expr or "\r" in expr or "\x00" in expr:
        sys.exit(f"agent {_r(name)}: the {_r(kind)} trigger `expr` must be a single line — "
                 f"write the {lang} on one line")
    # PromQL is PARSED, but not here: the verify suite runs `promtool check rules` over the
    # rendered PrometheusRule (stage3), which is Prometheus's own parser and the only
    # honest answer to "is this expression real". LogQL has no such local oracle, so it
    # gets the cheap structural check below and the docs say plainly that it is not a
    # parser.
    if kind == "logs":
        _logql_shape(expr, name)
    for_dur = trig.get("for")
    if (not isinstance(for_dur, str) or not for_dur
            or not PROM_DURATION_RE.fullmatch(for_dur)):
        # The pattern is the CRD's own, so anything refused here would have been refused
        # by the admission webhook AFTER a green render and a push — a CI-green,
        # cluster-red wedge with the Argo app stuck SyncFailed and nothing else reporting.
        sys.exit(f"agent {_r(name)}: a {_r(kind)} trigger needs a `for` duration the condition "
                 f"must hold before it fires (e.g. 5m, 90s, 1h30m); got {_r(for_dur)}. Units "
                 f"run largest to smallest (y w d h m s ms) and each appears at most once "
                 f"— this is the PrometheusRule CRD's own pattern, so `30m1h` or `5m5m5m` "
                 f"would be rejected by the cluster after rendering green here.")
    if for_dur != FOR_IMMEDIATE and _duration_seconds(for_dur) < MIN_FOR_SECONDS:
        sys.exit(f"agent {_r(name)}: a {_r(kind)} trigger's `for` is {_r(for_dur)}, under the "
                 f"{MIN_FOR_SECONDS}s floor. Below a minute a flapping condition wakes the "
                 f"agent — and spends the org's subscription — once per evaluation. Write "
                 f"`{FOR_IMMEDIATE}` if firing on the first evaluation is genuinely what "
                 f"you mean (the AGENT-PLANE.md record, 'Runaway stance').")
    summary = trig.get("summary")
    if not isinstance(summary, str) or not summary.strip() or len(summary) > MAX_SUMMARY:
        sys.exit(f"agent {_r(name)}: a {_r(kind)} trigger needs a human `summary` (at most "
                 f"{MAX_SUMMARY} chars) — it is the annotation the alert and the agent see")
    _no_templating(summary, name, kind, "`summary`")
    description = trig.get("description")
    if description is not None:
        if (not isinstance(description, str) or not description.strip()
                or len(description) > MAX_DESCRIPTION):
            sys.exit(f"agent {_r(name)}: a {_r(kind)} trigger's optional `description` must be "
                     f"a non-empty string of at most {MAX_DESCRIPTION} chars")
        _no_templating(description, name, kind, "`description`")
    severity = trig.get("severity", "warning")
    if severity not in TRIGGER_SEVERITIES:
        sys.exit(f"agent {_r(name)}: {_r(kind)} trigger severity {_r(severity)} must be one of "
                 f"{TRIGGER_SEVERITIES}")
    if severity == INHIBITED_SEVERITY:
        _warn_info_severity(name, kind, trig.get("name"))
    raw_labels = trig.get("labels")
    labels: dict = {}
    if raw_labels is not None:
        if not isinstance(raw_labels, dict):
            sys.exit(f"agent {_r(name)}: a {_r(kind)} trigger's `labels` must be a mapping")
        for key, value in raw_labels.items():
            if not isinstance(key, str) or not LABEL_RE.fullmatch(key):
                sys.exit(f"agent {_r(name)}: {_r(kind)} label key {_r(key)} is not a label name")
            if key in RESERVED_RULE_LABELS:
                sys.exit(f"agent {_r(name)}: {_r(kind)} label {_r(key)} is set by the generator "
                         f"(severity has its own field; alertname/orbit_* are the router's "
                         f"and provenance) — a catalog label may not shadow it")
            if key.startswith(RESERVED_LABEL_PREFIX):
                sys.exit(f"agent {_r(name)}: {_r(kind)} label {_r(key)} is in Prometheus's own "
                         f"`__`-prefixed namespace (__name__, __address__, __meta_*). A "
                         f"label there is either dropped or rewrites what the sample IS; "
                         f"the catalog does not get to write it.")
            if not isinstance(value, str) or not value or len(value) > 256:
                sys.exit(f"agent {_r(name)}: {_r(kind)} label {_s(key, "label")}={_r(value)} must be a "
                         f"non-empty string of at most 256 characters")
            _no_templating(value, name, kind, f"label {_r(key)}")
            labels[key] = value
    return {
        "type": kind, "name": tname, "alertname": alertname,
        # The reused routing path: identical shape to an `alert` trigger's route entry, so
        # a metric/logs trigger and an alert trigger pointing at one agent compile to the
        # SAME structure in routes.json — the intake has one matcher kind, not three.
        #
        # `status: firing` is part of the match, not an afterthought: Alertmanager's
        # webhook fires once when the group starts firing and again when it resolves, and
        # without this every incident would dispatch the agent TWICE — the second time on
        # a payload saying the problem is over. (The plane's AM receiver also sets
        # send_resolved: false, so this is belt and braces; both are cheap.)
        "match": {"alertname": alertname, "status": "firing"},
        "expr": expr, "for": for_dur, "summary": summary,
        "description": description, "severity": severity, "labels": labels,
    }


def _trigger_rule(name: str, trig: dict) -> dict:
    """The alerting-rule body a metric/logs trigger renders — the shape Prometheus AND the
    Loki ruler both consume (groups[].rules[]). Provenance labels tie the alert back to the
    agent+trigger; the router itself only ever matches on the alertname those imply."""
    labels = {"severity": trig["severity"]}
    labels.update(trig["labels"])
    labels["orbit_agent"] = name
    labels["orbit_trigger"] = trig["name"]
    annotations = {"summary": trig["summary"]}
    # The Telegram receiver's message template already prints `{{ .Annotations.description
    # }}`; without this key every generated alert rendered a blank line there.
    if trig.get("description"):
        annotations["description"] = trig["description"]
    return {
        "alert": trig["alertname"],
        "expr": trig["expr"],
        "for": trig["for"],
        "labels": labels,
        "annotations": annotations,
    }


def agent_triggers(agent: dict, name: str, mode: str, claimed: dict,
                   inst: dict) -> list[dict]:
    """Validate an agent's `triggers:` list into the normalised form the router consumes.

    Closed key set PER TYPE, and the type itself enumerated. Refuses rather than ignores,
    for the reason every schema here is closed: a trigger that renders nothing reports
    nothing, and the operator's evidence that an agent is wired is that it was accepted.
    """
    raw = agent.get("triggers")
    if raw is None:
        return []
    if not isinstance(raw, list) or not raw:
        sys.exit(f"agent {_r(name)}: `triggers` must be a non-empty list of trigger entries "
                 f"— an empty list declares an event-driven agent nothing can wake")
    if len(raw) > MAX_TRIGGERS:
        sys.exit(f"agent {_r(name)}: {len(raw)} triggers (max {MAX_TRIGGERS})")
    out: list[dict] = []
    seen_a2a = False
    for trig in raw:
        if not isinstance(trig, dict):
            sys.exit(f"agent {_r(name)}: each entry under `triggers` must be a mapping")
        kind = trig.get("type")
        if kind in TRIGGER_DEFERRED:
            sys.exit(f"agent {_r(name)}: trigger type {_r(kind)} is not rendered here — "
                     f"{TRIGGER_DEFERRED[kind]}")
        if kind not in TRIGGER_KEYS:
            sys.exit(f"agent {_r(name)}: unknown trigger type {_r(kind)}; this cut renders "
                     f"{sorted(TRIGGER_KEYS)}")
        unknown = sorted(set(trig) - TRIGGER_KEYS[kind], key=str)
        if unknown:
            sys.exit(f"agent {_r(name)}: trigger type {_r(kind)} declares unknown keys "
                     f"{_s(unknown, 'key')}. The trigger schema is closed per type "
                     f"(known: {sorted(TRIGGER_KEYS[kind])}).")
        if kind == "alert":
            match = trig.get("match")
            if not isinstance(match, dict) or not match:
                sys.exit(f"agent {_r(name)}: an `alert` trigger needs a non-empty `match` of "
                         f"label: value pairs. An empty matcher routes EVERY alert on the "
                         f"platform to this agent, on the org's subscription.")
            pairs = {}
            for key, value in match.items():
                if not isinstance(key, str) or not LABEL_RE.fullmatch(key):
                    sys.exit(f"agent {_r(name)}: alert matcher key {_r(key)} is not a label name")
                # str(value) would turn `severity: 1` into "1" and a YAML `on` into "True",
                # neither of which any label ever equals — a matcher that can never fire.
                if not isinstance(value, str) or not value or len(value) > 256:
                    sys.exit(f"agent {_r(name)}: alert matcher {_s(key, "label")}={_r(value)} must be a "
                             f"non-empty string of at most 256 characters (matching is "
                             f"exact; quote the value in YAML)")
                _no_templating(value, name, kind, f"matcher {_r(key)}")
                if key == "alertname" and value.startswith(DERIVED_ALERT_PREFIX):
                    # The `OrbitAgent*` space belongs to DERIVED alertnames. A hand-written
                    # matcher in it either shadows a metric/logs trigger's route (one alert,
                    # two agents, the wrong prompt and bearer on one of them) or squats a
                    # name a future trigger would derive.
                    sys.exit(f"agent {_r(name)}: alert matcher alertname={_r(value)} is in the "
                             f"{DERIVED_ALERT_PREFIX}* space, which belongs to the "
                             f"alertnames metric/logs triggers derive. Declare the "
                             f"metric/logs trigger itself — it renders the rule AND this "
                             f"route — rather than matching a derived name by hand.")
                if key in PROVENANCE_RULE_LABELS:
                    # Only a GENERATED alert carries these. A hand-written matcher on one
                    # matches nothing else in the cluster, by construction — so it is not a
                    # matcher, it is a second subscription to some metric/logs trigger's
                    # alert, dispatching it to a second agent with the wrong prompt and the
                    # wrong bearer.
                    sys.exit(f"agent {_r(name)}: alert matcher {_s(key, "label")}={_r(value)} names a label "
                             f"only the agent plane's own generated alerts carry "
                             f"({sorted(PROVENANCE_RULE_LABELS)} are set by the generator "
                             f"on every metric/logs rule). Matching it double-dispatches "
                             f"another agent's trigger; declare the metric/logs trigger "
                             f"itself instead.")
                pairs[key] = value
            if "alertname" not in pairs:
                # Squat guard, the general case. The guard above only settles a match that
                # NAMES an alertname; a match without one is evaluated against every alert
                # on the platform INCLUDING the plane's own — a generated alert carries
                # `severity`, `namespace` and the trigger's catalog labels, so
                # `match: {severity: critical}` silently catches every critical metric
                # trigger's alert and runs this agent alongside the one that declared it.
                # An alertname is also the only thing that can be routed: the shipped
                # Alertmanager template sends `OrbitAgent*` to the intake and nothing else,
                # so a match with no alertname cannot be reached through it anyway (a
                # custom alertname still needs its own hand-added Alertmanager route — see
                # docs/AGENT-PLANE.md, "`alert` triggers and the shipped route").
                sys.exit(f"agent {_r(name)}: an `alert` trigger's `match` must name an "
                         f"`alertname` (got {sorted(pairs)}). Without one the matcher is "
                         f"evaluated against the plane's OWN generated alerts too — "
                         f"`{{severity: critical}}` catches every critical metric trigger "
                         f"and dispatches a second agent with the wrong prompt and bearer "
                         f"— and nothing in the shipped Alertmanager route would deliver "
                         f"it to the intake in the first place.")
            out.append({"type": "alert", "match": pairs})
        elif kind == "github":
            # GATED on the instance declaring the intake's public webhook door wired. The
            # generator renders the ROUTE (routes.json) and never the DOOR: it emits no
            # Ingress and no Certificate for the intake, by design. Without a hand-written,
            # path-scoped Ingress and the netpol that admits the ingress controller to the
            # intake pods, GitHub cannot reach /webhook/github at all — the route would be
            # live in front of a door that does not exist, which is exactly the shape the
            # `logs` gate was introduced to refuse.
            if inst.get(INTAKE_INGRESS_KEY) != INTAKE_INGRESS_WIRED:
                sys.exit(
                    f"agent {_r(name)} declares a `github` trigger, but instance/config.yaml "
                    f"does not say `{INTAKE_INGRESS_KEY}: {INTAKE_INGRESS_WIRED}`. A "
                    f"github trigger renders an intake ROUTE; it does NOT render a door. "
                    f"The generator emits no Ingress and no Certificate for the intake on "
                    f"purpose — the router holds every warm agent's bearer and can create "
                    f"Jobs, so it is never published because a catalog line said so. "
                    f"Rendering this now would publish a LIVE route GitHub cannot reach: "
                    f"every delivery would fail at the edge and nothing on this side would "
                    f"report why. Declaring `{INTAKE_INGRESS_KEY}: {INTAKE_INGRESS_WIRED}` "
                    f"asserts that this instance hand-wrote BOTH halves: (1) an Ingress "
                    f"whose host reaches the intake Service and is PATH-SCOPED to "
                    f"/webhook/github alone — the intake's GET /healthz and /metrics are "
                    f"its only unauthenticated routes and must stay unpublished — and (2) "
                    f"a NetworkPolicy admitting the ingress controller's pods to the intake "
                    f"pods' port, pod-narrowed on both sides. Wire both, then declare it "
                    f"(the AGENT-PLANE.md record, '`github` triggers').")
            repo = trig.get("repo")
            if not isinstance(repo, str) or not GITHUB_REPO_RE.fullmatch(repo):
                sys.exit(f"agent {_r(name)}: a `github` trigger needs repo: owner/name "
                         f"(got {_r(repo)})")
            events = trig.get("events")
            if not isinstance(events, list) or not events or len(events) > 32:
                sys.exit(f"agent {_r(name)}: a `github` trigger needs a non-empty `events` "
                         f"list of at most 32 GitHub event names. There is deliberately no "
                         f"wildcard: every delivery GitHub sends would wake the agent.")
            for event in events:
                if not isinstance(event, str) or not GITHUB_EVENT_RE.fullmatch(event):
                    sys.exit(f"agent {_r(name)}: {_r(event)} is not a GitHub event name "
                             f"(lower-case with underscores, e.g. pull_request)")
            out.append({"type": "github", "repo": repo, "events": sorted(set(events))})
        elif kind in ("metric", "logs"):
            out.append(_metric_logs_trigger(trig, name, kind, claimed, inst))
        else:  # a2a
            if seen_a2a:
                sys.exit(f"agent {_r(name)}: declares the `a2a` trigger twice; it is a "
                         f"presence flag, not a list of paths")
            seen_a2a = True
            out.append({"type": "a2a"})

    dispatch = agent.get("dispatch", "job" if mode == "cronjob" else "service")
    if dispatch not in AGENT_DISPATCH:
        sys.exit(f"agent {_r(name)}: dispatch {_r(dispatch)} must be one of {AGENT_DISPATCH}")
    if mode == "cronjob" and dispatch == "service":
        # A cronjob agent renders no Deployment and no Service, so there is nothing for the
        # intake to POST to: every event would 502 forever with nothing reporting why.
        sys.exit(f"agent {_r(name)} is mode: cronjob and sets dispatch: service, but a "
                 f"scheduled agent renders no Service — the intake would have nothing to "
                 f"call. Use dispatch: job (the default here), or make it a deployment "
                 f"agent so a warm instance exists.")
    for trig in out:
        trig["dispatch"] = dispatch
    return out


def intake_routes(routed: list[dict], image: str, tag: str) -> dict:
    """Compile every agent's triggers into the ONE routing config the intake mounts.

    The router's schema is closed and it CRASHES on an unknown key (services/intake/app.py
    `_boot_config`), so this function and that parser are one contract with two ends. It is
    built from validated agent data only — nothing here is copied through from the catalog
    unvalidated, and the event payload contributes nothing at all.
    """
    agents: dict[str, dict] = {}
    alert, github, a2a = [], [], []
    for entry in routed:
        name = entry["name"]
        dispatch = entry["dispatch"]
        if dispatch == "service":
            # Cluster-local http to a ClusterIP Service in this same namespace, whose
            # NetworkPolicy admits only its own pods. The per-agent bearer reaches the
            # intake as its OWN env var, sourced from the SAME Secret the agent reads.
            agents[name] = {
                "dispatch": "service",
                "url": f"http://{AGENTS_PROJECT}-{name}/",
                "tokenEnv": "INTAKE_AGENT_TOKEN_" + name.upper().replace("-", "_"),
            }
        else:
            job = {
                "namespace": f"{AGENTS_PROJECT}-{AGENTS_ENV}",
                # The instance pin, tag included. There is no per-agent image anywhere in
                # the plane — a per-agent image chooses the registry that receives the
                # mounted subscription token — and the intake is no exception: it can only
                # ever create a Job on the image the generator wrote here.
                "image": f"{image}:{tag}",
                # The plane's ordinary ServiceAccount, NOT the intake's. The Job needs no
                # Kubernetes API of its own, and must not inherit the create right merely
                # because the thing that launched it holds one.
                "serviceAccountName": AGENTS_PROJECT,
                "uid": entry["uid"],
                "memory": entry["memory"],
                "activeDeadlineSeconds": entry["timeout"] + ACTIVE_DEADLINE_GRACE_S,
                "backoffLimit": INTAKE_JOB_BACKOFF,
                "ttlSecondsAfterFinished": INTAKE_JOB_TTL_S,
                "imagePullSecret": "ghcr-pull",
                "env": {
                    "ORBIT_AGENT_NAME": name,
                    "ORBIT_AGENT_PROMPT": entry["prompt"],
                    "ORBIT_AGENT_MODEL": entry["model"],
                    "ORBIT_AGENT_MAX_TURNS": str(entry["turns"]),
                    "ORBIT_AGENT_TIMEOUT_S": str(entry["timeout"]),
                    # The OTLP exporter ENDPOINT is deliberately NOT set here: intake injects
                    # its own trusted endpoint into the Job at dispatch (job_manifest), and
                    # the router's env allowlist REFUSES a config-set OTEL_EXPORTER_* so a
                    # poisoned config cannot point an agent Job's telemetry at an attacker
                    # collector. Only the two identity variables are carried — a Job bypasses
                    # the chart, so without them every event-triggered run would trace as an
                    # anonymous service and the console's run history would lose it
                    # (OBSERVABILITY.md Decision 5).
                    "OTEL_SERVICE_NAME": f"{AGENTS_PROJECT}-{name}",
                    "OTEL_RESOURCE_ATTRIBUTES":
                        f"service.namespace={AGENTS_PROJECT},"
                        f"deployment.environment.name={AGENTS_ENV},"
                        f"service.version={tag}",
                },
                "secretEnv": {
                    "ORBIT_AGENT_TOKEN": {"secret": entry["token_secret"],
                                          "key": entry["token_key"]},
                    AGENT_MODEL_KEY: {"secret": AGENT_MODEL_SECRET, "key": AGENT_MODEL_KEY},
                },
            }
            if entry.get("cpu"):
                job["cpu"] = entry["cpu"]
            if entry.get("skills_cm"):
                job["volumes"] = [{"name": "content", "mountPath": AGENT_SKILLS_DIR,
                                   "configMap": entry["skills_cm"]}]
            agents[name] = {"dispatch": "job", "job": job}
        for trig in entry["triggers"]:
            # `metric` and `logs` deliver through Alertmanager exactly as `alert` does —
            # their generated rule fires an alert, and the router matches it on the
            # alertname the rule carries. So all three share ONE routing path: no fork, no
            # second matcher kind. The rule half is emitted in agents_project.
            if trig["type"] in ("alert", "metric", "logs"):
                alert.append({"agent": name, "match": trig["match"]})
            elif trig["type"] == "github":
                github.append({"agent": name, "repo": trig["repo"],
                               "events": trig["events"]})
            else:
                a2a.append({"agent": name})
    return {"version": 1, "agents": agents,
            "routes": {"alert": alert, "github": github, "a2a": a2a}}


def agents_project(entries, inst: dict) -> tuple[dict, list[dict], dict] | None:
    """The `agents:` catalog block -> one synthetic project the ordinary renderer emits.

    Returns (plane, extras, values) — the synthetic project dict, the generator-BUILT
    manifests (skills ConfigMaps, the intake's routing ConfigMap) that travel with its
    Application, and generator-BUILT chart values that no catalog field can address (the
    intake's RBAC directive). The extras and values are
    returned SEPARATELY rather than stashed on the project dict on purpose: catalog
    project entries have no closed key set, so a field the render loop reads off the
    project dict is a field a hand-written catalog project can also set — and these
    manifests are applied with the app-controller's cluster-wide rights. Keeping them out
    of any catalog-addressable structure is what stops `_extra:` in a tenant project from
    smuggling a ClusterRoleBinding into the cluster.

    Returns None when nothing is declared or everything is disabled, so an instance with
    no agents gets no namespace, no Application and no quota — the plane costs nothing
    until it is used.
    """
    # A CLOSED value, checked here rather than on the `logs` path that consumes it: an
    # instance flag whose value is a typo (`lokiRuler: true`, `lokiRuler: soon`) must not
    # read as "wired" and must not read as absent either. Inside `_metric_logs_trigger` it
    # only ran once a metric/logs trigger existed, so an instance could carry a misspelt
    # declaration for months and discover it at the moment it first declared a trigger —
    # the one moment it believed the ruler was already wired.
    ruler = inst.get(LOKI_RULER_KEY)
    if ruler is not None and ruler != LOKI_RULER_WIRED:
        sys.exit(f"instance/config.yaml `{LOKI_RULER_KEY}` is {_r(ruler)}; the only value "
                 f"it takes is {_r(LOKI_RULER_WIRED)} (or remove the key). It is a "
                 f"declaration that this instance's Loki ruler is wired to Alertmanager "
                 f"and reads its rules from its local rule-store dir — see the AGENT-PLANE.md record.")
    # The same closed-value discipline for the intake's public-door declaration, checked in
    # the same place and for the same reason: read only on the `github` trigger path, a
    # misspelt `intakeIngress: true` would sit green for months and surface at the exact
    # moment the operator believed the door was already open.
    door = inst.get(INTAKE_INGRESS_KEY)
    if door is not None and door != INTAKE_INGRESS_WIRED:
        sys.exit(f"instance/config.yaml `{INTAKE_INGRESS_KEY}` is {_r(door)}; the only value "
                 f"it takes is {_r(INTAKE_INGRESS_WIRED)} (or remove the key). It is a "
                 f"declaration that this instance hand-wrote the intake's public webhook "
                 f"door: an Ingress path-scoped to /webhook/github alone, plus a netpol "
                 f"admitting the ingress controller's pods to the intake pods — see "
                 f"the AGENT-PLANE.md record.")
    if entries is None:
        return None
    if not isinstance(entries, list):
        sys.exit("catalog `agents:` must be a list of agent entries")
    runtime = inst.get("agentRuntime") or {}
    if not isinstance(runtime, dict):
        sys.exit("instance/config.yaml `agentRuntime` must be a mapping of image/tag")

    components: list[dict] = []
    secrets: list[dict] = [{"name": AGENT_MODEL_SECRET, "keys": [AGENT_MODEL_KEY]}]
    extras: list[dict] = []
    seen: set[str] = set()
    # Agents that declare `triggers:` — collected here and compiled into ONE routing
    # config after the loop, so the router has a single source of truth for the plane.
    routed: list[dict] = []
    # The rule half of `metric`/`logs` triggers: (agent, normalised trigger). Compiled
    # after the loop into ONE PrometheusRule and ONE Loki-ruler ConfigMap for the plane,
    # so the alert half and the intake route half are always emitted together.
    metric_rules: list[tuple[str, dict]] = []
    logs_rules: list[tuple[str, dict]] = []
    # Every alertname derived from a metric/logs trigger, plane-wide: {alertname: (agent,
    # trigger)}. ONE dict for the whole catalog, because the collision that matters is
    # ACROSS agents — the intake matches on the alertname alone, so two agents deriving
    # one name means one alert dispatched to both, each with the other's prompt/bearer.
    claimed_alertnames: dict[str, tuple[str, str]] = {}

    # The runtime image is ONE instance-level pin for the whole plane. Validated once,
    # here, so every agent runs exactly the image core CI published — no per-agent
    # override exists, so no catalog entry can point an agent (and the token mounted into
    # it) at a registry of its choosing.
    only_enabled = [a for a in entries
                    if isinstance(a, dict) and a.get("enabled", True) is not False]
    if only_enabled:
        image = runtime.get("image")
        tag = runtime.get("tag")
        if not image or not tag:
            sys.exit("instance/config.yaml declares no agentRuntime.image + "
                     "agentRuntime.tag, but the catalog declares agents. The agent "
                     "runtime image is published by core CI under the core repo's owner; "
                     "the generator refuses to guess the reference (the AGENT-PLANE.md record).")
        if not IMAGE_RE.fullmatch(str(image)):
            sys.exit(f"agentRuntime.image {_r(image)} must be a bare repository reference — "
                     f"the tag belongs in agentRuntime.tag (a ':' here renders "
                     f"'repo:tag:latest', which pulls nothing)")
        if not TAG_RE.fullmatch(str(tag)):
            sys.exit(f"agentRuntime.tag {_r(tag)} is not a valid image tag")
        image, tag = str(image), str(tag)

    for agent in entries:
        if not isinstance(agent, dict):
            sys.exit("each entry under `agents:` must be a mapping")
        name = agent.get("name")
        if not isinstance(name, str) or not AGENT_NAME_RE.fullmatch(name):
            sys.exit(f"invalid agent name {_r(name)}: must match {AGENT_NAME_RE.pattern} "
                     f"(it becomes a workload name, a Service and an OTel service.name)")
        if name == INTAKE_COMPONENT:
            # The intake renders as a component of this same project, so both would be
            # named orbit-agents-intake: one Deployment silently overwrites the other and
            # the plane's router or the agent — whichever lost — simply is not there.
            sys.exit(f"agent name {_r(INTAKE_COMPONENT)} is reserved for the plane's event "
                     f"router (Phase D.3); both would render as "
                     f"{AGENTS_PROJECT}-{INTAKE_COMPONENT} and collide")
        unknown = sorted(set(agent) - AGENT_KEYS, key=str)
        if unknown:
            sys.exit(f"agent {_r(name)} declares unknown keys {_s(unknown, 'key')}. The agent schema "
                     f"is closed on purpose: an unknown key either renders nothing at "
                     f"all or is a value — a token, a URL — that would be committed in "
                     f"plaintext. Known keys: {sorted(AGENT_KEYS)}")
        if name in seen:
            sys.exit(f"duplicate agent name {_r(name)}")
        seen.add(name)

        enabled = agent.get("enabled", True)
        if not isinstance(enabled, bool):
            sys.exit(f"agent {_r(name)}: `enabled` must be true or false")

        mode = agent.get("mode", "deployment")
        if mode not in ("deployment", "cronjob"):
            sys.exit(f"agent {_r(name)}: unknown mode {_r(mode)}; the plane renders "
                     f"'deployment' (a long-running Deployment + Service) and 'cronjob' "
                     f"(a scheduled CronJob, Phase D.3)")
        # Cron controls mean nothing on a Deployment: they would render nothing and report
        # nothing — a scheduled intent silently becoming a long-running service. Refuse
        # them off the cronjob path rather than drop them on the floor.
        if mode != "cronjob":
            stray = sorted(set(agent) & CRON_KEYS)
            if stray:
                sys.exit(f"agent {_r(name)} is mode: {mode} but sets {stray}, which apply "
                         f"only to mode: cronjob — a schedule on a Deployment never fires")

        # `triggers:` (Phase D.3) is what makes an agent event-driven. `dispatch:` says
        # HOW the intake wakes it and means nothing without one, so it is refused alone
        # rather than ignored — the same rule the cron controls follow.
        triggers = agent_triggers(agent, name, mode, claimed_alertnames, inst)
        if not triggers and "dispatch" in agent:
            sys.exit(f"agent {_r(name)} sets `dispatch` but declares no `triggers` — "
                     f"dispatch only says how the intake wakes an agent, and nothing "
                     f"would ever wake this one")

        # A DISABLED agent renders nothing — and is validated anyway, above this line.
        # `enabled: false` is how an agent is parked mid-edit, and the moment it is worth
        # anything is the moment someone flips it back: a parked agent whose trigger
        # schema is wrong, or whose metric trigger derives an alertname another agent
        # already holds, must be red NOW rather than at the flip. Its derived alertnames
        # are claimed in the same plane-wide registry for the same reason — a name a
        # parked agent will wake up holding is not a name a live one may take.
        if not enabled:
            continue

        prompt = agent.get("prompt")
        if not isinstance(prompt, str) or not prompt.strip():
            sys.exit(f"agent {_r(name)} declares no prompt. Absent one the image falls back "
                     f"to its placeholder system prompt and the agent does something "
                     f"nobody asked for, with the org's subscription paying for it.")
        if len(prompt) > MAX_PROMPT:
            sys.exit(f"agent {_r(name)}: prompt is {len(prompt)} characters (max "
                     f"{MAX_PROMPT}) — long instructions belong in a skill, which is "
                     f"mounted and versioned rather than pasted into a Deployment")

        model = str(agent.get("model", AGENT_MODEL))
        if not AGENT_MODEL_RE.fullmatch(model):
            sys.exit(f"agent {_r(name)}: model {_r(model)} is not a model id")

        klass = agent.get("class", "background")
        if klass not in AGENT_CLASSES:
            sys.exit(f"agent {_r(name)}: class {_r(klass)} must be one of {AGENT_CLASSES}")

        # Validate the resource quantities in the generator, not only in the chart's
        # schema downstream: a bad `memory: 512MB` that reaches the chart renders a broken
        # Application on a green commit, and the failure surfaces as an Argo sync error
        # long after review.
        memory = str(agent.get("memory", "512Mi"))
        if not QUANTITY_RE.fullmatch(memory):
            sys.exit(f"agent {_r(name)}: memory {_r(memory)} is not a Kubernetes quantity "
                     f"(e.g. 512Mi, 1Gi — not 512MB)")
        # The optional burst ceiling. `memory` is the reservation the scheduler holds;
        # `memoryLimit` is what the kernel kills on, and for an agent those are genuinely
        # different numbers — the pod is a small Python server that spawns the `claude`
        # CLI per task, so it is ~14Mi idle and ~275Mi for the minute a task runs.
        # Defaulting it to `memory` keeps the historical request == limit shape for every
        # agent that does not name one.
        #
        # A limit BELOW the request is refused here rather than at apply time: the API
        # server rejects such a pod, which on a green commit means an Application that
        # syncs to a Deployment whose ReplicaSet can never create a pod — a failure that
        # surfaces as a mismatched replica count, not as a validation error.
        memory_limit = agent.get("memoryLimit")
        if memory_limit is not None:
            memory_limit = str(memory_limit)
            if not QUANTITY_RE.fullmatch(memory_limit):
                sys.exit(f"agent {_r(name)}: memoryLimit {_r(memory_limit)} is not a "
                         f"Kubernetes quantity (e.g. 512Mi, 1Gi — not 512MB)")
            if _quantity_bytes(memory_limit) < _quantity_bytes(memory):
                sys.exit(f"agent {_r(name)}: memoryLimit {_r(memory_limit)} is below its "
                         f"memory request {_r(memory)} — the ceiling cannot sit under the "
                         f"reservation")
        cpu = agent.get("cpu")
        if cpu is not None and not CPU_RE.fullmatch(str(cpu)):
            sys.exit(f"agent {_r(name)}: cpu {_r(cpu)} is not a Kubernetes CPU quantity "
                     f"(e.g. 100m, 1)")

        uid = _agent_int(agent, name, "uid", AGENT_UID, 1, 65535)
        turns = _agent_int(agent, name, "maxTurns", 12, 1, 100)
        timeout = _agent_int(agent, name, "timeoutSeconds", 300, 30, 3600)
        concurrency = _agent_int(agent, name, "maxConcurrency", 2, 1, 10)

        # --- cron controls (mode: cronjob only). Each is validated HERE and passed to the
        # chart as an EXPLICIT value rather than leaning on a chart default — a bad value
        # fails at generation, and the CronJob's controls are auditable in the committed
        # Application rather than implied by the chart. schedule is validated against the
        # same pattern the chart schema enforces.
        cron: dict = {}
        if mode == "cronjob":
            schedule = agent.get("schedule")
            if not isinstance(schedule, str) or not CRON_RE.fullmatch(schedule):
                sys.exit(f"agent {_r(name)} is mode: cronjob and needs a valid `schedule` — "
                         f"five cron fields ('0 3 * * *') or an @macro ('@daily'); got "
                         f"{_r(schedule)}. A missing or malformed schedule renders a CronJob "
                         f"the API server rejects at apply time, on a green commit.")
            concurrency_policy = agent.get("concurrencyPolicy", "Forbid")
            if concurrency_policy not in CRON_CONCURRENCY:
                sys.exit(f"agent {_r(name)}: concurrencyPolicy {_r(concurrency_policy)} must be "
                         f"one of {CRON_CONCURRENCY} — never 'Allow': each concurrent run "
                         f"holds the org subscription token and spends against it, so the "
                         f"plane caps a scheduled agent to one running Job.")
            cron = {
                "kind": "cron",
                "schedule": schedule,
                "concurrencyPolicy": concurrency_policy,
                "startingDeadlineSeconds":
                    _agent_int(agent, name, "startingDeadlineSeconds", 300, 10, 3600),
                "backoffLimit": _agent_int(agent, name, "backoffLimit", 2, 0, 10),
                "successfulJobsHistoryLimit":
                    _agent_int(agent, name, "successfulJobsHistoryLimit", 3, 0, 20),
                "failedJobsHistoryLimit":
                    _agent_int(agent, name, "failedJobsHistoryLimit", 3, 0, 20),
                # Derived, not a catalog key: the Job-level wall-clock backstop UNDER
                # app.py's in-process ORBIT_AGENT_TIMEOUT_S — the agent's own timeout plus
                # a grace for image pull and CLI spawn.
                "activeDeadlineSeconds": timeout + ACTIVE_DEADLINE_GRACE_S,
                # An agent never restarts in place: a failed scheduled run is a fresh Job
                # attempt bounded by backoffLimit, not a hot-loop on the same pod that
                # would re-run a partial task and re-spend the subscription.
                "restartPolicy": "Never",
            }

        # --- skills: read out of the repo, shipped as a ConfigMap, mounted one
        # directory per skill. The runtime only discovers <dir>/SKILL.md, so the item
        # paths are the contract; a flat mount reports nothing and teaches the agent
        # nothing.
        # The text lands in a committed manifest, so it goes through no-credentials.py
        # like every other generated value. That scanner matches KNOWN credential SHAPES
        # (sk-ant-… Anthropic tokens, ghp_… GitHub tokens, private keys, JWTs, AWS keys),
        # so a shaped secret pasted into a SKILL.md turns the tree red — the check
        # working, not a false positive, since a skill is documentation and documentation
        # needs no live secret. It does NOT catch a high-entropy string of no recognisable
        # shape (a bare hex/base64 blob), and adding an entropy heuristic was rejected in
        # review as too false-positive-prone; that gap is stated plainly rather than
        # papered over. The agent bearer is not exposed to it either way — it is emitted
        # ONLY as a Secret pointer below, never as a literal.
        skills = agent.get("skills") or []
        if not isinstance(skills, list):
            sys.exit(f"agent {_r(name)}: `skills` must be a list of skill directory names")
        config_map = None
        if skills:
            data: dict[str, str] = {}
            for skill in skills:
                if not isinstance(skill, str) or not SKILL_RE.fullmatch(skill):
                    sys.exit(f"agent {_r(name)}: invalid skill name {_r(skill)} — a directory "
                             f"name under .claude/skills, matching {SKILL_RE.pattern}")
                if skill in data:
                    sys.exit(f"agent {_r(name)}: skill {_r(skill)} listed twice")
                src = SKILLS_SRC / skill / "SKILL.md"
                if not src.is_file():
                    sys.exit(f"agent {_r(name)} mounts skill {_r(skill)}, but "
                             f".claude/skills/{skill}/SKILL.md does not exist. A skills "
                             f"mount naming nothing is inert: the pod starts, the card "
                             f"lists no skill, and the agent runs without the knowledge "
                             f"it was given.")
                data[skill] = src.read_text()
            cm_name = f"{AGENTS_PROJECT}-{name}-skills"
            extras.append({"apiVersion": "v1", "kind": "ConfigMap",
                           "metadata": {"name": cm_name,
                                        "namespace": f"{AGENTS_PROJECT}-{AGENTS_ENV}"},
                           "data": data})
            config_map = {"name": cm_name, "mountPath": AGENT_SKILLS_DIR,
                          "items": [{"key": s, "path": f"{s}/SKILL.md"}
                                    for s in sorted(data)]}

        # --- the caller bearer. The runtime answers 503 to every POST unless
        # ORBIT_AGENT_TOKEN is set (services/agent-runtime/app.py), so this is what
        # makes a rendered agent reachable at all. Per agent, so revoking one does not
        # revoke the plane, and a pointer rather than a value: the token is sealed once
        # in the sops plane under the key named here, and anything that must CALL this
        # agent (the D.3 intake, an operator) reads the same Secret in the same namespace.
        # The bearer's ONLY sanctioned form is this pointer. A high-entropy random string
        # (openssl rand) has no shape the credential scanner recognises, so an operator
        # who pasted a literal bearer into a catalog `env` value would NOT be caught by
        # no-credentials.py — that is operator error the scanner does not cover, and the
        # generator never emits one: it is always the {secret, key} reference below.
        token_secret = f"agent-{name}-token"
        token_key = "AGENT_" + name.upper().replace("-", "_") + "_TOKEN"
        secrets.append({"name": token_secret, "keys": [token_key]})

        component = {
            "name": name,
            "class": klass,
            # The instance pin, identical for every agent — validated once above.
            "image": image,
            "tag": tag,
            "memory": memory,
            "securityContext": {"runAsUser": uid, "runAsGroup": uid},
            # `memoryLimit` is emitted only when declared, so the generated Application
            # of an agent that does not split the two is byte-identical to before.
            **({"memoryLimit": memory_limit} if memory_limit else {}),
            "env": {
                "ORBIT_AGENT_NAME": name,
                "ORBIT_AGENT_PROMPT": prompt,
                "ORBIT_AGENT_MODEL": model,
                "ORBIT_AGENT_TOKEN": {"secret": token_secret, "key": token_key},
                AGENT_MODEL_KEY: {"secret": AGENT_MODEL_SECRET, "key": AGENT_MODEL_KEY},
                # The runtime emits one OTLP span per run over plain HTTP — no SDK, by
                # design (its dependency surface is a security property on a pod holding
                # a subscription token). So `language:` is deliberately NOT set here:
                # operator injection would add a second SDK to instrument a service that
                # already traces itself. Identity still comes from the platform — the
                # chart sets OTEL_SERVICE_NAME/OTEL_RESOURCE_ATTRIBUTES for every
                # component (OBSERVABILITY.md Decision 5).
                "OTEL_EXPORTER_OTLP_ENDPOINT": OTLP_ENDPOINT,
                # Brakes (AGENT-PLANE.md): a run that cannot end is a budget with no
                # ceiling. Spend caps proper are the console's; these are the floor.
                "ORBIT_AGENT_MAX_TURNS": str(turns),
                "ORBIT_AGENT_TIMEOUT_S": str(timeout),
                "ORBIT_AGENT_MAX_CONCURRENCY": str(concurrency),
            },
        }
        if mode == "cronjob":
            # A CronJob on the runtime image, run ONE-SHOT: the container performs the
            # agent's scheduled task once and EXITS with its status — no long-running
            # server, so restartPolicy/backoffLimit/activeDeadlineSeconds behave as batch
            # controls should. No `port` and no `probePath`: the chart therefore renders
            # NO Service and no probes, because a scheduled agent has no external caller
            # and serves nothing. `kind: cron` + the cron controls come from `cron`.
            #
            # ORBIT_AGENT_ONESHOT is the switch the runtime reads to run one task and exit
            # instead of serving. app.py has no one-shot entrypoint today (it is
            # HTTP-server-only); that is a REPORTED runtime change (docs/AGENT-PLANE.md),
            # and it reuses the exact same run path — check_credentials + the tool
            # denylist + the brakes — so it does not touch the HTTP server's security
            # posture. Until the image gains it, a rendered cron agent starts the server
            # and the Job is killed at activeDeadlineSeconds; the renderer ships with the
            # runtime change, never a loosened server.
            component["env"]["ORBIT_AGENT_ONESHOT"] = "1"
            component.update(cron)
        else:
            # `web` + a port renders a Deployment and a Service; with no hostnames on the
            # environment the chart renders no Ingress and no certificate, which is the
            # whole point — an agent is reachable by its neighbours, never from outside.
            component["kind"] = "web"
            component["port"] = AGENT_PORT
            component["probePath"] = AGENT_HEALTH
        if cpu is not None:
            component["cpu"] = str(cpu)
        if config_map:
            component["configMap"] = config_map
        components.append(component)

        if triggers:
            # Carried as VALIDATED values, never as the raw catalog dict: intake_routes
            # copies these straight into a config the router trusts to name an image, a
            # namespace and an env, so nothing unvalidated may reach it.
            routed.append({
                "name": name, "triggers": triggers,
                "dispatch": triggers[0]["dispatch"],
                # The BURST size, not the reservation. This value sizes the one-shot Job
                # the intake creates per event (dispatch: job), and that pod's entire life
                # IS the run — there is no idle steady state to reserve for, so it gets
                # the ceiling. A warm `dispatch: service` agent is the opposite case and
                # is sized by the component above.
                "prompt": prompt, "model": model, "uid": uid,
                "memory": memory_limit or memory,
                "cpu": str(cpu) if cpu is not None else None,
                "turns": turns, "timeout": timeout,
                "token_secret": token_secret, "token_key": token_key,
                "skills_cm": ({"name": config_map["name"], "items": config_map["items"]}
                              if config_map else None),
            })
            for trig in triggers:
                if trig["type"] == "metric":
                    metric_rules.append((name, trig))
                elif trig["type"] == "logs":
                    logs_rules.append((name, trig))

    if not components:
        return None

    # --- the intake, rendered ONLY when something declares a trigger -----------------
    # No triggers, no router: an instance with only cron and manual agents pays nothing
    # for the event plane, and its generated output is byte-identical to before D.3.
    gen_values: dict = {}
    if routed:
        intake = inst.get("intakeRuntime") or {}
        if not isinstance(intake, dict):
            sys.exit("instance/config.yaml `intakeRuntime` must be a mapping of image/tag")
        i_image, i_tag = intake.get("image"), intake.get("tag")
        if not i_image or not i_tag:
            sys.exit("the catalog declares agent `triggers:`, which need the intake event "
                     "router, but instance/config.yaml declares no intakeRuntime.image + "
                     "intakeRuntime.tag. Like the agent runtime, the intake image is "
                     "published by core CI under the core repo's owner and pinned per "
                     "instance; the generator refuses to guess the reference.")
        if not IMAGE_RE.fullmatch(str(i_image)):
            sys.exit(f"intakeRuntime.image {_r(i_image)} must be a bare repository reference "
                     f"— the tag belongs in intakeRuntime.tag")
        if not TAG_RE.fullmatch(str(i_tag)):
            sys.exit(f"intakeRuntime.tag {_r(i_tag)} is not a valid image tag")
        i_image, i_tag = str(i_image), str(i_tag)

        config = intake_routes(routed, image, tag)
        cm_name = f"{AGENTS_PROJECT}-{INTAKE_COMPONENT}-routes"
        # sort_keys + a fixed indent: the routing config is committed, so a
        # regenerate-and-diff drift check has to be byte-stable across runs and
        # across Python versions (dict order is insertion order, and the route
        # lists are built in catalog order, which is the author's own).
        routes_json = json.dumps(config, indent=1, sort_keys=True)
        # FOUND LIVE (2026-08-29). The intake parses routes.json ONCE, at import
        # (services/intake/app.py `CONFIG = _boot_config()`). Nothing on the rendered
        # Deployment referred to the ConfigMap's CONTENT, so adding a trigger updated the
        # ConfigMap, Argo reported Synced/Healthy, and the running pod kept serving the
        # routing table it booted with: Alertmanager delivered the new alert and the
        # router answered 404 no_route until someone ran `kubectl rollout restart`. The
        # standard checksum/config pattern closes it — the hash of the rendered content is
        # part of the pod template, so a routes change IS a Deployment change and Argo
        # rolls the router by itself. A digest of the exact bytes written below, never a
        # timestamp: an unchanged catalog must re-render byte-identically or `make check`
        # would go red on every run.
        routes_checksum = hashlib.sha256(routes_json.encode("utf-8")).hexdigest()
        extras.append({
            "apiVersion": "v1", "kind": "ConfigMap",
            "metadata": {"name": cm_name,
                         "namespace": f"{AGENTS_PROJECT}-{AGENTS_ENV}"},
            "data": {INTAKE_ROUTES_KEY: routes_json},
        })
        secrets.append({"name": INTAKE_SECRET, "keys": sorted(INTAKE_SECRET_KEYS)})

        plane_ns = f"{AGENTS_PROJECT}-{AGENTS_ENV}"
        # NEW-1: the plane's default-deny NetworkPolicy admits only own-plane pods, but
        # Alertmanager lives in `monitoring` and its POST — and Prometheus's scrape of
        # /metrics — would be dropped, leaving the alert trigger path dead. A SEPARATE,
        # tightly-scoped policy admits `monitoring` to the intake pod ONLY, on the intake
        # port ONLY; every other pod on the plane stays own-pods-only. (GitHub from the
        # internet needs a public Ingress+Certificate and is a tracked follow-up, not this
        # cut — see docs/ONBOARDING.md.)
        extras.append({
            "apiVersion": "networking.k8s.io/v1", "kind": "NetworkPolicy",
            "metadata": {"name": f"{AGENTS_PROJECT}-{INTAKE_COMPONENT}-allow-monitoring",
                         "namespace": plane_ns},
            "spec": {
                "podSelector": {"matchLabels": {
                    "app.kubernetes.io/name": INTAKE_COMPONENT,
                    "app.kubernetes.io/instance": AGENTS_PROJECT}},
                "policyTypes": ["Ingress"],
                "ingress": [{
                    "from": [{"namespaceSelector": {"matchLabels": {
                        "kubernetes.io/metadata.name": "monitoring"}}}],
                    "ports": [{"protocol": "TCP", "port": INTAKE_PORT}],
                }],
            },
        })
        # NEW-3: dispatch happens after the 202, so a failed dispatch is invisible to
        # HTTP-level metrics and, for a GitHub event, lost for good. Alert on the router's
        # own dispatch-outcome counter. Discovered cluster-wide (kube-prometheus-stack runs
        # nil rule selectors); the ServiceMonitor that feeds it is rendered by the chart
        # from the intake component's `metrics` block.
        extras.append({
            "apiVersion": "monitoring.coreos.com/v1", "kind": "PrometheusRule",
            "metadata": {"name": f"{AGENTS_PROJECT}-{INTAKE_COMPONENT}",
                         "namespace": plane_ns},
            "spec": {"groups": [{
                "name": "orbit.intake",
                "rules": [{
                    "alert": "OrbitIntakeDispatchFailing",
                    "expr": ('sum by (source) (increase('
                             'orbit_intake_dispatch_total{outcome=~"failed|refused|crashed"}'
                             '[15m])) > 0'),
                    "for": "5m",
                    "labels": {"severity": "warning"},
                    "annotations": {
                        "summary": "Orbit intake is failing to dispatch events",
                        "description": ("The intake router failed to dispatch one or more "
                                        "{{ $labels.source }} events in the last 15m. The "
                                        "source may not redeliver (GitHub only retries on a "
                                        "non-2xx and the reply was 202), so the run may be "
                                        "lost — check the intake logs."),
                    },
                }],
            }]},
        })

        # metric triggers: ONE PrometheusRule for the whole plane, one alert per trigger.
        # The alert fires when the catalog's PromQL `expr` holds for its `for`, under the
        # deterministic alertname the intake route matches — so the rule half and the route
        # half come from the one catalog block. Same namespace and cluster-wide discovery as
        # the NEW-3 rule above (kube-prometheus-stack runs nil rule selectors). Rules sorted
        # by alertname so a regenerate-and-diff stays byte-stable regardless of catalog order.
        if metric_rules:
            rules = sorted((_trigger_rule(n, t) for n, t in metric_rules),
                           key=lambda r: r["alert"])
            extras.append({
                "apiVersion": "monitoring.coreos.com/v1", "kind": "PrometheusRule",
                "metadata": {"name": f"{AGENTS_PROJECT}-metric-triggers",
                             "namespace": plane_ns},
                "spec": {"groups": [{"name": "orbit.agents.metric", "rules": rules}]},
            })

        # logs triggers: the SAME rule body, but it is Loki's ruler — not Prometheus — that
        # evaluates a LogQL `expr` and fires the alert to Alertmanager. The ruler reads its
        # rule files from a local directory, so the rule ships as one ConfigMap in the loki
        # namespace and the INSTANCE mounts that ConfigMap into the ruler's local rule-store
        # dir (/etc/loki/rules/fake). The chart's rules sidecar is NOT involved — it was
        # disabled by a security audit — so the `loki_rule` label on the ConfigMap is
        # vestigial (harmless, the sidecar's old discovery label). The ConfigMap is NOT the
        # whole wiring, which is why this block is reached only when instance/config.yaml
        # declares `lokiRuler: wired`: the ruler also needs `rulerConfig.alertmanager_url`
        # (without it a firing rule notifies nobody) and local rule storage pointed at that
        # directory (the stock config uses the S3 object store, so the mounted rule is read
        # by no one). The gate is upstream, in _metric_logs_trigger, so an unwired instance
        # never renders a live route in front of a rule that cannot fire.
        if logs_rules:
            rules = sorted((_trigger_rule(n, t) for n, t in logs_rules),
                           key=lambda r: r["alert"])
            group_doc = yaml.safe_dump({"groups": [{"name": "orbit.agents.logs",
                                                    "rules": rules}]},
                                       sort_keys=True, default_flow_style=False)
            extras.append({
                "apiVersion": "v1", "kind": "ConfigMap",
                "metadata": {"name": f"{AGENTS_PROJECT}-logs-triggers",
                             "namespace": LOKI_RULE_NAMESPACE,
                             "labels": {LOKI_RULE_LABEL: "1"}},
                "data": {"orbit-agents-logs.yaml": group_doc},
            })

        # >= every routed agent's own task timeout, so a warm (synchronous) dispatch never
        # times out before the agent finishes (finding #1). max + grace, and the app.py
        # default is the agent default + the same grace, so intake < agent is impossible.
        dispatch_timeout = max(e["timeout"] for e in routed) + INTAKE_DISPATCH_GRACE_S
        env: dict = {
            "OTEL_EXPORTER_OTLP_ENDPOINT": OTLP_ENDPOINT,
            "INTAKE_ROUTES_FILE": f"{INTAKE_ROUTES_DIR}/{INTAKE_ROUTES_KEY}",
            "INTAKE_DISPATCH_TIMEOUT_S": str(dispatch_timeout),
        }
        for key in sorted(INTAKE_SECRET_KEYS):
            env[key] = {"secret": INTAKE_SECRET, "key": key}
        # The per-agent bearers the router presents when it calls a WARM agent. One env
        # var per routed agent, read from the SAME Secret the agent itself reads — no
        # second copy of the credential, and revoking one agent's bearer revokes the
        # router's ability to call it in the same edit. A job-dispatch agent gets no entry
        # here: its bearer goes into the Job's env as a secretKeyRef the KUBELET resolves,
        # so the intake process never holds it.
        for entry in routed:
            if entry["dispatch"] == "service":
                env["INTAKE_AGENT_TOKEN_" + entry["name"].upper().replace("-", "_")] = {
                    "secret": entry["token_secret"], "key": entry["token_key"]}
        components.append({
            "name": INTAKE_COMPONENT,
            "kind": "web",
            "class": "background",
            "port": INTAKE_PORT,
            "probePath": INTAKE_HEALTH,
            # Scrape the dispatch-outcome counter (NEW-3). Same port as the app, so the
            # chart renders one ServiceMonitor endpoint; the monitoring->intake NetworkPolicy
            # extra below admits Prometheus (in `monitoring`) to reach it.
            "metrics": {"port": INTAKE_PORT, "path": INTAKE_METRICS_PATH},
            # The instance pin, exactly like the runtime image: there is NO per-agent and
            # no per-catalog override for it. The intake holds every warm agent's bearer
            # and can create Jobs in the namespace that mounts the subscription token, so
            # a catalog-chosen image here would be a catalog-chosen router.
            "image": i_image,
            "tag": i_tag,
            "memory": INTAKE_MEMORY,
            "securityContext": {"runAsUser": INTAKE_UID, "runAsGroup": INTAKE_UID},
            # Nothing is written at runtime: the routing config is a read-only mount and
            # the process keeps its dedup set in memory.
            "readOnlyRootFilesystem": True,
            "env": env,
            "configMap": {"name": cm_name, "mountPath": INTAKE_ROUTES_DIR,
                          "items": [{"key": INTAKE_ROUTES_KEY, "path": INTAKE_ROUTES_KEY}]},
            # The routing table's content hash, on the POD TEMPLATE (see routes_checksum
            # above). Without it a catalog change that alters routes.json updates the
            # ConfigMap and leaves the running router serving the old table.
            "configChecksum": routes_checksum,
        })
        if any(e["dispatch"] == "job" for e in routed):
            # The ONLY Kubernetes API right anything on this plane holds: create and get
            # Jobs, in this namespace, for this one component. Passed as a GENERATOR-BUILT
            # chart value rather than a component key, because component dicts pass through
            # from the catalog verbatim — a component key would let any hand-written
            # catalog project mint itself a Role. main() merges this from a local dict no
            # catalog entry can address, exactly like the skills ConfigMaps.
            gen_values["rbac"] = {"jobRunner": INTAKE_COMPONENT}

    plane = {
        "id": AGENTS_PROJECT,
        "name": "Orbit agents",
        "hosted": "self",
        # No hostname, no Ingress, no certificate, no probe. An agent that answers the
        # public internet is one bearer guess away from spending the org's subscription.
        "environments": {AGENTS_ENV: {"expose": False}},
        "components": components,
        "secrets": secrets,
    }
    # The skills / routing ConfigMaps and the intake's RBAC directive travel with the
    # plane's Application but are NOT stored on the project dict — they are returned
    # separately and keyed by (id, env) in main(), where catalog data cannot reach them.
    return plane, extras, gen_values


def check_reserved_secrets(projects) -> None:
    """Refuse any NON-plane project that names an agent-plane credential.

    The sealed plane Secret is namespaced, but a tenant that names key
    CLAUDE_CODE_OAUTH_TOKEN — or any AGENT_*_TOKEN bearer — in its own Secret
    would still mount the crown jewel into its own namespace. The catalog names
    remote keys under `secrets:`, so this is where a tenant would reach for it,
    and this is where it is refused. (The plane itself is synthetic and appended
    after this check; it is the ONLY thing allowed to name these.)
    """
    for p in projects:
        pid = p.get("id")
        for s in (p.get("secrets") or []):
            if not isinstance(s, dict):
                continue
            sname = str(s.get("name", ""))
            if RESERVED_SECRET_NAME_RE.fullmatch(sname):
                sys.exit(f"project {_r(pid)} declares a Secret named {_r(sname)}, which is "
                         f"reserved for the agent plane. Only agents (the catalog "
                         f"`agents:` block) may name the plane's credentials.")
            for k in (s.get("keys") or []):
                # A non-string entry (e.g. `keys: [{CLAUDE_CODE_OAUTH_TOKEN: x}]`) would
                # otherwise be matched as str(dict) — which never hits the reserved
                # pattern and so slips this guard, only for the chart schema to catch it
                # downstream. Reject it HERE so the reserved check reasons about the real
                # value, not its repr.
                if not isinstance(k, str):
                    sys.exit(f"project {_r(pid)}: Secret {_r(sname)} has a non-string entry "
                             f"in `keys` ({_r(k)}); a remote key must be a plain string")
                if RESERVED_SECRET_KEY_RE.fullmatch(k):
                    sys.exit(f"project {_r(pid)} names remote key {_r(k)} on Secret "
                             f"{_r(sname)}. That key holds an agent-plane credential (the "
                             f"org subscription token or an agent bearer); the shared "
                             f"vault store is unscoped, so reading it from a tenant "
                             f"namespace is exfiltration. Refused.")


def guard_writable(docs, where: str) -> None:
    """Every document written under clusters/apps/ must be a kind the generator is
    allowed to emit. Argo applies that directory with the app-controller's cluster-wide
    rights, so a ClusterRoleBinding or a privileged Pod that reached it would run with
    them. This is the backstop under the closed key sets: whatever path produced a
    document, if its kind is not on the allowlist it does not get written."""
    for d in docs:
        if not isinstance(d, dict):
            sys.exit(f"{where}: refusing to write a non-object manifest")
        kind = d.get("kind")
        if kind not in WRITABLE_KINDS:
            sys.exit(f"{where}: refusing to write a {_r(kind)} manifest — only "
                     f"{sorted(WRITABLE_KINDS)} may be generated into clusters/apps/. "
                     f"A kind outside that set is an injection or a bug, and this "
                     f"directory is applied with cluster-wide rights.")


def guard_namespaces(docs, allowed: set[str], where: str) -> None:
    """Every generated document must land in a namespace this generator is allowed to
    write to. The companion of guard_writable: that one bounds WHAT is written, this one
    bounds WHERE. Until the `logs` trigger arrived, every generated object went to the
    project's own namespace or to `monitoring` (dashboards) — the loki ConfigMap was the
    first write to a namespace the catalog never named, and nothing checked. An allowlist
    keeps that a deliberate, enumerated act: a manifest addressed at `kube-system` (or at
    another tenant) is refused before the file exists rather than discovered in Argo.

    A namespaced kind with NO namespace is refused too — it would land wherever the
    applier's context points, which for the app-controller is `argocd`.
    """
    for d in docs:
        meta = d.get("metadata") or {}
        if d.get("kind") == "Namespace":          # cluster-scoped: its NAME is the target
            target = meta.get("name")
        else:
            target = meta.get("namespace")
        if target not in allowed:
            sys.exit(f"{where}: refusing to write {_r(d.get('kind'))} "
                     f"{_r(meta.get('name'))} into namespace {_r(target)} — generated "
                     f"manifests may only target {sorted(allowed)}. A namespace outside "
                     f"that set is an injection or a bug: this directory is applied with "
                     f"the app-controller's cluster-wide rights, so a manifest can reach "
                     f"any namespace on the cluster.")


def application(pid: str, env: str, values: dict) -> dict:
    ns = f"{pid}-{env}"
    return {
        "apiVersion": "argoproj.io/v1alpha1",
        "kind": "Application",
        "metadata": {"name": ns, "namespace": "argocd",
                     "annotations": {"argocd.argoproj.io/sync-wave": "10"}},
        "spec": {
            "project": "default",
            "source": {"repoURL": instance()["git"]["repoUrl"],
                       "targetRevision": "main",
                       "path": "charts/service", "helm": {"valuesObject": values}},
            "destination": {"server": "https://kubernetes.default.svc", "namespace": ns},
            "syncPolicy": {
                "automated": {"prune": True, "selfHeal": True},
                "syncOptions": ["CreateNamespace=false", "ServerSideApply=true"],
            },
        },
    }


def door_path(components: list) -> str:
    """The path a door check requests on a project's hostnames: "" (the root) when some web
    component serves `/` (the chart's default path), otherwise the first web component's
    path plus its probePath. A project whose only route is `/api` has no route at `/`, so a
    check of the bare host met Traefik's 404 — on the protected dev door too, since no
    router (and so no basic-auth middleware) matches `/` (docs/V1.md §8, checkers-lab).
    The ingress forwards the prefix unchanged and the app contract is to accept it
    (orbit-develop), so `<path><probePath>` is a URL the app answers."""
    web = [c for c in components or []
           if isinstance(c, dict) and c.get("kind", "web") == "web" and c.get("port")]
    if not web or any((c.get("path") or "/") == "/" for c in web):
        return ""
    first = web[0]
    return str(first.get("path")).rstrip("/") + str(first.get("probePath") or "")


def tenant_probe(pid: str, env: str, hosts: list[str], protected: bool,
                 name: str, path: str = "") -> dict:
    """The public-door check for one rendered tenant environment.

    `hosts` is the exact env_hostnames() result already used by the chart values.  In
    particular, an empty list means there is no public door and therefore no Probe.
    A protected non-prod door returning 401 proves only that the auth boundary is
    closed; authenticated application liveness is deliberately outside this check.
    """
    return {
        "apiVersion": "monitoring.coreos.com/v1",
        "kind": "Probe",
        "metadata": {
            "name": name,
            "namespace": DASHBOARD_NAMESPACE,
            "labels": {
                "orbit.local/project": pid,
                "orbit.local/env": env,
                "orbit.local/check": "protected-door" if protected else "public-surface",
            },
        },
        "spec": {
            "jobName": name,
            "prober": {"url": "blackbox-exporter.monitoring.svc:9115"},
            "module": "http_401" if protected else "http_2xx",
            "targets": {"staticConfig": {
                "static": [f"https://{host}{path}" for host in hosts],
            }},
        },
    }


def public_ingress_hosts(values: dict) -> list[str]:
    """Return only hosts for which the service chart renders a usable backend.

    The chart emits a rule for every environment hostname, but a rule has paths only
    when an environment-selected web component has a port and either has no `hosts`
    restriction or explicitly includes that host.  A path-less rule is not a public
    application surface and must not become a blackbox target.
    """
    web_components = [
        component for component in values["components"]
        if component.get("kind", "web") == "web" and component.get("port")
    ]
    return [
        host for host in values["hostnames"]
        if any(not component.get("hosts") or host in component["hosts"]
               for component in web_components)
    ]


def main(probe_digest=_sha256_hexdigest) -> int:
    cat = yaml.safe_load(CATALOG.read_text())
    if not isinstance(cat, dict):
        sys.exit("catalog/projects.yaml must be a mapping with a `projects:` list")
    # Single authority: domains, zones and registry come from instance/config.yaml.
    # A catalog that still declares them is a config error, not an override — two
    # sources of the same fact is how the split leaks instance strings back into core.
    if "defaults" in cat or "dnsZones" in cat:
        sys.exit("catalog/projects.yaml declares defaults/dnsZones — these moved to "
                 "instance/config.yaml at extraction (2026-08-25); remove them")
    inst = instance()
    defaults = {"domain": inst["domains"]["primary"],
                "devDomain": inst["domains"]["dev"],
                "registry": inst["registry"]}
    zones = inst["domains"]["dnsZones"]
    # Tags are CI-written and live apart from the hand-edited catalog, so a deploy shows
    # up as one line in catalog/tags.yaml rather than as noise in projects.yaml.
    tags_doc = yaml.safe_load(TAGS.read_text()) if TAGS.is_file() else {}
    retired = retired_archives()
    check_catalog_shape(cat, inst, tags_doc or {})
    tags = (tags_doc or {}).get("tags") or {}
    apply_pairs = orbitd_apply_pairs(inst)
    dashboards_on = feature_on(inst, "dashboards")
    uptime_on = feature_on(inst, "uptime")
    databases_on = feature_on(inst, "databases")
    backups_on = feature_on(inst, "backups")
    secrets_plane(inst)  # validates the provider; sops is the only plane
    APPS_DIR.mkdir(parents=True, exist_ok=True)
    if apply_pairs:
        ORBIT_DIR.mkdir(parents=True, exist_ok=True)

    # The agent plane (Phase D.2). `agents:` is a sibling of `projects:` in the catalog
    # and renders as one synthetic project, so agents get the same namespace, quota,
    # NetworkPolicy, pull secret and Instrumentation as every tenant workload — and the
    # same checks. The id is reserved: a tenant project claiming it would have its
    # manifests overwritten by whichever of the two was written last.
    projects = list(cat["projects"])
    for p in projects:
        if p.get("id") == AGENTS_PROJECT:
            sys.exit(f"project id {_r(AGENTS_PROJECT)} is reserved for the agent plane — "
                     f"declare agents under the catalog's `agents:` key instead")
        if p.get("id") == PLATFORM_PROJECT:
            sys.exit(f"project id {_r(PLATFORM_PROJECT)} is reserved for the platform layer "
                     f"(orbit platform apply) — choose another id")
        if "_extra" in p:
            # `_extra` was a generator-internal channel; a catalog project has no business
            # naming it. It is refused rather than ignored so the closed-namespace
            # invariant is self-announcing: no catalog field feeds the arbitrary-manifest
            # path (that path now lives in a local dict main() keys, below).
            sys.exit(f"project {_r(p.get('id'))} declares `_extra`, which is not a catalog "
                     f"field — generated manifests are not authored in the catalog")
    # Refuse a tenant that names an agent-plane credential BEFORE the synthetic plane is
    # appended (the plane is the only thing allowed to name them).
    check_reserved_secrets(projects)
    # (id, env) -> generator-BUILT manifests that travel with that Application. A local
    # dict, deliberately not addressable from any catalog entry: the render loop reads
    # extras from HERE, never off the project dict.
    gen_extras: dict[tuple[str, str], list[dict]] = {}
    # (id, env) -> generator-BUILT chart values, merged over the values assembled from the
    # catalog. Local for the same reason gen_extras is: `rbac` mints a Role, and a catalog
    # project must have no way to name it. main() builds `values` from an explicit key
    # list, so nothing under `projects:` can reach a root value either way — this is the
    # channel that stays closed as that list grows.
    gen_values: dict[tuple[str, str], dict] = {}
    plane = agents_project(cat.get("agents"), inst)
    if plane:
        plane_project, plane_extras, plane_values = plane
        projects.append(plane_project)
        gen_extras[(AGENTS_PROJECT, AGENTS_ENV)] = plane_extras
        gen_values[(AGENTS_PROJECT, AGENTS_ENV)] = plane_values

    written: set[pathlib.Path] = set()
    outputs: dict[pathlib.Path, str] = {}
    identity_claims = GeneratedIdentityClaims()

    def stage(path: pathlib.Path, content: str, source: tuple[str, ...], docs) -> None:
        """Register and buffer an output; nothing reaches disk until render succeeds."""
        relative = str(path.relative_to(ROOT))
        identity_claims.claim(generated_path_identity(relative), relative, source)
        for ordinal, doc in enumerate(docs):
            meta = doc.get("metadata") or {}
            name = meta.get("name")
            if name:
                namespace = meta.get("namespace", "<cluster-scoped>")
                scope = (f"Kubernetes {doc.get('apiVersion')}/{doc.get('kind')} identity "
                         f"in {namespace}")
                identity_claims.claim(scope, str(name),
                                      source + (f"document {ordinal}",))
        outputs[path] = content
        written.add(path)
    namespaces: list[dict] = []
    cards: list[dict] = []
    seen_ids: set[str] = set()
    seen_ns: dict[str, str] = {}
    host_owner: dict[str, str] = {}
    platform_hosts = {f"{label}.{defaults['domain']}" for label in PLATFORM_HOST_LABELS}

    # Externally-hosted projects reserve their hostnames in a FIRST PASS, before any
    # self-hosted project claims one. Registering them inline made the collision check
    # order-dependent: with the external project listed second, the self-hosted project had
    # already claimed the host and the external `setdefault` was a silent no-op, so a
    # cross-project hijack passed validation purely because of ordering in the YAML file.
    for p in projects:
        if p.get("hosted", "self") != "self" or p.get("managed_elsewhere"):
            for env, envcfg in (p.get("environments") or {}).items():
                for h in env_hostnames(p["id"], env, envcfg or {}, defaults):
                    host_owner.setdefault(h, p["id"])

    for p in projects:
        pid = p["id"]
        if not ID_RE.fullmatch(str(pid)):
            sys.exit(f"invalid project id {_r(pid)}: must match {ID_RE.pattern}")
        # `dashboard-` is reserved as a filename prefix: the generated files are
        # <pid>-<env>.yaml and dashboard-<pid>-<env>.yaml, so a project id starting with
        # `dashboard-` makes its Application file collide with another project's dashboard
        # file — one silently overwrites the other and an Application vanishes. Now that
        # orbit-agents/prod is a documented render target, this latent collision is a
        # refusal.
        if str(pid).startswith("dashboard-"):
            sys.exit(f"project id {_r(pid)} may not start with 'dashboard-': that prefix is "
                     f"reserved for generated dashboard files and the two would collide")
        if pid in seen_ids:
            sys.exit(f"duplicate project id {_r(pid)}")
        seen_ids.add(pid)

        # --- hub card: every project, self-hosted or not. External services still get a
        # card and an uptime probe, which is the point of a catalog rather than a
        # deployment manifest.
        if p.get("showcase") or p.get("internal"):
            first_env = (p.get("environments") or {}).get("prod") or {}
            url = p.get("url")
            if not url and p.get("hosted") == "self":
                # An unexposed environment yields no hostnames, so there is no URL to
                # link. A card without one is honest; indexing [0] here would crash the
                # generator on the first internal-only service anyone showcases.
                _h = env_hostnames(pid, "prod", first_env, defaults)
                url = "https://" + _h[0] if _h else None
            cards.append({
                "id": pid, "name": p["name"], "blurb": p.get("blurb", ""),
                "tags": sorted(p.get("tags") or []), "url": url,
                "repo": p.get("repo"), "hosted": p.get("hosted", "self"),
                "internal": bool(p.get("internal")), "showcase": bool(p.get("showcase")),
                "probeHosts": sorted(env_hostnames(pid, "prod", first_env, defaults)) if first_env else [],
            })

        if p.get("hosted", "self") != "self" or p.get("managed_elsewhere"):
            continue  # hostnames already reserved in the first pass above
        if not (p.get("components") or []):
            sys.exit(f"project {_r(pid)} is hosted: self but declares no components — "
                     f"either add components or leave it hosted: external until it has some")
        check_component_environments(p)
        check_component_overrides(p)
        check_database_parameters(p)

        for env, envcfg in sorted((p.get("environments") or {}).items()):
            envcfg = envcfg or {}
            if env != "prod" and not envcfg.get("enabled"):
                continue

            if not ID_RE.fullmatch(str(env)):
                sys.exit(f"invalid environment name {_r(env)} in {_r(pid)}: must match {ID_RE.pattern}")
            # The identity that actually collides is the derived one. Project "a" with an
            # environment "b-prod" and project "a-b" with "prod" both resolve to namespace
            # "a-b-prod" — one silently overwrote the other's manifest and emitted a
            # duplicate Namespace, which makes Argo reject the whole services app.
            ns_name = f"{pid}-{env}"
            if len(ns_name) > MAX_NS:
                sys.exit(f"derived namespace {_r(ns_name)} is {len(ns_name)} characters; "
                         f"the limit is {MAX_NS}. Shorten the project id or the env name.")
            if ns_name in seen_ns:
                sys.exit(f"namespace collision: {_r(pid)}/{_r(env)} and {seen_ns[ns_name]} both resolve to {_r(ns_name)}")
            seen_ns[ns_name] = f"{_r(pid)}/{_r(env)}"

            # Normalise the hostnames THEMSELVES, not a local copy used only for the
            # collision map. Previously `h = h.lower().rstrip(".")` rebound the loop
            # variable, so the mixed-case host was compared in normalised form but rendered
            # verbatim into the Ingress, where the API server rejects it as not a DNS1123
            # subdomain — a generator success that cannot be applied.
            hosts = env_hostnames(pid, env, envcfg, defaults)
            named = {str(x).lower().rstrip(".") for x in envcfg.get("hostnames") or []}
            for h in hosts:
                if "*" in h:
                    # A wildcard host renders a valid Ingress covered by the platform
                    # TLSStore, silently claiming every unrouted subdomain.
                    sys.exit(f"wildcard hostname {_r(h, 'hostname')} in {_r(pid)} is not allowed")
                if not HOST_RE.fullmatch(h):
                    sys.exit(f"invalid hostname {_r(h, 'hostname')} in {_r(pid)}/{_r(env)}: not a DNS name")
                # The platform's own doors (Grafana, orbitd, the MCP door, intake) live
                # under the primary domain too. A project that named one was accepted
                # and got an Ingress rule for it in its own namespace (QA of v1.0.0, F24).
                if h in platform_hosts:
                    sys.exit(f"hostname {_r(h, 'hostname')} in {_r(pid)}/{_r(env)} is one of the platform's own "
                             f"({', '.join(sorted(platform_hosts))}): a project cannot take it")
                # `hostnames` are names under the instance's own domains. A domain of the
                # project's own is a `customDomains` entry (its certificate is ordered per
                # host); written here it was accepted, and its order then failed forever.
                if h in named and not registrable(h, [defaults["domain"], defaults["devDomain"], *zones]):
                    sys.exit(f"hostname {_r(h, 'hostname')} in {_r(pid)}/{_r(env)} is outside this instance's domains "
                             f"({_r(defaults['domain'], 'hostname')}, {_r(defaults['devDomain'], 'hostname')}): "
                             f"a domain of your own goes under `customDomains`, not `hostnames`")
                # Keyed by project AND environment. Keyed on the project alone, one
                # project's prod and dev could claim the same host and render two Ingresses
                # for it — one protected by basic-auth and one not — with Traefik picking a
                # winner by load order.
                owner = host_owner.setdefault(h, f"{pid}/{env}")
                if owner != f"{pid}/{env}" and owner != pid:
                    # Two projects rendering an Ingress for one host is a silent hijack:
                    # whichever Traefik loads last wins, with no error anywhere.
                    # (The owner is `project` or `project/env`: shown part by part, since
                    # the pair is not a name's shape and was replaced by a description.)
                    shown = "/".join(_r(part) for part in owner.split("/"))
                    sys.exit(f"hostname {_r(h, 'hostname')} claimed by both {shown} and {_r(pid)}/{_r(env)}")
            # An environment whose every component was filtered out renders an
            # Application with an empty workload list: Argo reports it Synced and Healthy
            # because there is nothing to be unhealthy, and the environment looks deployed.
            if not components_for(p, env):
                sys.exit(f"{_r(pid)} environment {_r(env)} has no components — every component "
                         f"excludes it via `environments:`")
            values = {
                "project": pid,
                "env": env,
                "hostnames": sorted(hosts),
                "tlsGroups": tls_groups(pid, env, hosts, defaults, zones),
                # `repo` and `environments` are stripped: both are generator directives.
                # `repo` says which repository BUILDS a component, which the dispatch
                # authorization check needs and the chart has no use for; leaving it in put
                # build-time metadata into the deployed manifest, where a reader would
                # reasonably assume the chart did something with it. `environments` has
                # already been consumed by the filter above.
                # A component with no tag yet (nothing shipped, none in the catalog) carries
                # no `tag` at all: the chart then idles it (zero replicas, a suspended
                # CronJob) until the first ship. Writing "latest" here made every such pod
                # pull an image an Orbit registry never has (docs/V1.md §8).
                "components": [
                    {k: v for k, v in {**c, "tag": tags.get(f"{pid}/{env}/{c['name']}", c.get("tag"))}.items()
                     if k not in ("repo", "environments") and not (k == "tag" and v in (None, ""))}
                    for c in components_for(p, env)
                ],
                # Non-prod is protected by default: a dev URL is a public URL, and
                # half-finished software on the open internet is scanner food.
                "protected": env != "prod",
            }
            # A database is declared once for the project and rendered per environment, so
            # dev and prod each get their own Cluster and their own generated credentials.
            # Sharing one would make a dev deploy able to write prod's data, which is
            # exactly the boundary the per-environment secret plane exists to hold
            # everywhere else.
            if p.get("database"):
                if not databases_on:
                    sys.exit(f"{_r(pid)} declares a database and the databases feature is off: there is "
                             f"no operator to run it. Turn databases on (`orbit features enable "
                             f"databases`), or remove `database:` from the project.")
                values["database"] = database_values(p, retired.get(pid, 0))
            # Declared Secrets pass through to the chart, which declares them for
            # its render guard and creates none. The catalog names keys, never
            # values — same contract as env. Copied entry by entry so the marking
            # below never mutates the catalog's own list in place.
            if p.get("secrets"):
                values["secrets"] = [dict(s) for s in p["secrets"]]
            # Prod databases WAL-archive to the instance's platform bucket when the
            # instance declares one. Prod only: dev data is disposable by design, and
            # archiving it would seal the bucket credential into the dev Secret for
            # nothing. The credential rides the chart's ordinary secrets path —
            # orbit-s3 is declared like any other, and the sops plane seals it.
            obj = inst.get("objectStorage") or {}
            # ...and only while the backups feature is on: off, a prod database is backed up
            # like any other, by the nightly dump the chart renders when there is no `s3`.
            if p.get("database") and env == "prod" and backups_on and (obj.get("buckets") or {}).get("platform"):
                if not obj.get("endpoint"):
                    sys.exit(f"objectStorage declares a platform bucket but no endpoint — "
                             f"{pid}'s database cannot WAL-archive to an unaddressable bucket")
                values["s3"] = {"bucket": obj["buckets"]["platform"],
                                "endpoint": obj["endpoint"]}
                # the chart would refuse a bucket or endpoint of the wrong shape at render
                # time, on a commit already made: refuse first, naming the field
                _check_against_schema("objectStorage", "s3", values["s3"], _chart_schema()["properties"]["s3"])
                ret = obj.get("retention")
                if ret is not None:
                    if not re.fullmatch(r"[1-9][0-9]*[dwm]", str(ret)):
                        sys.exit(f"objectStorage.retention '{_s(ret)}' is not <n>d/<n>w/<n>m — "
                                 f"CNPG would reject it at apply time, after a clean render")
                    values["s3"]["retention"] = str(ret)
                # The key names are the ones the platform's own copies of orbit-s3 carry
                # (loki, tempo, orbit), so a project's copy is a COPY: `orbit secrets copy
                # --file clusters/platform/secrets/orbit/orbit-s3.yaml --namespace <ns>`, no
                # value read. Until 2026-10-09 the database asked for the same two values
                # under other names, which nothing but a hand with the raw keys could make,
                # and a copy made the obvious way passed every check and archived nothing
                # (docs/V1.md §8).
                # A project may declare orbit-s3 itself (an app wanting bucket access);
                # appending a second copy would declare the same Secret twice, and
                # Argo rejects the whole Application over it.
                if not any(s.get("name") == "orbit-s3" for s in values.get("secrets") or []):
                    values["secrets"] = list(values.get("secrets") or []) + [
                        {"name": "orbit-s3",
                         "keys": ["access-key", "secret-key"]}]
            # The sops plane supplies Secrets from SopsSecrets: every declared entry
            # (the appended bucket credential included) is marked `from: external`,
            # so the chart declares each Secret and creates none. A declared source
            # is never rewritten under the org: anything but the two accepted
            # values is refused, naming the value.
            if values.get("secrets"):
                for entry in values["secrets"]:
                    source = entry.get("from", "external")
                    if source not in ("sops", "external"):
                        sys.exit(f"{_r(pid)}/{_r(env)} declares Secret {_r(entry.get('name'))} "
                                 f"with from: {_r(source)} — that source is retired. "
                                 f"Accepted values are 'sops' and 'external'.")
                    entry["from"] = "external"
            # Every pod presents the shared registry credential. Unconditional on
            # purpose: presenting it for a public image is a no-op, and making it
            # conditional on "is this image private" would mean the generator has an
            # opinion it cannot verify — visibility lives at GHCR and changes without a
            # catalog edit. The Secret itself is produced per namespace below.
            values["imagePullSecret"] = "ghcr-pull"
            # Generator-BUILT chart values (the intake's `rbac.jobRunner`), merged LAST so
            # they are unmistakably the generator's. Read from the local gen_values dict
            # keyed by (id, env), never off the project dict.
            values.update(gen_values.get((pid, env)) or {})
            # Scratch is passed through unchanged (the chart renders it) but refused here
            # with the same two messages the chart fails with, so `make check` dies before
            # helm. `drain:` is an integer on the component dict and rides the copy above.
            for comp in values["components"]:
                check_component_scratch(comp)
            # Per-component egress is validated here, at generation, so a catalog that
            # names an address nobody declared dies in `make check` rather than
            # rendering a lockdown that is really an open pod. The declared aliases
            # ride along to the namespaces render below, which mints the policies.
            targets = egress_targets(inst)
            narrowed = {}
            for comp in values["components"]:
                aliases = check_component_egress(
                    pid, env, comp, targets,
                    has_database=bool(p.get("database")))
                if aliases is not None:
                    narrowed[comp["name"]] = aliases
            # Generator-BUILT manifests that travel with the app (the agent plane's
            # skills ConfigMaps). Read from the local gen_extras dict keyed by (id, env),
            # NEVER off the project dict — a catalog project cannot address this channel.
            extra = list(gen_extras.get((pid, env)) or [])
            for comp in values["components"]:
                cm = comp.get("configMap")
                if cm and (ROOT / "hub" / "dist" / "index.html").exists():
                    extra.append({
                        "apiVersion": "v1", "kind": "ConfigMap",
                        "metadata": {"name": cm["name"], "namespace": f"{pid}-{env}"},
                        "data": {"index.html": (ROOT / "hub" / "dist" / "index.html").read_text()},
                    })

            dest_dir = ORBIT_DIR if f"{pid}/{env}" in apply_pairs else APPS_DIR
            dest_dir.mkdir(parents=True, exist_ok=True)
            path = dest_dir / f"{pid}-{env}.yaml"
            app_docs = [application(pid, env, values)] + extra
            # Nothing but the allowlisted kinds may reach clusters/apps/ — this directory
            # is applied with the app-controller's cluster-wide rights.
            guard_writable(app_docs, str(path.relative_to(ROOT)))
            # WHERE, as well as what. The plane is the only project allowed to address
            # `loki` (its `logs`-trigger rule ConfigMap); a tenant app file may not.
            allowed_ns = {f"{pid}-{env}", "argocd"}
            if pid == AGENTS_PROJECT:
                allowed_ns.add(LOKI_RULE_NAMESPACE)
            guard_namespaces(app_docs, allowed_ns, str(path.relative_to(ROOT)))
            stage(path, BANNER + yaml.safe_dump_all(
                app_docs, sort_keys=True, default_flow_style=False),
                (str(pid), str(env), "application"), app_docs)

            # Only while the dashboards feature is on: off, there is no Grafana to load it.
            if dashboards_on:
                # The project's generated RED dashboard (Decision 6): same sync, same
                # prune discipline as the Application it describes.
                dpath = APPS_DIR / f"dashboard-{pid}-{env}.yaml"
                # `probe_digest` is the render's one digest seam (the collision regression
                # injects through it); the dashboard uid shares it
                identity_claims.claim("Grafana dashboard uid", dashboard_uid(pid, env, probe_digest),
                                      (str(pid), str(env), "dashboard"))
                dash_doc = red_dashboard(pid, env, p, values.get("hostnames") or [],
                                         wal=bool(values.get("s3")), probes=uptime_on,
                                         digest_fn=probe_digest)
                guard_writable([dash_doc], str(dpath.relative_to(ROOT)))
                guard_namespaces([dash_doc], {DASHBOARD_NAMESPACE},
                                 str(dpath.relative_to(ROOT)))
                stage(dpath, BANNER + yaml.safe_dump(
                    dash_doc, sort_keys=True, default_flow_style=False),
                    (str(pid), str(env), "dashboard"), [dash_doc])

            # One Probe per environment with at least one usable public route. The host
            # set mirrors the chart's web/port/hosts routing predicate exactly; the chart
            # currently leaves empty rules for uncovered hosts, and those are deliberately
            # neither repaired here nor treated as probeable application surfaces.
            # A separate file leaves established Application/dashboard bytes untouched
            # and lets the ordinary written-set pruner remove the check when routes vanish.
            public_hosts = public_ingress_hosts(values)
            # only while the uptime feature is on: off, there is no prober to run the check
            if public_hosts and uptime_on:
                probe_name, probe_filename = generated_probe_identity(
                    pid, env, "tenant-http", identity_claims, probe_digest)
                ppath = APPS_DIR / probe_filename
                probe_doc = tenant_probe(pid, env, public_hosts, values["protected"],
                                         probe_name, path=door_path(values["components"]))
                guard_writable([probe_doc], str(ppath.relative_to(ROOT)))
                guard_namespaces([probe_doc], {DASHBOARD_NAMESPACE},
                                 str(ppath.relative_to(ROOT)))
                stage(ppath, BANNER + yaml.safe_dump(
                    probe_doc, sort_keys=True, default_flow_style=False),
                    (str(pid), str(env), "tenant-http"), [probe_doc])

            namespaces.append({"name": f"{pid}-{env}", "project": pid, "env": env,
                               "components": values["components"],
                               "database": values.get("database"),
                               "wal": bool(values.get("s3")) and bool(values.get("database")),
                               "narrowed": narrowed, "targets": targets})

    # --- namespaces + quotas + pull secret placeholders -----------------------
    docs = []
    traces_on = feature_on(inst, "traces")
    for ns in sorted(namespaces, key=lambda n: n["name"]):
        docs.append({"apiVersion": "v1", "kind": "Namespace",
                     "metadata": {"name": ns["name"],
                                  "labels": {"orbit.local/project": ns["project"],
                                             "orbit.local/env": ns["env"],
                                             # The chart's hardened securityContext is
                                             # overridable per component, so admission is
                                             # the backstop that stops an override from
                                             # quietly running a workload as root.
                                             "pod-security.kubernetes.io/enforce": "restricted",
                                             "pod-security.kubernetes.io/warn": "restricted"}}})
        # A ceiling per project, so one runaway service cannot schedule fifty pods or
        # reserve the whole box. Sized from this environment's catalog — component
        # requests/limits, one rolling-update surge pod of the largest web component,
        # and the database's declared memory if the project has one — not a hard-coded
        # 3Gi. It is still a backstop, now fitted to what the catalog actually scheduled.
        docs.append({"apiVersion": "v1", "kind": "ResourceQuota",
                     "metadata": {"name": "orbit-quota", "namespace": ns["name"]},
                     "spec": {"hard": derived_quota(ns["components"], ns.get("database"),
                                                    wal=bool(ns.get("wal")))}})
        # The registry credential comes from a SopsSecret the desk applies per
        # namespace: one token (read:packages only) sealed once per env, presented
        # by every project namespace as the `ghcr-pull` imagePullSecret the chart
        # names. Nothing is rendered for it here.
        # The Instrumentation resource the OTel Operator's webhook resolves when a pod
        # carries an inject annotation ("true" means "the Instrumentation in my own
        # namespace"). One per project namespace so the reference can never dangle.
        # Traces only: the OTLP metrics/logs exporters are explicitly OFF — metrics ride
        # the Prometheus scrape path (Decision 5: two ingest paths make `job` mean two
        # different things), and logs ride stdout->Alloy. Without these envs every
        # injected SDK pushes metrics at Tempo, which drops them noisily forever.
        otel_off = [{"name": "OTEL_METRICS_EXPORTER", "value": "none"},
                    {"name": "OTEL_LOGS_EXPORTER", "value": "none"},
                    {"name": "OTEL_EXPORTER_OTLP_PROTOCOL", "value": "http/protobuf"}]
        # Log correlation: the python SDK stamps otelTraceID/otelSpanID onto every
        # logging record. Whether they reach STDOUT depends on the app's formatter —
        # that half of the logs<->trace link is the app contract (orbit-develop).
        py_env = list(otel_off) + [{"name": "OTEL_PYTHON_LOG_CORRELATION", "value": "true"}]
        # Only while the traces feature is on: off, the operator and its CRD are not on
        # the cluster, so the object could not be applied (and the inject annotation
        # the chart still stamps is read by nothing).
        if traces_on:
            init_resources = _instrumentation_resources()
            docs.append({"apiVersion": "opentelemetry.io/v1alpha1", "kind": "Instrumentation",
                         "metadata": {"name": "orbit", "namespace": ns["name"]},
                         "spec": {
                             "exporter": {"endpoint": OTLP_ENDPOINT},
                             "propagators": ["tracecontext", "baggage"],
                             "sampler": {"type": "parentbased_always_on"},
                             "python": _instrumentation_lang("python", py_env, init_resources),
                             "dotnet": _instrumentation_lang("dotnet", list(otel_off), init_resources),
                             "nodejs": _instrumentation_lang("nodejs", list(otel_off), init_resources),
                             "java": _instrumentation_lang("java", list(otel_off), init_resources),
                         }})
        # The htpasswd behind every non-prod environment's basic-auth Middleware comes
        # from a SopsSecret the desk applies per namespace — nothing is rendered for
        # it here, but the per-namespace rule still matters: it was hand-written for
        # the first dev environment back when it was the only one, and the SECOND dev
        # environment came up with a Middleware naming a Secret nothing created. Traefik disables a
        # router whose middleware is broken, so the symptom is a 404 on a URL that is
        # supposed to answer 401: the guard is not closed, it is absent, and the check that
        # says "protected" is answering about a route that does not exist.
        # The ingress admit list. A tenant namespace admits the platform namespaces it
        # actually needs plus its own pods; the AGENT PLANE admits ONLY its own pods.
        # That narrowing is not cosmetic: an agent pod holds the org's subscription token
        # and serves an unauthenticated agent card on /healthz, and the broad list would
        # let a Grafana editor in `monitoring` reach it through the datasource proxy. The
        # agent plane needs none of traefik (no Ingress), monitoring (no ServiceMonitor —
        # agents declare no metrics) or cnpg-system (no database) inbound; whatever wakes
        # an agent (the D.3 intake) is a neighbour and is covered by its own pods. The kubelet's
        # liveness/readiness probes are not subject to NetworkPolicy, so /healthz still
        # answers. Non-prod namespaces add one more ingress rule: pods in the orbitd
        # namespace (the name taken from that Namespace document, not a second copy here)
        # may reach the web ports the chart's Service publishes, which is the post-ship
        # web assertion's in-cluster URL; production is excluded because its check goes
        # through the public origin, not the Service.
        own_pods = {"podSelector": {}}  # a namespace reaching ITSELF; never omit this
        if ns["project"] == AGENTS_PROJECT:
            ingress_from = [own_pods]
        else:
            ingress_from = [
                {"namespaceSelector": {"matchLabels": {
                    "kubernetes.io/metadata.name": "traefik"}}},
                {"namespaceSelector": {"matchLabels": {
                    "kubernetes.io/metadata.name": "monitoring"}}},
                # A service's own pods. Without this the policy also denies a namespace's
                # traffic to ITSELF: an API pod could not open a connection to the
                # Postgres pod beside it, and the symptom is a connection timeout at
                # startup that reads like a bad password or a slow database rather than a
                # policy decision.
                own_pods,
                # CloudNativePG reaches each instance directly on the pod IP to read
                # status and run switchover; denied, the Cluster never leaves "Setting up
                # primary" and the operator log is the only place that says why.
                {"namespaceSelector": {"matchLabels": {
                    "kubernetes.io/metadata.name": "cnpg-system"}}},
            ]
        # Basic auth lives on the Traefik router, so without this any workload in the
        # cluster could read a "protected" dev surface straight from its Service IP.
        ingress_rules = [{"from": ingress_from}]
        if ns["env"] != "prod":
            web_ports = _web_ports(ns["components"])
            if web_ports:
                ingress_rules.append({
                    "from": [{"namespaceSelector": {"matchLabels": {
                        "kubernetes.io/metadata.name": _orbitd_namespace()}}}],
                    "ports": [{"protocol": "TCP", "port": p} for p in web_ports],
                })
        # Per-component egress narrows by REMOVAL, not addition: NetworkPolicies are
        # additive, so a per-component grant beside the open grant changes nothing
        # until the open grant stops selecting the narrowed pod. The chart stamps
        # orbit.local/egress=declared on exactly those pods, and the open grant
        # selects on that label being ABSENT — keyed on absence, never on a list of
        # component names, so pods carrying no component name at all (the database,
        # the backup Job) keep the open grant. A namespace with no narrowed
        # component keeps podSelector: {} byte-identical to before.
        narrowed = ns.get("narrowed") or {}
        open_selector = {}
        if narrowed:
            open_selector = {"matchExpressions": [
                {"key": EGRESS_LABEL, "operator": "DoesNotExist"}]}
        docs.append({"apiVersion": "networking.k8s.io/v1", "kind": "NetworkPolicy",
                     "metadata": {"name": "default-deny-ingress", "namespace": ns["name"]},
                     "spec": {"podSelector": open_selector,
                              "policyTypes": ["Ingress", "Egress"],
                              "ingress": ingress_rules,
                              # Egress is open to the internet but NOT to link-local:
                              # 169.254.169.254 is the Hetzner metadata service, which
                              # serves the node's build-time root password to any pod
                              # that asks. Ingress-only policy left that readable.
                              "egress": [
                                  {"to": [{"ipBlock": {"cidr": "0.0.0.0/0",
                                                       "except": ["169.254.0.0/16"]}}]},
                                  {"to": [{"namespaceSelector": {"matchLabels": {
                                       "kubernetes.io/metadata.name": "kube-system"}}}],
                                   "ports": [{"protocol": "UDP", "port": 53},
                                             {"protocol": "TCP", "port": 53}]},
                              ]}})
        # One egress policy per narrowed component, selecting that component's pods
        # alone: the component name plus the declared label the chart stamps. The
        # label is matched, never selected on by the open grant above, which is
        # what makes the narrowing real under the union rule.
        #
        # The policy also carries Ingress with the open grant's exact rules. The
        # narrowing removes the pod from the open grant's selector, and under
        # Kubernetes semantics a pod no Ingress-carrying policy selects accepts
        # connections from everywhere — so an Egress-only replacement silently
        # drops the inbound protection, first on the component holding the model
        # provider's key. Declaring an outbound allowlist is not a request to
        # change who may call in: the narrowed component admits exactly what its
        # unnarrowed neighbour admits.
        for comp_name in sorted(narrowed):
            docs.append({
                "apiVersion": "networking.k8s.io/v1", "kind": "NetworkPolicy",
                "metadata": {"name": f"egress-{comp_name}", "namespace": ns["name"]},
                "spec": {
                    "podSelector": {"matchLabels": {
                        "app.kubernetes.io/name": comp_name,
                        EGRESS_LABEL: _egress_label_value()}},
                    "policyTypes": ["Ingress", "Egress"],
                    "ingress": copy.deepcopy(ingress_rules),
                    "egress": component_egress_rules(
                        ns["project"], narrowed[comp_name], ns.get("targets") or {}),
                }})

        # orbitd's ConfigMap grant in this namespace (see WRITABLE_KINDS).
        docs.extend(applier_configmaps_rbac(ns["name"]))

    nspath = APPS_DIR / "namespaces.yaml"
    guard_writable(docs, str(nspath.relative_to(ROOT)))
    guard_namespaces(docs, {n["name"] for n in namespaces},
                     str(nspath.relative_to(ROOT)))
    stage(nspath, BANNER + yaml.safe_dump_all(
        docs, sort_keys=True, default_flow_style=False),
        ("catalog", "all", "namespaces"), docs)

    # --- hub content: REMOVED at extraction (2026-08-25). The hub came down with the
    # portfolio; the card data (`cards`) is still collected so the hub's return as an
    # ordinary tenant is a re-add of the write, not a re-derivation of the data.

    # A failed render leaves the prior tree untouched.  This matters most for an injected
    # digest collision: neither colliding tuple gets to overwrite the other's old path.
    for path in sorted(outputs):
        path.write_text(outputs[path])

    # --- prune anything the catalog no longer describes ------------------------
    for stale in sorted(APPS_DIR.glob("*.yaml")):
        if stale not in written:
            stale.unlink()
            print(f"pruned {stale.relative_to(ROOT)}")
    if ORBIT_DIR.is_dir():
        for stale in sorted(ORBIT_DIR.glob("*.yaml")):
            if stale not in written:
                stale.unlink()
                print(f"pruned {stale.relative_to(ROOT)}")

    for f in sorted(written):
        print(f"wrote {f.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
