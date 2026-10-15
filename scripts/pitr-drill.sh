#!/usr/bin/env bash
# PITR restore drill: prove the bucket's base-backup + WAL chain can actually become a
# running database again. A backup nobody has restored is a hope, not a backup — this is
# the drill OBSERVABILITY.md's Phase 1 requires, runnable on demand (it moves real data,
# so it is not part of every verify run).
#
#   scripts/pitr-drill.sh <project>
#
# Restores <project>-db from the instance's platform bucket into a throwaway namespace,
# PINNED to the instant the live reference was taken (recoveryTarget.targetLSN), so the
# row-count comparison is exact by construction rather than exact-if-nothing-moved. On
# PASS the namespace is deleted; on FAIL it is KEPT with diagnostics printed, because a
# failed restore's evidence is the whole point of running a drill.
#
# Touches on the live side: two read-only queries and one pg_switch_wal() — which closes
# the current WAL segment so the segment covering the reference moment reaches the bucket.
# That is an ordinary archiving nudge, not a data change.
set -euo pipefail
P="${1:?usage: pitr-drill.sh <project>}"
case "$P" in (*[!a-z0-9-]*|"") echo "project id '$P' is not a catalog id"; exit 1;; esac
NS="orbit-pitr-drill"
SRC_NS="$P-prod"

[ -f instance/config.yaml ] || { echo "no instance/config.yaml here — run from the instance repo root"; exit 1; }
BUCKET=$(python3 -c "import yaml; print((((yaml.safe_load(open('instance/config.yaml')) or {}).get('objectStorage') or {}).get('buckets') or {}).get('platform',''))")
ENDPOINT=$(python3 -c "import yaml; print(((yaml.safe_load(open('instance/config.yaml')) or {}).get('objectStorage') or {}).get('endpoint',''))")
[ -n "$BUCKET" ] && [ -n "$ENDPOINT" ] || { echo "no objectStorage bucket/endpoint in instance/config.yaml"; exit 1; }
kubectl -n "$SRC_NS" get cluster "$P-db" >/dev/null || { echo "$SRC_NS/$P-db does not exist"; exit 1; }

if kubectl get ns "$NS" >/dev/null 2>&1; then
  # the same rule as pitr-restore.sh: a namespace half an hour old is an earlier drill's
  # leftover and is taken away; a younger one is a drill in progress and a stop
  AGE=$(( $(date +%s) - $(date -d "$(kubectl get ns "$NS" -o jsonpath='{.metadata.creationTimestamp}' 2>/dev/null)" +%s 2>/dev/null || date +%s) ))
  if [ "$AGE" -ge 1800 ]; then
    echo "removing the namespace an earlier drill left ($NS, ${AGE}s old)"
    kubectl delete ns "$NS" --wait=true --timeout=300s >/dev/null 2>&1 \
      || { echo "FAIL: the earlier drill's namespace $NS could not be removed in 5 minutes"; exit 1; }
  else
    echo "namespace $NS exists and is ${AGE}s old: another drill is running."
    echo "It is taken away by the next drill once it is 30 minutes old."
    exit 1
  fi
fi

PASSED=0
SEALED_COPY=""
finish() {
  if [ -n "$SEALED_COPY" ]; then rm -f "$SEALED_COPY"; fi
  if [ "$PASSED" = 1 ]; then
    kubectl delete ns "$NS" --wait=false >/dev/null 2>&1 || true
  else
    # what an inspection would read is printed, and the namespace goes: kept, it is a
    # database with no healthy instance, which is firing alerts and a red `make verify`
    echo "--- drill FAILED; what its copy in $NS looked like, before it is removed ---"
    kubectl -n "$NS" get cluster drill -o jsonpath='{.status.phase}: {.status.conditions}' 2>/dev/null; echo
    kubectl -n "$NS" get pods 2>/dev/null || true
    kubectl -n "$NS" logs -l cnpg.io/cluster=drill --tail=20 --prefix 2>/dev/null | tail -30 || true
    kubectl delete ns "$NS" --wait=false >/dev/null 2>&1 || true
  fi
}
trap finish EXIT

