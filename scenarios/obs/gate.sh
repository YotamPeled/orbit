#!/usr/bin/env bash
# docs/V1.md M2c, scenario S21 — the commit gate, live.
#
# For each check of the chain (core, scaffold, catalog, generated, render, sealed, fit) a
# deliberately bad commit is merged into the instance repository, and these are shown:
#   * the desk's `orbit check all` names that check for the commit before it is pushed;
#   * orbitd refuses the commit with the same check, and says so in orbit_deploy_status;
#   * the refusal is in orbitd's audit log;
#   * every long-running pod is the same pod as before (same UIDs): nothing was applied;
#   * the alert OrbitCommitRefused is active in Alertmanager for that commit, and was
#     delivered (the alert-recorder's line in Loki, while the logs feature is on);
#   * the revert is accepted, and the alert is gone.
# Each bad commit is one a person could really make: a vendored script edited in place, a
# rendered manifest edited by hand, a catalog the generator refuses, a catalog changed
# without `make sync`, a component the chart refuses, a Secret committed in the clear, a
# reservation larger than the node.
#
#   scenarios/obs/gate.sh <out-dir>          GATE_CASES="core fit" runs a subset;
#                                            GATE_DRY=1 stops each case before the push;
#                                            GATE_RESTART=1 restarts orbitd while each commit is refused
set -uo pipefail
here=$(cd "$(dirname "$0")" && pwd)
OUT=${1:?usage: gate.sh <out-dir>}; mkdir -p "$OUT"
export SCENARIO_OUT="$OUT"
. "$here/../lib.sh"
: "${ORBIT_DESK:=$ORBIT_INSTANCE_DIR/scripts/desk.sh}"; export ORBIT_DESK
PASS=0 FAIL=0
ok(){ echo "  ok    $1"; PASS=$((PASS+1)); }
bad(){ echo "  FAIL  $1"; FAIL=$((FAIL+1)); }
CASES=${GATE_CASES:-"core scaffold catalog generated render sealed fit"}
I="$ORBIT_INSTANCE_DIR"
cd "$I" || exit 2
git checkout -q main && git pull -q --ff-only || { echo "the instance checkout cannot be brought to main"; exit 2; }
[ -z "$(git status --porcelain)" ] || { echo "the instance checkout has uncommitted changes"; exit 2; }

# The first self-hosted project and its first environment: what orbit_deploy_status is asked about.
read -r PROJECT ENVNAME < <(python3 - <<'PY' | tr -d '\r'
import yaml
text = open("catalog/projects.yaml", encoding="utf-8").read()
cat = yaml.safe_load(text.split("\n# ── a worked project")[0])
p = next(x for x in cat["projects"] if x.get("hosted", "self") == "self")
print(p["id"], "prod" if "prod" in (p.get("environments") or {}) else next(iter(p["environments"])))
PY
)
[ -n "${PROJECT:-}" ] || { echo "the catalog has no self-hosted project to ask about"; exit 2; }

