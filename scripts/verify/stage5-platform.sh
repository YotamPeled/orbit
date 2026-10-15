#!/usr/bin/env bash
# Stage 5-platform — the planes CORE ships, and nothing an org happens to run on them.
#
# 2026-09-07, ROADMAP C2 item 1c. core-manifest.txt vendored stages 2-4 only, so every
# platform artifact under template/ — the observability stack, the blackbox probes, the
# alert path, the five NetworkPolicies, the scrape collectors, the image
# renderer, the restart alert and the webhook doors — arrived at a new org with NO
# ORACLE THAT CHECKS IT. METHOD.md
# principle 1 makes the oracle the load-bearing piece, so an artifact shipped without one
# is an artifact nobody can prove works. This stage is that oracle, extracted from the
# reference instance's stage6 (the only place these checks existed) and parameterised so it
# carries no instance's hostname, tenant, namespace or id.
#
# WHAT IS AND IS NOT HERE. Every row below asserts something `template/` ACTUALLY SHIPS.
# Tenant assertions — "this project's service is scraped", "this app's dashboard has
# data", "this domain still resolves to the cluster" — stay in the instance's own stages,
# because core cannot know them and a check that names one is a check that fails for
# every other org.
#
# THREE WAYS A ROW KEYS OFF, and each says so out loud rather than vanishing:
#   * PRE-BOOT — no kubeconfig FILE (the stage3 convention): the instance was scaffolded
#     and never bootstrapped, so every LIVE row keys off with a visible zero. Keyed on the
#     file's existence, never on reachability: a bootstrapped instance whose cluster is
#     down still fails loudly, and deleting the kubeconfig to silence the suite is
#     self-announcing.
#   * A CLOSED GATE — the same two gates scripts/instance-init.py renders by (bucket,
#     intake). A gate deletes the artifact, so asserting it would be asserting a
#     file the org's own config took away. Gate CLOSED is a keyed-off row; gate OPEN with
#     the artifact missing is RED.
#   * NOT DEPLOYED YET — an optional component's stored inventory does not exist (the desk
#     has not applied its app). This is a declaration (applied intent), not a reachability probe.
# "Could not ask" is NEVER one of them. An unanswerable question is red, because an
# unreadable answer and a wrong answer look identical from the outside.
set -uo pipefail
cd "$(dirname "$0")/../.."
# the python blocks below import the shared display rule (scripts/verify/shown.py)
export PYTHONPATH="$(pwd)/scripts/verify${PYTHONPATH:+:$PYTHONPATH}"
. "$(dirname "$0")/../lib-instance.sh" || exit 2
export KUBECONFIG="${KUBECONFIG:-$HOME/.kube/config-${ORBIT_INSTANCE_NAME}-platform}"
MON=monitoring
PASS=0; FAIL=0
ok(){  echo "  ok    $1"; PASS=$((PASS+1)); }
bad(){ echo "  FAIL  $1"; FAIL=$((FAIL+1)); }

TMP=$(mktemp -d); trap 'rm -rf "$TMP"' EXIT

# Pre-boot, exactly as stage3 declares it.
PREBOOT=0
# KUBECONFIG is a LIST (colon-separated) the way kubectl reads it: pre-boot means no
# entry is a readable non-empty file. An entry that exists but is empty or unreadable is
# neither a cluster nor pre-boot, and the stage refuses rather than guess.
kubeconfig_present(){ local f any=1; set -f; IFS=: ; for f in $1; do IFS=$' \t\n'   # set -f: the list is split, never glob-expanded
    if [ -s "$f" ] && [ -r "$f" ]; then any=0
    elif [ -e "$f" ]; then echo "kubeconfig $f exists but is empty or unreadable — neither a cluster nor pre-boot; fix or remove it" >&2; exit 2; fi
  done; IFS=$' \t\n'; set +f; return $any; }
kubeconfig_present "$KUBECONFIG" || PREBOOT=1

echo "== the instance's own declarations =="
# The two gates, read from the SAME config keys scripts/instance-init.py gates the
# staged tree by (its GATES table). Read here rather than re-derived from the tree,
# because "is the file there" cannot tell a gate the org closed from a file somebody
# deleted by accident — the config is what declares the intent.
# The optional features (logs, traces, ...) are read the same way, through the one module
# every reader of `features:` uses (scripts/verify/orbit_features.py) against the registry
# vendored with the staged tree. FEATURE_<NAME> is 1 when the feature is on. A tree
# vendored before the registry existed has none: logs and traces then follow the bucket,
# which is how that tree rendered them.
GATE_BUCKET=0; GATE_INTAKE=0; FEATURE_LOGS=0; FEATURE_TRACES=0; FEATURE_DASHBOARDS=1; FEATURES_BAD=0; FEATURE_APPS=""; FEATURE_SUMMARY=""
# what a tree vendored before these were features always had: the prober and the database
# operator; bucket backups wherever there was a bucket
FEATURE_UPTIME=1; FEATURE_DATABASES=1; FEATURE_BACKUPS=0; FEATURE_ALERTS_TELEGRAM=1
GATEOUT=$(python3 - "${ORBIT_INSTANCE_CONFIG:-instance/config.yaml}" <<'PY' 2>/dev/null
import re, sys, yaml
c = yaml.safe_load(open(sys.argv[1])) or {}
obj = c.get("objectStorage") or {}
bucket = bool((obj.get("buckets") or {}).get("platform"))
print("GATE_BUCKET=%d" % bucket)
print("GATE_INTAKE=%d" % (c.get("intakeIngress") == "wired"))
try:
    import orbit_features
    registry = orbit_features.load_registry(".")
except Exception:          # no registry in this tree (or no module): the pre-features reading
    print("FEATURE_LOGS=%d" % bucket); print("FEATURE_TRACES=%d" % bucket); print("FEATURE_BACKUPS=%d" % bucket)
    print("FEATURE_SUMMARY='logs=%d traces=%d (this tree predates the features registry)'" % (bucket, bucket))
    sys.exit(0)
try:
    on = orbit_features.states(c, registry)
except orbit_features.FeatureError:
    print("FEATURES_BAD=1"); sys.exit(0)
apps, summary = [], []
for name, spec in registry.items():
    if spec["always"]:
        continue
    print("FEATURE_%s=%d" % (name.upper().replace("-", "_"), on[name]))
    summary.append("%s=%d" % (name, on[name]))
    apps += ["%s:%s:%d" % (name, app, on[name]) for app in spec["apps"]]
print("FEATURE_SUMMARY='%s'" % " ".join(s for s in summary if re.fullmatch(r"[a-z0-9=-]+", s)))
# feature:app:on triples; a word that is not of that shape is dropped, never printed
print("FEATURE_APPS='%s'" % " ".join(a for a in apps if re.fullmatch(r"[a-z0-9:-]+", a)))
PY
)
if [ -z "$GATEOUT" ]; then
  bad "could not read the gates out of instance/config.yaml — every gated row below keys off for the wrong reason (gate CLOSED is the fail-closed default), so this row alone carries the verdict and the rows that follow are keyed-off, not passing"
else
  eval "$GATEOUT"
  ok "gates read from instance/config.yaml: bucket=$GATE_BUCKET intake=$GATE_INTAKE (the same set scripts/instance-init.py renders the staged tree by)"
  if [ "$FEATURES_BAD" = 1 ]; then
    bad "instance/config.yaml's features: block cannot be read against provisioning/features.yaml (an unknown feature, a value that is not true or false, or a feature turned on without what it requires) — make init says which; every feature row below keys off for the wrong reason"
  else
    ok "features read from instance/config.yaml: $FEATURE_SUMMARY (scripts/verify/orbit_features.py, the reading the scaffolder, the generator and orbitd share)"
  fi
fi

if [ "$PREBOOT" = 1 ]; then
  echo "  note  PRE-BOOT: no kubeconfig at $KUBECONFIG — every live row below keys off visibly; bootstrap arms them"
fi

# ── is there a cluster at all ─────────────────────────────────────────────────────────
# ONE reachability probe, hoisted, asked once, reused everywhere. `kubectl get` fails
# IDENTICALLY (rc != 0) whether the object is genuinely absent, the API server is
# unreachable, or RBAC forbids the read — so every "the component is not deployed, so
# these rows key off" decision in this file, keyed on such an rc, turns a cluster that is
# merely DOWN into an org that merely installed nothing: a whole suite of silent green
# with a kubeconfig sitting right there. That is the rule for the file:
# The kube-system namespace is what every cluster has, so its readability is the
# cheapest honest answer to "did the cluster answer".
ICLUSTER=0
if [ "$PREBOOT" != 1 ]; then
  # BOUNDED and NON-INTERACTIVE, both deliberately. This one call decides whether every
  # live row below runs, so it must always come back: `--request-timeout` stops a
  # black-holed endpoint from hanging the suite, and `</dev/null` stops a kubeconfig whose
  # user carries no usable credential from PROMPTING on stdin and blocking forever (a
  # verify stage runs unattended in CI; a prompt there is an outage of the checker, not of
  # the cluster). Either way the answer is "the cluster did not answer", which is the
  # answer this probe exists to give.
  kubectl --request-timeout=20s get namespace kube-system >/dev/null 2>&1 </dev/null && ICLUSTER=1
  if [ "$ICLUSTER" = 1 ]; then
    ok "the cluster answers (namespace kube-system is readable) — so every 'not deployed' below is a real ABSENCE read off a reachable API server, not a failed question"
  else
    bad "a kubeconfig exists at $KUBECONFIG but the cluster did not answer (namespace kube-system is unreadable) — THIS is the row that says so, once and loudly; every row below that would have keyed off on 'not deployed' keys off on this instead, because an unreachable API server and an uninstalled component are indistinguishable from out here"
  fi
fi

# keyoff_absent <row text> — the ONLY permitted spelling of "the component is not deployed,
# so its rows key off". It refuses to call an unanswerable question an absence: with the
# cluster unreachable the row still keys off (the reachability FAIL above already carries
# the verdict — one loud row, not one per component), but it says WHY, so nobody reads a
# down cluster's output as an inventory.
keyoff_absent(){
  if [ "$ICLUSTER" = 1 ]; then
    ok "$1"
  else
    ok "UNREACHABLE: $1 — keyed off because the cluster did not ANSWER, not because the component is absent; the reachability FAIL above is what carries this"
  fi
}

# ── one mode for "no live row can run" ────────────────────────────────────────────────
# There are exactly TWO reasons a live row cannot run, and they are told apart because an
# operator acts differently on each: PRE-BOOT (no kubeconfig file — the org was scaffolded
# and never bootstrapped, which is a normal state) and UNREACHABLE (a kubeconfig is there
# and the cluster did not answer, which is an incident). What they have in common is that
# NOTHING live can be asked, so both key the live rows off through this one flag.
#
# Round 2, Sol: before this, only the "not deployed" decisions consulted ICLUSTER. Every
# other live section asked its own question anyway and reported the silence as a broken
# component — a kubeconfig pointing at a dead endpoint produced 38 FAIL lines, of which 37
# said things like "Prometheus is not ready" about a cluster nobody could reach. That is
# not a verdict, it is noise around the one line that matters, and an operator reading it
# starts debugging Prometheus. One cluster, one FAIL. Every static row still runs, because
# none of them needs the cluster.
LIVEOFF=0
[ "$PREBOOT" = 1 ] && LIVEOFF=1
[ "$PREBOOT" != 1 ] && [ "$ICLUSTER" != 1 ] && LIVEOFF=1

# keyoff_live <what keys off> — the ONLY spelling of a live row keying off, so pre-boot and
# unreachable can never be worded into each other.
keyoff_live(){
  if [ "$PREBOOT" = 1 ]; then
    ok "PRE-BOOT: $1 — they arm from the kubeconfig FILE, which does not exist yet"
  else
    ok "UNREACHABLE: $1 — the kubeconfig exists and the cluster did not answer, so this is keyed off, NOT passing; the single reachability FAIL above is the whole verdict, and one dead cluster is not dozens of broken components"
  fi
}

# ── talking to the stack ──────────────────────────────────────────────────────────────
# Through the API server's service proxy rather than a port-forward: a port-forward that
# dies mid-run turns real answers into flaky ones.
promq(){  kubectl get --raw "/api/v1/namespaces/$MON/services/kube-prometheus-stack-prometheus:9090/proxy/api/v1/query?query=$(python3 -c 'import urllib.parse,sys;print(urllib.parse.quote(sys.argv[1]))' "$1")" 2>/dev/null; }
promraw(){ kubectl get --raw "/api/v1/namespaces/$MON/services/kube-prometheus-stack-prometheus:9090/proxy$1" 2>/dev/null; }
amraw(){   kubectl get --raw "/api/v1/namespaces/$MON/services/kube-prometheus-stack-alertmanager:9093/proxy$1" 2>/dev/null; }
first(){ python3 -c "import json,sys;r=json.load(sys.stdin).get('data',{}).get('result',[]);print(r[0]['value'][1] if r else '')"; }
# promval keeps three answers apart that `first` collapses onto '': a real value, the
# token ABSENT (the query ran and matched no series) and QUERYERR (the call failed, or
# Prometheus did not answer status:success). An empty result silently read as a passing
# zero is the failure this exists to make impossible.
promval(){ promq "$1" | python3 -c '
import json,sys
try: d=json.load(sys.stdin)
except Exception: print("QUERYERR"); sys.exit()
if d.get("status")!="success": print("QUERYERR"); sys.exit()
import re
try: r=d["data"]["result"]
except Exception: print("QUERYERR"); sys.exit()
if not r: print("ABSENT"); sys.exit()
try: v=str(r[0]["value"][1])
except Exception: print("QUERYERR"); sys.exit()
# a sample value is printed in rows: only a NUMBER (Prometheus float syntax) is one
print(v if re.fullmatch(r"[+-]?(\d+(\.\d*)?|\.\d+)([eE][+-]?\d+)?|[+-]?Inf|NaN", v) else "QUERYERR")'; }

GRAF="https://grafana.$ORBIT_DOMAIN"
GRAFPW_FILE="$ORBIT_CONFIG_DIR/grafana-admin-password"
HAVEPW=0
[ -r "$GRAFPW_FILE" ] && HAVEPW=1
# `-q` FIRST on every credential-bearing curl: curl reads ~/.curlrc (or CURL_HOME) before
# `-K -`, and a `trace-ascii` there would log the Authorization header (round 18)
# The password reaches curl as a config document on its stdin (`-K -`), never as an
# argument (a process's argv is readable by every user on the host, its stdin is not) and
# never as a shell value (a traced shell, `bash -x`, prints every assignment and every
# expanded word to stderr). A Python process reads the FILE, whose path is its only
# argument, and writes the `user = "..."` line with the two escapes curl's quoted config
# syntax reads (backslash, double quote); the shell only ever holds the path.
curluser(){ python3 -c 'import sys;v=open(sys.argv[1]).read().replace("\n","").replace("\r","");v=v.replace("\\","\\\\").replace("\"","\\\"");print("user = \"%s:%s\"" % (sys.argv[2], v))' "$1" "$2"; }
# The credential-bearing requests are PINNED to this instance's ingress (its control-plane
# address, from the loader) and bypass any proxy: DNS for grafana.<domain> is the
# public internet's answer, and a request carrying the admin password must never follow
# it elsewhere. Without the pin the request is not sent at all.
GRAFPIN=()
[ -n "${ORBIT_CONTROL_PLANE_IP:-}" ] && GRAFPIN=(--noproxy '*' --resolve "grafana.$ORBIT_DOMAIN:443:$ORBIT_CONTROL_PLANE_IP")
grafpinned(){ [ "${#GRAFPIN[@]}" -gt 0 ] || { echo "grafana: no control-plane address to pin the authenticated request to — not sent" >&2; return 1; }; }
grafauth(){ curluser "$GRAFPW_FILE" admin; }
# curlq writes a NON-secret value (a query) as curl's quoted config syntax wants it.
curlq(){ local v=${1//\\/\\\\}; v=${v//\"/\\\"}; printf '%s' "$v"; }
# grafapi <path> [curl args...] -> the body, authenticated as the local admin.
grafapi(){ local p=$1; shift; grafpinned || return 1; grafauth | curl -q -s --max-time 25 -K - "${GRAFPIN[@]}" "$@" "$GRAF$p"; }

# The namespaces the CATALOG manages, read by the SHAPE of the label the generator
# stamps rather than by its literal key: scripts/catalog-sync.py writes
# `<label-prefix>/project`. The prefix was an ORG-shaped string when this was written (a
# leak this file must not carry) and is `orbit.local/` since 2026-09-07; matching on the
# SUFFIX meant the rename reached this file as no edit at all, and it will survive the
# next one too. Any namespace holding a `.../project` label is a tenant namespace.
# marker <NAME> <file>: the value after NAME= on the file's last such line, shaped inside
# the pipeline — a three-digit status, an empty value (a missing marker), or `unreadable`;
# the raw text never sits in a shell variable (a traced shell prints assignments)
marker(){ sed -n "s/^$1=//p" "$2" 2>/dev/null | tr -d '\r' | tail -1 | awk '/^[0-9][0-9][0-9]$/{print; f=1; next} {print "unreadable"; f=1} END{if (!f) printf ""}'; }
project_namespaces(){ # prints the project namespaces (names of a namespace shape only;
  # the rest are counted in $TMP/ns.odd); rc 1 when they could not be listed
  kubectl get ns -o json 2>/dev/null | python3 -c '
import json,re,sys
try: items=json.load(sys.stdin).get("items",[])
except Exception: sys.exit(1)
NS = re.compile(r"[a-z0-9]([-a-z0-9]{0,61}[a-z0-9])?")
odd = 0
for ns in items:
    labels=(ns.get("metadata") or {}).get("labels") or {}
    if any(k.endswith("/project") for k in labels):
        n = (ns.get("metadata") or {}).get("name")
        if isinstance(n, str) and NS.fullmatch(n): print(n)
        else: odd += 1
open(sys.argv[1], "w").write(str(odd))' "${TMP:-/nonexistent}/ns.odd"
}
ns_odd(){ cat "${TMP:-/nonexistent}/ns.odd" 2>/dev/null || echo 0; }
# cani <kubectl auth can-i args>: the answer as yes / no / unreadable, shaped inside the
# pipeline — the server's text never sits in a shell variable
cani(){ kubectl auth can-i "$@" 2>/dev/null | awk 'NR==1 && /^(yes|no)$/{print; f=1} END{if (!f) print "unreadable"}'; }

echo "== the artifacts this org's own gates say it must have, asked of git alone =="
# WHY THIS SECTION EXISTS AT ALL (round 1, Sol 4 / Muse 3). Almost every static assertion
# in this file used to sit INSIDE a live branch — "the component is deployed, so check
# its values". On a fresh scaffold,
# which is the ONLY mode a new org runs first, none of those branches is taken: deleting
# a bucket-gated loki.yaml with the bucket declared and unquoting a `refreshInterval`
# both left a green run. A question
# about GIT has the same answer before bootstrap as after it, so it is asked here,
# unconditionally, and every row below this line that is still inside a live branch is
# a question about the CLUSTER.

# --- the gated tree, derived from the SAME table that renders it ---------------------
# Not a hand-copied list of paths: scripts/instance-init.py's GATES is imported and its
# own gates_for() is called, so a gate added or moved there cannot drift from the rows
# that police it. core-manifest.txt vendors that script, so every org has it. The rule is
# exact in both directions — a path whose gates are all OPEN must be rendered, a path
# with any gate CLOSED must be gone (a leftover is an app the desk can never apply),
# and a staged path in no gate at all is unconditional.
GATED=$(python3 - "$GATE_BUCKET" "$GATE_INTAKE" "${ORBIT_INSTANCE_CONFIG:-instance/config.yaml}" <<'PY'
import importlib.util, os, sys, yaml
spec = importlib.util.spec_from_file_location("orbit_instance_init", "scripts/instance-init.py")
if spec is None or spec.loader is None:
    print("FAIL scripts/instance-init.py is not importable — this instance cannot render its "
          "own staged tree, and the gate table these rows are derived from does not exist")
    sys.exit(1)
mod = importlib.util.module_from_spec(spec)
try:
    spec.loader.exec_module(mod)
except Exception as e:
    print("FAIL scripts/instance-init.py did not import (%s) — the gate table these rows are "
          "derived from cannot be read" % e.__class__.__name__); sys.exit(1)
# THREE GATES NOW. The secret plane is sops on every org: no vault is rendered and no
# operator reads one, so the `vault`, `cloudvault` and `eso` gates the scaffolder
# still carries are CLOSED here — a leftover vault or operator artifact is red, not
# grandfathered in.
open_gates = {"bucket": sys.argv[1] == "1",
              "intake": sys.argv[2] == "1", "vault": False,
              "cloudvault": False, "eso": False}
# A scaffolder that knows features gates each one's paths by its name; the states are
# its own gates() answer for this instance's config, so this row and `make init` cannot
# read `features:` differently.
if hasattr(mod, "FEATURES"):
    if getattr(mod, "FEATURES_ERROR", None):
        print("FAIL the features registry cannot be read (provisioning/features.yaml missing or "
              "malformed) — `make init` refuses this tree, so which staged files should be "
              "rendered cannot be decided. Catch the template up (`make core-sync`)"); sys.exit(1)
    try:
        cfg = yaml.safe_load(open(sys.argv[3], encoding="utf-8")) or {}
        open_gates.update(mod.gates(cfg))
    except SystemExit:
        print("FAIL the scaffolder refuses this instance's config (`make init` says why), so "
              "which staged files should be rendered cannot be decided"); sys.exit(1)
    except Exception as e:
        print("FAIL instance/config.yaml could not be read for the feature gates (%s)" % e.__class__.__name__); sys.exit(1)
staged = "provisioning/clusters"
# WHICH SCAFFOLDER DOES THIS INSTANCE ACTUALLY RUN. Staged rendering (provisioning/clusters
# -> clusters/, gated per path) arrived after some instances were cut, and an instance
# vendors core's scripts and catches its TEMPLATE up separately (`make core-sync` + `make init`).
# So the two states have to be told apart, and the SCRIPT is what tells them:
#   * no gate mechanism in the vendored scaffolder -> this instance predates staged
#     rendering, there is no staged tree to check, and the row keys off. It arms by itself
#     the moment core-sync brings the newer scaffolder in.
#   * the mechanism IS there and provisioning/clusters is not -> `make init` REFUSES to run
#     at all on this repo (it exits on exactly this condition), so the org cannot regenerate
#     its cluster tree. Red, with the fix named.
if not (hasattr(mod, "GATES") and
        (hasattr(mod, "gates_for") or hasattr(mod, "gate_for"))):
    print("KEYOFF this instance's vendored scripts/instance-init.py predates staged rendering "
          "(it carries no GATES table), so there is no provisioning/clusters tree for this row "
          "to police; it arms the moment core-sync brings the newer scaffolder in")
    sys.exit(0)
# gates_for() names every gate on a path (a file can sit under two); gate_for() is the
# older single-gate spelling a vendored scaffolder may still carry.
resolve = getattr(mod, "gates_for", None)
if resolve is None:
    def resolve(rel, _single=mod.gate_for):
        gate = _single(rel)
        return [] if gate is None else [gate]
if not os.path.isdir(staged):
    print("FAIL the vendored scripts/instance-init.py renders from %s and this repo has no such "
          "directory — `make init` exits on exactly this condition, so this org cannot "
          "regenerate its cluster tree at all. Catch the template up (`make core-sync`, then `make init`)"
          % staged)
    sys.exit(1)
import shown as display   # the shared display rule (scripts/verify/shown.py)
def shown_rel(rel):
    return display.shown_parts(rel.replace(os.sep, "/"))
bad, checked, off = [], 0, 0
for root, dirs, files in os.walk(staged):
    dirs.sort()
    for fn in sorted(files):
        if not fn.endswith((".yaml", ".yml")):
            continue
        rel = os.path.relpath(os.path.join(root, fn), staged)
        shown = shown_rel(rel)             # a staged file's name is tree text (round 20)
        needed = resolve(rel)
        closed = [g for g in needed if not open_gates.get(g, False)]
        dst = os.path.join("clusters", rel)
        if closed:
            off += 1
            if os.path.exists(dst):
                bad.append("clusters/%s is present although its %s gate is CLOSED — `make init` "
                           "deletes it, so this is a hand-restored Application the org's own "
                           "config says it cannot run" % (shown, "+".join(repr(g) for g in closed)))
            continue
        checked += 1
        if not os.path.exists(dst):
            bad.append("clusters/%s is MISSING although %s — the artifact this org asked for was "
                       "never rendered, or was deleted after it was"
                       % (shown, "its %s gate is OPEN" % "+".join(repr(g) for g in needed) if needed else "it is ungated (every org gets it)"))
            continue
        try:
            list(yaml.safe_load_all(open(dst, encoding="utf-8")))
        except Exception as e:
            bad.append("clusters/%s does not parse as YAML (%s) — the desk cannot render it either"
                       % (shown, e.__class__.__name__))
if bad:
    print("FAIL " + "; ".join(bad)); sys.exit(1)
print("OK %d rendered file(s) present and parseable, %d correctly absent behind a closed gate"
      % (checked, off))
PY
); GATED_RC=$?
case "$GATED" in
  KEYOFF*) ok "${GATED#KEYOFF }";;
  *)
[ "$GATED_RC" -eq 0 ] && ok "every staged artifact whose gate is OPEN is rendered under clusters/ and parses, and every artifact behind a CLOSED gate is gone [$GATED] — derived from scripts/instance-init.py's own GATES table, so deleting a gate-open Application (loki.yaml on a bucket-ful org) is red from git alone, in pre-boot, before any of it is deployed" \
                      || bad "gated tree: ${GATED#FAIL }";;
esac

# --- the platform artifacts that ship UNGATED and are asserted from a live branch -----
# Named one by one rather than swept, because each one is here because a ROW below reads
# it: a file in this list going missing turns that row into a silent key-off, which is
# exactly the shape this section exists to remove.
UNGATED_MISS=""
for f in clusters/platform/config/monitoring/netpol.yaml \
         clusters/platform/monitoring/rules.yaml \
         clusters/platform/monitoring/scrape-collectors.yaml; do
  [ -f "$f" ] || UNGATED_MISS="$UNGATED_MISS $f"
done
# the blackbox Application belongs to the uptime feature: with it on the file is read by
# rows below like the others; with it off the gated-tree row above asserts its absence
if [ "$FEATURE_UPTIME" = 1 ] && [ ! -f clusters/platform/apps/blackbox-exporter.yaml ]; then
  UNGATED_MISS="$UNGATED_MISS clusters/platform/apps/blackbox-exporter.yaml"
fi
[ -z "$UNGATED_MISS" ] \
  && ok "every ungated platform artifact a row below reads is in the tree (the monitoring netpol, the rules, the collectors' ServiceMonitors and, while the uptime feature is on, the blackbox Application) — the vault and operator artifacts are staged and gate-closed, so the gated-tree row above asserts their absence instead of this one" \
  || bad "ungated platform artifact(s) missing from this tree:$UNGATED_MISS — each one is read by a row below, and a missing file turns that row into a silent key-off rather than a red line"

# --- the renderer's shared token is pinned in the values ------------------------------
# Moved out of the live branch: whether the chart is allowed to MINT the token is a
# property of the committed values (left to itself it calls randAlphaNum on every render,
# `lookup` of the previous Secret being empty whenever Helm runs without cluster access —
# which is how Argo renders — so a re-render deals Grafana and the renderer different
# values and the render path 401s in silence). The two live rows below still assert the
# Secret exists and that both consumers read it.
KPS=clusters/platform/apps/kube-prometheus-stack.yaml
# Read the KEY, not a line that looks like it: `existingSecret` appears under grafana.admin
# as well, and a grep for the bare word happily reports the admin Secret as proof the
# renderer is pinned.
PIN=$(python3 -c '
import sys, yaml
try: app = yaml.safe_load(open(sys.argv[1]))
except Exception: sys.exit(0)
src = ((app.get("spec") or {}).get("source") or {})
v = src.get("helm", {}).get("valuesObject")
if v is None:
    v = yaml.safe_load(src.get("helm", {}).get("values") or "") or {}
import re
ref = (v.get("grafana", {}).get("imageRenderer", {}) or {}).get("existingSecret", "") or ""
# a Secret NAME (a Kubernetes object name) is printed; anything else is a status word only
print(ref if isinstance(ref, str) and re.fullmatch(r"[a-z0-9]([-a-z0-9]*[a-z0-9])?(\.[a-z0-9]([-a-z0-9]*[a-z0-9])?)*", ref) and len(ref) <= 253 else ("" if not ref else "NOT-A-SECRET-NAME"))' "$KPS" 2>/dev/null)
case "$PIN" in
  "")  bad "imageRenderer.existingSecret is unset in $KPS — the chart will mint a fresh renderer token on every re-render and the render path will 401 in silence";;
  NOT-A-SECRET-NAME) bad "imageRenderer.existingSecret in $KPS is not a Secret name (the value is not repeated) — the render path cannot mount it";;
  *)   ok "imageRenderer.existingSecret is pinned in the values ($PIN) — the chart cannot mint a fresh renderer token on every re-render";;
