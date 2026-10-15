#!/usr/bin/env bash
# Point-in-time recovery of a prod database's DATA, to a moment before something destroyed
# it (a bad migration, a wrong DELETE). The desk command the orbit-restore-db skill drives.
#
#   scripts/pitr-restore.sh <project> --to <RFC3339 UTC time> [--yes]
#
# 1. Restores <project>-db from the platform bucket's base backup + WAL into a throwaway
#    namespace, recovered to --to (recoveryTarget.targetTime) — the same mechanism
#    scripts/pitr-drill.sh proves on every run.
# 2. Prints, per table, the live row count beside the restored one, and stops unless --yes.
# 3. With --yes: dumps the application database from the restored copy and loads it into
#    the LIVE primary in one transaction (pg_dump --clean --if-exists | psql
#    --single-transaction), so the live database ends with exactly the restored data. Writes
#    made after --to are lost; that is what restoring to a point means.
# 4. Prints the per-table counts after, and deletes the throwaway namespace.
#
# Run from the instance repo root through the desk (scripts/desk.sh), with the sops key
# (the bucket credential is copied into the throwaway namespace, sealed). It never reads a
# Secret's value. Touches the live database only in step 3, and only with --yes.
set -euo pipefail
P="${1:?usage: pitr-restore.sh <project> --to <RFC3339> [--yes]}"; shift
TO=""; YES=0
while [ $# -gt 0 ]; do
  case "$1" in
    --to) TO="${2:-}"; shift 2;;
    --yes) YES=1; shift;;
    *) echo "unknown argument (not repeated)"; exit 2;;
  esac
done
case "$P" in (*[!a-z0-9-]*|"") echo "project id is not a catalog id"; exit 2;; esac
# An RFC3339 instant in UTC, nothing else: it lands in a YAML scalar.
[[ "$TO" =~ ^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(\.[0-9]+)?Z$ ]] \
  || { echo "--to must be an RFC3339 UTC instant like 2026-10-04T15:00:00Z"; exit 2; }
NS="orbit-pitr-restore"
SRC_NS="$P-prod"
[ -f instance/config.yaml ] || { echo "no instance/config.yaml here — run from the instance repo root"; exit 2; }
ENDPOINT=$(python3 -c "import yaml; print(((yaml.safe_load(open('instance/config.yaml')) or {}).get('objectStorage') or {}).get('endpoint',''))")
[ -n "$ENDPOINT" ] || { echo "no objectStorage endpoint in instance/config.yaml — no bucket, no PITR"; exit 2; }
kubectl -n "$SRC_NS" get cluster "$P-db" >/dev/null || { echo "$SRC_NS/$P-db does not exist"; exit 2; }
# A comparison (no --yes) deletes its throwaway namespace without waiting, and the restore
# that follows it starts seconds later (S07, 2026-10-06: it was refused here, after its
# safety backup, and the data stayed lost). A namespace that is on its way out is waited
# for; one that is not is somebody's evidence and is left alone.
for i in $(seq 1 60); do
  [ "$(kubectl get ns "$NS" -o jsonpath='{.status.phase}' 2>/dev/null)" = Terminating ] || break
  [ "$i" = 1 ] && echo "waiting for the previous run's namespace $NS to finish going (up to 5m)..."
  sleep 5
