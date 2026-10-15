"""Monitor files (`monitors/<name>.yaml`) rendered into the objects Prometheus, blackbox
and the Loki ruler already evaluate.

Pure: no cluster, no checkout — the caller reads the files and applies the objects.
A bad file is reported in `errors` and skipped; it never blocks the other monitors.

Metric and probe monitors render PrometheusRules (a probe adds the blackbox Probe
whose job its rule watches); log monitors share one Loki ruler ConfigMap, so a log
monitor with no wired ruler is refused rather than rendered where nothing would
evaluate it.
"""

from __future__ import annotations

import ipaddress
import math
import re
from copy import deepcopy
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlsplit

import yaml

# A Prometheus/Loki duration, VERBATIM from the PrometheusRule CRD's own `for` pattern
# (monitoring.coreos.com/v1, spec.groups[].rules[].for): ORDERED, each unit at most once.
# The looser `([0-9]+(ms|[smhdwy]))+` this replaced accepted `30m1h` and `5m5m5m` — which
# render green, ship, and are then REJECTED by the CRD's admission webhook, so the app
# sits unapplied with the render still green. The renderer refuses what the cluster will.
PROM_DURATION_RE = re.compile(
    r"0|(([0-9]+)y)?(([0-9]+)w)?(([0-9]+)d)?(([0-9]+)h)?(([0-9]+)m)?(([0-9]+)s)?(([0-9]+)ms)?")

# A monitor name: a DNS label, so it is safe in object names, alert names and label values.
# Anchored: the pattern is also a pydantic Field(pattern=...) constraint on the tool
# surface, where an unanchored pattern is a search and constrains nothing.
NAME_RE = re.compile(r"^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?$")

KINDS = ("metric", "log", "probe")
# Free-text fields ride into Prometheus rule annotations and a ConfigMap; a bound
# keeps one monitor file from carrying an object the API server refuses.
SUMMARY_MAX = 1024
QUERY_MAX = 4096
RUNBOOK_MAX = 2048
COMPARISONS = (">", ">=", "<", "<=", "==", "!=")
SEVERITIES = ("page", "warn", "info")
# The stock inhibition levels severity maps onto: page wakes someone, warn and info do not.
SEVERITY_MAP = {"page": "critical", "warn": "warning", "info": "info"}
NOTIFY_CHANNELS = ("telegram",)

MONITORING_NS = "monitoring"
LOKI_NS = "loki"
LOG_CONFIGMAP = "orbit-monitors-logs"
LOG_GROUP = "orbit.monitors.logs"
BLACKBOX_URL = "blackbox-exporter.monitoring.svc:9115"
PROBE_MODULE_DEFAULT = "http_2xx"

LABEL_MONITOR = "orbit.local/monitor"
LABEL_COMPONENT = "orbit.local/component"
COMPONENT = "monitors"

# Probe targets an agent may ask blackbox to fetch: syntactic policy only, no DNS.
# One canonical public form: an ASCII DNS name whose last label is alphabetic,
# or a canonical IP literal (dotted decimal without leading zeros, or bracketed
# IPv6), then the public-address rules. Anything else is refused.
_DNS_LABEL_RE = re.compile(r"[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?")
_TLD_RE = re.compile(r"[a-z]+")
_UPPER_RE = re.compile(r"[A-Z]")
_CANON_V4_PART = re.compile(r"(0|[1-9][0-9]*)")
_CGNAT_V4 = ipaddress.ip_network("100.64.0.0/10")
_NAT64_NET = ipaddress.ip_network("64:ff9b::/96")
_SIXTOFOUR_NET = ipaddress.ip_network("2002::/16")


def _is_canonical_ipv4(host: str) -> bool:
    """Four decimal octets 0..255 with no leading zeros."""
    parts = host.split(".")
    if len(parts) != 4:
        return False
    for part in parts:
        if not _CANON_V4_PART.fullmatch(part):
            return False
        try:
            value = int(part)
        except Exception:
            return False
        if not 0 <= value <= 255:
            return False
    return True