# Live reference FIRST, with its timestamp; the restore below is pinned to this moment.
PRIMARY=$(kubectl -n "$SRC_NS" get pod -l cnpg.io/cluster="$P-db",cnpg.io/instanceRole=primary -o jsonpath='{.items[0].metadata.name}')
# The database name is in the Cluster's own spec — never the application Secret,
# whose contents nothing this platform ships may render.
DBNAME=$(kubectl -n "$SRC_NS" get cluster "$P-db" -o jsonpath='{.spec.bootstrap.initdb.database}')
[ -n "$DBNAME" ] || { echo "no database in $SRC_NS/$P-db spec (.spec.bootstrap.initdb.database)"; exit 1; }
# The chart names the database after the project id (letters, digits, hyphens), and the name
# reaches only psql -d argv and a YAML scalar, never SQL text: the catalog-id class is the gate.
case "$DBNAME" in *[!a-z0-9_-]*|-*|*-) echo "the database name in $SRC_NS/$P-db spec is not a catalog id (letters, digits, hyphens) — refused (not repeated)"; exit 1;; esac
# n_live_tup > 0: comparing an empty table restores 0==0 and proves nothing — the
# leaderboard table of the tenant this was first run against was legitimately empty, and
# the drill would have passed vacuously.
TABLE=$(kubectl -n "$SRC_NS" exec "$PRIMARY" -c postgres -- psql -d "$DBNAME" -At -c \
  "select relname from pg_stat_user_tables where n_live_tup > 0 order by n_live_tup desc limit 1")
[ -n "$TABLE" ] || { echo "no user table with rows in live $DBNAME — nothing to prove a restore against"; exit 1; }
# The table name is the tenant's own catalog text (they own the database and may name a
# table anything, a double quote included) and it is spliced into a quoted identifier
# below, run by the superuser the exec lands as. Only an unquoted-identifier shape passes;
# anything else is refused by description, never repeated (round 1 of the area-2 sweep).
case "$TABLE" in
  *[!a-z0-9_]*|[0-9]*|"") echo "the busiest live table in $DBNAME has a name that is not a plain identifier (lowercase letters, digits, underscore) — the drill does not quote arbitrary names; rename it or pick the project by hand"; exit 1;;
esac
[ "${#TABLE}" -le 63 ] || { echo "the busiest live table in $DBNAME has a name longer than 63 bytes — refused"; exit 1; }
# The pin is the LSN of a WAL marker record emitted IN THE SAME STATEMENT as the count.
# Not a timestamp: recovery_target_time is only "reached" when a newer COMMIT replays,
# which never happens on a quiet database. Not pg_current_wal_lsn either: at a segment
# boundary that returns a position no WAL record ever *passes* (and pg_switch_wal on an
# empty segment is a no-op), so recovery still hits end-of-WAL and PG17 FATALs. Both
# found by execution. The marker (pg_logical_emit_message — a WAL-only record, no table
# data) is guaranteed to replay, and replaying it satisfies the target.
REF=$(kubectl -n "$SRC_NS" exec "$PRIMARY" -c postgres -- psql -d "$DBNAME" -At -F'|' -c \
  "select count(*), pg_logical_emit_message(false, 'orbit-pitr-drill', 'marker') from \"$TABLE\"")
LIVE_COUNT=${REF%|*}; TARGET_LSN=${REF#*|}
case "$LIVE_COUNT" in ''|*[!0-9]*) echo "the live row count is not a number — refused (not repeated)"; exit 1;; esac
case "$TARGET_LSN" in ''|*[!0-9A-F/]*) echo "the marker LSN is not of an LSN shape — refused (not repeated)"; exit 1;; esac
echo "live reference: $DBNAME.$TABLE = $LIVE_COUNT rows, marker at LSN $TARGET_LSN"
# Close the current WAL segment so the archive provably contains the reference moment.
kubectl -n "$SRC_NS" exec "$PRIMARY" -c postgres -- psql -d "$DBNAME" -At -c "select pg_switch_wal()" >/dev/null
sleep 20

# The live cluster's own archive path — archiveGeneration may have been bumped
# post-recovery, so read it from the deployed ObjectStore rather than reconstructing.
SRC_DEST=$(kubectl -n "$SRC_NS" get objectstores.barmancloud.cnpg.io "$P-db-store" \
  -o jsonpath='{.spec.configuration.destinationPath}')
[ -n "$SRC_DEST" ] || { echo "no ObjectStore $P-db-store in $SRC_NS — is WAL archiving deployed?"; exit 1; }
SRC_SC=$(kubectl -n "$SRC_NS" get cluster "$P-db" -o jsonpath='{.spec.storage.storageClass}')
IMAGE=$(kubectl -n "$SRC_NS" get cluster "$P-db" -o jsonpath='{.spec.imageName}')
SIZE=$(kubectl -n "$SRC_NS" get cluster "$P-db" -o jsonpath='{.spec.storage.size}')

