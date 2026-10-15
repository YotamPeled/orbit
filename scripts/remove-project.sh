#!/usr/bin/env bash
# Remove a project from the cluster after its catalog entry is gone (V1 §8, S17).
#
#   scripts/desk.sh scripts/remove-project.sh <project> [--yes]
#
# Run from the instance checkout: through the desk, or by the orbit_project_remove tool
# in a Job on the cluster (which is how an agent removes a project).
# orbitd prunes only inside a pair that still renders, and the platform `services` prune
# refuses Namespaces by design, so a project dropped from the catalog leaves its namespaces,
# its databases and its monitoring objects behind. This is the supported path:
#
# 1. Refuses unless the project is absent from catalog/projects.yaml in a clean checkout
#    (the removal itself lands through a catalog pull request first, then `make sync`).
# 2. Lists what would go: every namespace labelled orbit.local/project=<project>, what runs
#    in it, its databases, and the monitoring-namespace objects carrying the same label.
#    Without --yes it stops there and changes nothing.
# 3. With --yes, first backs up every CloudNativePG database in those namespaces with
#    scripts/db-backup.sh: a dump into the backups directory (0600), restored into a
#    scratch database and compared table by table with the rows it was taken from, and
#    copied to the bucket when there is one. A backup that is refused stops the removal
#    before anything is deleted (V1 §2: never delete data without a verified backup).
# 4. Prunes the platform `services` app (the namespace list, probes, dashboards), deletes
#    the labelled monitoring objects, then the namespaces, and waits for them to go.
# 5. Proves it: no namespace and no monitoring object carries the label, the services plan
#    is empty, then prints `REMOVED: <project>`.
set -euo pipefail
P=${1:-}; YES=0
[ "${2:-}" = "--yes" ] && YES=1
case "$P" in ""|-*|*[!a-z0-9-]*) echo "usage: remove-project.sh <project-id> [--yes]"; exit 2;; esac
[ -f instance/config.yaml ] || { echo "no instance/config.yaml here — run from the instance repo root"; exit 2; }
KCFG=${KUBECONFIG:-}
LABEL="orbit.local/project=$P"
MON_NS=monitoring
MON_KINDS=servicemonitors.monitoring.coreos.com,podmonitors.monitoring.coreos.com,prometheusrules.monitoring.coreos.com,probes.monitoring.coreos.com,configmaps,secrets

if python3 - "$P" <<'PY'
import sys, yaml
cat = yaml.safe_load(open("catalog/projects.yaml", encoding="utf-8")) or {}
sys.exit(0 if any((p or {}).get("id") == sys.argv[1] for p in cat.get("projects") or []) else 1)
PY
then
  echo "REFUSED: $P is still in catalog/projects.yaml — remove it there through a pull request, run 'make sync', merge, then run this"
  exit 2
fi
if [ -n "$(git status --porcelain 2>/dev/null)" ]; then
  echo "REFUSED: the instance checkout has uncommitted changes — the cluster must be compared with a committed catalog"
  exit 2
fi

mapfile -t NSS < <(kubectl get ns -l "$LABEL" -o jsonpath='{range .items[*]}{.metadata.name}{"\n"}{end}')
MON=$(kubectl -n "$MON_NS" get "$MON_KINDS" -l "$LABEL" -o name 2>/dev/null || true)
if [ "${#NSS[@]}" -eq 0 ] && [ -z "$MON" ]; then
  echo "nothing labelled $LABEL is left in the cluster"
  echo "REMOVED: $P"; exit 0
fi

echo "== what $P left behind"
DBS=()
for ns in "${NSS[@]}"; do
  echo "namespace $ns:"
  kubectl -n "$ns" get deploy,cronjob,statefulset,pvc -o name 2>/dev/null | sed 's/^/  /'
  while IFS= read -r c; do
    [ -n "$c" ] && { DBS+=("$ns/$c"); echo "  database $c (CloudNativePG)"; }
  done < <(kubectl -n "$ns" get clusters.postgresql.cnpg.io -o jsonpath='{range .items[*]}{.metadata.name}{"\n"}{end}' 2>/dev/null)
done
[ -n "$MON" ] && { echo "monitoring objects:"; sed 's/^/  /' <<<"$MON"; }

