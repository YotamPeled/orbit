#!/usr/bin/env bash
# Stage 3 — delivery. Assumes stage2.sh passed.
set -uo pipefail
cd "$(dirname "$0")/../.."
# the python blocks below import the shared display rule (scripts/verify/shown.py)
export PYTHONPATH="$(pwd)/scripts/verify${PYTHONPATH:+:$PYTHONPATH}"
. "$(dirname "$0")/../lib-instance.sh" || exit 2
export KUBECONFIG="${KUBECONFIG:-$HOME/.kube/config-${ORBIT_INSTANCE_NAME}-platform}"
PASS=0; FAIL=0

# Pre-boot mode: a scaffolded-but-never-bootstrapped instance repo has no kubeconfig
# and no node. Live-cluster and node assertions key off — as ONE loud, visible check —
# on the kubeconfig FILE's existence (a declaration that bootstrap happened), never on
# reachability: a bootstrapped instance whose cluster is down still fails every live
# check. Deleting the kubeconfig to silence the suite is self-announcing.
PREBOOT=0
# KUBECONFIG is a LIST (colon-separated) the way kubectl reads it: pre-boot means no
# entry is a readable non-empty file. An entry that exists but is empty or unreadable is
# neither a cluster nor pre-boot, and the stage refuses rather than guess.
kubeconfig_present(){ local f any=1; set -f; IFS=: ; for f in $1; do IFS=$' \t\n'   # set -f: the list is split, never glob-expanded
    if [ -s "$f" ] && [ -r "$f" ]; then any=0
    elif [ -e "$f" ]; then echo "kubeconfig $f exists but is empty or unreadable — neither a cluster nor pre-boot; fix or remove it" >&2; exit 2; fi
  done; IFS=$' \t\n'; set +f; return $any; }
kubeconfig_present "$KUBECONFIG" || PREBOOT=1
# Tenant-population checks additionally key off the catalog's own declaration: a fresh
# org with zero projects has zero tenant pods/namespaces, truthfully. The agent plane is
# a real namespace with real pods but is NOT under `projects:`, so it is counted here too
# — otherwise an org with zero projects and one agent has running pods while the
# population guard "cannot fail" over them.
# The catalog is instance text: read behind try/except (a YAML error's message quotes the
# line around the error), and an unreadable catalog is UNREADABLE — never the 0 that keys
# every population check off (round 16).
PROJCOUNT=$(python3 -c "
import yaml
try:
    c = yaml.safe_load(open('catalog/projects.yaml')) or {}
    if not isinstance(c, dict): raise ValueError('not a mapping')
    # the projects this cluster serves: hosted elsewhere or managed elsewhere renders nothing
    n = len([p for p in (c.get('projects') or [])
             if isinstance(p, dict) and p.get('hosted', 'self') == 'self' and not p.get('managed_elsewhere')])
    # the agent plane counts toward the POD floor only when it renders a persistent
    # workload: a deployment-mode agent, or a trigger (the intake Deployment). A plane of
    # cron agents alone has pods only while a Job runs; its CronJobs are asserted by name
    # in the agent rows (round 21)
    agents = [a for a in (c.get('agents') or []) if isinstance(a, dict) and a.get('enabled', True)]
    if any((a.get('mode') or 'deployment') != 'cronjob' or a.get('triggers') for a in agents):
        n += 1
    print(n)
except Exception as exc:
    print('UNREADABLE(%s)' % type(exc).__name__)" 2>/dev/null || echo 'UNREADABLE(no-python)')
case "$PROJCOUNT" in
  ''|*[!0-9]*) bad "catalog/projects.yaml could not be read ($PROJCOUNT — the parser's message is not repeated) — every population check below is UNCHECKED, not keyed off"; PROJCOUNT=UNREADABLE;;
esac
ok(){ echo "  ok    $1"; PASS=$((PASS+1)); }
bad(){ echo "  FAIL  $1"; FAIL=$((FAIL+1)); }

# The generator now refuses to run without instance/config.yaml, and refuses a catalog
# that still declares defaults:/dnsZones itself. Sandboxes below that build a catalog from
# scratch (rather than `cp -r instance` alongside a real catalog/ tree) need their own
# fixture instance/config.yaml — same shape as the real file, values are the fixtures' own.
write_sandbox_instance() { # <dir>
  mkdir -p "$1/instance"
  cat > "$1/instance/config.yaml" <<'EOF'
instance:
  name: example
git:
  repoUrl: git@github.com:example-org/example-instance.git
  branch: main
  github_org: example-org
domains:
  primary: example.org
  dev: dev.example.org
  dnsZones: [example.org]
registry: ghcr.io/example-org
EOF
}

# promtool — the TRUTH oracle for a generated alerting rule. Every generator-side check on
# a `metric` trigger is STRUCTURAL (closed schema, CRD-valid `for`, reserved labels); none
# of them parses the PromQL, so a garbage `expr` renders green, is committed, and is only
# discovered when Prometheus refuses to load the rule group — at which point the rule that
# was supposed to wake an agent silently does not exist. promtool is Prometheus's own
# parser, so it is the only honest answer to "is this expression real".
#
# Resolved in this order: $PROMTOOL, promtool on PATH, then a PINNED download (version +
# sha256 below) cached under ~/.cache/orbit-verify. If none works the check goes RED — an
# oracle that cannot run is a check that is not running, and saying so is the point.
PROMTOOL_VERSION=3.5.0
promtool_sha() { case "$1" in
    linux-amd64) echo e811827af26d822afb09a4f28314f61b618b12cff5369835a67f674d8b46f39a;;
    linux-arm64) echo 173389cc42bf09c4e6e54cb53fa07a5a835d7c261e14775d2183181d6e385d1c;;
    darwin-arm64) echo c17bc2c992f0b5515eebe2b4ccd626214f8e309a0fe6ec40ed13c42ec13d05fb;;
  esac; }
PROMTOOL_CACHE="${ORBIT_VERIFY_CACHE:-$HOME/.cache/orbit-verify}"
promtool_bin() { # -> path on stdout, or non-zero
  if [ -n "${PROMTOOL:-}" ] && [ -x "${PROMTOOL:-}" ]; then echo "$PROMTOOL"; return 0; fi
  command -v promtool 2>/dev/null && return 0
  dest="$PROMTOOL_CACHE/promtool-$PROMTOOL_VERSION"
  [ -x "$dest" ] && { echo "$dest"; return 0; }
  case "$(uname -s)-$(uname -m)" in
    Linux-x86_64)   plat=linux-amd64;;
    Linux-aarch64)  plat=linux-arm64;;
    Darwin-arm64)   plat=darwin-arm64;;
    *) return 1;;
  esac
  sha=$(promtool_sha "$plat"); [ -n "$sha" ] || return 1
  mkdir -p "$PROMTOOL_CACHE" 2>/dev/null || return 1
  # a PRIVATE scratch directory per caller: two checkouts verifying at once each extract
  # their own copy and publish it by one rename; whoever loses the rename accepts the
  # winner's binary (it is the same pinned digest)
  work=$(mktemp -d "$PROMTOOL_CACHE/.fetch.XXXXXX" 2>/dev/null) || return 1
  tgz="$work/prometheus-$PROMTOOL_VERSION.$plat.tar.gz"
  # 60s, not 300. This is a ~100MB release asset on a working link; five minutes of a
  # hung TCP connection is five minutes the suite looks alive and is doing nothing, and
  # the failure path is a RED row, not a broken run — so timing out early is cheap.
  curl -sfL --max-time 60 -o "$tgz" \
    "https://github.com/prometheus/prometheus/releases/download/v$PROMTOOL_VERSION/prometheus-$PROMTOOL_VERSION.$plat.tar.gz" \
    >/dev/null 2>&1 || { rm -rf "$work"; return 1; }
  # Pinned by digest: a verify oracle downloaded over the network is itself a supply
  # chain, and an unverified binary run against generated manifests is a worse one.
  echo "$sha  $tgz" | sha256sum -c --status 2>/dev/null || { rm -rf "$work"; return 1; }
  tar -xzf "$tgz" -C "$work" --strip-components=1 \
      "prometheus-$PROMTOOL_VERSION.$plat/promtool" >/dev/null 2>&1 || { rm -rf "$work"; return 1; }
  # The tarball has done its job; the BINARY is the cache, the archive is scratch.
  chmod +x "$work/promtool" 2>/dev/null || { rm -rf "$work"; return 1; }
  publish_promtool "$work/promtool" "$dest"; rc=$?
  rm -rf "$work"
  [ "$rc" -eq 0 ] && echo "$dest"
}
publish_promtool(){ # <private binary> <cache path>: one rename; a binary already published by another caller is accepted
  [ -x "$2" ] && return 0
  mv -n "$1" "$2" 2>/dev/null
  [ -x "$2" ]
}

echo "== generator =="
# Delegate to make check rather than re-implementing a weaker version of it: the earlier
# local copy re-ran catalog-sync.py alone, so it certified a served page that was stale
# relative to the catalog.
make check >/dev/null 2>&1 && ok "catalog, manifests and served page all agree" \
  || bad "the generated files were stale — 'make check' has rewritten them: commit the result"
# Every generated Application must render. A broken chart freezes all delivery while the
# old pods keep serving, so nothing else here would notice.
RENDERFAIL=""; RENDERED=0; ATTEMPTED=0; RENDERODD=0
# The extractor's exit status is checked explicitly. Previously a crash left an EMPTY
# values file behind, and `helm template -f empty.yaml` exits 0 — so an unparseable
# Application read as "renders fine". Empty input is a failure of the check, not a pass.
# A pair's Application sits under clusters/orbit/ when orbitd.applyPairs lists it and
# under clusters/apps/ otherwise; clusters/apps/ also holds files that are not
# Applications (the tenant Probes), which the extractor names and this loop skips.
# The extracted values live in a file of this run's own: two verifications running at
# once (two checkouts, two operators) must not read each other's values.
VALS=$(mktemp)
for f in clusters/apps/*-*.yaml clusters/orbit/*.yaml; do
  [ -e "$f" ] || continue
  case "$f" in */namespaces.yaml|*/dashboard-*.yaml) continue;; esac
  # a file name is tree text: only a name of a file-name shape becomes an argument or a
  # row; the rest are counted (round 20)
  case "${f##*/}" in *[!A-Za-z0-9._-]*) RENDERODD=$((RENDERODD+1)); continue;; esac
  # the render is the renderer's (orbitd render.py): the release is helm.releaseName or
  # the Application's name, in its destination namespace — Helm refuses a release name
  # over 53 characters, which a fixed placeholder name never showed (round 22). Both are
  # tree text: printed for argv only in object-name shape, else the file fails to render
  META=$(python3 -c "
import re,sys,yaml
docs=[x for x in yaml.safe_load_all(open(sys.argv[1])) if x]
apps=[x for x in docs if x.get('kind')=='Application']
if not apps: sys.exit(3)
a=apps[0]; src=a['spec']['source']; helm=src.get('helm') or {}
v=helm['valuesObject']
assert v, 'empty valuesObject'
rel=str(helm.get('releaseName') or (a.get('metadata') or {}).get('name') or '').strip()
ns=str((a['spec'].get('destination') or {}).get('namespace') or '').strip()
NAME=re.compile(r'[a-z0-9]([-a-z0-9.]*[a-z0-9])?')
if not (NAME.fullmatch(rel) and len(rel) <= 253 and NAME.fullmatch(ns) and len(ns) <= 63): sys.exit(4)
yaml.safe_dump(v, open(sys.argv[2], 'w'))
print(rel + ' ' + ns)" "$f" "$VALS" 2>/dev/null)
  rc=$?
  [ "$rc" -eq 3 ] && continue
  ATTEMPTED=$((ATTEMPTED+1))
  [ "$rc" -eq 4 ] && { RENDERFAIL="$RENDERFAIL $f(unshaped-name-or-namespace)"; continue; }
  if [ "$rc" -ne 0 ]; then RENDERFAIL="$RENDERFAIL $f(unreadable)"; continue; fi
  [ -s "$VALS" ] || { RENDERFAIL="$RENDERFAIL $f(empty-values)"; continue; }
  helm template "${META%% *}" charts/service --namespace "${META#* }" -f "$VALS" >/dev/null 2>&1 \
    && RENDERED=$((RENDERED+1)) || RENDERFAIL="$RENDERFAIL $f"
done
rm -f "$VALS"
# A glob that matches nothing must not read as "everything renders" — unless the
# catalog declares no projects at all, in which case zero rendered files is the truth.
EXPECTED=$ATTEMPTED
# Generated dashboards get their own assertion rather than a silent skip: every
# dashboard file must parse, carry valid JSON, and name the uid its filename claims —
# a truncated or hand-mangled dashboard otherwise ships and renders a blank page.
DASHBAD=""
DASHCOUNT=0
DASHODD=0
for f in clusters/apps/dashboard-*.yaml; do
  [ -e "$f" ] || continue
  # a file name is tree text: only a name of a file-name shape becomes an argument or a
  # row; the rest are counted
  case "${f#clusters/apps/}" in
    dashboard-*[!A-Za-z0-9._-]*.yaml|dashboard-.yaml) DASHODD=$((DASHODD+1)); continue;;
  esac
  DASHCOUNT=$((DASHCOUNT+1))
  python3 -c "
import sys, yaml, json, pathlib, re
f = sys.argv[1]
d = yaml.safe_load(open(f))
assert d['kind'] == 'ConfigMap' and d['metadata']['labels']['grafana_dashboard'] == '1'
dash = json.loads(next(iter(d['data'].values())))
stem = pathlib.Path(f).stem.removeprefix('dashboard-')
# the generator's own rule: the readable uid when it fits Grafana's 40, else a digest tail
readable = f'orbit-red-{stem}'
assert (len(readable) <= 40 and dash['uid'] == readable) or (len(readable) > 40 and (dash['uid'] == readable[:40] or re.fullmatch(r'orbit-red-[0-9a-f]{30}', dash['uid']))), (dash['uid'], stem)
assert dash['panels'], 'no panels'
" "$f" 2>/dev/null || DASHBAD="$DASHBAD $f"
done
[ "$DASHODD" -eq 0 ] || bad "$DASHODD dashboard file(s) under clusters/apps have a name not of a file-name shape (not repeated) — they were not checked"
if [ "$DASHCOUNT" -gt 0 ]; then
  [ -z "$DASHBAD" ] && ok "all $DASHCOUNT generated dashboards parse, carry panels, and match their uid" \
    || bad "broken generated dashboard(s):$DASHBAD"
fi
if [ "$PROJCOUNT" = 0 ] && [ "$EXPECTED" -eq 0 ]; then
  ok "no projects declared yet — render check keyed off (arms with the first project)"
  RENDERFAIL=""; EXPECTED=-1
fi
[ "$RENDERODD" -eq 0 ] || bad "$RENDERODD Application file(s) under clusters/apps or clusters/orbit have a name not of a file-name shape (not repeated) — they were not rendered"
if [ "$EXPECTED" = "-1" ]; then :
elif [ -n "$RENDERFAIL" ]; then bad "fails to render:$RENDERFAIL"
elif [ "$RENDERED" -eq 0 ] || [ "$RENDERED" -ne "$EXPECTED" ]; then
  bad "render check covered $RENDERED of $EXPECTED Applications — the check itself is broken"
else ok "every generated Application renders ($RENDERED of $EXPECTED)"; fi

echo "== extraction: the core carries no instance strings =="
# The check itself is scripts/verify/no-instance-strings.py — one implementation, two
# callers (this suite and the core CI test that plants a string and proves it goes red),
# for the same reason no-credentials.py and am-routes.py are files rather than heredocs.
# Extracted from here 2026-09-07, when `.claude/skills` joined its scope. The scope is no
# longer written here OR there: it is DERIVED from core-manifest.txt, so every path an
# instance vendors is checked and adding one to the manifest is the whole edit. (The
# hand-kept list this replaced had already drifted — pitr-drill.sh, core-sync.sh and the
# verify stages were vendored and unchecked while this row said otherwise.) The standing
# label-key exemption is gone with it; see that file's header.
#
# Fail CLOSED. Exit 0 means the check ran and found nothing; 1 means leaks; anything else
# means it did not run, and that must never be reportable as clean. The bug this posture
# exists for: an empty result read as a passing zero (2026-08-28 audit), which is how the
# earlier inline version passed vacuously from the day it was written.
LEAKS=$(python3 scripts/verify/no-instance-strings.py 2>&1); LEAKRC=$?
if [ "$LEAKRC" -eq 0 ]; then
  ok "everything core vendors carries no instance strings (core-manifest.txt scope, values from instance/config.yaml)"
elif [ "$LEAKRC" -eq 1 ]; then
  bad "instance strings leaked into core: $(echo "$LEAKS" | head -5 | tr '\n' ' ')"
else
  bad "the instance-string leak check did not run (exit $LEAKRC: $(echo "$LEAKS" | head -1)) — core is UNCHECKED, not proven clean"
fi

echo "== extraction: the vendored core matches the pin =="
# An instance repo vendors core files at the ref in orbit-core.lock. Editing a vendored
# file in place is the forbidden fork — the fix belongs in orbit-core, then a pin bump.
# In the core repo itself there is no lock and the check keys off.
if [ -f orbit-core.lock ]; then
  if scripts/core-sync.sh --check >/dev/null 2>&1; then
    ok "vendored core matches the pinned ref (no local forks of core files)"
  else
    bad "vendored core drifted from the pin — edit orbit-core and bump, never the vendor copy"
  fi
else
  ok "no orbit-core.lock — this is the core repo itself; vendor check keyed off"
fi

echo "== dispatch authorization (stage 3b) =="
# The deploy path's only authorization check, and until now it had no test. It previously
# failed open twice: a payload that simply OMITTED repo left the env var empty and skipped
# the comparison, and any catalog project with no repo: key was deployable by anyone
# holding the dispatch token. Both are asserted here against a scratch copy of the repo, so
# a real tags.yaml is never written.
# What the generator reads besides the catalog: the service chart and the orbitd Namespace
# document (the instance's copy, or the template's when the sandbox is built from the core).
seed_generator_inputs(){ mkdir -p "$1/clusters/platform/config/orbitd"; cp -r charts "$1/" 2>/dev/null
  { cp clusters/platform/config/orbitd/namespace.yaml "$1/clusters/platform/config/orbitd/" \
    || cp template/provisioning/clusters/platform/config/orbitd/namespace.yaml "$1/clusters/platform/config/orbitd/"; } 2>/dev/null; }
W=$(mktemp -d); cp -r catalog scripts instance "$W/" 2>/dev/null; seed_generator_inputs "$W"; mkdir -p "$W/clusters/apps"
cat > "$W/catalog/projects.yaml" <<'EOF'
projects:
  - id: victim
    name: Victim
    repo: example-org/victim
    environments:
      prod: {branch: main}
    components:
      - {name: api, kind: web, image: ghcr.io/example-org/victim-api, port: 80, path: /}
  - id: noowner
    name: NoOwner
    environments:
      prod: {branch: main}
    components:
      - {name: api, kind: web, image: ghcr.io/example-org/noowner-api, port: 80, path: /}
EOF
printf '# scratch\ntags: {}\n' > "$W/catalog/tags.yaml"
dispatch() { # <label> <expect: refuse|accept> <env assignments...>
  lbl="$1"; want="$2"; shift 2
  # the generator's output is diagnostics over instance text (a refusal quotes the value
  # it refused, and the fixture carries this instance's config): a private file, never a
  # shell value (a traced shell prints every value) and never a row — the row carries the
  # exit status and whether the word "refusing" appeared (round 22)
  OUTF=$(mktemp)
  (cd "$W" && env "$@" python3 scripts/bump-tag.py > "$OUTF" 2>&1); rc=$?
  refused=0; grep -qi refusing "$OUTF" && refused=1; rm -f "$OUTF"
  if [ "$want" = refuse ]; then
    { [ $rc -ne 0 ] && [ $refused = 1 ]; } \
      && ok "dispatch: $lbl" || bad "dispatch: $lbl (rc=$rc; the generator's message is not repeated)"
  else
    [ $rc -eq 0 ] && ok "dispatch: $lbl" || bad "dispatch: $lbl (rc=$rc; the generator's message is not repeated)"
  fi
}
dispatch "legitimate owner accepted" accept \
  REPO=example-org/victim IMAGE=ghcr.io/example-org/victim-api TAG=v1 BRANCH=main
dispatch "wrong owner refused" refuse \
  REPO=example-org/attacker IMAGE=ghcr.io/example-org/victim-api TAG=pwn BRANCH=main
dispatch "omitted repo refused" refuse \
  IMAGE=ghcr.io/example-org/victim-api TAG=pwn BRANCH=main
dispatch "empty repo refused" refuse \
  REPO= IMAGE=ghcr.io/example-org/victim-api TAG=pwn BRANCH=main
dispatch "project with no repo: in catalog is undeployable" refuse \
  REPO=example-org/anything IMAGE=ghcr.io/example-org/noowner-api TAG=pwn BRANCH=main
dispatch "tag with a trailing newline refused" refuse \
  REPO=example-org/victim IMAGE=ghcr.io/example-org/victim-api TAG="v1
" BRANCH=main
dispatch "owner whose branch claims nothing is not an auth failure" accept \
  REPO=example-org/victim IMAGE=ghcr.io/example-org/victim-api TAG=v9 BRANCH=no-such-branch

# One project can be assembled from more than one repository — an engine built from one
# repo and the front end from a website repo. Ownership keyed on the project
# alone refused the front end's own deploy, which is how this gap surfaced: as a correct
# refusal of a legitimate build, rather than as an accepted illegitimate one.
cat > "$W/catalog/projects.yaml" <<'EOF'
projects:
  - id: split
    name: Split
    repo: example-org/split-api
    environments:
      prod: {branch: main}
    components:
      - {name: api, kind: web, image: ghcr.io/example-org/split-api, port: 80, path: /}
      - {name: web, kind: web, image: ghcr.io/example-org/split-web, port: 8080, path: /ui,
         repo: example-org/split-frontend}
EOF
dispatch "a component's own repo may deploy that component" accept \
  REPO=example-org/split-frontend IMAGE=ghcr.io/example-org/split-web TAG=v1 BRANCH=main
dispatch "the project repo may still deploy components that declare none" accept \
  REPO=example-org/split-api IMAGE=ghcr.io/example-org/split-api TAG=v1 BRANCH=main
# And the narrowing must actually narrow: owning the project is not owning every component.
dispatch "the project repo may NOT deploy a component built elsewhere" refuse \
  REPO=example-org/split-api IMAGE=ghcr.io/example-org/split-web TAG=pwn BRANCH=main
dispatch "an unrelated repo may not deploy a component either" refuse \
  REPO=example-org/attacker IMAGE=ghcr.io/example-org/split-web TAG=pwn BRANCH=main

# A component that exists in only some environments must be tagged only for those. A key
# for an environment the component does not render in is never consumed by anything, and
# `git log catalog/tags.yaml` — the deploy history — would show it as deployed there.
cat > "$W/catalog/projects.yaml" <<'EOF'
projects:
  - id: scoped
    name: Scoped
    repo: example-org/scoped
    environments:
      prod: {branch: main}
      dev: {enabled: true, branch: main}
    components:
      - {name: api, kind: web, image: ghcr.io/example-org/scoped-api, port: 80, path: /}
      - {name: tool, kind: web, image: ghcr.io/example-org/scoped-tool, port: 81, path: /t,
         environments: [dev]}
EOF
printf '# scratch\ntags: {}\n' > "$W/catalog/tags.yaml"
dispatch "a dev-only component is deployable" accept \
  REPO=example-org/scoped IMAGE=ghcr.io/example-org/scoped-tool TAG=v1 BRANCH=main
KEYS=$(python3 -c "
import yaml,sys
print(' '.join(sorted((yaml.safe_load(open(sys.argv[1])) or {}).get('tags') or {})))" "$W/catalog/tags.yaml")
case "$KEYS" in
  "scoped/dev/tool") ok "dispatch: a dev-only component is tagged for dev alone ($KEYS)";;
  *) bad "dispatch: dev-only component tagged as '$KEYS' — expected scoped/dev/tool only";;
esac
rm -rf "$W"

# --- two components of one project deploying at once ---------------------------
# A project that builds FOUR images from one push turns that push into four dispatches and
# four jobs
# editing catalog/tags.yaml. Two different mechanisms have dropped one of them in silence:
# the old retry rebased a committed bump onto main, hit a conflict, left unmerged files
# behind and died on those forever; and the concurrency group that replaced it as the
# "real" serialiser cancels a run that is merely PENDING when the next dispatch arrives,
# destroying the job before it can retry anything. A partial deploy reads as success
# everywhere except the component that did not move, which is why this races four real
# processes rather than reasoning about it.
R=$(mktemp -d)
git init -q --bare "$R/origin.git"
# The bare origin's HEAD names main explicitly: git's default branch name is the
# machine's setting, and a clone below must check out the branch the seed pushes.
git --git-dir="$R/origin.git" symbolic-ref HEAD refs/heads/main
git clone -q "$R/origin.git" "$R/seed" 2>/dev/null
cp -r catalog scripts instance "$R/seed/"; seed_generator_inputs "$R/seed"; mkdir -p "$R/seed/clusters/apps"
cat > "$R/seed/catalog/projects.yaml" <<'EOF'
projects:
  - id: three
    name: Three
    repo: example-org/three
    hosted: self
    environments: {prod: {branch: main}}
    components:
      - {name: web, kind: web, image: ghcr.io/example-org/three-web, port: 80, path: /}
      - {name: listener, kind: worker, image: ghcr.io/example-org/three-listener}
      - {name: refresh, kind: cron, image: ghcr.io/example-org/three-refresh, schedule: "0 3 * * *"}
      - {name: replay, kind: web, image: ghcr.io/example-org/three-replay, port: 81, path: /r}
EOF
printf 'tags: {}\n' > "$R/seed/catalog/tags.yaml"
( cd "$R/seed" && git add -A && git -c user.email=t@t -c user.name=t commit -qm seed \
  && git branch -M main && git push -q origin main ) >/dev/null 2>&1
# Four clones taken from the same commit, none aware of the others — the race, exactly.
for c in a b c d; do git clone -q --branch main "$R/origin.git" "$R/$c" 2>/dev/null; cp -r scripts "$R/$c/"; seed_generator_inputs "$R/$c"; done
( cd "$R/a" && IMAGE=ghcr.io/example-org/three-web TAG=t-aaa BRANCH=main \
    REPO=example-org/three ATTEMPTS=8 ./scripts/bump-and-push.sh ) > "$R/a.log" 2>&1 &
( cd "$R/b" && IMAGE=ghcr.io/example-org/three-listener TAG=t-bbb BRANCH=main \
    REPO=example-org/three ATTEMPTS=8 ./scripts/bump-and-push.sh ) > "$R/b.log" 2>&1 &
( cd "$R/c" && IMAGE=ghcr.io/example-org/three-refresh TAG=t-ccc BRANCH=main \
    REPO=example-org/three ATTEMPTS=8 ./scripts/bump-and-push.sh ) > "$R/c.log" 2>&1 &
( cd "$R/d" && IMAGE=ghcr.io/example-org/three-replay TAG=t-ddd BRANCH=main \
    REPO=example-org/three ATTEMPTS=8 ./scripts/bump-and-push.sh ) > "$R/d.log" 2>&1 &
wait
# Read the result out of the bare repository. Cloning it checks out nothing — `git init
# --bare` leaves HEAD on the default branch name, which is not the branch that was
# pushed, and the first version of this check read an empty working tree as "no tags
# landed" while all of them had.
git -C "$R/origin.git" show main:catalog/tags.yaml > "$R/landed.yaml" 2>/dev/null
LANDED=$(grep -cE 'three/prod/(web|listener|refresh|replay): t-(aaa|bbb|ccc|ddd)' "$R/landed.yaml" 2>/dev/null || true)
[ "${LANDED:-0}" -eq 4 ] \
  && ok "four concurrent tag bumps all land — none is silently dropped" \
  || bad "concurrent bumps: only ${LANDED:-0} of 4 landed ($(cat "$R/landed.yaml" 2>/dev/null | tr '\n' ' '))"
# A conflicted working tree is the failure that was actually shipped: the retry loop kept
# running and every attempt died on the unmerged files it had left itself.
CONFLICTED=""
for c in a b c d; do
  git -C "$R/$c" diff --name-only --diff-filter=U 2>/dev/null | grep -q . && CONFLICTED="$CONFLICTED $c"
done
[ -z "$CONFLICTED" ] \
  && ok "no bump left its working tree in a conflicted state" \
  || bad "conflicted after the race:$CONFLICTED"
rm -rf "$R"

# RETIRED (v0.12.1): a row here used to grep `.github/workflows/deploy-on-build.yml` for a
# `concurrency:` group. Its subject was the repository_dispatch receiver of the old
# per-repo deploy path, which core has never shipped — nothing in template/ creates that
# file — so on every instance scaffolded from core the grep read a missing file, exited 2,
# and fell into the else, printing a green row about a workflow that did not exist. Do not
# reintroduce a check in this shape: an assertion whose absence-of-subject case is `ok` is
# fail-open, which this suite bans. Bumps now reach the cluster through the catalog, and
# the row that matters — that four concurrent bumps all land — is the one directly above.

echo "== generator validation (each row is a fix that was previously untested) =="
# Every one of these corresponds to a shipped fix. An adversarial review found that nine
# such fixes could be reverted with the suite still fully green — an untested fix is
# indistinguishable from no fix, and this is the section that makes that false.
G=$(mktemp -d); cp -r scripts "$G/"; seed_generator_inputs "$G"; mkdir -p "$G/catalog" "$G/clusters/apps"
write_sandbox_instance "$G"
printf 'tags: {}\n' > "$G/catalog/tags.yaml"
gen() { # <label> <expect: refuse|accept> <projects yaml>
  printf 'projects:\n%s\n' "$3" > "$G/catalog/projects.yaml"
  out=$(cd "$G" && python3 scripts/catalog-sync.py 2>&1); rc=$?
  if [ "$2" = refuse ]; then
    [ $rc -ne 0 ] && ok "generator: $1" || bad "generator: $1 (accepted, should refuse)"
  else
    [ $rc -eq 0 ] && ok "generator: $1" || bad "generator: $1 (rc=$rc, $(echo "$out" | head -1))"
  fi
}
CMP='components: [{name: api, kind: web, image: x, port: 80, path: /}]'
gen "ordinary project accepted" accept \
  "  - {id: mine, name: M, hosted: self, environments: {prod: {}}, $CMP}"
gen "id with a trailing newline refused (re.match vs fullmatch)" refuse \
  "  - {id: \"evil\n\", name: E, hosted: self, environments: {prod: {}}, $CMP}"
gen "derived namespace over 63 chars refused" refuse \
  "  - {id: aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa, name: L, hosted: self, environments: {bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb: {enabled: true}}, $CMP}"
gen "one host claimed by prod and dev refused" refuse \
  "  - {id: mine, name: M, hosted: self, environments: {prod: {hostnames: [same.example.org]}, dev: {enabled: true, hostnames: [same.example.org]}}, $CMP}"
gen "external listed AFTER the self-hosted claimant still refused" refuse \
  "  - {id: mine, name: M, hosted: self, environments: {prod: {hostnames: [clash.example.org]}}, $CMP}
  - {id: ext, name: E, hosted: external, url: 'https://clash.example.org', environments: {prod: {hostnames: [clash.example.org]}}}"
gen "hostname that is not a DNS name refused" refuse \
  "  - {id: mine, name: M, hosted: self, environments: {prod: {hostnames: [bad_host.example.org]}}, $CMP}"
gen "self-hosted with no components refused" refuse \
  "  - {id: mine, name: M, hosted: self, environments: {prod: {}}}"
