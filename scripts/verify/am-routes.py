#!/usr/bin/env python3
"""Where would an alert actually GO? — the agent plane's delivery half, asserted.

A metric/logs trigger renders an alerting RULE and an intake ROUTE. Neither is delivery:
the rule fires an alert into Alertmanager, and if Alertmanager routes it anywhere other
than the intake, the agent never runs and a human is paged with a machine's work item.
That route lives in the platform's Alertmanager config, which no generator writes — so it
is asserted here, by SIMULATING Alertmanager's own first-match-wins routing over the
config rather than grepping it for a string.

    am-routes.py app|raw <alertmanager-config> [--catalog <projects.yaml>]
                                               [--secrets <sealed.yaml|sealed-dir>]
                                               [--producers-optional]
                                               [--desired <git kube-prometheus-stack.yaml>]
    --desired  raw mode only: the running config redacts every secret-typed value (the
               webhook URL among them, shown as <secret>), so the intake receiver is
               the one the GIT config identifies by URL, matched by name in the live
               config; its live URL must be the intake URL or the redaction marker.

    app   an Argo Application carrying the kube-prometheus-stack values (git: core's
          template, or an instance's own). {{placeholders}} are substituted before parsing.
    raw   a plain alertmanager.yaml, as read out of the running config Secret.

    --catalog  also assert that every alertname THIS catalog's triggers fire on reaches
               the intake, and ARM the plane's wiring rows only once a metric/logs trigger
               is actually declared; without it the generic OrbitAgent* route is required
               outright (which is the right rule for core's template).
    --secrets  also assert the sealed (sops) producer of every Secret Alertmanager
               mounts, and that the intake bearer's Secret seals the key the intake
               verifies. The path is one sealed YAML file or a directory of them.

Four properties this simulation is careful about, because each one was a way the check
could pass over a config that does not deliver:

  * FULL LABELS. A generated alert does not carry `alertname` alone — the generator emits
    `severity`, `orbit_agent`, `orbit_trigger` and the trigger's own catalog labels beside
    it. Simulating with a two-label alert made a `severity =~ "critical|warning"` route
    sitting above the plane's route invisible, and that route steals every agent alert.
  * FAIL CLOSED. Alertmanager accepts matcher forms this file has to parse: the braced
    `{alertname=~"^OrbitAgent.*"}` and comma-joined `a="1", b="2"`. A matcher this parser
    cannot read is NOT "a route that does not match" — that is a fail-open answer. It
    ABORTS the whole check with a red row naming the matcher.
  * INHIBITION, not just routing. An alert can be routed perfectly and still be held
    `suppressed` by an inhibit rule, before the notification pipeline runs — which is how
    a `severity: info` trigger reached the intake receiver on paper and never once woke an
    agent. The rules simulated are the ones Alertmanager will RUN: an `app` config that
    declares none inherits kube-prometheus-stack's stock four, because a values list
    replaces the chart default rather than merging with it.
  * AM's ACTUAL ROUTING. A parent receiver is used only when NO child matched (not
    appended unconditionally, which produced false FAILs on a legitimate `continue: true`);
    a child inherits its parent's receiver but NOT its time intervals (each route's own,
    dispatch/route.go); the terminal route's `mute_time_intervals` / `active_time_intervals`
    are reported, because a muted route delivers nothing however well it matches; and a
    root route carrying an interval is refused, as Alertmanager refuses that config.

Prints one `ok `/`FAIL ` line per assertion — stage3.sh turns them into its own rows — and
exits non-zero if any failed, so core CI can call it directly. Importing it runs no checks
at all: stage3.sh's mutation harness imports it for the matcher simulation, so the route it
sabotages is chosen with the same semantics this checker reads the config with.
"""
import importlib.util
import pathlib
import re
import sys
import urllib.parse

import yaml

# The checks below import the generator to reuse its alertname derivation; never leave a
# __pycache__ behind in the repo being verified.
sys.dont_write_bytecode = True

# Everything argv-dependent lives under the `__main__` guard at the bottom, so this file
# can also be IMPORTED for its matcher simulation alone. stage3.sh's mutation harness does
# exactly that: it has to answer "which route carries the derived alertname space" before
# it can sabotage that route, and a second matcher parser written over there could disagree
# with this one — at which point the harness would mutate a route this checker never reads.
out = []
def check(label, cond): out.append(("ok " if cond else "FAIL ") + label)


def finish():
    print("\n".join(out))
    sys.exit(1 if any(line.startswith("FAIL") for line in out) else 0)


def abort(label):
    """A red row, and STOP. Used only where the config cannot be read honestly: carrying
    on would mean answering "where does this alert go" from a config half of which this
    simulator did not understand — which is the fail-open answer, not a conservative one."""
    check(label, False)
    finish()


# --- the generator's own derivation, imported rather than copied -------------------
# The sample alertname below used to be a hardcoded literal, so a rename of
# DERIVED_ALERT_PREFIX in catalog-sync.py would leave this oracle asserting a route for a
# prefix nothing fires under any more — green, and completely wrong. It is derived from
# the generator instead. Resolved from THIS file's location, not the cwd: core CI runs it
# from the repo root and the verify suite runs it from an instance root, and the generator
# sits beside it either way.
GENERATOR = pathlib.Path(__file__).resolve().parent.parent / "catalog-sync.py"
def load_generator():
    if not GENERATOR.exists():
        abort(f"the generator {GENERATOR} is not there, so the derived alertname this "
              f"oracle asserts a route for cannot be derived from it")
    try:
        spec = importlib.util.spec_from_file_location("orbit_catalog_sync", GENERATOR)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module
    except BaseException as exc:      # SystemExit included: a generator that exits on
        abort(f"importing {GENERATOR} to derive the alertname prefix failed ({exc!r}) — "
              f"the generator must be import-safe for this oracle to agree with it")


cs = load_generator()
PREFIX = cs.DERIVED_ALERT_PREFIX
SEVERITIES = cs.TRIGGER_SEVERITIES
# The plane's own namespace — a generated alert is grouped by [alertname, namespace] and
# an instance-side route keyed on the namespace must be visible to this simulation.
PLANE_NAMESPACE = f"{cs.AGENTS_PROJECT}-{cs.AGENTS_ENV}"
# Probes that span the WHOLE derived space, not one name in it: a route narrowed to
# `^OrbitAgentSomeAgentSomeTrigger$` carries the sample and nothing else, and every trigger
# anyone declares later would fall through to a human. Every probe must reach the intake.
SAMPLE = cs._agent_alertname("some-agent", "some-trigger")
PROBES = [SAMPLE,
          cs._agent_alertname("zzz-team", "disk-full"),
          cs._agent_alertname("a", "b"),
          PREFIX + "X"]
if not all(p.startswith(PREFIX) for p in PROBES):
    abort(f"the generator's alertname derivation no longer produces names under "
          f"{PREFIX!r}; this oracle and catalog-sync.py have drifted")