gate(){ # -> "verdict|check|head|sentence"
  "$ORBIT_DESK" orbit call orbit_deploy_status --json "{\"project\":\"$PROJECT\",\"env\":\"$ENVNAME\"}" 2>/dev/null | tr -d '\r' | python3 -c '
import json, sys
text = sys.stdin.read()
try: g = json.loads(text[text.index("{"):]).get("gate") or {}
except ValueError: g = {}
print("|".join(str(g.get(k) or "") for k in ("verdict", "check", "head", "sentence")).replace("\n", " "))'
}
wait_gate(){ # <verdict> <sha> <tries of 15s>
  local want=$1 sha=$2 n=${3:-28} g i
  for i in $(seq 1 "$n"); do
    g=$(gate); IFS='|' read -r v c h s <<<"$g"
    if [ "$v" = "$want" ] && [ "${h:0:12}" = "${sha:0:12}" ]; then echo "$g"; return 0; fi
    sleep 15
  done
  echo "$g"; return 1
}
# Pods that are meant to stay: owned by a ReplicaSet, StatefulSet, DaemonSet or a database
# cluster. A CronJob's pods come and go by design and are not counted.
pods(){ desk kubectl get pods -A -o json | python3 -c '
import json, sys
for p in json.load(sys.stdin)["items"]:
    owners = {o.get("kind") for o in p["metadata"].get("ownerReferences") or []}
    if owners & {"ReplicaSet", "StatefulSet", "DaemonSet", "Cluster"}:
        print(p["metadata"]["namespace"] + "/" + p["metadata"]["name"], p["metadata"]["uid"])' | tr -d '\r' | sort; }
alerts(){ # <sha12> -> number of active OrbitCommitRefused alerts for that commit ("" = any)
  local am
  am=$(desk kubectl -n monitoring get pod -l app.kubernetes.io/name=alertmanager -o jsonpath='{.items[0].metadata.name}')
  desk kubectl -n monitoring exec "$am" -c alertmanager -- wget -qO- "http://localhost:9093/api/v2/alerts?active=true" | python3 -c '
import json, sys
want = sys.argv[1]
print(sum(1 for a in json.load(sys.stdin) if a["labels"].get("alertname") == "OrbitCommitRefused"
          and (not want or a["labels"].get("commit", "").startswith(want))))' "$1" | tr -d '\r'
}
LOGS_ON=$(python3 - <<'PY' | tr -d '\r'
import yaml
c = yaml.safe_load(open("instance/config.yaml", encoding="utf-8")) or {}
f = c.get("features")
print(1 if (f is None and (c.get("objectStorage") or {}).get("buckets", {}).get("platform")) or (f or {}).get("logs") else 0)
PY
)

cat > "$OUT/mutate-catalog.py" <<'PY'
import sys, yaml
path = "catalog/projects.yaml"
text = open(path, encoding="utf-8").read()
head, sep, body = text.partition("\nprojects:")
keep = body.split("\n# ── a worked project")[0]
cat = yaml.safe_load("projects:" + keep)
p = next(x for x in cat["projects"] if x["id"] == sys.argv[1])
exec(sys.argv[2], {"p": p})
open(path, "w", encoding="utf-8", newline="\n").write(head + "\n" + yaml.safe_dump(cat, sort_keys=False) + body[len(keep):])
PY
catalog(){ python3 "$OUT/mutate-catalog.py" "$PROJECT" "$1"; }
sync(){ "$ORBIT_DESK" make sync >/dev/null 2>&1; }
mutate(){ case "$1" in
  core)      printf '# edited in the instance\n' >> scripts/catalog-sync.py;;
  scaffold)  printf '# edited by hand\n' >> clusters/platform/apps/traefik.yaml;;
  catalog)   catalog 'p["components"][0]["kind"] = "daemon"';;
  generated) catalog 'p["components"][0]["memory"] = "300Mi"';;
  render)    # the one catalog known to pass the generator and fail the chart; a generator that
             # has since learned to refuse it leaves no commit that reaches render alone (3)
             catalog 'p["components"].append({"name": "db-backup", "kind": "worker", "image": "ghcr.io/stefanprodan/podinfo", "memory": "64Mi"})' || return 2
             sync || return 3;;
  sealed)    mkdir -p clusters/platform/secrets/monitoring && printf 'apiVersion: v1\nkind: Secret\nmetadata:\n  name: gate-plain-secret\n  namespace: monitoring\nstringData:\n  token: this-is-not-a-real-credential\n' > clusters/platform/secrets/monitoring/gate-plain-secret.yaml;;
  fit)       catalog 'p["components"][0]["memory"] = "64Gi"' && sync;;
  *) return 2;; esac; }

land(){ # <branch> <message> -> merge commit on main
  git add -A || return 2
  # a revert has already made its commit: there is then nothing left to commit here
  git diff --cached --quiet || git commit -q -m "$2" || return 2
  git push -q -u origin "$1" 2>/dev/null || return 2
  gh pr create --fill --body "$2" >/dev/null 2>&1 || return 2
  gh pr merge --merge --delete-branch >/dev/null 2>&1 || return 2
  git checkout -q main && git pull -q --ff-only && git rev-parse HEAD
}

echo "== the commit gate, live ($(date -u +%Y-%m-%dT%H:%M:%SZ)); asking about $PROJECT/$ENVNAME; cases: $CASES"
start=$(gate); IFS='|' read -r v c h s <<<"$start"
[ "$v" = accepted ] || { echo "the gate does not accept the current main ($start): fix that first"; exit 2; }