done
if kubectl get ns "$NS" >/dev/null 2>&1; then
  if [ "$(kubectl get ns "$NS" -o jsonpath='{.status.phase}' 2>/dev/null)" = Terminating ]; then
    echo "FAIL: namespace $NS has been terminating for 5 minutes; nothing was changed"; exit 1
  fi
  # A run that failed marks its scratch namespace and leaves it for half an hour, for whoever
  # wants to look. After that, or once marked, the NEXT run takes it away itself: an agent
  # that reaches this script through orbitd has no way to delete a namespace, and until
  # 2026-10-09 one failed comparison blocked every later one ("inspect it, then: kubectl
  # delete ns", addressed to nobody; docs/V1.md §8). A namespace that is neither marked
  # nor old is a run in progress and still a stop.
  STATE=$(kubectl get ns "$NS" -o jsonpath='{.metadata.annotations.orbit\.local/pitr-run}' 2>/dev/null || true)
  AGE=$(( $(date +%s) - $(date -d "$(kubectl get ns "$NS" -o jsonpath='{.metadata.creationTimestamp}' 2>/dev/null)" +%s 2>/dev/null || date +%s) ))
  if [ "$STATE" = failed ] || [ "$AGE" -ge 1800 ]; then
    echo "removing the scratch namespace an earlier run left ($NS, ${AGE}s old, ${STATE:-unmarked}): $(kubectl -n "$NS" get clusters.postgresql.cnpg.io -o jsonpath='{range .items[*]}{.metadata.name} was in phase {.status.phase}{end}' 2>/dev/null || true)"
    kubectl delete ns "$NS" --wait=true --timeout=300s >/dev/null 2>&1 \
      || { echo "FAIL: the earlier run's namespace $NS could not be removed in 5 minutes; nothing was changed"; exit 1; }
  else
    echo "namespace $NS exists and is ${AGE}s old: another restore is running (or failed without saying so)."
    echo "It is taken away by the next run once it is 30 minutes old."; exit 2
  fi
fi
SEALED_COPY=""; DONE=0
finish(){
  [ -n "$SEALED_COPY" ] && rm -f "$SEALED_COPY"
  if [ "$DONE" = 1 ]; then kubectl delete ns "$NS" --wait=false >/dev/null 2>&1 || true
  else
    # A run that did not finish says what its scratch copy looked like and takes it away.
    # It used to be kept "for inspection": on the rebuilt test box (2026-10-09) the kept
    # namespace held a failed Job and a database with no healthy instance for hours, which
    # is three firing alerts and a red `make verify` for a platform with nothing wrong, and
    # nobody an agent could ask to inspect it. What an inspection would read is printed here.
    echo "--- restore did not finish; what the scratch copy in $NS looked like, before it is removed ---"
    kubectl annotate ns "$NS" orbit.local/pitr-run=failed --overwrite >/dev/null 2>&1 || true
    kubectl -n "$NS" get clusters.postgresql.cnpg.io -o jsonpath='{range .items[*]}cluster {.metadata.name}: phase "{.status.phase}", {.status.readyInstances} ready instance(s){"\n"}{end}' 2>/dev/null || true
    kubectl -n "$NS" get pods --no-headers 2>/dev/null | awk '{print "pod " $1 ": " $3 " (" $2 " ready, " $4 " restarts)"}' | head -6 || true
    for pod in $(kubectl -n "$NS" get pods -o name 2>/dev/null | head -2); do
      kubectl -n "$NS" logs "$pod" --all-containers --tail=200 2>/dev/null \
        | grep -iE '"level":"(error|fatal)"|error|fatal|could not|no such|not found|denied' | tail -4 | cut -c1-300 | sed "s|^|${pod#pod/}: |" || true
    done
    kubectl delete ns "$NS" --wait=false >/dev/null 2>&1 || true
    echo "--- $NS removed ---"
  fi
}
trap finish EXIT

PRIMARY=$(kubectl -n "$SRC_NS" get pod -l cnpg.io/cluster="$P-db",cnpg.io/instanceRole=primary -o jsonpath='{.items[0].metadata.name}')
DBNAME=$(kubectl -n "$SRC_NS" get cluster "$P-db" -o jsonpath='{.spec.bootstrap.initdb.database}')
case "$DBNAME" in *[!a-z0-9_-]*|-*|*-|"") echo "the database name in the Cluster spec is not a catalog id — refused"; exit 2;; esac
# Close the current WAL segment so everything up to now is in the archive.
kubectl -n "$SRC_NS" exec "$PRIMARY" -c postgres -- psql -d "$DBNAME" -At -c "select pg_switch_wal()" >/dev/null
sleep 20

SRC_DEST=$(kubectl -n "$SRC_NS" get objectstores.barmancloud.cnpg.io "$P-db-store" -o jsonpath='{.spec.configuration.destinationPath}')
[ -n "$SRC_DEST" ] || { echo "no ObjectStore $P-db-store in $SRC_NS — WAL archiving is not deployed, so there is no point to restore to"; exit 2; }
SRC_SC=$(kubectl -n "$SRC_NS" get cluster "$P-db" -o jsonpath='{.spec.storage.storageClass}')
IMAGE=$(kubectl -n "$SRC_NS" get cluster "$P-db" -o jsonpath='{.spec.imageName}')
SIZE=$(kubectl -n "$SRC_NS" get cluster "$P-db" -o jsonpath='{.spec.storage.size}')