def alert_labels(alertname, severity="warning", agent="some-agent", trigger="some-trigger",
                 extra=None):
    """The label set catalog-sync puts on a generated alert (`_trigger_rule`), which is
    what Alertmanager routes on — not the alertname alone. The generator sets NO
    namespace label: an alert carries one only when the expression's series does
    (`vector(1)` carries none), so a route keyed on a namespace is simulated by
    `variants`, which hands back the alert both ways."""
    labels = {"alertname": alertname, "severity": severity,
              "orbit_agent": agent, "orbit_trigger": trigger}
    labels.update(extra or {})
    return labels


def variants(labels):
    """The alert as its series may deliver it: with no namespace label, and with the
    plane's own (the namespace a series scraped from the plane carries). A route must
    deliver BOTH; one that requires the namespace loses every expression-only alert."""
    if "namespace" in labels:
        # a rule label overrides the series' label (Prometheus and the Loki ruler both):
        # an alert whose catalog labels name a namespace carries THAT one, only (round 23)
        return [dict(labels)]
    return [dict(labels), {**labels, "namespace": PLANE_NAMESPACE}]


def load_yaml(path, what):
    """A file read as YAML, or an abort naming the file and the error's CLASS: a parser's
    message quotes the text around the error, and a config may hold anything there."""
    try:
        return yaml.safe_load(open(path, encoding="utf-8"))
    except (OSError, ValueError, yaml.YAMLError) as exc:
        abort(f"{what} {path} cannot be read as YAML ({type(exc).__name__})")


AM_NAMESPACE = "monitoring"     # where the Alertmanager pod mounts: the Application's destination


def load_am(mode, path):
    if mode == "raw":                       # a live alertmanager.yaml, as the pod reads it
        cfg = load_yaml(path, "the running Alertmanager config")
        if not isinstance(cfg, dict):
            abort(f"the running Alertmanager config {path} is not a YAML map")
        return {"config": cfg}
    doc = load_yaml(path, "the Alertmanager Application")   # the Argo Application that carries the values
    global AM_NAMESPACE
    try:
        dest = (doc["spec"].get("destination") or {}).get("namespace")
        if dest is not None and not (isinstance(dest, str) and re.fullmatch(r"[a-z0-9]([-a-z0-9]{0,61}[a-z0-9])?", dest)):
            abort(f"the Alertmanager Application {path} has a destination namespace that is not "
                  f"a namespace name (the value is not repeated)")
        AM_NAMESPACE = dest if dest else AM_NAMESPACE
        helm = doc["spec"]["source"]["helm"]
        # the renderer (orbitd platform.py) appends helm.valueFiles as later -f arguments
        # and passes helm.parameters: the effective values would then differ from the
        # inline ones this checker certifies — refused explicitly, never certified (round 24)
        extra = [k for k in ("valueFiles", "parameters", "fileParameters", "valuesFrom") if helm.get(k)]
        if extra:
            abort(f"the Alertmanager Application {path} carries helm.{extra[0]} — the effective values "
                  f"are not the inline ones, so the route cannot be certified from valuesObject/values alone")
        vals = helm.get("valuesObject")
        if vals is None:
            # The template carries {{placeholders}} an instance fills in; they are not YAML.
            vals = yaml.safe_load(re.sub(r"\{\{[^{}]*\}\}", "PLACEHOLDER", helm["values"]))
        am = vals["alertmanager"]
        if not isinstance(am, dict) or not isinstance(am.get("config"), dict):
            raise KeyError("alertmanager.config")
        return am
    except (KeyError, TypeError, AttributeError, yaml.YAMLError) as exc:
        abort(f"the Alertmanager Application {path} does not carry alertmanager.config the way "
              f"the chart values do ({type(exc).__name__})")


# --- matchers ---------------------------------------------------------------------
class Unparseable(Exception):
    """A matcher this simulator cannot read. Never swallowed into "does not match"."""


MATCHER = re.compile(r'^\s*([A-Za-z_][A-Za-z0-9_]*)\s*(=~|!~|!=|=)\s*(.*?)\s*$')
UNQUOTED = re.compile(r'[^\s,{}=~!"\']*')
# Every key Alertmanager reads on a route. Anything else is either a typo (so the route is
# not doing what its author thinks) or a key that changes delivery in a way this simulation
# does not model — both are aborts, not silent passes.
ROUTE_KEYS = frozenset({
    "receiver", "group_by", "continue", "matchers", "match", "match_re", "routes",
    "group_wait", "group_interval", "repeat_interval",
    "mute_time_intervals", "active_time_intervals",
})


def split_matchers(expr):
    """One `matchers:` entry -> the list of matchers it holds.

    Alertmanager's own parser accepts all three spellings, and a config using any of them
    is legal: `a="1"`, `{a="1",b="2"}` and `a="1", b="2"`. Splitting on top-level commas
    only — a comma inside a quoted value, or inside a regex repetition like `{2,3}`, is
    part of the matcher, not a separator."""
    if not isinstance(expr, str):
        raise Unparseable(repr(expr), "a matcher that is not a string")
    s = expr.strip()
    braced = s.startswith("{")
    if braced:
        if not s.endswith("}"):
            raise Unparseable(expr, "a `{`-braced matcher with no closing `}`")
        s = s[1:-1]
    parts, buf, depth, quote, esc = [], "", 0, "", False
    for ch in s:
        if esc:
            buf += ch
            esc = False
            continue
        if quote:
            buf += ch
            if ch == "\\":
                esc = True
            elif ch == quote:
                quote = ""
            continue
        if ch in "\"'":
            quote = ch
        elif ch in "([{":
            depth += 1
        elif ch in ")]}":
            depth -= 1
        elif ch == "," and depth == 0:
            parts.append(buf)
            buf = ""
            continue
        buf += ch
    if quote:
        raise Unparseable(expr, "an unterminated quoted value")
    if depth:
        raise Unparseable(expr, "unbalanced brackets")
    parts.append(buf)
    parts = [p.strip() for p in parts]
    parts = [p for p in parts if p]
    if not parts and not (braced and not s.strip()):
        # `{}` is Alertmanager's legal match-everything; an empty non-braced entry is not.
        raise Unparseable(expr, "an empty matcher")
    return parts


def unquote(value, expr):
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        body, result, i = value[1:-1], "", 0
        # Alertmanager's lexer unescapes the string escapes; a REGEX's own escapes (`\.`,
        # `\d`) are not string escapes and must survive verbatim or every anchored regex
        # matcher in the config would be silently rewritten before it is evaluated.
        while i < len(body):
            if body[i] == "\\" and i + 1 < len(body):
                nxt = body[i + 1]
                result += {"\\": "\\", '"': '"', "'": "'",
                           "n": "\n", "t": "\t", "r": "\r"}.get(nxt, body[i:i + 2])
                i += 2
                continue
            result += body[i]
            i += 1
        return result
    if value[:1] in ("\"", "'") or value[-1:] in ("\"", "'"):
        raise Unparseable(expr, "a quoted value with no matching closing quote")
    if UNQUOTED.fullmatch(value) is None:
        raise Unparseable(expr, "an unquoted value Alertmanager would not accept")
    return value