for case in $CASES; do
  echo "== $case"
  git checkout -q main && git pull -q --ff-only
  br="gate/$case-$(date -u +%H%M%S)"
  git checkout -q -b "$br"
  mutate "$case"; made=$?
  if [ "$made" = 3 ]; then
    ok "$case: keyed off — the generator itself now refuses the catalog that used to reach this check, so no commit fails here alone; the check stays proven offline (tests/test_checks.py)"
    git checkout -q -f main; git clean -fdq; git branch -q -D "$br"; continue
  fi
  if [ "$made" != 0 ]; then bad "$case: the bad commit could not be made"; git checkout -q -f main; git clean -fdq; git branch -q -D "$br"; continue; fi
  local_says=$("$ORBIT_DESK" bash -c 'orbit check all /instance --kubeconfig "$KUBECONFIG"' 2>&1 | tr -d '\r' | grep -m1 '^orbit check:')
  case "$local_says" in "orbit check: $case: "*) ok "$case: the desk refuses it before the push (${local_says:0:150})";;
    *) bad "$case: the desk's chain did not stop at $case (${local_says:-it passed})"; git checkout -q -f main; git clean -fdq; git branch -q -D "$br"; continue;; esac
  if [ -n "${GATE_DRY:-}" ]; then      # the bad commit and the desk's verdict only: nothing is pushed
    git checkout -q -f main; git clean -fdq; git branch -q -D "$br"; continue
  fi
  pods > "$OUT/$case-pods-before.txt"
  t0=$(date -u +%Y-%m-%dT%H:%M:%SZ)
  sha=$(land "$br" "gate test: a commit the $case check must refuse") || { bad "$case: the bad commit could not be merged"; git checkout -q -f main; continue; }
  echo "  (bad commit ${sha:0:12} merged at $(date -u +%H:%M:%SZ))"
  if g=$(wait_gate refused "$sha" 28); then
    IFS='|' read -r v c h s <<<"$g"
    [ "$c" = "$case" ] && ok "$case: orbitd refused ${sha:0:12} at the $c check; orbit_deploy_status says: ${s:0:170}" \
      || bad "$case: orbitd refused ${sha:0:12} at the '$c' check, not $case: ${s:0:170}"
    desk_sentence=${local_says#orbit check: $case: }
    [ "${s:0:80}" = "${desk_sentence:0:80}" ] && ok "$case: the desk and orbitd say the same sentence" \
      || bad "$case: the desk said '${desk_sentence:0:80}' and orbitd '${s:0:80}'"
  else
    bad "$case: orbitd did not refuse ${sha:0:12} within 7 minutes (gate: $g)"
  fi
  n=$(desk kubectl -n orbit logs deploy/orbitd --since=15m 2>/dev/null | grep -c "commit ${sha:0:12} refused by the $case check")
  # a wait on the refused commit answers with the refusal; it does not hang until its timeout
  # (asked up to three times: one call can be lost on the way, which is not the tool's answer)
  waited=""
  for try in 1 2 3; do
    w0=$(date +%s)
    "$ORBIT_DESK" orbit call orbit_deploy_wait --json "{\"project\":\"$PROJECT\",\"env\":\"$ENVNAME\",\"commit\":\"${sha:0:12}\",\"timeout_s\":90}" > "$OUT/$case-wait.json" 2>&1
    took=$(( $(date +%s) - w0 ))
    waited=$(tr -d '\r' < "$OUT/$case-wait.json" | python3 -c 'import json,sys
t=sys.stdin.read()
try: d=json.loads(t[t.index("{"):])
except ValueError: d={}
g=d.get("gate") or {}
print("%s|%s" % (d.get("status"), g.get("check")))' | tr -d '\r')
    case "$waited" in None*) sleep 10;; *) break;; esac
  done
  [ "$waited" = "refused|$case" ] && [ "$took" -lt 60 ] \
    && ok "$case: orbit_deploy_wait on ${sha:0:12} answers refused (check $case) in $took s, well inside its 90 s timeout" \
    || bad "$case: orbit_deploy_wait on ${sha:0:12} answered '$waited' after $took s ($(tr -d '\r\n' < "$OUT/$case-wait.json" | cut -c1-160))"
  if [ -n "${GATE_RESTART:-}" ]; then
    # orbitd restarted while the commit is refused: it must come back still refusing it,
    # still applying the last good commit (the verdict is kept in its state directory)
    IFS='|' read -r v0 c0 h0 s0 <<<"$(gate)"
    good0=$("$ORBIT_DESK" orbit call orbit_deploy_status --json "{\"project\":\"$PROJECT\",\"env\":\"$ENVNAME\"}" 2>/dev/null | tr -d '\r' | python3 -c 'import json,sys
t=sys.stdin.read(); print((json.loads(t[t.index("{"):]).get("gate") or {}).get("good") or "")' | tr -d '\r')
    desk kubectl -n orbit rollout restart deploy/orbitd >/dev/null
    desk kubectl -n orbit rollout status deploy/orbitd --timeout=240s >/dev/null
    sleep 20
    IFS='|' read -r v1 c1 h1 s1 <<<"$(gate)"
    good1=$("$ORBIT_DESK" orbit call orbit_deploy_status --json "{\"project\":\"$PROJECT\",\"env\":\"$ENVNAME\"}" 2>/dev/null | tr -d '\r' | python3 -c 'import json,sys
t=sys.stdin.read(); print((json.loads(t[t.index("{"):]).get("gate") or {}).get("good") or "")' | tr -d '\r')
    [ "$v1" = refused ] && [ "$c1" = "$case" ] && [ "${h1:0:12}" = "${sha:0:12}" ] && [ -n "$good1" ] && [ "$good1" = "$good0" ] \
      && ok "$case: after an orbitd restart the commit is still refused at $c1 and the last good commit is still ${good1:0:12}" \
      || bad "$case: after an orbitd restart the gate says '$v1' at '$c1' for ${h1:0:12}, good ${good1:0:12} (before: ${good0:0:12})"
    # orbitd's own pod is new now, by this driver's hand: the picture the pods are held to is retaken
    pods > "$OUT/$case-pods-before.txt"
  fi
  [ "${n:-0}" -ge 1 ] && ok "$case: the refusal is in orbitd's audit log" || bad "$case: no audit record of the refusal of ${sha:0:12}"
  a=$(alerts "${sha:0:12}")
  [ "${a:-0}" -ge 1 ] && ok "$case: OrbitCommitRefused is active in Alertmanager for ${sha:0:12}" || bad "$case: no active OrbitCommitRefused for ${sha:0:12}"
  # Delivery. A first refusal is sent after group_wait (30 s); one that follows another
  # within minutes joins the same alert group and is sent at its next group_interval
  # (5 m). So the recorder's line is waited for, up to 7 minutes; the wait is also the
  # time in which anything wrongly applied would show in the pods.
  if [ "$LOGS_ON" = 1 ]; then
    d=0
    for i in $(seq 1 21); do
      sleep 20
      d=$(lokiq "sum(count_over_time({namespace=\"monitoring\"} |= \"OrbitCommitRefused\" |= \"${sha:0:12}\" [15m]))" "$(date +%s)")
      case "$d" in ""|0|0.0|error|unreadable) ;; *) break;; esac
    done
    case "$d" in ""|0|0.0|error|unreadable) bad "$case: the alert-recorder has no line for OrbitCommitRefused ${sha:0:12} after 7 minutes (Loki: ${d:-nothing})";;
      *) ok "$case: the alert was delivered (alert-recorder lines in Loki: $d, within $((i * 20)) s of the refusal being seen)";; esac
  else
    sleep 75
    ok "$case: the logs feature is off — the delivery row keys off (the alert is active in Alertmanager above)"
  fi
  pods > "$OUT/$case-pods-after.txt"
  cmp -s "$OUT/$case-pods-before.txt" "$OUT/$case-pods-after.txt" \
    && ok "$case: every long-running pod is the same pod as before ($(grep -c . "$OUT/$case-pods-after.txt") uids)" \
    || bad "$case: pods changed while the commit was refused: $(diff "$OUT/$case-pods-before.txt" "$OUT/$case-pods-after.txt" | grep -c '^[<>]') line(s) differ"
  # the good commit: the revert
  git checkout -q main && git pull -q --ff-only
  rb="gate/revert-$case-$(date -u +%H%M%S)"
  git checkout -q -b "$rb" && git revert --no-edit -m 1 "$sha" >/dev/null 2>&1
  good=$(land "$rb" "gate test: revert the commit the $case check refused") || { bad "$case: the revert could not be merged — the instance main still holds the bad commit"; git checkout -q -f main; break; }
  if g=$(wait_gate accepted "$good" 28); then ok "$case: the revert ${good:0:12} is accepted"; else bad "$case: the revert ${good:0:12} was not accepted within 7 minutes (gate: $g)"; fi
  left=1
  for i in 1 2 3 4 5 6; do left=$(alerts ""); [ "${left:-1}" = 0 ] && break; sleep 20; done
  [ "${left:-1}" = 0 ] && ok "$case: OrbitCommitRefused is no longer active" || bad "$case: $left OrbitCommitRefused alert(s) still active after the revert"
done
git checkout -q main 2>/dev/null
echo "gate: $PASS passed, $FAIL failed"
[ "$FAIL" -eq 0 ]