# A component may exist in only some environments — a replay server feeding a listener is
# dev-only, and it has to be in the listener's own namespace because the default-deny
# NetworkPolicy
# admits nothing else. Two ways that goes silently wrong, both refused:
gen "component naming an undeclared environment refused" refuse \
  "  - {id: mine, name: M, hosted: self, environments: {prod: {}, dev: {enabled: true}},
      components: [{name: api, kind: web, image: x, port: 80, path: /},
                   {name: tool, kind: web, image: y, port: 81, path: /t, environments: [devv]}]}"
gen "an environment left with no components refused" refuse \
  "  - {id: mine, name: M, hosted: self, environments: {prod: {}, dev: {enabled: true}},
      components: [{name: api, kind: web, image: x, port: 80, path: /, environments: [prod]}]}"
gen "an empty environments list refused" refuse \
  "  - {id: mine, name: M, hosted: self, environments: {prod: {}},
      components: [{name: api, kind: web, image: x, port: 80, path: /, environments: []}]}"
# And the property the feature exists for, asserted on the rendered output rather than on
# the exit code: the component is in dev and is NOT in prod. An accepted generation that
# rendered the dev-only tool into production would pass every check above.
printf 'projects:\n  - {id: mine, name: M, hosted: self, environments: {prod: {}, dev: {enabled: true}}, components: [{name: api, kind: web, image: x, port: 80, path: /}, {name: tool, kind: web, image: y, port: 81, path: /t, environments: [dev]}]}\n' > "$G/catalog/projects.yaml"
if (cd "$G" && python3 scripts/catalog-sync.py >/dev/null 2>&1); then
  grep -q 'name: tool' "$G/clusters/apps/mine-dev.yaml" \
    && ok "generator: a dev-only component renders in dev" \
    || bad "generator: the dev-only component is missing from the dev Application"
  grep -q 'name: tool' "$G/clusters/apps/mine-prod.yaml" \
    && bad "generator: the dev-only component leaked into PRODUCTION" \
    || ok "generator: a dev-only component does not render in prod"
  # Same reasoning as `repo`: a generator directive in a deployed manifest reads as
  # something the chart acts on, and the chart has never heard of it.
  grep -q 'environments' "$G/clusters/apps/mine-dev.yaml" \
    && bad "generator: the environments directive reached the rendered values" \
    || ok "generator: the environments directive is stripped from chart values"
else
  bad "generator: per-environment component fixture failed to generate"
fi
# Normalisation has to reach the rendered value, not just the comparison. This is the exact
# split that left the Ingress fixed and the hub card raw.
printf 'projects:\n  - {id: mine, name: M, hosted: self, showcase: true, environments: {prod: {hostnames: ["MiXeD.Example.ORG."]}}, %s}\n' "$CMP" > "$G/catalog/projects.yaml"
if (cd "$G" && python3 scripts/catalog-sync.py >/dev/null 2>&1); then
  # Match the fixture's own spelling only. A broad pattern matched "example-org" in the
  # repoURL and could never pass — a check that cannot go green is as useless as one that
  # cannot go red. This row was BOTH for a while, and review found it 2026-09-07: the
  # pattern still named the PREVIOUS fixture host, from before the fixture became
  # `MiXeD.Example.ORG.`, so it matched nothing; and `grep -c` on ONE file prints a bare
  # count, on which `awk -F: '{s+=$2}'` sums an empty field and yields 0 whatever the
  # count was. Two independent reasons the row could never go red. Fixed by greping the
  # raw fixture spelling across every rendered file — which is what the row claims — with
  # a recursive grep, whose `path:count` output is the shape the awk was written for.
  RAW=$(grep -r -c 'MiXeD\.Example\.ORG\.' "$G/clusters/apps/" 2>/dev/null | awk -F: '{s+=$NF} END{print s+0}')
  [ "$RAW" = "0" ] && ok "generator: hostnames normalised in every rendered file" \
    || bad "generator: un-normalised hostname still reaches a rendered file"
else
  bad "generator: normalisation fixture failed to generate"
fi
# Duplicate hosts across hostnames and customDomains must collapse, not render twice.
printf 'projects:\n  - {id: mine, name: M, hosted: self, environments: {prod: {hostnames: [dup.example.org, dup.example.org], customDomains: [{host: dup.example.org}]}}, %s}\n' "$CMP" > "$G/catalog/projects.yaml"
if (cd "$G" && python3 scripts/catalog-sync.py >/dev/null 2>&1); then
  N=$(grep -c 'dup\.example\.org' "$G/clusters/apps/mine-prod.yaml")
  [ "$N" -le 2 ] && ok "generator: repeated hostnames deduplicated (appears ${N}x)" \
    || bad "generator: repeated hostname rendered ${N}x — duplicate Ingress rules"
else bad "generator: dedup fixture failed to generate"; fi
rm -rf "$G"

echo "== the agent plane renders from the catalog (Phase D.2) =="
# An agent is a catalog entry and NOTHING else turns it into something that runs: kagent
# was dropped 2026-08-27, so the generator is the entire execution plane. These run on a
# fixture, not on the live catalog, so they hold for an instance that has declared no
# agent yet — the day someone declares one, the plumbing has already been proven.
#
# The property under test is not "a Deployment appeared". It is that the runtime comes up
# ABLE TO SERVE: services/agent-runtime/app.py answers 503 to every POST unless
# ORBIT_AGENT_TOKEN is present, and refuses to run a task without CLAUDE_CODE_OAUTH_TOKEN
# — so a green pod with either one unwired is an agent that exists and can do nothing.
A=$(mktemp -d); cp -r scripts "$A/"; seed_generator_inputs "$A"; mkdir -p "$A/catalog" "$A/clusters/apps" "$A/.claude"
write_sandbox_instance "$A"
cat >> "$A/instance/config.yaml" <<'EOF'
agentRuntime:
  image: ghcr.io/example-core/orbit-agent-runtime
  tag: main-0000000
intakeRuntime:
  image: ghcr.io/example-core/orbit-intake
  tag: main-0000000
EOF
printf 'tags: {}\n' > "$A/catalog/tags.yaml"
# The skills library the agent mounts. Its own fixture rather than the repo's, so a
# renamed core skill cannot make this section red for the wrong reason.
mkdir -p "$A/.claude/skills/fixture-skill"
printf -- '---\nname: fixture-skill\ndescription: a fixture\n---\n\nbody\n' \
  > "$A/.claude/skills/fixture-skill/SKILL.md"
AGENT_FIXTURE='  - name: hello-orbit
    prompt: You are an Orbit platform agent.
    skills: [fixture-skill]'
# A scheduled agent (Phase D.3). Same wiring as a deployment agent — bearer, subscription
# token, skills, OTel — but rendered as a CronJob with no Service and no external door.
AGENT_CRON_FIXTURE='  - name: nightly-audit
    prompt: You are a scheduled Orbit auditor.
    mode: cronjob
    schedule: "0 3 * * *"
    skills: [fixture-skill]
    class: batch
    timeoutSeconds: 600'
agen() { # <label> <expect: refuse|accept> <agents yaml>
  printf 'version: 3\nprojects: []\nagents:\n%s\n' "$3" > "$A/catalog/projects.yaml"
  out=$(cd "$A" && python3 scripts/catalog-sync.py 2>&1); rc=$?
  if [ "$2" = refuse ]; then
    [ $rc -ne 0 ] && ok "agents: $1" || bad "agents: $1 (accepted, should refuse)"
  else
    [ $rc -eq 0 ] && ok "agents: $1" || bad "agents: $1 (rc=$rc, $(echo "$out" | head -1))"
  fi
}
# Every row below is a way an agent goes wrong quietly. The closed key set is the one
# that matters most: an unknown key in a catalog entry is either a typo that renders
# nothing, or a VALUE — `token: hunter2` — that would be committed to git in plaintext
# inside the generated Application.
agen "a minimal agent entry renders" accept "$AGENT_FIXTURE"
agen "an inline credential key is refused (the schema is closed)" refuse \
  "  - {name: a, prompt: hi, token: hunter2}"
# Phase D.3: mode: cronjob is now RENDERED (a CronJob), no longer refused — but ONLY with
# a valid schedule. A schedule is the one field a scheduled agent cannot be guessed
# without, so its absence or malformation is a refusal, not a default.
agen "a cronjob agent with a valid schedule renders" accept "$AGENT_CRON_FIXTURE"
agen "a cronjob agent with NO schedule is refused (a CronJob that never runs)" refuse \
  "  - {name: a, prompt: hi, mode: cronjob}"
agen "a cronjob agent with an invalid schedule is refused" refuse \
  "  - {name: a, prompt: hi, mode: cronjob, schedule: every tuesday}"
agen "a four-field cron schedule is refused (the API server rejects it at apply)" refuse \
  "  - {name: a, prompt: hi, mode: cronjob, schedule: '0 3 * *'}"
# Manifest injection: a newline in the schedule must not smuggle a second line into the
# CronJob spec. fullmatch anchors both ends, so it is refused.
agen "a newline-injected schedule is refused (no manifest injection)" refuse \
  "  - {name: a, prompt: hi, mode: cronjob, schedule: \"0 3 * * *\\nevil * * * *\"}"
# Cron controls only mean something on a CronJob. On a deployment agent a schedule renders
# nothing and fires never — refused rather than silently dropped.
agen "a schedule on a deployment agent is refused (a silent no-op otherwise)" refuse \
  "  - {name: a, prompt: hi, schedule: '0 3 * * *'}"
agen "concurrencyPolicy: Allow is refused (token-holding runs must not pile up)" refuse \
  "  - {name: a, prompt: hi, mode: cronjob, schedule: '0 3 * * *', concurrencyPolicy: Allow}"
agen "an out-of-range backoffLimit is refused in the generator" refuse \
  "  - {name: a, prompt: hi, mode: cronjob, schedule: '0 3 * * *', backoffLimit: 99}"
agen "an unknown key on a cronjob agent is still refused (closed schema)" refuse \
  "  - {name: a, prompt: hi, mode: cronjob, schedule: '0 3 * * *', token: hunter2}"
agen "an unknown mode is refused" refuse "  - {name: a, prompt: hi, mode: daemonset}"
# --- triggers (Phase D.3, the intake). Each row is a way an event-driven agent goes wrong
# quietly: a trigger that renders nothing reports nothing, and the operator's only evidence
# that an agent is wired is that the generator accepted it.
agen "an alert trigger renders" accept \
  "  - {name: a, prompt: hi, triggers: [{type: alert, match: {alertname: OrbitSiteDown, severity: critical}}]}"
# A `github` trigger is GATED on the instance declaring the intake's public webhook door
# wired. The generator renders the ROUTE and never the DOOR — it emits no Ingress and no
# Certificate for the intake (asserted structurally further below) — so on a stock instance
# GitHub cannot reach /webhook/github at all: a LIVE route in front of a door that does not
# exist, the same anti-pattern the `logs` gate refuses.
GITHUB_FIXTURE="  - {name: a, prompt: hi, triggers: [{type: github, repo: acme/widgets, events: [push]}]}"
agen "a github trigger is REFUSED when the instance does not declare intakeIngress: wired" refuse \
  "$GITHUB_FIXTURE"
# Same trigger, same catalog, with the instance declaration present: it renders.
cp "$A/instance/config.yaml" "$A/instance/config.yaml.nointakeingress"
printf 'intakeIngress: wired\n' >> "$A/instance/config.yaml"
agen "the same github trigger renders once the instance declares intakeIngress: wired" accept \
  "$GITHUB_FIXTURE"
agen "an a2a trigger renders" accept "  - {name: a, prompt: hi, triggers: [{type: a2a}]}"
agen "an unknown trigger type is refused" refuse \
  "  - {name: a, prompt: hi, triggers: [{type: kafka}]}"
# An EMPTY matcher routes every alert on the platform to this agent, on the org's
# subscription. It is the single most expensive thing a typo here can do.
agen "an alert trigger with an EMPTY match is refused (it would route every alert)" refuse \
  "  - {name: a, prompt: hi, triggers: [{type: alert, match: {}}]}"
agen "an alert trigger with no match at all is refused" refuse \
  "  - {name: a, prompt: hi, triggers: [{type: alert}]}"
# A YAML-unquoted number or boolean becomes a value no label ever equals — a matcher that
# can never fire, and reports nothing.
agen "a non-string alert matcher value is refused (a matcher that can never fire)" refuse \
  "  - {name: a, prompt: hi, triggers: [{type: alert, match: {severity: 1}}]}"
agen "an unknown key inside a trigger is refused (the trigger schema is closed)" refuse \
  "  - {name: a, prompt: hi, triggers: [{type: a2a, token: hunter2}]}"
agen "an empty triggers list is refused (nothing would ever wake the agent)" refuse \
  "  - {name: a, prompt: hi, triggers: []}"
# Still under `intakeIngress: wired` from the gate block above, so these two refuse for
# their OWN reason rather than for the gate's — a refusal that fires for the wrong reason
# is a row that would stay green after the check it names is deleted.
agen "a github trigger with no events is refused even with the door declared (there is no wildcard)" refuse \
  "  - {name: a, prompt: hi, triggers: [{type: github, repo: acme/widgets, events: []}]}"
agen "a github trigger with a malformed repo is refused even with the door declared" refuse \
  "  - {name: a, prompt: hi, triggers: [{type: github, repo: 'not a repo', events: [push]}]}"
# The flag is a closed value, and it is read on EVERY generation — not only once a github
# trigger exists. `true`/`yes`/`soon` must not read as wired and must not read as absent.
cp "$A/instance/config.yaml.nointakeingress" "$A/instance/config.yaml"
printf 'intakeIngress: true\n' >> "$A/instance/config.yaml"
agen "an intakeIngress value other than 'wired' is refused (closed value)" refuse \
  "  - {name: a, prompt: hi, triggers: [{type: github, repo: acme/widgets, events: [push]}]}"
agen "a misspelt intakeIngress is refused with NO github trigger declared at all" refuse \
  "$AGENT_FIXTURE"
cp "$A/instance/config.yaml.nointakeingress" "$A/instance/config.yaml"
# metric/logs now render BOTH halves — the alerting rule AND the intake route — from one
# block. A full, valid trigger of each type is accepted; the rendered rule + route are
# asserted structurally in the dedicated section further below.
agen "a metric trigger renders (rule + route from one block)" accept \
  "  - {name: a, prompt: hi, triggers: [{type: metric, name: hot, expr: 'up == 0', for: 5m, summary: down}]}"
# A `logs` trigger is GATED on the instance declaring its Loki ruler wired. Without the
# flag the rule renders into a ruler that loads nothing (no alertmanager_url, and rule
# storage on S3 rather than the /rules the sidecar writes) — a LIVE intake route in front
# of a rule that can never fire, which is the anti-pattern that deferred `logs` at all.
LOGS_FIXTURE="  - {name: a, prompt: hi, triggers: [{type: logs, name: oom, expr: 'sum(count_over_time({app=\"x\"} |= \"OOM\" [5m])) > 0', for: 2m, summary: ooms}]}"
agen "a logs trigger is REFUSED when the instance does not declare lokiRuler: wired" refuse \
  "$LOGS_FIXTURE"
# Same trigger, same catalog, with the instance declaration present: it renders.
cp "$A/instance/config.yaml" "$A/instance/config.yaml.nolokiruler"
printf 'lokiRuler: wired\n' >> "$A/instance/config.yaml"
agen "the same logs trigger renders once the instance declares lokiRuler: wired" accept \
  "$LOGS_FIXTURE"
agen "a logs trigger with no expr is refused even with the ruler wired" refuse \
  "  - {name: a, prompt: hi, triggers: [{type: logs, name: oom, for: 2m, summary: s}]}"
# There is no local LogQL parser the way promtool is one for PromQL, so `logs` gets a
# structural check and the docs say so plainly. It catches what is unambiguously not
# LogQL — unbalanced delimiters, an unterminated string, no stream selector at all.
agen "a logs expr with unbalanced delimiters is refused" refuse \
  "  - {name: a, prompt: hi, triggers: [{type: logs, name: oom, expr: 'sum(count_over_time({app=\"x\"} |= \"OOM\" [5m)) > 0', for: 2m, summary: s}]}"
agen "a logs expr with no stream selector is refused (every LogQL query selects a stream)" refuse \
  "  - {name: a, prompt: hi, triggers: [{type: logs, name: oom, expr: '1 > 0', for: 2m, summary: s}]}"
# The flag is a closed value: `true`/`soon`/`yes` must not read as wired, and must not
# read as absent either — an instance that misspells it gets told, not ignored.
cp "$A/instance/config.yaml.nolokiruler" "$A/instance/config.yaml"
printf 'lokiRuler: true\n' >> "$A/instance/config.yaml"
agen "a lokiRuler value other than 'wired' is refused (closed value)" refuse \
  "  - {name: a, prompt: hi, triggers: [{type: metric, name: hot, expr: 'up == 0', for: 5m, summary: s}]}"
# ...and it is checked on EVERY generation, not only once a metric/logs trigger exists.
# Read on the trigger path, a misspelt declaration sat green for as long as nobody declared
# one — and surfaced at the exact moment the operator believed the ruler was already wired.
agen "a misspelt lokiRuler is refused with NO metric/logs trigger declared at all" refuse \
  "$AGENT_FIXTURE"
cp "$A/instance/config.yaml.nolokiruler" "$A/instance/config.yaml"
# A DISABLED agent renders nothing and is validated anyway: `enabled: false` is how an
# agent is parked mid-edit, and the moment it matters is the moment someone flips it back.
# Enabling must never be able to turn a green catalog red.
agen "a disabled agent's malformed trigger is still refused (enabling can't flip green->red)" refuse \
  "  - {name: a, prompt: hi, enabled: false, triggers: [{type: metric, name: hot, expr: 'up == 0', for: 30s, summary: s}]}"
agen "a disabled agent still CLAIMS its derived alertnames (an enabled one may not take them)" refuse \
  "  - {name: foo-bar, prompt: hi, enabled: false, triggers: [{type: metric, name: baz, expr: 'up == 0', for: 5m, summary: s}]}
  - {name: foo, prompt: hi, triggers: [{type: metric, name: bar-baz, expr: 'up == 1', for: 5m, summary: t}]}"
agen "a disabled agent with a VALID trigger still renders nothing and stays green" accept \
  "  - {name: a, prompt: hi, enabled: false, triggers: [{type: metric, name: hot, expr: 'up == 0', for: 5m, summary: s}]}
  - {name: b, prompt: hi}"
printf 'version: 3\nprojects: []\nagents:\n%s\n' \
  "  - {name: a, prompt: hi, enabled: false, triggers: [{type: metric, name: hot, expr: 'up == 0', for: 5m, summary: s}]}
  - {name: b, prompt: hi}" > "$A/catalog/projects.yaml"
if (cd "$A" && python3 scripts/catalog-sync.py >/dev/null 2>&1); then
  grep -q 'OrbitAgentAHot' "$A/clusters/apps/orbit-agents-prod.yaml" \
    && bad "agents(disabled): a disabled agent's metric trigger still RENDERED a rule" \
    || ok "agents(disabled): validated but not rendered — no rule, no route, no alertname"
else
  bad "agents(disabled): the disabled-agent fixture failed to generate"
fi
cp "$A/instance/config.yaml.nolokiruler" "$A/instance/config.yaml"
# Every field a metric/logs trigger cannot render safely without is a refusal, not a
# default — a rule that renders wrong reports nothing until it silently never fires.
agen "a metric trigger with no name is refused (the alertname has no tail)" refuse \
  "  - {name: a, prompt: hi, triggers: [{type: metric, expr: 'up == 0', for: 5m, summary: s}]}"
agen "a metric trigger with no expr is refused (a rule with no condition)" refuse \
  "  - {name: a, prompt: hi, triggers: [{type: metric, name: hot, for: 5m, summary: s}]}"
agen "a metric trigger with no for is refused (pages on a single scrape blip)" refuse \
  "  - {name: a, prompt: hi, triggers: [{type: metric, name: hot, expr: 'up == 0', summary: s}]}"
agen "a metric trigger with a bad for duration is refused" refuse \
  "  - {name: a, prompt: hi, triggers: [{type: metric, name: hot, expr: 'up == 0', for: soon, summary: s}]}"
# The `for:` pattern is the PrometheusRule CRD's OWN: ordered units, each at most once.
# A looser one renders green, ships, and is then rejected by the CRD's admission webhook —
# CI green, cluster red, the Argo app stuck SyncFailed with nothing else reporting it.
agen "a for duration with repeated/unordered units (30m1h) is refused — the CRD rejects it" refuse \
  "  - {name: a, prompt: hi, triggers: [{type: metric, name: hot, expr: 'up == 0', for: 30m1h, summary: s}]}"
agen "a for duration repeating one unit (5m5m5m) is refused — the CRD rejects it" refuse \
  "  - {name: a, prompt: hi, triggers: [{type: metric, name: hot, expr: 'up == 0', for: 5m5m5m, summary: s}]}"
agen "a plain CRD-valid for (5m) is accepted" accept \
  "  - {name: a, prompt: hi, triggers: [{type: metric, name: hot, expr: 'up == 0', for: 5m, summary: s}]}"
agen "an ordered multi-unit for (1h30m) is accepted" accept \
  "  - {name: a, prompt: hi, triggers: [{type: metric, name: hot, expr: 'up == 0', for: 1h30m, summary: s}]}"
# The floor: under a minute, a flapping condition wakes the agent — and spends the org's
# subscription — once per evaluation. `0s` is the spelled-out opt-out (AGENT-PLANE.md).
agen "a for under the 1m floor is refused (a flap would run the agent per evaluation)" refuse \
  "  - {name: a, prompt: hi, triggers: [{type: metric, name: hot, expr: 'up == 0', for: 30s, summary: s}]}"
agen "the explicit 0s opt-out is accepted (a decision, not a typo)" accept \
  "  - {name: a, prompt: hi, triggers: [{type: metric, name: hot, expr: 'up == 0', for: 0s, summary: s}]}"
agen "a metric trigger with no summary is refused (nothing for the agent to read)" refuse \
  "  - {name: a, prompt: hi, triggers: [{type: metric, name: hot, expr: 'up == 0', for: 5m}]}"
agen "a metric trigger with an unknown severity is refused" refuse \
  "  - {name: a, prompt: hi, triggers: [{type: metric, name: hot, expr: 'up == 0', for: 5m, summary: s, severity: apocalyptic}]}"
# A catalog label may not shadow one the generator owns (alertname is the router's whole
# matcher; severity has its own field) — it would either be a no-op or fight the route.
agen "a metric label shadowing the reserved alertname is refused" refuse \
  "  - {name: a, prompt: hi, triggers: [{type: metric, name: hot, expr: 'up == 0', for: 5m, summary: s, labels: {alertname: x}}]}"
agen "a metric label in Prometheus's own __ namespace is refused" refuse \
  "  - {name: a, prompt: hi, triggers: [{type: metric, name: hot, expr: 'up == 0', for: 5m, summary: s, labels: {__name__: x}}]}"
# Prometheus expands Go templates in a firing rule's annotations AND labels, and the
# template function `query` executes PromQL against the server. The summary is a human
# line, not a program: catalog text carrying `{{`/`}}` is refused rather than escaped.
agen "a summary carrying Prometheus template syntax is refused ({{ query }} runs PromQL)" refuse \
  "  - {name: a, prompt: hi, triggers: [{type: metric, name: hot, expr: 'up == 0', for: 5m, summary: '{{ query \"up\" }}'}]}"
agen "a label VALUE carrying template syntax is refused too" refuse \
  "  - {name: a, prompt: hi, triggers: [{type: metric, name: hot, expr: 'up == 0', for: 5m, summary: s, labels: {team: '{{ .Labels.x }}'}}]}"
agen "an optional description renders (the Telegram template prints it)" accept \
  "  - {name: a, prompt: hi, triggers: [{type: metric, name: hot, expr: 'up == 0', for: 5m, summary: s, description: 'the fuller line'}]}"
agen "a description carrying template syntax is refused" refuse \
  "  - {name: a, prompt: hi, triggers: [{type: metric, name: hot, expr: 'up == 0', for: 5m, summary: s, description: '{{ query \"up\" }}'}]}"
agen "an unknown key on a metric trigger is refused (closed schema)" refuse \
  "  - {name: a, prompt: hi, triggers: [{type: metric, name: hot, expr: 'up == 0', for: 5m, summary: s, query: x}]}"
# Uniqueness is enforced on the DERIVED alertname, plane-wide — not on the raw name. The
# derivation drops `-`/`_`, so a raw-name comparison passes pairs that compile to ONE
# alertname; the intake routes on the alertname alone, so such an alert dispatches to BOTH
# agents, each with the other's prompt and bearer.
agen "two metric triggers with the same name on one agent are refused" refuse \
  "  - {name: a, prompt: hi, triggers: [
        {type: metric, name: hot, expr: 'up == 0', for: 5m, summary: s},
        {type: metric, name: hot, expr: 'up == 1', for: 5m, summary: t}]}"
agen "two AGENTS whose triggers derive one alertname are refused (foo-bar/baz vs foo/bar-baz)" refuse \
  "  - {name: foo-bar, prompt: hi, triggers: [{type: metric, name: baz, expr: 'up == 0', for: 5m, summary: s}]}
  - {name: foo, prompt: hi, triggers: [{type: metric, name: bar-baz, expr: 'up == 1', for: 5m, summary: t}]}"
agen "dash-normalised names that derive one alertname are refused (hot-x vs hot--x)" refuse \
  "  - {name: a, prompt: hi, triggers: [
        {type: metric, name: hot-x, expr: 'up == 0', for: 5m, summary: s},
        {type: metric, name: hot--x, expr: 'up == 1', for: 5m, summary: t}]}"
agen "digit-leading names that derive one alertname are refused (err-5 vs err5)" refuse \
  "  - {name: a, prompt: hi, triggers: [
        {type: metric, name: err-5, expr: 'up == 0', for: 5m, summary: s},
        {type: metric, name: err5, expr: 'up == 1', for: 5m, summary: t}]}"
# The derived-name space is reserved: a hand-written matcher inside it either shadows a
# generated route or squats a name a future trigger would derive.
agen "an alert trigger matching a name in the OrbitAgent* space is refused" refuse \
  "  - {name: a, prompt: hi, triggers: [{type: alert, match: {alertname: OrbitAgentAHot}}]}"
# ...and the same squat through a label OTHER than the alertname. A generated alert carries
# severity, namespace, orbit_agent, orbit_trigger and the trigger's catalog labels, so a
# match that never names an alertname is evaluated against the plane's own alerts too:
# `{severity: critical}` catches EVERY critical metric trigger and runs a second agent on
# it, with the wrong prompt and the wrong bearer. It is also unreachable through the
# shipped Alertmanager route, which carries OrbitAgent* and nothing else.
agen "an alert trigger with a severity-only match is refused (it catches generated alerts)" refuse \
  "  - {name: a, prompt: hi, triggers: [{type: alert, match: {severity: critical}}]}"
agen "an alert trigger with no alertname at all is refused (nothing routes it either)" refuse \
  "  - {name: a, prompt: hi, triggers: [{type: alert, match: {namespace: prod, status: firing}}]}"
agen "an alert trigger matching orbit_agent is refused (only generated alerts carry it)" refuse \
  "  - {name: a, prompt: hi, triggers: [{type: alert, match: {alertname: OrbitSiteDown, orbit_agent: b}}]}"
agen "an alert trigger matching orbit_trigger is refused (only generated alerts carry it)" refuse \
  "  - {name: a, prompt: hi, triggers: [{type: alert, match: {alertname: OrbitSiteDown, orbit_trigger: t}}]}"
# Alert matcher VALUES get the same refusal every other catalog-supplied string gets. The
# value is compiled into the intake's routing ConfigMap and echoed in its logs; `{{` there
# is the one syntax on this platform that is a program somewhere downstream.
agen "a templating marker in an alert matcher value is refused" refuse \
  "  - {name: a, prompt: hi, triggers: [{type: alert, match: {alertname: '{{ query \\\"up\\\" }}'}}]}"
agen "a cron trigger is refused — the cron trigger IS mode: cronjob + schedule" refuse \
  "  - {name: a, prompt: hi, triggers: [{type: cron, schedule: '0 3 * * *'}]}"
agen "an unknown dispatch value is refused" refuse \
  "  - {name: a, prompt: hi, dispatch: carrier, triggers: [{type: a2a}]}"
# dispatch: service on a cronjob agent renders no Service at all: every event would 502
# forever with nothing anywhere reporting why.
agen "dispatch: service on a cronjob agent is refused (there is no Service to call)" refuse \
  "  - {name: a, prompt: hi, mode: cronjob, schedule: '0 3 * * *', dispatch: service,
      triggers: [{type: a2a}]}"
agen "dispatch without triggers is refused (nothing would ever wake it)" refuse \
  "  - {name: a, prompt: hi, dispatch: job}"
# The intake renders as a component of this same project, so an agent of that name would
# collide with it and one of the two would silently vanish.
agen "an agent named 'intake' is refused (it collides with the router)" refuse \
  "  - {name: intake, prompt: hi}"
# The intake image is an INSTANCE pin like the runtime image, never a catalog choice: the
# router holds every warm agent's bearer and can create Jobs beside the subscription token.
agen "a per-agent intake image override is refused (unknown key)" refuse \
  "  - {name: a, prompt: hi, intakeImage: 'evil.net/x', triggers: [{type: a2a}]}"
agen "an agent with no prompt is refused" refuse "  - {name: a}"
agen "a name that is not a DNS label is refused" refuse "  - {name: 'A_bad', prompt: hi}"
agen "two agents with one name are refused" refuse \
  "  - {name: a, prompt: hi}
  - {name: a, prompt: ho}"
agen "a skill with no SKILL.md on disk is refused (an inert mount)" refuse \
  "  - {name: a, prompt: hi, skills: [absent]}"
agen "a skill name that escapes the skills directory is refused" refuse \
  "  - {name: a, prompt: hi, skills: ['../../etc']}"
# C2: there is NO per-agent image/tag. It was dropped because a per-agent image chooses
# the registry the mounted subscription token is sent to — one `image: evil.net/x` and a
# single task exfiltrates. Both must now be REFUSED as unknown keys.
agen "a per-agent image override is refused (would redirect the token's registry)" refuse \
  "  - {name: a, prompt: hi, image: 'evil.net/x/agent'}"
agen "a per-agent tag override is refused" refuse \
  "  - {name: a, prompt: hi, tag: 'evil'}"
agen "an out-of-range budget cap is refused" refuse "  - {name: a, prompt: hi, maxTurns: 0}"
agen "uid 0 is refused" refuse "  - {name: a, prompt: hi, uid: 0}"
agen "an unknown resource class is refused" refuse "  - {name: a, prompt: hi, class: urgent}"
# L1: resource quantities are validated in the generator, not only downstream in the
# chart schema — a bad quantity otherwise yields a green commit and a broken Application.
agen "a bad memory quantity is refused in the generator" refuse \
  "  - {name: a, prompt: hi, memory: 512MB}"
agen "a bad cpu quantity is refused in the generator" refuse \
  "  - {name: a, prompt: hi, cpu: fast}"
# A disabled agent must leave NOTHING behind — not a namespace, not a quota, not an
# Application Argo would report Healthy over an empty workload list.
printf 'version: 3\nprojects: []\nagents:\n  - {name: a, prompt: hi, enabled: false}\n' > "$A/catalog/projects.yaml"
if (cd "$A" && python3 scripts/catalog-sync.py >/dev/null 2>&1); then
  [ -f "$A/clusters/apps/orbit-agents-prod.yaml" ] \
    && bad "agents: a disabled agent still rendered an Application" \
    || ok "agents: a disabled agent renders nothing at all"