# What Python's engine accepts and RE2 refuses: lookaround, atomic groups, conditionals,
# backreferences, possessive quantifiers, \Z and \G. A matcher using one compiles here
# and makes Alertmanager refuse its whole config — so it is refused here first.
NOT_RE2 = re.compile(r"\(\?=|\(\?!|\(\?<=|\(\?<!|\(\?>|\(\?\(|\(\?#|\(\?P=|\\[1-9]|\\k<|(?<!\\)[*+?}]\+|\\Z|\\G")


# RE2 refuses a counted repetition above 1000 (Python does not); RE2 has the POSIX
# classes `[[:alnum:]]` etc. inside brackets (Python does not) — both read the way the
# router reads them
RE2_MAX_REPEAT = 1000
POSIX_CLASS = {"alnum": "a-zA-Z0-9", "alpha": "a-zA-Z", "digit": "0-9", "lower": "a-z", "upper": "A-Z",
               "xdigit": "0-9A-Fa-f", "word": "a-zA-Z0-9_", "space": " \\t\\n\\r\\f\\v", "blank": " \\t",
               "punct": "!-/:-@\\[-`{-~", "cntrl": "\\x00-\\x1f\\x7f", "print": " -~", "graph": "!-~", "ascii": "\\x00-\\x7f"}


# Go's \d \w \s \b are ASCII-only while its (?i) folds Unicode case (k and K, the Kelvin
# sign, are one letter): Python's re.ASCII made the classes right and the folding wrong.
# The classes are spelled out as ASCII sets instead, and the pattern compiles WITHOUT
# re.ASCII so case folding is Go's (round 24). Inside a bracket class the bare ranges are
# spliced in; a complement class inside brackets has no spelling and is refused.
_ASCII_CLASS = {"d": "0-9", "w": "0-9A-Za-z_", "s": "\\t\\n\\f\\r\\v "}
_ASCII_OUT = {"d": "[0-9]", "D": "[^0-9]", "w": "[0-9A-Za-z_]", "W": "[^0-9A-Za-z_]", "s": "[\\t\\n\\f\\r\\v ]", "S": "[^\\t\\n\\f\\r\\v ]",
              "b": "(?:(?<![0-9A-Za-z_])(?=[0-9A-Za-z_])|(?<=[0-9A-Za-z_])(?![0-9A-Za-z_]))",
              "B": "(?:(?<![0-9A-Za-z_])(?![0-9A-Za-z_])|(?<=[0-9A-Za-z_])(?=[0-9A-Za-z_]))"}


def ascii_classes(pattern, expr):
    out, i, in_class = [], 0, False
    while i < len(pattern):
        c = pattern[i]
        if c == "\\" and i + 1 < len(pattern):
            n = pattern[i + 1]
            if in_class and n in _ASCII_CLASS:
                out.append(_ASCII_CLASS[n]); i += 2; continue
            if in_class and n in ("D", "W", "S", "b", "B"):
                raise Unparseable(expr, "a complement or boundary class inside a bracket expression, which this "
                                        "simulator cannot spell in Go's ASCII sense; the pattern is not repeated")
            if not in_class and n in _ASCII_OUT:
                out.append(_ASCII_OUT[n]); i += 2; continue
            out.append(c + n); i += 2; continue
        if c == "[" and not in_class:
            in_class = True; out.append(c); i += 1
            if i < len(pattern) and pattern[i] == "^": out.append("^"); i += 1
            if i < len(pattern) and pattern[i] == "]": out.append("\\]"); i += 1
            continue
        if c == "]" and in_class:
            in_class = False
        out.append(c); i += 1
    return "".join(out)


def re2_to_python(pattern, expr):
    for m in re.finditer(r"\{(\d+)(?:,(\d*))?\}", pattern):
        if any(int(n) > RE2_MAX_REPEAT for n in m.groups() if n):
            raise Unparseable(expr, "a counted repetition above 1000, which Alertmanager's RE2 engine "
                                    "refuses — Alertmanager would refuse the whole config; the pattern is not repeated")
    def cls(m):
        name = m.group(1)
        if name not in POSIX_CLASS:
            raise Unparseable(expr, "a POSIX character class RE2 does not have; the pattern is not repeated")
        return POSIX_CLASS[name]
    return ascii_classes(re.sub(r"\[:([a-z]+):\]", cls, pattern), expr)


# Go's simple case folding has no orbit for İ (U+0130) and ı (U+0131): under (?i) they
# match only themselves, while Python folds both onto i/I. Both are moved to private-use
# code points (no case at all) in the pattern and in the subject, so the two engines
# agree; every other orbit Python knows (K/k/Kelvin, s/S/ſ, µ/μ/Μ, the Greek symbol
# letters) is one Go has too (round 25).
_NOFOLD = str.maketrans({"\u0130": "\ue130", "\u0131": "\ue131"})


class _GoRe:
    __slots__ = ("_re", "pattern")

    def __init__(self, compiled, pattern):
        self._re, self.pattern = compiled, pattern

    def fullmatch(self, s):
        return self._re.fullmatch(s.translate(_NOFOLD))

    def search(self, s):
        return self._re.search(s.translate(_NOFOLD))


def compile_re(pattern, expr):
    """Alertmanager compiles with Go's RE2, where \\d, \\w, \\s and \\b are ASCII-only
    (spelled out as ASCII sets by re2_to_python) while (?i) folds Unicode case (kept:
    the pattern compiles without re.ASCII) — round 24. İ and ı, which Go never folds,
    are matched through _GoRe — round 25."""
    if NOT_RE2.search(pattern):
        raise Unparseable(expr, "regex syntax Alertmanager's RE2 engine does not have (a lookaround, "
                                "backreference, atomic or possessive form, conditional, comment group, \\Z or \\G) — "
                                "Alertmanager would refuse the whole config; the pattern is not repeated")
    pattern = re2_to_python(pattern, expr).translate(_NOFOLD)
    try:
        return _GoRe(re.compile(pattern), pattern)   # Unicode case folding, like Go's (rounds 24, 25)
    except re.error:
        # the engine's message quotes the pattern (a group name, a bad escape): a fixed
        # category only, the matcher is never repeated
        raise Unparseable(expr, "a regex that does not compile (the engine's message is not "
                                "repeated: it quotes the pattern)")


def one_matcher(expr, labels):
    m = MATCHER.match(expr)
    if not m:
        raise Unparseable(expr, "not a `label <op> value` matcher")
    key, op, value = m.group(1), m.group(2), unquote(m.group(3), expr)
    got = labels.get(key, "")
    if op == "=":
        return got == value
    if op == "!=":
        return got != value
    # Alertmanager anchors regex matchers at both ends.
    if op == "=~":
        return compile_re(value, expr).fullmatch(got) is not None
    return compile_re(value, expr).fullmatch(got) is None