esac

# retention_of <yaml file> <dotted path>: the scalar at that path, printed only with a
# duration's shape (digits + d/h/m); `unreadable` for anything else, `` for a missing file
retention_of(){ python3 - "$1" "$2" <<'PY' 2>/dev/null
import re, sys, yaml
try:
    node = yaml.safe_load(open(sys.argv[1]))
    for k in sys.argv[2].split("."):
        node = node.get(k) if isinstance(node, dict) else None
except FileNotFoundError:
    print(""); sys.exit()
except Exception:
    print("unreadable"); sys.exit()
print(node if isinstance(node, str) and re.fullmatch(r"[0-9]{1,6}[dhm]", node) else ("" if node is None else "unreadable"))
PY
}
# --- log retention, in git ------------------------------------------------------------
# Bucket-gated (no bucket, no Loki app, nothing to read). The live comparison
# stays live; the FLOOR is a property of the committed value: the coherence floor is the
# longest alert window plus repeat_interval (~38h), so a 24h starter deletes the logs
# covering a failure before the alert detecting it can fire. Arithmetic in python, not
# `bc` — a box without bc turns this row into a permanent false red.
GITRET=""
if [ "$FEATURE_LOGS" != 1 ]; then
  ok "the logs feature is off (no platform bucket, or features.logs is not true) — Loki is not rendered on this orbit, so the retention rows key off (they arm with the logs feature)"
else
  # the field itself, read structurally (a comment or a string that merely contains the
  # words is not the field); a value reaches the shell only with a duration's shape
  GITRET=$(retention_of clusters/platform/apps/loki.yaml spec.source.helm.valuesObject.loki.limits_config.retention_period)
  RETH=$(python3 -c "
import re,sys
m=re.fullmatch(r'(\d+)([dh])', sys.argv[1])
print(int(m.group(1))*(24 if m.group(2)=='d' else 1) if m else -1)" "${GITRET:-}" 2>/dev/null)
  if [ "${RETH:--1}" -ge 168 ] 2>/dev/null; then
    ok "log retention is the raised value ($GITRET = ${RETH}h >= 7d), coherent with the alert windows"
  else
    bad "log retention is ${GITRET:-unreadable} (${RETH:-unreadable}h) — below the documented 7d floor; a failure's logs are deleted before the alert detecting it can fire"
  fi
fi

# --- every dashboard the generator rendered carries a uid, from git alone --------------
# The live half (Grafana serves that uid, and the JSON is canonically identical) needs a
# cluster and stays below. But "the generator emitted a ConfigMap whose JSON does not parse
# or carries no uid" is a fact about GIT, and it was only ever asked inside the live branch
# — so on a scaffold a malformed dashboard shipped green. A dashboard with no uid can never
# be asserted against, live or otherwise.
# a dashboard file name is tree text: only a name of a file-name shape becomes an
# argument or a row; the rest are counted, by position (round 19)
DASHLIST=(); DASHODD=0
for f in clusters/apps/dashboard-*.yaml; do
  [ -e "$f" ] || continue
  case "${f#clusters/apps/}" in
    dashboard-*[!A-Za-z0-9._-]*.yaml|dashboard-.yaml) DASHODD=$((DASHODD+1)); continue;;
  esac
  DASHLIST+=("$f")
done
[ "$DASHODD" -eq 0 ] || bad "$DASHODD dashboard file(s) under clusters/apps have a name not of a file-name shape (not repeated) — they were not checked"
if [ "${#DASHLIST[@]}" -eq 0 ]; then
  ok "the catalog renders no dashboards yet — the dashboard-shape row keys off (it arms with the first project that declares one)"
else
  DSHAPE=$(python3 - "${DASHLIST[@]}" <<'PY'
import json, re, sys, yaml
import shown as display   # the shared display rule (scripts/verify/shown.py)
bad, n = [], 0
for path in sys.argv[1:]:
    try: d = yaml.safe_load(open(path, encoding="utf-8")) or {}
    except Exception as e:
        bad.append("%s does not parse as YAML (%s)" % (path, e.__class__.__name__)); continue
    data = d.get("data") or {}
    if not data:
        bad.append("%s carries no data: keys — the ConfigMap holds no dashboard at all" % path)
        continue
    for ki, (k, v) in enumerate(sorted(data.items())):
        # a data key is file text: shown with a key's shape, else by position
        ks = k if display.fits(k, "key") else "#%d (a key not of a key shape, or of a credential's shape)" % ki
        try: j = json.loads(v)
        except Exception as e:
            bad.append("%s key %s is not parseable JSON (%s) — the sidecar will drop it silently"
                       % (path, ks, e.__class__.__name__)); continue
        if not isinstance(j, dict) or not j.get("uid"):
            bad.append("%s key %s has no uid — a dashboard without one cannot be asserted "
                       "against Grafana by identity, ever" % (path, ks))
        elif not isinstance(j.get("uid"), str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,40}", j["uid"]):
            # Grafana's own grammar; anything else is never placed in a URL (nor printed)
            bad.append("%s key %s has a uid that is not a Grafana uid (letters, digits, - and _, at most 40)" % (path, ks))
            continue
        n += 1
if bad:
    print("FAIL " + "; ".join(bad)); sys.exit(1)
print("OK %d dashboard(s) across %d file(s) parse and carry a uid" % (n, len(sys.argv) - 1))
PY
); DSHAPE_RC=$?
  [ "$DSHAPE_RC" -eq 0 ] && ok "every rendered dashboard parses and carries a uid [$DSHAPE] — the identity the live comparison below is asserted BY, checked from git so a malformed dashboard is red on a scaffold too" \
                         || bad "dashboard shape: ${DSHAPE#FAIL }"
fi


echo "== the observability stack is up =="
# Every name here is rendered by template/provisioning/clusters/platform/apps/
# kube-prometheus-stack.yaml (release name kube-prometheus-stack) and
# template/clusters/platform/apps/blackbox-exporter.yaml (fullnameOverride:
# blackbox-exporter). No org can rename them without editing a template artifact.
if [ "$LIVEOFF" = 1 ]; then
  keyoff_live "the stack-readiness rows key off (0 of 6 armed)"
else
  for d in kube-prometheus-stack-operator \
           kube-prometheus-stack-kube-state-metrics; do
    kubectl -n $MON get deploy "$d" -o jsonpath='{.status.readyReplicas}' 2>/dev/null | grep -q '^[1-9]' \
      && ok "$d is ready" || bad "$d is not ready"
  done
  if [ "$FEATURE_UPTIME" = 1 ]; then
    kubectl -n $MON get deploy blackbox-exporter -o jsonpath='{.status.readyReplicas}' 2>/dev/null | grep -q '^[1-9]' \
      && ok "blackbox-exporter is ready" || bad "blackbox-exporter is not ready"
  elif kubectl -n $MON get deploy blackbox-exporter >/dev/null 2>&1; then
    bad "the uptime feature is off but its prober is still deployed (blackbox-exporter in $MON): it still runs and still costs its memory — converge the features (orbit features converge)"
  else
    ok "the uptime feature is off and its prober (blackbox-exporter) is not deployed"
  fi
  GRAFDEP=kube-prometheus-stack-grafana
  if [ "$FEATURE_DASHBOARDS" = 1 ]; then
    kubectl -n $MON get deploy "$GRAFDEP" -o jsonpath='{.status.readyReplicas}' 2>/dev/null | grep -q '^[1-9]' \
      && ok "$GRAFDEP is ready" || bad "$GRAFDEP is not ready"
  elif kubectl -n $MON get deploy "$GRAFDEP" >/dev/null 2>&1; then
    bad "the dashboards feature is off but Grafana is still deployed ($GRAFDEP in $MON): it still runs and still costs its memory — converge the features (orbit features converge)"
  else
    ok "the dashboards feature is off and Grafana is not deployed"
  fi
  kubectl -n $MON get statefulset prometheus-kube-prometheus-stack-prometheus -o jsonpath='{.status.readyReplicas}' 2>/dev/null | grep -q '^[1-9]' \
    && ok "Prometheus is ready" || bad "Prometheus is not ready"
  kubectl -n $MON get statefulset alertmanager-kube-prometheus-stack-alertmanager -o jsonpath='{.status.readyReplicas}' 2>/dev/null | grep -q '^[1-9]' \
    && ok "Alertmanager is ready" || bad "Alertmanager is not ready"
fi

echo "== nothing is quietly not being scraped =="
# The check that catches half the kubelet targets being unreachable forever while
# TargetDown fires against a completely healthy node and every dashboard stays green.
if [ "$LIVEOFF" = 1 ]; then
  keyoff_live "the scrape-coverage rows key off (0 of 2 armed)"
else
  promraw "/api/v1/targets?state=active" > "$TMP/targets.json"
  python3 - "$TMP/targets.json" "$TMP/down.txt" "$TMP/count.txt" <<'PY' 2>/dev/null
import json,re,sys, urllib.parse
import shown as display   # the shared display rule (scripts/verify/shown.py)
open(sys.argv[2], 'w').close()      # every path leaves a readable file behind
IPV = re.compile(r"[0-9a-f.:]{2,45}")
try:
    d = json.load(open(sys.argv[1]))['data']['activeTargets']
except Exception:
    open(sys.argv[3], 'w').write('-1'); raise SystemExit
# Only the down ones, one per line, and NOTHING AT ALL when there are none: joining an
# empty list leaves a trailing blank line that `grep -c`/`wc -l` counts as one target
# down, and a check that calls a healthy cluster broken gets deleted rather than believed.
with open(sys.argv[2], 'w') as f:
    for t in d:
        if t['health'] != 'up':
            # The job and the scrape host:port name the target; lastError is the remote's
            # text and is not repeated (a diagnostic carries names, never a body).
            u = urllib.parse.urlparse(t.get('scrapeUrl') or '')
            try: port = u.port
            except ValueError: port = None
            job = t['labels'].get('job')
            job = display.shown(job, "label", "job label")
            host = u.hostname or ''
            # the scrape host is Prometheus's text too: a DNS name or an IP address, else counted
            if not display.fits(host, "hostname") and not IPV.fullmatch(host):
                host = "?" + display.described("hostname", "host")
            f.write("%s %s\n" % (job, host + (':%d' % port if port else '')))
open(sys.argv[3], 'w').write(str(len(d)))
PY
  NDOWN=$(wc -l < "$TMP/down.txt" 2>/dev/null || echo 0)
  NTOT=$(cat "$TMP/count.txt" 2>/dev/null || echo -1)
  if [ "${NTOT:--1}" -gt 0 ] 2>/dev/null; then
    ok "Prometheus has $NTOT active targets"
  else
    bad "could not read Prometheus's active targets (got '${NTOT:-nothing}') — scrape coverage is UNCHECKED, not proven"
  fi
  # NDOWN==0 is only a PASS when the read actually succeeded. The python above truncates
  # down.txt before it raises, so a Prometheus that never answered leaves NDOWN=0 with
  # NTOT=-1 — and an unconditional ok here printed "every scrape target is up" about a
  # stack that said nothing at all, which is the empty-grep shape this file exists to
  # forbid. The count row above is red in that case; this row must not contradict it.
  if [ "${NTOT:--1}" -le 0 ] 2>/dev/null; then
    bad "the active-target read failed (count '${NTOT:-nothing}'), so '0 targets down' is the ABSENCE of an answer, not the answer 'nothing is down' — scrape health is unknown"
  elif [ "$NDOWN" -eq 0 ]; then
    ok "every scrape target is up (out of $NTOT active)"
  else
    # A target down RIGHT NOW is not yet a defect — a rolling pod has an Endpoint for a
    # few seconds before its first successful scrape, and this suite runs after every
    # deploy. "Has it been down for five minutes" is the question TargetDown asks and the
    # one worth answering. `or vector(0)` forces a real 0 on a healthy cluster, so an
    # empty answer can now only mean the query never ran — which must FAIL.
    SUSTAINED=$(promval 'count(max_over_time(up[5m]) == 0) or vector(0)')
    case "$SUSTAINED" in
      QUERYERR|ABSENT) bad "could not answer the sustained-down query ($SUSTAINED) — the check did not run; that is not a healthy pass";;
      0|0.*) ok "the $NDOWN down target(s) are mid-rollout, not down (nothing has failed every scrape for 5m)";;
      *) bad "${SUSTAINED%%.*} target(s) down for 5 minutes (job and scrape host; read lastError in Prometheus's targets page): $(tr '\n' ';' < "$TMP/down.txt")";;
    esac
  fi
fi

echo "== the platform's own collectors are scraped, not just running =="
# template/clusters/platform/monitoring/scrape-collectors.yaml. Alloy is the SOLE path
# from container stdout to Loki and Traefik is the ingress every external request
# crosses; both emitted metrics with no ServiceMonitor for months, so neither had an `up`
# series, no RED signal and no TargetDown coverage — Alloy dropped ~269k log lines on a
# Ready pod unnoticed. The static half runs with no cluster and is the row that goes red
# when the file is deleted; the live half proves the scrape actually happens.
SCOLL=clusters/platform/monitoring/scrape-collectors.yaml
SC_OUT=$(python3 - "$SCOLL" "$FEATURE_LOGS" <<'PY'
import os, re, sys, yaml
import shown as display   # the shared display rule (scripts/verify/shown.py)
path, bucket = sys.argv[1], sys.argv[2] == "1"
if not os.path.isfile(path):
    print("FAIL %s is absent — the two collectors the platform depends on have no scrape "
          "target, so a dead log pipeline emits no `up` series and nothing alerts" % path)
    sys.exit(1)
try:
    docs = [d for d in yaml.safe_load_all(open(path)) if isinstance(d, dict)]
except Exception as exc:     # the class only: a parser's message quotes the input
    print("FAIL %s cannot be read as YAML (%s)" % (path, type(exc).__name__)); sys.exit(1)
want = {("PodMonitor", "traefik", "traefik")}
if bucket:
    want.add(("ServiceMonitor", "alloy", "alloy"))
got = {(d.get("kind"), (d.get("metadata") or {}).get("name"),
        (d.get("metadata") or {}).get("namespace")) for d in docs}
missing = want - got
if missing:
    # what the file declares is its text: shown only with an object name's shape, the
    # rest counted; what is MISSING is this row's own constant and may be repeated
    OBJ = re.compile(r"[A-Za-z0-9]([-A-Za-z0-9.]*[A-Za-z0-9])?")
    shaped = sorted(t for t in got if all(isinstance(x, str) and OBJ.fullmatch(x) and len(x) <= 253 and not display.tokenish(x) for x in t))
    odd = len(got) - len(shaped)
    print("FAIL %s declares %s%s but not %s" % (path, shaped, " (+%d object(s) whose kind, name or namespace is not of an object-name shape, or of a credential's shape)" % odd if odd else "", sorted(missing)))
    sys.exit(1)
print("OK %s" % ", ".join("%s/%s in ns %s" % (k, n, ns) for k, n, ns in sorted(want)))
PY
)
[ $? -eq 0 ] && ok "the collector scrape declarations are in git [$SC_OUT] — deleting one silences the collector it names" \
             || bad "collector scrape declarations: $SC_OUT"
if [ "$LIVEOFF" = 1 ]; then
  keyoff_live "the collector up-series rows key off"
else
  # The PodMonitor labels its job <namespace>/<name>, hence traefik/traefik.
  UPT=$(promval 'count(up{job="traefik/traefik"} == 1) or vector(0)')
  # the count is read as a NUMBER: two healthy replicas are two targets, not a failure (round 23)
  case "$UPT" in
    QUERYERR|ABSENT) bad "could not answer the traefik scrape query ($UPT) — the ingress collector's coverage is UNCHECKED";;
    0|0.0) bad "traefik is not being scraped (up==1 count is 0) — the PodMonitor in $SCOLL is gone or ineffective";;
    *) if printf '%s' "$UPT" | grep -qE '^[1-9][0-9]*(\.[0-9]+)?$'; then ok "Prometheus reports up==1 for the traefik PodMonitor ($UPT target(s))"
       else bad "the traefik scrape count is not a number (not repeated) — the ingress collector's coverage is UNCHECKED"; fi;;
  esac
  if [ "$FEATURE_LOGS" = 1 ]; then
    UPA=$(promval 'count(up{job="alloy"} == 1) or vector(0)')
    case "$UPA" in
      QUERYERR|ABSENT) bad "could not answer the alloy scrape query ($UPA) — the log collector's coverage is UNCHECKED";;
      0|0.0) bad "alloy is not being scraped (up==1 count is 0) — its delivery-drop counters cannot be alerted on";;
      *) if printf '%s' "$UPA" | grep -qE '^[1-9][0-9]*(\.[0-9]+)?$'; then ok "Prometheus reports up==1 for the alloy ServiceMonitor ($UPA target(s))"
         else bad "the alloy scrape count is not a number (not repeated) — the log collector's coverage is UNCHECKED"; fi;;
    esac
    # promval, not `promq | first`: the sample is cluster text, and only a NUMBER (or the
    # tokens ABSENT / QUERYERR) reaches a shell test or a row (round 19)
    ALLOYN=$(promval 'count({__name__=~"alloy_.*"})')
    case "$ALLOYN" in
      QUERYERR) bad "could not answer the alloy_* series query — the log collector's delivery is UNCHECKED";;
      ABSENT|0|0.0) bad "no alloy_* series — the alloy scrape is configured but returning nothing";;
      *) ok "alloy_* metrics are landing ($ALLOYN series; the delivery-drop counters can be alerted on)";;
    esac
  else
    ok "no platform bucket declared — Alloy is not rendered on this orbit, so its scrape rows key off (they arm with objectStorage.buckets.platform)"
  fi
fi

echo "== Grafana is behind auth and knows where to look =="
# template/provisioning/clusters/platform/apps/kube-prometheus-stack.yaml renders the
# Ingress at grafana.<primary domain>. The static row asserts the monitoring Namespace
# the stack's credentials live in is declared in git; the live rows assert the door
# and the datasource. The credentials themselves arrive sealed from the secret plane,
# which stage4 owns.
GSEC=clusters/platform/config/monitoring/secrets.yaml
GS_OUT=$(python3 - "$GSEC" <<'PY'
import os, sys, yaml
path = sys.argv[1]
if not os.path.isfile(path):
    print("FAIL %s is absent — the monitoring namespace and every credential the stack "
          "reads are declared there" % path); sys.exit(1)
try:
    docs = [d for d in yaml.safe_load_all(open(path)) if isinstance(d, dict)]
except Exception as exc:     # the class only: a parser's message quotes the input
    print("FAIL %s cannot be read as YAML (%s)" % (path, type(exc).__name__)); sys.exit(1)
ns = [d for d in docs if d.get("kind") == "Namespace"
      and (d.get("metadata") or {}).get("name") == "monitoring"]
if not ns:
    print("FAIL %s does not declare the monitoring Namespace — it is created in the same "
          "wave as the credentials its consumers wait for" % path); sys.exit(1)
print("OK monitoring Namespace declared in %s" % path)
PY
)
[ $? -eq 0 ] && ok "the monitoring Namespace is declared in git [$GS_OUT]" \
             || bad "monitoring credentials: $GS_OUT"
if [ "$LIVEOFF" = 1 ]; then
  keyoff_live "the Grafana door rows key off"
elif [ "$FEATURE_DASHBOARDS" != 1 ]; then
  ok "the dashboards feature is off — the Grafana door rows key off (the stack row above is what goes red for a Grafana that is still deployed)"
elif [ "$HAVEPW" = 0 ]; then
  # Not a keyed-off: the credential is an operator artifact ONBOARDING creates, and a
  # check that cannot run is not a pass.
  bad "no Grafana admin password at $ORBIT_CONFIG_DIR/grafana-admin-password — the authenticated Grafana rows below CANNOT run; a check that cannot run is not a pass"
else
  [ "$(curl -s -o /dev/null -w '%{http_code}' --max-time 20 "$GRAF/api/org")" = "401" ] \
    && ok "Grafana refuses an anonymous API call" || bad "Grafana answered anonymously"
  [ "$(grafapi /api/org -o /dev/null -w '%{http_code}')" = "200" ] \
    && ok "Grafana accepts the admin credential" || bad "Grafana rejected the admin credential"
  grafapi /api/datasources > "$TMP/ds.json"
  python3 -c "
import json,sys
try: d=json.load(open(sys.argv[1]))
except Exception: sys.exit(1)
p=[x for x in d if x.get('type')=='prometheus' and x.get('isDefault')]
sys.exit(0 if p and p[0].get('uid')=='prometheus' else 1)" "$TMP/ds.json" \
    && ok "the default datasource is Prometheus at the uid every generated dashboard names" \
    || bad "no default Prometheus datasource with uid 'prometheus' — every generated dashboard draws nothing"
fi

echo "== every dashboard the generator rendered is the dashboard Grafana serves =="
# The GENERIC half of the instance's dashboard check. scripts/catalog-sync.py renders one
# RED dashboard per project environment into clusters/apps/dashboard-*.yaml; core knows
# those exist and knows nothing about what any org puts IN them, so the assertion is
# identity, not content: Grafana must serve the same uid, and the live JSON must be
# CANONICALLY identical to git. A stale sidecar copy with the same panel count but
# renamed metrics, a wrong datasource uid or wrong thresholds keeps every count equal
# while every panel a human opens is empty. Grafana injects id/version/meta on load, so
# those three are stripped before hashing and everything else must match.
if [ "$LIVEOFF" = 1 ]; then
  keyoff_live "the dashboard-identity rows key off"
elif [ "$FEATURE_DASHBOARDS" != 1 ]; then
  ok "the dashboards feature is off — the dashboard-identity rows key off"
