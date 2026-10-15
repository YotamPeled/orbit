# Shared helpers for scenario setup.sh / check.sh (sourced). Every change to the world
# goes the way a real one does: a catalog edit through a pull request the driver merges,
# applied by orbitd from git. A kubectl edit would be drift the reconcile loop reverts.
#
# Needs: ORBIT_INSTANCE_DIR (a clean checkout of the instance repo), ORBIT_DESK, gh.

desk(){ "$ORBIT_DESK" "$@" 2>/dev/null | tr -d '\r' | grep -v '^Warning: Permanently added'; }

# A Windows operator machine's `python3` can be the Store's stub, which prints a hint and
# fails; `python` is the real interpreter there. Every helper and check calls python3.
if ! python3 -c 'import yaml' >/dev/null 2>&1 && python -c 'import yaml' >/dev/null 2>&1; then
  python3(){ python "$@"; }
fi

# called <tool-regex>: the transcript shows a CALL of an orbitd tool matching <tool-regex>
# (its "## call N: mcp__orbit__<tool>" heading) — a tool's name in a ToolSearch query or a
# tool list is not a call, and once let a check pass on a silence that never happened.
called(){ grep -qE "^## call [0-9]+: mcp__orbit__($1)[[:space:]]*$" "$SCENARIO_TRANSCRIPT"; }

# clean_fault_jobs <env> <since-rfc3339>: a fault window makes checkers-lab's bot runs fail
# on purpose (its target answers errors, or nothing); those failed Jobs are the scenario's
# own debris — stage3 reads any failed Job as a broken cron and KubeJobFailed fires — so
# the ones created since <since> are deleted, by name, and named in the log.
clean_fault_jobs(){
  local ns="checkers-lab-$1" jobs j
  jobs=$(desk kubectl -n "$ns" get jobs -o jsonpath='{range .items[*]}{.metadata.name} {.metadata.creationTimestamp} {.status.conditions[?(@.type=="Failed")].status}{"\n"}{end}' \
    | awk -v t0="$2" '$1 ~ /^checkers-lab-bot-/ && $2 >= t0 && $3 == "True" {print $1}')
  for j in $jobs; do desk kubectl -n "$ns" delete job "$j" >/dev/null && echo "deleted the fault window's failed bot Job $ns/$j"; done
}

# net3 <command...>: a command that talks to GitHub from the operator machine, asked up to
# three times twenty seconds apart. That machine's link and its name lookups drop for a
# moment now and then ("Could not resolve host: github.com" ended S17's setup on
# 2026-10-09), and a scenario is not about that.
net3(){
  local i
  for i in 1 2 3; do "$@" && return 0; [ "$i" = 3 ] || sleep 20; done
  return 1
}

# catalog_edit <branch-slug> <commit-message> <python-snippet>
# Runs the snippet with `cat` bound to the parsed catalog/projects.yaml (ruamel-free:
# plain PyYAML, so comments in the edited entry are not preserved), writes it back,
# regenerates, and lands it through a merged pull request. Prints the merge commit.
catalog_edit(){
  local slug=$1 msg=$2 snippet=$3 br
  br="scenario/$slug-$(date -u +%H%M%S)"
  ( cd "$ORBIT_INSTANCE_DIR" || exit 2
    git checkout -q main && net3 git pull -q --ff-only || exit 2
    git checkout -q -b "$br" || exit 2
    # a failure past this point puts the checkout back on main and drops the half-made branch
    trap 'rc=$?; [ $rc -ne 0 ] && { git checkout -q -f main; git branch -q -D "$br"; } 2>/dev/null; exit $rc' EXIT
    python3 - "$snippet" <<'PY' || exit 2
import sys, yaml
path = "catalog/projects.yaml"
text = open(path, encoding="utf-8").read()
head, sep, body = text.partition("\nprojects:")
cat = yaml.safe_load("projects:" + body.split("\n# ── a worked project")[0])
exec(sys.argv[1], {"cat": cat})
tail = body[len(body.split("\n# ── a worked project")[0]):]
open(path, "w", encoding="utf-8", newline="\n").write(head + "\n" + yaml.safe_dump(cat, sort_keys=False) + tail)
PY
    "$ORBIT_DESK" make sync >/dev/null 2>&1 || { echo "catalog_edit: make sync failed" >&2; exit 2; }
    git add -A && git commit -q -m "$msg" && net3 git push -q -u origin "$br" 2>/dev/null || exit 2
    net3 gh pr create --fill --body "Scenario setup: $msg" >/dev/null 2>&1 || gh pr view --json number >/dev/null 2>&1 || exit 2
    net3 gh pr merge --merge --delete-branch >/dev/null 2>&1 || [ "$(gh pr view --json state -q .state 2>/dev/null)" = MERGED ] || exit 2
    git checkout -q main && net3 git pull -q --ff-only && git rev-parse HEAD )
}