kubectl create ns "$NS" >/dev/null
kubectl label ns "$NS" pod-security.kubernetes.io/enforce=restricted pod-security.kubernetes.io/warn=restricted >/dev/null
kubectl -n "$SRC_NS" get networkpolicy default-deny-ingress -o json \
  | python3 -c "import sys,json; s=json.load(sys.stdin); [s['metadata'].pop(k,None) for k in ('namespace','uid','resourceVersion','creationTimestamp','managedFields','labels','annotations')]; print(json.dumps(s))" \
  | kubectl -n "$NS" apply -f - >/dev/null
if [ -n "${ORBIT_IN_CLUSTER:-}" ]; then
  # Run by orbitd in a Job on the cluster, where there is no sealing key and no Secret is
  # ever read: the Job was handed the bucket keys in its environment (from the sealed
  # orbit/orbit-s3), and the throwaway namespace's Secret is written from them on stdin.
  [ -n "${ORBIT_S3_ACCESS_KEY:-}" ] && [ -n "${ORBIT_S3_SECRET_KEY:-}" ] \
    || { echo "FAIL: this Job was given no bucket keys (the sealed secret orbit/orbit-s3 is missing), so the bucket's backup cannot be read"; exit 1; }
  # the two values go to kubectl as files in a private directory (never an argument), under
  # the key names every copy of orbit-s3 carries
  KEYDIR=$(umask 077; mktemp -d)
  printf '%s' "$ORBIT_S3_ACCESS_KEY" > "$KEYDIR/access-key"; printf '%s' "$ORBIT_S3_SECRET_KEY" > "$KEYDIR/secret-key"
  kubectl -n "$NS" create secret generic orbit-s3 --from-file=access-key="$KEYDIR/access-key" --from-file=secret-key="$KEYDIR/secret-key" >/dev/null
  rm -rf "$KEYDIR"
else
  SEALED_COPY=$(orbit secrets copy . --file "clusters/platform/secrets/$SRC_NS/orbit-s3.yaml" --namespace "$NS")
  kubectl apply -f "$SEALED_COPY" >/dev/null
fi
for i in $(seq 1 60); do kubectl -n "$NS" get secret orbit-s3 >/dev/null 2>&1 && break; sleep 2; done
kubectl -n "$NS" get secret orbit-s3 >/dev/null 2>&1 || { echo "no Secret appeared in $NS from the sealed copy — is the sops operator running?"; exit 1; }
cat <<EOF | kubectl apply -f - >/dev/null
apiVersion: barmancloud.cnpg.io/v1
kind: ObjectStore
metadata: {name: restore-origin, namespace: $NS}
spec:
  configuration:
    destinationPath: $SRC_DEST
    endpointURL: $ENDPOINT
    s3Credentials:
      accessKeyId: {name: orbit-s3, key: access-key}
      secretAccessKey: {name: orbit-s3, key: secret-key}
    wal: {compression: gzip}
EOF
SC_LINE=""; [ -n "$SRC_SC" ] && SC_LINE="storageClass: $SRC_SC"
cat <<EOF | kubectl apply -f - >/dev/null
apiVersion: postgresql.cnpg.io/v1
kind: Cluster
metadata: {name: restored, namespace: $NS}
spec:
  instances: 1
  imageName: $IMAGE
  enableSuperuserAccess: false
  bootstrap:
    recovery:
      source: origin
      database: $DBNAME
      owner: $P
      recoveryTarget:
        targetTime: "$TO"
  externalClusters:
    - name: origin
      plugin:
        name: barman-cloud.cloudnative-pg.io
        parameters: {barmanObjectName: restore-origin, serverName: $P-db}
  storage:
    size: $SIZE
    $SC_LINE
  resources:
    requests: {cpu: 100m, memory: 256Mi}
    limits: {memory: 256Mi}