else
  if [ "${#DASHLIST[@]}" -eq 0 ]; then
    ok "the catalog renders no dashboards yet — the dashboard-identity rows key off (they arm with the first project)"
  else
    for f in "${DASHLIST[@]}"; do      # the shaped names only (round 19)
      # One ConfigMap can carry several dashboard JSONs; each is asserted by its own uid.
      # Only a uid of Grafana's own grammar reaches the URL below (one path segment);
      # any other was reported by the static row above and is neither placed nor printed.
      UIDS=$(python3 -c "
import json,re,sys,yaml
d=yaml.safe_load(open(sys.argv[1])) or {}
for k,v in (d.get('data') or {}).items():
    try: u=json.loads(v)['uid']
    except Exception: continue
    if isinstance(u, str) and re.fullmatch(r'[A-Za-z0-9_-]{1,40}', u): print(u)" "$f" 2>/dev/null)
      # An unparseable, uid-less or ill-named dashboard was already reported by the static
      # row above; here it only means there is nothing to compare.
      [ -n "$UIDS" ] || continue
      for u in $UIDS; do
        python3 -c "
import json,sys,yaml
d=yaml.safe_load(open(sys.argv[1])) or {}
for v in (d.get('data') or {}).values():
    try: j=json.loads(v)
    except Exception: continue
    if j.get('uid')==sys.argv[2]:
        json.dump(j, open(sys.argv[3],'w')); break" "$f" "$u" "$TMP/dash.json" 2>/dev/null
        grafapi "/api/dashboards/uid/$u" > "$TMP/live.json"
        if ! python3 -c "import json,sys;json.load(open(sys.argv[1]))['dashboard']['uid']" "$TMP/live.json" >/dev/null 2>&1; then
          bad "Grafana does not serve dashboard $u, which $f declares — the sidecar never picked it up"
          continue
        fi
        python3 - "$TMP/dash.json" "$TMP/live.json" <<'PY' \
          && ok "$u: the live dashboard is canonically identical to git (queries, datasources, thresholds all match)" \
          || bad "$u: the live dashboard has DRIFTED from git — same panel count, different content; a stale sidecar copy is being served"
import json,sys,hashlib
def canon(d):
    d=dict(d); [d.pop(k,None) for k in ('id','version','meta')]
    return hashlib.sha256(json.dumps(d,sort_keys=True).encode()).hexdigest()
g=json.load(open(sys.argv[1]))
l=json.load(open(sys.argv[2]))['dashboard']
sys.exit(0 if canon(g)==canon(l) else 1)
PY
      done
    done
  fi
fi

echo "== the public surfaces are watched from outside the cluster =="
# template/provisioning/clusters/platform/monitoring/probes.yaml. probe_success is the
# only signal here that crosses DNS, the CDN, TLS and Traefik; `up` for the same services
# stays 1 through every one of those failing.
PROBEF=clusters/platform/monitoring/probes.yaml
if [ "$FEATURE_UPTIME" != 1 ]; then
  ok "the uptime feature is off — the public-surface rows key off (nothing probes the doors from outside; the instance chose that)"
  # ...and nothing of it may be left behind: a Probe object with no prober is a check
  # that looks declared and never runs
  if [ -f "$PROBEF" ]; then
    bad "the uptime feature is off but $PROBEF is still in the tree — run make init"
  else
    ok "the uptime feature is off and $PROBEF is not in the tree"
  fi
  if [ "$LIVEOFF" = 1 ]; then
    keyoff_live "the leftover-Probe row keys off"
  else
    NPROBE=$(kubectl get probes.monitoring.coreos.com -A -o name 2>/dev/null | grep -c . || true)
    [ "${NPROBE:-0}" = 0 ] && ok "the uptime feature is off and the cluster holds no Probe object" \
      || bad "the uptime feature is off but the cluster still holds $NPROBE Probe object(s) — declared checks that nothing runs: converge the features (orbit features converge)"
  fi
elif [ ! -f "$PROBEF" ]; then
  bad "$PROBEF is absent — the platform's own public doors are watched by nothing from outside the cluster"
else
  # Every Probe the tree declares, not one file: the platform's own doors in $PROBEF, the
  # generated tenant Probes under clusters/apps/ (the daemon applies those with each
  # pair), and the target of every probe monitor under monitors/ (orbitd renders each
  # into a Probe of its own), because the live comparison below is against every
  # probe_success series and a door probed from its own file is not a drift. Without the
  # monitors, one uptime monitor made by a user turned this row red for as long as the
  # monitor existed (QA of v1.0.0, F13: "6 declared, 7 live", no host named).
  python3 -c "
import glob,sys,yaml
t=[]
for f in ['$PROBEF'] + sorted(glob.glob('clusters/apps/*.yaml')):
    try: docs = list(yaml.safe_load_all(open(f)))
    except Exception: continue
    for d in docs:
        if isinstance(d,dict) and d.get('kind')=='Probe':
            t += ((d.get('spec') or {}).get('targets') or {}).get('staticConfig',{}).get('static') or []
for f in sorted(glob.glob('monitors/*.yaml')):
    try: d = yaml.safe_load(open(f))
    except Exception: continue
    if isinstance(d,dict) and d.get('kind')=='probe' and isinstance(d.get('target'),str):
        t.append(d['target'])
print('\n'.join(sorted(set(t))))" > "$TMP/want.txt" 2>/dev/null
  NWANT=$(grep -c . "$TMP/want.txt" 2>/dev/null || true)
  [ "$NWANT" -ge 1 ] && ok "the Probe tree ($PROBEF, the tenant Probes and the probe monitors) declares $NWANT target(s)" \
    || bad "the Probe tree declares no probe targets at all — the platform's own doors are unwatched"
  if [ "$LIVEOFF" = 1 ]; then
    keyoff_live "the live probe rows key off"
  else
    promq 'probe_success' | python3 -c "
import json,sys
try: r=json.load(sys.stdin)['data']['result']
except Exception: sys.exit(1)
# One line per target: a door probed by two Probes (the platform's and a tenant's) is one
# target, not a difference from git.
print('\n'.join(sorted(set(x['metric']['instance'] for x in r))))" > "$TMP/have.txt" 2>/dev/null
    # A difference is reported by HOST: a target's path or query may carry a token, and a
    # host is enough to find the Probe that declares it.
    probe_drift(){ python3 - "$1" "$2" <<'PY'
import re, sys, urllib.parse
import shown as display   # the shared display rule (scripts/verify/shown.py)
HOST = re.compile(r"[a-z0-9]([a-z0-9-]{0,62}[a-z0-9])?(\.[a-z0-9]([a-z0-9-]{0,62}[a-z0-9])?)*")
def targets(p): return [l.strip() for l in open(p) if l.strip()]
def host(t):
    # both lists are text (git's, Prometheus's): a host is repeated only with a hostname's shape
    try: h = urllib.parse.urlsplit(t).hostname or ""
    except ValueError: return "?(unparseable)"   # the parser's message quotes the target
    return h if display.fits(h, "hostname") else "?" + display.described("hostname", "host")
def hosts(ts): return {host(t) for t in ts}
w, h = targets(sys.argv[1]), targets(sys.argv[2])
print("%d target(s) declared, %d live; declared but unprobed host(s): %s; probed but undeclared host(s): %s"
      % (len(w), len(h), ", ".join(sorted(hosts(w) - hosts(h))) or "none", ", ".join(sorted(hosts(h) - hosts(w))) or "none"))
PY
    }
    diff -q "$TMP/want.txt" "$TMP/have.txt" >/dev/null 2>&1 \
      && ok "every probe target in git is being probed ($NWANT of them)" \
      || bad "probe targets differ from git: $(probe_drift "$TMP/want.txt" "$TMP/have.txt")"
    # COVERAGE, derived from the LIVE surface rather than a hand-copied want-list: every
    # hostname that actually has an Ingress must be probed, or be one of the platform's
    # own doors that a blackbox http_2xx probe CANNOT honestly watch. Those two are
    # named from $ORBIT_DOMAIN, so the list is this org's, not some other org's:
    #   mcp.       the MCP door — no unauthenticated 200 exists (the door 401s by design)
    #   intake.    the agent plane's webhook door — a http_2xx probe is FAIL-OPEN here,
    #              because Traefik's no-router 404 and the intake's own 404 are
    #              indistinguishable; its own block asserts the door instead
    # A NEW public hostname therefore fails this suite until it is probed or excused.
    # Fail CLOSED on an unanswerable listing. An empty host set and a kubectl that could
    # not answer look identical downstream, and the second one silently reports "nothing
    # unprobed" for a surface nobody looked at.
    # the host listing is cluster text: a private file, never a shell variable; a host
    # reaches the row only with a hostname's shape, the rest are counted
    kubectl get ingress -A -o jsonpath='{range .items[*]}{range .spec.rules[*]}{.host}{"\n"}{end}{end}' > "$TMP/inghosts.raw" 2>/dev/null; ING_RC=$?
    sort -u < "$TMP/inghosts.raw" | grep -v '^$' > "$TMP/inghosts.txt"; rm -f "$TMP/inghosts.raw"
    NING=$(grep -c . "$TMP/inghosts.txt")
    UNCOV=$(python3 - "$TMP/want.txt" "$TMP/inghosts.txt" "$ORBIT_DOMAIN" <<'PY' 2>/dev/null
import re, sys, urllib.parse
import shown as display   # the shared display rule (scripts/verify/shown.py)
HOST = re.compile(r"(?i)[a-z0-9]([a-z0-9-]{0,62}[a-z0-9])?(\.[a-z0-9]([a-z0-9-]{0,62}[a-z0-9])?)*")
def host(t):
    try: return urllib.parse.urlsplit(t).hostname
    except ValueError: return None      # the parser's message quotes the target
probed = {host(l.strip()) for l in open(sys.argv[1]) if l.strip()} - {None}
dom = sys.argv[3]
exclude = {p + "." + dom for p in ("mcp", "intake")}
ing = {l.strip() for l in open(sys.argv[2]) if l.strip()}
shaped = {h for h in ing if display.fits(h, "hostname")}
odd = len(ing) - len(shaped)
out = sorted(h for h in shaped if h not in probed and h not in exclude)
if odd: out.append("(+%d host(s) not of a hostname shape, or of a credential's shape — unprobed by construction)" % odd)
print(" ".join(out))
PY
); UNCOV_RC=$?
    if [ "$UNCOV_RC" -ne 0 ]; then
      bad "the probe-coverage comparison could not run (rc=$UNCOV_RC) — the coverage question is UNANSWERED"
    elif [ "$ING_RC" -ne 0 ] || [ "$NING" -eq 0 ]; then
      bad "could not enumerate the live Ingress surface (rc=$ING_RC, $NING hosts) — the probe-coverage question is UNANSWERED, not answered 'nothing is unprobed'; the platform alone renders Ingresses for Argo CD and Grafana, so zero is never a real answer on a running cluster"
    elif [ -z "$UNCOV" ]; then
      ok "every live Ingress host is probed or is one of the three platform doors a blackbox probe cannot honestly watch ($NING hosts on the surface)"
    else
      bad "public Ingress host(s) with NO blackbox probe and no excuse:$UNCOV — add a target to $PROBEF or assert it some other way"
    fi
    NBAD=$(promq 'probe_success == 0' | python3 -c "import json,sys;print(len(json.load(sys.stdin)['data']['result']))" 2>/dev/null)
    [ "${NBAD:-x}" = "0" ] && ok "every probed surface answers 200 over the public path" \
      || bad "${NBAD:-an unknown number of} probed surface(s) failing"
    # The alert threshold is 14 days; assert the real thing is nowhere near it, so a
    # green alert rule is not green merely because nobody has looked.
    MIND=$(promval 'min((probe_ssl_earliest_cert_expiry - time()) / 86400)')
    case "$MIND" in
      QUERYERR|ABSENT|"") bad "could not read the minimum certificate lifetime ($MIND) — the expiry guard did not run";;
      *) python3 -c "import sys;sys.exit(0 if float(sys.argv[1]) > 14 else 1)" "$MIND" 2>/dev/null \
           && ok "the nearest certificate has $(printf '%.0f' "$MIND") days left, past the 14-day alert" \
           || bad "certificate expiry is $MIND days — at or under the alert threshold";;
    esac
  fi
fi

echo "== every platform rule in git is loaded and able to fire =="
# template/provisioning/clusters/platform/monitoring/rules.yaml. A rule Prometheus could not evaluate
# reports health "err" and will never fire; it looks identical to a quiet one in every UI.
RULEF=clusters/platform/monitoring/rules.yaml
if [ ! -f "$RULEF" ]; then
  bad "$RULEF is absent — the platform ships no alert rules at all"
else
  python3 -c "
import sys,yaml
d=yaml.safe_load(open(sys.argv[1])) or {}
print('\n'.join(r['alert'] for g in (d.get('spec') or {}).get('groups') or [] for r in g.get('rules') or [] if r.get('alert')))" "$RULEF" \
    > "$TMP/gitrules.txt" 2>/dev/null
  NRULES=$(grep -c . "$TMP/gitrules.txt" 2>/dev/null || true)
  [ "$NRULES" -ge 1 ] && ok "$RULEF declares $NRULES alert rule(s)" \
    || bad "$RULEF declares no alert rules — the platform's own alerting is empty"
  # PlatformContainerRestarted is the rule the template ships for the gap
  # KubePodCrashLooping leaves: a container that OOMKills and comes straight back Ready
  # never reaches CrashLoopBackOff, so nothing fires. Named here because it is the one
  # rule whose deletion is invisible.
  grep -qx "PlatformContainerRestarted" "$TMP/gitrules.txt" \
    && ok "the clean-restart rule PlatformContainerRestarted is still declared in git" \
    || bad "PlatformContainerRestarted is GONE from $RULEF — a container that OOMKills and returns Ready fires nothing"
  if [ "$LIVEOFF" = 1 ]; then
    keyoff_live "the rule-loading rows key off"
  else
    promraw "/api/v1/rules" > "$TMP/rules.json"
    python3 - "$TMP/rules.json" > "$TMP/loaded.txt" 2>/dev/null <<'PY'
import json,sys
try: d=json.load(open(sys.argv[1]))['data']['groups']
except Exception: raise SystemExit
import re
NAME = re.compile(r"[A-Za-z_:][A-Za-z0-9_:.]{0,127}")
for g in d:
    for r in g['rules']:
        if not isinstance(r, dict) or r.get('type') != 'alerting': continue
        # the rule's name and health are the server's text: a name of an alert-name shape,
        # a health from the API's fixed set, and nothing else
        n, h = r.get('name'), r.get('health')
        print((n if isinstance(n, str) and NAME.fullmatch(n) else "(a-rule-name-not-of-an-alert-name-shape)"),
              (h if h in ("ok", "err", "unknown") else "unreadable"))
PY
    if [ ! -s "$TMP/loaded.txt" ]; then
      bad "Prometheus returned no alerting rules at all — the rule-loading check did not run; that is not a pass"
    else
      # Declared names are compared LITERALLY (set membership in python), never as a
      # grep pattern where a dot in a name is a wildcard; a name not of an alert-name
      # shape is counted, not printed.
      MISS=$(python3 - "$TMP/gitrules.txt" "$TMP/loaded.txt" <<'PY'
import re, sys
import shown as display   # the shared display rule (scripts/verify/shown.py)
NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,127}")
want = [l.rstrip("\n") for l in open(sys.argv[1]) if l.strip()]
loaded = {l.split(" ", 1)[0] for l in open(sys.argv[2]) if l.strip()}
miss = [w for w in want if w not in loaded]
for w in miss:
    print("         not loaded: " + display.shown(w, "alert-name", "rule name"), file=sys.stderr)
print(len(miss))
PY
)
      case "$MISS" in ''|*[!0-9]*) MISS=1;; esac
      [ "$MISS" -eq 0 ] && ok "all $NRULES platform alert rules are loaded" \
        || bad "$MISS platform alert rule(s) are not loaded"
      grep -Eq " (err|unreadable)$" "$TMP/loaded.txt" \
        && bad "some alert rules failed to evaluate (or report a health not in the API's set): $(grep -E ' (err|unreadable)$' "$TMP/loaded.txt" | tr '\n' ' ')" \
        || ok "every loaded alert rule evaluates without error"
      # And the metric the restart rule keys on must exist, or the rule is
      # green-but-unfireable. The design fell into exactly that trap once:
      # kube_pod_container_status_last_terminated_reason returns reason=Error and never
      # OOMKilled on this kubelet, so a reason-based rule would be permanently dormant.
      RC=$(promval 'count(kube_pod_container_status_restarts_total)')
      case "$RC" in
        QUERYERR|ABSENT|"") bad "kube_pod_container_status_restarts_total is absent ($RC) — PlatformContainerRestarted cannot fire";;
        *) ok "kube_pod_container_status_restarts_total exists ($RC series) — the clean-restart rule can fire";;
      esac
    fi
  fi
fi

echo "== no platform workload is sized under its own peak usage =="
# Grafana was OOM-crash-looping at a 256Mi limit its own working set exceeded; the fix
# raised it to 512Mi in template/provisioning/clusters/platform/apps/
# kube-prometheus-stack.yaml. This guard keeps it there. The subtlety that makes a naive
# check useless: container_memory_working_set_bytes is CENSORED by the limit — the cgroup
# SIGKILLs at the limit, so the observed peak can never exceed it and `limit >= peak` is
# trivially true precisely when the workload is being OOMKilled. So the honest signal is
# HEADROOM: a peak within 10% of the limit is the OOM signature itself.
if [ "$LIVEOFF" = 1 ]; then
  keyoff_live "the under-sizing guard keys off"
elif [ "$FEATURE_DASHBOARDS" != 1 ]; then
  ok "the dashboards feature is off — Grafana's under-sizing guard keys off"
else
  # The limit is the cluster's text: it goes to a scratch file and a FIXED python reads it
  # as data (never spliced into a program), printing bytes only once it has a quantity's shape.
  kubectl -n $MON get deploy kube-prometheus-stack-grafana \
    -o jsonpath='{.spec.template.spec.containers[?(@.name=="grafana")].resources.limits.memory}' > "$TMP/glim" 2>/dev/null
  GLIMB=$(python3 - "$TMP/glim" <<'PY' 2>/dev/null
import re, sys
s = open(sys.argv[1]).read().strip()
u = {'Ki': 1024, 'Mi': 1024**2, 'Gi': 1024**3, 'K': 1000, 'M': 1000**2, 'G': 1000**3}
m = re.fullmatch(r'(\d+)(Ki|Mi|Gi|K|M|G)?', s)
print(int(m.group(1)) * u.get(m.group(2) or '', 1) if m else 0)
PY
)
  GPEAKB=$(promval 'max(max_over_time(container_memory_working_set_bytes{namespace="monitoring",container="grafana"}[6h]))')
  case "$GPEAKB" in
    QUERYERR|ABSENT|"") bad "could not read grafana's peak working set ($GPEAKB) — the sizing guard did not run";;
    *) if [ "${GLIMB:-0}" -le 0 ]; then
         bad "grafana has NO memory limit — an unbounded container cannot be checked against its usage"
       else
         VERDICT=$(awk -v p="$GPEAKB" -v l="$GLIMB" 'BEGIN{printf (p >= 0.9*l) ? "UNDER" : "OK"}')
         PMI=$(awk -v p="$GPEAKB" 'BEGIN{printf "%.0f", p/1048576}')
         LMI=$(awk -v l="$GLIMB" 'BEGIN{printf "%.0f", l/1048576}')
         [ "$VERDICT" = "OK" ] \
           && ok "grafana's limit (${LMI}Mi) sits comfortably above its observed peak working set (${PMI}Mi) — headroom, not a ceiling" \
           || bad "grafana's peak working set (${PMI}Mi) is at/over 90% of its ${LMI}Mi limit — it is sized under its own usage and will OOMKill; raise the limit above the measured peak"
       fi;;
  esac
fi

echo "== and no platform workload reserves far more than it uses =="
# The mirror image, and the reason the right-sizing work exists: a node can sit at ~94%
# memory REQUESTED while ~64% is USED, so the scheduler is full while the RAM is free and
# the next component has nowhere to land. Over-reservation is silent — nothing alerts,
# nothing crashes, every dashboard is green — right up until a pod is unschedulable.
# Four deliberate narrowings:
#   p99, not peak.  A request should cover the STEADY state; genuine bursts are the
#                   limit's job, and it is the limit the kernel kills on.
#   >= 24h old.     A pod scheduled minutes ago has no window to be judged against.
#   > 64Mi only.    Below that the reservation is noise and 3x p99 is an absurd bound.
#   PLATFORM only.  Tenant namespaces (the ones scripts/catalog-sync.py labels
#                   `.../project`) are the instance's own to size, and kube-system is
#                   k3s-managed and declared in no repo — a red in either names a request
#                   nobody reading THIS file can edit.
if [ "$LIVEOFF" = 1 ]; then
  keyoff_live "the over-reservation oracle keys off"
else
  EXCL="kube-system"
  # a namespace name from the API lands inside a PromQL regex: only a label-shaped one
  # goes in (a `"` or a `|` in a name would rewrite the query); the rest are counted
  EXCL_ODD=0
  for n in $(project_namespaces); do
    case "$n" in
      *[!a-z0-9-]*|""|-*|*-) EXCL_ODD=$((EXCL_ODD + 1));;
      *) EXCL="$EXCL|$n";;
    esac
  done
  [ "$EXCL_ODD" -eq 0 ] || bad "$EXCL_ODD project namespace(s) have a name that is not a namespace name (not repeated) — they are left OUT of the over-reservation exclusion"
  # max by (namespace,pod,container) around the working set is load-bearing: cadvisor
  # keeps a SEPARATE series per container instance, so any pod that has ever restarted
  # gives the join two right-hand matches and the whole query fails with "found duplicate
  # series for the match group" — which the apiserver proxy flattens to "invalid request".
  RS_SUBJECT='(kube_pod_container_resource_requests{resource="memory",namespace!~"'"$EXCL"'"} > 67108864)
   / on(namespace,pod,container) group_left()
   max by (namespace,pod,container) (
     quantile_over_time(0.99, container_memory_working_set_bytes{container!="",container!="POD"}[48h]))
   and on(namespace,pod) (time() - max by (namespace,pod) (kube_pod_created)) > 86400'
  # First prove the oracle can SEE anything. A renamed metric, a broken join or a
  # Prometheus that lost its 48h of history all produce zero offenders, which reads
  # exactly like a clean bill of health. The liveness count measures the POPULATION, not
  # the post-age-gate scope: counting the scope would make the guard fire on a mass
  # restart rather than on a broken query.
  RS_POP=$(promval 'count(kube_pod_container_resource_requests{resource="memory",namespace!~"'"$EXCL"'"} > 67108864)')
  case "$RS_POP" in
    QUERYERR|ABSENT|"") bad "the over-reservation oracle could not evaluate ($RS_POP) — platform requests are UNCHECKED, not proven sane";;
    *) if [ "${RS_POP%%.*}" -lt 5 ]; then
         bad "the over-reservation oracle sees only $RS_POP platform containers with a memory request — too few to be believed; the query or the metrics changed"
       else
         promq "($RS_SUBJECT) > 3" > "$TMP/overreserved.json"
         python3 - "$TMP/overreserved.json" "$TMP/overreserved.txt" <<'PY'
import json,re,sys
NS = re.compile(r"[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?")
def sh(v): return v if isinstance(v, str) and NS.fullmatch(v) else "(unshaped)"
try: d=json.load(open(sys.argv[1]))
except Exception: d=None
try:
    # labels are Prometheus's text: shown only with a label's shape; a value that is
    # not a number makes the whole answer unreadable
    out="QUERYERR" if not d or d.get("status")!="success" else " ".join(
      "%s/%s=%.1fx" % (sh(s["metric"].get("namespace")), sh(s["metric"].get("container")), float(s["value"][1]))
      for s in d.get("data",{}).get("result",[]))
except Exception:
    out="QUERYERR"
open(sys.argv[2],"w").write(out)
PY
         RS_OVER=$(cat "$TMP/overreserved.txt")
         if [ "$RS_OVER" = "QUERYERR" ]; then
           bad "the over-reservation query did not answer — platform requests are UNCHECKED"
         elif [ -z "$RS_OVER" ]; then
           ok "no platform workload reserves more than 3x its 48h p99 working set ($RS_POP containers carry a request; those >= 24h old were judged)"
         else
           bad "over-reserved (memory request > 3x the 48h p99 working set): $RS_OVER — re-measure and cut the request"
         fi
       fi;;
  esac
fi

echo "== the alert path reaches a human and the agent, and nothing spams either =="
# template/provisioning/clusters/platform/apps/kube-prometheus-stack.yaml ships the whole
# path: one Telegram receiver reading a bot_token_file, the Watchdog null-route, and the
# single `^OrbitAgent.*` route to the agent plane's intake. The credentials it mounts are
# the Secrets the sealed plane renders for template/provisioning/clusters/platform/config/monitoring/.
if [ "$LIVEOFF" = 1 ]; then
  keyoff_live "the alert-path rows key off"
else
  amraw "/api/v2/status" > "$TMP/amstatus.json"
  # All three rows below are answered by the running configuration Alertmanager serves
  # itself, which redacts every secret-typed VALUE but keeps every KEY: a config that
  # inlines the credential still shows the `bot_token` key where a correct one shows
  # `bot_token_file`. No Secret is read anywhere in this section.
  if [ "$FEATURE_ALERTS_TELEGRAM" = 1 ]; then
  python3 -c "
import json,sys,yaml
try:
    raw=json.load(open(sys.argv[1]))['config']['original']
    c=yaml.safe_load(raw) if isinstance(raw,str) else raw
except Exception: sys.exit(1)
tcs=[t for r in (c.get('receivers') or []) for t in (r.get('telegram_configs') or [])]
if not tcs or not all(t.get('bot_token_file') for t in tcs): sys.exit(1)
if any('bot_token' in t for t in tcs): sys.exit(1)" "$TMP/amstatus.json" \
    && ok "Alertmanager notifies Telegram by reading a file, not an inlined token" \
    || bad "Alertmanager's Telegram config does not use bot_token_file (or its status could not be read)"
  else
    ok "the alerts-telegram feature is off — the three Telegram rows key off (alerts are still raised, recorded and read through orbitd; nobody is messaged)"
    # ...and the running config must agree: a notifier still configured is one that still sends
    python3 -c "
import json,sys,yaml
try:
    raw=json.load(open(sys.argv[1]))['config']['original']
    c=yaml.safe_load(raw) if isinstance(raw,str) else raw
    r=c['receivers']
except Exception: sys.exit(2)
sys.exit(1 if any((x or {}).get('telegram_configs') for x in r) else 0)" "$TMP/amstatus.json"
    case $? in
      0) ok "the alerts-telegram feature is off and the running Alertmanager config has no Telegram notifier";;
      1) bad "the alerts-telegram feature is off but the running Alertmanager config still has a Telegram notifier — it still sends: converge the features (orbit features converge)";;
      *) bad "the alerts-telegram feature is off and the running Alertmanager config could not be read — 'no Telegram notifier' is UNPROVEN";;
    esac
  fi
  # The workstation-copy comparison is gone with the Secret read, deliberately: the row
  # above now proves the property STRUCTURALLY — no `bot_token` key anywhere a Telegram
  # message can be sent from — on every run, instead of proving it only on the one
  # machine that happened to hold a copy of the token.
  #
  # And the GENERAL property the deleted workstation row used to prove whole-config:
  # no receiver OF ANY TYPE carries a credential inline, and no URL embeds one — a
  # token reaching the same chat through a webhook receiver's URL was caught by the
  # old grep and is invisible to the `bot_token` key check above. Run over the same
  # redacted status view: secret VALUES show as `<secret>` there, but the offending
  # KEYS are still visible, which is what the checker reads — and what it reports.
  python3 -c "
import json,sys,yaml
try:
    raw=json.load(open(sys.argv[1]))['config']['original']
    c=yaml.safe_load(raw) if isinstance(raw,str) else raw
    yaml.safe_dump(c, open(sys.argv[2],'w'))
except Exception: sys.exit(1)" "$TMP/amstatus.json" "$TMP/amconfig.yaml" \
    && python3 scripts/verify/am-inline-credentials.py "$TMP/amconfig.yaml" --desired clusters/platform/apps/kube-prometheus-stack.yaml \
    && ok "no receiver of any type carries a credential inline (checked over the running config; a secret-typed URL the running config redacts is clean in git at the same place — its equality with the running value is UNVERIFIED — see the row below — never read from the Secret)" \
    || bad "a receiver carries a credential inline (or a redacted URL has no clean counterpart in git), or the running config could not be checked for it"
  # A redacted URL's git counterpart is proven clean; whether the RUNNING value equals
  # it is not readable without the Secret's data, which no stage reads, and the desk
  # (which applies the platform layer; Argo CD is retired) keeps no drift report for
  # platform apps. Said so, as an [UNVERIFIED] row — never a pass, never a guess.
  ok "[UNVERIFIED] a secret-typed URL the running config redacts is clean in git at the same place; its equality with the running value would need the Secret's data, which no stage reads — the intake delivery rows below are the behavioural proof"
  if [ "$FEATURE_ALERTS_TELEGRAM" = 1 ]; then
  kubectl -n $MON exec alertmanager-kube-prometheus-stack-alertmanager-0 -c alertmanager -- \
    ls /etc/alertmanager/secrets/alertmanager-telegram/bot-token >/dev/null 2>&1 \
    && ok "the token is mounted in the Alertmanager pod" || bad "the token is not mounted in the Alertmanager pod"
  # Exactly one telegram receiver. A second one is how the human and the agent start
  # being told different things: the agent plane's notifier joins THIS receiver.
  python3 -c "
import json,sys,yaml
try:
    raw=json.load(open(sys.argv[1]))['config']['original']
    c=yaml.safe_load(raw) if isinstance(raw,str) else raw
except Exception: sys.exit(1)
r=[x for x in (c.get('receivers') or []) if 'telegram_configs' in x]
sys.exit(0 if len(r)==1 else 1)" "$TMP/amstatus.json" \
    && ok "exactly one Telegram receiver carries the human-facing route" \
    || bad "there is not exactly one Telegram receiver"
  fi
  # Watchdog fires permanently by design. Routed anywhere visible it trains you to ignore
  # the channel, which is the most expensive thing that can happen to an alerting system.
  python3 -c "
import json,sys,yaml
try:
    raw=json.load(open(sys.argv[1]))['config']['original']
    c=yaml.safe_load(raw) if isinstance(raw,str) else raw
