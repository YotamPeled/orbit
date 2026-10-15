#!/usr/bin/env bash
# docs/V1.md §6.3 — alerts end to end, one per class: fault -> pending -> firing ->
# Alertmanager -> delivered (the alert-recorder's record, Telegram counters) -> resolved.
#
#   scenarios/obs/alerts-e2e.sh <out-dir> [class ...]
#
# Classes (default: all, in phases that do not interfere):
#   crashloop  a throwaway Deployment in `monitoring` that exits 1      PlatformContainerRestarted
#   oom        a throwaway Deployment in `monitoring` that outgrows 32Mi PlatformContainerRestarted (exit 137)
#   reserve    a pause pod requesting enough memory to pass 90% of allocatable  OrbitNodeReservationHigh
#   platmem    a throwaway pod in `monitoring` holding 1.5 GiB RSS      OrbitPlatformMemoryOverBudget
#   disk       a file on the node leaving ~12% of / free                NodeDiskLow
#   dbdown     checkers-lab-dev's database fenced (Postgres stopped)    OrbitDatabaseDown
#   errors     checkers-lab dev api answering 60% 5xx under traffic     OrbitIngress5xxRateHigh
#   latency    checkers-lab dev api answering in 1.5 s under traffic    OrbitIngressLatencyHigh
#   down       Grafana and orbitd scaled to zero                        OrbitSiteDown (two doors)
#   walfail    prod's archive target pointed at a missing bucket while orbitd is down  OrbitWALArchiveFailing
#
# Every fault is reverted before the script ends, also on failure (trap). Faults that
# orbitd would revert within its 3-minute reconcile (dbdown, walfail) are written by a
# field manager that is not a hand-edit tool, or while orbitd is down. Each class prints
# one ok/FAIL line per stage; the summary counts them. Needs ORBIT_INSTANCE_DIR and the
# node's ssh alias (ORBIT_NODE_SSH, default `orbit`) for the disk class.
set -uo pipefail
here=$(cd "$(dirname "$0")" && pwd)
OUT=${1:?usage: alerts-e2e.sh <out-dir> [class ...]}; shift
mkdir -p "$OUT"
export SCENARIO_OUT="$OUT"
. "$here/../lib.sh"
: "${ORBIT_DESK:=$ORBIT_INSTANCE_DIR/scripts/desk.sh}"; export ORBIT_DESK
NODE=${ORBIT_NODE_SSH:-orbit}
PASS=0 FAIL=0
ok(){ echo "  ok    $1"; PASS=$((PASS+1)); }
bad(){ echo "  FAIL  $1"; FAIL=$((FAIL+1)); }
T0=$(date -u +%s); T0ISO=$(date -u +%Y-%m-%dT%H:%M:%SZ)
REVERTS=()
on_exit(){ local r; for r in "${REVERTS[@]}"; do eval "$r" >/dev/null 2>&1 || true; done; }
trap on_exit EXIT