EOF
echo "restoring $P-db to $TO (up to 10m)..."
PHASE=""
for i in $(seq 1 120); do
  PHASE=$(kubectl -n "$NS" get cluster restored -o jsonpath='{.status.phase}' 2>/dev/null || true)
  [ "$PHASE" = "Cluster in healthy state" ] && break
  sleep 5
done
[ "$PHASE" = "Cluster in healthy state" ] || { echo "FAIL: the restore did not become healthy in 10m (phase '$PHASE')"; exit 1; }
RPOD=$(kubectl -n "$NS" get pod -l cnpg.io/cluster=restored,cnpg.io/instanceRole=primary -o jsonpath='{.items[0].metadata.name}')

counts(){ # <namespace> <pod> -> "schema.table rows" lines, exact counts
  kubectl -n "$1" exec -i "$2" -c postgres -- psql -d "$DBNAME" -At 2>&1 <<'SQL' | sed -n 's/^NOTICE:  //p'
DO $$ DECLARE r record; n bigint; BEGIN
  FOR r IN SELECT schemaname, tablename FROM pg_tables WHERE schemaname NOT IN ('pg_catalog','information_schema') ORDER BY 1,2 LOOP
    EXECUTE format('SELECT count(*) FROM %I.%I', r.schemaname, r.tablename) INTO n;
    RAISE NOTICE '% %', r.schemaname || '.' || r.tablename, n;
  END LOOP; END $$;
SQL
}
echo "== rows: live now | restored to $TO"
join -a1 -a2 -e 0 -o 0,1.2,2.2 <(counts "$SRC_NS" "$PRIMARY" | sort) <(counts "$NS" "$RPOD" | sort) | awk '{print "  " $1, $2, $3}'
if [ "$YES" != 1 ]; then
  echo "nothing changed on the live database. Re-run with --yes to replace its data with the restored copy (writes after $TO are lost)."
  DONE=1; exit 0
fi
echo "== loading the restored data into the live primary (one transaction)"
# Owners and grants travel with the data. The load runs as the superuser, so a dump
# without them (--no-owner --no-privileges) recreated every table owned by postgres and
# the application's own role lost access to its tables: S07's restore put the right rows
# back and the leaderboard answered 500 until the owners were fixed by hand (docs/V1.md
# §8). The restored copy is a physical recovery of the same cluster, so its roles are the
# live database's roles.
kubectl -n "$NS" exec "$RPOD" -c postgres -- pg_dump -d "$DBNAME" --clean --if-exists \
  | kubectl -n "$SRC_NS" exec -i "$PRIMARY" -c postgres -- psql -d "$DBNAME" -q --single-transaction -v ON_ERROR_STOP=1 >/dev/null
echo "== rows: live after the restore"
counts "$SRC_NS" "$PRIMARY" | awk '{print "  " $1, $2}'
# Prove the application can still use what was loaded: every relation outside the system
# schemas belongs to the database's owner, as initdb made it.
OWNER=$(kubectl -n "$SRC_NS" get cluster "$P-db" -o jsonpath='{.spec.bootstrap.initdb.owner}')
case "$OWNER" in *[!a-z0-9_-]*|-*|*-|"") echo "the owner in the Cluster spec is not a catalog id — ownership not verified"; exit 1;; esac
WRONG=$(kubectl -n "$SRC_NS" exec -i "$PRIMARY" -c postgres -- psql -d "$DBNAME" -At -v owner="$OWNER" <<'SQL'
SELECT n.nspname || '.' || c.relname || ' (' || pg_get_userbyid(c.relowner) || ')'
FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
WHERE n.nspname NOT IN ('pg_catalog', 'information_schema') AND n.nspname NOT LIKE 'pg\_%'
  AND c.relkind IN ('r', 'p', 'S', 'v', 'm') AND pg_get_userbyid(c.relowner) <> :'owner';
SQL
)
if [ -n "$WRONG" ]; then
  echo "LOADED, BUT NOT OWNED BY $OWNER — the application cannot use these: $(echo $WRONG)"
  echo "fix: ALTER TABLE/SEQUENCE ... OWNER TO \"$OWNER\" for each, then check the application"
  DONE=1; exit 1
fi
echo "== every relation is owned by $OWNER"
DONE=1
echo "RESTORED: $SRC_NS/$P-db data is as of $TO"