except Exception: sys.exit(1)
rs=[r for r in (c.get('route') or {}).get('routes',[]) if any('Watchdog' in m for m in r.get('matchers',[]))]
sys.exit(0 if rs and rs[0].get('receiver')=='null' else 1)" "$TMP/amstatus.json" \
    && ok "Watchdog is routed to the null receiver" || bad "Watchdog is not routed to null"
  NF=$(promval 'sum(alertmanager_notifications_failed_total) or vector(0)')
  case "$NF" in
    QUERYERR|ABSENT|"") bad "could not read Alertmanager's notification-failure counter ($NF) — the delivery check did not run";;
    0|0.*) ok "Alertmanager has never failed to deliver a notification";;
    *) bad "Alertmanager has $NF failed notifications — an alert was routed somewhere that refused it";;
  esac
  # Anything firing other than the two that fire by design (Watchdog, the dead-man's
  # switch; InfoInhibitor, kube-prometheus-stack's inhibitor for info-severity alerts) is
  # a real problem this suite must not paper over.
  amraw "/api/v2/alerts" > "$TMP/alerts.json"
  # An alertname is a label value from the cluster: printed only when it has the shape
  # of a name; the rest are counted.
  FIRING=$(python3 -c "
import json,re,sys
import shown as display   # the shared display rule (scripts/verify/shown.py)
try:
    a=json.load(open(sys.argv[1]))
    if not isinstance(a, list) or not all(isinstance(x, dict) for x in a):
        raise ValueError('not an alert list')
    names=[(x.get('labels') or {}).get('alertname') for x in a]
    names=[n for n in names if n not in ('Watchdog','InfoInhibitor')]
    good=sorted({n for n in names if display.fits(n, 'alert-name')})
    note=display.shown_list(names, 'alert-name', 'alert')[1]     # the count of the rest, worded by the rule
    print(','.join(good) + (' ' + note if note else ''))
except Exception:
    # a null, a map, a crash: not an answer — never the empty line that reads as 'nothing firing'
    print('UNREADABLE')" "$TMP/alerts.json" 2>/dev/null); FIRINGRC=$?; [ "$FIRINGRC" -eq 0 ] || FIRING=UNREADABLE
  case "$FIRING" in
    "") ok "nothing is firing except Watchdog and InfoInhibitor (the two that fire by design)";;
    UNREADABLE) bad "could not read Alertmanager's alert list — 'nothing is firing' is UNPROVEN, not true";;
    *) bad "alerts firing: $FIRING";;
  esac
fi

echo "== the observability plane refuses arbitrary pods =="
# The monitoring, loki and tempo namespaces carried no ingress NetworkPolicy, so any pod
# in the cluster reached Prometheus (:9090 query AND remote-write), Loki (:3100
# read/push/delete), Alertmanager (:9093 silences) and Tempo (:3200, every trace) with
# auth_enabled off — a compromised public pod could read every log and trace, forge logs
# and metrics, and silence the alerts about its own compromise. The three netpols are
# template/clusters/platform/config/monitoring/netpol.yaml and
# template/provisioning/clusters/platform/config/{loki,tempo}/netpol.yaml.
#
# STATIC FIRST, and it is not decoration. The live dials below prove REFUSAL but are
# BLIND TO WIDENING: this CNI programs allow-sources on a sync interval, so a ~10s probe
# pod is never in the allow-set and a "blocked" row stays green whatever SHAPE the
# from-element has. Widen the Alertmanager grant back to the whole loki namespace and
# only this static row goes red. It runs with no cluster, so it is honest in pre-boot.
# gather_netpols <namespace> <out>: every NetworkPolicy the renderer would apply into
# that namespace from under clusters/ — an explicit metadata.namespace, or none at all
# under a directory Application whose destination is that namespace (shadow.py
# _apply_namespace). Both YAML suffixes, the renderer's own input rule.
# The membership rule is the renderer's (platform.py _render_directory / _manifest_files):
# an Application with a path and no chart is a directory source; it applies the files
# directly under its path, or every file below it with directory.recurse; a file matching
# directory.exclude (ArgoCD doublestar) is not applied; both YAML suffixes. A file no
# Application applies is nobody's policy, wherever its metadata.namespace says it lives —
# and an APPLIED file the renderer cannot read fails the whole render, so it is RED here
# (rc 3), never skipped (round 16).
gather_netpols(){ python3 - clusters "$2" "$1" <<'PY'
import copy, fnmatch, pathlib, posixpath, re, sys, yaml
import shown as display   # the shared display rule (scripts/verify/shown.py)
root, out, want = pathlib.Path(sys.argv[1]), sys.argv[2], sys.argv[3]
# the renderer follows a symlinked source directory (iterdir); this walk does not descend
# one, so a symlink anywhere under clusters/ is refused outright, never silently skipped (round 26)
if root.is_dir() and any(p.is_symlink() for p in root.rglob("*")):
    print("clusters/ carries a symlink (the renderer follows it; this audit does not — the path is not repeated) — the isolation audit is UNCHECKED, not clean"); sys.exit(3)
files = sorted(p for p in root.rglob("*") if p.is_file() and p.suffix in (".yaml", ".yml")) if root.is_dir() else []
def load(f):
    try: return [d for d in yaml.safe_load_all(f.read_text(encoding="utf-8", errors="replace")) if isinstance(d, dict)]
    except Exception: return None
def patterns(exclude):
    if exclude is None or exclude is False: return []
    parts = [str(p) for p in exclude] if isinstance(exclude, list) else [str(exclude)]
    out = []
    for part in parts:
        text = part.strip().strip("'\"")
        if text.startswith("{") and text.endswith("}"): out += [t.strip() for t in text[1:-1].split(",") if t.strip()]
        elif text: out += [t.strip() for t in text.split(",") if t.strip()]
    return out
def glob_match(rel, pat):
    if pat.endswith("/**"):
        prefix = pat[:-3]; return rel == prefix or rel.startswith(prefix + "/")
    return fnmatch.fnmatch(rel, pat) or fnmatch.fnmatch(pathlib.Path(rel).name, pat)
def srcpath(p):
    """the renderer's path: platform.py _render_directory strips it and joins it through
    pathlib, which drops `./` and `a/../` components — the membership test compares the
    SAME normal form (round 21); a path that climbs out of the tree applies nothing here"""
    n = posixpath.normpath(p.strip().lstrip("/"))
    return None if n in (".", "..") or n.startswith("../") else n
dirapps = []
# the renderer's entrypoints, not a tree-wide search (round 19): platform.py
# load_applications reads clusters/platform/apps/*.yaml|*.yml, and _render_directory
# skips an Application document nested under a directory source — an Application file
# anywhere else is nobody's and applies nothing
PLATFORM_APPS = root / "platform" / "apps"
# ONE Application per name: the renderer keeps the LAST in path order and renders that set
# (platform.py load_applications by_name) — an earlier file's source is never applied (round 20)
by_name = {}
for f in files:
    if f.parent != PLATFORM_APPS: continue
    for d in load(f) or []:
        if d.get("kind") != "Application": continue
        name = str((d.get("metadata") or {}).get("name") or "")
        if name: by_name[name] = d
for name in sorted(by_name):
    d = by_name[name]
    spec = d.get("spec") or {}; src = spec.get("source") or {}
    ns = (spec.get("destination") or {}).get("namespace")
    if src.get("chart") or not isinstance(src.get("path"), str) or not isinstance(ns, str) or not ns: continue
    directory = src.get("directory") or {}
    if not isinstance(directory, dict): directory = {}
    path = srcpath(src["path"])
    if path is None: continue
    # the renderer opens `root / rel` as written (pathlib keeps `a/../b`; the kernel
    # refuses it when `a` is missing) — a source that does not open is a RenderError,
    # never a directory this audit credits (round 26)
    if not pathlib.Path(src["path"].strip().lstrip("/")).is_dir():
        print("FAIL Application %s names a source directory the renderer cannot open (missing, or reached through a missing directory; the path is not repeated) — the renderer refuses the set" % display.shown(name, "object-name")); sys.exit(3)
    dirapps.append((path, ns, bool(directory.get("recurse")), patterns(directory.get("exclude") or "")))
# a pair file (clusters/orbit/<project>-<env>.yaml, or clusters/apps/) is an Application
# PLUS companion documents: orbitd's renderer applies the companions into that
# Application's destination namespace (render.py: extras), and its tenant tree applies the
# clusters/apps/namespaces.yaml documents whose explicit namespace is the destination
# (round 18) — a widening policy beside the Application is applied, so it is audited
companions, appdests = {}, set()
PAIR_DIRS = (str(root / "orbit"), str(root / "apps"))     # render.py: clusters/orbit/<p>-<e>.yaml, or clusters/apps/
for f in files:
    for d in load(f) or []:
        if d.get("kind") != "Application": continue
        ns = ((d.get("spec") or {}).get("destination") or {}).get("namespace")
        if not isinstance(ns, str) or not ns: continue
        appdests.add(ns)
        if str(f.parent) in PAIR_DIRS: companions.setdefault(str(f).strip("/"), []).append(ns)
TENANT_TREE = str(root / "apps" / "namespaces.yaml").strip("/")
def applied_into(f):
    """The destination namespaces of EVERY Application that applies this file (the
    renderer renders each Application independently — round 17: one file under two
    Applications is applied twice, into two namespaces), in file order; [] if none."""
    rel = str(f).strip("/"); out = []
    for prefix, ns, recurse, excl in dirapps:
        if not (rel == prefix or rel.startswith(prefix + "/")): continue
        sub = rel[len(prefix) + 1:]
        if not recurse and "/" in sub: continue
        if any(glob_match(sub, p) for p in excl): continue
        if ns not in out: out.append(ns)
    for ns in companions.get(rel, []):
        if ns not in out: out.append(ns)
    return out
found, bad = [], 0
for f in files:
    dests = applied_into(f)
    tenant = str(f).strip("/") == TENANT_TREE
    if not dests and not tenant: continue
    docs = load(f)
    if docs is None:
        bad += 1; continue        # an applied file the renderer cannot read
    for d in docs:
        if d.get("kind") != "NetworkPolicy": continue
        explicit = (d.get("metadata") or {}).get("namespace")
        if tenant and not dests and explicit not in appdests: continue
        for ns in ([explicit] if explicit else dests):
            if ns == want:
                dd = copy.deepcopy(d); dd.setdefault("metadata", {})["namespace"] = ns
                found.append(dd); break
# the renderer keeps ONE object per (kind, namespace, name) — the last in sorted file
# order (orbitd platform.py, by_key) — so an earlier policy of the same identity is never
# applied; the audit reads the same effective set, and names the duplication (round 25)
by_key, dups = {}, set()
for dd in found:
    key = (dd.get("kind"), dd["metadata"].get("namespace"), (dd["metadata"] or {}).get("name"))
    if key in by_key: dups.add(key)
    by_key[key] = dd
found = list(by_key.values())
yaml.safe_dump_all(found, open(out, "w"), sort_keys=False)
if bad:
    print("%d applied manifest file(s) under clusters/ cannot be read as YAML (the renderer refuses the whole tree; the parser's message is not repeated)" % bad); sys.exit(3)
if dups:
    print("%d NetworkPolicy identity(ies) are rendered more than once under clusters/ — the renderer applies only the LAST in file order, so the earlier one's isolation never lands (the names are not repeated)" % len(dups)); sys.exit(3)
PY
}
NETPOL=clusters/platform/config/monitoring/netpol.yaml
if [ ! -f "$NETPOL" ]; then
  bad "$NETPOL is absent — Prometheus, Grafana and Alertmanager answer every pod in the cluster, and Alertmanager has no authentication of any kind"
else
  # policies are ADDITIVE: a second file anywhere under clusters/ that selects
  # Alertmanager widens the grant just as well, so every rendered NetworkPolicy for ns
  # monitoring is gathered and audited as one set
  if GERR=$(gather_netpols monitoring "$TMP/monitoring-netpols.yaml"); then :; else
    bad "the monitoring policy set could not be gathered: $GERR — the Alertmanager grant is UNAUDITED"; : > "$TMP/monitoring-netpols.yaml"
  fi
  NETPOL="$TMP/monitoring-netpols.yaml"     # the gathered set stands in for the one file
  WOUT=$(python3 - "$NETPOL" <<'PY'
import re, sys, yaml
import shown as display   # the shared display rule (scripts/verify/shown.py)
try:
    docs = [d for d in yaml.safe_load_all(open(sys.argv[1])) if isinstance(d, dict)]
except Exception as exc:     # the class only: a parser's message quotes the input
    print("FAIL %s cannot be read as YAML (%s)" % (sys.argv[1], type(exc).__name__)); sys.exit(1)

def ns_selects_loki(ns):
    # None => the peer has NO namespaceSelector => it selects the policy's OWN namespace
    #         (monitoring), never loki -> not a loki peer, out of scope.
    if ns is None:
        return False
    ml = ns.get('matchLabels') or {}
    me = ns.get('matchExpressions') or []
    if not ml and not me:            # empty {} selects ALL namespaces, loki among them
        return True
    # CONSERVATIVE: the peer is out of scope only when its selector PROVABLY excludes
    # loki — a metadata.name label pinned to one other namespace, or `In` expressions on
    # metadata.name whose values do not include loki. Any other form (a NotIn, an Exists,
    # a label loki may carry, an operator this reader does not model) may admit loki and
    # is read as a loki peer, so a missing podSelector on it is an offender.
    if ml:
        name = ml.get('kubernetes.io/metadata.name')
        if name is not None and name != 'loki' and not me:
            return False
        return True
    for e in me:
        if not isinstance(e, dict):
            return True
        if e.get('key') == 'kubernetes.io/metadata.name' and e.get('operator') == 'In':
            if 'loki' in (e.get('values') or []):
                return True
            continue                     # an In on another name: excludes loki, keep reading
        return True                      # NotIn / Exists / DoesNotExist / another key: not provable
    return False

def pod_narrowed(pod):
    # A POSITIVE pin only: a non-empty matchLabels, or an `In` expression with values.
    # podSelector {} matches EVERY pod; so does {matchExpressions: [{key: x, operator:
    # DoesNotExist}]} when no pod carries x, and Exists/NotIn prove nothing about which
    # workload is admitted — none of those is a narrowing.
    if not isinstance(pod, dict) or not pod:
        return False
    ml = pod.get('matchLabels')
    if isinstance(ml, dict) and any(isinstance(k, str) and isinstance(v, str) and v for k, v in ml.items()):
        return True
    me = pod.get('matchExpressions')
    return isinstance(me, list) and any(isinstance(e, dict) and e.get('operator') == 'In'
                                        and isinstance(e.get('values'), list) and e['values']
                                        and all(isinstance(v, str) and v for v in e['values']) for e in me)

def rule_admits_9093(rule):
    ports = rule.get('ports')
    if not ports:                    # no ports key OR an empty list => every port, incl 9093
        return True
    for p in ports:
        if not isinstance(p, dict):
            return True              # a port entry this reader cannot read is not a narrowing
        proto = p.get('protocol')
        if proto is not None and str(proto).upper() != 'TCP':
            continue                 # a UDP/SCTP entry admits nothing on TCP :9093 (round 20)
        port, end = p.get('port'), p.get('endPort')
        if port is None or not str(port).isdigit():
            return True              # absent = all ports; a named port cannot be proven not-9093
        if end is not None:
            if not str(end).isdigit():
                return True          # a range this reader cannot read is not a narrowing
            if int(port) <= 9093 <= int(end):
                return True          # the range covers 9093
        elif int(port) == 9093:
            return True
    return False

def targets_alertmanager(doc):
    """Every policy whose podSelector selects the Alertmanager pod: the one keyed on its
    label, AND an empty podSelector, which selects every pod in the namespace — policies
    are additive, so a second policy admitting the world admits it to Alertmanager too."""
    if doc.get('kind') != 'NetworkPolicy' \
       or (doc.get('metadata') or {}).get('namespace') != 'monitoring':
        return False
    psel = (doc.get('spec') or {}).get('podSelector')
    if not psel or not isinstance(psel, dict):     # {} or absent: selects every pod
        return True
    return selects_alertmanager(psel)

# The labels the operator puts on the Alertmanager pod: two with a value this reader
# knows, two whose value it does not. A selector EXCLUDES Alertmanager only when a term
# contradicts a known label; a term on a key this reader cannot evaluate (another label,
# Exists) is assumed to match — policies are additive, so the conservative answer to
# "does this policy also govern Alertmanager" is yes.
AM_KNOWN = {'app.kubernetes.io/name': 'alertmanager', 'app.kubernetes.io/managed-by': 'prometheus-operator'}
def selects_alertmanager(psel):
    for k, v in (psel.get('matchLabels') or {}).items():
        if k in AM_KNOWN and str(v) != AM_KNOWN[k]:
            return False
    for e in (psel.get('matchExpressions') or []):
        if not isinstance(e, dict):
            return True                            # unreadable term: not a proof of exclusion
        k, op, vals = e.get('key'), e.get('operator'), [str(x) for x in (e.get('values') or [])]
        if k not in AM_KNOWN:
            continue
        if op == 'In' and AM_KNOWN[k] not in vals: return False
        if op == 'NotIn' and AM_KNOWN[k] in vals: return False
        if op == 'DoesNotExist': return False
    return True

policies = [d for d in docs if d.get('kind') == 'NetworkPolicy'
            and (d.get('metadata') or {}).get('namespace') == 'monitoring']
if not policies:
    print("FAIL no NetworkPolicy for ns monitoring in this file — the observability plane "
          "is default-ALLOW"); sys.exit(1)
def isolates_ingress(d):
    # a policy isolates a pod for INGRESS only when Ingress is among its policyTypes;
    # with policyTypes absent the API server infers Ingress (always) and Egress (when
    # egress rules exist) — an explicit [Egress] isolates nothing on the way in
    types = (d.get('spec') or {}).get('policyTypes')
    if types is None:
        return True
    return isinstance(types, list) and 'Ingress' in types
def selects_workload(psel, want):
    """The whole selector, not its matchLabels alone: a matchExpressions term on the
    same key that contradicts the label (In without it, NotIn with it, DoesNotExist)
    selects NO pod, and a policy selecting no pod isolates nothing. A constraint on ANY
    other key (round 17: matchLabels x=yes plus DoesNotExist x, or a label the workload
    lacks) cannot be proven to select the workload either — the shipped selectors carry
    the one app.kubernetes.io/name label and nothing else."""
    if not isinstance(psel, dict): return False
    ml = psel.get('matchLabels') or {}
    mx = psel.get('matchExpressions') or []
    if not isinstance(ml, dict) or not isinstance(mx, list) or not all(isinstance(e, dict) for e in mx): return False
    if ml.get('app.kubernetes.io/name') != want: return False
    if set(map(str, ml)) - {'app.kubernetes.io/name'} or {str(e.get('key')) for e in mx} - {'app.kubernetes.io/name'}: return False
    for e in mx:
        if e.get('key') != 'app.kubernetes.io/name': continue
        op, vals = e.get('operator'), [str(x) for x in (e.get('values') or [])]
        if op == 'In' and want not in vals: return False
        if op == 'NotIn' and want in vals: return False
        if op == 'DoesNotExist': return False
    return True
targeted = {want for want in ('prometheus', 'grafana', 'alertmanager')
            if any(isolates_ingress(d) and selects_workload((d.get('spec') or {}).get('podSelector'), want) for d in policies)}
for want in ('prometheus', 'grafana', 'alertmanager'):
    if want not in targeted:
        print("FAIL no policy selects %s for INGRESS (policyTypes without Ingress isolates "
              "nothing on the way in) — that workload is reachable from every pod in "
              "the cluster" % want); sys.exit(1)

offenders, checked = [], 0
for di, doc in enumerate(docs):
    if not targets_alertmanager(doc):
        continue
    # a policy's name is file text: shown with an object name's shape, else by position
    name = (doc.get('metadata') or {}).get('name')
    name = name if display.fits(name, "object-name") else "(document #%d, a name not of an object-name shape, or of a credential's shape)" % di
    for ri, rule in enumerate((doc.get('spec') or {}).get('ingress') or []):
        if not rule_admits_9093(rule):
            continue
        if not rule.get('from'):
            # an ingress rule with no `from` admits EVERY peer on those ports
            offenders.append("policy %r ingress[%d] admits every peer (no from:)" % (name, ri)); continue
        for pi, peer in enumerate(rule.get('from') or []):
            if not isinstance(peer, dict) or not (peer.get('namespaceSelector') is not None
                                                  or peer.get('podSelector') is not None or peer.get('ipBlock') is not None):
                offenders.append("policy %r ingress[%d] from[%d] is a peer of no readable form" % (name, ri, pi)); continue
            if peer.get('ipBlock') is not None:
                # an IP range is not a pod: no range is a sanctioned Alertmanager client
                offenders.append("policy %r ingress[%d] from[%d] admits an IP range (ipBlock) to :9093" % (name, ri, pi)); continue
            if not ns_selects_loki(peer.get('namespaceSelector')):
                continue
            checked += 1
            if not pod_narrowed(peer.get('podSelector')):
                offenders.append("policy %r ingress[%d] from[%d]" % (name, ri, pi))
if offenders:
    print("FAIL bare namespace-wide loki grant at " + "; ".join(offenders)); sys.exit(1)
print("OK 3 workloads default-deny, %d loki peer(s) on AM :9093, every one pod-narrowed by a positive label pin" % checked)
PY
)
  [ $? -eq 0 ] && ok "static-parse (no cluster): Prometheus, Grafana and Alertmanager are each default-deny, and every loki from-peer admitted to Alertmanager :9093 is pod-narrowed [$WOUT] — an unauthenticated POST to :9093 dispatches an agent, pages a human with chosen text and suppresses alerts two ways, so an OR-split back to whole-namespace fails THIS row, the widening the CNI-lagged live probe is blind to" \
               || bad "static-parse: $WOUT"
fi
if [ "$LIVEOFF" = 1 ]; then
  keyoff_live "the live observability-plane dials key off"