else bad "agents: the disabled fixture failed to generate"; fi
# The plane's project id is reserved. A tenant claiming it would have its manifests
# overwritten by whichever of the two the generator wrote last.
printf 'version: 3\nprojects:\n  - {id: orbit-agents, name: X, hosted: self, environments: {prod: {}}, components: [{name: a, kind: web, image: x, port: 80, path: /}]}\n' > "$A/catalog/projects.yaml"
(cd "$A" && python3 scripts/catalog-sync.py >/dev/null 2>&1) \
  && bad "agents: a project may still claim the reserved orbit-agents id" \
  || ok "agents: the orbit-agents project id is reserved"

# --- refusals that protect the crown jewels (each a reproduced review finding) --------
proj() { # <label> <expect> <projects yaml> — a catalog with NO agents, testing a tenant
  printf 'version: 3\nprojects:\n%s\n' "$3" > "$A/catalog/projects.yaml"
  out=$(cd "$A" && python3 scripts/catalog-sync.py 2>&1); rc=$?
  if [ "$2" = refuse ]; then
    [ $rc -ne 0 ] && ok "agents: $1" || bad "agents: $1 (accepted, should refuse)"
  else
    [ $rc -eq 0 ] && ok "agents: $1" || bad "agents: $1 (rc=$rc, $(echo "$out" | head -1))"
  fi
}
TCMP='components: [{name: c, kind: web, image: x, port: 80, path: /}]'
# C3: a tenant that names a plane credential key would read it straight into its own
# namespace. Refused at render, which — because every Secret reference comes through
# the generator — is a real control, not advice.
proj "a tenant naming CLAUDE_CODE_OAUTH_TOKEN as a remote key is refused" refuse \
  "  - {id: t, name: T, hosted: self, environments: {prod: {}}, $TCMP, secrets: [{name: t-app, keys: [CLAUDE_CODE_OAUTH_TOKEN]}]}"
proj "a tenant naming an AGENT_*_TOKEN bearer key is refused" refuse \
  "  - {id: t, name: T, hosted: self, environments: {prod: {}}, $TCMP, secrets: [{name: t-app, keys: [AGENT_HELLO_ORBIT_TOKEN]}]}"
proj "a tenant naming the reserved Secret orbit-claude-code is refused" refuse \
  "  - {id: t, name: T, hosted: self, environments: {prod: {}}, $TCMP, secrets: [{name: orbit-claude-code, keys: [X]}]}"
proj "a tenant declaring an ordinary secret still renders" accept \
  "  - {id: t, name: T, hosted: self, environments: {prod: {}}, $TCMP, secrets: [{name: t-app, keys: [API_TOKEN]}]}"
# Tightening (review LOW 2): the reserved match is case-insensitive, so a lowercase or
# mixed-case variant of the crown-jewel key is refused too — refusing more never loosens.
proj "a tenant naming a lowercase claude_code_oauth_token key is refused" refuse \
  "  - {id: t, name: T, hosted: self, environments: {prod: {}}, $TCMP, secrets: [{name: t-app, keys: [claude_code_oauth_token]}]}"
proj "a tenant naming a mixed-case reserved Secret is refused" refuse \
  "  - {id: t, name: T, hosted: self, environments: {prod: {}}, $TCMP, secrets: [{name: Orbit-Claude-Code, keys: [X]}]}"
# Tightening (review LOW 3): a non-string entry in `keys` (a map) must be refused by the
# reserved check itself, not slip through str(dict) to be caught only by the chart schema.
proj "a map-valued entry in a secret's keys is refused by the generator" refuse \
  "  - {id: t, name: T, hosted: self, environments: {prod: {}}, $TCMP, secrets: [{name: t-app, keys: [{CLAUDE_CODE_OAUTH_TOKEN: x}]}]}"
# C1: `_extra` was the generator's internal arbitrary-manifest channel. A catalog project
# naming it could smuggle a ClusterRoleBinding into a directory Argo applies with
# cluster-wide rights. It is refused as a non-catalog field, and the channel now lives in
# a local dict main() keys that no catalog entry can address.
proj "a project declaring _extra is refused (arbitrary-manifest injection)" refuse \
  "  - {id: t, name: T, hosted: self, environments: {prod: {}}, $TCMP, _extra: {prod: [{apiVersion: rbac.authorization.k8s.io/v1, kind: ClusterRoleBinding, metadata: {name: pwn}}]}}"
# L3: `dashboard-` is a reserved filename prefix; a project id starting with it collides
# with another project's generated dashboard file and one Application silently vanishes.
proj "a project id starting with dashboard- is refused (filename collision)" refuse \
  "  - {id: dashboard-x, name: T, hosted: self, environments: {prod: {}}, $TCMP}"

# C1 backstop: the kind-allowlist over everything written to clusters/apps/. Unit-tested
# directly because the `_extra` path that would exercise it end-to-end is now closed — the
# guard is the belt under those braces, and must reject a disallowed kind AND accept an
# allowlisted one (so the test cannot pass by the module simply failing to import). Run
# in the sandbox, where an instance/config.yaml exists for the module's import.
( cd "$A" && python3 - ) <<'PY' && ok "agents: the writable-kind guard rejects a non-allowlisted kind, accepts an allowed one" \
  || bad "agents: the writable-kind guard is broken (see message)"
import importlib.util, sys
spec = importlib.util.spec_from_file_location("cs", "scripts/catalog-sync.py")
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)   # a failure here is a real failure, not a skip
# It must accept an Application (allowlisted) without exiting...
m.guard_writable([{"kind": "Application", "metadata": {"name": "ok"}}], "probe")
# ...and refuse a ClusterRoleBinding.
try:
    m.guard_writable([{"kind": "ClusterRoleBinding", "metadata": {"name": "pwn"}}], "probe")
except SystemExit as e:
    sys.exit(0 if e.code not in (0, None) else 1)
sys.stderr.write("guard_writable returned without refusing a ClusterRoleBinding\n")
sys.exit(1)
PY

# --- and now the rendered output, through the real chart -----------------------
# `helm template` over the values the generator actually emitted: what Argo will apply.
printf 'version: 3\nprojects: []\nagents:\n%s\n' "$AGENT_FIXTURE" > "$A/catalog/projects.yaml"
if (cd "$A" && python3 scripts/catalog-sync.py >/dev/null 2>&1) \
   && python3 -c "
import sys, yaml
app = [d for d in yaml.safe_load_all(open(sys.argv[1])) if d][0]
open(sys.argv[2], 'w').write(yaml.safe_dump(app['spec']['source']['helm']['valuesObject']))
" "$A/clusters/apps/orbit-agents-prod.yaml" "$A/values.yaml" 2>/dev/null && helm template a charts/service -f "$A/values.yaml" > "$A/out.yaml" 2>"$A/err"; then
  cat > "$A/check.py" <<'PY'
# One line per assertion, so a broken structure names itself instead of collapsing the
# whole section into a single red. Prints nothing at all only if it died — which the
# caller treats as a failure, never as a pass.
import sys, yaml
rendered = [d for d in yaml.safe_load_all(open(sys.argv[1])) if d]
app_docs = [d for d in yaml.safe_load_all(open(sys.argv[2])) if d]
out = []
def check(label, cond):
    out.append(("ok " if cond else "FAIL ") + label)
by = {}
for d in rendered:
    by.setdefault(d["kind"], {})[d["metadata"]["name"]] = d
dep = by.get("Deployment", {}).get("orbit-agents-hello-orbit")
svc = by.get("Service", {}).get("orbit-agents-hello-orbit")
check("the catalog entry renders a Deployment", dep is not None)
check("the agent gets a Service its trigger can address", svc is not None)
# No public door. An agent reachable from the internet is one bearer guess away from
# spending the org's subscription.
check("no Ingress and no Certificate are rendered for an agent",
      not by.get("Ingress") and not by.get("Certificate"))
if dep:
    spec = dep["spec"]["template"]["spec"]
    c = spec["containers"][0]
    env = {e["name"]: e for e in c.get("env") or []}
    check("the workload runs the runtime image from the instance's agentRuntime pin",
          c["image"] == "ghcr.io/example-core/orbit-agent-runtime:main-0000000")
    # THE bearer. app.py refuses every POST with 503 unless this is set, so an agent
    # without it is deployed and inert.
    tok = env.get("ORBIT_AGENT_TOKEN") or {}
    check("ORBIT_AGENT_TOKEN is mounted from a Secret",
          bool(tok.get("valueFrom", {}).get("secretKeyRef")))
    check("ORBIT_AGENT_TOKEN is a pointer, never a literal in git", "value" not in tok)
    check("the bearer Secret is per agent, not one key for the whole plane",
          tok.get("valueFrom", {}).get("secretKeyRef", {}).get("name")
          == "agent-hello-orbit-token")
    # Model auth: subscription setup-token only (AGENT-PLANE.md). An API key is refused
    # by the runtime itself; what the generator owes is the right variable, from a Secret.
    mod = env.get("CLAUDE_CODE_OAUTH_TOKEN") or {}
    check("CLAUDE_CODE_OAUTH_TOKEN is mounted from a Secret",
          bool(mod.get("valueFrom", {}).get("secretKeyRef")))
    check("CLAUDE_CODE_OAUTH_TOKEN is a pointer, never a literal in git",
          "value" not in mod)
    check("no credential-routing env var is set on an agent pod",
          not ({"ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_BASE_URL",
                "CLAUDE_CODE_USE_BEDROCK", "CLAUDE_CODE_USE_VERTEX"} & set(env)))
    # Identity is set by the platform, never by the app (OBSERVABILITY.md Decision 5),
    # and the runtime emits its own OTLP — with no endpoint it starts, serves, and
    # traces nothing, which is exactly the failure D.2's DONE-WHEN is written against.
    check("the platform sets the agent's OTel service identity",
          env.get("OTEL_SERVICE_NAME", {}).get("value") == "orbit-agents-hello-orbit")
    check("the agent has an OTLP endpoint to emit its run spans to",
          env.get("OTEL_EXPORTER_OTLP_ENDPOINT", {}).get("value", "").startswith("http"))
    check("the budget brakes reach the pod",
          {"ORBIT_AGENT_MAX_TURNS", "ORBIT_AGENT_TIMEOUT_S",
           "ORBIT_AGENT_MAX_CONCURRENCY"} <= set(env))
    check("the model is pinned on the pod, not left to the image default",
          env.get("ORBIT_AGENT_MODEL", {}).get("value") == "claude-sonnet-4-6")
    # The image's own USER is 10001 and the SDK writes its session state under $HOME.
    # With the chart's default 65532 the pod starts and the first task dies on a
    # permission error inside ~/.claude, which reads as a model failure.
    sec = spec.get("securityContext") or {}
    check("the pod runs as the runtime image's own uid",
          sec.get("runAsUser") == 10001 and sec.get("runAsNonRoot") is True)
    # Skills: the runtime discovers <skill>/SKILL.md and nothing else. A flat mount
    # projects one file per key, lists no skill, and reports no error.
    vols = {v["name"]: v for v in spec.get("volumes") or []}
    items = (vols.get("content", {}).get("configMap", {}) or {}).get("items") or []
    check("the skills ConfigMap is projected as <skill>/SKILL.md",
          any(i.get("path") == "fixture-skill/SKILL.md" for i in items))
    mounts = {m["name"]: m for m in c.get("volumeMounts") or []}
    check("skills are mounted read-only at the path the runtime reads",
          mounts.get("content", {}).get("mountPath") == "/home/orbit/.claude/skills"
          and mounts.get("content", {}).get("readOnly") is True)
# The skills ConfigMap travels with the Application, or the mount references nothing and
# the pod never starts.
cms = [d for d in app_docs if d.get("kind") == "ConfigMap"]
check("the skills ConfigMap ships alongside the Application",
      any(d["metadata"]["name"] == "orbit-agents-hello-orbit-skills"
          and "fixture-skill" in (d.get("data") or {}) for d in cms))
print("\n".join(out))
PY
  python3 "$A/check.py" "$A/out.yaml" "$A/clusters/apps/orbit-agents-prod.yaml" > "$A/res" 2>&1
  if [ -s "$A/res" ] && grep -q '^ok \|^FAIL ' "$A/res"; then
    while IFS= read -r line; do
      case "$line" in
        "ok "*)   ok "agents: ${line#ok }" ;;
        "FAIL "*) bad "agents: ${line#FAIL }" ;;
      esac
    done < "$A/res"
  else
    # Fail CLOSED. An empty result must never read as "nothing was wrong" — that
    # conflation is what hid the instance-string check for its entire life.
    bad "agents: the rendered-output checks did not run: $(head -3 "$A/res" | tr '\n' ' ')"
  fi
  # The generated Application is committed in plaintext, and an agent's ConfigMap carries
  # whole skill documents into it. Run the real credential scanner over exactly that,
  # AND prove the scanner now knows the plane's own credential shape: an sk-ant token
  # dropped into a skill must turn it red (M2). Copy the fixture skill and poison it.
  (cd "$A" && python3 scripts/no-credentials.py >/dev/null 2>&1) \
    && ok "agents: no credentials in the generated agent manifests" \
    || bad "agents: the credential scanner found something in the generated agent manifests"
  mkdir -p "$A/.claude/skills/poison-skill"
  printf -- '---\nname: poison-skill\ndescription: x\n---\n\nexample token sk-ant-oat01-%s\n' \
    "$(printf 'A%.0s' $(seq 1 30))" > "$A/.claude/skills/poison-skill/SKILL.md"
  printf 'version: 3\nprojects: []\nagents:\n  - {name: hello-orbit, prompt: hi, skills: [poison-skill]}\n' > "$A/catalog/projects.yaml"
  if (cd "$A" && python3 scripts/catalog-sync.py >/dev/null 2>&1); then
    (cd "$A" && python3 scripts/no-credentials.py >/dev/null 2>&1) \
      && bad "agents: an sk-ant token in a mounted skill passed the scanner (M2 shape missing)" \
      || ok "agents: an sk-ant token in a mounted skill is caught by the scanner"
  else
    bad "agents: the poisoned-skill fixture failed to generate"
  fi

  # M3: the agent plane's NetworkPolicy is NARROWER than a tenant's — it admits ONLY its
  # own pods, because these pods hold the subscription token and the broad tenant admit
  # list would let a Grafana editor reach an agent through the datasource proxy.
  printf 'version: 3\nprojects: []\nagents:\n%s\n' "$AGENT_FIXTURE" > "$A/catalog/projects.yaml"
  (cd "$A" && python3 scripts/catalog-sync.py >/dev/null 2>&1)
  python3 - "$A/clusters/apps/namespaces.yaml" <<'PY' \
    && ok "agents: the plane's NetworkPolicy ingress admits ONLY its own pods" \
    || bad "agents: the plane's NetworkPolicy admits a foreign namespace (token exposed to it)"
import sys, yaml
np = [d for d in yaml.safe_load_all(open(sys.argv[1])) if d
      and d.get("kind") == "NetworkPolicy"
      and d["metadata"]["namespace"] == "orbit-agents-prod"]
if not np:
    print("no NetworkPolicy for orbit-agents-prod", file=sys.stderr); sys.exit(1)
froms = np[0]["spec"]["ingress"][0]["from"]
# Exactly one source, and it is podSelector:{} (own pods) — no namespaceSelector at all.
ok = (len(froms) == 1 and froms[0] == {"podSelector": {}})
sys.exit(0 if ok else 1)
PY
else
  bad "agents: the fixture agent did not render through the chart ($(head -2 "$A/err" 2>/dev/null | tr '\n' ' '))"
fi

# --- a SCHEDULED agent, through the real chart (Phase D.3) ----------------------
# A cronjob agent must render as a CronJob and NOT a Deployment, get NO Service (a
# scheduled agent has no external caller and serves no port), keep the SAME crown-jewel
# wiring a deployment agent gets — its own bearer, the subscription token, skills, OTel —
# and carry the cron controls that make a batch run safe: a bounded schedule, Forbid
# concurrency, restartPolicy Never, a retry budget, and a wall-clock deadline. Each is a
# way a scheduled agent goes wrong quietly, so each is its own assertion.
printf 'version: 3\nprojects: []\nagents:\n%s\n' "$AGENT_CRON_FIXTURE" > "$A/catalog/projects.yaml"
if (cd "$A" && python3 scripts/catalog-sync.py >/dev/null 2>&1) \
   && python3 -c "
import sys, yaml
app = [d for d in yaml.safe_load_all(open(sys.argv[1])) if d][0]
open(sys.argv[2], 'w').write(yaml.safe_dump(app['spec']['source']['helm']['valuesObject']))
" "$A/clusters/apps/orbit-agents-prod.yaml" "$A/cron-values.yaml" 2>/dev/null && helm template a charts/service -f "$A/cron-values.yaml" > "$A/cron-out.yaml" 2>"$A/cron-err"; then
  cat > "$A/cron-check.py" <<'PY'
import sys, yaml
rendered = [d for d in yaml.safe_load_all(open(sys.argv[1])) if d]
out = []
def check(label, cond):
    out.append(("ok " if cond else "FAIL ") + label)
by = {}
for d in rendered:
    by.setdefault(d["kind"], {})[d["metadata"]["name"]] = d
cron = by.get("CronJob", {}).get("orbit-agents-nightly-audit")
# THE property that separates D.3 from D.2: a scheduled agent is a CronJob, not a
# long-running Deployment. Rendering the wrong one changes both cost and when it runs.
check("a cronjob agent renders a CronJob", cron is not None)
check("a cronjob agent renders NO Deployment", not by.get("Deployment"))
# No Service: a scheduled agent serves no port. A Service with no pods behind it is the
# quiet failure the chart's cron path exists to avoid.
check("a cronjob agent gets NO Service", not by.get("Service"))
check("a cronjob agent gets no Ingress and no Certificate",
      not by.get("Ingress") and not by.get("Certificate"))
if cron:
    spec = cron["spec"]
    check("the schedule is present and is the declared expression",
          spec.get("schedule") == "0 3 * * *")
    # Forbid, never Allow: overlapping runs each hold the subscription token.
    check("concurrencyPolicy is Forbid", spec.get("concurrencyPolicy") == "Forbid")
    check("a starting deadline is set", isinstance(spec.get("startingDeadlineSeconds"), int))
    check("a successful/failed history limit is set",
          isinstance(spec.get("successfulJobsHistoryLimit"), int)
          and isinstance(spec.get("failedJobsHistoryLimit"), int))
    jspec = (spec.get("jobTemplate") or {}).get("spec") or {}
    check("a backoff limit bounds retries", isinstance(jspec.get("backoffLimit"), int))
    # The wall-clock backstop UNDER app.py's in-process timeout: 600s task + 120s grace.
    check("activeDeadlineSeconds is tied to the agent's timeout (600+grace)",
          jspec.get("activeDeadlineSeconds") == 720)
    pspec = (jspec.get("template") or {}).get("spec") or {}
    # Never: a failed run is a fresh Job attempt bounded by backoffLimit, not a hot-loop
    # on the same pod that would re-run a partial task and re-spend the subscription.
    check("restartPolicy is Never", pspec.get("restartPolicy") == "Never")
    c = (pspec.get("containers") or [{}])[0]
    env = {e["name"]: e for e in c.get("env") or []}
    check("the cron runs the runtime image from the instance's agentRuntime pin",
          c.get("image") == "ghcr.io/example-core/orbit-agent-runtime:main-0000000")
    # The one-shot switch: the scheduled run performs one task and exits, rather than
    # starting the HTTP server (which would never complete the Job).
    check("ORBIT_AGENT_ONESHOT is set so the run is one-shot, not a served port",
          env.get("ORBIT_AGENT_ONESHOT", {}).get("value") == "1")
    # A cron pod serves nothing: no container port and no probes.
    check("the cron container exposes no port", not c.get("ports"))
    check("the cron container has no readiness/liveness probe",
          not c.get("readinessProbe") and not c.get("livenessProbe"))
    # SAME crown-jewel wiring as a deployment agent — reused code path, so identical
    # invariants: the bearer and subscription token are Secret pointers, never literals.
    tok = env.get("ORBIT_AGENT_TOKEN") or {}
    check("ORBIT_AGENT_TOKEN is mounted from the per-agent Secret",
          tok.get("valueFrom", {}).get("secretKeyRef", {}).get("name")
          == "agent-nightly-audit-token")
    check("ORBIT_AGENT_TOKEN is a pointer, never a literal in git", "value" not in tok)
    mod = env.get("CLAUDE_CODE_OAUTH_TOKEN") or {}
    check("CLAUDE_CODE_OAUTH_TOKEN is mounted from a Secret",
          bool(mod.get("valueFrom", {}).get("secretKeyRef")))
    check("CLAUDE_CODE_OAUTH_TOKEN is a pointer, never a literal in git",
          "value" not in mod)
    check("no credential-routing env var is set on a cron agent pod",
          not ({"ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_BASE_URL",
                "CLAUDE_CODE_USE_BEDROCK", "CLAUDE_CODE_USE_VERTEX"} & set(env)))
    check("the platform sets the cron agent's OTel service identity",
          env.get("OTEL_SERVICE_NAME", {}).get("value") == "orbit-agents-nightly-audit")
    check("the cron agent has an OTLP endpoint to emit its run span to",
          env.get("OTEL_EXPORTER_OTLP_ENDPOINT", {}).get("value", "").startswith("http"))
    check("the budget brakes reach the cron pod",
          {"ORBIT_AGENT_MAX_TURNS", "ORBIT_AGENT_TIMEOUT_S"} <= set(env))
    sec = pspec.get("securityContext") or {}
    check("the cron pod runs as the runtime image's own uid",
          sec.get("runAsUser") == 10001 and sec.get("runAsNonRoot") is True)
    vols = {v["name"]: v for v in pspec.get("volumes") or []}
    items = (vols.get("content", {}).get("configMap", {}) or {}).get("items") or []
    check("the cron agent's skills ConfigMap is projected as <skill>/SKILL.md",
          any(i.get("path") == "fixture-skill/SKILL.md" for i in items))
print("\n".join(out))
PY
  python3 "$A/cron-check.py" "$A/cron-out.yaml" > "$A/cron-res" 2>&1
  if [ -s "$A/cron-res" ] && grep -q '^ok \|^FAIL ' "$A/cron-res"; then
    while IFS= read -r line; do
      case "$line" in
        "ok "*)   ok "agents(cron): ${line#ok }" ;;
        "FAIL "*) bad "agents(cron): ${line#FAIL }" ;;
      esac
    done < "$A/cron-res"
  else
    bad "agents(cron): the rendered-output checks did not run: $(head -3 "$A/cron-res" | tr '\n' ' ')"
  fi
  # The scheduled agent's Application is committed in plaintext like every other.
  (cd "$A" && python3 scripts/no-credentials.py >/dev/null 2>&1) \
    && ok "agents(cron): no credentials in the generated cronjob agent manifests" \
    || bad "agents(cron): the credential scanner found something in the cronjob manifests"
else
  bad "agents(cron): the cronjob agent did not render through the chart ($(head -2 "$A/cron-err" 2>/dev/null | tr '\n' ' '))"
fi

# --- the INTAKE, through the real chart (Phase D.3) ----------------------------
# An agent that declares `triggers:` needs an event router, and the router is rendered from
# the catalog exactly like everything else: an ordinary component of the same plane project,
# a ConfigMap holding the compiled routing table, three Secret-backed source
# credentials, and — only when something dispatches as a Job — the narrowest RBAC on the
# platform. Each assertion below is a way that goes wrong quietly.
#
# The fixture deliberately mixes both dispatch modes: a warm deployment agent the router
# CALLS, and a scheduled agent the router LAUNCHES as a one-shot Job.
INTAKE_FIXTURE='  - name: triage
    prompt: You triage alerts.
    skills: [fixture-skill]
    triggers:
      - {type: alert, match: {alertname: OrbitSiteDown, severity: critical, status: firing}}
      - {type: a2a}
  - name: pr-reviewer
    prompt: You review PRs.
    mode: cronjob
    schedule: "0 3 * * *"
    timeoutSeconds: 600
    triggers:
      - {type: github, repo: acme/widgets, events: [pull_request, push]}'
printf 'version: 3\nprojects: []\nagents:\n%s\n' "$INTAKE_FIXTURE" > "$A/catalog/projects.yaml"
# The fixture declares a `github` trigger, so this sandbox declares the intake's public
# webhook door wired — the gate itself is asserted above, both directions. Restored at the
# end of the section so nothing downstream inherits the declaration.
cp "$A/instance/config.yaml" "$A/instance/config.yaml.nodoor"
printf 'intakeIngress: wired\n' >> "$A/instance/config.yaml"
if (cd "$A" && python3 scripts/catalog-sync.py >/dev/null 2>&1) \
   && python3 -c "
import sys, yaml
app = [d for d in yaml.safe_load_all(open(sys.argv[1])) if d][0]
open(sys.argv[2], 'w').write(yaml.safe_dump(app['spec']['source']['helm']['valuesObject']))
" "$A/clusters/apps/orbit-agents-prod.yaml" "$A/intake-values.yaml" 2>/dev/null && helm template a charts/service -f "$A/intake-values.yaml" > "$A/intake-out.yaml" 2>"$A/intake-err"; then
  cat > "$A/intake-check.py" <<'PY'
import hashlib, json, sys, yaml
rendered = [d for d in yaml.safe_load_all(open(sys.argv[1])) if d]
app_docs = [d for d in yaml.safe_load_all(open(sys.argv[2])) if d]
out = []
def check(label, cond):
    out.append(("ok " if cond else "FAIL ") + label)
by = {}
for d in rendered:
    by.setdefault(d["kind"], {})[d["metadata"]["name"]] = d

# --- the router itself ---------------------------------------------------------
dep = by.get("Deployment", {}).get("orbit-agents-intake")
check("declaring a trigger renders the intake Deployment", dep is not None)
check("the intake gets a Service the webhook sources can reach",
      "orbit-agents-intake" in by.get("Service", {}))
# The GENERATOR never renders the door. The router authenticates three sources and holds
# every warm agent's bearer; an Ingress emitted because a catalog line said so would put
# that surface on the internet without anyone deciding to. Publishing it is a deliberate,
# hand-written, instance-side act — an Ingress the operator wrote, path-scoped to the one
# webhook path, gated behind `intakeIngress: wired` (asserted both directions above) and
# verified instance-side. Note the fixture DOES declare a github trigger and the sandbox
# DOES declare the door wired: so this row proves the declaration renders a ROUTE and still
# renders NO door, which is the whole shape of the gate.
check("the intake gets no Ingress and no Certificate even with a github trigger and the door declared",
      not by.get("Ingress") and not by.get("Certificate"))
if dep:
    spec = dep["spec"]["template"]["spec"]
    c = spec["containers"][0]
    env = {e["name"]: e for e in c.get("env") or []}
    # C2, restated for the router: the image is the INSTANCE pin. A catalog-chosen router
    # image would be a catalog-chosen holder of every warm agent's bearer.
    check("the router runs the image from the instance's intakeRuntime pin, not the agent one",
          c["image"] == "ghcr.io/example-core/orbit-intake:main-0000000")
    for key in ("INTAKE_ALERTMANAGER_TOKEN", "INTAKE_GITHUB_SECRET", "INTAKE_A2A_TOKEN"):
        ref = env.get(key) or {}
        check(f"{key} is mounted from a Secret",
              ref.get("valueFrom", {}).get("secretKeyRef", {}).get("name") == "orbit-intake")
        check(f"{key} is a pointer, never a literal in git", "value" not in ref)
    # The per-agent bearer the router presents to a WARM agent, read from the SAME Secret
    # the agent itself reads — one credential, not a second copy.
    warm = env.get("INTAKE_AGENT_TOKEN_TRIAGE") or {}
    check("the router gets the warm agent's bearer as its own env var",
          warm.get("valueFrom", {}).get("secretKeyRef", {}).get("name") == "agent-triage-token")
    check("that bearer is a pointer, never a literal", "value" not in warm)
    # A job-dispatch agent's bearer must NOT be handed to the router: it goes on the Job as
    # a secretKeyRef the kubelet resolves, so the router process never holds it.
    check("a JOB-dispatch agent's bearer is NOT given to the router",
          "INTAKE_AGENT_TOKEN_PR_REVIEWER" not in env)
    check("the router points at the mounted routing config",
          env.get("INTAKE_ROUTES_FILE", {}).get("value") == "/etc/orbit/intake/routes.json")
    check("the router has an OTLP endpoint to emit its event spans to",
          env.get("OTEL_EXPORTER_OTLP_ENDPOINT", {}).get("value", "").startswith("http"))
    # Finding #1: the router's read timeout for a warm (synchronous) dispatch must be >=
    # the agent's own task timeout, or every real warm run times out, is billed failed and
    # releases its dedup claim — and the source re-sends and double-spends. The fixture's
    # largest agent timeout is 600s, so the rendered value must be at least that.
    dt = env.get("INTAKE_DISPATCH_TIMEOUT_S", {}).get("value")
    check("the router's dispatch read timeout is >= the agent's own task timeout (#1)",
          dt is not None and int(dt) >= 600)
    check("no credential-routing env var is set on the router pod",
          not ({"ANTHROPIC_API_KEY", "ANTHROPIC_BASE_URL", "CLAUDE_CODE_OAUTH_TOKEN"} & set(env)))
    check("the router runs read-only-rootfs, non-root, with no capabilities",
          c["securityContext"]["readOnlyRootFilesystem"] is True
          and c["securityContext"]["allowPrivilegeEscalation"] is False
          and spec["securityContext"]["runAsNonRoot"] is True)
    mounts = {m["name"]: m for m in c.get("volumeMounts") or []}
    check("the routing config is mounted read-only",
          mounts.get("content", {}).get("readOnly") is True)
    # FOUND LIVE (2026-08-29): the router parses routes.json ONCE at import, so a catalog
    # change that alters the routing table updated the ConfigMap, synced green, and left
    # the running pod serving the routing table it booted with — an alert Alertmanager
    # delivered correctly came back 404 no_route until someone restarted the Deployment by
    # hand. The pod template must carry a hash of the routing table's CONTENT, which is
    # what makes a routes change a Deployment change and rolls the router automatically.
    ann = (dep["spec"]["template"]["metadata"].get("annotations") or {})
    stamped = ann.get("checksum/config")
    check("the intake pod template is stamped with a checksum of its routing config",
          bool(stamped))
    # ...and it is the hash of THIS routing table, not any old constant: a stamp that does
    # not track the content rolls the pod once and then never again.
    cm_doc = next((d for d in app_docs if d.get("kind") == "ConfigMap"
                   and d["metadata"]["name"] == "orbit-agents-intake-routes"), None)
    if cm_doc:
        want = hashlib.sha256(
            cm_doc["data"]["routes.json"].encode("utf-8")).hexdigest()
        check("...and it is the sha256 of the routing config the ConfigMap actually ships",
              stamped == want)

# --- RBAC: the narrowest grant on the platform ---------------------------------
roles = by.get("Role", {})
check("a job-dispatch agent renders exactly one Role", list(roles) == ["orbit-agents-intake"])
role = roles.get("orbit-agents-intake")
if role:
    # Enumerated, not "looks about right": every widening here is a widening of what the
    # only API-capable workload on the platform can do.
    check("the Role grants batch/jobs and NOTHING else",
          role["rules"] == [{"apiGroups": ["batch"], "resources": ["jobs"],
                             "verbs": ["create", "get"]}])
check("the grant is a RoleBinding, never a ClusterRoleBinding",
      "orbit-agents-intake" in by.get("RoleBinding", {})
      and not by.get("ClusterRole") and not by.get("ClusterRoleBinding"))
rb = by.get("RoleBinding", {}).get("orbit-agents-intake")
if rb:
    check("the binding names the intake ServiceAccount in the plane's own namespace",
          rb["subjects"] == [{"kind": "ServiceAccount", "name": "orbit-agents-intake",
                              "namespace": "orbit-agents-prod"}]
          and rb["roleRef"]["kind"] == "Role")