def matcher_set(entries, match, match_re, labels):
    """One Alertmanager matcher SET — the `matchers` list plus the deprecated `match` /
    `match_re` maps — evaluated against a label set. ALL of them must hold.

    Shared by the route simulation and the inhibition simulation below, which read the
    same three spellings: a second copy of this for inhibit rules could disagree with the
    one the routing rows use, and then the two halves of the same question ("does this
    config touch the derived space") would be answered by two different parsers."""
    if isinstance(entries, str):
        entries = [entries]
    for entry in entries or []:
        for expr in split_matchers(entry):
            if not one_matcher(expr, labels):
                return False
    for k, v in (match or {}).items():
        if not isinstance(v, str):
            raise Unparseable(f"{k}: {v!r}", "a non-string `match` value")
        if labels.get(k, "") != v:       # an absent label IS "" to Alertmanager's matcher
            return False
    for k, v in (match_re or {}).items():
        if not isinstance(v, str):
            raise Unparseable(f"{k}: {v!r}", "a non-string `match_re` value")
        if compile_re(v, f"{k} =~ {v}").fullmatch(labels.get(k, "")) is None:
            return False
    return True


def route_matches(node, labels):
    if not isinstance(node, dict):
        raise Unparseable(repr(node), "a route entry that is not a mapping")
    unknown = sorted(set(node) - ROUTE_KEYS)
    if unknown:
        raise Unparseable(repr(unknown), "route key(s) this simulation does not model")
    return matcher_set(node.get("matchers") or [], node.get("match"),
                       node.get("match_re"), labels)


_LABEL_KEY = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


def _entry_keys(entries, match, match_re):
    keys = set()
    if isinstance(entries, str):
        entries = [entries]
    try:
        for entry in entries or []:
            for expr in split_matchers(entry):
                m = MATCHER.match(expr)
                keys.add(m.group(1) if m else "(an unreadable matcher)")
    except Unparseable:
        keys.add("(an unreadable matcher)")
    for mp in (match, match_re):
        for k in (mp or {}) if isinstance(mp, dict) else ():
            keys.add(k if isinstance(k, str) and _LABEL_KEY.fullmatch(k) else "(a key not of a label name's shape)")
    return keys


def reachable_keys(cfg, labels):
    """The label names an alert with these (fixed) labels is tested on: every route up to
    and including the first terminal match at each level (a later sibling is never
    evaluated), plus the inhibit rules' TARGET matchers (an alert is suppressed only as a
    target; `equal` and the source side decide nothing about it). A route a derived label
    could have matched is among them, so a derived value on any of these keys is a
    delivery the fixed simulation cannot prove (round 25). Names come from git's config
    and pass the label-name grammar; anything else is described."""
    keys = set()

    def walk(node):
        for child in node.get("routes") or []:
            if not isinstance(child, dict):
                keys.add("(an unreadable matcher)"); continue
            keys.update(_entry_keys(child.get("matchers"), child.get("match"), child.get("match_re")))
            try:
                hit = route_matches(child, labels)
            except Unparseable:
                keys.add("(an unreadable matcher)"); continue
            if hit:
                walk(child)
                if not child.get("continue"):
                    break
    root = cfg.get("route")
    if isinstance(root, dict):
        keys.update(_entry_keys(root.get("matchers"), root.get("match"), root.get("match_re")))
        walk(root)
    for rule in cfg.get("inhibit_rules") or []:
        if not isinstance(rule, dict):
            continue
        keys.update(_entry_keys(rule.get("target_matchers"), rule.get("target_match"), rule.get("target_match_re")))
    return keys


def match_routes(node, labels, inherited=None, silenced=()):
    """Alertmanager's `Route.Match`, as it actually behaves.

    A node that matches descends into its children; the FIRST matching child wins and is
    terminal unless it sets `continue`. The node's own receiver is used only when NO child
    matched — appending it unconditionally is what made a legitimate `continue: true` look
    like a leak to the human receiver. A child with no `receiver` inherits its parent's.
    `mute_time_intervals` / `active_time_intervals` are the TERMINAL route's OWN: NewRoute
    assigns them from each child's declaration unconditionally (dispatch/route.go), so a
    child declaring none has none whatever its parent declares, and the dispatcher mutes
    on the route the alert landed on (round 18: inheriting the parent's was a false FAIL,
    and would have hidden nothing — but a simulator that is wrong in one direction is
    wrong). A route that matches perfectly and is muted delivers nothing."""
    if not route_matches(node, labels):
        return []
    receiver = node.get("receiver") or inherited
    silenced = tuple(node.get("mute_time_intervals") or []) \
             + tuple(node.get("active_time_intervals") or [])
    landed = []
    for child in (node.get("routes") or []):
        hit = match_routes(child, labels, receiver, silenced)
        landed.extend(hit)
        if hit and not child.get("continue"):
            break
    return landed or [(receiver, silenced)]


def validate_matcher_tree(cfg, mode):
    """Parse EVERY matcher in the config — route tree and inhibit rules — with no
    evaluation short-circuit: the simulation stops at the first miss in a matcher list
    and never descends into an unmatched parent, so a broken matcher behind either would
    otherwise pass unread while Alertmanager refuses the whole config."""
    def one_set(entries, match, match_re):
        if isinstance(entries, str):
            entries = [entries]
        for entry in entries or []:
            for expr in split_matchers(entry):
                one_matcher(expr, {})
        for k, v in (match or {}).items():
            if not isinstance(v, str):
                raise Unparseable(f"{k}: {v!r}", "a non-string `match` value")
        for k, v in (match_re or {}).items():
            if not isinstance(v, str):
                raise Unparseable(f"{k}: {v!r}", "a non-string `match_re` value")
            compile_re(v, f"{k} =~ {v}")
    def walk(node):
        if not isinstance(node, dict):
            raise Unparseable(repr(node), "a route entry that is not a mapping")
        unknown = sorted(set(node) - ROUTE_KEYS)
        if unknown:
            raise Unparseable(repr(unknown), "route key(s) this simulation does not model")
        one_set(node.get("matchers") or [], node.get("match"), node.get("match_re"))
        for child in (node.get("routes") or []):
            walk(child)
    try:
        walk(cfg.get("route") or {})
        root_route = cfg.get("route") or {}
        if isinstance(root_route, dict) and (root_route.get("mute_time_intervals") or root_route.get("active_time_intervals")):
            raise Unparseable("root", "a time interval on the root route (Alertmanager refuses that config, and it silences nothing)")
        rules, _ = inhibit_rules(cfg, mode)
        for r in rules:
            if not isinstance(r, dict):
                raise Unparseable(repr(r), "an inhibit rule that is not a mapping")
            for side in ("source", "target"):
                one_set(r.get(f"{side}_matchers") or [], r.get(f"{side}_match"), r.get(f"{side}_match_re"))
    except Unparseable as exc:
        abort(f"the Alertmanager config carries {exc.args[1]} (the matcher text is not repeated here: "
              f"a value may hold anything) — every matcher is parsed before any route is "
              f"simulated, because Alertmanager refuses the whole config over one it cannot read")