else
  # One ephemeral pod in ANOTHER namespace tries a direct dial of each and must be BLOCKED
  # at L3/L4 (no HTTP answer at all). Red the moment a policy is deleted, because the dial
  # then reaches the Service and answers HTTP.
  # On when the feature is declared on AND the app was applied. The stored inventory alone
  # is not "on": a retired app keeps it, and with traces off this block dialled a Tempo
  # that was gone and failed "Tempo accepted ZERO spans" (docs/V1.md §8, the minimal profile).
  TEMPO_ON=0; LOKI_ON=0
  [ "$FEATURE_TRACES" = 1 ] && kubectl -n orbit get configmap orbit-platform-inventory-tempo >/dev/null 2>&1 && TEMPO_ON=1
  [ "$FEATURE_LOGS" = 1 ]   && kubectl -n orbit get configmap orbit-platform-inventory-loki  >/dev/null 2>&1 && LOKI_ON=1
  OPPOD="orbit-obsplane-probe-$$"
  # THE CONTROL, and why 000 alone proves nothing (round 1, Sol 5). This block reads a
  # blocked dial as `000` — but a missing curl, a DNS failure, a pod that never got an
  # address and an actual L3/L4 denial all produce exactly `000`, so an unqualified 000 is
  # a row that passes when the PROBE is broken rather than when the POLICY holds. The pod
  # therefore dials something it IS allowed to reach first: the apiserver's own Service,
  # which no NetworkPolicy in this tree selects. A real HTTP status from that (401 without
  # a token is a fine answer — it means the packet arrived) proves curl runs, CoreDNS
  # answers, and egress from this pod works, so the 000s that follow can only be the
  # policy. The MARK line proves the shell ran to completion rather than the output being
  # truncated.
  OPSCRIPT='
    C=$(curl -s -k -o /dev/null -w "%{http_code}" --max-time 8 https://kubernetes.default.svc/healthz 2>/dev/null); echo "CONTROL=${C:-000}"
    P=$(curl -s -o /dev/null -w "%{http_code}" --max-time 6 http://kube-prometheus-stack-prometheus.monitoring.svc:9090/api/v1/query?query=up 2>/dev/null); echo "PROM=${P:-000}"
    A=$(curl -s -o /dev/null -w "%{http_code}" --max-time 6 http://kube-prometheus-stack-alertmanager.monitoring.svc:9093/api/v2/status 2>/dev/null); echo "AM=${A:-000}"
'
  # Loki and Tempo are bucket-gated components. Their dials MUST key off when they are not
  # deployed: the name would not resolve, curl would return 000, and 000 is the PASS value
  # for a block check — so the assertion would go falsely GREEN for a policy that does not
  # exist. Decide once, here, and gate the dial and the assertion together.
  [ "$LOKI_ON" = 1 ] && OPSCRIPT="$OPSCRIPT"'
    L=$(curl -s -o /dev/null -w "%{http_code}" --max-time 6 http://loki.loki.svc:3100/loki/api/v1/labels 2>/dev/null); echo "LOKI=${L:-000}"
'
  [ "$TEMPO_ON" = 1 ] && OPSCRIPT="$OPSCRIPT"'
    T=$(curl -s -o /dev/null -w "%{http_code}" --max-time 6 http://tempo.tempo.svc:3200/api/search?limit=1 2>/dev/null); echo "TEMPO=${T:-000}"
'
  OPSCRIPT="$OPSCRIPT"'
    echo "MARK=obsplane-complete"
'
  # the pod's stdout is remote text: a private file, never a shell variable a traced
  # shell prints; only the five markers are read out of it, each shaped below
  OPF="$TMP/obsplane.out"
  kubectl run "$OPPOD" -n default --image=curlimages/curl:8.11.1 --restart=Never -i --rm --quiet \
    --pod-running-timeout=90s --command -- sh -c "$OPSCRIPT" > "$OPF" 2>/dev/null
  OPPROM=$(marker PROM "$OPF"); OPLOKI=$(marker LOKI "$OPF"); OPAM=$(marker AM "$OPF")
  OPTEMPO=$(marker TEMPO "$OPF"); OPCTL=$(marker CONTROL "$OPF")
  OPMARK=$(grep -c '^MARK=obsplane-complete' "$OPF" 2>/dev/null || true)
  OPMISS=""
  [ -n "$OPPROM" ] || OPMISS="$OPMISS PROM"
  [ -n "$OPAM" ]   || OPMISS="$OPMISS AM"
  [ -n "$OPCTL" ]  || OPMISS="$OPMISS CONTROL"
  [ "$LOKI_ON"  = 1 ] && [ -z "$OPLOKI" ]  && OPMISS="$OPMISS LOKI"
  [ "$TEMPO_ON" = 1 ] && [ -z "$OPTEMPO" ] && OPMISS="$OPMISS TEMPO"
  [ "${OPMARK:-0}" -ge 1 ] || OPMISS="$OPMISS MARK"
  if [ ! -s "$OPF" ]; then
    bad "the off-path probe pod produced no output at all — the observability-plane denials are UNTESTED, not proven"
  elif [ -n "$OPMISS" ]; then
    bad "the off-path probe pod's output is missing marker(s)$OPMISS — the script did not run to completion, so the 000s it did print are not evidence of a denial; the observability-plane denials are UNTESTED"
  elif [ "${OPCTL:-000}" = "000" ] || [ "$OPCTL" = unreadable ]; then
    bad "the off-path probe pod could not reach even the apiserver Service, which NO policy in this tree denies it (CONTROL=$OPCTL, not an HTTP status) — curl, CoreDNS or this pod's egress is broken, and a 000 from Prometheus or Alertmanager therefore says nothing about the netpols. The denials are UNTESTED, not proven"
  else
    ok "the off-path probe pod reached what it IS allowed to reach (the apiserver Service answered HTTP $OPCTL) and printed every marker — so each 000 below is a policy denial, not a broken probe"
    case "$OPPROM" in
      000) ok "a direct dial of Prometheus :9090 is BLOCKED from another namespace (netpol holds)";;
      *) bad "Prometheus :9090 answered HTTP $OPPROM to an off-path pod — the monitoring netpol is gone or ineffective";;
    esac
    # Alertmanager has NO authz, so :9093 is the whole v2 API: POST /api/v2/alerts
    # DISPATCHES an agent (the intake's bearer is AM-supplied, so a forged alert is
    # authenticated), PAGES a human with attacker text, and SUPPRESSES real alerts.
    case "$OPAM" in
      000) ok "a direct dial of Alertmanager :9093 is BLOCKED from another namespace (netpol holds — the logs-trigger door does not admit arbitrary pods)";;
      *) bad "Alertmanager :9093 answered HTTP $OPAM to an off-path pod — a forged alert can dispatch an agent, page a human, and suppress alerts";;
    esac
    if [ "$LOKI_ON" = 1 ]; then
      case "$OPLOKI" in
        000) ok "a direct dial of Loki :3100 is BLOCKED from another namespace (netpol holds)";;
        *) bad "Loki :3100 answered HTTP $OPLOKI to an off-path pod — the loki netpol is gone or ineffective";;
      esac
    else
      keyoff_absent "loki app not deployed — its dial rows key off (arm on deploy)"
    fi
    if [ "$TEMPO_ON" = 1 ]; then
      case "$OPTEMPO" in
        000) ok "a direct dial of Tempo's query API :3200 is BLOCKED from another namespace (netpol holds)";;
        *) bad "Tempo :3200 answered HTTP $OPTEMPO to an off-path pod — the tempo netpol is gone or ineffective";;
      esac
    else
      keyoff_absent "tempo app not deployed — its dial rows key off (arm on deploy)"
    fi
  fi
  # The AND, proven: the Alertmanager grant carries a namespaceSelector AND a podSelector
  # in ONE from-element, so it admits the Loki RULER POD, not the loki NAMESPACE. A pod
  # that lives IN ns loki without the loki app label must ALSO be refused at :9093. This
  # is the row that goes red if the from-element is ever split.
  if [ "$LOKI_ON" = 1 ]; then
    LNPOD="orbit-loki-nolabel-probe-$$"
    LNF="$TMP/lokiprobe.out"
    kubectl run "$LNPOD" -n loki --image=curlimages/curl:8.11.1 --restart=Never -i --rm --quiet \
      --pod-running-timeout=90s --command -- sh -c \
      'C=$(curl -s -k -o /dev/null -w "%{http_code}" --max-time 8 https://kubernetes.default.svc/healthz 2>/dev/null); echo "CONTROL=${C:-000}"
       A=$(curl -s -o /dev/null -w "%{http_code}" --max-time 6 http://kube-prometheus-stack-alertmanager.monitoring.svc:9093/api/v2/status 2>/dev/null); echo "AM=${A:-000}"
       echo "MARK=loki-nolabel-complete"' > "$LNF" 2>/dev/null
    LNAM=$(marker AM "$LNF"); LNCTL=$(marker CONTROL "$LNF")
    LNMARK=$(grep -c '^MARK=loki-nolabel-complete' "$LNF" 2>/dev/null || true)
    if [ ! -s "$LNF" ]; then
      bad "the in-loki-namespace probe pod produced no output — the podSelector narrowing is UNTESTED"
    elif [ -z "$LNAM" ] || [ "${LNMARK:-0}" -lt 1 ]; then
      bad "the in-loki-namespace probe pod did not print both of its markers — the script did not run to completion, so its silence is not evidence of a denial and the podSelector narrowing is UNTESTED"
    elif [ "${LNCTL:-000}" = "000" ]; then
      bad "the in-loki-namespace probe pod could not reach the apiserver Service either (CONTROL=$LNCTL), which no policy denies it — curl, CoreDNS or egress is broken in that pod, so its 000 at :9093 says nothing about the podSelector narrowing"
    else
      case "$LNAM" in
        000) ok "a pod IN ns loki but WITHOUT the loki app label is BLOCKED at Alertmanager :9093, and the same pod reached the apiserver Service (HTTP $LNCTL) in the same run — so this is the podSelector AND, not a broken probe and not the namespace";;
        *) bad "an unlabeled pod in ns loki reached Alertmanager :9093 (HTTP $LNAM) — the netpol admits the whole loki namespace, not just the ruler pod";;
      esac
    fi
    # The positive half — the ruler pod IS admitted — cannot be proven with an ephemeral
    # probe (see the CNI note above), so it is proven by a REAL long-lived client's
    # traffic: a `logs` trigger's alert is evaluated ONLY by the Loki ruler, so that alert
    # appearing in Alertmanager is proof loki reached :9093 through the netpol. Armed only
    # when a logs trigger is actually deployed.
    # absent means the API SAID NotFound: a Forbidden or a server error is a read that
    # did not happen, and keys nothing off
    kubectl get configmap -n loki orbit-agents-logs-triggers -o name > /dev/null 2> "$TMP/logs-cm.err"; LCMRC=$?
    if [ "$LCMRC" -ne 0 ] && ! grep -q "NotFound" "$TMP/logs-cm.err" 2>/dev/null; then
      bad "could not read the logs-trigger ConfigMap in ns loki (kubectl rc=$LCMRC, not a NotFound) — the loki-ruler->:9093 proof is UNCHECKED, not keyed off"
    elif [ "$LCMRC" -eq 0 ]; then
      # Both lists are the cluster's text (a ConfigMap, the Alertmanager API): they go
      # to scratch files and are intersected LITERALLY in python, as sets of names —
      # never through a shell word split or a grep pattern, where `Unrelated.*` in the
      # ConfigMap would match any alert. Only name-shaped alert names are repeated.
      kubectl get configmap -n loki orbit-agents-logs-triggers -o jsonpath='{.data.orbit-agents-logs\.yaml}' > "$TMP/logs-rules.yaml" 2>/dev/null
      amraw /api/v2/alerts > "$TMP/am-alerts.json"
      read -r HIT WANT < <(python3 - "$TMP/logs-rules.yaml" "$TMP/am-alerts.json" <<'PY'
import json, re, sys, yaml
import shown as display   # the shared display rule (scripts/verify/shown.py)
NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,127}")
try: d = yaml.safe_load(open(sys.argv[1])) or {}
except Exception: d = {}
want = [r.get("alert") for g in (d.get("groups") or []) if isinstance(g, dict) for r in (g.get("rules") or []) if isinstance(r, dict)]
want = [w for w in want if isinstance(w, str) and w]
try: got = {(a.get("labels") or {}).get("alertname") for a in json.load(open(sys.argv[2]))}
except Exception: got = set()
hit = any(w in got for w in want)                      # literal set membership
good = [w for w in want if display.fits(w, "alert-name")]; odd = len(want) - len(good)
print(("1" if hit else "0") + " " + " ".join(good) + (" (+%d rule name(s) not of an alert-name shape, or of a credential's shape)" % odd if odd else ""))
PY
)
      # A healthy installation has NO logs alert firing most of the time, so the proof
      # cannot demand one. Without a firing alert the ruler's own notifier counters
      # stand in — but only counters of TRAFFIC prove a path: a notifier that has sent
      # nothing has tried nothing, and a blocked connection shows in no counter until
      # it does. So: sent > 0 and nothing failed or dropped in the hour is the positive
      # half; discovered but silent is UNVERIFIED, said so, and not a pass or a fail.
      RDISC=$(promval 'max(loki_prometheus_notifications_alertmanagers_discovered)')
      RSENT=$(promval 'sum(increase(loki_prometheus_notifications_sent_total[1h])) or vector(0)')
      RERR=$(promval 'sum(increase(loki_prometheus_notifications_errors_total[1h])) + sum(increase(loki_prometheus_notifications_dropped_total[1h])) or vector(0)')
      rpos(){ python3 -c "import sys;sys.exit(0 if float(sys.argv[1]) > 0 else 1)" "$1" 2>/dev/null; }
      rzero(){ python3 -c "import sys;sys.exit(0 if float(sys.argv[1]) == 0 else 1)" "$1" 2>/dev/null; }
      if [ "$HIT" = "1" ]; then
        ok "a Loki-ruler logs-trigger alert is live in Alertmanager — the ruler reached :9093, so the podSelector admits it (the positive half)"
      elif rpos "${RSENT:-0}" && rzero "${RERR:-1}"; then
        ok "no logs-trigger alert is firing right now; the Loki ruler sent notifications to Alertmanager in the last hour and failed or dropped none (the positive half, by the ruler's own traffic counters)"
      elif case "$RDISC" in QUERYERR|ABSENT|""|0|0.0) false;; *) true;; esac && rzero "${RSENT:-QUERYERR}" && rzero "${RERR:-1}"; then
        ok "[UNVERIFIED] the Loki ruler has discovered Alertmanager but sent nothing in the last hour (no logs alert to send), so the loki->:9093 admit rule is not proven either way — it is proven the first time a logs trigger fires"
      else
        bad "a logs-trigger ConfigMap is deployed, no Loki-ruler alert ($WANT) is in Alertmanager, and the ruler's notifier counters do not clear it (discovered=$RDISC, sent/1h=$RSENT, errors+dropped/1h=$RERR) — the loki->:9093 admit rule is missing or over-narrow"
      fi
    else
      keyoff_absent "no logs trigger deployed — the loki-ruler->:9093 ADMIT proof keys off (it arms the moment a logs trigger's ConfigMap lands in ns loki)"
    fi
  fi
  if [ "$TEMPO_ON" = 1 ]; then
    # Tempo's two ALLOW halves, proven by the REAL long-lived clients rather than by a
    # throwaway pod (which this CNI refuses for every allow rule, making such a check
    # false-red forever). WRITE: Traefik's root span and every injected SDK push OTLP
    # continuously, so the receiver's accepted-span counter must be moving. READ:
    # Grafana's own datasource health check makes the long-lived Grafana pod dial
    # tempo:3200 and report what it got — exactly the path the netpol's first rule permits.
    SPANS=$(promval 'sum(increase(tempo_receiver_accepted_spans[10m])) or vector(0)')
    case "$SPANS" in
      QUERYERR|ABSENT|"") bad "could not read Tempo's accepted-span counter ($SPANS) — trace ingest is UNCHECKED";;
      *) python3 -c "import sys;sys.exit(0 if float(sys.argv[1]) > 0 else 1)" "$SPANS" 2>/dev/null \
           && ok "Tempo's OTLP ingest is LIVE — $(printf '%.0f' "$SPANS") spans accepted in the last 10m (the netpol admits the real writers)" \
           || bad "Tempo accepted ZERO spans in 10m — trace ingest is dead; the netpol is over-blocking :4317/:4318 or the writers stopped";;
    esac
    if [ "$HAVEPW" = 1 ] && [ "$FEATURE_DASHBOARDS" = 1 ]; then
      # the health status is Grafana's text: a fixed category (OK, not-OK, unreadable)
      # is all that reaches the shell
      TDS=$(grafapi /api/datasources/uid/tempo/health | python3 -c "import json,sys
try:
    d = json.load(sys.stdin); s = d.get('status') if isinstance(d, dict) else None
    print('OK' if s == 'OK' else ('not-OK' if isinstance(s, str) and s else 'unreadable'))
except Exception: print('unreadable')")
      [ "$TDS" = "OK" ] \
        && ok "Grafana's Tempo datasource reports OK — the real reader in monitoring reaches :3200 (netpol admits it)" \
        || bad "Grafana's Tempo datasource health is $TDS (its status text is not repeated) — monitoring cannot reach Tempo :3200; the netpol's read rule is gone or narrowed"
    fi
  fi
fi

echo "== features: what is declared is what is applied =="
# One row per Application an optional feature owns (provisioning/features.yaml), keyed on
# the app's stored inventory, both ways: a feature that is on with an app that was never
# applied is a feature the instance asked for and does not have; a feature that is off
# with an app still applied is one that still runs and still costs its memory.
if [ "$FEATURES_BAD" = 1 ]; then
  ok "the features: block could not be read — the feature rows key off; the row that read it carries the verdict"
elif [ -z "$FEATURE_APPS" ]; then
  ok "this tree carries no features registry with optional Applications (vendored before features) — the feature rows key off"
elif [ "$LIVEOFF" = 1 ]; then
  keyoff_live "the feature rows key off"
else
  for triple in $FEATURE_APPS; do
    feat=${triple%%:*}; rest=${triple#*:}; app=${rest%%:*}; on=${rest##*:}
    # applied = its stored inventory lists an object other than a Namespace: a retired app
    # keeps its inventory ConfigMap, and in it the namespace a retire never deletes
    if kubectl -n orbit get configmap "orbit-platform-inventory-$app" -o jsonpath='{.data.objects}' 2>/dev/null \
         | grep -v '/Namespace/' | grep -q '[^[:space:]]'; then have=1; else have=0; fi
    case "$on$have" in
      11) ok "feature $feat is on and its app $app is applied";;
      00) ok "feature $feat is off and its app $app is not applied";;
      10) bad "feature $feat is on but its app $app is not applied (no orbit-platform-inventory-$app in namespace orbit) — DECLARED AND NOT LIVE: apply it (orbit platform apply --app $app)";;
      01) bad "feature $feat is off but its app $app is still applied — it still runs and still costs its memory: retire it (orbit platform retire --app $app)";;
    esac
  done
  # backups owns no Application of its own: its workload is the barman-cloud plugin, a
  # directory inside platform-config, so it is asked of the cluster by name
  if kubectl -n cnpg-system get deploy barman-cloud >/dev/null 2>&1; then have=1; else have=0; fi
  case "${FEATURE_BACKUPS:-0}$have" in
    11) ok "feature backups is on and its plugin (barman-cloud in cnpg-system) is deployed";;
    00) ok "feature backups is off and its plugin (barman-cloud in cnpg-system) is not deployed";;
    10) bad "feature backups is on but its plugin (barman-cloud in cnpg-system) is not deployed — DECLARED AND NOT LIVE: apply it (orbit platform apply --app platform-config)";;
    01) bad "feature backups is off but its plugin (barman-cloud in cnpg-system) is still deployed — it still runs and still costs its memory: converge the features (orbit features converge)";;
  esac
fi

echo "== logs: Loki serves what Alloy collects =="
# Keyed off the app's stored inventory — a declaration (applied intent), not
# reachability — and doubly off the bucket gate, since scripts/instance-init.py deletes
# template/provisioning/clusters/platform/apps/{loki,alloy}.yaml on a bucket-less orbit.
if [ "$LIVEOFF" = 1 ]; then
  keyoff_live "the log-pipeline rows key off"
elif ! kubectl -n orbit get configmap orbit-platform-inventory-loki >/dev/null 2>&1; then
  if [ "$FEATURE_LOGS" = 1 ] && [ "$ICLUSTER" = 1 ]; then
    bad "the logs feature is on, so loki.yaml is rendered — but no app loki is applied on a reachable cluster: DECLARED AND NOT LIVE (the desk has not applied it, or it was pruned). That is red, not a keyed-off pass"
  else
    keyoff_absent "the logs feature is off / no loki app — the log-pipeline rows key off (they arm with the logs feature and the first apply)"
  fi
elif [ "$FEATURE_LOGS" != 1 ]; then
  ok "the logs feature is off — the log-pipeline rows key off; the features row is what goes red for the loki app that is still applied"
elif [ "$FEATURE_DASHBOARDS" = 1 ] && [ "$HAVEPW" = 0 ]; then
  bad "no Grafana admin password on this workstation — every Loki row below is asked through Grafana's datasource proxy and CANNOT run"