# catalog_append <branch-slug> <commit-message> <entry-file>
# Inserts the YAML text of <entry-file> (one `  - id: ...` project entry, indented as a
# list item) at the end of the projects list, as text — every comment in the catalog is
# kept — then regenerates and lands it through a merged pull request. Prints the merge commit.
catalog_append(){
  local slug=$1 msg=$2 entry=$3 br
  br="scenario/$slug-$(date -u +%H%M%S)"
  ( cd "$ORBIT_INSTANCE_DIR" || exit 2
    git checkout -q main && net3 git pull -q --ff-only || exit 2
    git checkout -q -b "$br" || exit 2
    # a failure past this point puts the checkout back on main and drops the half-made branch
    trap 'rc=$?; [ $rc -ne 0 ] && { git checkout -q -f main; git branch -q -D "$br"; } 2>/dev/null; exit $rc' EXIT
    python3 - "$entry" <<'PY' || exit 2
import sys
path = "catalog/projects.yaml"
text = open(path, encoding="utf-8").read()
marker = "\n# ── a worked project"
i = text.index(marker) if marker in text else len(text)
add = open(sys.argv[1], encoding="utf-8").read().rstrip("\n") + "\n"
# the entry takes the catalog's own list indentation: an entry written "  - id:" appended
# to a list written "- id:" nests under the last project's components instead
import re
want = re.search(r"^( *)- id:", text, re.M)
have = re.search(r"^( *)- id:", add, re.M)
if want and have and len(want.group(1)) != len(have.group(1)):
    cut, pad = len(have.group(1)), want.group(1)
    add = "".join(pad + l[cut:] if l[:cut].strip() == "" and l.strip() else l
                  for l in add.splitlines(True))
open(path, "w", encoding="utf-8", newline="\n").write(text[:i].rstrip("\n") + "\n\n" + add + "\n" + text[i:])
PY
    "$ORBIT_DESK" make sync >/dev/null 2>&1 || { echo "catalog_append: make sync failed" >&2; exit 2; }
    git add -A && git commit -q -m "$msg" && net3 git push -q -u origin "$br" 2>/dev/null || exit 2
    net3 gh pr create --fill --body "Scenario setup: $msg" >/dev/null 2>&1 || gh pr view --json number >/dev/null 2>&1 || exit 2
    net3 gh pr merge --merge --delete-branch >/dev/null 2>&1 || [ "$(gh pr view --json state -q .state 2>/dev/null)" = MERGED ] || exit 2
    git checkout -q main && net3 git pull -q --ff-only && git rev-parse HEAD )
}

# revert_since <base-sha> <branch-slug> <commit-message>
# Undoes every commit on the instance's main after <base-sha> (what an agent landed during
# a scenario), as one revert commit through a merged pull request — history is kept, the
# tree goes back to <base-sha>'s. Prints the merge commit, or nothing when there is nothing.
revert_since(){
  local base=$1 slug=$2 msg=$3 br
  br="scenario/$slug-$(date -u +%H%M%S)"
  ( cd "$ORBIT_INSTANCE_DIR" || exit 2
    git checkout -q main && git pull -q --ff-only || exit 2
    [ "$(git rev-parse HEAD)" = "$(git rev-parse "$base")" ] && exit 0
    git checkout -q -b "$br" || exit 2
    # a failure past this point puts the checkout back on main and drops the half-made branch
    trap 'rc=$?; [ $rc -ne 0 ] && { git checkout -q -f main; git branch -q -D "$br"; } 2>/dev/null; exit $rc' EXIT
    git read-tree -u --reset "$base" || exit 2      # base's tree exactly: added files go too
    git diff --cached --quiet && { git checkout -q main; git branch -q -D "$br"; exit 0; }
    git commit -q -m "$msg" && git push -q -u origin "$br" 2>/dev/null || exit 2
    gh pr create --fill --body "Scenario teardown: $msg" >/dev/null 2>&1 || exit 2
    gh pr merge --merge --delete-branch >/dev/null 2>&1 || exit 2
    git checkout -q main && git pull -q --ff-only && git rev-parse HEAD )
}

