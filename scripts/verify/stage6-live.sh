#!/usr/bin/env bash
# Stage 6 — the live platform is the committed one. Assumes stage5-platform passed.
#
# Every earlier stage checks that what runs is healthy; none checked that what runs is
# what git says. On the V1 test box orbitd sat at v0.31.6 for days while the instance
# pinned v0.32.1: the pin bump was committed and nobody ran `orbit platform apply`, and
# every stage stayed green (docs/V1.md §8, 2026-10-04). The platform layer is applied
# from the desk, never by a controller, so "committed but not applied" is a normal state
# that only a comparison can see. This stage is that comparison:
#
#   * `orbit platform plan` over the whole tree is EMPTY (any un-applied change — a pin
#     bump, a values edit, a removed object — is a non-empty plan);
#   * the orbitd pod RUNNING is the image the instance pins (a Deployment can carry the
#     new image while its rollout is stuck on the old pod);
#   * orbitd's /healthz reports the pinned release (the process itself, not the spec).
set -uo pipefail
cd "$(dirname "$0")/../.."
export PYTHONPATH="$(pwd)/scripts/verify${PYTHONPATH:+:$PYTHONPATH}"
. "$(dirname "$0")/../lib-instance.sh" || exit 2
export KUBECONFIG="${KUBECONFIG:-$HOME/.kube/config-${ORBIT_INSTANCE_NAME}-platform}"
PASS=0; FAIL=0
ok(){ echo "  ok    $1"; PASS=$((PASS+1)); }
bad(){ echo "  FAIL  $1"; FAIL=$((FAIL+1)); }
kubeconfig_present(){ local f any=1; set -f; IFS=: ; for f in $1; do IFS=$' \t\n'   # set -f: the list is split, never glob-expanded
    if [ -s "$f" ] && [ -r "$f" ]; then any=0
    elif [ -e "$f" ]; then echo "kubeconfig $f exists but is empty or unreadable — neither a cluster nor pre-boot; fix or remove it" >&2; exit 2; fi
  done; IFS=$' \t\n'; set +f; return $any; }
if ! kubeconfig_present "$KUBECONFIG"; then
  echo "stage6-live: 0 passed, 0 failed (pre-boot — no kubeconfig at $KUBECONFIG; bootstrap arms this stage)"
  exit 0
fi
TMP=$(mktemp -d); trap 'rm -rf "$TMP"' EXIT

echo "== the live platform is the committed one =="
if ! kubectl --request-timeout=20s get --raw /version >/dev/null 2>&1 </dev/null; then
  bad "a kubeconfig exists at $KUBECONFIG but the cluster did not answer — every row below is UNCHECKED, not clean"
  echo; echo "stage6-live: $PASS passed, $FAIL failed"; exit 1
fi

# 1. The whole platform plan is empty.
if ! command -v orbit >/dev/null 2>&1; then
  bad "the \`orbit\` desk CLI is not on PATH — the platform plan CANNOT run, and a check that cannot run is not a pass (run make verify through scripts/desk.sh, whose image carries it)"
else
  # stdout is the plan document (it carries live objects, Secrets' metadata among them):
  # it goes to a private scratch file and is deleted with it; only the per-app summary
  # lines on stderr are read, and only app names and counts come back out.
  orbit platform plan . --summary --kubeconfig "${KUBECONFIG%%:*}" > "$TMP/plan.json" 2> "$TMP/plan.err" </dev/null
  rc=$?
  case "$rc" in
    0) ok "orbit platform plan is empty — every platform app matches the committed tree";;
    1) DRIFT=$(python3 - "$TMP/plan.err" <<'PY'
import re, sys
from shown import shown
apps = []
for line in open(sys.argv[1], encoding="utf-8", errors="replace"):
    m = re.match(r"([a-z0-9][a-z0-9-]{0,62}) empty=false rendered=\d+ matched=\d+ changed=(\d+) missing=(\d+) extra=(\d+)", line)
    if m:
        apps.append("%s (changed %s, missing %s, extra %s)" % (shown(m.group(1), "name", "app"), m.group(2), m.group(3), m.group(4)))
print("; ".join(apps) or "no app named on the summary")
PY
)
       bad "the live platform differs from the committed tree: $DRIFT — a committed change was never applied (or the live copy was edited); \`orbit platform plan\` shows it, \`orbit platform apply --app <name>\` applies it";;
    *) bad "orbit platform plan could not render or reach the cluster (exit $rc) — the comparison is UNCHECKED, not clean";;
  esac