sas = by.get("ServiceAccount", {})
check("the router has its OWN ServiceAccount, and it is the one the pod uses",
      "orbit-agents-intake" in sas
      and (dep or {}).get("spec", {}).get("template", {}).get("spec", {})
          .get("serviceAccountName") == "orbit-agents-intake")
check("the router's ServiceAccount is the only one that mounts a token",
      sas.get("orbit-agents-intake", {}).get("automountServiceAccountToken") is True
      and sas.get("orbit-agents", {}).get("automountServiceAccountToken") is False)
# The agents themselves must NOT be attached to it: an agent pod that could create Jobs
# could create a Job on any image with the subscription token mounted.
agent_dep = by.get("Deployment", {}).get("orbit-agents-triage")
if agent_dep:
    check("an AGENT pod still runs under the token-less project ServiceAccount",
          agent_dep["spec"]["template"]["spec"]["serviceAccountName"] == "orbit-agents")

# --- the compiled routing table ------------------------------------------------
cms = {d["metadata"]["name"]: d for d in app_docs if d.get("kind") == "ConfigMap"}
cm = cms.get("orbit-agents-intake-routes")
check("the routing ConfigMap ships alongside the Application", cm is not None)
if cm:
    cfg = json.loads(cm["data"]["routes.json"])
    check("the routing config declares the version the router understands",
          cfg["version"] == 1)
    check("the alert trigger compiled to a route with its exact matchers",
          {"agent": "triage", "match": {"alertname": "OrbitSiteDown",
                                        "severity": "critical", "status": "firing"}}
          in cfg["routes"]["alert"])
    check("the github trigger compiled to a route on that repo and those events",
          {"agent": "pr-reviewer", "repo": "acme/widgets",
           "events": ["pull_request", "push"]} in cfg["routes"]["github"])
    check("the a2a trigger compiled to a callable agent",
          {"agent": "triage"} in cfg["routes"]["a2a"])
    warm, cold = cfg["agents"]["triage"], cfg["agents"]["pr-reviewer"]
    check("a deployment agent is dispatched by CALLING its Service",
          warm["dispatch"] == "service"
          and warm["url"] == "http://orbit-agents-triage/"
          and warm["tokenEnv"] == "INTAKE_AGENT_TOKEN_TRIAGE")
    check("a cronjob agent is dispatched by LAUNCHING a Job (it has no Service)",
          cold["dispatch"] == "job")
    job = cold["job"]
    # THE property: there is no per-agent image anywhere in the plane, and the router is
    # given exactly one it may ever launch.
    check("the Job the router may create is pinned to the instance's agentRuntime image",
          job["image"] == "ghcr.io/example-core/orbit-agent-runtime:main-0000000")
    check("the Job runs in the plane's own namespace",
          job["namespace"] == "orbit-agents-prod")
    check("the Job runs under the plane's ordinary ServiceAccount, not the router's",
          job["serviceAccountName"] == "orbit-agents")
    check("the Job's credentials are Secret POINTERS in the routing config",
          job["secretEnv"]["ORBIT_AGENT_TOKEN"]["secret"] == "agent-pr-reviewer-token"
          and job["secretEnv"]["CLAUDE_CODE_OAUTH_TOKEN"]["secret"] == "orbit-claude-code")
    check("the Job carries no credential-routing variable",
          not ({"ANTHROPIC_API_KEY", "ANTHROPIC_BASE_URL", "ANTHROPIC_AUTH_TOKEN",
                "CLAUDE_CODE_USE_BEDROCK"} & set(job["env"])))
    # The router owns the task text; a literal here would silently win or lose against it.
    check("the routing config does not pin ORBIT_AGENT_TASK (the event supplies it)",
          "ORBIT_AGENT_TASK" not in job["env"])
    check("the Job's wall-clock deadline derives from the agent's own timeout (600+grace)",
          job["activeDeadlineSeconds"] == 720)
    check("an event-triggered Job is not retried (a retry re-runs and re-spends)",
          job["backoffLimit"] == 0)
    check("the run's OTel identity is carried onto the Job (it bypasses the chart)",
          job["env"]["OTEL_SERVICE_NAME"] == "orbit-agents-pr-reviewer")
# --- NEW-3: dispatch happens after the 202, so a failed dispatch is invisible to HTTP-level
# metrics and (for GitHub) lost for good. The router must be scraped and alerted on.
check("a ServiceMonitor scrapes the router's /metrics (NEW-3)",
      "orbit-agents-intake" in by.get("ServiceMonitor", {}))
pr = [d for d in app_docs if d.get("kind") == "PrometheusRule"]
check("a PrometheusRule alerts on the router's dispatch failures (NEW-3)",
      any("orbit_intake_dispatch_total" in json.dumps(d) for d in pr))
# --- NEW-1: Alertmanager lives in `monitoring`; without a rule admitting that namespace to
# the intake pod the alert trigger POST is dropped by the plane's own default-deny and the
# whole path is dead. A targeted policy must make monitoring -> intake reachable. This is
# the suite's only cross-namespace reachability assertion; it goes RED if the rule is
# removed, leaving the default-deny "own pods only" in force for the intake too.
npols = [d for d in app_docs if d.get("kind") == "NetworkPolicy"]
def _admits_monitoring_to_intake(d):
    if d["spec"].get("podSelector", {}).get("matchLabels", {}).get(
            "app.kubernetes.io/name") != "intake":
        return False
    for rule in d["spec"].get("ingress", []):
        for src in rule.get("from", []):
            if src.get("namespaceSelector", {}).get("matchLabels", {}).get(
                    "kubernetes.io/metadata.name") == "monitoring":
                return True
    return False
check("the monitoring namespace can REACH the intake pod (alert path reachable, NEW-1)",
      any(_admits_monitoring_to_intake(d) for d in npols))
print("\n".join(out))
PY
  python3 "$A/intake-check.py" "$A/intake-out.yaml" \
      "$A/clusters/apps/orbit-agents-prod.yaml" > "$A/intake-res" 2>&1
  if [ -s "$A/intake-res" ] && grep -q '^ok \|^FAIL ' "$A/intake-res"; then
    while IFS= read -r line; do
      case "$line" in
        "ok "*)   ok "agents(intake): ${line#ok }" ;;
        "FAIL "*) bad "agents(intake): ${line#FAIL }" ;;
      esac
    done < "$A/intake-res"
  else
    bad "agents(intake): the rendered-output checks did not run: $(head -3 "$A/intake-res" | tr '\n' ' ')"
  fi
  # The routing table is committed in plaintext like every other generated file.
  (cd "$A" && python3 scripts/no-credentials.py >/dev/null 2>&1) \
    && ok "agents(intake): no credentials in the generated routing config" \
    || bad "agents(intake): the credential scanner found something in the routing config"
  # Deterministic by construction, or "regenerate and diff" is noise rather than a drift
  # check — and the routing config is JSON built from dicts, which is exactly where a
  # non-deterministic key order creeps in.
  cp "$A/clusters/apps/orbit-agents-prod.yaml" "$A/first.yaml"
  (cd "$A" && python3 scripts/catalog-sync.py >/dev/null 2>&1)
  cmp -s "$A/first.yaml" "$A/clusters/apps/orbit-agents-prod.yaml" \
    && ok "agents(intake): regeneration is byte-identical (the routing config is deterministic)" \
    || bad "agents(intake): regeneration changed the rendered bytes — the drift check is noise"
  # ...and the OTHER half of the same property. The checksum stamp is only worth having if
  # it MOVES when the routing table moves: byte-identical re-render (above) proves it is
  # not a timestamp, and this proves it is not a constant. A trigger is added to the same
  # fixture and the stamp must change — the exact catalog edit that, live, updated the
  # ConfigMap and left the router serving the old routes.
  stamp_of() { python3 -c "
import sys, yaml
for d in yaml.safe_load_all(open(sys.argv[1])):
    if d and d.get('kind') == 'Application':
        for c in d['spec']['source']['helm']['valuesObject']['components']:
            if c['name'] == 'intake':
                print(c.get('configChecksum', '')); break
" "$1"; }
  BEFORE_STAMP=$(stamp_of "$A/first.yaml")
  printf 'version: 3\nprojects: []\nagents:\n%s\n      - {type: alert, match: {alertname: OrbitDiskFull, status: firing}}\n' \
    "$INTAKE_FIXTURE" > "$A/catalog/projects.yaml"
  if (cd "$A" && python3 scripts/catalog-sync.py >/dev/null 2>&1); then
    AFTER_STAMP=$(stamp_of "$A/clusters/apps/orbit-agents-prod.yaml")
    { [ -n "$BEFORE_STAMP" ] && [ "$BEFORE_STAMP" != "$AFTER_STAMP" ]; } \
      && ok "agents(intake): declaring one more trigger CHANGES the pod-template checksum (the router rolls itself)" \
      || bad "agents(intake): a new trigger left the intake pod template unchanged — the running router would keep the old routes (404 no_route)"
  else
    bad "agents(intake): the extra-trigger fixture failed to generate"
  fi
  printf 'version: 3\nprojects: []\nagents:\n%s\n' "$INTAKE_FIXTURE" > "$A/catalog/projects.yaml"
  (cd "$A" && python3 scripts/catalog-sync.py >/dev/null 2>&1)
else
  bad "agents(intake): the trigger fixture did not render through the chart ($(head -2 "$A/intake-err" 2>/dev/null | tr '\n' ' '))"
fi
cp "$A/instance/config.yaml.nodoor" "$A/instance/config.yaml"

# --- metric and logs triggers: rule + route from ONE catalog block (Phase D.3) ------
# The property that separates these from `alert`: the operator writes NO rule by hand. One
# catalog block renders the alerting RULE (a PrometheusRule for metric, a Loki-ruler
# ConfigMap for logs) AND the intake route, and both halves must agree on the same derived
# alertname or the alert fires under a name nothing routes. The fixture also declares a
# plain `alert` trigger on the SAME agent, so the section can assert the reused routing path
# directly: metric, logs and alert must all compile to the identical {agent, match} shape.
# Each row below is a way that goes wrong quietly, so each is its own assertion.
ML_FIXTURE='  - name: watcher
    prompt: You watch signals.
    triggers:
      - {type: metric, name: high-5xx, expr: "sum(rate(http_5xx_total[5m])) > 10", for: 10m, summary: "5xx elevated", description: "the fuller line the Telegram template prints", severity: critical, labels: {team: sre}}
      - {type: logs, name: oomkills, expr: "sum(count_over_time({namespace=\"prod\"} |= \"OOMKilled\" [5m])) > 0", for: 2m, summary: "OOMKilled in prod"}
      - {type: alert, match: {alertname: OrbitSiteDown}}'
printf 'version: 3\nprojects: []\nagents:\n%s\n' "$ML_FIXTURE" > "$A/catalog/projects.yaml"
# The fixture declares a `logs` trigger, so this sandbox declares the ruler wired — the
# gate itself is asserted above, both directions.
cp "$A/instance/config.yaml" "$A/instance/config.yaml.nolokiruler"
printf 'lokiRuler: wired\n' >> "$A/instance/config.yaml"
if (cd "$A" && python3 scripts/catalog-sync.py >/dev/null 2>&1); then
  cat > "$A/ml-check.py" <<'PY'
import json, sys, yaml
app_docs = [d for d in yaml.safe_load_all(open(sys.argv[1])) if d]
out = []
def check(label, cond):
    out.append(("ok " if cond else "FAIL ") + label)
by_kind = {}
for d in app_docs:
    by_kind.setdefault(d.get("kind"), []).append(d)

# --- the metric half: a PrometheusRule the platform discovers -----------------------
prs = [d for d in by_kind.get("PrometheusRule", [])
       if d["metadata"]["name"] == "orbit-agents-metric-triggers"]
check("a metric trigger renders a PrometheusRule for the plane", len(prs) == 1)
mrule = None
if prs:
    pr = prs[0]
    check("the metric PrometheusRule lives in the plane namespace (discovered cluster-wide)",
          pr["metadata"]["namespace"] == "orbit-agents-prod")
    rules = pr["spec"]["groups"][0]["rules"]
    mrule = next((r for r in rules if r["alert"] == "OrbitAgentWatcherHigh5xx"), None)
    check("the alertname is derived deterministically as OrbitAgent<Agent><Trigger>",
          mrule is not None)
if mrule:
    check("the rule carries the catalog's PromQL expr verbatim",
          mrule["expr"] == "sum(rate(http_5xx_total[5m])) > 10")
    check("the rule carries the catalog's for duration", mrule["for"] == "10m")
    check("the rule carries the catalog's summary annotation",
          mrule["annotations"]["summary"] == "5xx elevated")
    check("the declared severity becomes a rule label", mrule["labels"]["severity"] == "critical")
    check("an extra catalog label is carried onto the rule", mrule["labels"].get("team") == "sre")
    check("provenance labels tie the alert to the agent and trigger",
          mrule["labels"].get("orbit_agent") == "watcher"
          and mrule["labels"].get("orbit_trigger") == "high-5xx")
    # The Telegram receiver's message template prints {{ .Annotations.description }};
    # before this key existed it printed a blank line on every generated alert.
    check("the optional description becomes the description annotation",
          mrule["annotations"].get("description")
          == "the fuller line the Telegram template prints")

# --- the logs half: a Loki-ruler alerting rule, shipped as a ConfigMap ---------------
lcms = [d for d in by_kind.get("ConfigMap", [])
        if d["metadata"]["name"] == "orbit-agents-logs-triggers"]
check("a logs trigger renders a Loki-ruler rule ConfigMap", len(lcms) == 1)
if lcms:
    lcm = lcms[0]
    # loki namespace + sidecar discovery label, exactly as the generated dashboards use.
    check("the logs rule ConfigMap lives in the loki namespace",
          lcm["metadata"]["namespace"] == "loki")
    check("the logs rule ConfigMap carries the ruler sidecar discovery label",
          lcm["metadata"].get("labels", {}).get("loki_rule") == "1")
    grp = yaml.safe_load(lcm["data"]["orbit-agents-logs.yaml"])
    lrule = next((r for r in grp["groups"][0]["rules"]
                  if r["alert"] == "OrbitAgentWatcherOomkills"), None)
    check("the Loki rule uses the same deterministic alertname derivation", lrule is not None)
    if lrule:
        check("the Loki rule carries the catalog's LogQL expr verbatim",
              lrule["expr"] == 'sum(count_over_time({namespace="prod"} |= "OOMKilled" [5m])) > 0')
        check("the Loki rule carries the catalog's for duration", lrule["for"] == "2m")
        check("an unset severity defaults to warning on the rule",
              lrule["labels"]["severity"] == "warning")

# --- the reused routing path: metric, logs and alert are ONE matcher kind -----------
cms = {d["metadata"]["name"]: d for d in by_kind.get("ConfigMap", [])}
routes = json.loads(cms["orbit-agents-intake-routes"]["data"]["routes.json"])
alert_routes = routes["routes"]["alert"]
# `status: firing` is part of the generated match. Alertmanager webhooks a group TWICE —
# once firing, once resolved — so without it every incident dispatches the agent twice, the
# second time on a payload saying the problem is over.
check("the metric trigger compiled to an intake route on its derived alertname, status=firing",
      {"agent": "watcher",
       "match": {"alertname": "OrbitAgentWatcherHigh5xx", "status": "firing"}} in alert_routes)
check("the logs trigger compiled to an intake route on its derived alertname, status=firing",
      {"agent": "watcher",
       "match": {"alertname": "OrbitAgentWatcherOomkills", "status": "firing"}} in alert_routes)
check("no generated metric/logs route omits status:firing (the resolved POST is not a second run)",
      all(r["match"].get("status") == "firing" for r in alert_routes
          if r["match"].get("alertname", "").startswith("OrbitAgent")))
check("the plain alert trigger compiled to the SAME-SHAPED route entry",
      {"agent": "watcher", "match": {"alertname": "OrbitSiteDown"}} in alert_routes)
# No divergence: metric/logs must NOT invent a new routes bucket of their own.
check("metric/logs do NOT fork a new routes kind — they reuse routes.alert",
      set(routes["routes"]) == {"alert", "github", "a2a"})
print("\n".join(out))
PY
  python3 "$A/ml-check.py" "$A/clusters/apps/orbit-agents-prod.yaml" > "$A/ml-res" 2>&1
  if [ -s "$A/ml-res" ] && grep -q '^ok \|^FAIL ' "$A/ml-res"; then
    while IFS= read -r line; do
      case "$line" in
        "ok "*)   ok "agents(metric/logs): ${line#ok }" ;;
        "FAIL "*) bad "agents(metric/logs): ${line#FAIL }" ;;
      esac
    done < "$A/ml-res"
  else
    bad "agents(metric/logs): the rendered-output checks did not run: $(head -3 "$A/ml-res" | tr '\n' ' ')"
  fi
  # The generated rules are committed in plaintext like every other manifest.
  (cd "$A" && python3 scripts/no-credentials.py >/dev/null 2>&1) \
    && ok "agents(metric/logs): no credentials in the generated rules" \
    || bad "agents(metric/logs): the credential scanner found something in the generated rules"
  # Deterministic by construction: the rules are sorted by alertname, so a regenerate-and-
  # diff drift check stays byte-stable regardless of catalog order.
  cp "$A/clusters/apps/orbit-agents-prod.yaml" "$A/ml-first.yaml"
  (cd "$A" && python3 scripts/catalog-sync.py >/dev/null 2>&1)
  cmp -s "$A/ml-first.yaml" "$A/clusters/apps/orbit-agents-prod.yaml" \
    && ok "agents(metric/logs): regeneration is byte-identical (rules are deterministic)" \
    || bad "agents(metric/logs): regeneration changed the rendered bytes — the drift check is noise"

  # --- promtool: the generated PromQL is PARSED, not just shaped -------------------
  # The generator never parses `expr`. promtool does — Prometheus's own parser — so this is
  # the oracle for "is the rendered rule a rule the server will actually load". Structural
  # generator checks stay; they answer a different question.
  PT=$(promtool_bin)
  if [ -z "$PT" ]; then
    bad "agents(metric/logs): promtool could not be resolved (PATH, \$PROMTOOL, or the pinned download) — nothing is parsing the rendered PromQL"
  else
    # The rendered rule groups, lifted out of the PrometheusRule into the file shape
    # promtool reads. Same bytes, no rewriting: what is checked is what was rendered.
    python3 -c "
import sys, yaml
docs = [d for d in yaml.safe_load_all(open(sys.argv[1])) if d]
pr = next(d for d in docs if d.get('kind') == 'PrometheusRule'
          and d['metadata']['name'] == 'orbit-agents-metric-triggers')
yaml.safe_dump(pr['spec'], open(sys.argv[2], 'w'), sort_keys=True)
" "$A/clusters/apps/orbit-agents-prod.yaml" "$A/ml-rules.yaml" 2>/dev/null
    if [ -s "$A/ml-rules.yaml" ] && "$PT" check rules "$A/ml-rules.yaml" >"$A/pt-out" 2>&1; then
      ok "agents(metric/logs): promtool parses the generated rule group (the PromQL is real)"
    else
      bad "agents(metric/logs): promtool rejected the generated rule group: $(head -3 "$A/pt-out" 2>/dev/null | tr '\n' ' ')"
    fi
    # The oracle must have teeth. Garbage PromQL passes every generator-side check — it is
    # a non-empty single-line string under the length cap — so it renders; promtool is what
    # refuses it. Without this row the check above proves nothing.
    printf 'version: 3\nprojects: []\nagents:\n  - {name: g, prompt: hi, triggers: [{type: metric, name: junk, expr: "sum(rate(", for: 5m, summary: s}]}\n' \
      > "$A/catalog/projects.yaml"
    if (cd "$A" && python3 scripts/catalog-sync.py >/dev/null 2>&1); then
      python3 -c "
import sys, yaml
docs = [d for d in yaml.safe_load_all(open(sys.argv[1])) if d]
pr = next(d for d in docs if d.get('kind') == 'PrometheusRule'
          and d['metadata']['name'] == 'orbit-agents-metric-triggers')
yaml.safe_dump(pr['spec'], open(sys.argv[2], 'w'), sort_keys=True)
" "$A/clusters/apps/orbit-agents-prod.yaml" "$A/ml-junk.yaml" 2>/dev/null
      "$PT" check rules "$A/ml-junk.yaml" >/dev/null 2>&1 \
        && bad "agents(metric/logs): promtool ACCEPTED unparseable PromQL — the oracle is toothless" \
        || ok "agents(metric/logs): promtool refuses unparseable PromQL that the structural checks pass"
    else
      bad "agents(metric/logs): the garbage-PromQL fixture did not render — the promtool oracle was not exercised"
    fi
    printf 'version: 3\nprojects: []\nagents:\n%s\n' "$ML_FIXTURE" > "$A/catalog/projects.yaml"
    (cd "$A" && python3 scripts/catalog-sync.py >/dev/null 2>&1)
    # The fixture proves the oracle; this tree's OWN rendered rules are what the server
    # loads. Every PrometheusRule under clusters/ (the platform's rules, and the agent
    # plane's metric triggers rendered from the real catalog) has its rule groups lifted,
    # unchanged, into promtool's file shape and parsed. Files are counted; a file name is
    # repeated only with a path's shape.
    mkdir -p "$A/real-rules"
    REALRULES=$(python3 - clusters "$A/real-rules" <<'PY'
import pathlib, re, sys, yaml
import shown as display   # the shared display rule (scripts/verify/shown.py)
root, out = pathlib.Path(sys.argv[1]), pathlib.Path(sys.argv[2])
PATH = re.compile(r"[A-Za-z0-9._/-]{1,300}")
n = 0; names = []
# both suffixes: the renderer (platform.py) reads .yaml and .yml alike
for f in sorted(p for p in root.rglob("*") if p.is_file() and p.suffix in (".yaml", ".yml")) if root.is_dir() else []:
    try:
        docs = list(yaml.safe_load_all(f.read_text()))
    except Exception:
        continue                       # not YAML: another row's business
    for d in docs:
        if isinstance(d, dict) and d.get("kind") == "PrometheusRule" and isinstance(d.get("spec"), dict):
            n += 1
            yaml.safe_dump(d["spec"], open(out / f"{n}.yaml", "w"), sort_keys=True)
            rel = str(f)
            names.append(display.shown_parts(rel, what="file"))
print(n); print(" ".join(names))
PY
)
    RR_N=${REALRULES%%$'\n'*}
    case "$RR_N" in ''|*[!0-9]*) RR_N=0;; esac
    if [ "$RR_N" -eq 0 ]; then
      ok "agents(metric/logs): this tree renders no PrometheusRule under clusters/ — promtool parsed the fixture only (a core tree, or an instance with no rules yet)"
    else
      RR_BAD=0; RR_WHICH=""
      for f in "$A"/real-rules/*.yaml; do
        if ! "$PT" check rules "$f" >"$A/pt-real-out" 2>&1; then
          RR_BAD=$((RR_BAD + 1)); RR_WHICH="$RR_WHICH ${f##*/}"
          [ -s "$A/pt-real-fail" ] || cp "$A/pt-real-out" "$A/pt-real-fail" 2>/dev/null
        fi
      done
      [ "$RR_BAD" -eq 0 ] \
        && ok "agents(metric/logs): promtool parses every PrometheusRule this tree renders ($RR_N rule object(s) under clusters/: ${REALRULES#*$'\n'})" \
        || bad "agents(metric/logs): promtool rejected $RR_BAD of $RR_N PrometheusRule object(s) rendered under clusters/ (extracted as:$RR_WHICH, in the order of the file list above) — the server will refuse to load them; promtool's message quotes rule text and is not repeated, run \`promtool check rules\` on the extracted object"
    fi
  fi

  # --- the namespace allowlist: WHERE a generated manifest may land ----------------
  # guard_writable bounds WHAT is written; this bounds WHERE. The loki ConfigMap was the
  # first generated object addressed at a namespace no catalog entry names, and nothing
  # checked — a manifest pointed at kube-system would have been written, and applied with
  # the app-controller's cluster-wide rights. No catalog field can reach the namespace, so
  # the guard is exercised directly, exactly as the generator calls it.
  cat > "$A/ns-guard.py" <<'NSGUARD'
import importlib.util, sys
spec = importlib.util.spec_from_file_location("cs", "scripts/catalog-sync.py")
cs = importlib.util.module_from_spec(spec); spec.loader.exec_module(cs)
out = []
def check(label, cond): out.append(("ok " if cond else "FAIL ") + label)
def refused(doc, allowed):
    try:
        cs.guard_namespaces([doc], allowed, "test")
    except SystemExit:
        return True
    return False
cm = lambda ns: {"kind": "ConfigMap", "metadata": {"name": "x", "namespace": ns}}
check("a manifest addressed at a namespace outside the allowlist is refused",
      refused(cm("kube-system"), {"orbit-agents-prod", "loki"}))
check("a manifest addressed at ANOTHER TENANT's namespace is refused",
      refused(cm("tenant-prod"), {"orbit-agents-prod", "loki"}))
check("a namespaced manifest with no namespace at all is refused",
      refused({"kind": "ConfigMap", "metadata": {"name": "x"}}, {"orbit-agents-prod"}))
check("the plane's own namespace and loki are allowed",
      not refused(cm("orbit-agents-prod"), {"orbit-agents-prod", "loki"})
      and not refused(cm("loki"), {"orbit-agents-prod", "loki"}))
check("the dashboard namespace the allowlist names is the one dashboards are written to",
      cs.DASHBOARD_NAMESPACE == "monitoring")
print("\n".join(out))
NSGUARD
  (cd "$A" && python3 ns-guard.py) > "$A/ns-res" 2>&1
  if grep -q '^ok \|^FAIL ' "$A/ns-res"; then
    while IFS= read -r line; do
      case "$line" in
        "ok "*)   ok "agents(namespaces): ${line#ok }" ;;
        "FAIL "*) bad "agents(namespaces): ${line#FAIL }" ;;
      esac
    done < "$A/ns-res"
  else
    bad "agents(namespaces): the namespace-allowlist checks did not run: $(head -3 "$A/ns-res" | tr '\n' ' ')"
  fi
else
  bad "agents(metric/logs): the metric+logs fixture failed to generate"
fi

# --- the ONE Alertmanager route that makes every trigger deliver -------------------
# A rendered rule is half a trigger. The other half is Alertmanager: a rule fires an alert,
# Alertmanager decides where it goes, and if nothing routes it to the intake the alert
# falls through to the HUMAN receiver — the agent never runs and a person is paged with a
# machine's work item. Nothing in the generator can fix that: the route lives in the
# platform's Alertmanager config, not in clusters/apps/.
#
# The fix is structural and one-time: every alertname a metric/logs trigger derives starts
# with `OrbitAgent`, so ONE terminal regex route carries every trigger that exists and
# every trigger anyone declares later. This section is the oracle for it — it SIMULATES
# Alertmanager's own first-match-wins routing over the config, so it answers "where would
# this alert actually go", not "does the file contain a string".
# Its own scratch dir for OUTPUT only — the checker itself is a core script
# (scripts/verify/am-routes.py), so core CI runs the same assertions over the TEMPLATE on
# every PR, not just whichever instance happens to run the suite.
AMDIR=$(mktemp -d)
AMCHECK=scripts/verify/am-routes.py
am_rows() { # <label-prefix> <mode> <am-file> [catalog] [secrets] [extra flags...]
  local label=$1 mode=$2 file=$3 catalog=${4:-} secrets=${5:-}; shift 5 2>/dev/null || shift $#
  python3 "$AMCHECK" "$mode" "$file" ${catalog:+--catalog "$catalog"} ${secrets:+--secrets "$secrets"} "$@" > "$AMDIR/am-res" 2>&1
  if grep -q '^ok \|^FAIL ' "$AMDIR/am-res"; then
    while IFS= read -r line; do
      case "$line" in
        "ok "*)   ok "$label: ${line#ok }" ;;
        "FAIL "*) bad "$label: ${line#FAIL }" ;;
      esac
    done < "$AMDIR/am-res"
  else
    bad "$label: the Alertmanager-route checks did not run: $(head -3 "$AMDIR/am-res" | tr '\n' ' ')"
  fi
}
# The template's copy moved under provisioning/ when init started re-rendering every
# config-derived file (v0.16.0); the instance's rendered copy is unmoved.
AM_TEMPLATE=template/provisioning/clusters/platform/apps/kube-prometheus-stack.yaml
AM_INSTANCE=clusters/platform/apps/kube-prometheus-stack.yaml
AM_ANY=0
# Core's template: a scaffolded orbit is BORN wired, so no instance ever has to add this
# route and no trigger ever needs an Alertmanager edit.
if [ -f "$AM_TEMPLATE" ]; then
  AM_ANY=1
  am_rows "alertmanager(template)" app "$AM_TEMPLATE" "" \
    template/provisioning/clusters/platform/config/monitoring/secrets.yaml --producers-optional
fi
# This instance's own Alertmanager config, in git — what the applier applies. The
# producer of the bearer Alertmanager presents lives in the instance's SEALED tree
# (clusters/platform/secrets/monitoring/, one SopsSecret per file), so that is what the
# secrets rows are pointed at; the rendered config file holds only the namespace.
# No fallback: a missing sealed tree is a missing producer, and the checker goes red on
# the path itself (round 2, 2026-09-13 — the fallback to the rendered config file, which
# renders no Secret, keyed every producer row off on an instance that had lost its tree).
AM_SEALED=clusters/platform/secrets/monitoring
if [ -f "$AM_INSTANCE" ]; then
  AM_ANY=1
  am_rows "alertmanager(instance)" app "$AM_INSTANCE" catalog/projects.yaml "$AM_SEALED"
fi
[ "$AM_ANY" = 1 ] || bad "alertmanager: no Alertmanager config found to check (neither $AM_TEMPLATE nor $AM_INSTANCE) — the delivery half of every trigger is unverified"

# --- and the oracle's OWN oracle -------------------------------------------------
# A simulator that says "reaches the intake" over every config it is shown is not a check.
# Each mutant below is a config that DOES NOT deliver, and the row asserts the checker goes
# red on it — plus two that DO deliver, which must stay green. Every one of these passed
# green against the first cut of am-routes.py, which is why they are here.
AMSRC=$AM_TEMPLATE; [ -f "$AMSRC" ] || AMSRC=$AM_INSTANCE
if [ -f "$AMSRC" ] && [ -f "$AMCHECK" ]; then
  MUT="$AMDIR/mutants"; mkdir -p "$MUT"
  # the source config is instance text: a parser's message quotes the tag or the line it
  # choked on, so the harness reads it behind try/except (its class alone), its stderr is
  # never inherited, and a harness that could not build lands every mutant row RED (round 23)
  python3 - "$AMSRC" "$MUT" "$AMCHECK" 2>/dev/null <<'PY'
import copy, importlib.util, re, sys, yaml
sys.dont_write_bytecode = True
src, out, checker = sys.argv[1], sys.argv[2], sys.argv[3]
# The checker's OWN matcher simulation, imported rather than re-implemented here: picking
# the route to sabotage means answering "does this route carry the derived alertname
# space", which is the question am-routes.py exists to answer. A second matcher parser in
# this harness could disagree with it, and then the mutants would sabotage a route the
# checker never reads — which is exactly the bug this selection fixes.
_spec = importlib.util.spec_from_file_location("orbit_am_routes", checker)
amr = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(amr)
PLANE_PROBES = [amr.alert_labels(a, severity=s)
                for a in amr.PROBES for s in amr.SEVERITIES]
try:
    doc = yaml.safe_load(open(src))
    helm = doc["spec"]["source"]["helm"]
    vals = helm.get("valuesObject")
    if vals is None:
        vals = yaml.safe_load(re.sub(r"\{\{[^{}]*\}\}", "PLACEHOLDER", helm["values"]))
    if not isinstance(vals, dict): raise ValueError("values are not a mapping")