def delivery(cfg, labels):
    try:
        return match_routes(cfg["route"], labels)
    except Unparseable as exc:
        abort(f"the Alertmanager config carries {exc.args[1]} (the matcher text is not repeated here: "
              f"a value may hold anything) — this check "
              f"REFUSES to treat a matcher it cannot read as non-matching, because that "
              f"would answer 'the plane's route is reached' over a config half of which "
              f"was never evaluated. Fix the matcher, or teach this simulator its form.")


def receivers_for(cfg, labels):
    return [receiver for receiver, _ in delivery(cfg, labels)]


# The intake receiver is recognised by the WHOLE of its URL, read as a URL: the in-cluster
# service by name (bare, namespaced, or fully qualified), the intake's path, and nothing
# in front of the host. A URL that merely CONTAINS those words somewhere (in its path,
# in userinfo, in a foreign host's label) is not the intake.
REDACTED = "<secret>"                 # what /api/v2/status shows for a secret-typed value


import shown as display   # the shared display rule (scripts/verify/shown.py)
tokenish = display.tokenish


def shown_path(p):
    """A sealed file's path is tree text: each component is repeated only when it has
    the shape of a file name (round 21)."""
    return display.shown_parts(str(p))


def shown(names):
    """Receiver and interval names come from the config: a name-shaped one is repeated,
    the rest are counted. A diagnostic never carries text it did not validate."""
    good, note = display.shown_list(names, "name")
    return good + ([note] if note else [])


def label_shown(value):
    """An alertname or severity comes from the catalog or a hand-written matcher: shown
    only with a label value's shape, else counted as one that is not."""
    return display.shown(value, "label", "value")


def key_shown(key):
    """A Secret key from git (the bearer path's last element): shown with a key's shape and
    not a credential's; otherwise described (round 25)."""
    return repr(key) if display.fits(key, "key") else display.described("key", "key")
INTAKE_SERVICE = "orbit-agents-intake"
INTAKE_PATH = "/webhook/alertmanager"
# The intake as Alertmanager (in ns monitoring) can reach it: the service name WITH the
# plane's namespace — a bare name would resolve in Alertmanager's own namespace, and
# another namespace's service is another service — over plain http on the service port.
_INTAKE_HOST = re.compile(re.escape(INTAKE_SERVICE) + r"\." + r"(?P<ns>[a-z0-9]([a-z0-9-]*[a-z0-9])?)(\.svc(\.cluster\.local)?)?\.?")


def is_intake_url(url):
    if not isinstance(url, str):
        return False
    try:
        parts = urllib.parse.urlsplit(url)
    except ValueError:
        return False
    if parts.scheme not in ("http", "https") or parts.username is not None or parts.password is not None:
        return False
    host = (parts.hostname or "").lower()
    try:
        port = parts.port
    except ValueError:
        return False
    m = _INTAKE_HOST.fullmatch(host)
    return bool(m) and m.group("ns") == PLANE_NAMESPACE and parts.scheme == "http" \
        and port in (None, 80) and parts.path == INTAKE_PATH


# --- inhibition ---------------------------------------------------------------------
# FOUND LIVE (2026-08-29): routing is not the last thing that can stop an alert. A metric
# trigger declared `severity: info` was matched by the plane's route AND then held
# `state=suppressed` by kube-prometheus-stack's stock InfoInhibitor rule — the agent never
# ran, no notification failed, and every routing row above was green about it. Inhibition
# runs BEFORE the notification pipeline, so the plane's own safety nets (the intake 404,
# Alertmanager's notification-failure counter) cannot see it at all.
INHIBIT_KEYS = frozenset({"source_matchers", "source_match", "source_match_re",
                          "target_matchers", "target_match", "target_match_re", "equal"})
# The rules kube-prometheus-stack ships in its OWN values.yaml. They are not in an
# instance's Application unless the instance writes them there — and a values list REPLACES
# the chart default rather than merging with it, so "the file declares no inhibit_rules"
# means the running Alertmanager gets exactly these. Simulating an `app`-mode config that
# declares none as "nothing inhibits anything" is the fail-open answer, and is precisely the
# state the live finding was found in.
STOCK_INHIBIT_RULES = [
    {"source_matchers": ["severity = critical"],
     "target_matchers": ["severity =~ warning|info"],
     "equal": ["namespace", "alertname"]},
    {"source_matchers": ["severity = warning"],
     "target_matchers": ["severity = info"],
     "equal": ["namespace", "alertname"]},
    {"source_matchers": ["alertname = InfoInhibitor"],
     "target_matchers": ["severity = info"],
     "equal": ["namespace"]},
    {"target_matchers": ["alertname = InfoInhibitor"]},
]


def inhibit_rules(cfg, mode):
    """(rules, inherited) — what Alertmanager will actually run, and whether that came
    from the chart's defaults rather than from the file being checked."""
    declared = cfg.get("inhibit_rules")
    if declared is None and mode == "app":
        return STOCK_INHIBIT_RULES, True
    if declared is not None and not isinstance(declared, list):
        abort(f"`inhibit_rules` is {type(declared).__name__}, not a list — this check "
              f"cannot say what would be suppressed over a config it cannot read")
    return list(declared or []), False


def rule_name(i, rule):
    """How a red row names the rule that would suppress a work item: by INDEX. A matcher
    is config text and may hold anything (a value pasted where a label name was meant),
    so a row never repeats one."""
    return f"inhibit_rules[{i}]"


def suppresses(rule, labels):
    """Would this rule's TARGET side select this alert?

    Only the target side is simulated, deliberately. Whether a matching SOURCE alert is
    firing is a runtime fact no config can answer — and for the rule that actually fired
    live the source is InfoInhibitor, a synthetic alert whose whole purpose is to be
    firing in the namespace whenever an info alert is. Treating "I cannot prove a source
    exists" as "not suppressed" would have called the live config wired. A derived alert
    that ANY rule can target is the finding: machine work items are exempt from inhibition
    or they are not, and "exempt unless something louder happens to be firing" is not a
    property anyone can operate on."""
    if not isinstance(rule, dict):
        raise Unparseable(repr(rule), "an inhibit rule that is not a mapping")
    unknown = sorted(set(rule) - INHIBIT_KEYS)
    if unknown:
        raise Unparseable(repr(unknown),
                          "inhibit-rule key(s) this simulation does not model")
    return matcher_set(rule.get("target_matchers") or [], rule.get("target_match"),
                       rule.get("target_match_re"), labels)


def inhibitors(rules, labels):
    """Every rule that could suppress this alert, named."""
    try:
        return [rule_name(i, r) for i, r in enumerate(rules) if suppresses(r, labels)]
    except Unparseable as exc:
        abort(f"an inhibit rule carries {exc.args[1]} (the matcher text is not repeated here) — this check REFUSES "
              f"to treat a rule it cannot read as harmless, because that would answer "
              f"'the derived space is exempt from inhibition' over a rule it never "
              f"evaluated. Fix the rule, or teach this simulator its form.")