def _split_netloc(netloc: str) -> tuple[str, str | None]:
    """Raw host (brackets kept for IPv6) and raw port (None when absent)."""
    if netloc.startswith("["):
        end = netloc.find("]")
        if end == -1:
            raise ValueError("unterminated bracket")
        host_raw = netloc[: end + 1]
        rest = netloc[end + 1:]
        if rest == "":
            return host_raw, None
        if not rest.startswith(":"):
            raise ValueError("bad bracket suffix")
        return host_raw, rest[1:]
    if "[" in netloc or "]" in netloc:
        raise ValueError("stray bracket")
    if ":" in netloc:
        host_raw, _, port_raw = netloc.rpartition(":")
        if not host_raw:
            raise ValueError("empty host")
        return host_raw, port_raw
    if not netloc:
        raise ValueError("empty netloc")
    return netloc, None


def _target_problem(target: Any) -> str | None:
    """One `target` message when the probe target is refused, else None."""
    base = ("monitor `target` must be an `http://` or `https://` URL "
            "with a public host (no userinfo, no internal names, no "
            "non-global IPs)")
    if not isinstance(target, str):
        return base
    for ch in target:
        if ord(ch) < 0x21 or ord(ch) > 0x7E:
            return base
    try:
        parsed = urlsplit(target)
    except Exception:
        return base
    if parsed.scheme not in ("http", "https"):
        return base
    try:
        netloc = parsed.netloc
    except Exception:
        return base
    if not netloc:
        return base
    if "%" in netloc:
        return base
    if "@" in netloc:
        return base
    try:
        if parsed.username is not None or parsed.password is not None:
            return base
    except Exception:
        return base
    try:
        parsed_port = parsed.port
    except Exception:
        return base
    try:
        host_raw, port_raw = _split_netloc(netloc)
    except Exception:
        return base
    if port_raw is not None:
        if not port_raw or not port_raw.isascii() or not port_raw.isdigit():
            return base
        try:
            port_int = int(port_raw)
        except Exception:
            return base
        if not 1 <= port_int <= 65535:
            return base
        if parsed_port is None or parsed_port != port_int:
            # urlsplit agrees on numeric ports; any disagreement is a refusal.
            try:
                if parsed_port != port_int:
                    return base
            except Exception:
                return base
    elif parsed_port is not None:
        return base
    # The host as written: uppercase is refused, never folded.
    if _UPPER_RE.search(host_raw):
        return base
    if host_raw.startswith("["):
        if not host_raw.endswith("]"):
            return base
        inner = host_raw[1:-1]
        if not inner:
            return base
        try:
            ip = ipaddress.IPv6Address(inner)
        except Exception:
            return base
        try:
            if not ip.is_global:
                return base
            if ip.is_multicast:
                return base
        except Exception:
            return base
        try:
            mapped = ip.ipv4_mapped
        except Exception:
            mapped = None
        if mapped is not None:
            try:
                if not mapped.is_global:
                    return base
                if mapped.is_multicast:
                    return base
            except Exception:
                return base
        try:
            if ip in _NAT64_NET:
                embedded = ipaddress.IPv4Address(int(ip) & 0xFFFFFFFF)
                if not embedded.is_global:
                    return base
                if embedded.is_multicast:
                    return base
        except Exception:
            return base
        try:
            if ip in _SIXTOFOUR_NET:
                try:
                    embedded6 = ip.sixtofour
                except Exception:
                    embedded6 = None
                if embedded6 is None:
                    embedded6 = ipaddress.IPv4Address(
                        (int(ip) >> 80) & 0xFFFFFFFF)
                if not embedded6.is_global:
                    return base
                if embedded6.is_multicast:
                    return base
        except Exception:
            return base
        return None
    if ":" in host_raw:
        return base
    if host_raw.endswith("."):
        return base
    if _is_canonical_ipv4(host_raw):
        try:
            ip = ipaddress.IPv4Address(host_raw)
        except Exception:
            return base
        try:
            if ip in _CGNAT_V4:
                return base
            if not ip.is_global:
                return base
            if ip.is_multicast:
                return base
        except Exception:
            return base
        return None
    if len(host_raw) > 253:
        return base
    labels = host_raw.split(".")
    if len(labels) < 2:
        return base
    for label in labels:
        if not _DNS_LABEL_RE.fullmatch(label):
            return base
        if label.startswith("xn--"):
            return base
    if not _TLD_RE.fullmatch(labels[-1]):
        return base
    if host_raw == "localhost":
        return base
    for suffix in (".svc", ".cluster.local", ".internal", ".local", ".localhost"):
        if host_raw.endswith(suffix):
            return base
    return None