# ---- reading Alertmanager and the recorder ------------------------------------------
am_pod(){ desk kubectl -n monitoring get pod -l app.kubernetes.io/name=alertmanager -o jsonpath='{.items[0].metadata.name}'; }
# am_state <alertname> <label-regex>: "active" | "suppressed" | "none"
am_state(){
  desk kubectl -n monitoring exec "$(am_pod)" -c alertmanager -- wget -qO- \
      "http://localhost:9093/api/v2/alerts?filter=alertname%3D$1&active=true&silenced=true&inhibited=true" \
    | python3 -c '
import json, re, sys
a = [x for x in json.loads(sys.stdin.read() or "[]")
     if re.search(sys.argv[1], json.dumps(x["labels"], sort_keys=True))]
print(a[0]["status"]["state"] if a else "none")' "$2"
}
# wait_am <alertname> <label-regex> <want: active|none> <seconds>
wait_am(){
  local until=$(( $(date +%s) + $4 )) s
  while [ "$(date +%s)" -lt "$until" ]; do
    s=$(am_state "$1" "$2"); [ "$s" = "$3" ] && return 0
    sleep 30
  done
  return 1
}
# recorded <alertname> <status> <label-regex>: the recorder logged that notification
recorded(){
  desk kubectl -n monitoring logs deploy/alert-recorder --since-time="$T0ISO" \
    | python3 -c '
import json, re, sys
name, status, rx = sys.argv[1:4]
for line in sys.stdin:
    try: r = json.loads(line)
    except ValueError: continue
    if r.get("alertname") == name and r.get("status") == status and re.search(rx, json.dumps(r, sort_keys=True)):
        sys.exit(0)
sys.exit(1)' "$1" "$2" "$3"
}
# A firing alert joining a group Alertmanager already notified (same alertname and namespace:
# crashloop beside oom, Grafana's door beside orbitd's) is sent with that group's next
# notification, up to one group_interval (5m) later, plus group_wait (30s). 2026-10-05: both
# such notifications were delivered and recorded, after the old 240 s wait had given up.
FIRING_WAIT=420
wait_recorded(){
  local until=$(( $(date +%s) + $4 ))
  while [ "$(date +%s)" -lt "$until" ]; do recorded "$1" "$2" "$3" && return 0; sleep 30; done
  return 1
}
# judge <class> <alertname> <label-regex> <fire-timeout-s> <revert-command>
judge(){
  local class=$1 name=$2 rx=$3 to=$4 revert=$5
  if wait_am "$name" "$rx" active "$to"; then ok "$class: $name is firing in Alertmanager"; else bad "$class: $name did not fire within $to s"; eval "$revert"; return; fi
  if wait_recorded "$name" firing "$rx" "$FIRING_WAIT"; then ok "$class: the firing notification was delivered (alert-recorder)"; else bad "$class: no firing notification reached the alert-recorder"; fi
  eval "$revert"
  if wait_am "$name" "$rx" none 1200; then ok "$class: $name resolved after the fault was reverted"; else bad "$class: $name still active 20 min after the revert"; fi
  if wait_recorded "$name" resolved "$rx" 600; then ok "$class: the resolved notification was delivered"; else bad "$class: no resolved notification reached the alert-recorder"; fi
}

# ---- throwaway pods in monitoring (crashloop, oom, platmem) ---------------------------
e2e_deploy(){ # <name> <memory-limit> <python-or-shell command json array>
  python3 - "$1" "$2" "$3" > "$OUT/$1.json" <<'PY'
import json, sys
name, limit, cmd = sys.argv[1], sys.argv[2], json.loads(sys.argv[3])
print(json.dumps({"apiVersion": "apps/v1", "kind": "Deployment",
  "metadata": {"name": name, "namespace": "monitoring", "labels": {"orbit.local/e2e": "alerts"}},
  "spec": {"replicas": 1, "selector": {"matchLabels": {"app": name}}, "template": {
    "metadata": {"labels": {"app": name, "orbit.local/e2e": "alerts"}},
    "spec": {"automountServiceAccountToken": False, "terminationGracePeriodSeconds": 1,
      "securityContext": {"runAsNonRoot": True, "runAsUser": 65532, "seccompProfile": {"type": "RuntimeDefault"}},
      "containers": [{"name": name, "image": "python:3.13-alpine", "command": cmd,
        "resources": {"requests": {"cpu": "5m", "memory": limit}, "limits": {"memory": limit}},
        "securityContext": {"allowPrivilegeEscalation": False, "capabilities": {"drop": ["ALL"]}}}]}}}}))
PY
  desk kubectl apply -f - < "$OUT/$1.json" >/dev/null
  REVERTS+=("desk kubectl -n monitoring delete deploy $1 --ignore-not-found")
}