except Exception as exc:
    print("the Alertmanager source config could not be read for mutation (%s)" % type(exc).__name__)
    sys.exit(2)

def emit(name, alertmanager):
    d = copy.deepcopy(doc)
    v = copy.deepcopy(vals); v["alertmanager"] = alertmanager
    d["spec"]["source"]["helm"].pop("values", None)
    d["spec"]["source"]["helm"]["valuesObject"] = v
    open(f"{out}/{name}.yaml", "w").write(yaml.safe_dump(d, sort_keys=False))

def mutate(name, fn, keep_inhibition=False):
    """One mutant, built from the real config with ONE thing sabotaged.

    Inhibition is neutralised by default, and that is a property of the harness rather
    than a shortcut: every mutant below except the inhibition pair is about WHERE an alert
    is routed, and the config they are cut from may be an instance that has not yet added
    the derived-space exemption. Left alone, that instance's own (real, separately
    reported) inhibition failure would turn every `green` routing mutant red and blame the
    routing simulation for it — three confusing rows in front of the one true finding. The
    inhibition mutants opt back in and assert the same property on its own axis."""
    am = copy.deepcopy(vals["alertmanager"])
    if not keep_inhibition:
        am["config"]["inhibit_rules"] = []
    fn(am)
    emit(name, am)

def routes(am): return am["config"]["route"]["routes"]
def intake_receiver(am):
    """The intake receiver by what it IS (a webhook at the intake URL), exactly as the
    real checker finds it — never by a fixed name an instance is free to change."""
    found = [r.get("name") for r in (am["config"].get("receivers") or [])
             if any(amr.is_intake_url((w or {}).get("url")) for w in (r.get("webhook_configs") or []))]
    if len(found) != 1:
        raise SystemExit("the source config has no single intake receiver (a webhook at the intake URL) to mutate")
    return found[0]
INTAKE = intake_receiver(vals["alertmanager"])
def covers_plane_space(r):
    """Does this route carry the WHOLE derived alertname space, or just a corner of it?"""
    try:
        return all(amr.route_matches(r, labels) for labels in PLANE_PROBES)
    except amr.Unparseable:
        return False   # a route this simulator cannot read is not the plane's generic one
def plane(am):
    """The route the mutants must sabotage: the plane's GENERIC one.

    Selected by what its matchers cover, not by position. An instance is free to
    hand-write its own narrow intake route — the deployed orbit has an explicit
    `alertname = "OrbitHelloProbe"` route to the same receiver — and it may sit ABOVE the
    generic `^OrbitAgent.*` route. Taking the first intake-receiver route mutated THAT
    one: the generic route survived, the config still delivered, am-routes.py correctly
    said so, and this harness read its green as a FAIL — two spurious reds on a perfectly
    wired instance (narrow-route and muted-plane)."""
    for i, r in enumerate(routes(am)):
        if r.get("receiver") == INTAKE and covers_plane_space(r):
            return i
    raise SystemExit("the source config has no plane route to mutate")

# A route ABOVE the plane's, keyed on a label the plane's alerts carry but the old
# two-label simulation never supplied. Every generated alert is stolen to Telegram.
mutate("thief-severity-regex", lambda am: routes(am).insert(
    plane(am), {"matchers": ['severity =~ "critical|warning"'], "receiver": "telegram"}))
mutate("thief-severity-eq", lambda am: routes(am).insert(
    plane(am), {"matchers": ['severity = "warning"'], "receiver": "telegram"}))
mutate("thief-orbit-agent", lambda am: routes(am).insert(
    plane(am), {"matchers": ['orbit_agent =~ ".+"'], "receiver": "telegram"}))
# Alertmanager's BRACED and comma-joined matcher forms — legal, and unparsed by the first
# cut, which returned "does not match" for both and called the config wired.
mutate("thief-braced", lambda am: routes(am).insert(
    plane(am), {"matchers": ['{alertname=~"^OrbitAgent.*"}'], "receiver": "telegram"}))
mutate("thief-comma", lambda am: routes(am).insert(
    plane(am), {"matchers": ['alertname=~"^OrbitAgent.*", severity="warning"'],
                "receiver": "telegram"}))
# A matcher NOTHING can parse must abort the check, never be treated as non-matching.
mutate("unparseable", lambda am: routes(am).insert(
    plane(am), {"matchers": ['alertname ~~ "nonsense'], "receiver": "telegram"}))
# A plane route narrowed to the ONE sample alertname: it carries the oracle's own probe
# and nothing anyone declares later.
def narrow(am):
    # The generator's own sample name, not a literal: a rename in catalog-sync.py would
    # otherwise leave this "narrower" mutant matching nothing at all — red for the wrong
    # reason, and no longer a test of the coverage rule.
    routes(am)[plane(am)]["matchers"] = [f'alertname =~ "^{amr.SAMPLE}$"']
mutate("narrow-route", narrow)
# A muted plane route matches perfectly and delivers nothing.
def muted(am):
    routes(am)[plane(am)]["mute_time_intervals"] = ["offhours"]
    am["config"]["time_intervals"] = [{"name": "offhours", "time_intervals":
                                       [{"times": [{"start_time": "00:00",
                                                    "end_time": "06:00"}]}]}]
mutate("muted-plane", muted)
# The plane route deleted outright — the pre-PR shape, and the whole point of the section.
# EVERY route to the intake goes, deliberately: pre-PR there was no intake receiver at all,
# so a hand-written narrow route to it could not have existed either. This one is keyed on
# the receiver rather than on plane(), and correctly so.
def gone(am):
    am["config"]["route"]["routes"] = [r for r in routes(am)
                                       if r.get("receiver") != INTAKE]
    am["config"]["receivers"] = [r for r in am["config"]["receivers"]
                                 if r["name"] != INTAKE]
    am["alertmanagerSpec"]["secrets"] = ["alertmanager-telegram"]
mutate("pre-pr", gone)

# --- inhibition: the way a PERFECTLY ROUTED alert still never arrives ------------
# FOUND LIVE (2026-08-29). Every routing mutant above is about where an alert GOES. This
# pair is about an alert that goes exactly where it should and is held `suppressed` before
# the notification pipeline runs — which is what kube-prometheus-stack's stock
# InfoInhibitor rule does to every `severity: info` alert, the derived space included.
# Neither of these shapes touches a single route, so every routing row stays green on both.
STOCK = amr.STOCK_INHIBIT_RULES
# The pre-fix shape: the values declare no inhibit_rules at all, so the CHART's four apply.
# A simulator that read "no inhibit_rules in this file" as "nothing is inhibited" would
# call this config wired — it is the exact config the live finding was found in.
def inhibit_inherited(am):
    am["config"].pop("inhibit_rules", None)
mutate("inhibit-inherited", inhibit_inherited, keep_inhibition=True)
# The rules spelled out, but WITHOUT the derived-space exemption — the same suppression,
# now visible in the file, so this one is red for a reason the config states outright.
def inhibit_stock(am):
    am["config"]["inhibit_rules"] = copy.deepcopy(STOCK)
mutate("inhibit-stock", inhibit_stock, keep_inhibition=True)
# An inhibit rule this simulator cannot read must ABORT, exactly like an unreadable route
# matcher: "a rule I could not parse" is not "a rule that does not suppress".
def inhibit_unreadable(am):
    am["config"]["inhibit_rules"] = [{"target_matchers": ['severity ~~ "info']}]
mutate("inhibit-unparseable", inhibit_unreadable, keep_inhibition=True)
# ...and the two shapes that DO deliver. Explicitly empty is a decision — an instance that
# turns inhibition off entirely suppresses nothing — and a rule that targets something
# outside the derived space is none of the plane's business.
def inhibit_empty(am):
    am["config"]["inhibit_rules"] = []
mutate("ok-inhibit-empty", inhibit_empty, keep_inhibition=True)
def inhibit_elsewhere(am):
    am["config"]["inhibit_rules"] = [
        {"source_matchers": ['severity = critical'],
         "target_matchers": ['severity = warning', 'namespace = "kube-system"'],
         "equal": ["namespace"]}]
mutate("ok-inhibit-elsewhere", inhibit_elsewhere, keep_inhibition=True)

# --- and the two that DO deliver, which must stay GREEN -------------------------
# `continue: true` on the plane route with nothing below it matching: real Alertmanager
# falls back to the parent receiver only when NO child matched, so this delivers to the
# intake alone. The first cut appended the parent unconditionally and called it a leak.
mutate("ok-continue", lambda am: routes(am)[plane(am)].update({"continue": True}))
# A child route with no `receiver` of its own inherits its parent's.
def inherit(am):
    i = plane(am)
    routes(am)[i] = {"matchers": [f'alertname =~ "^{amr.PREFIX}.*"'], "receiver": INTAKE,
                     "routes": [{"matchers": ['severity = "critical"']}]}
mutate("ok-inherited-receiver", inherit)

# --- and the shape THIS harness itself got wrong --------------------------------
# The deployed orbit hand-writes its own narrow intake route — `alertname =
# "OrbitHelloProbe"`, the connectivity probe — and it sits ABOVE the generic route. When
# `plane()` picked the FIRST intake-receiver route, both sabotages below landed on that
# probe route: the generic route survived, the config still delivered, am-routes.py said
# so correctly, and this harness read that green as a FAIL. Two spurious reds on a
# perfectly wired instance. The same two sabotages are re-run here over a config carrying
# the decoy, so that selection cannot silently go back to "whichever comes first".
DECOY_ALERT = "OrbitHelloProbe"
def decoy_mutate(name, fn):
    am = copy.deepcopy(vals["alertmanager"])
    am["config"]["inhibit_rules"] = []          # a routing mutant — see mutate()
    routes(am).insert(plane(am), {"matchers": [f'alertname = "{DECOY_ALERT}"'],
                                  "receiver": INTAKE})
    fn(am)
    emit(name, am)
decoy_mutate("decoy-clean", lambda am: None)
decoy_mutate("decoy-narrow", narrow)
decoy_mutate("decoy-muted", muted)
PY
  HARNESS_RC=$?
  am_mutant() { # <name> <expect: red|green> <why>
    [ "$HARNESS_RC" -eq 0 ] || { bad "alertmanager(oracle): $3 — no mutant was built (the source config could not be read for mutation; the parser's message is not repeated)"; return; }
    python3 "$AMCHECK" app "$MUT/$1.yaml" >"$MUT/$1.out" 2>&1; rc=$?
    if [ "$2" = red ]; then
      [ $rc -ne 0 ] && ok "alertmanager(oracle): $3" \
                    || bad "alertmanager(oracle): $3 — the checker passed a config that does not deliver"
    else
      [ $rc -eq 0 ] && ok "alertmanager(oracle): $3" \
                    || bad "alertmanager(oracle): $3 — the checker failed a config that DOES deliver ($(grep -m1 '^FAIL' "$MUT/$1.out"))"
    fi
  }
  am_mutant thief-severity-regex   red   "a severity=~ route above the plane's is caught (FULL label simulation)"
  am_mutant thief-severity-eq      red   "a severity= route above the plane's is caught"
  am_mutant thief-orbit-agent      red   "a route keyed on the plane's own orbit_agent label is caught"
  am_mutant thief-braced           red   "Alertmanager's braced {a=~\"...\"} matcher form is PARSED, not skipped"
  am_mutant thief-comma            red   "a comma-joined matcher list is PARSED, not skipped"
  am_mutant unparseable            red   "a matcher the parser cannot read ABORTS red (never 'non-matching')"
  am_mutant narrow-route           red   "a route narrower than the whole OrbitAgent* space is refused"
  am_mutant muted-plane            red   "a MUTED plane route is not delivery"
  am_mutant pre-pr                 red   "the pre-PR shape (no route, no receiver, no mount) is caught"
  am_mutant inhibit-inherited      red   "a config declaring NO inhibit_rules inherits the chart's stock four — and they suppress the derived space"
  am_mutant inhibit-stock          red   "the stock inhibit rules WITHOUT the derived-space exemption are caught (a routed alert that is never notified)"
  am_mutant inhibit-unparseable    red   "an inhibit rule the parser cannot read ABORTS red (never 'does not suppress')"
  am_mutant ok-inhibit-empty       green "inhibition turned off outright suppresses nothing"
  am_mutant ok-inhibit-elsewhere   green "an inhibit rule aimed outside the derived space is not the plane's problem"
  am_mutant ok-continue            green "a legitimate continue:true on the plane route is NOT a leak"
  am_mutant ok-inherited-receiver  green "a child route inheriting its parent's receiver still delivers"
  # ...and the three that keep THIS harness honest about WHICH route it sabotages: an
  # instance's own narrow intake route above the plane's must not be mistaken for it.
  am_mutant decoy-clean            green "a hand-written narrow intake route ABOVE the plane's is not a mutant"
  am_mutant decoy-narrow           red   "...and the narrowing sabotage still lands on the GENERIC route, not on it"
  am_mutant decoy-muted            red   "...and so does the mute"
  # ...and the arming that keeps the row honest on an instance that declares no trigger:
  # the pre-PR shape plus an EMPTY catalog is green, and goes red on the first metric
  # trigger — with every actionable row shown, not just the first.
  printf 'version: 3\nprojects: []\nagents: []\n' > "$MUT/cat-empty.yaml"
  printf 'version: 3\nprojects: []\nagents:\n  - name: w\n    prompt: hi\n    triggers:\n      - {type: metric, name: hot, expr: "up == 0", for: 5m, summary: s}\n' \
    > "$MUT/cat-metric.yaml"
  python3 "$AMCHECK" app "$MUT/pre-pr.yaml" --catalog "$MUT/cat-empty.yaml" >"$MUT/arm-empty.out" 2>&1 \
    && ok "alertmanager(oracle): a pre-PR config with ZERO agents is GREEN (the rows arm with the first trigger)" \
    || bad "alertmanager(oracle): a pre-PR config with zero agents went red — an unactionable row on every core bump"
  python3 "$AMCHECK" app "$MUT/pre-pr.yaml" --catalog "$MUT/cat-metric.yaml" >"$MUT/arm-metric.out" 2>&1
  if [ $? -ne 0 ] && [ "$(grep -c '^FAIL' "$MUT/arm-metric.out")" -ge 5 ]; then
    ok "alertmanager(oracle): one metric trigger arms it — and ALL actionable rows are emitted, not just the first"
  else
    bad "alertmanager(oracle): the first metric trigger did not produce a full red report ($(grep -c '^FAIL' "$MUT/arm-metric.out") FAIL rows)"
  fi
else
  bad "alertmanager(oracle): no config to build the checker's own negative fixtures from"
fi

# The router costs nothing until something declares a trigger. An instance with only cron
# and manual agents must render exactly what it did before D.3 — no Deployment, no Role, no
# ConfigMap — because every one of those is a resource an operator would
# have to reason about for a feature nobody asked for.
printf 'version: 3\nprojects: []\nagents:\n%s\n' "$AGENT_FIXTURE" > "$A/catalog/projects.yaml"
if (cd "$A" && python3 scripts/catalog-sync.py >/dev/null 2>&1); then
  grep -q 'intake' "$A/clusters/apps/orbit-agents-prod.yaml" \
    && bad "agents(intake): an agent with NO triggers still rendered the router" \
    || ok "agents(intake): no triggers declared -> no router, no RBAC, no routing config"
else
  bad "agents(intake): the no-trigger fixture failed to generate"
fi
# And the refusal that stops a half-wired instance: triggers without an intake image pin.
python3 - "$A/instance/config.yaml" <<'PY'
import sys, yaml
p = sys.argv[1]
c = yaml.safe_load(open(p))
c.pop("intakeRuntime", None)
open(p, "w").write(yaml.safe_dump(c, sort_keys=True))
PY
printf 'version: 3\nprojects: []\nagents:\n%s\n' "$INTAKE_FIXTURE" > "$A/catalog/projects.yaml"
(cd "$A" && python3 scripts/catalog-sync.py >/dev/null 2>&1) \
  && bad "agents(intake): triggers rendered with no intakeRuntime pin — the router image was guessed" \
  || ok "agents(intake): triggers with no instance intakeRuntime pin are refused, never guessed"
rm -rf "$A"

echo "== chart fixes that previously had no test =="
C=$(mktemp -d)
chart() { # <label> <expect: refuse|accept> <component yaml fragment> [grep pattern that must appear]
  printf 'project: c\nenv: prod\nhostnames: [c.example.org]\ncomponents:\n  - {name: api, kind: web, image: img, port: 80, path: /, %s}\n' "$3" > "$C/c.yaml"
  out=$(helm template c charts/service -f "$C/c.yaml" 2>&1); rc=$?
  if [ "$2" = refuse ]; then
    [ $rc -ne 0 ] && ok "chart: $1" || bad "chart: $1 (rendered, should refuse)"
  elif [ -n "${4:-}" ]; then
    { [ $rc -eq 0 ] && echo "$out" | grep -q "$4"; } && ok "chart: $1" \
      || bad "chart: $1 (rc=$rc, expected /$4/ in output)"
  else
    [ $rc -eq 0 ] && ok "chart: $1" || bad "chart: $1 (rc=$rc)"
  fi
}
chart "explicit null runAsUser is refused by the schema" refuse \
  'securityContext: {runAsUser: null}'
# And the template still strips nils, for anything reaching it without schema validation.
printf 'project: c\nenv: prod\nhostnames: [c.example.org]\npodSecurityContext: {runAsUser: 65532, runAsNonRoot: true}\ncomponents:\n  - {name: api, kind: web, image: img, port: 80, path: /}\n' > "$C/c.yaml"
helm template c charts/service -f "$C/c.yaml" 2>/dev/null | grep -q 'runAsUser: 65532' \
  && ok "chart: the hardened UID is pinned by default" \
  || bad "chart: default pod securityContext lost its pinned UID"
printf 'project: c\nenv: prod\nhostnames: [c.example.org]\ncomponents:\n  - {name: api, kind: web, image: "nginx:1.27", port: 80, path: /}\n' > "$C/c.yaml"
helm template c charts/service -f "$C/c.yaml" >/dev/null 2>&1 \
  && bad "chart: image with an embedded tag should be refused" \
  || ok "chart: image with an embedded tag is refused (no ':latest' welded on)"
# imagePullSecret is empty by default; setting it must produce a reference, and leaving it
# empty must produce none. The old chart named a Secret that did not exist, on every pod.
printf 'project: c\nenv: prod\nhostnames: [c.example.org]\nimagePullSecret: ""\ncomponents:\n  - {name: api, kind: web, image: img, port: 80, path: /}\n' > "$C/c.yaml"
helm template c charts/service -f "$C/c.yaml" 2>/dev/null | grep -q imagePullSecrets \
  && bad "chart: empty imagePullSecret still emits a reference" \
  || ok "chart: no imagePullSecret reference when none is configured"
printf 'project: c\nenv: prod\nhostnames: [c.example.org]\nimagePullSecret: real-secret\ncomponents:\n  - {name: api, kind: web, image: img, port: 80, path: /}\n' > "$C/c.yaml"
helm template c charts/service -f "$C/c.yaml" 2>/dev/null | grep -q 'name: real-secret' \
  && ok "chart: a configured imagePullSecret is referenced" \
  || bad "chart: configured imagePullSecret was not referenced"
# The Ingress path must be quoted, or a trailing YAML comment truncates it into a collision.
printf 'project: c\nenv: prod\nhostnames: [c.example.org]\ncomponents:\n  - {name: a, kind: web, image: i1, port: 80, path: /api}\n' > "$C/c.yaml"
helm template c charts/service -f "$C/c.yaml" 2>/dev/null | grep -q 'path: "/api"' \
  && ok "chart: Ingress path is quoted" \
  || bad "chart: Ingress path is not quoted — a trailing comment can truncate it"

# `configChecksum` — the chart half of the stale-mounted-config fix (found live
# 2026-08-29). A long-lived pod that parses its mounted ConfigMap once at startup keeps
# serving the old copy forever unless the CONTENT is part of the pod template.
# A real digest, not a run of zeros: YAML reads an all-numeric string as a NUMBER,
# and the schema (correctly) refuses a number here — which would make this row test
# the schema instead of the annotation.
DIGEST=4fa1a13ac468ac495f3390e859d76d5e8ef49806815b45a21de7711bcc624194
chart "a configChecksum reaches the POD TEMPLATE as checksum/config" accept \
  "configChecksum: $DIGEST" "checksum/config: \"$DIGEST\""
# It shares the annotations block with `language:`, and the OTel operator's injection
# annotation is what makes a service traced at all — an either/or here would silently
# untrace every component that also carries a checksum.
chart "language injection and configChecksum coexist in one annotations block" accept \
  "language: python, configChecksum: $DIGEST" "inject-python"
printf 'project: c\nenv: prod\nhostnames: [c.example.org]\ncomponents:\n  - {name: api, kind: web, image: img, port: 80, path: /, language: python, configChecksum: %s}\n' "$DIGEST" > "$C/c.yaml"
helm template c charts/service -f "$C/c.yaml" 2>/dev/null | grep -q "checksum/config" \
  && ok "chart: ...and the checksum survives beside it" \
  || bad "chart: a component with both language and configChecksum lost the checksum"
# The value reaches a pod annotation, so the one thing it may be is a digest the generator
# computed. Free-form here would be a catalog-writable annotation value.
chart "a configChecksum that is not a sha256 digest is refused" refuse \
  'configChecksum: "not-a-digest"'
# A CronJob's pod is created fresh for every run and always reads the current ConfigMap —
# there is nothing to roll, and an annotation implying otherwise is a claim the chart does
# not honour.
printf 'project: c\nenv: prod\nhostnames: [c.example.org]\ncomponents:\n  - {name: job, kind: cron, image: img, schedule: "0 3 * * *", configChecksum: %s}\n' "$DIGEST" > "$C/c.yaml"
helm template c charts/service -f "$C/c.yaml" 2>/dev/null | grep -q "checksum/config" \
  && bad "chart: a CronJob got a config checksum — there is no long-lived pod to roll" \
  || ok "chart: a CronJob gets no config checksum (its pod is fresh every run)"

# A ConfigMap `items` path is a filesystem path inside the mount (the agent plane's
# skills library is one). The kubelet refuses an absolute path or one containing '..',
# but only at apply time — long after a clean render. And the schema has to express that
# STRUCTURALLY: Helm validates with Go's RE2, so a '(?!\.\.)' lookahead would not compile
# and would take every render on the platform down with it.
# --- the ONE Kubernetes API grant on the platform (Phase D.3) -----------------
# `rbac.jobRunner` exists for the agent plane's intake and nothing else. Three properties,
# each of which would be a real privilege escalation if it stopped holding.
printf 'project: c\nenv: prod\nhostnames: [c.example.org]\ncomponents:\n  - {name: api, kind: web, image: img, port: 80, path: /}\n' > "$C/c.yaml"
OUT=$(helm template c charts/service -f "$C/c.yaml" 2>&1)
{ ! echo "$OUT" | grep -qE '^kind: (Role|RoleBinding)$'; } \
  && ok "chart: no rbac value -> no Role and no RoleBinding anywhere" \
  || bad "chart: a Role is rendered for a release that asked for none"
echo "$OUT" | grep -q 'serviceAccountName: c$' \
  && ok "chart: without rbac a pod keeps the token-less project ServiceAccount" \
  || bad "chart: the default ServiceAccount changed for a release with no rbac"
# A component key must NOT be able to mint a Role. Component dicts pass through from
# catalog/projects.yaml essentially verbatim, so if this ever became a component key any
# hand-written project could grant itself create-Jobs in its own namespace.
# Since the area-5 sweep the component item is closed (additionalProperties: false), so the
# schema refuses the key outright: the render never happens, and nothing can be minted.
# The probe carries the value a string-typed key would accept, so the row can only stay
# green while the schema really refuses the key (a boolean would fail on type alone).
chart "a component-level jobRunner/rbac key is refused by the schema (the selector is a root value)" refuse \
  'jobRunner: "api", rbac: true'
# A root rbac.jobRunner naming a component that does not exist would bind a ServiceAccount
# no pod uses, while the component it was meant for ran with no rights and failed every
# dispatch — green render, dead router.
printf 'project: c\nenv: prod\nhostnames: [c.example.org]\nrbac: {jobRunner: ghost}\ncomponents:\n  - {name: api, kind: web, image: img, port: 80, path: /}\n' > "$C/c.yaml"
helm template c charts/service -f "$C/c.yaml" >/dev/null 2>&1 \
  && bad "chart: rbac.jobRunner naming a nonexistent component rendered anyway" \
  || ok "chart: rbac.jobRunner naming a nonexistent component is refused at render"
printf 'project: c\nenv: prod\nhostnames: [c.example.org]\nrbac: {jobRunner: "../evil"}\ncomponents:\n  - {name: api, kind: web, image: img, port: 80, path: /}\n' > "$C/c.yaml"
helm template c charts/service -f "$C/c.yaml" >/dev/null 2>&1 \
  && bad "chart: a path-shaped rbac.jobRunner was accepted by the schema" \
  || ok "chart: rbac.jobRunner is schema-constrained to a DNS label"

chart "a ConfigMap item path escaping the mount is refused" refuse \
  'configMap: {name: cm, mountPath: /m, items: [{key: k, path: "../escape"}]}'
chart "an absolute ConfigMap item path is refused" refuse \
  'configMap: {name: cm, mountPath: /m, items: [{key: k, path: "/abs"}]}'
chart "a ConfigMap item projects a key into a subdirectory" accept \
  'configMap: {name: cm, mountPath: /m, items: [{key: k, path: "s/SKILL.md"}]}' \
  'path: s/SKILL.md'

# The dump Job's name is a digest of its pod template, and the digest must cover every
# input that REACHES the template. imagePullSecrets comes from a root-level value the
# component dict never sees: adding it changed the immutable template under an unchanged
# name, and every project's sync failed at once on "field is immutable".
printf 'project: c\nenv: prod\nhostnames: [c.example.org]\ndatabase: {size: 1Gi}\ncomponents:\n  - {name: api, kind: web, image: img, port: 80, path: /, env: {PGHOST: {secret: c-db-app, key: host}}}\n' > "$C/c.yaml"
J1=$(helm template c charts/service -f "$C/c.yaml" 2>/dev/null | grep -oE 'name: c-db-backup-[0-9a-f]{8}' | head -1)
printf 'project: c\nenv: prod\nhostnames: [c.example.org]\nimagePullSecret: ghcr-pull\ndatabase: {size: 1Gi}\ncomponents:\n  - {name: api, kind: web, image: img, port: 80, path: /, env: {PGHOST: {secret: c-db-app, key: host}}}\n' > "$C/c.yaml"
J2=$(helm template c charts/service -f "$C/c.yaml" 2>/dev/null | grep -oE 'name: c-db-backup-[0-9a-f]{8}' | head -1)
{ [ -n "$J1" ] && [ -n "$J2" ] && [ "$J1" != "$J2" ]; } \
  && ok "chart: a pull-secret change renames the dump Job instead of mutating it" \
  || bad "chart: dump Job name unchanged across a pod-template change ($J1 vs $J2)"

# --- a scraped component that serves nothing else -----------------------------
# A ServiceMonitor selects Endpoints, not pods. A worker declaring `metrics:` used to
# render the ServiceMonitor and no Service at all: green everywhere, zero targets, and
# an alert built on that metric could never fire — the exact shape of a singleton
# listener whose only HTTP surface is its own liveness. The check parses the render and matches
# the ServiceMonitor's endpoint port against the Service's actual port NAMES, because a
# Service that exists under a different port name fails just as silently.
printf 'project: c\nenv: prod\nhostnames: [c.example.org]\ncomponents:\n  - {name: w, kind: worker, image: img, metrics: {port: 9100}}\n' > "$C/c.yaml"
if helm template c charts/service -f "$C/c.yaml" > "$C/w.yaml" 2>"$C/w.err"; then
  python3 - "$C/w.yaml" > "$C/w.txt" <<'PY'
import sys, yaml
docs = [d for d in yaml.safe_load_all(open(sys.argv[1])) if d]
svc = [d for d in docs if d["kind"] == "Service"]
sm  = [d for d in docs if d["kind"] == "ServiceMonitor"]
names = {p["name"] for s in svc for p in s["spec"]["ports"]}
ports = {p["port"] for s in svc for p in s["spec"]["ports"]}
want  = {e["port"] for m in sm for e in m["spec"]["endpoints"]}
print("svc", len(svc), "sm", len(sm), "names", sorted(names), "ports", sorted(ports),
      "sm_wants", sorted(want), "matched", want and want <= names)
PY
  grep -q 'svc 1 sm 1' "$C/w.txt" \
    && ok "chart: a scraped worker renders both a Service and a ServiceMonitor" \
    || bad "chart: scraped worker rendered $(cat "$C/w.txt")"
  grep -q 'ports \[9100\]' "$C/w.txt" \
    && ok "chart: the worker's Service exposes the metrics port and no http port" \
    || bad "chart: worker Service ports wrong — $(cat "$C/w.txt")"
  grep -q 'matched True' "$C/w.txt" \
    && ok "chart: the ServiceMonitor's endpoint names a port the Service actually has" \
    || bad "chart: ServiceMonitor endpoint does not match any Service port — $(cat "$C/w.txt")"
else
  bad "chart: a worker declaring metrics failed to render ($(head -1 "$C/w.err"))"
fi
# A CronJob's pod lives for seconds; scraping it is not a smaller version of scraping a
# service, it is a metric that is absent almost always. Refuse rather than render a
# ServiceMonitor whose target list is empty except during the job.
printf 'project: c\nenv: prod\nhostnames: [c.example.org]\ncomponents:\n  - {name: j, kind: cron, image: img, schedule: "0 3 * * *", metrics: {port: 9100}}\n' > "$C/c.yaml"
helm template c charts/service -f "$C/c.yaml" >/dev/null 2>&1 \
  && bad "chart: a cron component declaring metrics rendered a ServiceMonitor over an ephemeral pod" \
  || ok "chart: a cron component declaring metrics is refused"

# --- declared databases ------------------------------------------------------
# A `database:` block is the only place the chart emits an image reference that already
# carries a tag, which is a code path no catalog component can reach — the component
# schema forbids a colon in `image:`. The first thing to take that path found a
# whitespace-trim bug that had been sitting in the helper unnoticed: the render came out
# as `- name: db-backupimage: ghcr.io/...`, one glued line. Parsing the output is what
# catches it; grepping for a substring does not.
DB_VALS='project: c
env: prod
hostnames: [c.example.org]
database: {size: 1Gi}
components:
  - {name: api, kind: web, image: img, port: 80, path: /, env: {PGHOST: {secret: c-db-app, key: host}}}
'
printf '%s' "$DB_VALS" > "$C/c.yaml"
DBOUT=$(helm template c charts/service -f "$C/c.yaml" 2>&1) || true
python3 - "$C/c.yaml" <<'PY' > "$C/db.txt" 2>"$C/db.err"
import subprocess, sys, yaml
out = subprocess.run(["helm", "template", "c", "charts/service", "-f", sys.argv[1]],
                     capture_output=True, text=True)
if out.returncode != 0:
    sys.exit(out.stderr)
docs = [d for d in yaml.safe_load_all(out.stdout) if d]
kinds = {d["kind"] for d in docs}
cluster = next((d for d in docs if d["kind"] == "Cluster"), None)
cron = next((d for d in docs if d["kind"] == "CronJob"), None)
print("kinds", sorted(kinds))
if cluster:
    print("clusterimage", cluster["spec"]["imageName"])
    print("clustercpu", (cluster["spec"].get("resources") or {}).get("requests", {}).get("cpu", ""))
    print("clustermemlimit", (cluster["spec"].get("resources") or {}).get("limits", {}).get("memory", ""))