def _query_problem(query: str) -> str | None:
    """One `query` message when the expression could escape its parentheses."""
    if "\n" in query or "\r" in query:
        return "monitor `query` must not contain newlines"
    paren = bracket = brace = 0
    i = 0
    n = len(query)
    while i < n:
        c = query[i]
        if c == '"' or c == "'":
            quote = c
            i += 1
            closed = False
            while i < n:
                d = query[i]
                if d == "\\":
                    i += 2
                    continue
                if d == quote:
                    closed = True
                    i += 1
                    break
                i += 1
            if not closed:
                return "monitor `query` has an unterminated string literal"
            continue
        if c == "`":
            i += 1
            closed = False
            while i < n:
                if query[i] == "`":
                    closed = True
                    i += 1
                    break
                i += 1
            if not closed:
                return "monitor `query` has an unterminated string literal"
            continue
        if c == "#":
            return "monitor `query` must not contain `#` outside string literals"
        if c == "(":
            paren += 1
        elif c == ")":
            paren -= 1
            if paren < 0:
                return "monitor `query` has unbalanced parentheses"
        elif c == "[":
            bracket += 1
        elif c == "]":
            bracket -= 1
            if bracket < 0:
                return "monitor `query` has unbalanced brackets"
        elif c == "{":
            brace += 1
        elif c == "}":
            brace -= 1
            if brace < 0:
                return "monitor `query` has unbalanced braces"
        i += 1
    if paren != 0 or bracket != 0 or brace != 0:
        return "monitor `query` has unbalanced parentheses/brackets/braces"
    return None


@dataclass
class RenderResult:
    """What `render_monitors` made: sorted objects, and filename → one message."""

    objects: list[dict[str, Any]] = field(default_factory=list)
    errors: dict[str, str] = field(default_factory=dict)