# set_env <project> <component> <KEY> <VALUE|->   ('-' removes the key)
set_env(){
  catalog_edit "env-$1-$2-$3" "scenario: $1/$2 env $3=$4" "
for p in cat['projects']:
    if p['id'] == '$1':
        for c in p['components']:
            if c['name'] == '$2':
                env = c.setdefault('env', {})
                if '$4' == '-': env.pop('$3', None)
                else: env['$3'] = '$4'
"
}

# wait_applied <namespace> <deployment> <timeout-s>: until the Deployment's rollout is done
wait_applied(){
  desk kubectl -n "$1" rollout status "deploy/$2" --timeout="${3:-300}s"
}

# build_variant <component> <name> "<FAULTS>"  -> prints the pushed tag <branch>-<sha7>-<name>
# Built from checkers-lab's clean, pushed main (CHECKERS_LAB_DIR), like every build.
build_variant(){
  local comp=$1 name=$2 faults=$3 tag
  ( cd "${CHECKERS_LAB_DIR:?CHECKERS_LAB_DIR is not set}" || exit 2
    git fetch -q && git checkout -q main && git pull -q --ff-only || exit 2
    [ -z "$(git status --porcelain)" ] || { echo "checkers-lab tree is not clean" >&2; exit 2; }
    tag="main-$(git rev-parse --short=7 HEAD)-$name"
    docker buildx build --platform linux/amd64 --build-arg APP="$comp" --build-arg FAULTS="$faults" \
      -t "ghcr.io/$ORBIT_SCENARIO_ORG/checkers-lab-$comp:$tag" --push . >/dev/null 2>&1 || exit 2
    echo "$tag" )
}

# driver_ship <env> <component> <tag> [<project>]: the DRIVER's ship (admin desk session), for
# setup and teardown — never the agent under test. Project defaults to checkers-lab. Waits
# for the job; prints its verdict.
driver_ship(){
  local env=$1 comp=$2 tag=$3 proj=${4:-checkers-lab}
  "$ORBIT_DESK" bash -c "
    # orbitd answers busy while the pair's lock is held (the reconcile loop applying a
    # pair that was just declared, another job): asked again, as an agent would
    for try in 1 2 3 4 5 6 7 8; do
      orbit call orbit_deploy_ship --yes --json '{\"project\":\"$proj\",\"env\":\"$env\",\"components\":[\"$comp\"],\"tag\":\"$tag\"}' > /tmp/s.json 2>&1
      J=\$(python3 -c 'import json;t=open(\"/tmp/s.json\").read();d=json.loads(t[t.index(\"{\"):]);print((d.get(\"evidence\") or {}).get(\"job_id\",\"\"))')
      [ -n \"\$J\" ] && break
      # ...and asked again when the call never reached orbitd (the operator machine's link:
      # \"failed on the transport; next: retry the call\"), as the answer itself says to
      grep -qiE 'busy|transport' /tmp/s.json || break
      sleep 15
    done
    [ -n \"\$J\" ] || { echo \"no job: \$(head -c 300 /tmp/s.json | tr '\\n' ' ')\"; exit 0; }
    for i in 1 2 3 4 5 6 7 8 9 10; do
      orbit call orbit_job_wait --json \"{\\\"job_id\\\":\\\"\$J\\\",\\\"timeout_s\\\":120}\" > /tmp/w.json 2>&1
      st=\$(python3 -c 'import json;t=open(\"/tmp/w.json\").read();print(json.loads(t[t.index(\"{\"):]).get(\"status\",\"\"))')
      case \$st in completed|failed|cancelled) break;; esac
    done
    python3 -c 'import json;t=open(\"/tmp/w.json\").read();d=json.loads(t[t.index(\"{\"):]);r=d.get(\"result\") or {};print(d.get(\"status\"), (r.get(\"evidence\") or {}).get(\"verdict\"))'
  " 2>/dev/null | tr -d '\r' | grep -v '^Warning'
}

# driver_ship_back <env> <component> <image-before>: a fault taken back, and SEEN taken
# back. A ship that fails once is asked again (three tries); returns 1, with a line that
# starts TEARDOWN FAILED, when the component is still not on the image it started from:
# run.sh then fails the scenario instead of handing the fault to the next one (the
# rebuilt box, 2026-10-08: S06b's one ship back failed and S06c started from its fault).
driver_ship_back(){
  local env=$1 comp=$2 before=$3 try back
  [ -n "$before" ] || { echo "TEARDOWN FAILED: the image $env $comp ran before the scenario was never read, so there is nothing to ship back to"; return 1; }
  for try in 1 2 3; do
    back=$(driver_ship "$env" "$comp" "${before##*:}")
    echo "driver ship back (try $try): $back"
    [ "$(image_of "$env" "$comp")" = "$before" ] && return 0
    sleep 20
  done
  echo "TEARDOWN FAILED: $env $comp is on $(image_of "$env" "$comp"), not on $before"
  return 1
}

# fault_is_planted <env> <component> <tag>: the component's Deployment carries the fault's
# tag. A ship can end `failed` and still have planted the fault (a build that crashes is
# what some scenarios plant), and it can fail before anything changed (the registry could
# not be reached: the rebuilt box, 2026-10-08, where S06b then PASSED with no fault in the
# cluster, its agent having truthfully found no 5xx). The tag is what tells them apart.
fault_is_planted(){
  local i
  for i in 1 2 3 4 5 6; do
    case "$(image_of "$1" "$2")" in *":$3") return 0;; esac
    sleep 10
  done
  echo "THE FAULT WAS NOT PLANTED: $1 $2 is on $(image_of "$1" "$2"), not on $3"
  return 1
}