class_crashloop(){
  e2e_deploy e2e-crashloop 16Mi '["sh","-c","sleep 5; exit 1"]'
  judge crashloop PlatformContainerRestarted '"pod": "e2e-crashloop' 1500 "desk kubectl -n monitoring delete deploy e2e-crashloop --ignore-not-found"
}
class_oom(){
  e2e_deploy e2e-oom 32Mi '["python","-c","import time; time.sleep(5); b=bytearray(128*1024*1024); time.sleep(60)"]'
  judge oom PlatformContainerRestarted '"pod": "e2e-oom' 1500 "desk kubectl -n monitoring delete deploy e2e-oom --ignore-not-found"
}
# A tenant workload that fills its memory limit and stays alive (a pod in checkers-lab-dev:
# the alert is for tenant namespaces). 116 Mi touched inside 128 Mi, with the interpreter
# about 94%.
class_memlimit(){
  python3 - > "$OUT/e2e-memlimit.json" <<'PY'
import json
print(json.dumps({"apiVersion": "v1", "kind": "Pod", "metadata": {"name": "e2e-memlimit", "namespace": "checkers-lab-dev", "labels": {"orbit.local/e2e": "alerts"}},
  "spec": {"automountServiceAccountToken": False, "restartPolicy": "Never", "terminationGracePeriodSeconds": 1,
    "securityContext": {"runAsNonRoot": True, "runAsUser": 65532, "seccompProfile": {"type": "RuntimeDefault"}},
    "containers": [{"name": "e2e-memlimit", "image": "python:3.13-alpine",
      "command": ["python", "-c", "import time; b=bytearray(116*1024*1024); [b.__setitem__(i,1) for i in range(0,len(b),4096)]; time.sleep(7200)"],
      "resources": {"requests": {"cpu": "5m", "memory": "128Mi"}, "limits": {"memory": "128Mi"}},
      "securityContext": {"allowPrivilegeEscalation": False, "capabilities": {"drop": ["ALL"]}}}]}}))
PY
  desk kubectl apply -f - < "$OUT/e2e-memlimit.json" >/dev/null
  REVERTS+=("desk kubectl -n checkers-lab-dev delete pod e2e-memlimit --ignore-not-found --force --grace-period=0")
  judge memlimit OrbitWorkloadMemoryAtLimit 'e2e-memlimit' 1800 "desk kubectl -n checkers-lab-dev delete pod e2e-memlimit --ignore-not-found --force --grace-period=0"
}
class_platmem(){
  e2e_deploy e2e-platmem 1700Mi '["python","-c","import time; b=bytearray(1536*1024*1024); [b.__setitem__(i,1) for i in range(0,len(b),4096)]; time.sleep(7200)"]'
  judge platmem OrbitPlatformMemoryOverBudget '' 3000 "desk kubectl -n monitoring delete deploy e2e-platmem --ignore-not-found"
}

# ---- node reservation: requests, not usage --------------------------------------------
class_reserve(){
  local alloc req want
  alloc=$(desk kubectl get node -o jsonpath='{.items[0].status.allocatable.memory}' | python3 -c 'import sys;v=sys.stdin.read().strip();print(int(v[:-2])*1024 if v.endswith("Ki") else int(v))')
  req=$("$ORBIT_DESK" bash -c "orbit call orbit_metrics_query --json '{\"query\":\"sum(cluster:namespace:pod_memory:active:kube_pod_container_resource_requests)\"}'" 2>/dev/null \
    | python3 -c 'import json,sys;t=sys.stdin.read();d=json.loads(t[t.index("{"):]);print(int(float(d["items"][0]["values"][-1][-1])))')
  want=$(( (alloc * 93 / 100 - req) / 1048576 ))
  [ "$want" -gt 0 ] || { bad "reserve: requests already at $req of $alloc"; return; }
  desk kubectl create namespace e2e-reserve >/dev/null 2>&1
  REVERTS+=("desk kubectl delete namespace e2e-reserve --ignore-not-found --wait=false")
  python3 - "$want" > "$OUT/reserve.json" <<'PY'
import json, sys
mi = sys.argv[1]
print(json.dumps({"apiVersion": "v1", "kind": "Pod", "metadata": {"name": "reserve", "namespace": "e2e-reserve"},
  "spec": {"automountServiceAccountToken": False,
    "securityContext": {"runAsNonRoot": True, "runAsUser": 65532, "seccompProfile": {"type": "RuntimeDefault"}},
    "containers": [{"name": "pause", "image": "registry.k8s.io/pause:3.10",
      "resources": {"requests": {"cpu": "1m", "memory": f"{mi}Mi"}, "limits": {"memory": f"{mi}Mi"}},
      "securityContext": {"allowPrivilegeEscalation": False, "capabilities": {"drop": ["ALL"]}}}]}}))
PY
  desk kubectl apply -f - < "$OUT/reserve.json" >/dev/null
  judge reserve OrbitNodeReservationHigh '' 1800 "desk kubectl delete namespace e2e-reserve --ignore-not-found --wait=false"
}