def validate(body: Any, *, catalog: dict[str, Any],
             filename: str | None = None, loki_ruler_wired: bool = False,
             uptime_on: bool = True, telegram_on: bool = True) -> list[str]:
    """Every problem with one monitor body, as messages naming the field; [] when valid."""
    if not isinstance(body, dict):
        return [f"monitor body must be a mapping of fields, got {type(body).__name__}"]
    problems: list[str] = []
    try:
        name = body.get("name")
        if not isinstance(name, str) or not NAME_RE.fullmatch(name):
            problems.append(
                f"monitor `name` {name!r} must be a DNS label (lowercase letters, digits, "
                f"`-`, at most 63 chars)")
        if filename is not None and isinstance(name, str):
            try:
                stem = filename[:-len(".yaml")] if filename.endswith(".yaml") else filename
            except Exception:
                stem = filename
            if name != stem:
                problems.append(
                    f"monitor name {name!r} does not match filename {filename!r}: the file "
                    f"for monitor {stem!r} must be named {stem!r}.yaml")

        projects = catalog.get("projects") if isinstance(catalog, dict) else None
        ids: list[str] = []
        if isinstance(projects, list):
            for p in projects:
                try:
                    if isinstance(p, dict):
                        pid = p.get("id")
                        if isinstance(pid, str):
                            ids.append(pid)
                except Exception:
                    continue
        project = body.get("project")
        entry = None
        if isinstance(projects, list):
            try:
                entry = next((p for p in projects
                              if isinstance(p, dict) and p.get("id") == project), None)
            except Exception:
                entry = None
        if entry is None:
            problems.append(f"monitor `project` {project!r} is not a catalog project "
                            f"(known: {sorted(ids)})")
        env = body.get("env")
        envs = entry.get("environments") if isinstance(entry, dict) else None
        env_ok = False
        if isinstance(envs, dict) and isinstance(env, str):
            try:
                env_ok = env in envs
            except Exception:
                env_ok = False
        if not env_ok:
            try:
                known = sorted(k for k in envs.keys() if isinstance(k, str)) \
                    if isinstance(envs, dict) else []
            except Exception:
                known = []
            problems.append(f"monitor `env` {env!r} is not an environment of project "
                            f"{project!r} (known: {known})")

        kind = body.get("kind")
        if kind not in KINDS:
            problems.append(f"monitor `kind` {kind!r} must be one of {sorted(KINDS)}")

        condition = body.get("condition")
        if not isinstance(condition, dict):
            problems.append(f"monitor `condition` must be a mapping, got {condition!r}")
            condition = {}
        if kind in ("metric", "log"):
            query = body.get("query")
            if not isinstance(query, str) or not query.strip():
                problems.append("monitor `query` must be a non-empty PromQL/LogQL string")
            elif len(query) > QUERY_MAX:
                problems.append(f"monitor `query` is {len(query)} characters; at most {QUERY_MAX}")
            else:
                try:
                    qp = _query_problem(query)
                except Exception as exc:
                    qp = f"monitor `query` is invalid: {exc!r}"
                if qp is not None:
                    problems.append(qp)
            try:
                comparison = condition.get("comparison")
            except Exception:
                comparison = None
            if not isinstance(comparison, str) or comparison not in COMPARISONS:
                problems.append(f"monitor `condition.comparison` {comparison!r} must be one "
                                f"of {sorted(COMPARISONS)}")
            try:
                has_threshold = "threshold" in condition
                threshold = condition.get("threshold") if has_threshold else None
            except Exception:
                has_threshold = False
                threshold = None
            if not has_threshold or threshold is None:
                problems.append("monitor `condition.threshold` is required")
            elif isinstance(threshold, bool) or not isinstance(threshold, (int, float)):
                problems.append(f"monitor `condition.threshold` must be a number (int or "
                                f"float, never bool or string), got {threshold!r}")
            elif not math.isfinite(threshold):
                problems.append("monitor `condition.threshold` must be finite (never nan or inf)")
        elif kind == "probe":
            try:
                tp = _target_problem(body.get("target"))
            except Exception as exc:
                tp = f"monitor `target` is invalid: {exc!r}"
            if tp is not None:
                problems.append(tp)
            try:
                module = body.get("module", PROBE_MODULE_DEFAULT)
            except Exception:
                module = None
            if not isinstance(module, str) or not module:
                problems.append(f"monitor `module` must be a non-empty blackbox module "
                                f"name, got {module!r}")
        if kind == "probe" and not uptime_on:
            problems.append(
                f"probe monitor {name!r} needs the uptime feature, which is off on this "
                f"platform: nothing would run the probe, so the monitor is refused rather "
                f"than rendered")
        if kind == "log" and not loki_ruler_wired:
            problems.append(
                f"log monitor {name!r} needs the Loki ruler, and this instance does not run "
                f"one that reads the monitors' rules: nothing would evaluate the rule, so "
                f"the monitor is refused rather than rendered. No field of the monitor "
                f"changes that. The ruler comes with the logs feature (orbit_features_list; "
                f"an admin turns it on with orbit_features_set); on an instance whose Loki "
                f"is wired by hand, declare `lokiRuler: wired` in instance/config.yaml "
                f"(docs/MONITORS.md, \"Log monitors\"). A metric monitor needs neither")

        try:
            for_dur = condition.get("for")
        except Exception:
            for_dur = None
        if not isinstance(for_dur, str) or not for_dur or not PROM_DURATION_RE.fullmatch(for_dur):
            problems.append(
                f"monitor `condition.for` {for_dur!r} must be a Prometheus duration (e.g. "
                f"5m, 90s, 1h30m): units run largest to smallest (y w d h m s ms), each at "
                f"most once")

        severity = body.get("severity")
        if severity not in SEVERITIES:
            problems.append(f"monitor `severity` {severity!r} must be one of {sorted(SEVERITIES)}")

        try:
            notify = body.get("notify", [])
        except Exception:
            notify = None
        try:
            notify_bad = (not isinstance(notify, list)
                          or any(n not in NOTIFY_CHANNELS for n in notify))
        except Exception:
            notify_bad = True
        if not notify_bad and "telegram" in notify and not telegram_on:
            problems.append(
                f"monitor {name!r} notifies telegram and the alerts-telegram feature is off "
                f"on this platform: nobody would be messaged, so the monitor is refused "
                f"rather than rendered (use `notify: []`, or turn the feature on)")
        if notify_bad:
            problems.append(f"monitor `notify` {notify!r} must be a list with every entry "
                            f"in {sorted(NOTIFY_CHANNELS)}; empty means no channel")

        summary = body.get("summary")
        if not isinstance(summary, str) or not summary.strip():
            problems.append("monitor `summary` must be a non-empty human sentence")
        elif len(summary) > SUMMARY_MAX:
            problems.append(f"monitor `summary` is {len(summary)} characters; at most {SUMMARY_MAX}")
        runbook = body.get("runbook")
        if runbook is not None and not isinstance(runbook, str):
            problems.append(f"monitor `runbook` must be a URL string, got {runbook!r}")
        elif isinstance(runbook, str) and len(runbook) > RUNBOOK_MAX:
            problems.append(f"monitor `runbook` is {len(runbook)} characters; at most {RUNBOOK_MAX}")
    except Exception as exc:
        problems.append(f"monitor file is invalid: {exc!r}")

    return problems