def sealed_secret_keys(path):
    """Index the Secrets a sealed tree renders: {(namespace, secret name): {key names}}.

    `path` is one sealed YAML file (possibly multi-document) or a directory of
    sealed files, as the desk applies them. The operator names each rendered
    Secret after its secretTemplate entry - never after the file - so the index
    is keyed by template name. Only NAMES are indexed: values never leave the
    file, and nothing that looks like ciphertext reaches the caller. Returns
    None when the path does not exist, so the caller can fail closed on it.
    """
    root = pathlib.Path(path)
    if not root.exists():
        return None
    files = [root] if root.is_file() else sorted(
        f for f in root.rglob("*") if f.is_file() and f.suffix in (".yaml", ".yml"))
    rendered, producers = {}, {}
    for seed in files:
        try:
            docs = [d for d in yaml.safe_load_all(open(seed)) if d]
        except yaml.YAMLError:
            check(f"sealed file {shown_path(seed)} does not parse, so its Secrets cannot be "
                  f"proven", False)
            continue
        for doc in docs:
            if not isinstance(doc, dict) or doc.get("kind") != "SopsSecret":
                continue
            # the operator renders every template into the SopsSecret's OWN namespace: a
            # Secret of the right name in another namespace is not what the pod mounts
            ns = (doc.get("metadata") or {}).get("namespace")
            if not isinstance(ns, str) or not ns:
                continue
            for template in ((doc.get("spec") or {}).get("secretTemplates") or []):
                if not isinstance(template, dict) or not template.get("name"):
                    continue
                key = (ns, template["name"])
                # ONE producer per rendered Secret: a second template naming it (in this
                # SopsSecret or another) is a second producer, and the operator applies
                # whichever came last — a union of their keys is a Secret nobody renders
                # (round 18); the name is dropped from the index, so its consumer fails closed
                if key in producers:
                    check(f"two sealed producers render Secret {shown([ns])[0]}/{shown([template['name']])[0]} "
                          f"({shown_path(producers[key])} and {shown_path(seed)}) — the operator applies ONE of them, never a union", False)
                    rendered.pop(key, None); continue
                producers[key] = seed
                # data: and stringData: are both key maps the operator renders
                for block in ("data", "stringData"):
                    if isinstance(template.get(block), dict):
                        rendered.setdefault(key, set()).update(template[block].keys())
                rendered.setdefault(key, set())
    return rendered


# A regex in a matcher is config text evaluated by Python's backtracking engine
# (Alertmanager's RE2 has no such cost). A pathological one must fail loudly, not hang
# the stage: the process gets a CPU budget and dies past it, and the caller reads a
# checker that printed no rows as "the checks did not run". AT IMPORT, not under
# __main__: the stage3 mutation harness imports this module and calls route_matches
# itself, and the budget must bind that process too.
def cpu_budget(seconds=60):
    try:
        import resource
        soft, hard = resource.getrlimit(resource.RLIMIT_CPU)
        want = seconds if hard in (resource.RLIM_INFINITY, -1) else min(seconds, hard)
        resource.setrlimit(resource.RLIMIT_CPU, (want, want))
    except (ImportError, ValueError, OSError):
        pass


cpu_budget()