if cron:
    print("cronimage", cron["spec"]["jobTemplate"]["spec"]["template"]["spec"]["containers"][0]["image"])
PY
if [ -s "$C/db.err" ]; then
  bad "chart: a declared database does not render as parseable YAML ($(head -1 "$C/db.err"))"
else
  grep -q "^kinds .*'Cluster'" "$C/db.txt" \
    && ok "chart: a declared database renders a CloudNativePG Cluster" \
    || bad "chart: a declared database rendered no Cluster"
  grep -q "^kinds .*'CronJob'" "$C/db.txt" \
    && ok "chart: a declared database renders its nightly dump job" \
    || bad "chart: a declared database rendered no backup CronJob"
  # The quota in every project namespace names requests.cpu and limits.memory, and a quota
  # that names a resource makes it mandatory for every pod in that namespace. A Cluster
  # without them is admitted by the operator and then rejected pod by pod, surfacing as a
  # database that simply never starts.
  { grep -q "^clustercpu .\+" "$C/db.txt" && grep -q "^clustermemlimit .\+" "$C/db.txt"; } \
    && ok "chart: the database declares resources, as the namespace quota requires" \
    || bad "chart: the database has no resource requests — the quota will reject its pods"
  # pg_dump refuses to dump a server newer than itself. One pinned image for both makes
  # that mismatch unrepresentable; two references make it a matter of remembering.
  CI=$(sed -n 's/^clusterimage //p' "$C/db.txt"); BI=$(sed -n 's/^cronimage //p' "$C/db.txt")
  [ -n "$CI" ] && [ "$CI" = "$BI" ] \
    && ok "chart: the dump job runs the same Postgres image as the server" \
    || bad "chart: dump job image ($BI) differs from the server image ($CI) — pg_dump may be older"
fi
# Identity + injection (Phase 4): every container carries the OTel identity envs (the
# join key across signals — inconsistency breaks correlation SILENTLY), and a declared
# language becomes the operator's inject annotation on the POD template, both kinds.
printf 'project: c\nenv: prod\nhostnames: [c.example.org]\ncomponents:\n  - {name: api, kind: web, image: img, tag: t1, port: 80, path: /, language: python}\n  - {name: j, kind: cron, image: img, schedule: "0 3 * * *", language: python}\n' > "$C/c.yaml"
python3 - "$C/c.yaml" <<'PY' > "$C/otel.txt" 2>&1
import subprocess, sys, yaml
out = subprocess.run(["helm", "template", "c", "charts/service", "-f", sys.argv[1]],
                     capture_output=True, text=True)
if out.returncode != 0:
    sys.exit(out.stderr)
docs = [d for d in yaml.safe_load_all(out.stdout) if d]
dep = next(d for d in docs if d["kind"] == "Deployment")
cron = next(d for d in docs if d["kind"] == "CronJob")
denv = {e["name"]: e.get("value") for e in dep["spec"]["template"]["spec"]["containers"][0]["env"]}
print("svcname", denv.get("OTEL_SERVICE_NAME") == "c-api")
print("attrs", denv.get("OTEL_RESOURCE_ATTRIBUTES") == "service.namespace=c,deployment.environment.name=prod,service.version=t1")
print("dep_inject", dep["spec"]["template"]["metadata"].get("annotations", {}).get("instrumentation.opentelemetry.io/inject-python") == "true")
print("cron_inject", cron["spec"]["jobTemplate"]["spec"]["template"]["metadata"].get("annotations", {}).get("instrumentation.opentelemetry.io/inject-python") == "true")
PY
for k in svcname attrs dep_inject cron_inject; do
  grep -q "^$k True" "$C/otel.txt" \
    && ok "chart: OTel $k is rendered correctly" \
    || bad "chart: OTel $k wrong or missing ($(head -1 "$C/otel.txt"))"
done
helm template c charts/service --set project=c --set env=prod --set 'hostnames[0]=c.example.org' --set 'components[0].name=api' --set 'components[0].kind=web' --set 'components[0].image=img' --set 'components[0].port=80' --set 'components[0].path=/' 2>/dev/null \
  | grep -q "instrumentation.opentelemetry.io" \
  && bad "chart: inject annotation rendered with NO language declared" \
  || ok "chart: no language, no inject annotation (the floor covers it instead)"

# WAL archiving renders when (and ONLY when) the generator hands the chart an s3 block.
# Both directions matter: with s3 the Cluster must carry barman + a ScheduledBackup + the
# orbit-s3 Secret, and without it the render must be byte-identical to the
# bucket-less platform this chart also serves.
printf '%s' "$DB_VALS" > "$C/c.yaml"
printf 'secrets: [{name: orbit-s3, keys: [access-key, secret-key]}]\ns3: {bucket: testbucket, endpoint: https://s3.example.invalid}\n' >> "$C/c.yaml"
python3 - "$C/c.yaml" <<'PY' > "$C/wal.txt" 2>&1
import subprocess, sys, yaml
out = subprocess.run(["helm", "template", "c", "charts/service", "-f", sys.argv[1]],
                     capture_output=True, text=True)
if out.returncode != 0:
    sys.exit(out.stderr)
docs = [d for d in yaml.safe_load_all(out.stdout) if d]
cluster = next((d for d in docs if d["kind"] == "Cluster"), None)
store = next((d for d in docs if d["kind"] == "ObjectStore"), None)
bar = ((store or {}).get("spec") or {}).get("configuration") or {}
print("dest", bar.get("destinationPath", ""))
# The credential must point at THE secret the chart also renders — a barman block
# naming some other secret renders clean and archives nothing, caught only post-deploy.
creds = bar.get("s3Credentials") or {}
print("credname", {(creds.get("accessKeyId") or {}).get("name"),
                   (creds.get("secretAccessKey") or {}).get("name")} == {"orbit-s3"})
# The Cluster must reference the SAME ObjectStore the chart renders, as a WAL archiver —
# a plugins block naming a different store archives into nothing.
plugs = (cluster or {}).get("spec", {}).get("plugins") or []
print("plug", any(p.get("name") == "barman-cloud.cloudnative-pg.io" and p.get("isWALArchiver")
                  and (p.get("parameters") or {}).get("barmanObjectName") == (store or {}).get("metadata", {}).get("name")
                  for p in plugs))
print("sched", any(d["kind"] == "ScheduledBackup" and d["spec"].get("immediate")
                   and d["spec"].get("method") == "plugin"
                   and (d["spec"].get("cluster") or {}).get("name") == "c-db" for d in docs))
print("podmon", ((cluster or {}).get("spec", {}).get("monitoring") or {}).get("enablePodMonitor") is True)
# The switch, positively asserted: with s3, the pg_dump machinery must be GONE — both
# paths rendering at once would double-back-up and keep the PVC the switch exists to shed.
print("dumpgone", not any("db-backup" in (d.get("metadata", {}).get("name") or "") for d in docs))
PY
grep -q "^dest s3://testbucket/cnpg/c-prod" "$C/wal.txt" \
  && ok "chart: with s3 set, the database WAL-archives to the platform bucket" \
  || bad "chart: s3 set but no barmanObjectStore rendered ($(head -1 "$C/wal.txt"))"
grep -q "^credname True" "$C/wal.txt" \
  && ok "chart: barman's credentials point at the orbit-s3 Secret the chart itself renders" \
  || bad "chart: barman names a credential Secret other than orbit-s3 — archiving would fail post-deploy"
grep -q "^plug True" "$C/wal.txt" \
  && ok "chart: the Cluster references the rendered ObjectStore as its WAL archiver" \
  || bad "chart: the Cluster's plugin block does not match the rendered ObjectStore"
grep -q "^sched True" "$C/wal.txt" \
  && ok "chart: an immediate plugin-method ScheduledBackup targets the right Cluster" \
  || bad "chart: no immediate plugin-method ScheduledBackup targeting c-db rendered"
grep -q "^podmon True" "$C/wal.txt" \
  && ok "chart: archiving enables the PodMonitor that feeds the WAL-stall alert" \
  || bad "chart: archiving without a PodMonitor — a dead archiver would fill the disk unseen"
grep -q "^dumpgone True" "$C/wal.txt" \
  && ok "chart: with s3, the pg_dump path does not render (the switch, not a duplicate)" \
  || bad "chart: s3 set but pg_dump artifacts still render — both backup paths at once"
printf '%s' "$DB_VALS" > "$C/c.yaml"
helm template c charts/service -f "$C/c.yaml" 2>/dev/null | grep -q "kind: ObjectStore\|kind: ScheduledBackup\|barman-cloud" \
  && bad "chart: WAL archiving rendered WITHOUT an s3 block — bucket-less instances just broke" \
  || ok "chart: without s3, the render carries no barman objects (bucket-less instances unchanged)"
# The generated Secret is auto-declared, but only when a database is actually declared.
printf 'project: c\nenv: prod\nhostnames: [c.example.org]\ncomponents:\n  - {name: api, kind: web, image: img, port: 80, path: /, env: {PGHOST: {secret: c-db-app, key: host}}}\n' > "$C/c.yaml"
helm template c charts/service -f "$C/c.yaml" >/dev/null 2>&1 \
  && bad "chart: a component referenced c-db-app with no database declared, and rendered anyway" \
  || ok "chart: referencing the database Secret without a database refuses to render"

# --- the graceful roll (departure edge) --------------------------------------
# Measured live on a tenant web service 2026-08-30 at 1s resolution: a RollingUpdate with
# maxUnavailable:0 produced ~3s of 502 at Traefik, roughly half of requests, while the
# NEW pod's own logs recorded nothing wrong. Both pods were Ready across the window and
# the pool had two servers, so half-failing means exactly one server was dead — the OLD
# one, which had already taken SIGTERM (maxUnavailable:0 scales the old pod down the
# instant the new one is Ready, so the two edges are the same second by construction)
# while Traefik's config reload had not yet dropped it. A pod leaves the EndpointSlice
# and leaves a router's pool at two different times, and the app dies at the first.
#
# Every row below is parsed out of the render, never grepped: the fields land in a pod
# template, and a preStop hook in the wrong place or on the wrong container renders
# green, applies clean, and drains nothing.
DRAIN_VALS='project: c
env: prod
hostnames: [c.example.org]
components:
  - {name: web, kind: web, image: img, port: 8080, path: /, probePath: /healthz}
  - {name: only, kind: web, image: img, port: 8080, path: /o, singleton: true}
  - {name: wk, kind: worker, image: img}
  - {name: job, kind: cron, image: img, schedule: "0 3 * * *"}
'
printf '%s' "$DRAIN_VALS" > "$C/c.yaml"
python3 - "$C/c.yaml" <<'PY' > "$C/drain.txt" 2>&1
import subprocess, sys, yaml
out = subprocess.run(["helm", "template", "c", "charts/service", "-f", sys.argv[1]],
                     capture_output=True, text=True)
if out.returncode != 0:
    sys.exit(out.stderr)
pods = {}
for d in (x for x in yaml.safe_load_all(out.stdout) if x):
    if d["kind"] == "Deployment":
        pods[d["metadata"]["name"]] = d["spec"]["template"]["spec"]
    elif d["kind"] == "CronJob":
        pods[d["metadata"]["name"]] = d["spec"]["jobTemplate"]["spec"]["template"]["spec"]
for name, spec in sorted(pods.items()):
    life = (spec["containers"][0].get("lifecycle") or {}).get("preStop") or {}
    print(name,
          "grace=%s" % spec.get("terminationGracePeriodSeconds"),
          "sleep=%s" % ((life.get("sleep") or {}).get("seconds")),
          "exec=%s" % ("exec" in life))
PY
if grep -q "^c-web " "$C/drain.txt"; then
  grep -q "^c-web grace=30 sleep=5 exec=False" "$C/drain.txt" \
    && ok "chart: a rolling web component drains — preStop sleep 5s, grace 30s" \
    || bad "chart: rolling web component has no drain ($(grep '^c-web ' "$C/drain.txt"))"
  # The exec form is not a stylistic choice. These images are distroless and run as
  # 65532: most have no shell and several have no /bin/sleep, so `exec: [sh, -c, sleep 5]`
  # fails, the kubelet logs FailedPreStopHook, the pod dies immediately — and the hook
  # reads as present in the manifest while doing nothing at all.
  grep -q "^c-web .*exec=False" "$C/drain.txt" \
    && ok "chart: the preStop hook is the native sleep action, not an exec into a shell" \
    || bad "chart: preStop uses exec — distroless images have no shell, so it never runs"
  # The blast radius is the point. A preStop sleep on a worker or a CronJob pod delays
  # every termination on the platform and protects nothing: no load balancer holds a
  # reference to them.
  grep -q "^c-wk grace=None sleep=None" "$C/drain.txt" \
    && ok "chart: a worker gets no drain (nothing load-balances over it)" \
    || bad "chart: a worker was given a drain ($(grep '^c-wk ' "$C/drain.txt"))"
  grep -q "^c-job grace=None sleep=None" "$C/drain.txt" \
    && ok "chart: a CronJob pod gets no drain" \
    || bad "chart: a CronJob pod was given a drain ($(grep '^c-job ' "$C/drain.txt"))"
  # A singleton is Recreate: Kubernetes deletes the old pod, waits for it to be GONE,
  # and only then creates the new one. A sleep does not shrink that outage, it moves it
  # later and makes the roll longer.
  grep -q "^c-only grace=None sleep=None" "$C/drain.txt" \
    && ok "chart: a Recreate singleton gets no drain (there is no second pod to serve)" \
    || bad "chart: a Recreate singleton was given a drain ($(grep '^c-only ' "$C/drain.txt"))"
else
  bad "chart: the drain fixture failed to render ($(head -1 "$C/drain.txt"))"
fi
# The grace period is ONE wall clock from delete to SIGKILL, covering the sleep AND the
# app's shutdown after SIGTERM. A grace period that does not exceed the sleep kills the
# pod during its own drain — the exact failure the drain exists to prevent, wearing a
# config that looks correct.
printf '%s' "$DRAIN_VALS" > "$C/c.yaml"
printf 'drain: {preStopSeconds: 30, gracePeriodSeconds: 30}\n' >> "$C/c.yaml"
helm template c charts/service -f "$C/c.yaml" >/dev/null 2>&1 \
  && bad "chart: grace period equal to the preStop sleep rendered — the pod is SIGKILLed mid-drain" \
  || ok "chart: a grace period that does not exceed the preStop sleep is refused"
# The values reach a pod spec. An out-of-range or non-integer sleep is refused by the API
# server at APPLY time, which under auto-sync is a chart that renders green and a
# Deployment that silently stops updating.
printf '%s' "$DRAIN_VALS" > "$C/c.yaml"
printf 'drain: {preStopSeconds: "5s"}\n' >> "$C/c.yaml"
helm template c charts/service -f "$C/c.yaml" >/dev/null 2>&1 \
  && bad "chart: a non-integer preStopSeconds reached a pod spec" \
  || ok "chart: preStopSeconds is schema-constrained to an integer"
printf '%s' "$DRAIN_VALS" > "$C/c.yaml"
printf 'drain: {preStopSeconds: 0}\n' >> "$C/c.yaml"
helm template c charts/service -f "$C/c.yaml" >/dev/null 2>&1 \
  && bad "chart: preStopSeconds 0 accepted — a hook that renders as protection and is none" \
  || ok "chart: preStopSeconds 0 is refused"
# Three times before this change, and once during it, a wrong Helm trim glued or blanked
# a line for EVERY workload on the platform. podSpec is nindent'ed by its caller, so the
# symptom of the drain block's own trim bug was an indented blank line in every pod spec
# — worker, cron and singleton included, none of which this change may touch.
printf '%s' "$DRAIN_VALS" > "$C/c.yaml"
helm template c charts/service -f "$C/c.yaml" 2>/dev/null | grep -qE '^[[:space:]]+$' \
  && bad "chart: a pod spec carries an indented blank line — a Helm trim regressed" \
  || ok "chart: no whitespace-only lines in the render (the trim is right)"

# --- the undeclared-port blackhole -------------------------------------------
# Reproduced live 2026-08-31 from a pod in orbit-agents-prod: `ClusterIP:8080` timed out
# at 8002 ms while `podIP:8080` and `ClusterIP:80` both returned 200. Nothing was broken.
# kube-proxy installs DNAT for DECLARED service ports only; a SYN to a ClusterIP on a port
# the Service does not declare matches no KUBE-SERVICES rule, and 10.43.0.0/16 has no route
# and no host, so the packet is blackholed and TCP retransmits until the caller gives up.
# A denial on this cluster fails in 0 ms — so the one symptom that is NOT a network policy
# is the one this produces, and it cost a full investigation before it was named.
# The fix is to declare the container's own number too, so the address a caller reaches for
# after reading the pod spec works instead of hanging. Parsed, never grepped: a port in the
# wrong Service, or a duplicate number, renders green and is refused at apply.
PORTS_VALS='project: c
env: prod
hostnames: [c.example.org]
components:
  - {name: web, kind: web, image: img, port: 8080, path: /}
  - {name: same, kind: web, image: img, port: 9000, path: /s, metrics: {port: 9000}}
  - {name: split, kind: web, image: img, port: 8080, path: /sp, metrics: {port: 9102}}
  - {name: eighty, kind: web, image: img, port: 80, path: /e}
  - {name: wk, kind: worker, image: img, metrics: {port: 8080}}
  - {name: job, kind: cron, image: img, schedule: "0 3 * * *"}
'
printf '%s' "$PORTS_VALS" > "$C/c.yaml"
python3 - "$C/c.yaml" <<'PY' > "$C/ports.txt" 2>&1
import subprocess, sys, yaml
out = subprocess.run(["helm", "template", "c", "charts/service", "-f", sys.argv[1]],
                     capture_output=True, text=True)
if out.returncode != 0:
    sys.exit(out.stderr)
svcs, mons, ings = {}, {}, []
for d in (x for x in yaml.safe_load_all(out.stdout) if x):
    if d["kind"] == "Service":
        svcs[d["metadata"]["name"]] = d["spec"]["ports"]
    elif d["kind"] == "ServiceMonitor":
        mons[d["metadata"]["name"]] = d["spec"]["endpoints"]
    elif d["kind"] == "Ingress":
        for rule in d["spec"]["rules"]:
            for p in rule["http"]["paths"]:
                ings.append(p["backend"]["service"])
for name, ports in sorted(svcs.items()):
    # name:port->targetPort triples, plus whether any NUMBER repeats (the API server
    # refuses a Service that declares one number twice, and Argo then stalls SyncFailed).
    trip = ",".join("%s:%s->%s" % (p["name"], p["port"], p["targetPort"]) for p in ports)
    dup = len({p["port"] for p in ports}) != len(ports) or len({p["name"] for p in ports}) != len(ports)
    print("svc", name, trip, "dup=%s" % dup)
for name, eps in sorted(mons.items()):
    print("mon", name, ",".join(str(e["port"]) for e in eps))
for b in ings:
    # The backend names a port by NAME or by NUMBER; print whichever it used.
    port = b["port"]
    print("ing", b["name"], port.get("name", port.get("number")))
PY
if grep -q "^svc c-web " "$C/ports.txt"; then
  # The whole point: 80 AND the container's own number, both landing on the container.
  grep -q "^svc c-web http:80->8080,http-direct:8080->8080 dup=False$" "$C/ports.txt" \
    && ok "chart: a web Service declares both 80 and the container's own port" \
    || bad "chart: web Service ports wrong ($(grep '^svc c-web ' "$C/ports.txt"))"
  # A component that already listens on 80 must NOT get the number twice.
  grep -q "^svc c-eighty http:80->80 dup=False$" "$C/ports.txt" \
    && ok "chart: a container already on 80 gets one port, not a duplicate" \
    || bad "chart: container port 80 produced a duplicate ($(grep '^svc c-eighty ' "$C/ports.txt"))"
  # Metrics on the app's own port (the common case, and what the intake does) already
  # shared a
  # number with the new port — the metrics entry must stay suppressed, not collide.
  grep -q "^svc c-same http:80->9000,http-direct:9000->9000 dup=False$" "$C/ports.txt" \
    && ok "chart: metrics on the app's own port does not double-declare that number" \
    || bad "chart: same-number metrics collided ($(grep '^svc c-same ' "$C/ports.txt"))"
  # A metrics port of its own is untouched by this and keeps its own name.
  grep -q "^svc c-split http:80->8080,http-direct:8080->8080,metrics:9102->9102 dup=False$" "$C/ports.txt" \
    && ok "chart: a separate metrics port is unchanged alongside the added http port" \
    || bad "chart: separate metrics port changed ($(grep '^svc c-split ' "$C/ports.txt"))"
  # Blast radius. A worker's Service exists only to give the ServiceMonitor Endpoints; it
  # has no http port and must not grow one, and a CronJob gets no Service at all.
  grep -q "^svc c-wk metrics:8080->8080 dup=False$" "$C/ports.txt" \
    && ok "chart: a metrics-only Service is unchanged (no port 80 invented)" \
    || bad "chart: a metrics-only Service changed ($(grep '^svc c-wk ' "$C/ports.txt"))"
  grep -q "^svc c-job " "$C/ports.txt" \
    && bad "chart: a CronJob component grew a Service" \
    || ok "chart: a CronJob component still gets no Service"
  # The ServiceMonitor selects by port NAME. Renaming or reordering here would silently
  # stop every scrape while the render stays green.
  grep -q "^mon c-same http$" "$C/ports.txt" && grep -q "^mon c-split metrics$" "$C/ports.txt" \
    && ok "chart: ServiceMonitors still scrape by the same port names" \
    || bad "chart: a ServiceMonitor's port name moved ($(grep '^mon ' "$C/ports.txt" | tr '\n' ' '))"
  # Public traffic must keep entering on 80. If the Ingress ever named the direct port,
  # every request would bypass the one address the platform documents.
  grep -q "^ing .* http$" "$C/ports.txt" && ! grep -qE "^ing .* (8080|9000)$" "$C/ports.txt" \
    && ok "chart: the Ingress still routes to the named http port, not the direct one" \
    || bad "chart: Ingress backend port moved ($(grep '^ing ' "$C/ports.txt" | tr '\n' ' '))"
else
  bad "chart: the service-ports fixture failed to render ($(head -1 "$C/ports.txt"))"
fi
# The one remaining way to declare a number twice: metrics on 80 under a web component
# listening elsewhere. That renders a Service the API server refuses, which under auto-sync
# is a green render and an Application stuck SyncFailed.
printf 'project: c\nenv: prod\nhostnames: [c.example.org]\ncomponents:\n  - {name: web, kind: web, image: img, port: 8080, path: /, metrics: {port: 80}}\n' > "$C/c.yaml"
helm template c charts/service -f "$C/c.yaml" >/dev/null 2>&1 \
  && bad "chart: metrics on 80 beside a web port rendered a Service with a duplicate number" \
  || ok "chart: metrics on 80 beside a different web port is refused"
rm -rf "$C"

echo "== no credentials in generated manifests =="
# Generated Applications are committed in plaintext. A credential rendered into their
# values would be a credential in git — this is the check that keeps that true.
python3 scripts/no-credentials.py >/dev/null 2>&1 \
  && ok "generated manifests carry no credentials" \
  || bad "credential found in generated manifests ($(python3 scripts/no-credentials.py 2>&1 | grep -c '^credential in a generated manifest') finding(s) — run scripts/no-credentials.py to read them; a finding names a key, which is file text, so it is not repeated here)"
# And the check must still catch one. A credential scanner nobody has ever seen go red is
# indistinguishable from a scanner that does nothing — which is what the previous version
# effectively became, since it matched the key name `PGPASSWORD:` and would have been
# "fixed" by deleting it.
CRED=$(mktemp -d); cp -r clusters scripts "$CRED/" 2>/dev/null
printf 'apiVersion: v1\nkind: ConfigMap\nmetadata: {name: bait}\ndata:\n  PASSWORD: hunter2\n' \
  > "$CRED/clusters/apps/bait.yaml"
(cd "$CRED" && python3 scripts/no-credentials.py >/dev/null 2>&1) \
  && bad "the credential scanner did not notice a planted password" \
  || ok "the credential scanner still catches a planted password"
# ...and must not fire on the correct way to consume one.
rm -f "$CRED/clusters/apps/bait.yaml"
printf 'apiVersion: v1\nkind: Pod\nmetadata: {name: ok}\nspec:\n  env:\n    PGPASSWORD: {secret: x-db-app, key: password}\n' \
  > "$CRED/clusters/apps/pointer.yaml"
(cd "$CRED" && python3 scripts/no-credentials.py >/dev/null 2>&1) \
  && ok "the credential scanner accepts a Secret pointer" \
  || bad "the credential scanner rejects a Secret pointer — it will be deleted rather than trusted"
# A Certificate's secretName is a reference too, and naming one is how TLS works.
rm -f "$CRED/clusters/apps/pointer.yaml"
printf 'apiVersion: v1\nkind: Certificate\nmetadata: {name: ok}\nspec:\n  secretName: tenant-prod-dns-tls\n' \
  > "$CRED/clusters/apps/certref.yaml"
(cd "$CRED" && python3 scripts/no-credentials.py >/dev/null 2>&1) \
  && ok "the credential scanner accepts a Certificate's secretName" \
  || bad "the credential scanner rejects a Certificate secretName — TLS cannot be configured"
# ...and the exemption must be about the NAME only. A credential parked under one of
# those keys is still a credential, or the exemption is a hole rather than a rule.
printf 'apiVersion: v1\nkind: Certificate\nmetadata: {name: bad}\nspec:\n  secretName: "$2y$10$abcdefghijklmnopqrstuv"\n' \
  > "$CRED/clusters/apps/certref.yaml"
(cd "$CRED" && python3 scripts/no-credentials.py >/dev/null 2>&1) \
  && bad "a bcrypt hash hidden under secretName passed the scanner" \
  || ok "a credential parked under secretName is still caught by shape"
rm -f "$CRED/clusters/apps/certref.yaml"
rm -rf "$CRED"

echo "== chart guard rails =="
S=$(mktemp -d)
cat > "$S/dup.yaml" <<'EOF'
project: dup
env: prod
hostnames: [dup.example.org]
components:
  - {name: api, kind: web, image: x, port: 80, path: /}
  - {name: web, kind: web, image: yy, port: 80, path: /}
EOF
# Assert the MESSAGE, not the exit code. A guard test that accepts any non-zero exit also
# accepts a chart that is broken for every input — which is exactly how a previous fix
# commit froze all delivery while this line reported green.
DUPOUT=$(helm template dup charts/service -f "$S/dup.yaml" 2>&1); DUPRC=$?
if [ $DUPRC -eq 0 ]; then
  bad "two web components sharing a path rendered (should fail)"
elif echo "$DUPOUT" | grep -q "both claim path"; then
  ok "duplicate path refuses to render, with the intended message"
else
  bad "chart failed, but not on the duplicate-path guard: $(echo "$DUPOUT" | head -2 | tr '\n' ' ')"
fi

# The root guard, as a matrix rather than a single case. Both halves matter: a guard that
# rejects everything is as broken as one that rejects nothing, and the "renders" column is
# what catches a whitespace-trim regression that breaks the chart for every input. The
# supplementalGroups row is here because that clause was dead code for a release — Helm
# parses YAML ints as float64, so `has 0 $list` could never match.
guard() { # <label> <want: yes|no> <securityContext yaml>
  cat > "$S/g.yaml" <<EOF
project: g
env: prod
hostnames: [g.example.org]
components:
  - name: api
    kind: web
    image: x
    port: 80
    path: /
$3
EOF
  if helm template g charts/service -f "$S/g.yaml" >/dev/null 2>&1; then got=yes; else got=no; fi
  [ "$got" = "$2" ] && ok "root guard: $1" || bad "root guard: $1 (renders=$got, want $2)"
}
S=$(mktemp -d)
guard "plain component renders"          yes ""
guard "runAsUser 0 rejected"             no  "    securityContext: {runAsUser: 0}"
guard "runAsUser \"0\" as string rejected" no  "    securityContext: {runAsUser: \"0\"}"
guard "runAsNonRoot false rejected"      no  "    securityContext: {runAsNonRoot: false}"
guard "fsGroup 0 rejected"               no  "    securityContext: {fsGroup: 0}"
guard "supplementalGroups [0] rejected"  no  "    securityContext: {supplementalGroups: [0]}"
guard "supplementalGroups [1000,0] rejected" no "    securityContext: {supplementalGroups: [1000, 0]}"
guard "supplementalGroups [1000] allowed" yes "    securityContext: {supplementalGroups: [1000]}"
guard "runAsGroup 0 with non-zero uid allowed" yes "    securityContext: {runAsUser: 1001, runAsGroup: 0}"
guard "nginx uid 101 allowed"            yes "    securityContext: {runAsUser: 101}"
guard "postgres 999/999 allowed"         yes "    securityContext: {runAsUser: 999, fsGroup: 999}"

# values.schema.json. `helm template` does no Kubernetes name or enum validation on its own,
# so without the schema a typo'd kind renders an unreachable workload and an unvalidated
# component name reaches metadata.name unchanged.
schema() { # <label> <want: yes|no> <components yaml>
  printf 'project: s\nenv: prod\nhostnames: [s.example.org]\ncomponents:\n%s\n' "$3" > "$S/s.yaml"
  if helm template s charts/service -f "$S/s.yaml" >/dev/null 2>&1; then got=yes; else got=no; fi
  [ "$got" = "$2" ] && ok "schema: $1" || bad "schema: $1 (renders=$got, want $2)"
}
schema "valid web renders"          yes '  - {name: api, kind: web, image: x, port: 80, path: /}'
schema "traversal in name rejected" no  '  - {name: ../../evil, kind: web, image: x, port: 80, path: /}'
schema "unknown kind rejected"      no  '  - {name: api, kind: bogus, image: x, port: 80}'
schema "unknown class rejected"     no  '  - {name: api, kind: web, class: turbo, image: x, port: 80, path: /}'
schema "cron without schedule rejected" no '  - {name: job, kind: cron, image: x}'
# Was "metrics on a non-web rejected" — the schema forbade it because the chart rendered a
# ServiceMonitor with no Service. The chart now renders the Service, so the constraint moved
# to where it is still true rather than being deleted: a cron's pod is gone before a scrape.
schema "metrics on a scraped worker accepted" yes '  - {name: w, kind: worker, image: x, metrics: {port: 9090}}'
schema "metrics on a cron rejected"     no  '  - {name: j, kind: cron, image: x, schedule: "0 3 * * *", metrics: {port: 9090}}'
# Phase D.3 cron controls: enums and bounded ints, so an injected or typo'd value fails at
# the render gate rather than reaching the CronJob spec verbatim.
schema "valid cron controls accepted" yes '  - {name: j, kind: cron, image: x, schedule: "0 3 * * *", concurrencyPolicy: Forbid, restartPolicy: Never, backoffLimit: 0, activeDeadlineSeconds: 720}'
schema "a bogus concurrencyPolicy is rejected" no '  - {name: j, kind: cron, image: x, schedule: "0 3 * * *", concurrencyPolicy: Sometimes}'
schema "a bogus restartPolicy is rejected" no '  - {name: j, kind: cron, image: x, schedule: "0 3 * * *", restartPolicy: Always}'
schema "activeDeadlineSeconds 0 is rejected (would kill the Job instantly)" no '  - {name: j, kind: cron, image: x, schedule: "0 3 * * *", activeDeadlineSeconds: 0}'
rm -rf "$S"

if [ "$PREBOOT" = 1 ]; then
  ok "PRE-BOOT: live-cluster and node checks keyed off (no kubeconfig at $KUBECONFIG) — bootstrap arms them"