def render_monitors(files: dict[str, Any], *, catalog: dict[str, Any],
                    loki_ruler_wired: bool = False, uptime_on: bool = True,
                    telegram_on: bool = True) -> RenderResult:
    """Render each file into cluster objects; a bad file is reported and skipped."""
    out = RenderResult()
    log_rules: list[dict[str, Any]] = []
    for filename, raw in files.items():
        try:
            body = deepcopy(raw)
        except Exception as exc:
            out.errors[filename] = f"monitor file is invalid: {exc!r}"
            continue
        try:
            problems = validate(body, catalog=catalog, filename=filename,
                                loki_ruler_wired=loki_ruler_wired, uptime_on=uptime_on,
                                telegram_on=telegram_on)
        except Exception as exc:
            out.errors[filename] = f"monitor file is invalid: {exc!r}"
            continue
        if problems:
            out.errors[filename] = "; ".join(problems)
            continue
        try:
            kind = body["kind"]
            if kind == "log":
                log_rules.append(_loki_rule(body))
            elif kind == "probe":
                out.objects.append(_probe(body))
                out.objects.append(_prometheus_rule(body, _probe_expr(body)))
            else:
                out.objects.append(_prometheus_rule(body, _metric_expr(body)))
        except KeyError as exc:
            out.errors[filename] = f"monitor `{exc.args[0]}` is missing or invalid: {exc!r}"
            continue
        except Exception as exc:
            out.errors[filename] = f"monitor file is invalid: {exc!r}"
            continue
    if log_rules:
        out.objects.append(log_configmap(log_rules))
    out.objects.sort(key=lambda o: (o["kind"], o["metadata"]["namespace"],
                                    o["metadata"]["name"]))
    return out