if [ "$YES" != 1 ]; then
  echo "dry run: nothing changed. With --yes: back up ${#DBS[@]} database(s) and verify the dumps, then delete the above."
  exit 0
fi

# A database that archives to the bucket leaves its archive there, and a project made under
# the name again must not be pointed at it (barman refuses the path and archives nothing).
# `catalog-edit.py remove` (orbit_project_remove) writes the generation down in
# catalog/retired.yaml; an entry deleted by hand did not, and that is refused here, before
# anything is deleted, with the line to add.
for db in "${DBS[@]}"; do
  ns=${db%%/*}
  [ -n "$(kubectl -n "$ns" get objectstores.barmancloud.cnpg.io -o name 2>/dev/null)" ] || continue
  if ! python3 - "$P" <<'PY'
import os, sys, yaml
doc = yaml.safe_load(open("catalog/retired.yaml", encoding="utf-8")) if os.path.isfile("catalog/retired.yaml") else {}
sys.exit(0 if isinstance((doc or {}).get("retired"), dict) and sys.argv[1] in doc["retired"] else 1)
PY
  then
    GEN=$(kubectl -n "$ns" get objectstores.barmancloud.cnpg.io -o jsonpath='{.items[0].spec.configuration.destinationPath}' 2>/dev/null | sed -n 's|.*/gen\([0-9][0-9]*\)$|\1|p')
    echo "REFUSED: $P has a database that archives to the bucket, and catalog/retired.yaml does not name $P."
    echo "Its archive stays in the bucket; without the line, a project made under this name again would archive nothing."
    echo "Add through a pull request (under 'retired:'):   $P: ${GEN:-1}   then run 'make sync', merge, and run this again. Nothing was deleted."
    exit 2
  fi
  break
done

# Every database is backed up by the one backup path (scripts/db-backup.sh): dumped in
# one snapshot, restored into a scratch database, row counts equal — or nothing is deleted.
HERE="$(cd "$(dirname "$0")" && pwd)"
BACKUP_DIR="${ORBIT_BACKUP_DIR:-${ORBIT_CONFIG_DIR:-$HOME/.config/orbit}/backups}"
for nc in "${DBS[@]}"; do
  ns=${nc%%/*}; c=${nc#*/}
  if ! ORBIT_BACKUP_DIR="$BACKUP_DIR" "$HERE/db-backup.sh" "$ns" "$c"; then
    echo "REFUSED: $nc has no verified backup — nothing deleted"
    exit 2
  fi
done

echo "== pruning the services app"
echo "prune services" | orbit platform apply "$PWD" --app services --prune ${KCFG:+--kubeconfig "$KCFG"} > /dev/null
if [ -n "$MON" ]; then
  echo "== deleting the monitoring objects"
  # shellcheck disable=SC2086
  kubectl -n "$MON_NS" delete $MON --ignore-not-found --wait=true
fi
for ns in "${NSS[@]}"; do
  echo "== deleting namespace $ns"
  kubectl delete ns "$ns" --wait=true --timeout=600s
  # orbitd's record of what it applied to the pair lives in namespace orbit, not in the
  # namespace that just went. Left behind, a project made again under this name starts
  # with a stale record of objects that no longer exist. Its absence proves nothing
  # about the removal, so a delete that is refused is said and not failed on.
  kubectl -n orbit delete configmap "orbit-inventory-$ns" --ignore-not-found >/dev/null 2>&1 \
    || echo "note: configmap orbit/orbit-inventory-$ns could not be deleted; it is orbitd's record of a namespace that is gone"
done

left_ns=$(kubectl get ns -l "$LABEL" -o name)
left_mon=$(kubectl -n "$MON_NS" get "$MON_KINDS" -l "$LABEL" -o name 2>/dev/null || true)
plan=$(orbit platform plan "$PWD" --app services --summary ${KCFG:+--kubeconfig "$KCFG"} 2>&1 >/dev/null | tail -1)
echo "left: namespaces [${left_ns}] monitoring [${left_mon}]"
echo "services plan: $plan"
if [ -n "$left_ns$left_mon" ] || ! grep -q 'empty=true' <<<"$plan"; then
  echo "NOT REMOVED: something labelled $LABEL is still there or the services app has drift (above)"
  exit 1
fi
echo "REMOVED: $P (backups in $BACKUP_DIR)"