# on_a_variant <image>: true when the tag is a build's tag with a fault's name after it
on_a_variant(){ [[ "$1" =~ :main-[0-9a-f]{7,}-[a-z]+$ ]]; }

# door <curl args...>: what a public door answers, as `-w` prints it (the caller passes -w).
# "000" is no answer at all: the request never got one, which from the operator machine is
# its own link as often as the door (measured, docs/V1.md §8). Asked up to three times;
# an ANSWER, whatever it is, is returned on the first ask.
door(){
  local got="" try
  for try in 1 2 3; do
    got=$(curl -s -o /dev/null --max-time 20 "$@")
    case "$got" in 000*|"") sleep 3;; *) break;; esac
  done
  echo "$got"
}

# image_of <env> <component>
image_of(){
  # asked up to three times when the answer is nothing: a desk command that could not reach
  # the cluster prints no image, and an empty "before" made a teardown ship back to no tag
  # (the rebuilt box, 2026-10-08, S08b)
  local img="" try
  for try in 1 2 3; do
    img=$(desk kubectl -n "checkers-lab-$1" get deploy "checkers-lab-$2" -o jsonpath='{.spec.template.spec.containers[0].image}' 2>/dev/null | tail -1)
    [ -n "$img" ] && break
    sleep 5
  done
  echo "$img"
}

# dev_traffic <seconds>: drive the dev api through its public door with the dev credential
dev_traffic(){
  local until=$(( $(date +%s) + $1 )) cfg
  cfg=$(mktemp); printf 'user = "orbit:%s"\n' "$(cat "${ORBIT_HOST_CONFIG_DIR:-$ORBIT_SCENARIO_MCP_DIR}/dev-basic-auth-password")" > "$cfg"
  while [ "$(date +%s)" -lt "$until" ]; do
    # a request that fails is the fault's business (that is what the scenario plants), or
    # the operator machine's link: never this function's exit status. A setup under
    # `set -e` died on the last request of the window and left its fault in place
    # (the rebuilt box, 2026-10-08, S06b).
    curl -s -m 30 -K "$cfg" -o /dev/null -X POST -H 'content-type: application/json' \
      -d '{"turnColor":"white","computerColor":"black","position":"111111111111########000000000000"}' \
      "https://checkers-lab.$ORBIT_SCENARIO_DEV_DOMAIN/api/startgame" || true
    curl -s -m 30 -K "$cfg" -o /dev/null "https://checkers-lab.$ORBIT_SCENARIO_DEV_DOMAIN/api/getwinners" || true
  done
  rm -f "$cfg"
  return 0
}