# Same fences as every generated namespace: restored prod data does not sit in a softer
# namespace just because it is temporary.
kubectl create ns "$NS"
kubectl label ns "$NS" pod-security.kubernetes.io/enforce=restricted pod-security.kubernetes.io/warn=restricted >/dev/null
# Copied from the source namespace, not authored here: the generated policy is
# deny-plus-allowlist (same-namespace, traefik, monitoring, cnpg-system), and a bare
# deny REJECTS the operator's status probe — the restored cluster then sits at
# "Instance Status Extraction Error" forever. Found by execution.
kubectl -n "$SRC_NS" get networkpolicy default-deny-ingress -o json \
  | python3 -c "import sys,json; s=json.load(sys.stdin); [s['metadata'].pop(k,None) for k in ('namespace','uid','resourceVersion','creationTimestamp','managedFields','labels','annotations')]; print(json.dumps(s))" \
  | kubectl -n "$NS" apply -f - >/dev/null
# The bucket credential, placed for the drill's lifetime by rewriting the sealed
# file's namespace — the ciphertext is untouched and only the in-cluster operator
# decrypts it. Never round-trip the live object here: rendering the value would
# force a rotation of the bucket key. The verb prints the copy's path, and that
# is what finish() removes: the name comes from the sealed document itself, so
# the drill takes it from the verb rather than spelling it out.
SEALED_COPY=$(orbit secrets copy . --file "clusters/platform/secrets/$SRC_NS/orbit-s3.yaml" --namespace "$NS")
kubectl apply -f "$SEALED_COPY" >/dev/null
# The operator renders the Secret asynchronously and the restore mounts it, so the
# sealed file being applied is not enough — wait for the Secret to exist first.
for i in $(seq 1 60); do
  kubectl -n "$NS" get secret orbit-s3 >/dev/null 2>&1 && break
  sleep 2
done
kubectl -n "$NS" get secret orbit-s3 >/dev/null 2>&1 \
  || { echo "sealed $SEALED_COPY applied but no Secret appeared in $NS — is the sops operator running?"; exit 1; }
# The recovery source, pointing at the SOURCE cluster's archive path verbatim.
cat <<EOF | kubectl apply -f - >/dev/null
apiVersion: barmancloud.cnpg.io/v1
kind: ObjectStore
metadata: {name: drill-origin, namespace: $NS}
spec:
  configuration:
    destinationPath: $SRC_DEST
    endpointURL: $ENDPOINT
    s3Credentials:
      accessKeyId: {name: orbit-s3, key: access-key}
      secretAccessKey: {name: orbit-s3, key: secret-key}
    wal: {compression: gzip}
EOF

# Same storage class as the source: on a box whose default class differs or is absent,
# the restore would hang ten minutes and fail for a reason unrelated to the backup.
SC_LINE=""
[ -n "$SRC_SC" ] && SC_LINE="storageClass: $SRC_SC"
cat <<EOF | kubectl apply -f - >/dev/null
apiVersion: postgresql.cnpg.io/v1
kind: Cluster
metadata:
  name: drill
  namespace: $NS
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
        targetLSN: "$TARGET_LSN"
  externalClusters:
    - name: origin
      plugin:
        name: barman-cloud.cloudnative-pg.io
        parameters:
          barmanObjectName: drill-origin
          serverName: $P-db
  storage:
    size: $SIZE
    $SC_LINE
  resources:
    requests: {cpu: 100m, memory: 256Mi}
    limits: {memory: 256Mi}
EOF

echo "waiting for the restore (up to 10m)..."
PHASE=""
for i in $(seq 1 120); do
  PHASE=$(kubectl -n "$NS" get cluster drill -o jsonpath='{.status.phase}' 2>/dev/null || true)
  [ "$PHASE" = "Cluster in healthy state" ] && break
  sleep 5
done
[ "$PHASE" = "Cluster in healthy state" ] || { echo "FAIL: restore did not reach healthy in 10m (phase: '$PHASE')"; exit 1; }

DRILL_POD=$(kubectl -n "$NS" get pod -l cnpg.io/cluster=drill,cnpg.io/instanceRole=primary -o jsonpath='{.items[0].metadata.name}')
RESTORED_COUNT=$(kubectl -n "$NS" exec "$DRILL_POD" -c postgres -- psql -d "$DBNAME" -At -c \
  "select count(*) from \"$TABLE\"")
echo "restored (as of LSN $TARGET_LSN): $DBNAME.$TABLE = $RESTORED_COUNT rows"

# Exact match, honestly: the restore is pinned to the reference moment, so any mismatch
# is a broken chain, not a busy site.
if [ "$RESTORED_COUNT" = "$LIVE_COUNT" ]; then
  PASSED=1
  echo "PASS: PITR restore of $P-db from s3://$BUCKET at LSN $TARGET_LSN matches the live reference ($LIVE_COUNT rows in $TABLE)"
else
  echo "FAIL: restored $RESTORED_COUNT rows vs live $LIVE_COUNT in $TABLE at the same instant"
  exit 1
fi