else
  # WHAT THIS ROW IS FOR: a BROKEN log pipeline (alloy -> loki). Nothing else in the suite
  # notices a dead collector; it is invisible until the night someone needs a log and
  # there is none.
  #
  # It is built on the only honest reference for "what SHOULD be in Loki": the KUBELET.
  # Counting lines under a namespace label is NOT evidence anything was collected —
  # measured, Alloy injects its OWN tailer errors into every target's stream, so a
  # namespace whose pods wrote nothing at all can still hold thousands of lines. And an
  # empty 5-minute window is not evidence of breakage either: an agent plane between jobs
  # logs nothing for hours. So: take the newest REAL line the kubelet holds for a pod of
  # the namespace and assert Loki holds THAT EXACT LINE for THAT pod. Content, not counts.
  #
  # Tenant activity is handled by BRANCHING, never by keying off. Every branch ends in an
  # assertion and names itself:
  #   ACTIVE  kubelet has a line <=15m old -> that fresh line must be in Loki.     RED if not.
  #   IDLE    the newest line is older     -> that historical line must STILL be in
  #                                           Loki, AND the pipeline must be proven
  #                                           alive right now by the control.      RED if either.
  #   SILENT  no pod wrote anything in 6h  -> nothing to collect, so the control
  #                                           alone carries the row.               RED if the control fails.
  #
  # THE CONTROL is Traefik: the ingress every request crosses, access logging on, and its
  # cadence is the PLATFORM's own uptime probes rather than tenant traffic. If Traefik
  # itself has said nothing in 5m, or Loki does not answer, pipeline liveness is
  # UNANSWERABLE and the row goes RED — "idle" must never become the excuse that also
  # swallows "broken".
  # A log line may hold anything (a token an app printed), and a request URI lands in
  # Traefik's and Grafana's access logs — so the line is NEVER part of the request
  # (round 21): Loki is asked for the pod's stream in a bounded window by label matchers
  # alone, and the comparison with the line happens here, in this process, from a file.
  # The selector still travels in the same stdin config document as the password and is
  # never a shell value. LFIND <selector-file> <from-epoch> <to-epoch> <needle-file> ->
  # how many held lines in the window carry the needle as a literal substring.
  # LRANGE <selector-file> <from-epoch> <to-epoch> -> Loki's query_range answer. Through
  # Grafana's datasource proxy while the dashboards feature is on (that is the path a
  # person's query takes, so it is the one checked); with dashboards off there is no
  # Grafana, and the same question goes to Loki through the API server's service proxy.
  # The selector is label matchers only (never a log line), so it may ride in a URL.
  LRANGE_GRAFANA(){ grafpinned || return 1; { grafauth; python3 -c 'import sys;v=open(sys.argv[1],"rb").read().decode("utf-8","surrogateescape");v=v.replace("\\","\\\\").replace("\"","\\\"");sys.stdout.buffer.write(("data-urlencode = \"query=%s\"\n" % v).encode("utf-8","surrogateescape"))' "$1"; } | curl -q -s --max-time 25 -K - "${GRAFPIN[@]}" --get \
             --data-urlencode "limit=5000" \
             --data-urlencode "start=${2}000000000" \
             --data-urlencode "end=${3}000000000" \
             --data-urlencode "direction=backward" \
             "$GRAF/api/datasources/proxy/uid/loki/loki/api/v1/query_range"; }
  LRANGE_DIRECT(){ kubectl get --raw "$(python3 -c 'import sys,urllib.parse
q=open(sys.argv[1],"rb").read().decode("utf-8","surrogateescape")
print("/api/v1/namespaces/loki/services/loki:3100/proxy/loki/api/v1/query_range?"+urllib.parse.urlencode({"query":q,"limit":"5000","start":sys.argv[2]+"000000000","end":sys.argv[3]+"000000000","direction":"backward"}))' "$1" "$2" "$3")" 2>/dev/null; }
  LRANGE(){ if [ "$FEATURE_DASHBOARDS" = 1 ]; then LRANGE_GRAFANA "$@"; else LRANGE_DIRECT "$@"; fi; }
  LFIND(){ LRANGE "$1" "$2" "$3" \
           | python3 -c '
import json,re,sys
def unq(q):
    # the needle file holds the line as a quoted Go string (KLAST): back to its bytes
    if not re.fullmatch(r"\"(?:[^\"\\\x00-\x1f\x7f]|\\(?:[\"\\]|x[0-9a-f]{2}))*\"", q): raise ValueError
    out, s, i = bytearray(), q[1:-1], 0
    while i < len(s):
        if s[i] == "\\":
            if s[i+1] == "x": out += bytes([int(s[i+2:i+4], 16)]); i += 4
            else: out += s[i+1].encode(); i += 2
        else: out += s[i].encode("utf-8"); i += 1
    return bytes(out)
try: d=json.load(sys.stdin)
except Exception: print("QUERYERR"); sys.exit()
if d.get("status")!="success": print("QUERYERR"); sys.exit()
try:
    needle = unq(open(sys.argv[1],"rb").read().decode("utf-8").split("\t",1)[1].rstrip("\n"))
    streams = d["data"]["result"]
    if not isinstance(streams, list): raise ValueError
    print(sum(1 for s in streams for _, line in (s.get("values") or []) if needle in line.encode("utf-8","surrogateescape")))
except Exception: print("QUERYERR")' "$4"; }
  # KLAST <ns> <pod> <since> <out-file> -> writes "<epoch><TAB><needle>" to <out-file> for
  # that pod's newest non-blank stdout line inside <since>, read from the KUBELET; the file
  # is empty when the pod genuinely said nothing — the one fact Loki cannot be asked for.
  # The needle is the line's first 300 characters as a quoted Go string, the form LFIND's
  # parser reads back: `"` and `\` escaped, every control byte (NUL included) and every
  # byte that is not UTF-8 as `\xNN`, and everything else as itself. So the raw line never
  # passes through the shell (which drops NUL, and which prints every value it holds when
  # traced), is never an argument, and the same bytes the kubelet gave are compared. The caller
  # reads the epoch from the file with cut; the needle stays in the file. A kubelet read
  # that FAILS (forbidden, a pod gone mid-read, an API hiccup) writes the one word
  # UNREADABLE: a failed read is not a silent pod, and the caller keeps the two apart.
  KLAST(){ kubectl -n "$1" logs "$2" --all-containers=true --since="$3" --tail=50 --timestamps > "$TMP/klast.raw" 2>/dev/null \
             || { echo UNREADABLE > "$4"; return 1; }
           python3 -c '
import sys, datetime
def goq(s):
    out = []
    for ch in s:
        o = ord(ch)
        if ch == "\"": out.append("\\\"")
        elif ch == "\\": out.append("\\\\")
        elif 0xDC80 <= o <= 0xDCFF: out.append("\\x%02x" % (o - 0xDC00))
        elif o < 0x20 or o == 0x7F: out.append("\\x%02x" % o)
        else: out.append(ch)
    return "\"" + "".join(out) + "\""
last = None
for L in sys.stdin.buffer.read().split(b"\n"):
    L = L.decode("utf-8", "surrogateescape")
    ts, _, txt = L.partition(" ")
    if not txt.strip(): continue
    try: e = int(datetime.datetime.strptime(ts[:19], "%Y-%m-%dT%H:%M:%S").replace(tzinfo=datetime.timezone.utc).timestamp())
    except ValueError: continue
    if last is None or e >= last[0]: last = (e, txt)
if last: sys.stdout.buffer.write(("%d\t%s\n" % (last[0], goq(last[1][:300]))).encode("utf-8"))' < "$TMP/klast.raw" > "$4"; }
  # HELD <ns> <pod> <needle-file> -> does Loki hold THAT line for THAT pod? The needle
  # file is "<epoch><TAB><quoted Go string>" (KLAST); only a file of exactly that shape
  # becomes a query, and the query is the pod's stream selector alone (label values as
  # JSON strings, so a name full of quotes stays literal) over a ten-second window
  # around the epoch. The selector is written to a file and LFIND reads it from there;
  # the line itself is compared inside LFIND's parser and is never an argument, a shell
  # value, or a request (round 21).
  HELD(){ local w; w=$(python3 -c '
import json, re, sys
ns, pod = sys.argv[1], sys.argv[2]
ts, sep, needle = open(sys.argv[3], "rb").read().decode("utf-8").partition("\t")
needle = needle.rstrip("\n")
if not sep or not re.fullmatch(r"[0-9]{1,12}", ts): sys.exit(3)
if not re.fullmatch(r"\"(?:[^\"\\\x00-\x1f\x7f]|\\(?:[\"\\]|x[0-9a-f]{2}))*\"", needle): sys.exit(3)
open(sys.argv[4], "wb").write(("{namespace=%s,pod=%s}" % (json.dumps(ns), json.dumps(pod))).encode("utf-8"))
print("%d %d" % (int(ts) - 5, int(ts) + 5))' "$1" "$2" "$3" "$TMP/query") || return 1
          LFIND "$TMP/query" "${w% *}" "${w#* }" "$3"; }

  CTL_NS=traefik
  # the pod's name is the API's text: a private file, and only a pod-shaped name comes
  # back to become a kubectl argument
  CTLF=$(mktemp)
  kubectl -n $CTL_NS get pods -l app.kubernetes.io/name=traefik -o jsonpath='{.items[0].metadata.name}' > "$CTLF" 2>/dev/null
  CTL_POD=$(python3 -c 'import re, sys; v = open(sys.argv[1], errors="replace").read().strip(); print(v if re.fullmatch(r"[a-z0-9]([-a-z0-9.]*[a-z0-9])?", v) and len(v) <= 253 else "")' "$CTLF" 2>/dev/null); rm -f "$CTLF"
  CTL_STATE=UNANSWERABLE; CTL_WHY="there is no traefik pod to read (or its name is not a pod name)"
  if [ -n "$CTL_POD" ]; then
    KLAST $CTL_NS "$CTL_POD" 5m "$TMP/ctl.needle"
    if grep -qx UNREADABLE "$TMP/ctl.needle" 2>/dev/null; then
      CTL_WHY="traefik's logs could not be read from the kubelet (kubectl logs failed) — not a silent pod, a failed read"
    elif [ ! -s "$TMP/ctl.needle" ]; then
      CTL_WHY="traefik wrote no access line in 5m — the platform's own uptime probes have stopped crossing the ingress, so pipeline liveness cannot be established"
    else
      CTL_TS=$(cut -f1 "$TMP/ctl.needle")
      CTL_H=$(HELD $CTL_NS "$CTL_POD" "$TMP/ctl.needle")
      for _ in 1 2 3 4 5; do [ "$CTL_H" = "0" ] || break; sleep 6; CTL_H=$(HELD $CTL_NS "$CTL_POD" "$TMP/ctl.needle"); done
      # Only a positive integer is evidence of a held line: a helper that failed (no
      # output, a needle it refused, a query error) is UNANSWERABLE, never ALIVE.
      case "$CTL_H" in
        0)        CTL_STATE=BROKEN; CTL_WHY="traefik wrote at $(date -u -d "@$CTL_TS" +%H:%M:%SZ) and Loki does not hold that line";;
        ''|*[!0-9]*) CTL_WHY="the Loki query for traefik's own newest line did not answer (or the line could not be turned into a query)";;
        *)        CTL_STATE=ALIVE;  CTL_WHY="traefik's line from $(date -u -d "@$CTL_TS" +%H:%M:%SZ) is in Loki (its first 300 characters, as a literal substring)";;
      esac
    fi
  fi
  case "$CTL_STATE" in
    ALIVE) ok "the alloy->loki pipeline is alive right now — $CTL_WHY (control: $CTL_NS/$CTL_POD, whose cadence is this platform's uptime probes, not tenant traffic)";;
    *)     bad "the alloy->loki log pipeline is $CTL_STATE — $CTL_WHY; any idle namespace below is UNVERIFIED, not healthy";;
  esac

  NOW=$(date +%s)
  NNS=0
  # A population that could not be LISTED is not an empty population: the namespace
  # list and each namespace's pod list fail their own row when kubectl fails.
  if ! NSLIST=$(project_namespaces); then
    bad "the project namespaces could not be listed (kubectl get ns failed) — the per-namespace collection rows are UNCHECKED, not keyed off"
    NSLIST=; NNS=-1
  elif [ "$(ns_odd)" != 0 ]; then
    bad "$(ns_odd) project namespace(s) have a name not of a namespace shape (not repeated) — skipped, their collection is UNCHECKED"
  fi
  for ns in $NSLIST; do
    NNS=$((NNS+1))
    NPOD=0; NUNREAD=0; SPOD=; STS=
    # the pod list is cluster text: a private file, and only pod-shaped names come out of
    # it into the loop (a name becomes a kubectl argument); the rest are counted
    if ! kubectl -n "$ns" get pods -o jsonpath='{.items[*].metadata.name}' > "$TMP/pods.raw" 2>/dev/null; then
      bad "$ns: its pods could not be listed (kubectl get pods failed) — collection for this namespace is UNCHECKED, not silent"
      continue
    fi
    PODS=$(tr ' ' '\n' < "$TMP/pods.raw" | grep -Ex '[a-z0-9]([-a-z0-9.]{0,251}[a-z0-9])?' | tr '\n' ' ')
    PODODD=$(( $(tr ' ' '\n' < "$TMP/pods.raw" | grep -c .) - $(printf '%s' "$PODS" | wc -w) ))
    [ "$PODODD" -le 0 ] || bad "$ns: $PODODD pod(s) have a name not of a pod-name shape (not repeated) — skipped, their collection is UNCHECKED"
    for p in $PODS; do
      NPOD=$((NPOD+1))
      if ! KLAST "$ns" "$p" 6h "$TMP/pod.needle"; then
        # A pod with no log to read is not a failed read: one that has not started a
        # container yet (a scheduled job's pod, seconds old) or that ended and was
        # deleted between the list and the read. That turned this row red once on a
        # healthy box and not again (QA of v1.0.0, F14). The pod is asked for; gone or
        # still Pending, it is left out of the count. Anything else is read once more,
        # and only then counted as a read that failed.
        PHASE=$(kubectl -n "$ns" get pod "$p" -o jsonpath='{.status.phase}' 2>/dev/null) || PHASE=GONE
        case "$PHASE" in
          GONE|Pending|'') NPOD=$((NPOD-1)); continue;;
        esac
        sleep 2
        KLAST "$ns" "$p" 6h "$TMP/pod.needle" || { NUNREAD=$((NUNREAD+1)); continue; }
      fi
      [ -s "$TMP/pod.needle" ] || continue
      E=$(cut -f1 "$TMP/pod.needle")
      if [ -z "$STS" ] || [ "$E" -gt "$STS" ]; then SPOD=$p; STS=$E; cp "$TMP/pod.needle" "$TMP/newest.needle"; fi
    done
    if [ "$NUNREAD" -gt 0 ]; then
      bad "$ns: the logs of $NUNREAD of $NPOD pod(s) could not be read from the kubelet (kubectl logs failed) — a failed read is not a silent pod; collection for this namespace is UNCHECKED"
      continue
    fi
    if [ -z "$SPOD" ]; then
      case "$CTL_STATE" in
        ALIVE) ok "[SILENT] $ns wrote nothing to stdout in 6h ($NPOD pod(s), all silent per the kubelet) — there is no line to collect, and the pipeline is proven alive by the control";;
        *)     bad "[SILENT] $ns wrote nothing in 6h AND the pipeline could not be proven alive ($CTL_STATE) — its collection is UNVERIFIED, not healthy";;
      esac
      continue
    fi
    AGE=$((NOW-STS)); [ "$AGE" -lt 0 ] && AGE=0
    H=$(HELD "$ns" "$SPOD" "$TMP/newest.needle")
    # Poll only when the line is fresh: Alloy's push lag is seconds, and waiting on a line
    # that is hours old is 30s spent to learn nothing.
    if [ "$AGE" -le 900 ]; then
      for _ in 1 2 3 4 5; do [ "$H" = "0" ] || break; sleep 6; H=$(HELD "$ns" "$SPOD" "$TMP/newest.needle"); done
    fi
    case "$H" in
      0)        bad "$ns: the kubelet has $SPOD writing a line ${AGE}s ago that Loki does NOT hold (the line itself is not printed: a log line may hold anything) — alloy->loki collection for this namespace is BROKEN";;
      ''|*[!0-9]*) bad "$ns: the Loki query for $SPOD's newest stdout line did not answer (or the line could not be turned into a query) — collection is UNCHECKED, not proven";;
      *)        if [ "$AGE" -le 900 ]; then
                  ok "[ACTIVE] $ns: $SPOD's newest stdout line (${AGE}s old) is in Loki (its first 300 characters, as a literal substring) — collection is live"
                else
                  case "$CTL_STATE" in
                    ALIVE) ok "[IDLE] $ns has not logged for ${AGE}s, but that line (its first 300 characters) IS in Loki and the pipeline is proven alive right now by the control";;
                    *)     bad "[IDLE] $ns has not logged for ${AGE}s and the pipeline could not be proven alive ($CTL_STATE) — its collection is UNVERIFIED";;
                  esac
                fi;;
    esac
  done
  [ "$NNS" -eq 0 ] && ok "the catalog manages no namespaces yet — the per-namespace collection rows key off; the control above still proves the pipeline"
  # Retention running == retention in git; a drifted valve is a silent keep-forever or a
  # silent data loss. The FLOOR ($GITRET >= 7d) was asserted from git in the static
  # section above; this is the half that needs a cluster.
  kubectl -n loki get cm loki -o jsonpath='{.data.config\.yaml}' > "$TMP/loki-config.yaml" 2>/dev/null
  LIVERET=$(retention_of "$TMP/loki-config.yaml" limits_config.retention_period)
  [ -n "$GITRET" ] && [ "$GITRET" = "$LIVERET" ] && ok "retention in cluster ($LIVERET) == retention in git" \
    || bad "retention drift: git=${GITRET:-unreadable} cluster=${LIVERET:-unreadable}"
  # The ingestion valve must never be silently closed. Two failure modes, kept distinct:
  # the whole Loki metric namespace unreadable (the valve check would then be blind — so
  # anchor on loki_build_info, which a running Loki always exports), and the valve
  # actively dropping lines (legitimately absent when nothing is dropped, hence
  # `or vector(0)`, and compared as a FLOAT so a sustained 0.9/s is not truncated to 0).
  ANCH=$(promval 'count(loki_build_info)')
  case "$ANCH" in
    QUERYERR|ABSENT) bad "loki_build_info is unreadable ($ANCH) — Loki's metric namespace is gone or unscraped; the ingestion-valve check is blind";;
    *) ok "Loki's metric namespace is scraped ($ANCH build-info series) — the valve is observable";;
  esac
  DISC=$(promval 'sum(rate(loki_discarded_samples_total{reason=~"rate_limited|per_stream_rate_limit|stream_limit"}[10m])) or vector(0)')
  case "$DISC" in
    QUERYERR|ABSENT) bad "could not answer the Loki discard-rate query ($DISC) — the valve check did not run";;
    *) python3 -c "import sys;sys.exit(0 if float(sys.argv[1]) < 0.001 else 1)" "${DISC:-0}" 2>/dev/null \
         && ok "the ingestion valve is open (rate-limit discards ~0/s)" \
         || bad "Loki is DISCARDING log lines at ${DISC}/s (rate limits) — raise the valve deliberately";;
  esac
  # BLAST RADIUS. The Loki chart's sidecar.rules default pays for itself with a
  # ClusterRole granting get/watch/list on ALL configmaps and secrets cluster-wide, no
  # resourceNames — which hands the loki ServiceAccount the age private key.
  # The template kills it
  # at the source (rbac.namespaced + sidecar.rules.enabled=false render no RBAC object for
  # loki at all). Asked AS THE SA rather than read off the ClusterRole, so a second grant
  # arriving by any other route is caught too. Fail closed: only a clean "no" passes.
  if kubectl -n loki get sa loki >/dev/null 2>&1; then
    ok "the loki SA exists — the probes below are asking about a real principal"
  else
    bad "ServiceAccount loki/loki does not exist — every can-i below would answer 'no' for a principal that isn't there; the blast-radius check is meaningless until this is resolved"
  fi
  # Each target is a core platform namespace plus, where there is one, the specific
  # credential that makes it worth probing — so a moved anchor fails loudly instead of
  # silently guarding an empty namespace.
  for target in sops:sops-age monitoring:? orbit:?; do
    tns=${target%%:*}; tsec=${target#*:}
    if ! kubectl get ns "$tns" >/dev/null 2>&1; then
      bad "core namespace $tns is missing — the blast-radius probe for it did not run, and the name in this check may be stale"
      continue
    fi
    if [ "$tsec" != "?" ] && ! kubectl -n "$tns" get secret "$tsec" >/dev/null 2>&1; then
      bad "$tns/$tsec no longer exists — this probe's anchor moved; re-point the blast-radius check at wherever that credential now lives"
    fi
    # get, list AND watch, each on its own: a Role that grants `list` hands over every
    # Secret's data in one call while `get` still answers "no"
    for verb in get list watch; do
      CANI=$(cani --as=system:serviceaccount:loki:loki "$verb" secrets -n "$tns")
      case "$CANI" in
        no)  ok "loki's SA cannot $verb secrets in $tns";;
        yes) bad "loki's SA CAN $verb secrets in $tns — the chart's cluster-wide configmaps/secrets grant is live; a compromised log store reads $tns's secrets (fix: rbac.namespaced + sidecar.rules.enabled=false in clusters/platform/apps/loki.yaml)";;
        *)   bad "could not determine whether loki's SA can $verb secrets in $tns (the answer was not yes/no) — the blast-radius check did not run";;
      esac
    done
  done
  for verb in get list watch; do
    CANIOWN=$(cani --as=system:serviceaccount:loki:loki "$verb" secrets -n loki)
    case "$CANIOWN" in
      no)  ok "loki's SA cannot $verb secrets even in its own namespace (no Role or ClusterRole is rendered for it)";;
      yes) bad "loki's SA can $verb secrets in its own namespace — a Role or ClusterRole is being rendered for it again; Loki reads nothing from the API server and should hold no grant";;
      *)   bad "could not determine whether loki's SA can $verb secrets in loki (the answer was not yes/no) — the blast-radius check did not run";;
    esac
  done
fi

echo "== the panel renderer =="
# The image renderer is what makes a dashboard change LOOKABLE AT before it is
# called correct.
if [ "$LIVEOFF" = 1 ]; then
  keyoff_live "the renderer rows key off"
elif [ "$FEATURE_DASHBOARDS" != 1 ]; then
  ok "the dashboards feature is off — the renderer rows key off"
else
  # The renderer's shared token must be pinned in the values, not minted by the chart.
  # Left to itself the chart mints it with randAlphaNum on every render — `lookup` of
  # the previous Secret is empty whenever Helm runs without cluster access, which is how
  # Argo renders — so a re-render deals Grafana and the renderer different values without
  # restarting either, and the render path 401s in silence. Two checks, in the order the
  # fix has to hold: pinned in the values, wired into BOTH consumers.
  # $PIN and $KPS were read from git in the static section above, before any live branch.
  # what the cluster answers is a Secret NAME only once it has that shape: the answer
  # lands in a private file and the shell sees the name or a placeholder, never the text
  secret_name_in(){ python3 -c 'import re, sys; v = open(sys.argv[1], errors="replace").read().strip(); print(v if re.fullmatch(r"[a-z0-9]([-a-z0-9.]*[a-z0-9])?", v) and len(v) <= 253 else "(not a Secret name)")' "$1" 2>/dev/null; rm -f "$1"; }
  RSF=$(mktemp); GSF=$(mktemp)
  kubectl -n $MON get deploy kube-prometheus-stack-grafana-image-renderer \
       -o jsonpath='{.spec.template.spec.containers[0].env[?(@.name=="AUTH_TOKEN")].valueFrom.secretKeyRef.name}' > "$RSF" 2>/dev/null
  kubectl -n $MON get deploy kube-prometheus-stack-grafana \
       -o jsonpath='{.spec.template.spec.containers[?(@.name=="grafana")].env[?(@.name=="GF_RENDERING_RENDERER_TOKEN")].valueFrom.secretKeyRef.name}' > "$GSF" 2>/dev/null
  RS=$(secret_name_in "$RSF"); GS=$(secret_name_in "$GSF")
  [ -n "$PIN" ] && [ "$RS" = "$PIN" ] && [ "$GS" = "$PIN" ] \
    && ok "both sides of the render path read the pinned Secret ($PIN)" \
    || bad "the renderer reads '$RS' and grafana reads '$GS', not the pinned '$PIN' — the values change has not landed"
fi

echo "== traces: the root span, the span metrics and the four correlation links =="
# Tempo is bucket-gated. The correlation links are jsonData on the datasources
# template/provisioning/clusters/platform/apps/kube-prometheus-stack.yaml provisions, and
# every failure mode in this domain is a link that silently does not render.
if [ "$LIVEOFF" = 1 ]; then
  keyoff_live "the trace rows key off"
elif ! kubectl -n orbit get configmap orbit-platform-inventory-tempo >/dev/null 2>&1; then
  if [ "$FEATURE_TRACES" = 1 ] && [ "$ICLUSTER" = 1 ]; then
    bad "the traces feature is on, so tempo.yaml is rendered — but no app tempo is applied on a reachable cluster: DECLARED AND NOT LIVE. That is red, not a keyed-off pass"
  else
    keyoff_absent "the traces feature is off / no tempo app — the trace rows key off"
  fi
elif [ "$FEATURE_TRACES" != 1 ]; then
  ok "the traces feature is off — the trace rows key off; the features row is what goes red for the tempo app that is still applied"
elif [ "$FEATURE_DASHBOARDS" = 1 ] && [ "$HAVEPW" = 0 ]; then
  bad "no Grafana admin password on this workstation — the trace rows are asked through Grafana's proxy and CANNOT run"