# instant_q <ns> <svc> <port> <api-path> <query> [time]: the instant value (sum of series)
# through a desk port-forward. The query travels base64-encoded: pasted into the bash -c
# string, a LogQL selector's own double quotes ended the string and curl sent nothing
# usable (2026-10-05: every ticker count came back empty). Retried, because a port-forward
# opened seconds after its pod restarted can refuse the first connection.
instant_q(){
  local q64 out i
  q64=$(printf '%s' "$5" | base64 | tr -d '\n')
  for i in 1 2 3; do
    out=$("$ORBIT_DESK" bash -c "kubectl -n $1 port-forward svc/$2 1$3:$3 >/dev/null 2>&1 & sleep 4; \
      curl -s -G http://127.0.0.1:1$3$4 --data-urlencode \"query=\$(echo $q64 | base64 -d)\" ${6:+--data-urlencode time=$6}" 2>/dev/null \
      | python3 -c 'import json,sys
try: d=json.load(sys.stdin)
except ValueError: sys.exit(1)
print(round(sum(float(r["value"][1]) for r in d["data"]["result"]),3) if d.get("status")=="success" else "error")') && { echo "$out"; return 0; }
    sleep 10
  done
  echo "unreadable"
}
promq(){ instant_q monitoring kube-prometheus-stack-prometheus 9090 /api/v1/query "$1" "${2:-}"; }
lokiq(){ instant_q loki loki 3100 /loki/api/v1/query "$1" "$2"; }

# Run one shell snippet in a throwaway pod that mounts the platform's backups volume at
# /b (read-only), from the desk image orbitd itself names. Prints the snippet's output.
backups_peek(){ # <sh snippet>
  local image name over
  image=$(desk kubectl -n orbit get deploy orbitd -o jsonpath='{.spec.template.spec.containers[0].env[?(@.name=="ORBITD_DESK_IMAGE")].value}')
  [ -n "$image" ] || { echo "backups_peek: orbitd names no desk image" >&2; return 2; }
  name="scn-peek-$$-$RANDOM"
  over=$(python3 - "$image" "$name" "$1" <<'PY'
import json, sys
image, name, snippet = sys.argv[1:4]
print(json.dumps({"apiVersion": "v1", "spec": {
    "restartPolicy": "Never", "automountServiceAccountToken": False,
    "imagePullSecrets": [{"name": "ghcr-pull"}],
    "securityContext": {"runAsNonRoot": True, "runAsUser": 10001, "runAsGroup": 10001, "fsGroup": 10001,
                        "seccompProfile": {"type": "RuntimeDefault"}},
    "containers": [{"name": name, "image": image, "command": ["sh", "-c", snippet],
                    "securityContext": {"allowPrivilegeEscalation": False, "readOnlyRootFilesystem": True,
                                        "capabilities": {"drop": ["ALL"]}},
                    "resources": {"requests": {"cpu": "10m", "memory": "32Mi"}, "limits": {"memory": "128Mi"}},
                    "volumeMounts": [{"name": "b", "mountPath": "/b", "readOnly": True}]}],
    "volumes": [{"name": "b", "persistentVolumeClaim": {"claimName": "orbit-backups", "readOnly": True}}]}}))
PY
)
  # run to completion, read the log once, delete: an attached run can print its output twice
  desk kubectl -n orbit run "$name" --restart=Never --image="$image" --overrides="$over" >/dev/null
  desk kubectl -n orbit wait pod "$name" --for=jsonpath='{.status.phase}'=Succeeded --timeout=120s >/dev/null
  desk kubectl -n orbit logs "$name"
  desk kubectl -n orbit delete pod "$name" --wait=false >/dev/null
}

# The agent was given orbitd's tools and nothing else. It may still TRY a shell (the host
# offers the tool's name); the harness refuses it, and the transcript records each try with
# `result (is_error):`. What must not exist is a shell, edit or write call that ran.
no_shell(){
  awk '/^## call [0-9]+: /{tool=$4; pending=(tool=="Bash"||tool=="Edit"||tool=="Write")}
       pending && /^result:/{ran++; pending=0}
       pending && /^result \(is_error\):/{pending=0}
       END{exit ran>0}' "$SCENARIO_TRANSCRIPT"
}

# One feature's row of orbit_features_list, as "declared=<bool> applied=<bool> ready=<bool>"
# (empty when orbitd does not answer or has no such feature).
feature_row(){ # <feature>
  "$ORBIT_DESK" orbit call orbit_features_list --json '{}' 2>/dev/null | tr -d '\r' | python3 -c '
import json, sys
text = sys.stdin.read()
try: items = json.loads(text[text.index("{"):]).get("items") or []
except ValueError: items = []
row = next((i for i in items if i.get("feature") == sys.argv[1]), None)
if row: print("declared=%s applied=%s ready=%s" % tuple(str(row.get(k)).lower() for k in ("declared", "applied", "ready")))' "$1"
}

# Wait until orbitd itself knows a project: its checkout follows the repository by up to a
# minute and its commit gate judges the commit first, so a ship asked for right after the
# catalog merge is asked of a catalog that does not have the project yet.
wait_project(){ # <project> [tries of 10 s, default 30]
  local i
  for i in $(seq 1 "${2:-30}"); do
    "$ORBIT_DESK" orbit call orbit_catalog_get --json "{\"project\":\"$1\"}" 2>/dev/null | tr -d '\r' | grep -q "\"id\": *\"$1\"" && return 0
    sleep 10
  done
  echo "wait_project: orbitd does not know $1 after $(( ${2:-30} * 10 )) s" >&2
  return 1
}