def log_configmap(log_rules: list[dict[str, Any]]) -> dict[str, Any]:
    """The one ConfigMap the Loki ruler reads every log monitor's rule from. One shared
    file: the ruler loads every ConfigMap key, and one file keeps the group's rule order
    (by alert name) obvious in a single place. With no rule it holds no group: that is
    how the last log monitor's rule leaves the ruler (see monitors_apply.plan_monitors)."""
    groups = [{"name": LOG_GROUP, "rules": sorted(log_rules, key=lambda r: r["alert"])}] if log_rules else []
    return {
        "apiVersion": "v1", "kind": "ConfigMap",
        "metadata": {"name": LOG_CONFIGMAP, "namespace": LOKI_NS,
                     "labels": {LABEL_COMPONENT: COMPONENT}},
        "data": {"rules.yaml": yaml.safe_dump({"groups": groups}, sort_keys=False)},
    }


def _fmt_threshold(threshold: int | float) -> str:
    # Thresholds ride inside PromQL text: render `120`, never `120.0` — the float
    # noise reads as precision the monitor does not have.
    if isinstance(threshold, float) and threshold.is_integer():
        return str(int(threshold))
    return str(threshold)


def _metric_expr(body: dict[str, Any]) -> str:
    condition = body["condition"]
    return f"({body['query']}) {condition['comparison']} {_fmt_threshold(condition['threshold'])}"


def _probe_expr(body: dict[str, Any]) -> str:
    return f"probe_success{{job=\"orbit-monitor-{body['name']}\"}} == 0"


def _rule_labels(body: dict[str, Any]) -> dict[str, str]:
    notify = body.get("notify") or []
    return {"severity": SEVERITY_MAP[body["severity"]],
            "orbit_severity": body["severity"],
            "orbit_monitor": body["name"],
            "orbit_notify": notify[0] if notify else "none",
            "project": body["project"], "env": body["env"]}


def _annotations(body: dict[str, Any]) -> dict[str, str]:
    annotations = {"summary": body["summary"]}
    if body.get("runbook"):
        annotations["runbook_url"] = body["runbook"]
    return annotations


def _loki_rule(body: dict[str, Any]) -> dict[str, Any]:
    # The shape Prometheus and the Loki ruler both consume (groups[].rules[]).
    return {"alert": body["name"], "expr": _metric_expr(body), "for": body["condition"]["for"],
            "labels": _rule_labels(body), "annotations": _annotations(body)}


def _object_meta(body: dict[str, Any]) -> dict[str, Any]:
    return {"name": f"orbit-monitor-{body['name']}", "namespace": MONITORING_NS,
            "labels": {LABEL_MONITOR: body["name"], LABEL_COMPONENT: COMPONENT}}


def _prometheus_rule(body: dict[str, Any], expr: str) -> dict[str, Any]:
    name = body["name"]
    return {
        "apiVersion": "monitoring.coreos.com/v1", "kind": "PrometheusRule",
        "metadata": _object_meta(body),
        "spec": {"groups": [{"name": f"orbit.monitor.{name}", "rules": [{
            "alert": name, "expr": expr, "for": body["condition"]["for"],
            "labels": _rule_labels(body), "annotations": _annotations(body)}]}]},
    }


def _probe(body: dict[str, Any]) -> dict[str, Any]:
    job = f"orbit-monitor-{body['name']}"
    return {
        "apiVersion": "monitoring.coreos.com/v1", "kind": "Probe",
        "metadata": _object_meta(body),
        "spec": {"jobName": job, "prober": {"url": BLACKBOX_URL},
                 # The module is the blackbox check to run; http_2xx is the probe
                 # almost every door wants, so it is the default, not a required field.
                 "module": body.get("module") or PROBE_MODULE_DEFAULT,
                 "targets": {"staticConfig": {"static": [body["target"]]}}},
    }