else
  # A real root span from a request WE drive, tied to it by a unique path marker and
  # bounded to a fresh window — not just any traefik trace sitting in Tempo's head block,
  # which the platform's own 60s blackbox probes keep populated regardless. The request is
  # driven at Grafana's host under a unique path, so this needs no tenant to exist.
  # The path matches nothing, so Grafana 404s — but the request still crosses Traefik's
  # router, which is the span this row is after.
  # Trace-ingestion lag here runs 12-130s, so a single sleep flaps; a bounded poll asserts
  # the trace from the request we just drove appears in a FRESH window.
  MK="orbitverify-$$-$(date +%s)"
  # with the dashboards feature off there is no Grafana host: the control surface's own
  # host is routed by the same Traefik, and an unknown path there is a 404 just the same
  if [ "$FEATURE_DASHBOARDS" = 1 ]; then MKHOST="grafana.$ORBIT_DOMAIN"; else MKHOST="orbit.$ORBIT_DOMAIN"; fi
  curl -s -o /dev/null --max-time 10 "https://$MKHOST/$MK" || true
  TSTART=$(( $(date +%s) - 180 ))
  TRACES=0
  for _ in $(seq 1 11); do
    sleep 12
    if [ "$FEATURE_DASHBOARDS" = 1 ]; then
      grafpinned || true
      TRACES=$(grafpinned && grafauth | curl -q -s --max-time 20 -K - "${GRAFPIN[@]}" --get \
        --data-urlencode "q={resource.service.name=\"traefik\" && span.url.path=~\".*$MK.*\"}" \
        --data-urlencode "start=$TSTART" --data-urlencode "end=$(date +%s)" --data-urlencode "limit=5" \
        "$GRAF/api/datasources/proxy/uid/tempo/api/search" \
        | python3 -c 'import sys,json;print(len(json.load(sys.stdin).get("traces",[])))' 2>/dev/null)
    else
      # no Grafana: the same search, asked of Tempo through the API server's service proxy
      TRACES=$(kubectl get --raw "$(python3 -c 'import sys,urllib.parse
print("/api/v1/namespaces/tempo/services/tempo:3200/proxy/api/search?"+urllib.parse.urlencode({"q":"{resource.service.name=\"traefik\" && span.url.path=~\".*%s.*\"}" % sys.argv[1],"start":sys.argv[2],"end":sys.argv[3],"limit":"5"}))' "$MK" "$TSTART" "$(date +%s)")" 2>/dev/null \
        | python3 -c 'import sys,json;print(len(json.load(sys.stdin).get("traces",[])))' 2>/dev/null)
    fi
    [ "${TRACES:-0}" -ge 1 ] 2>/dev/null && break
  done
  [ "${TRACES:-0}" -ge 1 ] 2>/dev/null \
    && ok "Tempo holds the traefik root span for OUR marked request ($MK), inside a fresh window — tracing of real requests is live, not a stale head-block artefact" \
    || bad "no traefik root span matching our marked request within ~2m — tracing of real requests is broken"
  # The span-metrics generator must land in Prometheus over the remote-write receiver,
  # with BOUNDED span names: one URL-templated name is the cardinality melt the locks
  # exist to prevent, so the SERIES COUNT is asserted, not just existence.
  SM=$(promval 'count(traces_spanmetrics_calls_total{service="traefik"})')
  [ "${SM%%.*}" -ge 1 ] 2>/dev/null \
    && ok "span metrics from the generator land in Prometheus ($SM traefik series)" \
    || bad "no traefik span-metrics series (got '$SM') — the generator or the remote-write receiver is dead"
  SMTOT=$(promval 'count(traces_spanmetrics_calls_total)')
  [ "${SMTOT%%.*}" -lt 500 ] 2>/dev/null \
    && ok "span-metric cardinality is bounded ($SMTOT series total)" \
    || bad "span-metric series is ${SMTOT:-unreadable} — a high-cardinality span name broke the locks"
  if [ "$FEATURE_DASHBOARDS" != 1 ]; then
    ok "the dashboards feature is off — the four correlation links are Grafana datasource settings and key off"
  else
  grafapi /api/datasources > "$TMP/ds.json"
  python3 - "$TMP/ds.json" <<'PY' > "$TMP/links.txt" 2>/dev/null
import json,sys
ds={d["uid"]:d.get("jsonData",{}) for d in json.load(open(sys.argv[1]))}
ex=ds.get("prometheus",{}).get("exemplarTraceIdDestinations")
print("trace_to_logs", (ds.get("tempo",{}).get("tracesToLogsV2") or {}).get("datasourceUid")=="loki")
print("logs_to_trace", any(f.get("datasourceUid")=="tempo" for f in ds.get("loki",{}).get("derivedFields") or []))
print("trace_to_metrics", (ds.get("tempo",{}).get("tracesToMetrics") or {}).get("datasourceUid")=="prometheus")
print("metrics_to_trace", (ex[0] if isinstance(ex,list) and ex else (ex or {})).get("datasourceUid")=="tempo")
PY
  for link in trace_to_logs logs_to_trace trace_to_metrics metrics_to_trace; do
    grep -q "^$link True" "$TMP/links.txt" \
      && ok "correlation link $link is provisioned with a fixed uid" \
      || bad "correlation link $link is missing or points at the wrong datasource"
  done
  fi
fi


echo "== orbit-intake: the agent plane's public webhook door =="
# template/provisioning/clusters/platform/config/orbit-intake/, rendered only while
# instance/config.yaml says `intakeIngress: wired` — the flag IS the org attesting that
# both halves of the door exist, and the generator renders a `github` trigger's ROUTE and
# never the DOOR. The property that matters is that the door publishes exactly one path:
# the intake's GET /healthz and GET /metrics are its only unauthenticated routes, and a
# `path: /` here would put both on the internet with nothing else noticing.
IDIR=clusters/platform/config/orbit-intake
IHOST="intake.$ORBIT_DOMAIN"
if [ "$GATE_INTAKE" != 1 ] && [ ! -f "$IDIR/ingress.yaml" ]; then
  ok "instance/config.yaml does not declare intakeIngress: wired and no door is rendered — the intake-door rows key off (fail-closed: no public webhook Ingress exists at all)"
elif [ ! -f "$IDIR/ingress.yaml" ] || [ ! -f "$IDIR/netpol.yaml" ]; then
  bad "instance/config.yaml declares intakeIngress: wired but the door files are missing from $IDIR — the attestation and the artifact disagree"
else
  DOUT=$(python3 - "$IDIR/ingress.yaml" "$IHOST" <<'PY'
import re, sys, yaml
import shown as display   # the shared display rule (scripts/verify/shown.py)
try:
    docs = [d for d in yaml.safe_load_all(open(sys.argv[1])) if isinstance(d, dict)]
except Exception as exc:     # the class only: a parser's message quotes the input
    print("FAIL %s cannot be read as YAML (%s)" % (sys.argv[1], type(exc).__name__)); sys.exit(1)
host = sys.argv[2]
# a diagnostic repeats a value from the file only once it has the shape of what the
# field holds: an object name, or a host name; anything else is described, not shown
def oname(v): return display.shown(v, "object-name", "value")
def hname(v): return display.shown(v, "hostname", "value")
ing = [d for d in docs if d.get("kind") == "Ingress"]
problems = []
if len(ing) != 1:
    print("FAIL expected exactly one Ingress, found %d" % len(ing)); sys.exit(1)
spec = ing[0].get("spec") or {}
md = ing[0].get("metadata") or {}
if md.get("namespace") != "orbit-agents-prod":
    problems.append("namespace is %s, not orbit-agents-prod" % oname(md.get("namespace")))
if spec.get("ingressClassName") != "traefik":
    problems.append("ingressClassName is %s, not traefik" % oname(spec.get("ingressClassName")))
if spec.get("defaultBackend"):
    # A defaultBackend is a CATCH-ALL that answers every path this Ingress does not
    # otherwise match, with no path rule at all — it would publish whatever Service it
    # names (an agent, or the intake past its /webhook/github scope) invisibly to the
    # path-scope row below.
    db = (((spec.get("defaultBackend") or {}).get("service")) or {}).get("name")
    problems.append("carries a spec.defaultBackend -> Service %s — a catch-all backend "
                    "publishes with no path rule" % oname(db))
hosts = [r.get("host") for r in (spec.get("rules") or [])]
if hosts != [host]:
    problems.append("hosts are [%s], expected exactly [%s]" % (", ".join(hname(h) for h in hosts), host))
for r in (spec.get("rules") or []):
    for p in ((r.get("http") or {}).get("paths") or []):
        svc = (((p.get("backend") or {}).get("service")) or {}).get("name")
        if svc != "orbit-agents-intake":
            problems.append("a path rule backs Service %s, not orbit-agents-intake — an AGENT "
                            "behind this Ingress would be an unauthenticated agent card on "
                            "the internet" % oname(svc))
        # the backend PORT is the intake Service's `http` port (charts/service renders http:80):
        # a name the Service does not carry is a 503 from traefik, and the door is dead (round 20)
        port = (((p.get("backend") or {}).get("service")) or {}).get("port")
        if port not in ({"name": "http"}, {"number": 80}):
            problems.append("a path rule backs a Service port that is not the intake's http port "
                            "({name: http} or {number: 80}; the value is not repeated) — traefik answers 503 for it")
paths = [(p.get("path"), p.get("pathType"))
         for r in (spec.get("rules") or [])
         for p in ((r.get("http") or {}).get("paths") or [])]
if paths != [("/webhook/github", "Prefix")]:
    # the rules are counted, not repeated: a path can carry a token
    problems.append("the door publishes %d path rule(s) that are not exactly one Prefix "
                    "/webhook/github; anything wider puts the intake's unauthenticated "
                    "GET /healthz and GET /metrics on the internet" % (len(paths),))
if any((d.get("spec") or {}).get("tls") for d in ing):
    # A tls block would demand a copy of the wildcard key inside the agent plane's
    # namespace (an Ingress may only name a Secret in its own namespace), which is the
    # last namespace in the cluster that should hold one. Traefik's default TLSStore
    # serves this host by SNI.
    problems.append("the door carries a tls: block — the design is SNI off Traefik's "
                    "default wildcard, with no ACME order and no wildcard key in the plane")
if problems:
    print("FAIL " + "; ".join(problems)); sys.exit(1)
print("OK one Ingress on %s, class traefik, backend orbit-agents-intake, one Prefix path "
      "/webhook/github, no tls block" % host)
PY
)
  [ $? -eq 0 ] && ok "the intake door is one path-scoped Ingress [$DOUT] — /healthz and /metrics stay unpublished, and there is no catch-all backend" \
               || bad "intake door: $DOUT"
  # The door router's middleware chain. The OOM guard must STAY ATTACHED, and nothing
  # reachable from it may rewrite the path (a stripPrefix could peel /webhook/github off
  # and hand the intake /metrics). And the inFlightReq cap must stay HOST-keyed: with a
  # per-IP or per-header sourceCriterion the amount becomes PER SOURCE, and a distributed
  # flood holds N in flight from each of many sources at once, making the single-replica
  # intake OOM-able again.
  MOUT=$(python3 - "$IDIR/ingress.yaml" <<'PY'
import re, sys, yaml
import shown as display   # the shared display rule (scripts/verify/shown.py)
try:
    docs = [d for d in yaml.safe_load_all(open(sys.argv[1])) if isinstance(d, dict)]
except Exception as exc:     # the class only: a parser's message quotes the input
    print("FAIL %s cannot be read as YAML (%s)" % (sys.argv[1], type(exc).__name__)); sys.exit(1)
ing = [d for d in docs if d.get("kind") == "Ingress"]
mws = {}
for d in docs:
    if d.get("kind") == "Middleware":
        md = d.get("metadata") or {}
        if md.get("namespace") == "orbit-agents-prod" and md.get("name"):
            mws[md["name"]] = d.get("spec") or {}
ANN = "traefik.ingress.kubernetes.io/router.middlewares"
ALLOWED = {"inFlightReq", "rateLimit", "ipAllowList"}
REWRITERS = {"replacePath", "replacePathRegex", "stripPrefix", "stripPrefixRegex", "addPrefix"}
NS_PREFIX, SUFFIX = "orbit-agents-prod-", "@kubernetescrd"
problems = []
if len(ing) != 1:
    print("FAIL expected exactly one Ingress, found %d" % len(ing)); sys.exit(1)
ann = ((ing[0].get("metadata") or {}).get("annotations") or {}).get(ANN)
if not ann:
    print("FAIL the door Ingress carries no %s annotation — the OOM guard is not attached; "
          "an unauthenticated flood can OOM the single-replica intake" % ANN); sys.exit(1)
roots = []
for ri, ref in enumerate([r.strip() for r in ann.split(",") if r.strip()], 1):
    # a ref is annotation text: named in a finding by POSITION, and its middleware name
    # carried further only with an object name's shape
    if not (ref.startswith(NS_PREFIX) and ref.endswith(SUFFIX)):
        problems.append("annotation ref #%d is not an orbit-agents-prod-<name>@kubernetescrd "
                        "reference (a middleware in another namespace could be anything; "
                        "the ref is not repeated)" % ri)
        continue
    root = ref[len(NS_PREFIX):-len(SUFFIX)]
    if not re.fullmatch(r"[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?", root):
        problems.append("annotation ref #%d names a middleware whose name is not an object name "
                        "(not repeated)" % ri)
        continue
    roots.append(root)
leaf_types, seen, queue = set(), set(), list(roots)
while queue:
    name = queue.pop()
    if name in seen: continue
    seen.add(name)
    if name not in mws:
        problems.append("annotation/chain references middleware %r which is not defined "
                        "here — it cannot be verified not to rewrite the path" % name)
        continue
    keys = list((mws[name] or {}).keys())
    if len(keys) != 1:
        # the keys are file text: only key-shaped ones are repeated, the rest counted
        kshown = [k for k in keys if display.fits(k, "word")]
        problems.append("middleware %r sets %d types (%s%s) — Traefik refuses a multi-type "
                        "middleware and disables the router (a 404)"
                        % (name, len(keys), ",".join(kshown) or "none of a key shape",
                           " +%d not of a key shape" % (len(keys) - len(kshown)) if len(keys) != len(kshown) else ""))
        continue
    t = keys[0]
    if t in REWRITERS:
        problems.append("middleware %r is a PATH-REWRITER (%s) attached to the door — it can "
                        "peel /webhook/github and put /metrics on the internet" % (name, t))
    elif t == "chain":
        for ci, m in enumerate((mws[name]["chain"] or {}).get("middlewares") or []):
            # a nested reference is file text too: shaped before it is queued or named
            ref = m.get("name") if isinstance(m, dict) else None
            rns = m.get("namespace") if isinstance(m, dict) else None
            if rns is not None and rns != "orbit-agents-prod":
                # the deployed traefik runs allowCrossNamespace: false — a reference into
                # another namespace is refused and the router is disabled (a 404); the
                # local middleware of the same name is NOT what the chain resolves to
                problems.append("middleware %r chain entry #%d references a middleware in another namespace "
                                "(not repeated) — traefik refuses cross-namespace references here" % (name, ci))
            elif isinstance(ref, str) and re.fullmatch(r"[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?", ref):
                queue.append(ref)
            else:
                problems.append("middleware %r chain entry #%d names a middleware whose name is not "
                                "an object name (not repeated) — it cannot be verified" % (name, ci))
    elif t in ALLOWED:
        leaf_types.add(t)
    else:
        problems.append("middleware %r is of type %s, not one of inFlightReq/rateLimit/"
                        "ipAllowList — only those may sit in front of the door"
                        % (name, t if display.fits(t, "word") else "(a type key not of a key shape; not repeated)"))
for need in ("inFlightReq", "rateLimit"):
    if need not in leaf_types:
        problems.append("the load-bearing %s is not in the chain — it is the OOM cap that "
                        "holds even if ipAllowList is rotated out" % need)
# A limit that is PRESENT but disabled or unbounded guards nothing: Traefik reads
# rateLimit.average 0 as "no rate limiting", and an inFlightReq amount in the millions
# is the OOM the cap exists to prevent. Only bounded, active values pass.
def _int(v):
    # the CRD types amount/average/burst as integers: a quoted "16" is refused by the API
    # server, and the router is disabled — not a number here either (round 24)
    if isinstance(v, bool) or not isinstance(v, int): return None
    return v
_DUR = {"ms": 0.001, "s": 1.0, "m": 60.0}
def _seconds(v):
    """A Traefik duration string (100ms, 1s, 1m); a bare number is not accepted — Traefik reads
    an unsuffixed value in its own unit, and a guess either way is a rate nobody set."""
    m = re.fullmatch(r"([0-9]+(?:\.[0-9]+)?)(ms|s|m)", v.strip()) if isinstance(v, str) else None
    return float(m.group(1)) * _DUR[m.group(2)] if m else None
for name in seen:
    spec = mws.get(name) or {}
    if "rateLimit" in spec and not isinstance(spec.get("rateLimit"), dict):
        # a scalar rateLimit is refused by Traefik: the router is disabled (a 404) — round 20
        problems.append("middleware %r rateLimit is not a map (its value is not repeated) — Traefik refuses it and disables the router" % name)
    if isinstance(spec.get("rateLimit"), dict):
        rl = spec["rateLimit"]; avg = _int(rl.get("average")); burst = _int(rl.get("burst", avg))
        if avg is None or not 1 <= avg <= 10000:
            problems.append("middleware %r rateLimit.average is not in 1..10000 (0 DISABLES rate limiting in Traefik)" % name)
        if burst is None or not 1 <= burst <= 10000:
            problems.append("middleware %r rateLimit.burst is not in 1..10000" % name)
        # the EFFECTIVE rate is average per period: 50 per 1ns is no limit at all (round 20)
        secs = _seconds(rl.get("period", "1s"))
        if secs is None or not 0.1 <= secs <= 60:
            problems.append("middleware %r rateLimit.period is not a duration between 100ms and 1m (its value is not repeated)" % name)
        elif avg is not None and not 1 <= avg / secs <= 10000:
            problems.append("middleware %r rateLimit is %d per %s — an effective rate outside 1..10000 requests per second" % (name, avg, rl.get("period", "1s")))
    if "inFlightReq" in spec and not isinstance(spec.get("inFlightReq"), dict):
        # the CRD requires an object: a scalar is refused and the router disabled (round 23)
        problems.append("middleware %r inFlightReq is not a map (its value is not repeated) — Traefik refuses it and disables the router" % name)
    if isinstance(spec.get("inFlightReq"), dict):
        amt = _int(spec["inFlightReq"].get("amount"))
        if amt is None or not 1 <= amt <= 1024:
            problems.append("middleware %r inFlightReq.amount is not in 1..1024 — an unbounded cap is no OOM guard" % name)
infl = [(n, s["inFlightReq"]) for n, s in mws.items() if isinstance(s.get("inFlightReq"), dict)]
# the cap that holds is the one the door's chain REACHES: an unattached inFlightReq map
# elsewhere in the file guards nothing (round 23)
if not any(n in seen for n, _ in infl):
    problems.append("no inFlightReq middleware (as a map) in the door's chain — the global OOM cap is gone")
for name, spec in infl:
    # every middleware in the file is read here, not only the chain's (shaped) names: the
    # name a row repeats is shown only in an object name's shape (round 20)
    name = display.shown(name, "object-name", "middleware")
    sc = spec.get("sourceCriterion")
    if sc is None:
        continue    # Traefik's default sourceCriterion is requestHost -> one global bucket.
    if not isinstance(sc, dict):
        problems.append("%s sets sourceCriterion to something that is not a map (its value is not repeated) — it must be absent or requestHost only" % name)
        continue
    if "requestHost" in sc and not isinstance(sc["requestHost"], bool):
        problems.append("%s sourceCriterion.requestHost is not a boolean (its value is not repeated) — the CRD refuses it and the router is disabled" % name)
    for k in ("ipStrategy", "requestHeaderName"):
        if k in sc:
            problems.append("%s sets sourceCriterion.%s — the amount cap becomes PER-SOURCE "
                            "instead of one global bucket, and a distributed flood can OOM the "
                            "intake again" % (name, k))
    extra = set(sc) - {"requestHost"}
    if not ({"ipStrategy", "requestHeaderName"} & set(sc)) and extra:
        problems.append("%s sourceCriterion has %d unexpected key(s) (not repeated) — only requestHost keeps "
                        "the cap a single global bucket" % (name, len(extra)))
if problems:
    print("FAIL " + "; ".join(problems)); sys.exit(1)
print("OK roots=%s leaves=%s, no path-rewriter, %d host-keyed inFlightReq cap(s)"
      % (",".join(sorted(roots)), ",".join(sorted(leaf_types)), len(infl)))
PY
)
  [ $? -eq 0 ] && ok "the door router carries the OOM guard, nothing in its chain rewrites the path, and the in-flight cap stays HOST-keyed (one global bucket) [$MOUT]" \
               || bad "intake door middleware: $MOUT"
  # The traefik -> intake grant, parsed rather than dialled, for the same reason as the
  # Alertmanager one: a live probe proves REFUSAL but is blind to WIDENING. An OR-split —
  # namespaceSelector in one from-element, podSelector in another — is a whole-namespace
  # grant wearing a pod-narrowing costume, and a naive "a podSelector exists somewhere"
  # test waves it through.
  # every policy the renderer applies into orbit-agents-prod, not the one file: a second
  # file widening the door counts just as well
  if GERR=$(gather_netpols orbit-agents-prod "$TMP/intake-netpols.yaml"); then :; else
    bad "the intake policy set could not be gathered: $GERR — the door's grant is UNAUDITED"; : > "$TMP/intake-netpols.yaml"
  fi
  WIOUT=$(python3 - "$TMP/intake-netpols.yaml" <<'PY'
import re, sys, yaml
import shown as display   # the shared display rule (scripts/verify/shown.py)
try:
    docs = [d for d in yaml.safe_load_all(open(sys.argv[1])) if isinstance(d, dict)]
except Exception as exc:     # the class only: a parser's message quotes the input
    print("FAIL %s cannot be read as YAML (%s)" % (sys.argv[1], type(exc).__name__)); sys.exit(1)
def pins(sel, key, value, allowed=None):
    """Does this selector pin `key` to exactly `value`? matchLabels, or an `In` expression
    with that one value. An Exists, a NotIn, a wider In or another key narrows to nothing
    this row can name — such a selector is NOT narrowed. `allowed`: the only label keys the
    intended pod is KNOWN to carry; a constraint on any other key (round 16: matchLabels
    x=yes plus DoesNotExist x selects nothing, and so does a label the pod lacks) cannot
    be proven satisfied, so the selector is not a pin."""
    if not isinstance(sel, dict):
        return False
    ml, mx = sel.get("matchLabels") or {}, sel.get("matchExpressions") or []
    if not isinstance(ml, dict) or not isinstance(mx, list) or not all(isinstance(e, dict) for e in mx):
        return False
    if allowed is not None and (set(map(str, ml)) - set(allowed) or {str(e.get("key")) for e in mx} - set(allowed)):
        return False
    exprs = [e for e in mx if e.get("key") == key]
    # selector terms are ANDed: a term on the same key that excludes the value (NotIn with
    # it, In without it, DoesNotExist) makes the selector select NOTHING — not a pin
    for e in exprs:
        op, vals = e.get("operator"), [str(x) for x in (e.get("values") or [])]
        if op == "NotIn" and value in vals: return False
        if op == "In" and value not in vals: return False
        if op == "DoesNotExist": return False
    if key in ml:
        # a matchLabels value on the key is decisive: another value ANDed with an In
        # [value] selects nothing (round 17)
        return str(ml[key]) == value
    return any(e.get("operator") == "In" and [str(x) for x in (e.get("values") or [])] == [value] for e in exprs)
INTAKE_KEYS = ("app.kubernetes.io/name", "app.kubernetes.io/instance")
def narrowed(sel):
    # the target: the intake pod, by both of the labels the chart renders on it, and nothing else
    return pins(sel, "app.kubernetes.io/name", "intake", INTAKE_KEYS) and pins(sel, "app.kubernetes.io/instance", "orbit-agents", INTAKE_KEYS)
def peer_narrowed(peer):
    return pins(peer.get("namespaceSelector"), "kubernetes.io/metadata.name", "traefik", ("kubernetes.io/metadata.name",)) \
        and pins(peer.get("podSelector"), "app.kubernetes.io/name", "traefik", ("app.kubernetes.io/name",))
def pinned_namespace(sel):
    """The one namespace name this selector pins kubernetes.io/metadata.name to, or None."""
    if not isinstance(sel, dict): return None
    cands = {str((sel.get("matchLabels") or {}).get("kubernetes.io/metadata.name", ""))} - {""}
    for e in sel.get("matchExpressions") or []:
        if isinstance(e, dict) and e.get("key") == "kubernetes.io/metadata.name" and e.get("operator") == "In":
            cands |= {str(x) for x in (e.get("values") or [])}
    return next((c for c in cands if pins(sel, "kubernetes.io/metadata.name", c)), None)
def peer_could_admit_traefik(peer):
    """Could this from-element admit a pod of the traefik namespace? A peer of no readable
    form or an IP range could; a podSelector alone reaches the intake's OWN namespace and
    cannot; a namespaceSelector pinned to another namespace cannot; anything else (an
    empty or unpinned namespaceSelector) could. Round 15: the audit reads every policy
    the renderer applies into the namespace, and the plane's own-pods default-deny and
    the monitoring grant are not traefik grants — only a rule that could admit the
    ingress controller is held to the door's contract."""
    if not isinstance(peer, dict): return True
    ns, pod = peer.get("namespaceSelector"), peer.get("podSelector")
    if peer.get("ipBlock") is not None: return True
    if ns is None: return pod is None
    pinned = pinned_namespace(ns)
    return pinned is None or pinned == "traefik"
pols = [d for d in docs if d.get("kind") == "NetworkPolicy"
        and (d.get("metadata") or {}).get("namespace") == "orbit-agents-prod"]
if not pols:
    print("FAIL no NetworkPolicy for orbit-agents-prod in this file"); sys.exit(1)
offenders, peers = [], 0
effective = 0
for di, pol in enumerate(pols):
    # a policy's name is file text: shown with an object name's shape, else by position
    name = (pol.get("metadata") or {}).get("name")
    name = name if display.fits(name, "object-name") else "(document #%d, a name not of an object-name shape, or of a credential's shape)" % di
    spec = pol.get("spec") or {}
    types = spec.get("policyTypes")
    applies_ingress = types is None or (isinstance(types, list) and "Ingress" in types)
    rules = spec.get("ingress") or []
    if not isinstance(rules, list) or not all(isinstance(r, dict) for r in rules):
        offenders.append("%s has an ingress list not of a rule list's shape" % name); continue
    # the rules that could admit the ingress controller: no from (every peer), or a
    # from-element that could reach from the traefik namespace
    relevant = [(ri, rule) for ri, rule in enumerate(rules)
                if not rule.get("from") or any(peer_could_admit_traefik(p) for p in (rule.get("from") or []))]
    if not relevant:
        continue          # grants traefik nothing: not this door's business
    if not applies_ingress:
        offenders.append("%s does not apply to Ingress (policyTypes without Ingress) — it grants nothing on the way in" % name)
    if not narrowed(spec.get("podSelector")):
        offenders.append("%s does not pin its target to the intake pod (app.kubernetes.io/name=intake "
                         "AND app.kubernetes.io/instance=orbit-agents, by matchLabels or a one-value In) "
                         "— an agent would be reachable from the ingress controller" % name)
    for ri, rule in relevant:
        ports = rule.get("ports")
        def scoped(p):
            # a missing protocol is TCP; an endPort makes the entry a range
            return isinstance(p, dict) and str(p.get("port")) == "8080" \
                and (p.get("protocol") or "TCP") == "TCP" and p.get("endPort") is None
        if not isinstance(ports, list) or not ports or not all(scoped(p) for p in ports):
            offenders.append("%s ingress[%d] is not scoped to exactly 8080/TCP — a missing or empty "
                             "ports list admits every port, another protocol or an endPort range "
                             "widens it (the declared ports are not repeated)" % (name, ri))
        if not rule.get("from"):
            # a rule with no `from` admits EVERY peer on those ports — the widest grant there is
            offenders.append("%s ingress[%d] admits every peer (no from:)" % (name, ri)); continue
        for pi, peer in enumerate(rule.get("from") or []):
            if not peer_could_admit_traefik(peer):
                continue      # a same-namespace or other-namespace peer beside the traefik one grants traefik nothing
            peers += 1
            if peer_narrowed(peer) and applies_ingress and narrowed(spec.get("podSelector")) \
                    and isinstance(ports, list) and ports and all(scoped(p) for p in ports):
                effective += 1
            if not peer_narrowed(peer):
                offenders.append("%s ingress[%d] from[%d] is not narrowed on BOTH sides in ONE "
                                 "element to the traefik namespace AND the traefik pod (by matchLabels "
                                 "or a one-value In; an Exists narrows to nothing) — the selectors are "
                                 "not repeated" % (name, ri, pi))
if not effective:
    # an empty ingress list is a policy that isolates the intake and admits NOBODY:
    # traefik cannot reach it, and the edge's 403 proves nothing about that
    offenders.append("no effective rule admits the traefik pod to the intake on 8080/TCP — the "
                     "door is isolated from its own ingress controller")
if offenders:
    print("FAIL " + "; ".join(offenders)); sys.exit(1)
print("OK %d peer(s), every one namespace+pod narrowed in one from-element, 8080 only, %d effective traefik grant(s)" % (peers, effective))
PY
)
  [ $? -eq 0 ] && ok "static-parse (no cluster): the traefik->intake grant is pod-narrowed on BOTH sides in ONE from-element and port-scoped to 8080 [$WIOUT]" \
               || bad "static-parse: $WIOUT — both selectors must sit in the SAME from-element; two elements is an OR, i.e. a whole-namespace grant"
  # And the declaration must match the wiring: the generator refuses a `github` trigger
  # without this key and refuses any value but `wired`.
  [ "$GATE_INTAKE" = 1 ] \
    && ok "instance/config.yaml declares intakeIngress: wired — the gate that lets a github trigger render, asserting exactly the door verified above" \
    || bad "the intake door files exist but instance/config.yaml does not declare intakeIngress: wired — the door is live and no github trigger can render (or the declaration is a typo the generator will refuse)"
  # --- live: the door as the internet sees it ---
  if [ "$LIVEOFF" = 1 ]; then
    keyoff_live "the live intake-door rows key off"
  elif kubectl -n orbit-agents-prod get ingress orbit-intake-github >/dev/null 2>&1; then
    TV=$(curl -s -o /dev/null -w '%{ssl_verify_result}' --max-time 15 "https://$IHOST/webhook/github")
    [ "$TV" = "0" ] && ok "$IHOST serves a VALID certificate (Traefik's default wildcard, by SNI — no Certificate object, no ACME order)" \
                    || bad "$IHOST failed certificate verification (ssl_verify_result=$TV) — GitHub will refuse the delivery"
    # 403 is the ONE correct answer from here, and it is a STRICT discriminator: this
    # suite runs from an operator/CI machine, NOT from GitHub's published hook ranges, and
    # ipAllowList sits IN FRONT OF the intake pod.
    #   403 -> router exists, ipAllowList active, our source correctly refused at the edge.
    #   404 -> no router matched: the door is absent or the path scope is wrong.
    #   401/503 -> the request REACHED the intake's HMAC gate from a non-GitHub source,
    #              which means the ipAllowList is GONE from the chain. A security regression.
    #   000 -> no answer: the netpol grant is missing or the edge is down.
    #   2xx -> the door is broken OPEN.
    PC=$(curl -s -o /dev/null -w '%{http_code}' --max-time 15 -X POST "https://$IHOST/webhook/github" \
         -H 'X-GitHub-Event: ping' -H 'Content-Type: application/json' -d '{"zen":"verify suite"}')
    case "$PC" in
      403) ok "POST /webhook/github is refused 403 AT THE EDGE — the router exists and the ipAllowList rejects this non-GitHub source before HMAC; DNS, Traefik and the guard chain are wired (this proves the edge only: a refused source never reaches the intake pod, so the netpol path to it is proven by the delivery rows, not here)";;
      401|503) bad "POST /webhook/github returned $PC — the request reached the intake's HMAC gate from a NON-GitHub source, so the ipAllowList is missing from the router's chain (a security regression)";;
      404) bad "POST /webhook/github returned 404 — nothing routes it: the door is absent or the path scope is wrong";;
      000) bad "POST /webhook/github did not answer at all (timeout) — the netpol grant is missing, so Traefik cannot reach the intake pods, or the edge is down";;
      *)   bad "POST /webhook/github returned $PC — expected 403; a 2xx would mean the door is broken open";;
    esac
    # The two unauthenticated routes must NOT be reachable through the public host. These
    # are the rows that go red the moment the path scope is widened to `/`.
    for p in healthz metrics; do
      # The body goes to a FILE and only a status code and a yes/no come back to the
      # shell: a response body is the far end's text, and a traced shell prints its values.
      RC=$(curl -s --max-time 15 -o "$TMP/door.body" -w '%{http_code}' "https://$IHOST/$p")
      if [ "$RC" != "200" ] && ! grep -q 'orbit_intake\|"ok"' "$TMP/door.body" 2>/dev/null; then
        ok "GET /$p is NOT reachable through $IHOST ($RC) — the intake's unauthenticated route stays private"
      else
        bad "GET /$p answered $RC through the public host — the Ingress is no longer path-scoped and an unauthenticated intake route is on the internet"
      fi
    done
    # Traversal: Traefik normalises the request path — dot segments and percent-encoded
    # dot segments alike — BEFORE matching a router, so these normalise to /metrics, match
    # no router on this host and die at the edge. Measured, not assumed; this keeps it so.
    for t in '/webhook/github/../metrics' '/webhook/github/%2e%2e/metrics'; do
      TRC=$(curl -s --path-as-is --max-time 15 -o "$TMP/door.body" -w '%{http_code}' "https://$IHOST$t")
      if [ "$TRC" != "200" ] && ! grep -q 'orbit_intake' "$TMP/door.body" 2>/dev/null; then
        ok "a traversal to the metrics route ($t) is refused at the edge ($TRC) — Traefik normalises before matching"
      else
        bad "$t served $TRC — the path scope can be walked out of; /metrics is reachable through the door"
      fi
    done
    RC=$(curl -s -o /dev/null -w '%{http_code}' --max-time 15 "https://$IHOST/")
    [ "$RC" = "404" ] && ok "the root of $IHOST publishes nothing (404) — the host carries one router and one path" \
                      || bad "GET https://$IHOST/ answered $RC, expected 404 — something other than the webhook path is published on this host"
  elif kubectl get ns orbit-agents-prod >/dev/null 2>&1; then
    bad "the door is declared wired and the cluster is reachable, but Ingress orbit-intake-github is ABSENT — declared and NOT live (Argo has not synced it, or it was pruned)"
  else
    bad "the door is declared wired but the live door cannot be confirmed — kubectl could read neither the Ingress nor the namespace; a declared door that cannot be verified is not a green pass"
  fi
fi