else
echo "== the checks below are only meaningful over a non-empty set =="
# Every "for each managed pod" assertion below passes trivially when the selector matches
# nothing — a typo in the label, a namespace pruned, an Application deleted, and the suite
# reports all-green over zero objects. Establish the population FIRST and fail if it
# shrank; a check that cannot fail is not a check.
# Expectations come from the catalog, not from any instance's remembered shape: a
# one-project org truthfully has one project namespace. What must never pass is
# FEWER than declared.
EXPECT_NS=$(python3 -c "
import yaml
try:
    cat = yaml.safe_load(open('catalog/projects.yaml')) or {}
    if not isinstance(cat, dict): raise ValueError('not a mapping')
except Exception as exc:
    print('UNREADABLE(%s)' % type(exc).__name__); raise SystemExit
n = 0
for p in cat.get('projects') or []:
    # Only self-hosted projects with components render namespaces; an external
    # card (hosted: external) or a component-less entry deliberately does not.
    if p.get('hosted', 'self') != 'self' or not p.get('components') or p.get('managed_elsewhere'): continue
    for env, e in (p.get('environments') or {}).items():
        if env == 'prod' or (e or {}).get('enabled'): n += 1
# The agent plane renders exactly one namespace (orbit-agents-prod) when any agent is
# enabled — count it, so a missing plane namespace reds rather than passing vacuously.
if any(isinstance(a, dict) and a.get('enabled', True) for a in (cat.get('agents') or [])):
    n += 1
print(n)" 2>/dev/null)
case "$EXPECT_NS" in ''|*[!0-9]*) EXPECT_NS=UNREADABLE;; esac
MANAGED=$(kubectl get pods -A -l app.kubernetes.io/managed-by=argocd --no-headers 2>/dev/null | wc -l)
if [ "$PROJCOUNT" = UNREADABLE ] || [ "$EXPECT_NS" = UNREADABLE ]; then
  bad "the catalog could not be read, so the managed-pod and project-namespace floors are UNKNOWN — $MANAGED managed pod(s) seen, nothing to compare them with"
elif [ "$PROJCOUNT" = 0 ]; then
  ok "zero projects declared — tenant-population checks keyed off (arm with the first project)"
else
[ "$MANAGED" -ge "$PROJCOUNT" ] && ok "found $MANAGED argocd-managed pods to assert over (>= $PROJCOUNT projects)" \
  || bad "only $MANAGED managed pods for $PROJCOUNT projects — resource-policy checks below are vacuous"
# Same for the platform apps: a loop that enumerates whatever exists lowers its pass
# count when something is pruned and raises no failure, so the count is asserted against
# a floor. The desk stores one inventory ConfigMap per applied platform app.
APPCOUNT=$(kubectl -n orbit get configmap -o name 2>/dev/null | grep -c '^configmap/orbit-platform-inventory-')
[ "$APPCOUNT" -ge 8 ] && ok "found $APPCOUNT applied platform apps (stored inventories; expected at least 8)" \
  || bad "only $APPCOUNT applied platform apps — one was pruned or never applied"
NSCOUNT=$(kubectl get ns -l orbit.local/project --no-headers 2>/dev/null | wc -l)
[ "$NSCOUNT" -ge "$EXPECT_NS" ] && ok "found $NSCOUNT project namespaces (catalog declares $EXPECT_NS)" \
  || bad "only $NSCOUNT project namespaces of $EXPECT_NS declared — one is missing"
fi  # PROJCOUNT population

echo "== resource policy in the live cluster =="
# Every container, not just containers[0]: a sidecar with a CPU limit would otherwise
# slip through a check that claims to cover "every managed pod".
#
# What this row asserts, and why it stopped asserting `request == limit` (2026-08-31):
#
#   no CPU limit          — unchanged, and the reason this row exists. CPU is
#                           compressible; a limit throttles its own pod on an idle
#                           machine and protects nobody.
#   BOTH numbers present  — a memory request AND a memory limit. This is NEW teeth, not
#                           a relaxation: the old equality test compared two empty
#                           strings for a container declaring NEITHER and passed it, so
#                           the one shape the row most needed to catch — an unbounded
#                           container — was its blind spot.
#   request <= limit      — replaces equality. Equality was the MECHANISM by which the
#                           mandatory limit was kept present and sane, never the goal;
#                           Phase RS (docs/phase-rs-measurements.md) already retired the
#                           "request == limit" convention for platform components on the
#                           grounds that a request is a scheduling reservation and a
#                           limit is what the kernel kills on. The service chart is now
#                           able to express the same split (`memoryLimit`), because the
#                           agent plane produced the first chart workload the convention
#                           could not describe honestly: ~14Mi idle, ~275Mi for the
#                           minute a task runs. One number is either a lie to the
#                           scheduler or an OOMKill.
#
# Compared as QUANTITIES, not strings: "1Gi" and "1024Mi" are the same reservation and
# "512Mi" sorts above "1Gi" lexically.
# Cluster listings are the API's text: they land in a private file, and only
# `ns/name`-shaped references (optionally `=value` / ` word` suffixed as the caller asks)
# come back, the rest COUNTED — a resource name is never an argument or a row verbatim.
shaped_refs(){ # <file> <mode: ref|eq|words|ns> ; prints shaped entries, "(+N not of a name shape)" when any
  python3 - "$1" "$2" <<'PY'
import re, sys
import shown as display   # the shared display rule (scripts/verify/shown.py)
NAME = r"[a-z0-9]([a-z0-9.-]{0,251}[a-z0-9])?"
NS = r"[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?"
mode = sys.argv[2]
pat = {"ref": rf"{NS}/{NAME}", "eq": rf"{NS}/{NAME}=[A-Za-z0-9.-]*", "words": rf"{NS}/{NAME}( [A-Za-z]*)*", "ns": NS, "name": NAME}[mode]
try: lines = [l.strip() for l in open(sys.argv[1], errors="replace").read().split() if l.strip()] if mode == "ns" \
        else [l.strip() for l in open(sys.argv[1], errors="replace").read().splitlines() if l.strip()]
except OSError: lines = []
good = [l for l in lines if re.fullmatch(pat, l) and not display.tokenish(l)]
odd = len(lines) - len(good)
print("\n".join(good) + ("\n(+%d not of a name shape, or of a credential's shape)" % odd if odd else ""))
PY
}
image_cmp(){ # <file holding the live image ref> <pin> -> match | mismatch | empty ; the live text never reaches the shell
  python3 -c 'import sys; v = open(sys.argv[1], errors="replace").read().strip(); print("match" if v and v == sys.argv[2] else ("empty" if not v else "mismatch"))' "$1" "$2" 2>/dev/null
  rm -f "$1"
}
with_project_namespaces(){ # <what the caller proves>; sets PNS (shaped names); rc 1 when the population cannot be trusted
  local f rc odd; f=$(mktemp)
  kubectl get ns -l orbit.local/project -o jsonpath='{.items[*].metadata.name}' > "$f" 2>/dev/null; rc=$?
  if [ "$rc" -ne 0 ]; then
    rm -f "$f"; PNS=""
    bad "the project namespaces could not be listed (kubectl rc=$rc) — $1 UNCHECKED, not proven"
    return 1
  fi
  PNS=$(shaped_refs "$f" ns | grep -v '^(+'); odd=$(shaped_refs "$f" ns | grep '^(+'); rm -f "$f"
  # an unshaped name is never iterated (it would become an argument) — but it is not
  # silently dropped either: the rows over it are UNCHECKED and say so
  [ -z "$odd" ] || bad "project namespace(s) $odd are skipped — $1 UNCHECKED for them"
  return 0
}
BADRES=$(kubectl get pods -A -l app.kubernetes.io/managed-by=argocd -o json 2>/dev/null \
  | python3 -c '
import json, re, sys
U = {"": 1, "k": 10**3, "M": 10**6, "G": 10**9, "T": 10**12, "P": 10**15, "E": 10**18,
     "Ki": 1024, "Mi": 1024**2, "Gi": 1024**3, "Ti": 1024**4, "Pi": 1024**5, "Ei": 1024**6}
def qty(s):
    m = re.fullmatch(r"([0-9]+(?:\.[0-9]+)?)([A-Za-z]*)", str(s))
    return float(m.group(1)) * U[m.group(2)] if m and m.group(2) in U else None
bad = []
try:
    pods = json.load(sys.stdin).get("items", [])
except Exception:
    print("QUERYERR"); sys.exit(0)
NS = re.compile(r"[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?")
NAME = re.compile(r"[a-z0-9]([a-z0-9.-]{0,251}[a-z0-9])?")
def shaped(v, pat): return v if isinstance(v, str) and pat.fullmatch(v) else "(unshaped)"
try:
  for pi, p in enumerate(pods):
    if not isinstance(p, dict) or not isinstance(p.get("metadata"), dict) or not isinstance(p.get("spec"), dict):
        raise ValueError("pod #%d is not of a Pod shape" % pi)
    # names are API text: shown only with the shape of a name (no apostrophe in this
    # program: it is single-quoted shell — round 18)
    ns, name = shaped(p["metadata"].get("namespace"), NS), shaped(p["metadata"].get("name"), NAME)
    for c in p["spec"].get("containers") or []:
        r = c.get("resources", {}) or {}
        req, lim = (r.get("requests") or {}), (r.get("limits") or {})
        where = "%s/%s/%s" % (ns, name, shaped(c.get("name"), NAME))
        if lim.get("cpu"):
            bad.append(where + "(cpu-limit)")
        if not req.get("memory") or not lim.get("memory"):
            bad.append(where + "(memory request/limit missing)")
            continue
        rq, lq = qty(req["memory"]), qty(lim["memory"])
        if rq is None or lq is None:
            bad.append(where + "(unparseable memory quantity)")
        elif rq > lq:
            bad.append("%s(request %s > limit %s)" % (where, req["memory"], lim["memory"]))
except Exception:                     # a document the reader did not model is NOT "no offender"
    print("QUERYERR"); sys.exit(0)
print(" ".join(bad))
'); BADRC=$?
if [ "$BADRES" = "QUERYERR" ] || [ "$BADRC" -ne 0 ]; then
  bad "could not read managed pods as JSON — the resource policy is UNCHECKED, not proven"
elif [ -z "$BADRES" ]; then
  ok "every managed pod: no CPU limit, and a memory request and limit that are both present with request <= limit"
else
  bad "policy violated by: $BADRES"
fi
# Per pod, not "does any pod in the cluster have one" — the concatenated form passed as
# long as a single unrelated pod matched.
# Each query's STATUS is read before its output: a query that failed (forbidden, an API
# hiccup) prints nothing, and nothing must never read as "no offender".
PRIOF=$(mktemp)
if kubectl get pods -A -l app.kubernetes.io/managed-by=argocd \
  -o jsonpath='{range .items[*]}{.metadata.namespace}/{.metadata.name}={.spec.priorityClassName}{"\n"}{end}' > "$PRIOF" 2>/dev/null; then
  NOPRIO=$(shaped_refs "$PRIOF" eq | grep -v '=orbit-' | cut -d= -f1 | tr '\n' ' ')
  [ -z "${NOPRIO// /}" ] && ok "every managed pod has an orbit PriorityClass" || bad "no PriorityClass on: $NOPRIO"
else
  bad "the managed pods could not be listed for their PriorityClass (kubectl failed) — UNCHECKED, not proven"
fi
# Skip Succeeded pods: a CronJob that completed correctly reports Ready=False forever, so
# including them turns the first successful scheduled run into a permanent false alarm.
rm -f "$PRIOF"
READYF=$(mktemp)
if kubectl get pods -A -l app.kubernetes.io/managed-by=argocd \
  -o jsonpath='{range .items[*]}{.metadata.namespace}/{.metadata.name} {.status.phase} {.status.conditions[?(@.type=="Ready")].status}{"\n"}{end}' > "$READYF" 2>/dev/null; then
  NOTREADY=$(shaped_refs "$READYF" words | awk '/^\(\+/ {print; next} NF && $2 != "Succeeded" && $3 != "True" {print $1}' | tr '\n' ' ')
  [ -z "${NOTREADY// /}" ] && ok "every managed pod is Ready" || bad "not Ready: $NOTREADY"
else
  bad "the managed pods could not be listed for readiness (kubectl failed) — UNCHECKED, not proven"
fi
rm -f "$READYF"
JOBF=$(mktemp)
# by the TERMINAL condition: status.failed counts attempts, and a Job that retried once
# and then completed carries failed=1 with Complete=True — not a failed Job
if kubectl get jobs -A -o jsonpath='{range .items[*]}{.metadata.namespace}/{.metadata.name} {.status.conditions[?(@.type=="Failed")].status}{"\n"}{end}' > "$JOBF" 2>/dev/null; then
  FAILEDJOBS=$(shaped_refs "$JOBF" words | awk '/^\(\+/ {print; next} NF && $2 == "True" {print $1}' | tr '\n' ' ')
  [ -z "${FAILEDJOBS// /}" ] && ok "no failed Jobs (a cron that always fails is otherwise invisible)" \
    || bad "failed Jobs: $FAILEDJOBS"
else
  bad "the Jobs could not be listed (kubectl failed) — 'no failed Jobs' is UNCHECKED, not proven"
fi
rm -f "$JOBF"

echo "== applied tree =="
# A stored inventory means the manifests applied, NOT that anything runs (the pod rows
# above are what prove that). What this row holds is the count: every declared pair has
# a tenant inventory, so a pair the daemon never applied, or pruned, is red here rather
# than silently absent from every loop below.
if [ "$PROJCOUNT" = 0 ]; then
  ok "zero projects declared — the applied-tree row keys off"
else
  # By name, pair by pair: a prefix count would let an unrelated inventory (the monitors
  # unit writes one too) stand in for a missing tenant. The pairs are the generator's
  # (self-hosted, with components, prod or an enabled environment).
  INVMISS=""; INVSEEN=0; INVODD=0
  # A pair name is built from catalog text and becomes a kubectl ARGUMENT: only a
  # <label>-<label> of the label shape is used; anything else is counted, never used.
  # the catalog is read inside the program: a parse error prints UNREADABLE and nothing
  # of the parser's message (PyYAML repeats an unknown tag verbatim) — and an unreadable
  # catalog is UNCHECKED, never "every pair has an inventory (0)" (round 19)
  PAIRS=$(python3 -c "
import re, sys, yaml
LABEL = re.compile(r'[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?')
try:
    cat = yaml.safe_load(open('catalog/projects.yaml')) or {}
    if not isinstance(cat, dict): raise ValueError('not a mapping')
except Exception:
    print('UNREADABLE'); sys.exit(0)
for p in cat.get('projects') or []:
    if not isinstance(p, dict) or p.get('hosted', 'self') != 'self' or not p.get('components') or p.get('managed_elsewhere'): continue
    for env, e in (p.get('environments') or {}).items():
        if env == 'prod' or (e or {}).get('enabled'):
            pid = p.get('id')
            print('%s-%s' % (pid, env) if isinstance(pid, str) and isinstance(env, str) and LABEL.fullmatch(pid) and LABEL.fullmatch(env) else 'UNSHAPED')" 2>/dev/null || echo UNREADABLE)
  if [ "$PAIRS" = UNREADABLE ]; then
    bad "catalog/projects.yaml could not be read (the parser's message is not repeated) — the tenant inventory rows are UNCHECKED"
    PAIRS=""
  fi
  for pair in $PAIRS; do
    INVSEEN=$((INVSEEN+1))
    [ "$pair" = UNSHAPED ] && { INVODD=$((INVODD+1)); continue; }
    kubectl -n orbit get configmap "orbit-inventory-$pair" >/dev/null 2>&1 || INVMISS="$INVMISS $pair"
  done
  [ "$INVODD" -gt 0 ] && bad "$INVODD declared pair(s) have an id or environment that is not a label (not repeated) — their inventory row cannot run"
  if [ -n "$PAIRS" ]; then
    [ -z "$INVMISS" ] && ok "every declared pair has a tenant inventory ($INVSEEN) — the daemon applied each" \
      || bad "declared pair(s) with no tenant inventory:$INVMISS — the daemon has not applied them, or pruned them"
  fi
fi
# Quota rejections never create a pod at all, so they are invisible to `get pods`; the
# failure lives only on the ReplicaSet.
RFF=$(mktemp)
if kubectl get rs -A -o jsonpath='{range .items[?(@.status.conditions[0].type=="ReplicaFailure")]}{.metadata.namespace}/{.metadata.name}{"\n"}{end}' > "$RFF" 2>/dev/null; then
  RF=$(shaped_refs "$RFF" ref | tr '\n' ' ')
  [ -z "${RF// /}" ] && ok "no ReplicaFailure (nothing silently blocked by quota)" || bad "ReplicaFailure on: $RF"
else
  bad "the ReplicaSets could not be listed (kubectl failed) — 'no ReplicaFailure' is UNCHECKED, not proven"
fi
rm -f "$RFF"
if [ "$PROJCOUNT" = 0 ]; then
  ok "zero projects declared — per-project quota check keyed off"
else
  if with_project_namespaces "the per-project quota row"; then
    if [ -z "$PNS" ]; then
      bad "$PROJCOUNT project(s) declared but zero project namespaces listed — the per-project quota row has no population to check"
    else
      QMISS=""
      for ns in $PNS; do
        kubectl -n "$ns" get resourcequota orbit-quota >/dev/null 2>&1 || QMISS="$QMISS $ns"
      done
      [ -z "$QMISS" ] && ok "every project namespace carries its ResourceQuota" \
        || bad "namespaces missing orbit-quota:$QMISS"
    fi
  fi
fi

echo "== WAL archiving (keyed off the instance's declared bucket) =="
# Expectations derive from declarations, never reachability: an instance without an
# objectStorage bucket truthfully archives nothing, and one WITH a bucket must archive
# every prod database — a Cluster whose archiver is failing loses the PITR promise
# silently while every dashboard stays green.
# The selector must FAIL LOUDLY, not key off, when it cannot answer: a swallowed
# exception here once meant "config unreadable" printed the same empty string as "no
# bucket", and the whole section keyed off green over a dead archiver. NONE is an
# explicit sentinel; empty output is an error, never a pass.
WAL_DBS=$(python3 -c "
import yaml
try:
    inst = yaml.safe_load(open('instance/config.yaml')) or {}
    if not isinstance(inst, dict): raise ValueError('not a mapping')
    if not (((inst.get('objectStorage') or {}).get('buckets') or {}).get('platform')):
        print('NONE'); raise SystemExit
    cat = yaml.safe_load(open('catalog/projects.yaml')) or {}
    if not isinstance(cat, dict): raise ValueError('not a mapping')
except SystemExit: raise
except Exception:
    raise SystemExit      # empty output: the row below reads it as unreadable; the parser's message stays out
# Mirror the generator's own skip rules: only self-hosted component-bearing projects
# render a prod namespace at all — anything else here is a phantom that reds forever.
# ... AND only a DECLARED prod environment renders a prod database (the generator
# iterates declared environments and enables WAL for prod), and a managed-elsewhere
# project renders nothing here.
import re
LABEL = re.compile(r'[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?')
# an id lands in a namespace and a label selector on a kubectl command line: only a
# label-shaped one goes there; the rest are counted (UNSHAPED), never repeated
ids = [p['id'] if isinstance(p.get('id'), str) and LABEL.fullmatch(p['id']) else 'UNSHAPED'
       for p in cat.get('projects') or []
       if isinstance(p, dict)
       and p.get('database') and p.get('hosted', 'self') == 'self' and p.get('components')
       and not p.get('managed_elsewhere') and 'prod' in (p.get('environments') or {})]
print(' '.join(ids) if ids else 'NONE')" 2>/dev/null)
if [ -z "${WAL_DBS// /}" ]; then
  bad "WAL selector produced nothing — instance/catalog config unreadable, checks below are vacuous"
elif [ "$WAL_DBS" = "NONE" ]; then
  ok "no bucket declared (or no prod databases) — WAL checks keyed off"
else
  for p in $WAL_DBS; do
    if [ "$p" = UNSHAPED ]; then
      bad "a prod-database project in catalog/projects.yaml has an id that is not a label (not repeated) — its WAL and backup rows cannot run"
      continue
    fi
    # Ground truth from pg_stat_archiver, NOT the ContinuousArchiving condition: with a
    # broken archive command the condition was observed reading True while zero bytes
    # reached the bucket (2026-08-27 — the in-tree/-standard-image trap). archived_count
    # proves the pipe has worked; the .ready backlog proves it works NOW. Not archival
    # AGE: archive_timeout forces a switch only when new WAL exists, so an idle database
    # legitimately goes quiet for hours — age-keyed, this check false-reds every quiet
    # night (observed on an idle leaderboard 34 minutes after its last write).
    PRIMF=$(mktemp)
    kubectl -n "$p-prod" get pod -l cnpg.io/cluster="$p-db",cnpg.io/instanceRole=primary \
      -o jsonpath='{.items[0].metadata.name}' > "$PRIMF" 2>/dev/null
    PRIMARY=$(shaped_refs "$PRIMF" name | grep -v '^(+'); rm -f "$PRIMF"
    [ -n "$PRIMARY" ] || bad "$p-db: no primary pod with a pod-shaped name was listed — the WAL archiver row below cannot be read"
    # the query's output is the database's text: a private file, and only three
    # non-negative integers come back out (anything else is `unreadable`, not repeated)
    ARCHF=$(mktemp)
    kubectl -n "$p-prod" exec "$PRIMARY" -c postgres -- psql -At -F'|' -c \
      "select archived_count, failed_count, (select count(*) from pg_ls_archive_statusdir() where name like '%.ready') from pg_stat_archiver" > "$ARCHF" 2>/dev/null
    read -r A_COUNT A_FAILED A_READY < <(python3 - "$ARCHF" <<'PY'
import re, sys
try: line = open(sys.argv[1], errors="replace").read().strip().split("\n")[0]
except OSError: line = ""
m = re.fullmatch(r"([0-9]{1,18})\|([0-9]{1,18})\|([0-9]{1,18})", line)
print("%s %s %s" % (m.groups() if m else ("unreadable",) * 3))
PY
)
    rm -f "$ARCHF"
    if [ "${A_COUNT:-0}" -gt 0 ] 2>/dev/null && [ "${A_READY:-99}" -le 2 ] 2>/dev/null; then
      ok "$p-db: WAL archiver is live ($A_COUNT archived, $A_READY waiting, $A_FAILED lifetime failures)"
    else
      bad "$p-db: WAL archiving is stuck (archived=${A_COUNT:-?} waiting=${A_READY:-?} failed=${A_FAILED:-?})"
    fi
    # The newest COMPLETED base backup must be <26h old. Newest-of-any-phase false-reds
    # while the nightly run is still in flight; completed-of-any-age passes over a chain
    # that broke last month. Both directions matter.
    # the phase and the timestamp are cluster text, read through a private file: only a
    # completed entry whose timestamp is of an RFC 3339 shape is dated or shown; the rest
    # are counted (round 18)
    BKF=$(mktemp)
    # only a backup OF THIS CLUSTER dates the row: the namespace may hold another
    # cluster's backups (the chart's ScheduledBackup names spec.cluster.name) — round 23
    kubectl -n "$p-prod" get backups.postgresql.cnpg.io \
      -o jsonpath='{range .items[*]}{.status.phase} {.metadata.creationTimestamp} {.spec.cluster.name}{"\n"}{end}' > "$BKF" 2>/dev/null
    BK_TS=$(awk -v CL="$p-db" '$1=="completed" && $3==CL && $2 ~ /^[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9]Z$/ {print $2}' "$BKF" | sort | tail -1)
    BK_ODD=$(awk -v CL="$p-db" '$1=="completed" && $3==CL && $2 !~ /^[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9]Z$/' "$BKF" | grep -c .)
    rm -f "$BKF"
    BK_NOTE=""; [ "$BK_ODD" -eq 0 ] || BK_NOTE=", plus $BK_ODD completed entry(ies) whose timestamp is not of a timestamp shape (not repeated)"
    BK_AGE=$(( $(date +%s) - $(date -d "${BK_TS:-1970-01-01}" +%s 2>/dev/null || echo 0) ))
    if [ -n "$BK_TS" ] && [ "$BK_AGE" -lt 93600 ]; then
      ok "$p-db: newest completed base backup is $((BK_AGE/3600))h old (<26h)$BK_NOTE"
    else
      bad "$p-db: no completed base backup in 26h (newest: ${BK_TS:-none}$BK_NOTE) — the PITR anchor is stale"
    fi
    # The dump path must actually be GONE from the cluster, not just from the render —
    # a leftover CronJob means the prune never landed and two backup systems run at once.
    # absence is proven by the API's NotFound, never by a lookup that failed some other
    # way (forbidden, a transport error) — round 23; kubectl's message is private
    CJF=$(mktemp)
    if kubectl -n "$p-prod" get cronjob "$p-db-backup" >/dev/null 2>"$CJF"; then
      bad "$p-prod still runs the pg_dump CronJob — the switch did not prune it"
    elif grep -q "NotFound" "$CJF"; then
      ok "$p-prod: pg_dump CronJob is gone (bucket backups are the only path)"
    else
      bad "$p-prod: the pg_dump CronJob could not be looked up (kubectl's message is not repeated) — its absence is UNPROVEN"
    fi; rm -f "$CJF"
  done
fi

echo "== isolation =="
if with_project_namespaces "the isolation rows"; then
  for ns in $PNS; do
    kubectl -n "$ns" get networkpolicy default-deny-ingress >/dev/null 2>&1 \
      && ok "$ns has a default-deny NetworkPolicy" || bad "$ns is reachable from any pod in the cluster"
    [ "$(kubectl get ns "$ns" -o jsonpath='{.metadata.labels.pod-security\.kubernetes\.io/enforce}' 2>/dev/null)" = "restricted" ] \
      && ok "$ns enforces the restricted PodSecurity standard" || bad "$ns has no PodSecurity enforcement"
  done
fi

echo "== metadata service is unreachable from the pod network =="
# Probe it, do not assert that a policy object exists. The NetworkPolicy this replaced was
# accepted by the API, visible in kubectl, and did nothing — link-local is routed by the
# host before the policy chain ever sees it. The only honest check is to try the request
# from inside a pod netns and require it to fail, and to prove the probe works at all by
# confirming ordinary egress from the same netns still succeeds.
# An array, so the key path is ONE argument to ssh whatever it holds (a space, a quote).
# the loader's validated values only: a legacy SSH_KEY/HOST override bypassed the shape
# checks in lib-instance.sh and put raw environment text in ssh's argv (round 22)
# ConnectionAttempts: ssh itself tries the connection again when it could not be made. One
# dropped connection on the operator machine's own link (seen on the test box's desk: a
# control address dropped at the same moments) read as "could not run the metadata probe"
# and "no raw/PREROUTING rule" on a healthy node. A node that refuses the key, or a rule
# that is not there, answers on the first connection and is as red as before.
SSHN=(ssh -o BatchMode=yes -o ConnectTimeout=15 -o ConnectionAttempts=3 -i "$ORBIT_SSH_KEY" "root@$ORBIT_NODE_IP")
# The PID must resolve to a real container. An unresolved PID makes nsenter run in the
# HOST netns, where the answer is about the host and not about a pod — that misread as
# "metadata answered from a pod" and would equally have misread the other way.
# The node's stdout is remote text: a private file, read only for the fixed tokens the
# script above prints (a status, `blocked`, `unreachable`, the three no-* markers); a
# failed ssh is its own category, never its output
PROBEF=$(mktemp)
"${SSHN[@]}" 'CID=$(crictl ps -q --name traefik | head -1)
  [ -n "$CID" ] || { echo "no-container"; exit 0; }
  PID=$(crictl inspect "$CID" | python3 -c "import sys,json;print(json.load(sys.stdin)[\"info\"][\"pid\"])" 2>/dev/null)
  case "$PID" in ""|0|*[!0-9]*) echo "no-pid"; exit 0;; esac
  nsenter -t $PID -n true 2>/dev/null || { echo "no-netns"; exit 0; }
  MD=$(nsenter -t $PID -n timeout 5 curl -s -o /dev/null -w %{http_code} http://169.254.169.254/hetzner/v1/metadata 2>/dev/null) || MD=blocked
  NET=$(nsenter -t $PID -n timeout 8 curl -s -o /dev/null -w %{http_code} https://1.1.1.1 2>/dev/null) || NET=unreachable
  echo "md=$MD net=$NET"' > "$PROBEF" 2>/dev/null; PRC=$?
PROBE=$(python3 - "$PROBEF" <<'PY'
import re, sys
try: lines = [l.strip() for l in open(sys.argv[1], errors="replace").read().splitlines() if l.strip()]
except OSError: lines = []
last = lines[-1] if lines else ""
if last in ("no-container", "no-pid", "no-netns"): print(last)
elif re.fullmatch(r"md=([0-9]{3}|blocked) net=([0-9]{3}|unreachable)", last): print(last)
elif not last: print("")
else: print("unreadable")
PY
); rm -f "$PROBEF"
[ "$PRC" -eq 0 ] || PROBE="ssh-failed(rc=$PRC)"
case "$PROBE" in
  ssh-failed*|unreadable)
    bad "could not run the metadata probe on the node ($PROBE — the node's output is not repeated) — this check proved nothing" ;;
  *"md=blocked"*"net=2"*|*"md=blocked"*"net=3"*)
    ok "metadata unreachable from a pod netns, while ordinary egress still works" ;;
  *"md=blocked"*)
    bad "metadata unreachable, but so is everything else — the probe proves nothing ($PROBE)" ;;
  ""|no-container|no-pid|no-netns)
    bad "could not run the metadata probe on the node ($PROBE) — this check proved nothing" ;;
  *)  bad "metadata service ANSWERED from inside a pod netns: $PROBE" ;;
esac
# Assert the rules are in the RAW table. In filter they had to hold position 1 against
# kube-router's resyncs and kept losing it, and `iptables -C` only asks whether a rule
# exists somewhere — it cannot see that something above it now accepts the packet first.
# raw/PREROUTING runs before any of that and nothing else writes to it.
# status-only rows: the node's stdout is discarded too — a shell profile or a command
# wrapper on the node may print, and nothing it prints is this row's business (round 23)
"${SSHN[@]}" 'iptables -t raw -C PREROUTING -d 169.254.169.254 -j DROP' >/dev/null 2>&1 \
  && ok "raw/PREROUTING blocks metadata (ordering-independent)" \
  || bad "no raw/PREROUTING rule — the block depends on filter-chain ordering again"
"${SSHN[@]}" 'iptables -t raw -C OUTPUT -d 169.254.169.254 -j DROP' >/dev/null 2>&1 \
  && ok "raw/OUTPUT blocks metadata (hostNetwork pods covered)" \
  || bad "no raw/OUTPUT rule — a hostNetwork pod reaches metadata directly"
# The rule existing is not the same as packets hitting it. Non-zero counters prove the
# probe above actually traversed these rules rather than failing for some unrelated reason.
# On an owned node there is no cloud metadata service to send packets at the rule, so
# zero counters are the honest state and the row keys off instead of asserting non-zero.
if [ "${ORBIT_NODE_PROVIDER:-hetzner}" = "owned" ]; then
  ok "metadata DROP counters row keyed off — no metadata service exists on an owned node, so zero counters are expected"
else
"${SSHN[@]}" 'iptables -t raw -L -n -v 2>/dev/null | awk "/169.254.169.254/ && \$1+0 > 0 {n++} END{exit !(n>=1)}"' >/dev/null 2>&1 \
  && ok "metadata DROP counters are non-zero (the rule is really carrying traffic)" \
  || bad "metadata DROP rules exist but have never matched a packet"