# ---- node disk -------------------------------------------------------------------------
class_disk(){
  local size free bytes
  read -r size free < <(ssh "$NODE" 'df -B1 --output=size,avail / | tail -1')
  bytes=$(( free - size * 12 / 100 ))
  [ "$bytes" -gt 0 ] || { bad "disk: / already under 12% free"; return; }
  ssh "$NODE" "fallocate -l $bytes /var/tmp/orbit-e2e-disk.fill" || { bad "disk: could not place the fill file"; return; }
  REVERTS+=("ssh $NODE 'rm -f /var/tmp/orbit-e2e-disk.fill'")
  judge disk NodeDiskLow '' 3000 "ssh $NODE 'rm -f /var/tmp/orbit-e2e-disk.fill'"
}

# ---- database down: fenced, by a field manager the hand-edit revert does not touch -----
class_dbdown(){
  local fence='{"metadata":{"annotations":{"cnpg.io/fencedInstances":"[\"checkers-lab-db-1\"]"}}}'
  local unfence='{"metadata":{"annotations":{"cnpg.io/fencedInstances":null}}}'
  desk kubectl -n checkers-lab-dev patch clusters.postgresql.cnpg.io checkers-lab-db --type=merge --field-manager=orbit-e2e-fault -p "$fence" >/dev/null
  REVERTS+=("desk kubectl -n checkers-lab-dev patch clusters.postgresql.cnpg.io checkers-lab-db --type=merge --field-manager=orbit-e2e-fault -p '$unfence'")
  judge dbdown OrbitDatabaseDown '"namespace": "checkers-lab-dev"' 1200 \
    "desk kubectl -n checkers-lab-dev patch clusters.postgresql.cnpg.io checkers-lab-db --type=merge --field-manager=orbit-e2e-fault -p '$unfence'"
}

# ---- ingress errors and latency: a dev api variant under traffic ----------------------
dev_variant_window(){ # <class> <variant-name> <FAULTS> <alertname>
  local before tag
  before=$(image_of dev api)
  tag=$(build_variant api "$2" "$3")
  driver_ship dev api "$tag" >/dev/null
  REVERTS+=("driver_ship dev api ${before##*:}")
  nohup bash -c ". '$here/../lib.sh'; until [ -e '$OUT/stop-$1' ]; do dev_traffic 60; done" >/dev/null 2>&1 &
  REVERTS+=("touch '$OUT/stop-$1'")
  judge "$1" "$4" 'checkers-lab-dev.*api' 1500 "touch '$OUT/stop-$1'; driver_ship dev api ${before##*:} >/dev/null"
  clean_fault_jobs dev "$T0ISO"
}
class_errors(){ dev_variant_window errors errors "ERROR_RATE=0.6" OrbitIngress5xxRateHigh; }
class_latency(){ dev_variant_window latency slow "SLOW_ENDPOINT_MS=1500" OrbitIngressLatencyHigh; }