# Grafana and Argo CD both sign in with their local admins now: the identity
# provider is retired, so there is no sign-on chain to check. Their doors (the
# dashboards' login page and datasource rows above) stay.
echo "== every in-cluster URL names a port its Service actually declares =="
# A Service answers on the ports it DECLARES and nothing else. kube-proxy installs DNAT
# for declared ports only, so a SYN to a ClusterIP on an undeclared port matches no rule,
# the service CIDR has no route and no host, and the packet is BLACKHOLED. The caller does
# not get a refusal — it hangs until its own timeout, and that signature is the one a
# NetworkPolicy denial cannot produce, because a denial here fails in 0 ms. Found the hard
# way: one in-cluster URL hung for 8s while the same pod on another port returned 200, and
# the netpol was audited, probed 76 ways and exonerated before the URL was suspected.
#
# STATIC, deliberately: a live probe of a URL nobody dials today proves nothing, and the
# failure mode here is a hang, which a probe reports as "still waiting". FAIL-CLOSED on
# three counts, each one a way an earlier draft would have waved the bug through: a
# Service this repo cannot resolve is RED, not skipped; a URL with NO port means 80 for
# http and 443 for https, never "no opinion", because that is the number the caller will
# actually dial; and a URL naming an upstream chart's Service is RED until that Service
# and its ports are declared in THIRD_PARTY, in the same commit that introduces the URL.
SVCPORT=$(python3 - . <<'PY'
import os, posixpath, re, subprocess, sys, tempfile, yaml
ROOT = sys.argv[1]
SCAN = ["clusters", "catalog"]
# Services installed by an upstream chart. Their ports exist in nothing this repo renders,
# so they are declared here BY HAND, with every port the chart publishes. This table is
# the fail-closed boundary: a URL naming a third-party Service not in it is an offender,
# not a skip — otherwise the next undeclared-port bug picks a Service the guard has never
# heard of and the guard waves it through.
THIRD_PARTY = {
    ("kubernetes", "default"): {443},
    ("tempo", "tempo"): {3200, 4317, 4318, 6831, 6832, 9411, 14250, 14268, 55678, 55680, 55681},
    ("loki", "loki"): {3100, 9095, 9096},
    ("kube-prometheus-stack-prometheus", "monitoring"): {8080, 9090},
    ("kube-prometheus-stack-alertmanager", "monitoring"): {8080, 9093},
    ("kube-prometheus-stack-grafana", "monitoring"): {80},
    # The prometheus-blackbox-exporter chart, with the template's `fullnameOverride:
    # blackbox-exporter` (clusters/platform/apps/blackbox-exporter.yaml). Its single
    # Service port is 9115 — the address every platform Probe is DISPATCHED through, and
    # the one this repo got wrong until the commit that added the Probe row below.
    ("blackbox-exporter", "monitoring"): {9115},
}
# ANY URI SCHEME, not just http(s) (round 1, Sol 1 / Muse 2). `redis://host:6379`,
# `postgres://host:5432` and friends dial a Service port exactly as an http URL does, and
# anchoring on `https?` made every one of them invisible to this guard — including one
# this template ships. Helm templating is substituted out FIRST so a `{{ .password }}`
# sitting in the USERINFO cannot hide the host behind it; an address whose HOST is itself
# computed at runtime is not one this repo can resolve, so it is counted and named rather
# than silently skipped.
TPL_RE = re.compile(r"\{\{.*?\}\}")
TEMPLATED = "\x00"
# Userinfo (anything up to an `@` before the first `/`, `?` or `#`) is consumed here and
# never captured: a password may hold the very punctuation that ends a bare URL, and a
# match cut inside it would hand the authority parser the secret as the host.
URL = re.compile(r"(?<![A-Za-z0-9+.\-])([A-Za-z][A-Za-z0-9+.\-]*)://(?:[^\s/?#]*@)?([^\s\"'`,;)\]]+)")
# The port a scheme dials when the URL names none. ONLY the two the platform's manifests
# actually use: for every other scheme the port is REQUIRED, because guessing a default is
# how a blackholed call gets blessed.
SCHEME_PORT = {"http": 80, "https": 443}

def walk():
    for d in SCAN:
        # os.walk swallows a listing error by default; a directory this scan could not
        # read is a scan that shrank — RED, its class and its top, never its path (round 22)
        def unlisted(e, d=d):
            findings.append("a directory under %s could not be listed (%s; the path is not repeated) — the endpoint scan is INCOMPLETE" % (d, type(e).__name__))
        for root, sub, files in os.walk(os.path.join(ROOT, d), onerror=unlisted):
            sub[:] = [x for x in sub if x not in (".git", ".github", "node_modules", ".claude")]
            # the renderer follows a symlink; this walk does not descend one — RED, never a
            # silent gap; the link's path is not repeated (round 26)
            linked = [x for x in sub + files if os.path.islink(os.path.join(root, x))]
            if linked:
                findings.append("%d symlink(s) under %s (the renderer follows them; this scan does not — the paths are not repeated) — the endpoint scan is INCOMPLETE" % (len(linked), d))
                sub[:] = [x for x in sub if x not in linked]; files = [x for x in files if x not in linked]
            for f in files:
                yield os.path.join(root, f)

UNREADABLE = []
findings = []                # duplicate definitions land here first (round 17)
def docs(path):
    try:
        return [d for d in yaml.safe_load_all(open(path, encoding="utf-8", errors="replace"))
                if isinstance(d, dict)]
    except Exception:
        UNREADABLE.append(path)      # judged below: RED when an Application applies the file
        return []

# `<name>.<namespace>` with no `.svc` suffix is a perfectly ordinary in-cluster address,
# and knowing this cluster's namespaces is what tells it apart from a public hostname.
import fnmatch
import shown as display   # the shared display rule (scripts/verify/shown.py)
def _patterns(exclude):
    if exclude is None or exclude is False: return []
    parts = [str(p) for p in exclude] if isinstance(exclude, list) else [str(exclude)]
    out = []
    for part in parts:
        text = part.strip().strip("'\"")
        if text.startswith("{") and text.endswith("}"): out += [t.strip() for t in text[1:-1].split(",") if t.strip()]
        elif text: out += [t.strip() for t in text.split(",") if t.strip()]
    return out
def _glob_match(rel, pat):
    if pat.endswith("/**"):
        prefix = pat[:-3]; return rel == prefix or rel.startswith(prefix + "/")
    return fnmatch.fnmatch(rel, pat) or fnmatch.fnmatch(os.path.basename(rel), pat)
namespaces, apps, dirapps, companions, platform_docs = set(), [], [], {}, []
for p in walk():
    for d in docs(p):
        if d.get("kind") == "Namespace":
            namespaces.add((d.get("metadata") or {}).get("name"))
        elif d.get("kind") == "Application":
            spec = d.get("spec") or {}
            ns = (spec.get("destination") or {}).get("namespace")
            rel = os.path.relpath(p, ROOT).replace(os.sep, "/").strip("/")
            # the renderer's entrypoints (round 19): platform.py load_applications reads
            # clusters/platform/apps/*.yaml|*.yml only, and a nested Application document
            # under a directory source is skipped; a pair file's Application renders its
            # chart and companions (render.py). An Application anywhere else applies nothing.
            platform_app = os.path.dirname(rel) == "clusters/platform/apps"
            pair_app = os.path.dirname(rel) in ("clusters/orbit", "clusters/apps")
            if not (platform_app or pair_app): continue
            if platform_app:
                # ONE Application per name — the renderer keeps the LAST in path order
                # (platform.py load_applications by_name): collected, resolved below (round 20)
                platform_docs.append((rel, p, d)); continue
            if ns: namespaces.add(ns)
            apps.append((p, spec, str((d.get("metadata") or {}).get("name") or "")))
            # a pair file's (clusters/orbit/<p>-<e>.yaml, clusters/apps/) companion documents
            # are applied into the Application's own destination by orbitd's renderer
            # (render.py: extras) — round 18
            if isinstance(ns, str) and ns:
                companions.setdefault(rel, []).append(ns)
_oname = lambda v: display.shown(v, "object-name")
_fname = lambda rel: display.shown_parts(rel)
def _srcpath(p):
    # the renderer's normal form of a source path (platform.py _render_directory joins it
    # through pathlib: `./` and `a/../` vanish) — round 21; a path climbing out applies nothing
    n = posixpath.normpath(p.strip().lstrip("/"))
    return None if n in (".", "..") or n.startswith("../") else n
winners, seen_at = {}, {}
for rel, p, d in sorted(platform_docs, key=lambda t: t[0]):
    name = str((d.get("metadata") or {}).get("name") or "")
    if not name: continue
    if name in seen_at:
        findings.append("Application %s is declared twice (%s and %s) — the renderer applies the LAST in path order "
                        "and never the other's source (platform.py load_applications)" % (_oname(name), _fname(seen_at[name]), _fname(rel)))
    seen_at[name] = rel; winners[name] = (p, d)
for name in sorted(winners):
    p, d = winners[name]
    spec = d.get("spec") or {}
    ns = (spec.get("destination") or {}).get("namespace")
    if ns: namespaces.add(ns)
    apps.append((p, spec, name))
    # a directory Application (a path, no chart — platform.py render_application):
    # it applies the files directly under its path, every file below with
    # directory.recurse, minus directory.exclude; a namespace-less object is
    # applied INTO its destination namespace (shadow.py _apply_namespace)
    src = spec.get("source") or {}
    directory = src.get("directory") or {}
    if not isinstance(directory, dict): directory = {}
    if not src.get("chart") and isinstance(src.get("path"), str) and isinstance(ns, str) and ns:
        path = _srcpath(src["path"])
        if path is not None and not os.path.isdir(os.path.join(ROOT, src["path"].strip().lstrip("/"))):
            # the renderer opens the path as written; one reached through a missing
            # directory is a RenderError, not a source (round 26)
            findings.append("Application %s names a source directory the renderer cannot open (missing, or reached through a missing directory; the path is not repeated) — the renderer refuses the set" % display.shown(name, "object-name")); continue
        if path is not None: dirapps.append((path, ns, bool(directory.get("recurse")), _patterns(directory.get("exclude") or "")))
def dests_for(path):
    """The destination namespaces of EVERY Application that applies this file (each is
    rendered on its own — round 17), in file order; [] when none does."""
    rel = os.path.relpath(path, ROOT).replace(os.sep, "/").strip("/"); out = []
    for prefix, ns, recurse, excl in dirapps:
        if not (rel == prefix or rel.startswith(prefix + "/")): continue
        sub = rel[len(prefix) + 1:]
        if not recurse and "/" in sub: continue
        if any(_glob_match(sub, pt) for pt in excl): continue
        if ns not in out: out.append(ns)
    for ns in companions.get(rel, []):
        if ns not in out: out.append(ns)
    return out
def dest_for(path):
    """Is this file applied at all? (the first destination, or None)"""
    d = dests_for(path); return d[0] if d else None

svcs = {}
# a port is (number, protocol): a Service exposing only UDP/8080 does not answer an HTTP
# dial of 8080 (round 16); a bare number in the tables above means TCP
def add(name, ns, ports): svcs.setdefault((name, ns), set()).update((p, "TCP") if isinstance(p, int) else p for p in ports)
# a Service is ONE definition: two applied definitions of name/namespace are a conflict
# the API server resolves by whichever applied last, never a union of their ports
# (round 17) — the second definition is RED, and its ports are not merged
defined = {}
def define(name, ns, ports, where):
    if (name, ns) in defined:
        findings.append("Service %s.%s is defined twice (%s and %s) — the cluster holds ONE of them, not the union of their ports"
                        % (shown_name(name), shown_name(ns), defined[(name, ns)], where)); return
    defined[(name, ns)] = where; add(name, ns, ports)
# every name a row here repeats goes through the shared display rule
def shown_name(v): return display.shown(v, "object-name")
def shown_path(p): return display.shown_parts(p)
def host_disp(h): return display.shown_parts(h, "host-label", "label", ".")
def ports_of(doc):
    out = set()
    for p in ((doc.get("spec") or {}).get("ports") or []):
        if p.get("port") is None: continue
        try:
            v = p["port"]
            if isinstance(v, bool) or not (isinstance(v, int) or (isinstance(v, str) and v.isdigit())) or not 1 <= int(v) <= 65535:
                raise ValueError("not a port")          # 8080.9 is not 8080 (round 18)
            proto = p.get("protocol")
            if proto is None: proto = "TCP"
            if proto not in ("TCP", "UDP", "SCTP"):      # the API's enum, case-sensitive; never repeated (round 26)
                raise KeyError("not a protocol")
            out.add((int(v), proto))
        except (TypeError, ValueError, KeyError) as exc:
            # the value is not repeated: a port or protocol field can hold anything a template put there
            m = doc.get("metadata") or {}
            what = "a protocol that is not TCP, UDP or SCTP (the value is not repeated)" if isinstance(exc, KeyError) else "a port that is not an integer in 1..65535"
            print("FAIL Service %s/%s declares %s" % (shown_name(m.get("namespace")), shown_name(m.get("name")), what))
            sys.exit(1)
    return out
for p in walk():
    for d in docs(p):
        if d.get("kind") == "Service":
            m = d.get("metadata") or {}
            if not dests_for(p):
                continue      # no Application applies this file: it is nobody's Service
            for ns in ([m.get("namespace")] if m.get("namespace") else dests_for(p)):
                define(m.get("name"), ns, ports_of(d), shown_path(os.path.relpath(p, ROOT)))
# The Services that exist only after Helm runs are RENDERED here, never re-implemented: a
# second copy of the chart's port rule would drift from the chart, and a drifted copy
# would bless exactly the URLs the cluster blackholes.
for path, spec, aname in apps:
    src = spec.get("source") or {}
    if src.get("path") != "charts/service": continue
    ns = (spec.get("destination") or {}).get("namespace")
    vals = (src.get("helm") or {}).get("valuesObject")
    if vals is None: continue
    # the renderer's release name and namespace (render.py: helm.releaseName or the
    # Application's name, in its destination) — a placeholder name hid Helm's refusal of a
    # release over 53 characters (round 22); both are tree text and reach argv only in
    # object-name shape
    release = str((src.get("helm") or {}).get("releaseName") or aname or "").strip()
    if shown_name(release) != release or not isinstance(ns, str) or shown_name(ns) != ns or len(ns) > 63:
        print("FAIL %s: its release name or destination namespace is not of an object-name shape (not repeated) — "
              "the renderer cannot hand it to Helm" % shown_path(path)); sys.exit(1)
    with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False) as fh:
        yaml.safe_dump(vals, fh); vf = fh.name
    try:
        out = subprocess.run(["helm", "template", release, os.path.join(ROOT, "charts/service"),
                              "--namespace", ns, "-f", vf], capture_output=True, text=True)
    except FileNotFoundError:
        # Not a skip. Half the Services in this repo exist only after Helm renders the
        # chart, so without helm the resolver cannot say which ports they declare and every
        # URL naming one would be judged against an incomplete table — the exact
        # false-green this row exists to prevent. Red WITH A REASON: an empty
        # `FAIL static-parse:` from an uncaught traceback tells the operator nothing.
        os.unlink(vf)
        print("FAIL helm is not installed — cannot resolve charts/service ports, so the "
              "Services %s renders are unknown and no in-cluster URL naming one can be "
              "judged. Install helm, or run this stage where the toolchain is complete"
              % path)
        sys.exit(1)
    os.unlink(vf)
    if out.returncode != 0:
        # Helm's message is not repeated: a template error quotes the values it choked on.
        print("FAIL could not render %s to learn which Services it declares (helm exited %d; "
              "run `helm template` on charts/service with that Application's values to read the error)"
              % (shown_path(path), out.returncode)); sys.exit(1)
    for d in yaml.safe_load_all(out.stdout):
        if not isinstance(d, dict): continue
        if d.get("kind") == "Service":
            define((d.get("metadata") or {}).get("name"), ns, ports_of(d), shown_path(os.path.relpath(path, ROOT)) + " (rendered)")
        elif d.get("kind") == "Cluster" and str(d.get("apiVersion", "")).startswith("postgresql.cnpg.io"):
            # CNPG creates -rw / -ro / -r on 5432 for every Cluster. They are as real as
            # any other Service and appear in no manifest; without this a legitimate
            # database URL would go RED for being unresolvable.
            for suffix in ("-rw", "-ro", "-r"):
                add(((d.get("metadata") or {}).get("name") or "") + suffix, ns, {5432})
for (name, ns), pset in THIRD_PARTY.items():
    # the cluster holds the definition the tree applies, not a union of it with the hand
    # table: a chart-installed Service the tree ALSO defines is a conflict (round 18)
    if (name, ns) in defined:
        findings.append("Service %s.%s is declared by hand as third-party AND defined by %s — the cluster holds that "
                        "definition alone, not the union of its ports with the hand table; drop one" % (name, ns, defined[(name, ns)]))
        continue
    add(name, ns, pset)

# Services the CORE GENERATOR renders only once the catalog asks for them, and that the
# template names BEFORE that happens. There is exactly one today: Alertmanager's
# `orbit-intake` receiver, whose route matches no alertname until some agent declares a
# trigger — the template says so in place. Until the plane exists the URL is INERT, so it
# is not an offender; the moment catalog-sync renders the Service, the rendered ports
# above take over and this table stops being consulted for it. Listed by hand, with the
# port the generator gives it, so a forward reference is a deliberate act rather than a
# hole every unresolvable name falls through.
FORWARD = {("orbit-agents-intake", "orbit-agents-prod"): {80}}

def resolve(name, ns):
    """The ports this Service declares, or None if the repo cannot say who it is."""
    if ns is None:
        # no namespace to resolve in: the repo cannot say which Service this is
        return None
    # exactly (name, ns): a Service is an identity in ONE namespace, never a name that
    # matches in every namespace looked up
    return svcs.get((name, ns)) or None

N = {"checked": 0, "inert": 0, "templated": 0}

def consider(raw, where, scheme, default_ns=None):
    """Judge one endpoint. `scheme` is "" for a SCHEMELESS address — the form a Probe's
    prober url takes — in which case a port MUST be named, because there is no scheme to
    take a default from and guessing one is how a blackhole gets blessed."""
    # The authority alone: what precedes the first "/", "?" or "#" (an address with no
    # slash can still carry a query or a fragment), minus any userinfo. That is all a
    # finding shows and all the judgement below reads: path, query, fragment and userinfo
    # can each carry a credential.
    rest = raw.split("/", 1)[0].split("?", 1)[0].split("#", 1)[0]
    if "@" in rest:                      # userinfo, e.g. redis://:<password>@host:6379
        rest = rest.rsplit("@", 1)[1]
    shown = (scheme + "://" if scheme else "") + rest.replace(TEMPLATED, "{{...}}")
    host, colon, port = rest.rpartition(":")
    if colon and not (port.isdigit() and 1 <= int(port) <= 65535):
        # `host:not-a-port` or `host:99999`: text after the last colon that is not a
        # port. Nothing dials this; the address is unusable, named and RED, never read
        # as a host with an odd name.
        # the authority is NOT repeated: what follows the colon is not a port, and a
        # pasted value can be anything; the host is shown only when it is host-shaped
        host_shown = host.replace(TEMPLATED, "{{...}}") if re.fullmatch(r"[A-Za-z0-9.{}\-]{1,253}", host) and not display.tokenish(host) else display.described("hostname", "host")
        findings.append("%s %s://%s:<not a port> names a malformed port (text after the last colon "
                        "that is not 1..65535; it is not repeated) — nothing can dial this address"
                        % (where, scheme or "(no scheme)", host_shown))
        return
    if not colon:
        host, port = rest, ""
    host = host.rstrip(".").lower()
    if not re.fullmatch(r"[a-z0-9.{}\-]{1,253}", host):
        # `[text]`, a space, a quote: not a hostname; nothing dials it, and the text is
        # a pasted value as likely as a typo, so it is not repeated
        findings.append("%s %s://(an authority not of a hostname shape; not repeated) names a "
                        "malformed host — nothing can dial this address" % (where, scheme or "(no scheme)"))
        return
    if not host or TEMPLATED in host:
        # The HOST itself is computed at render time. This repo cannot say who it is, and
        # neither can anyone reading it — named, counted, not silently dropped.
        N["templated"] += 1
        return
    shown = (scheme + "://" if scheme else "") + host_disp(host) + (":" + port if port else "")
    h, insvc = host, False
    for suffix in (".svc.cluster.local", ".svc"):
        if h.endswith(suffix):
            h, insvc = h[: -len(suffix)], True
            break
    labels = h.split(".")
    # IS THIS AN ADDRESS IN THIS CLUSTER? A `.svc` suffix says so outright, and
    # `<name>.<namespace>` says so once the namespace is one this repo creates. A BARE
    # single label is the ambiguous case, and widening the scan past http(s) made it
    # load-bearing: `redis://infisical-redis:6379` is a Service, `s3://some-bucket/path`
    # is not, and both are one label. The discriminator is "does it name a PORT, or is it
    # a scheme whose default port is a real dial" — an object-store URL names neither.
    if not insvc:
        if len(labels) == 2:
            if labels[1] not in namespaces:
                return                    # a public hostname
        elif len(labels) == 1:
            if not port and scheme.lower() not in SCHEME_PORT:
                return                    # a bucket, a repo path, anything but a Service
        else:
            return                        # a public hostname
    if len(labels) == 1:
        if default_ns is None:
            findings.append("%s %s is a bare Service name in a file with no single namespace — it "
                            "resolves in the consumer's namespace, which this repo cannot name, so "
                            "it is unresolvable (RED); write <service>.<namespace>" % (where, shown))
            return
        if shown_name(default_ns) != default_ns:
            # the file's namespace is the document's own text: one not of a namespace
            # shape is refused by the API, so nothing resolves in it — RED, not repeated (round 22)
            findings.append("%s %s is a bare Service name in a file whose namespace is not of a namespace "
                            "shape (not repeated) — the API refuses that document, and the name resolves "
                            "nowhere (RED)" % (where, shown))
            return
        svcname, nsname = labels[0], default_ns
    elif len(labels) == 2:
        svcname, nsname = labels
    else:
        findings.append("%s %s has more labels than <service>.<namespace> — unparseable is RED"
                        % (where, shown))
        return
    N["checked"] += 1
    if port:
        want = int(port)
    else:
        want = SCHEME_PORT.get(scheme.lower())
        if want is None:
            findings.append("%s %s names no port and its scheme %r has no default this repo will "
                            "guess — an in-cluster address must say which port it dials, because "
                            "the wrong one is a BLACKHOLE (a hang), not a refusal"
                            % (where, shown, scheme or "(none)"))
            return
    declared = resolve(svcname, nsname)
    tcp = None if declared is None else {p for p, proto in declared if proto == "TCP"}     # every URL scheme here dials TCP
    if declared is None and FORWARD.get((svcname, nsname)) == {want}:
        N["inert"] += 1
        return
    if declared is None:
        findings.append("%s %s names Service %s%s, which this repo neither declares, renders, nor "
                        "lists in THIRD_PARTY — an unresolvable Service is RED, not a skip"
                        % (where, shown, host_disp(svcname), "" if nsname is None else "." + host_disp(nsname)))
    elif want not in tcp:
        findings.append("%s %s dials port %d, which Service %s%s does not declare on TCP (it declares %s) "
                        "— kube-proxy has no rule for that port, so this call is blackholed and "
                        "hangs to the caller's timeout"
                        % (where, shown, want, host_disp(svcname),
                           "" if nsname is None else "." + host_disp(nsname), sorted("%d/%s" % p for p in declared)))

# The namespace a file's workloads live in, so a BARE Service name in it resolves the way
# the kubelet resolves it — in the consumer's own namespace — and never to "the one
# Service of that name anywhere". An Argo Application's workloads live in its
# spec.destination.namespace; a plain manifest's in metadata.namespace. A file with no
# single namespace gives a bare name no home, and the address is then unresolvable (RED).
def doc_ns(d):
    ns = ((d.get("spec") or {}).get("destination") or {}).get("namespace") if d.get("kind") == "Application" \
        else (d.get("metadata") or {}).get("namespace")
    return ns if isinstance(ns, str) and ns else None

file_ns = {}
for p in walk():
    found = {doc_ns(d) for d in docs(p)} - {None}
    file_ns[p] = found.pop() if len(found) == 1 else None

# PASS 1 — every URL in every file: over the DECODED string values where the file reads
# as YAML (a quoted scalar YAML folds across lines is one value, not two half-URLs), and
# over the raw text where it does not (a templated file with {{tokens}} in bare scalars).
def strings_of(node):
    if isinstance(node, str):
        yield node
    elif isinstance(node, dict):
        for v in node.values():
            yield from strings_of(v)
    elif isinstance(node, list):
        for v in node:
            yield from strings_of(v)

def scan_lines(lines, where_of, default_ns):
    for lineno, line in enumerate(lines, 1):
        # A URL in a COMMENT is prose, not a call — nothing dials it, and the template's
        # own documentation writes half-addresses like `https://status.<primary domain>`
        # that no parser can or should resolve. Skipping them keeps this row about the
        # URLs a process will actually open.
        if line.lstrip().startswith("#"):
            continue
        for m in URL.finditer(TPL_RE.sub(TEMPLATED, line)):
            consider(m.group(2), where_of(lineno), m.group(1), default_ns)

CATALOG = os.path.join(ROOT, "catalog", "projects.yaml")

def catalog_units(d):
    """The catalog's values, each with the namespaces it renders into: a component's
    values (its env above all) land in that project's namespace for every environment
    the generator renders (<id>-<env>: prod, and any other env that is enabled), so a
    bare sibling name such as http://<id>-engine:8080 resolves there and is judged there.
    Everything else in the catalog has no home of its own (None: the file's rule)."""
    yield {k: v for k, v in d.items() if k != "projects"}, None
    for pr in d.get("projects") or []:
        if not isinstance(pr, dict):
            yield pr, None; continue
        pid, nss = pr.get("id"), []
        if isinstance(pid, str) and pr.get("hosted", "self") == "self" and not pr.get("managed_elsewhere"):
            for env, cfg in (pr.get("environments") or {}).items():
                if env == "prod" or (isinstance(cfg, dict) and cfg.get("enabled")):
                    ns = "%s-%s" % (pid, env)
                    if shown_name(ns) == ns and len(ns) <= 63:
                        nss.append(ns)
        yield {k: v for k, v in pr.items() if k != "components"}, None
        for c in pr.get("components") or []:
            yield c, (nss or None)

for p in walk():
    ds = docs(p)
    if ds:
        # a decoded value belongs to ONE document, and a bare Service name in it resolves
        # in that document's namespace (a file that carries a routing ConfigMap in the
        # agent plane and a logs ConfigMap in loki is one file with two homes)
        for d in ds:
            # ... or, with no namespace anywhere in the file, in the destination namespace
            # of EVERY directory Application that ships it (the renderer's rule, once per
            # Application — round 17)
            homes = [doc_ns(d) or file_ns.get(p)] if (doc_ns(d) or file_ns.get(p)) else (dests_for(p) or [None])
            units = catalog_units(d) if os.path.abspath(p) == os.path.abspath(CATALOG) and isinstance(d.get("projects"), list) else [(d, None)]
            for node, own in units:
                for s in strings_of(node):
                    for home in (own or homes):
                        scan_lines(s.splitlines(), lambda n, p=p: "%s (a decoded value, line %d of it)" % (shown_path(p), n), home)
        continue
    try: text = open(p, encoding="utf-8", errors="replace").read()
    except Exception: continue
    for home in ([file_ns.get(p)] if file_ns.get(p) else (dests_for(p) or [None])):
        scan_lines(text.splitlines(), lambda n, p=p: "%s:%d" % (shown_path(p), n), home)

# PASS 2 — the endpoint fields that carry NO SCHEME, read STRUCTURALLY because no URL
# regex will ever see them. `Probe.spec.prober.url` is the worked example and the reason
# this pass exists: it is written `blackbox-exporter.monitoring.svc:9115`, it is the
# address EVERY platform uptime probe is dispatched through, and when it names a Service
# that does not exist the whole probe set is dead on arrival while `probe_success` simply
# has no series and nothing anywhere says a word. This template shipped exactly that bug
# — the chart sets `fullnameOverride: blackbox-exporter` and the Probe named
# `kube-prometheus-stack-blackbox-exporter...` — and a 40/0 pre-boot run said nothing.
for p in walk():
    for di, d in enumerate(docs(p)):
        if d.get("kind") != "Probe":
            continue
        meta = d.get("metadata") or {}
        # a Probe's name is repeated only when it is an object name; otherwise by position
        pname = meta.get("name")
        pname = pname if display.fits(pname, "object-name") else "(document %d, a name not of an object-name shape, or of a credential's shape)" % di
        raw = ((d.get("spec") or {}).get("prober") or {}).get("url")
        if not isinstance(raw, str) or not raw.strip():
            findings.append("%s Probe %s declares no spec.prober.url — it is dispatched at "
                            "nothing, and every target under it is dead on arrival"
                            % (shown_path(p), pname))
            continue
        raw = TPL_RE.sub(TEMPLATED, raw.strip())
        scheme, sep, rest = raw.partition("://")
        if not sep:
            scheme, rest = "", raw
        # default_ns = the Probe's OWN namespace: a bare `blackbox-exporter:9115` in a
        # Probe in ns monitoring means the Service next door, not an ambiguous global name.
        # ...and a Probe with no namespace of its own is applied INTO the destination of
        # every Application that renders it (platform.py / shadow.py) — round 20
        for home in ([meta.get("namespace")] if meta.get("namespace") else (dests_for(p) or [None])):
            consider(rest, "%s Probe/%s spec.prober.url" % (shown_path(p), shown_name(pname)), scheme, default_ns=home)

checked, inert = N["checked"], N["inert"]
# only what the renderer reads: a YAML file an Application applies (a README beside it is nobody's manifest)
unread = sorted({p for p in UNREADABLE if p.endswith((".yaml", ".yml")) and dest_for(p) is not None})
if unread:
    findings.append("%d applied manifest file(s) cannot be read as YAML (the renderer refuses the whole tree; "
                    "the parser's message is not repeated)" % len(unread))
if findings:
    print("FAIL " + "; ".join(sorted(set(findings)))); sys.exit(1)
print("OK %d in-cluster endpoint(s) — URLs of ANY scheme plus every Probe's schemeless "
      "prober address — checked against %d Service(s) declared by this repo%s%s"
      % (checked, len(svcs),
         ", %d inert forward reference(s) to a Service the catalog has not asked for yet"
         % inert if inert else "",
         ", %d address(es) whose host is templated at render time" % N["templated"]
         if N["templated"] else ""))
PY
)
[ $? -eq 0 ] && ok "static-parse (no cluster): every in-cluster URL under clusters/ and catalog/ names a port its Service declares [$SVCPORT] — an undeclared port is a BLACKHOLE, not a refusal, so this is the row that turns an 8-second hang into a red line in CI; an unresolvable Service name and a portless URL to a Service with no :80 both fail it too" \
             || bad "static-parse: $SVCPORT"

echo
echo "stage5-platform: $PASS passed, $FAIL failed"
[ "$FAIL" -eq 0 ]