fi

# 2. The running orbitd pod is the pinned image.
PIN=$(python3 - <<'PY'
import yaml
c = yaml.safe_load(open("instance/config.yaml", encoding="utf-8")) or {}
core = c.get("coreRegistry") or {}
owner, tag = str(core.get("owner") or ""), str(core.get("orbitdTag") or "")
print(f"{owner}/orbitd:{tag}" if owner and tag else "")
PY
)
if [ -z "$PIN" ]; then
  bad "instance/config.yaml names no coreRegistry.owner/orbitdTag — there is no pin to compare orbitd against"
else
  kubectl -n orbit get pods -l app=orbitd -o json > "$TMP/pods.json" 2>/dev/null </dev/null
  RUNNING=$(python3 - "$TMP/pods.json" "$PIN" <<'PY'
import json, sys
from shown import shown
try:
    items = json.load(open(sys.argv[1])).get("items", [])
except Exception:
    print("UNREADABLE"); sys.exit()
imgs = sorted({c.get("image", "") for p in items if (p.get("status") or {}).get("phase") == "Running"
               for c in (p.get("spec") or {}).get("containers", []) if c.get("name") == "orbitd"})
print(",".join(shown(i, "label", "image") for i in imgs) if imgs else "NONE")
PY
)
  case "$RUNNING" in
    "$PIN") ok "the running orbitd pod is the pinned image ($PIN)";;
    UNREADABLE) bad "the orbitd pods could not be listed — the pin comparison is UNCHECKED, not clean";;
    NONE) bad "no orbitd pod is Running in ns orbit — nothing to compare against the pin $PIN";;
    *) bad "orbitd runs $RUNNING but the instance pins $PIN — the pin was bumped and never applied (orbit platform apply --app platform-config), or the rollout is stuck";;
  esac
fi

# 3. orbitd's own process reports the pinned release. Pinned to the node like every
#    other door row, so DNS cannot answer for it.
TAG="${PIN##*:}"
case "$TAG" in
  v[0-9]*|main-[0-9a-f]*)
    curl -sS --max-time 15 --resolve "orbit.$ORBIT_DOMAIN:443:$ORBIT_NODE_IP" \
      "https://orbit.$ORBIT_DOMAIN/healthz" > "$TMP/healthz" 2>/dev/null
    VER=$(python3 - "$TMP/healthz" <<'PY'
import json, sys
from shown import shown
try:
    v = json.load(open(sys.argv[1])).get("version", "")
except Exception:
    print("UNREADABLE"); sys.exit()
print(shown(v, "name", "version") if isinstance(v, str) and v else "NONE")
PY
)
    if [ "$VER" = "$TAG" ]; then ok "orbitd's /healthz reports the pinned release ($TAG)"
    elif [ "$VER" = UNREADABLE ]; then bad "orbitd's /healthz did not answer with JSON — the release it runs is UNCHECKED"
    else bad "orbitd's /healthz reports \"$VER\", the instance pins $TAG — the process is not the pinned release"
    fi;;
  *) bad "the orbitd pin tag is not a release or a main-<sha7> build — /healthz has nothing to be compared with";;
esac

echo
echo "stage6-live: $PASS passed, $FAIL failed"
[ "$FAIL" -eq 0 ]