fi
# And that the re-assertion timer is genuinely armed. A latched oneshot reports
# NextElapseUSecMonotonic=infinity — which is how this ran exactly once and still passed.
# both answers are the node's text: files, and only a timestamp-shaped `next` and an
# integer run count come out (anything else is `unreadable`)
TIMF=$(mktemp)
"${SSHN[@]}" 'systemctl show orbit-block-metadata.timer -p NextElapseUSecRealtime --value' > "$TIMF" 2>/dev/null
NEXTEL=$(python3 -c 'import re, sys; v = open(sys.argv[1], errors="replace").read().strip(); print(v if v == "infinity" or re.fullmatch(r"[A-Za-z]{3} [0-9]{4}-[0-9]{2}-[0-9]{2} [0-9]{2}:[0-9]{2}:[0-9]{2} [A-Z]{1,5}", v) else ("" if not v else "unreadable"))' "$TIMF" 2>/dev/null)
"${SSHN[@]}" 'journalctl -u orbit-block-metadata.service --no-pager 2>/dev/null | grep -c Starting' > "$TIMF" 2>/dev/null
RUNS=$(python3 -c 'import re, sys; v = open(sys.argv[1], errors="replace").read().strip(); print(v if re.fullmatch(r"[0-9]{1,9}", v) else "0")' "$TIMF" 2>/dev/null)
rm -f "$TIMF"
[ -n "$NEXTEL" ] && [ "$NEXTEL" != "infinity" ] && [ "$NEXTEL" != "unreadable" ] && [ "${RUNS:-0}" -gt 3 ] \
  && ok "metadata re-assertion timer is armed and has fired ${RUNS}x (next: $NEXTEL)" \
  || bad "timer not re-firing (next=${NEXTEL:-unset}, runs=${RUNS:-0}) — it ran once and latched"

# --- LIVE: the Alertmanager the cluster is actually running ------------------------
# Git is the source of truth, but the config Alertmanager LOADED is the one that routes an
# alert tonight. Alertmanager serves that config itself at /api/v2/status as
# config.original, with every secret-typed value redacted and the routing structure
# intact — which is all the routing simulation needs, and no Secret is read anywhere
# in this section. Reached through the API server's service proxy, the same way the
# platform stage reads it — never a port-forward, never a Secret decode.
if kubectl get --raw "/api/v1/namespaces/monitoring/services/kube-prometheus-stack-alertmanager:9093/proxy/api/v2/status" 2>/dev/null | python3 -c "
import json, sys, yaml
try:
    raw = json.load(sys.stdin)['config']['original']
except Exception:
    sys.exit(1)
sys.stdout.write(raw if isinstance(raw, str) else yaml.safe_dump(raw))
" > "$AMDIR/am-live.yaml" 2>/dev/null; then
  if [ -s "$AMDIR/am-live.yaml" ]; then
    am_rows "alertmanager(live)" raw "$AMDIR/am-live.yaml" catalog/projects.yaml "" --desired "$AM_INSTANCE"
  else
    bad "alertmanager(live): Alertmanager served an empty running config — where a trigger's alert would go is UNPROVEN, not fine"
  fi
else
  bad "alertmanager(live): Alertmanager's /api/v2/status did not serve its running config — where a trigger's alert would go is UNPROVEN, not fine"
fi

fi  # PREBOOT live region
rm -rf "$AMDIR"

# The hub live checks left with the hub itself (extraction, 2026-08-25). A public
# surface serving catalog-generated content is asserted again when the hub returns as
# an ordinary tenant.

echo "== live: every declared agent is actually running (Phase D.2) =="
# Keyed on the catalog's own declaration, stage8-style: an instance with no agents has
# nothing to run and says so, and the section re-arms itself the moment one is declared.
# The fixture checks above prove the plumbing either way — this proves the plane.
# name:mode pairs — a deployment agent is a Deployment + Service, a cronjob agent is a
# CronJob with neither (Phase D.3), so the live assertions differ by mode.
AGENTNAMES=$(python3 -c "
import re, sys, yaml
try:
    c = yaml.safe_load(open('catalog/projects.yaml')) or {}
    if not isinstance(c, dict): raise ValueError('not a mapping')
except Exception:
    print('UNREADABLE'); sys.exit(0)      # never the parser's message (round 19)
# a name lands in an object name on a kubectl command line and a mode picks a branch:
# only the generator's own shapes go there; the rest print UNSHAPED, never the text
NAME = re.compile(r'[a-z0-9]([a-z0-9-]{0,20}[a-z0-9])?')
for a in (c.get('agents') or []):
    if isinstance(a, dict) and a.get('enabled', True) and a.get('name'):
        name, mode = a.get('name'), a.get('mode') or 'deployment'
        if isinstance(name, str) and NAME.fullmatch(name) and mode in ('deployment', 'cronjob'):
            print(name + ':' + mode)
        else:
            print('UNSHAPED:unshaped')" 2>/dev/null)
# The pin is printed in rows and compared with a live image: it is used only once it
# has the SHAPE of an image reference (registry/path:tag, no userinfo, no scheme), else
# the rows say the declaration is not an image reference and never repeat it.
PIN=$(python3 -c "
import re, yaml
r = (yaml.safe_load(open('instance/config.yaml')) or {}).get('agentRuntime') or {}
image, tag = r.get('image', ''), r.get('tag', '')
ok = isinstance(image, str) and isinstance(tag, str) \
    and re.fullmatch(r'[a-z0-9]([a-z0-9.-]*[a-z0-9])?(:[0-9]{1,5})?(/[a-z0-9]([a-z0-9._-]*[a-z0-9])?)+', image) \
    and re.fullmatch(r'[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}', tag)
print(f'{image}:{tag}' if ok else 'NOT-AN-IMAGE-REFERENCE')" 2>/dev/null)
if [ "$PREBOOT" = 1 ]; then
  ok "pre-boot: the agent plane's live checks key off the kubeconfig, like every other"
elif [ "$AGENTNAMES" = UNREADABLE ]; then
  bad "catalog/projects.yaml could not be read (the parser's message is not repeated) — the live agent rows are UNCHECKED, not keyed off"
elif [ -z "$AGENTNAMES" ]; then
  ok "no agent declared in the catalog — the live agent checks key off (an agentRuntime pin is not required without agents)"
else
  # the pin is REQUIRED only here: an instance with no enabled agent may omit agentRuntime
  [ "$PIN" = NOT-AN-IMAGE-REFERENCE ] && { bad "agentRuntime.image/tag is not an image reference (registry/path and a tag; the value is not repeated) — the agent pin rows cannot run"; PIN=; }
  for PAIR in $AGENTNAMES; do
    AG=${PAIR%:*}; MODE=${PAIR##*:}
    if [ "$PAIR" = UNSHAPED:unshaped ]; then
      bad "an enabled agent in catalog/projects.yaml has a name that is not an agent name, or a mode that is not deployment/cronjob (not repeated) — its live rows cannot run"
      continue
    fi
    if [ "$MODE" = cronjob ]; then
      # A scheduled agent is a CronJob — NOT a Deployment. Assert the CronJob exists and
      # that no Deployment or Service was rendered for it (the D.3 property, live).
      kubectl -n orbit-agents-prod get cronjob "orbit-agents-$AG" >/dev/null 2>&1 \
        && ok "cron agent $AG exists as a CronJob in orbit-agents-prod" \
        || bad "cron agent $AG has no CronJob — a scheduled agent that never runs"
      # absence is proven by the API's NotFound, never by a lookup that failed some other
      # way (forbidden, a transport error) — round 25; kubectl's message is private
      ABSF=$(mktemp)
      if kubectl -n orbit-agents-prod get deploy "orbit-agents-$AG" >/dev/null 2>"$ABSF"; then
        bad "cron agent $AG also has a Deployment — a scheduled agent rendered long-running"
      elif grep -q "NotFound" "$ABSF"; then
        ok "cron agent $AG has no Deployment (scheduled, not long-running)"
      else
        bad "cron agent $AG: the Deployment lookup failed some other way (kubectl's message is not repeated) — its absence is UNPROVEN"
      fi
      if kubectl -n orbit-agents-prod get svc "orbit-agents-$AG" >/dev/null 2>"$ABSF"; then
        bad "cron agent $AG has a Service — a scheduled agent serves no port"
      elif grep -q "NotFound" "$ABSF"; then
        ok "cron agent $AG has no Service (correct: it serves nothing)"
      else
        bad "cron agent $AG: the Service lookup failed some other way (kubectl's message is not repeated) — its absence is UNPROVEN"
      fi
      rm -f "$ABSF"
      LIVEF=$(mktemp)
      kubectl -n orbit-agents-prod get cronjob "orbit-agents-$AG" \
        -o jsonpath='{.spec.jobTemplate.spec.template.spec.containers[0].image}' > "$LIVEF" 2>/dev/null
      LIVECMP=$(image_cmp "$LIVEF" "$PIN")
      [ -n "$PIN" ] && [ "$LIVECMP" = match ] \
        && ok "cron agent $AG runs exactly the agentRuntime pin ($PIN)" \
        || bad "cron agent $AG does not run the agentRuntime pin '${PIN:-unset}' (the live image is not repeated: it is the cluster's text) — image redirected"
      continue
    fi
    # the condition status is cluster text: shaped inside the pipeline to True / False / absent / unreadable
    AVAIL=$(kubectl -n orbit-agents-prod get deploy "orbit-agents-$AG" \
      -o jsonpath='{.status.conditions[?(@.type=="Available")].status}' 2>/dev/null | awk 'NR==1 && /^(True|False|Unknown)$/{print; f=1} NR==1 && !/^(True|False|Unknown)$/{print ($0 == "" ? "absent" : "unreadable"); f=1} END{if (!f) print "absent"}')
    [ "$AVAIL" = "True" ] && ok "agent $AG is Available in orbit-agents-prod" \
      || bad "agent $AG is not Available (status=$AVAIL)"
    # Available is not rolled out: a paused or mid-rollout Deployment is Available on its
    # OLD pods while its template already names the pin. The rollout must be complete,
    # and the RUNNING pods (not the template) must carry the pin (round 25). The numbers
    # are the cluster's text: private files, shaped before they are compared.
    ROLLF=$(mktemp)
    kubectl -n orbit-agents-prod get deploy "orbit-agents-$AG" \
      -o jsonpath='{.spec.paused}{"\n"}{.metadata.generation}{"\n"}{.status.observedGeneration}{"\n"}{.spec.replicas}{"\n"}{.status.updatedReplicas}{"\n"}{.status.readyReplicas}{"\n"}' > "$ROLLF" 2>/dev/null
    ROLL=$(python3 -c 'import re, sys
ls = (open(sys.argv[1], errors="replace").read().split("\n") + [""] * 6)[:6]
if ls[0] == "true": print("paused"); sys.exit()
def n(s, dflt):
    return int(s) if re.fullmatch(r"[0-9]{1,9}", s) else (dflt if s == "" else None)
gen, obs, rep, upd, rdy = n(ls[1], None), n(ls[2], 0), n(ls[3], 1), n(ls[4], 0), n(ls[5], 0)
if None in (gen, obs, rep, upd, rdy): print("unreadable"); sys.exit()
if obs < gen: print("the observed generation is behind the spec"); sys.exit()
print("complete" if upd == rep == rdy and rep >= 1 else "%d updated, %d ready of %d desired" % (upd, rdy, rep))' "$ROLLF" 2>/dev/null || echo unreadable); rm -f "$ROLLF"
    [ "$ROLL" = complete ] && ok "agent $AG rollout is complete (not paused; generation observed; updated = ready = desired)" \
      || bad "agent $AG rollout is not complete ($ROLL) — its Available pods may run an older template"
    PODF=$(mktemp)
    kubectl -n orbit-agents-prod get pods -l "app.kubernetes.io/name=$AG,app.kubernetes.io/instance=orbit-agents" \
      -o jsonpath='{range .items[*]}{.status.phase}{" "}{.spec.containers[0].image}{"\n"}{end}' > "$PODF" 2>/dev/null
    PODCMP=$(python3 -c 'import sys
rows = [l.rstrip("\n").split(" ", 1) for l in open(sys.argv[1], errors="replace") if l.strip()]
run = [(r[1] if len(r) > 1 else "") for r in rows if r[0] == "Running"]
print("none" if not run else ("match" if all(i == sys.argv[2] for i in run) else "mismatch"))' "$PODF" "$PIN" 2>/dev/null || echo unreadable); rm -f "$PODF"
    [ -n "$PIN" ] && [ "$PODCMP" = match ] \
      && ok "agent $AG: every Running pod runs the agentRuntime pin" \
      || bad "agent $AG: the Running pods do not all run the agentRuntime pin ($PODCMP; the live images are not repeated) — the template's pin is not what serves"
    # A Service, not just a pod: whatever wakes the agent (the D.3 intake) addresses it
    # by name, and a Deployment with no Service is an agent nothing can call.
    kubectl -n orbit-agents-prod get svc "orbit-agents-$AG" >/dev/null 2>&1 \
      && ok "agent $AG is addressable as a Service" \
      || bad "agent $AG has no Service — nothing in the cluster can call it"
    # C2 live: the running image MUST equal the instance's agentRuntime pin. There is no
    # per-agent image override, so a Deployment running anything else means a hand-edit
    # or a mutation pointed the token-holding pod at another registry.
    LIVEF=$(mktemp)
    kubectl -n orbit-agents-prod get deploy "orbit-agents-$AG" \
      -o jsonpath='{.spec.template.spec.containers[0].image}' > "$LIVEF" 2>/dev/null
    LIVECMP=$(image_cmp "$LIVEF" "$PIN")
    [ -n "$PIN" ] && [ "$LIVECMP" = match ] \
      && ok "agent $AG runs exactly the agentRuntime pin ($PIN)" \
      || bad "agent $AG does not run the agentRuntime pin '${PIN:-unset}' (the live image is not repeated: it is the cluster's text) — image redirected"
  done
  # An AGENT must not be published, ever. The generator writes no Ingress into this
  # namespace at all (asserted structurally above, github trigger and door declaration
  # included), so anything here was hand-applied — and exactly ONE hand-written Ingress is
  # sanctioned: the intake's webhook door, which an instance may open under
  # `intakeIngress: wired`. This row used to demand ZERO ingresses; demanding zero would
  # have forced the instance that opens the door to delete the check rather than tighten
  # it, so it now asserts the SHAPE instead, which is strictly more than "none":
  #
  #   * every backend must be the intake Service (an Ingress in front of an AGENT is the
  #     original finding and is still RED — the agent card and its /.well-known are
  #     unauthenticated);
  #   * every path must be exactly /webhook/github, pathType Prefix (the intake's GET
  #     /healthz and /metrics are its only unauthenticated routes; a `/` prefix publishes
  #     both, and a path-scoped router is what keeps them private);
  #   * no `tls:` block (an Ingress can only name a Secret in its own namespace, so a tls
  #     block here would either fail or demand a copied wildcard key in the plane's
  #     namespace — the platform serves this host from Traefik's default certificate).
  # C3: a failed query is NOT an empty namespace. `kubectl get ingress -o json` writes
  # nothing to stdout on RBAC-forbidden / API-error / kubectl-missing AND on a genuinely
  # unpublished namespace; the old code fed `${ING:-null}` to the parser so BOTH collapsed
  # onto items=[] -> "unpublished -> GREEN". Keep the exit code and split them: rc!=0 is
  # "could not ask" and is RED. And an ABSENT door while the instance declares
  # intakeIngress: wired (the gate, asserted both directions above) is a door that is
  # declared and not live on a reachable cluster -> also RED, not a green "unpublished".
  # the whole response (annotations included) is the cluster's text: a private file,
  # never a shell variable a traced shell would print
  INGF=$(mktemp)
  kubectl -n orbit-agents-prod get ingress -o json > "$INGF" 2>/dev/null; INGRC=$?
  # The gate the instance declares — read the SAME way the agentRuntime pin is read above.
  # Empty when the instance repo is not the cwd (a bare core checkout); the wired-but-absent
  # rule below then does not fire, which is correct because there is no declaration to honour.
  # the field is instance text: python returns one of three fixed words, never the value
  INTAKEWIRED=$(python3 -c "
import yaml
try: v = (yaml.safe_load(open('instance/config.yaml')) or {}).get('intakeIngress')
except Exception: v = None
print('wired' if v == 'wired' else '' if v in (None, '') else 'invalid')" 2>/dev/null)
  if [ "$INTAKEWIRED" = invalid ]; then
    bad "instance/config.yaml: intakeIngress is not \`wired\` nor absent (the value is not repeated) — the door's gate cannot be read"
    INTAKEWIRED=""
  fi
  if [ "$INGRC" -ne 0 ]; then
    bad "orbit-agents-prod: could not read the ingress list (kubectl rc=$INGRC) — RBAC-forbidden, kubectl missing or an API error; a call that could not ask is NOT an unpublished namespace and must never read as a green 'no agent is published'"
  else
  IOUT=$(INTAKEWIRED="$INTAKEWIRED" python3 -c '
import json, os, re, sys
import shown as display   # the shared display rule (scripts/verify/shown.py)
try:
    raw = open(sys.argv[1], encoding="utf-8", errors="replace").read().strip()
except OSError:
    raw = ""
try:
    items = (json.loads(raw) or {}).get("items") or [] if raw and raw != "null" else []
except Exception as exc:                      # unreadable is NOT the same as empty
    print("FAIL could not read the ingress list: %s" % exc); sys.exit(1)
wired = os.environ.get("INTAKEWIRED", "") == "wired"
INTAKE_SVC = "orbit-agents-intake"            # C2: EXACT, not any "*-intake" suffix
bad = []
# every field a row repeats is cluster text: shown only in its shape (round 18)
NAME = re.compile(r"[a-z0-9]([a-z0-9.-]{0,251}[a-z0-9])?")
HOST = re.compile(r"[A-Za-z0-9]([A-Za-z0-9-]{0,61}[A-Za-z0-9])?(\.[A-Za-z0-9]([A-Za-z0-9-]{0,61}[A-Za-z0-9])?)*")
WORD = re.compile(r"[A-Za-z]{1,40}")
def shaped(v, pat, what):
    return v if isinstance(v, str) and pat.fullmatch(v) and len(v) <= 253 and not display.tokenish(v) else display.described(what, what)
for ing in items:
    name = shaped((ing.get("metadata") or {}).get("name", "<unnamed>"), NAME, "name")
    spec = ing.get("spec") or {}
    if spec.get("tls"):
        bad.append("%s carries a tls: block" % name)
    if spec.get("defaultBackend"):            # C1: a catch-all backend publishes with NO path rule
        db = shaped((((spec.get("defaultBackend") or {}).get("service")) or {}).get("name", ""), NAME, "name")
        bad.append("%s carries a spec.defaultBackend -> Service %s — a catch-all backend "
                   "answers every unmatched path with no rule at all, so an agent behind it "
                   "(or a catch-all to the intake past the path scope) is on the internet; "
                   "the door must publish ONLY its path rule and no defaultBackend"
                   % (name, db))
    for rule in spec.get("rules") or []:
        host = "<no host>" if rule.get("host") is None else shaped(rule.get("host"), HOST, "host")
        for p in ((rule.get("http") or {}).get("paths") or []):
            svc = (((p.get("backend") or {}).get("service")) or {}).get("name", "")
            path, ptype = p.get("path"), p.get("pathType")
            # the path itself is never repeated: it can carry a token
            if svc != INTAKE_SVC:             # C2: exact, so "orbit-agent-triage-intake" is caught
                bad.append("%s publishes a path rule on %s -> Service %s, which is not the intake %s "
                           "(an AGENT is on the internet; a \"*-intake\" impostor name is "
                           "NOT the door)" % (name, host, shaped(svc, NAME, "name"), INTAKE_SVC))
            elif path != "/webhook/github" or ptype != "Prefix":
                bad.append("%s publishes a path rule on %s that is not Prefix /webhook/github "
                           "(pathType %s) — the door must be that path alone, or /healthz and "
                           "/metrics become public" % (name, host, shaped(ptype, WORD, "pathType")))
if not items and wired:                       # C3: declared wired, cluster reachable, door ABSENT
    print("FAIL intakeIngress: wired is declared but orbit-agents-prod has NO Ingress — "
          "the door is declared and not live (the daemon has not applied it, or it was pruned); "
          "a reachable cluster with the gate set and no door is RED, not an unpublished pass")
    sys.exit(1)
if bad:
    print("FAIL " + "; ".join(bad)); sys.exit(1)
print("OK the plane namespace is unpublished (no Ingress at all)" if not items else
      "OK %d ingress(es) in the plane namespace, every one the path-scoped intake door"
      % len(items))
' "$INGF"); IRC=$?
  rm -f "$INGF"
    if [ "$IRC" -eq 0 ]; then
      ok "no agent is published; any Ingress in the agent namespace is the intake's path-scoped webhook door with no tls block [$IOUT]"
    else
      bad "orbit-agents-prod: $IOUT"
    fi
  fi
fi

if [ "$PREBOOT" = 1 ]; then
  ok "pre-boot: the referenced-Secret and protected-environment live checks key off the kubeconfig, like every other"
else
echo "== every referenced Secret actually exists =="
# A reference to a Secret that does not exist fails in two different quiet ways: a missing
# pull secret only warns until the first PRIVATE image (then ImagePullBackOff), and a
# missing basic-auth secret makes Traefik disable the router entirely — which answers 404,
# looks like a guard, and is the absence of one. Both were live and both were green.
# Each inventory's STATUS is read before it is walked: a listing that failed (Forbidden,
# an API hiccup) yields no references, and no references must never read as "every one
# exists". A reference is walked only when it has the shape <namespace>/<name>.
# the pull-secret references are cluster text: a private file, shaped names only into
# the shell and kubectl's argv, the rest counted
PULLF=$(mktemp)
if kubectl get pods -A -o go-template='{{range .items}}{{$ns := .metadata.namespace}}{{range .spec.imagePullSecrets}}{{$ns}}/{{.name}}{{"\n"}}{{end}}{{end}}' > "$PULLF" 2>/dev/null; then
  MISSINGSEC=""
  PULLODD=$(grep -c . <(grep -Ev '^[a-z0-9]([a-z0-9.-]*[a-z0-9])?/[a-z0-9]([a-z0-9.-]*[a-z0-9])?$' "$PULLF"))
  for e in $(sort -u < "$PULLF" | grep -E '^[a-z0-9]([a-z0-9.-]*[a-z0-9])?/[a-z0-9]([a-z0-9.-]*[a-z0-9])?$'); do
    kubectl -n "${e%%/*}" get secret "${e##*/}" >/dev/null 2>&1 || MISSINGSEC="$MISSINGSEC $e"
  done
  [ "$PULLODD" -eq 0 ] || bad "$PULLODD imagePullSecret reference(s) are not of a namespace/name shape (not repeated) — they cannot be checked"
  [ -z "$MISSINGSEC" ] && ok "every referenced imagePullSecret exists" \
    || bad "referenced but absent:$MISSINGSEC"
else
  bad "the pods could not be listed for their imagePullSecrets (kubectl failed) — 'every referenced imagePullSecret exists' is UNCHECKED, not proven"
fi
rm -f "$PULLF"
# the Middleware inventory is cluster text: a private file; a reference reaches the
# shell (and kubectl's argv) only as ns/name of a name's shape, and a basic-auth
# reference of any other shape is RED (the CRD lets the field hold any string, and a
# Middleware whose Secret cannot be addressed fails OPEN as a 404) — never dropped
MWF=$(mktemp)
if kubectl get middleware -A -o jsonpath='{range .items[*]}{.metadata.namespace}/{.spec.basicAuth.secret}{"\n"}{end}' > "$MWF" 2>/dev/null; then
  MISSINGMW=""
  MWODD=$(grep -c . <(grep -Ev '^[a-z0-9]([a-z0-9.-]*[a-z0-9])?/([a-z0-9]([a-z0-9.-]*[a-z0-9])?)?$' "$MWF"))
  for e in $(grep -E '^[a-z0-9]([a-z0-9.-]*[a-z0-9])?/[a-z0-9]([a-z0-9.-]*[a-z0-9])?$' "$MWF"); do
    kubectl -n "${e%%/*}" get secret "${e##*/}" >/dev/null 2>&1 || MISSINGMW="$MISSINGMW $e"
  done
  [ "$MWODD" -eq 0 ] || bad "$MWODD basic-auth Middleware reference(s) are not of a namespace/name shape (not repeated) — a Secret that cannot be addressed fails OPEN as a 404"
  [ -z "$MISSINGMW" ] && ok "every basic-auth Middleware has its Secret" \
    || bad "middleware references an absent secret (fails OPEN as 404):$MISSINGMW"
else
  bad "the Middlewares could not be listed (kubectl failed) — 'every basic-auth Middleware has its Secret' is UNCHECKED, not proven"
fi
rm -f "$MWF"

echo "== live: every protected environment is protected =="
# Iterate the catalog rather than probing one known hostname. The dev basic-auth Secret is
# declared for a single namespace, so a SECOND dev environment would ship with no
# credential at all — and a suite that only checks hub.dev.example.org would stay green.
# A hostname is printed and handed to curl only once it has the shape of one: a catalog
# entry that is not a DNS name (userinfo, a scheme, a path) is refused by project, env and
# position, never repeated. The check stops the section: probing a value is worse than
# not probing.
# The hosts are the ones the GENERATOR publishes (scripts/catalog-sync.py, env_hostnames):
# `expose: false` publishes nothing; an explicit `hostnames: []` publishes nothing; the
# default host is invented only when the key is absent; customDomains are doors too. The
# dev domain reaches the program as data (environment), never as program text.
PROTBAD=$(mktemp)
PROTHOSTS=$(python3 -c "
import os, re, sys, yaml
import shown as display   # the shared display rule (scripts/verify/shown.py)
HOST = re.compile(r'[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?(\.[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?)*')
DEV = os.environ.get('ORBIT_DEV_DOMAIN', '')
# every line this program writes to stderr is its OWN, positional and shaped: the catalog
# is read under a guard, so a parser's message (PyYAML repeats an unknown tag verbatim)
# never becomes the row's text (round 19)
try:
    c = yaml.safe_load(open('catalog/projects.yaml')) or {}
    if not isinstance(c, dict) or not isinstance(c.get('projects'), list): raise ValueError('not a catalog')
except Exception:
    print('catalog/projects.yaml could not be read (the parser message is not repeated)', file=sys.stderr); sys.exit(2)
LABEL = re.compile(r'[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?')
for pi,p in enumerate(c['projects']):
    if not isinstance(p, dict):
        print('projects[%d] is not a mapping' % pi, file=sys.stderr); sys.exit(2)
    if p.get('hosted','self')!='self' or p.get('managed_elsewhere'): continue
    for env,cfg in (p.get('environments') or {}).items():
        cfg=cfg or {}
        if env=='prod' or not cfg.get('enabled') or cfg.get('expose') is False: continue
        hosts=list(cfg.get('hostnames') or [])
        if not hosts and 'hostnames' not in cfg: hosts=[f\"{p['id']}.{DEV}\"]
        hosts += [(d or {}).get('host') for d in (cfg.get('customDomains') or [])]
        # the door path, catalog-sync's door_path rule: the root when some web component
        # serves '/', else the first web path plus its probePath (a project whose only route
        # is /api has no router, and so no basic-auth middleware, at '/': Traefik's 404)
        web=[x for x in (p.get('components') or []) if isinstance(x, dict) and x.get('kind','web')=='web' and x.get('port')]
        dpath='' if (not web or any((x.get('path') or '/')=='/' for x in web)) else str(web[0].get('path')).rstrip('/')+str(web[0].get('probePath') or '')
        if not re.fullmatch(r'(/[A-Za-z0-9._~-]+)*', dpath):
            print('projects[%d] door path is not a plain URL path' % pi, file=sys.stderr); sys.exit(2)
        for i,h in enumerate(hosts, 1):
            if not isinstance(h, str) or not HOST.fullmatch(h.strip().rstrip('.').lower()):
                # by POSITION, never by the id or the environment key: both are catalog text
                shown_env = display.shown(env, 'label', 'environment key')
                print('projects[%d]/%s hostname %d is not a DNS name' % (pi, shown_env, i), file=sys.stderr); sys.exit(2)
            print(h.strip().rstrip('.').lower() + dpath)" 2>"$PROTBAD"); PROTRC=$?
if [ "$PROTRC" -ne 0 ]; then
  bad "the catalog's protected hostnames cannot be probed: $(head -1 "$PROTBAD" | cut -c1-120) — fix the catalog entry"
  PROTHOSTS=""
fi
rm -f "$PROTBAD"
# Keyed on the catalog, stage8-style: with no dev environment enabled anywhere in the
# catalog there is nothing to protect and nothing to probe; the section re-arms
# by itself the moment any project declares an enabled dev environment. This is a
# keyed-off state, not a weakened check — the assertions below run in full whenever a
# protected host exists.
if [ -n "$PROTHOSTS" ]; then
  ok "found protected environments to probe: $(echo $PROTHOSTS | tr '\n' ' ')"
else
  ok "no dev environment enabled in the catalog — protection checks keyed off"
fi
for h in $PROTHOSTS; do
  # each token is <host><door path>; the host alone is what --resolve pins
  host=${h%%/*}; url="https://$h"; [ "$h" = "$host" ] && url="https://$h/"
  # 401 specifically. Traefik answers 404 when the middleware or its secret is missing,
  # which is the fail-open this whole section exists to catch — a missing guard looks
  # exactly like a closed one to any check that only asserts "not 200".
  C_NO=$(curl -s -o /dev/null -w '%{http_code}' --max-time 15 "$url")
  [ "$C_NO" = "401" ] && ok "$h without credentials -> 401" \
    || bad "$h without credentials -> $C_NO (404 means the guard is absent, not closed)"
  C_BAD=$(curl -s -o /dev/null -w '%{http_code}' --max-time 15 -u orbit:definitely-wrong "$url")
  [ "$C_BAD" = "401" ] && ok "$h with wrong credentials -> 401" || bad "$h with wrong credentials -> $C_BAD"
  if [ -f "$ORBIT_CONFIG_DIR/dev-basic-auth-password" ]; then
    # The password reaches curl on stdin as a config document, never as an argument, and
    # never as a shell value: a Python process reads the FILE (its path is the only
    # argument) and writes the `user = "..."` line, so a traced shell (bash -x) shows the
    # path and nothing else. Line ends are dropped, as the dashboard stage drops them.
    curluser(){ python3 -c 'import sys;v=open(sys.argv[1]).read().replace("\n","").replace("\r","");v=v.replace("\\","\\\\").replace("\"","\\\"");print("user = \"%s:%s\"" % (sys.argv[2], v))' "$1" "$2"; }
    # PINNED to this instance's own ingress (--resolve keeps the Host header and the TLS
    # name check, and only the connection's address is fixed): a catalog hostname is
    # operator input that DNS may point anywhere, and the two unauthenticated probes
    # above have already proven the public path; the one request that carries the
    # shared password goes to the node this instance declares, or nowhere.
    if [ -z "${ORBIT_CONTROL_PLANE_IP:-}" ]; then
      bad "$h: the authenticated probe was NOT sent — no control-plane IP is declared to pin it to (instance/config.yaml nodes)"
    else
      # --noproxy '*': a proxy from the environment (SOCKS5h above all) would resolve and
      # dial the name itself and the pin would bind nothing
      C_OK=$(curluser "$ORBIT_CONFIG_DIR/dev-basic-auth-password" orbit | curl -q -s -o /dev/null -w '%{http_code}' --max-time 15 -K - --noproxy '*' --resolve "$host:443:$ORBIT_CONTROL_PLANE_IP" "$url")
      [ "$C_OK" = "200" ] && ok "$h with correct credentials -> 200 (pinned to this instance's ingress)" || bad "$h with correct credentials -> $C_OK (pinned to this instance's ingress)"
    fi
  fi
done
fi  # PREBOOT: referenced Secrets + protected environments

echo
echo "stage3: $PASS passed, $FAIL failed"
exit $((FAIL > 0))