# ---- doors down (Grafana, orbitd) and WAL archiving failing while orbitd is down -----
class_down_and_walfail(){
  local store_ep
  desk kubectl -n monitoring scale deploy kube-prometheus-stack-grafana --replicas=0 >/dev/null
  REVERTS+=("desk kubectl -n monitoring scale deploy kube-prometheus-stack-grafana --replicas=1")
  desk kubectl -n orbit scale deploy orbitd --replicas=0 >/dev/null
  REVERTS+=("desk kubectl -n orbit scale deploy orbitd --replicas=1")
  store_ep=$(desk kubectl -n checkers-lab-prod get objectstores.barmancloud.cnpg.io checkers-lab-db-store -o jsonpath='{.spec.configuration.destinationPath}')
  echo "$store_ep" > "$OUT/walfail-destination"
  desk kubectl -n checkers-lab-prod patch objectstores.barmancloud.cnpg.io checkers-lab-db-store --type=merge \
    --field-manager=orbit-e2e-fault -p '{"spec":{"configuration":{"destinationPath":"s3://orbit-e2e-missing-bucket/cnpg"}}}' >/dev/null
  REVERTS+=("desk kubectl -n checkers-lab-prod patch objectstores.barmancloud.cnpg.io checkers-lab-db-store --type=merge --field-manager=orbit-e2e-fault -p '{\"spec\":{\"configuration\":{\"destinationPath\":\"$store_ep\"}}}'")
  # a WAL switch so the archiver has something to fail on
  desk kubectl -n checkers-lab-prod exec checkers-lab-db-1 -c postgres -- psql -d checkers-lab -Atc 'select pg_switch_wal()' >/dev/null 2>&1
  judge down-grafana OrbitSiteDown 'grafana' 900 "desk kubectl -n monitoring scale deploy kube-prometheus-stack-grafana --replicas=1"
  if wait_am OrbitSiteDown 'orbit\.' active 600; then ok "down-orbitd: OrbitSiteDown fires for orbitd's door"; else bad "down-orbitd: OrbitSiteDown did not fire for orbitd"; fi
  wait_recorded OrbitSiteDown firing 'orbit\.' "$FIRING_WAIT" && ok "down-orbitd: the firing notification was delivered" || bad "down-orbitd: no firing notification reached the alert-recorder"
  if wait_am OrbitWALArchiveFailing 'checkers-lab-prod' active 1500; then ok "walfail: OrbitWALArchiveFailing is firing"; else bad "walfail: OrbitWALArchiveFailing did not fire"; fi
  wait_recorded OrbitWALArchiveFailing firing 'checkers-lab-prod' "$FIRING_WAIT" && ok "walfail: the firing notification was delivered" || bad "walfail: no firing notification reached the alert-recorder"
  desk kubectl -n checkers-lab-prod patch objectstores.barmancloud.cnpg.io checkers-lab-db-store --type=merge --field-manager=orbit-e2e-fault \
    -p "{\"spec\":{\"configuration\":{\"destinationPath\":\"$store_ep\"}}}" >/dev/null
  desk kubectl -n orbit scale deploy orbitd --replicas=1 >/dev/null
  wait_am OrbitSiteDown 'orbit\.' none 900 && ok "down-orbitd: resolved once orbitd is back" || bad "down-orbitd: still active"
  wait_recorded OrbitSiteDown resolved 'orbit\.' 600 && ok "down-orbitd: the resolved notification was delivered" || bad "down-orbitd: no resolved notification"
  wait_am OrbitWALArchiveFailing 'checkers-lab-prod' none 2400 && ok "walfail: resolved once the archive target is back" || bad "walfail: still active 40 min after the revert"
}

# ---- the run ---------------------------------------------------------------------------
CLASSES=("$@")
[ "${#CLASSES[@]}" -eq 0 ] && CLASSES=(crashloop oom memlimit reserve platmem disk dbdown errors latency down)
echo "== alerts end to end ($(date -u +%FT%TZ)): ${CLASSES[*]}"
pids=()
for c in "${CLASSES[@]}"; do
  case "$c" in
    errors|latency|down|reserve) ;;   # sequential below: they share the dev api, orbitd; reserve last
    *) ( REVERTS=(); trap on_exit EXIT; "class_$c" ) > "$OUT/$c.log" 2>&1 & pids+=($!) ;;
  esac
done
for c in errors latency down; do
  case " ${CLASSES[*]} " in *" $c "*)
    if [ "$c" = down ]; then class_down_and_walfail > "$OUT/down.log" 2>&1; else "class_$c" > "$OUT/$c.log" 2>&1; fi ;;
  esac
done
for p in "${pids[@]}"; do wait "$p"; done
# reserve books 93% of the node's memory, measured when it starts: beside platmem's 1.7 GiB
# (2026-10-05) the two did not fit, the reserve pod stayed Pending, and a Pending pod's
# requests are on no node, so the alert rightly never saw them. It runs alone, last.
case " ${CLASSES[*]} " in *" reserve "*) ( REVERTS=(); trap on_exit EXIT; class_reserve ) > "$OUT/reserve.log" 2>&1 ;; esac
cat "$OUT"/*.log | grep -E '^  (ok|FAIL) ' | tee "$OUT/summary.txt"
sent_failed=$("$ORBIT_DESK" bash -c "orbit call orbit_metrics_query --json '{\"query\":\"sum(increase(alertmanager_notifications_total{integration=\\\"telegram\\\"}[$(( ($(date -u +%s) - T0) / 60 + 2 ))m])) or vector(0)\"}'" 2>/dev/null | python3 -c 'import json,sys;t=sys.stdin.read();d=json.loads(t[t.index("{"):]);print(round(sum(float(p[-1]) for x in d.get("items") or [] for p in x.get("values") or []),1))')
echo "  telegram notifications sent during the run: $sent_failed" | tee -a "$OUT/summary.txt"
p=$(grep -c '^  ok ' "$OUT/summary.txt"); f=$(grep -c '^  FAIL ' "$OUT/summary.txt")
echo "alerts-e2e: $p passed, $f failed"
[ "$f" -eq 0 ]