if __name__ == "__main__":
    # --- the checker proper ---------------------------------------------------------
    argv = sys.argv[1:]
    def _opt(flag):
        return argv[argv.index(flag) + 1] if flag in argv else None
    catalog, secrets_file, desired = _opt("--catalog"), _opt("--secrets"), _opt("--desired")
    producers_optional = "--producers-optional" in argv      # core's template only; last on the line
    positional = [a for i, a in enumerate(argv)
                  if not a.startswith("--") and (i == 0 or not argv[i - 1].startswith("--"))]
    if len(positional) < 2:
        sys.exit(__doc__)
    mode, path = positional[0], positional[1]

    am = load_am(mode, path)
    cfg = am["config"]
    validate_matcher_tree(cfg, mode)
    receivers = {r["name"]: r for r in cfg.get("receivers") or []}
    # The intake receiver identifies itself by what it IS — a webhook at the intake's path —
    # not by its name, so renaming it cannot make this check pass vacuously.
    intake = [name for name, r in receivers.items()
              if any(is_intake_url(w.get("url"))
                     for w in (r.get("webhook_configs") or []))]
    if mode == "raw" and desired:
        # The RUNNING config (Alertmanager's /api/v2/status) redacts every secret-typed
        # value, the webhook URL among them (`<secret>` since v0.33), so the live intake
        # cannot show its URL. The git config can: its intake receiver, found by URL, gives
        # the NAME; the live receiver of that name is the intake, provided its webhook URL
        # is the intake URL or the redaction marker — anything else is a different door.
        want = load_am("app", desired)["config"]
        want_names = [r["name"] for r in want.get("receivers") or []
                      if any(is_intake_url((w or {}).get("url")) for w in (r.get("webhook_configs") or []))]
        if len(want_names) != 1:
            abort("the git config does not name exactly one intake receiver (a webhook at the "
                  "intake URL), so the running config's intake cannot be identified")
        live = receivers.get(want_names[0]) or {}
        urls = [(w or {}).get("url") for w in (live.get("webhook_configs") or [])]
        if urls and all(is_intake_url(u) or u == REDACTED for u in urls):
            intake = want_names[:1]
        check(f"the running config carries the git config's intake receiver, with its webhook "
              f"URL redacted or equal to the intake URL", intake == want_names[:1])
    intake_name = intake[0] if len(intake) == 1 else None
    # a receiver is a delivery only through an integration it carries: a receiver named
    # `telegram` with no telegram_configs delivers nothing, and a human "reached" by it is
    # not paged (round 20). Any *_configs block counts; the intake must carry ONLY its
    # webhook — a human notifier on it pages a human with a machine's work item.
    def integrations(r):
        return sorted(k for k, v in (r or {}).items() if isinstance(k, str) and k.endswith("_configs") and v)
    human = [n for n in receivers if n not in intake and n != "null" and integrations(receivers[n])]
    if intake_name is not None:
        extra = [k for k in integrations(receivers[intake_name]) if k != "webhook_configs"]
        check("the intake receiver carries no human notifier (its webhook and nothing else — a "
              f"notifier on it pages a human with a machine's work item; found: {shown(extra)})", not extra)

    # Catalog-derived facts, read FIRST: they decide what this config MUST carry, and the
    # alert label sets it must be simulated with.
    derived, declared, derivers = [], [], []
    if catalog:
        cat = load_yaml(catalog, "the catalog") or {}
        if not isinstance(cat, dict):
            abort(f"the catalog {catalog} is not a YAML map")
        for agent in (cat.get("agents") or []):
            if not isinstance(agent, dict) or agent.get("enabled", True) is False:
                continue
            aname = agent.get("name", "")
            for t in (agent.get("triggers") or []):
                if not isinstance(t, dict):
                    continue
                if t.get("type") in ("metric", "logs") and isinstance(t.get("name"), str):
                    # The alert this trigger will fire, with the labels catalog-sync gives it.
                    extra = {k: v for k, v in (t.get("labels") or {}).items()
                             if isinstance(k, str) and isinstance(v, str)}
                    derived.extend(variants(alert_labels(cs._agent_alertname(aname, t["name"]),
                                                         severity=t.get("severity", "warning"),
                                                         agent=aname, trigger=t["name"], extra=extra)))
                    # the alert keeps every label its series carries (namespace, pod, whatever
                    # the expression selects or label_replace adds) at values the catalog does
                    # not fix: only the rule's own labels are known here, and a route keyed on
                    # any other label may catch it (rounds 25, 26)
                    derivers.append((aname, t["name"], alert_labels(cs._agent_alertname(aname, t["name"]),
                                                                     severity=t.get("severity", "warning"),
                                                                     agent=aname, trigger=t["name"], extra=extra)))
                elif t.get("type") == "alert":
                    match = t.get("match") or {}
                    if isinstance(match.get("alertname"), str):
                        # `status` is the webhook payload's field, not an alert label.
                        declared.append({k: v for k, v in match.items()
                                         if isinstance(v, str) and k != "status"})

    # The plane's wiring rows ARM with the first metric/logs trigger — which is exactly what
    # ONBOARDING promises. Unconditional they went red on a pre-PR instance with ZERO agents
    # the moment it bumped core, with one unactionable row and nothing to do about it. Core's
    # TEMPLATE (no --catalog) is armed outright: a scaffolded orbit is born wired.
    armed = catalog is None or bool(derived)

    if armed or declared:
        # An alert-only catalog still needs a receiver to deliver to — its per-alertname
        # coverage row below is meaningless without one — but NOT the bearer/mount/secret
        # rows, which are the metric/logs wiring an alert-only instance never had to add.
        check("Alertmanager declares a receiver that webhooks the agent-plane intake",
              intake_name is not None)
    else:
        check("no metric/logs trigger declared — the intake receiver, the generic "
              f"{PREFIX}* route, the bearer file and its sealed producer arm with the first "
              "one (an alert trigger's own alertnames are asserted below either way)", True)

    if armed or declared:
        # Simulated with the COMPLETE label set a generated alert carries, at every severity
        # the generator will put on one: a route above the plane's keyed on `severity`,
        # `orbit_agent` or the plane's namespace steals agent alerts, and a two-label
        # simulation cannot see it. The probes exist only once the derived space is armed;
        # an alert-only catalog runs the interval and inhibition rows over its own alerts.
        probes = [v for a in PROBES for s in SEVERITIES for v in variants(alert_labels(a, severity=s))] if armed else []
        if armed:
            stolen = sorted({f"{lb['alertname']}/severity={lb['severity']} -> "
                             f"{receivers_for(cfg, lb)}"
                             for lb in probes if receivers_for(cfg, lb) != [intake_name]})
            check(f"every alertname in the derived {PREFIX}* space — simulated with the FULL "
                  f"label set a generated alert carries (severity, orbit_agent, orbit_trigger, "
                  f"namespace) — is routed to the intake receiver "
                  f"(misrouted: {shown(stolen[:4])})", not stolen)
            leaks = sorted({r for lb in probes for r in receivers_for(cfg, lb)} & set(human))
            check(f"...and does NOT also reach the human receiver (the route is terminal) "
                  f"(leaked to: {shown(leaks)})", not leaks)
        # over the probes AND the alerts the catalog fires: an interval on a child route
        # keyed on a label only a real trigger carries is invisible to the probes
        silenced = sorted({s for lb in probes + derived + declared for _, ss in delivery(cfg, lb) for s in ss})
        check(f"...and is not silenced by a mute/active time interval on the route it lands on "
              f"(intervals on the path: {shown(silenced)})", not silenced)
        # ...and is not SUPPRESSED before the notification pipeline ever sees it. The
        # severity sweep is what makes this row real: `severity: info` is a legal trigger
        # severity, and under the stock rules an info-severity derived alert is routed
        # perfectly and then held behind InfoInhibitor forever. Simulated over the rules
        # the running Alertmanager will actually have — the chart's own defaults when the
        # values declare none, which is the state the live finding was found in.
        rules, inherited = inhibit_rules(cfg, mode)
        # Over the synthetic probes AND every alert this catalog's triggers actually fire
        # (with the labels the generator gives them): a rule keyed on a label only a real
        # trigger carries is invisible to the probes.
        held = sorted({f"{label_shown(lb.get('alertname'))}/severity={label_shown(lb.get('severity'))} <- {r}"
                       for lb in probes + derived + declared for r in inhibitors(rules, lb)})
        check(f"...and is not SUPPRESSED by an inhibit rule before it is ever notified — "
              f"a machine work item is not human notification noise, so the derived space "
              f"must be exempt from every inhibit rule "
              f"({'the chart-default rules, none declared in this config; ' if inherited else ''}"
              f"suppressed: {held[:4]})", not held)
    else:
        check(f"no metric/logs trigger declared — the generic {PREFIX}* route requirement "
              "arms with the first one (its alertnames are asserted below either way)", True)

    # Placement, asserted by behaviour rather than by index: the plane's route must sit BELOW
    # the Watchdog null-route, and must not shadow anything else.
    check("Watchdog still null-routes (the plane's route sits below it)",
          receivers_for(cfg, {"alertname": "Watchdog"}) == ["null"])
    check("an ordinary platform alert still falls through to the default (human) receiver, "
          "and that receiver carries a notifier (a receiver with no *_configs delivers nothing)",
          receivers_for(cfg, {"alertname": "KubeNodeNotReady", "severity": "warning",
                              "namespace": "kube-system"}) == [cfg["route"].get("receiver")]
          and cfg["route"].get("receiver") in human)

    if armed or declared:
        # Every row below is actionable on a pre-PR instance that just declared its first
        # trigger of any kind — so they are all EMITTED, rather than exiting at the first
        # one. An alert-only catalog delivers through the same receiver, with the same
        # bearer (the intake authenticates every Alertmanager request), so the receiver's
        # URL, resolve setting and credential rows run for it too.
        r = receivers.get(intake_name) or {}
        # Exactly ONE webhook entry: every row below reads entry [0], and a second entry
        # would post the same alerts with its own URL, resolve setting and credential
        # that nothing here checks.
        check("the intake receiver carries exactly one webhook entry (the rows below read it)",
              len(r.get("webhook_configs") or []) == 1)
        web = (r.get("webhook_configs") or [{}])[0]
        check("the intake receiver posts to the plane's intake service (the in-cluster "
              f"service {INTAKE_SERVICE}, path {INTAKE_PATH}, no userinfo; the whole URL "
              "is read, not a substring of it" + ("; the running config may redact it" if mode == "raw" else "") + ")",
              is_intake_url(web.get("url")) or (mode == "raw" and desired and web.get("url") == REDACTED))
        check("send_resolved is false (a resolved POST is an unrouted 404 at the intake)",
              web.get("send_resolved") is False)
        auth = ((web.get("http_config") or {}).get("authorization") or {})
        check("the intake bearer is read from a FILE, never inlined in the config Secret",
              bool(auth.get("credentials_file")) and "credentials" not in auth)
        # The intake accepts `Authorization: Bearer <token>` and nothing else; Alertmanager
        # defaults the scheme to Bearer when `type` is omitted, and any other spelling
        # (Token, Basic, a typo) is a 401 at the door with a correct file mounted.
        check("the intake authorization scheme is Bearer (the one the intake accepts; omitted = Bearer)",
              (auth.get("type") or "Bearer") == "Bearer")
        if mode == "app":
            secrets = ((am.get("alertmanagerSpec") or {}).get("secrets") or [])
            mount = (auth.get("credentials_file") or "").split("/")
            # The WHOLE path, against the mount contract: exactly
            # /etc/alertmanager/secrets/<Secret name>/<key>, one Secret name (a Kubernetes
            # object name) and one key, nothing before, between or after — an extra
            # directory or a dot segment would read a file the mount does not serve.
            check("the bearer path is /etc/alertmanager/secrets/<Secret>/<key> exactly, and "
                  "Alertmanager mounts that Secret",
                  len(mount) == 6 and mount[:4] == ["", "etc", "alertmanager", "secrets"]
                  and bool(re.fullmatch(r"[a-z0-9]([-a-z0-9]*[a-z0-9])?(\.[a-z0-9]([-a-z0-9]*[a-z0-9])?)*", mount[4]))
                  and bool(re.fullmatch(r"[A-Za-z0-9._-]{1,253}", mount[5])) and mount[5] not in (".", "..")
                  and mount[4] in secrets)

    # Coverage: every alertname THIS instance's agents actually trigger on must reach the
    # intake — derived ones (from the generator's own derivation, so the two cannot drift) and
    # hand-written `alert` matchers alike, each simulated with the labels its own alert carries.
    # This is what goes red the moment a metric/logs trigger is declared on an instance whose
    # Alertmanager was never given the route.
    wanted = derived + declared
    unrouted = [f"{label_shown(lb.get('alertname'))} -> {shown(receivers_for(cfg, lb))}"
                for lb in wanted if receivers_for(cfg, lb) != [intake_name]]
    if wanted:
        check("every alertname this catalog's triggers fire on reaches the intake "
              f"(unrouted: {unrouted})", not unrouted)
    for aname, tname, fixed in derivers:
        free = sorted(reachable_keys(cfg, fixed) - set(fixed))
        check(f"trigger {label_shown(aname)}/{label_shown(tname)}: a route on its alert's way, or an "
              f"inhibit rule's target side, tests label(s) {free} the rule does not fix — the alert "
              f"keeps its series' value there (whatever the expression selects or label_replace adds), "
              f"which this simulation cannot know: set those labels in the trigger's labels (a rule "
              f"label overrides the series'), or move that route below the intake route", not free)

    # The credential Alertmanager presents to the intake: every name on the `secrets:`
    # list under alertmanagerSpec is mounted into the pod, and a mount with no producer
    # is an Alertmanager that will not start. On the sealed plane the producer is a
    # SopsSecret the desk applies - one rendered Secret per entry in
    # spec.secretTemplates, named by the TEMPLATE, never by the file. Key NAMES only
    # below, never a value and never anything that looks like ciphertext: stdout here
    # is a CI log.
    if secrets_file and (armed or declared):
        rendered = sealed_secret_keys(secrets_file)
        if rendered is None:
            check(f"the sealed secrets path {secrets_file} does not exist, so the "
                  f"mounted Secrets cannot be proven", False)
        else:
            # What Alertmanager mounts is what must exist: derived from the values,
            # never hard-coded, so a mount added tomorrow is checked tomorrow.
            declared_mounts = list(((am.get("alertmanagerSpec") or {}).get("secrets") or [])) \
                if mode == "app" else []
            # a mount entry is values text: it names a Secret only with a Secret name's
            # shape, and anything else is refused by position, never repeated
            mounts = []
            for mi, m in enumerate(declared_mounts):
                if isinstance(m, str) and re.fullmatch(r"[a-z0-9]([-a-z0-9]*[a-z0-9])?(\.[a-z0-9]([-a-z0-9]*[a-z0-9])?)*", m) and len(m) <= 253:
                    mounts.append(m)
                else:
                    check(f"alertmanagerSpec.secrets[{mi}] is a Secret name (the value is not of "
                          f"a Secret name's shape and is not repeated)", False)
            # The intake token's Secret, read from the receiver the rows above already
            # required - from the credentials_file path, not from a hard-coded name.
            bearer_path = ((((receivers.get(intake_name) or {})
                              .get("webhook_configs") or [{}])[0]
                            .get("http_config") or {}).get("authorization") or {}) \
                .get("credentials_file", "").split("/")
            # Secret and key are read ONLY from a path that is the whole mount contract
            # (/etc/alertmanager/secrets/<Secret name>/<key>); a path that failed it above
            # names nothing here, so its text never reaches a row.
            bearer_secret = (bearer_path[4] if len(bearer_path) == 6
                             and bearer_path[:4] == ["", "etc", "alertmanager", "secrets"]
                             and re.fullmatch(r"[a-z0-9]([-a-z0-9]*[a-z0-9])?(\.[a-z0-9]([-a-z0-9]*[a-z0-9])?)*", bearer_path[4])
                             and re.fullmatch(r"[A-Za-z0-9._-]{1,253}", bearer_path[5]) and bearer_path[5] not in (".", "..")
                             else None)
            if not rendered and producers_optional:
                # Core's own TEMPLATE ships no producer at all (the caller says so with
                # --producers-optional): the rows KEY OFF with the reason, and the run
                # stays green. An INSTANCE's empty index is a missing producer, and the
                # rows below go red on it: its mounts must be rendered by its sealed tree.
                for name in mounts:
                    check(f"mounted Secret {name} is rendered by a sealed file: this "
                          f"tree ships no SopsSecret producer, the sealed (sops) plane "
                          f"provides it elsewhere, so this row keys off", True)
                check("the intake token: this tree ships no sealed producer, the "
                      "sealed (sops) plane provides it elsewhere, so this row keys off",
                      True)
            else:
                for name in mounts:
                    check(f"mounted Secret {name} is rendered by a sealed SopsSecret in {AM_NAMESPACE} "
                          f"(the namespace Alertmanager mounts from)", (AM_NAMESPACE, name) in rendered)
                if bearer_secret is not None:
                    # The key is the last path element of credentials_file: that is the
                    # file Alertmanager reads, and the Secret key the mount serves it from.
                    bearer_key = bearer_path[-1]
                    check(f"the intake token's Secret {bearer_secret} seals the key "
                          f"{key_shown(bearer_key)}, the file the bearer path names",
                          bool(bearer_key) and bearer_key in rendered.get((AM_NAMESPACE, bearer_secret), ()))


    finish()
