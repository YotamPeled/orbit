#!/usr/bin/env bash
# Back up one CloudNativePG database, and prove the backup restores before calling it one.
#
#   scripts/db-backup.sh <namespace> <cluster>
#
# The one way a database is backed up on demand: the orbit_db_backup tool runs this in a
# Job on the cluster, scripts/remove-project.sh runs it before it deletes a namespace, and
# scripts/db-restore.sh runs it before it changes a live database (docs/V1.md §2: never
# delete data without a verified backup).
#
# 1. Dumps the application database (pg_dump, owners and grants included) from the
#    primary, gzip-compressed, into $ORBIT_BACKUP_DIR/<namespace>/ (default: the config
#    directory's backups/), mode 0600. The dump and the per-table row counts beside it are
#    read in ONE snapshot of the database, so a database that is being written to while
#    it is dumped still compares exactly.
# 2. Restores that file into a scratch database on the same server and counts every
#    table there. Any difference from the snapshot's counts, or a restore that stops on
#    an error, ends with `BACKUP REFUSED:` and exit 2: the file is kept for inspection
#    under a `.refused` name and is not a backup. The scratch database is always dropped.
# 3. With a bucket (objectStorage in instance/config.yaml, when run from the instance
#    root) and its keys in ORBIT_S3_ACCESS_KEY / ORBIT_S3_SECRET_KEY: uploads the file to
#    s3://<platform bucket>/orbit-backups/<namespace>/, downloads it again and compares
#    the sha256. A copy that does not compare ends with `BACKUP REFUSED:` too.
# 4. Prints `BACKUP OK: <file>` as its last line.
#
# The scratch restore needs room for a second copy of the data on the database's volume
# for as long as the check runs.
set -euo pipefail
NS=${1:-}; C=${2:-}
case "$NS" in ""|-*|*[!a-z0-9-]*) echo "usage: db-backup.sh <namespace> <cluster>"; exit 2;; esac
case "$C" in ""|-*|*[!a-z0-9-]*) echo "usage: db-backup.sh <namespace> <cluster>"; exit 2;; esac
DIR="${ORBIT_BACKUP_DIR:-${ORBIT_CONFIG_DIR:-$HOME/.config/orbit}/backups}/$NS"
KEEP_DAYS=${ORBIT_BACKUP_KEEP_DAYS:-30}

refuse(){ echo "BACKUP REFUSED: $1"; exit 2; }

kubectl -n "$NS" get clusters.postgresql.cnpg.io "$C" >/dev/null 2>&1 || refuse "$NS has no database $C"
POD=$(kubectl -n "$NS" get pod -l cnpg.io/cluster="$C",cnpg.io/instanceRole=primary -o jsonpath='{.items[0].metadata.name}' 2>/dev/null || true)
[ -n "$POD" ] || refuse "$NS/$C has no running primary, so it cannot be dumped"
DB=$(kubectl -n "$NS" get clusters.postgresql.cnpg.io "$C" -o jsonpath='{.spec.bootstrap.initdb.database}')
case "$DB" in *[!a-z0-9_-]*|-*|"") refuse "$NS/$C names no plain database";; esac

mkdir -p "$DIR"; chmod 700 "$DIR"
STAMP=$(date -u +%Y%m%dT%H%M%SZ)
FILE="$DIR/$C-$STAMP.sql.gz"
# A backup never takes the name of one that exists. Names are stamped to the second, and
# the backup a restore takes first ("the state before") could be stamped in the second of
# the very backup being restored: it overwrote it, here and in the bucket, and the restore
# then loaded the state it was meant to undo and called it RESTORED (seen in
# test_db_backup.py on a fast machine, 2026-10-09, docs/V1.md §8).
while [ -e "$FILE" ]; do
  sleep 1
  STAMP=$(date -u +%Y%m%dT%H%M%SZ)
  FILE="$DIR/$C-$STAMP.sql.gz"
done
WORK=$(mktemp -d)
SCRATCH="orbit_verify_$(date -u +%s)_$$"
MADE_SCRATCH=0
finish(){
  if [ "$MADE_SCRATCH" = 1 ]; then
    kubectl -n "$NS" exec "$POD" -c postgres -- psql -q -d postgres -c "DROP DATABASE IF EXISTS \"$SCRATCH\"" >/dev/null 2>&1 \
      || echo "the scratch database $SCRATCH in $NS/$C could not be dropped: DROP DATABASE \"$SCRATCH\" removes it"
  fi
  rm -rf "$WORK"
}
trap finish EXIT

# Per-table row counts as "schema.table rows" lines, sorted. The DO block's NOTICE lines
# are the only thing read from stderr.
COUNT_SQL='DO $$ DECLARE r record; n bigint; BEGIN
  FOR r IN SELECT schemaname, tablename FROM pg_tables WHERE schemaname NOT IN ('"'"'pg_catalog'"'"','"'"'information_schema'"'"') LOOP
    EXECUTE format('"'"'SELECT count(*) FROM %I.%I'"'"', r.schemaname, r.tablename) INTO n;
    RAISE NOTICE '"'"'ORBIT-COUNT %.% %'"'"', r.schemaname, r.tablename, n;
  END LOOP; END $$;'
counts_of(){ sed -n 's/^NOTICE:  ORBIT-COUNT //p' "$1" | sort; }

echo "== dumping $NS/$C (database $DB) from $POD"
# One session holds a snapshot; pg_dump reads that snapshot (--snapshot) and the counts
# are taken in the same transaction. stdout carries the compressed dump and nothing else.
( umask 077
  kubectl -n "$NS" exec -i "$POD" -c postgres -- env PGDATABASE="$DB" psql -q -At -v ON_ERROR_STOP=1 -d "$DB" \
    > "$FILE.part" 2> "$WORK/dump.err" <<SQL
BEGIN ISOLATION LEVEL REPEATABLE READ;
SELECT pg_export_snapshot() AS snap \gset
\setenv ORBIT_SNAPSHOT :snap
\! bash -c 'set -o pipefail; pg_dump -d "\$PGDATABASE" --snapshot="\$ORBIT_SNAPSHOT" | gzip -6 && echo ORBIT-DUMP-OK >&2'
$COUNT_SQL
COMMIT;
SQL
) || { mv "$FILE.part" "$FILE.refused" 2>/dev/null || true; refuse "the dump of $NS/$C did not finish (nothing is deleted or changed)"; }
if ! grep -qx 'ORBIT-DUMP-OK' "$WORK/dump.err" || ! gzip -t "$FILE.part" 2>/dev/null; then
  mv "$FILE.part" "$FILE.refused"
  refuse "pg_dump of $NS/$C failed or wrote a broken file (kept as $(basename "$FILE.refused"))"
fi
counts_of "$WORK/dump.err" > "$WORK/live.txt"

echo "== restoring the dump into a scratch database on the same server"
kubectl -n "$NS" exec "$POD" -c postgres -- psql -q -v ON_ERROR_STOP=1 -d postgres -c "CREATE DATABASE \"$SCRATCH\"" >/dev/null
MADE_SCRATCH=1
RESTORED=1
gunzip -c "$FILE.part" | kubectl -n "$NS" exec -i "$POD" -c postgres -- psql -q -v ON_ERROR_STOP=1 -d "$SCRATCH" \
  >/dev/null 2> "$WORK/restore.err" || RESTORED=0
if [ "$RESTORED" = 1 ]; then
  kubectl -n "$NS" exec -i "$POD" -c postgres -- psql -q -At -v ON_ERROR_STOP=1 -d "$SCRATCH" \
    >/dev/null 2> "$WORK/scratch.err" <<<"$COUNT_SQL" || RESTORED=0
fi
if [ "$RESTORED" != 1 ]; then
  mv "$FILE.part" "$FILE.refused"
  # the server's own first error line says which object; a row's data is never in it
  WHY=$(grep -m1 -E '^(psql:.*)?ERROR:' "$WORK/restore.err" "$WORK/scratch.err" 2>/dev/null | sed 's/^[^:]*://' | cut -c1-240 || true)
  refuse "the dump of $NS/$C does not restore (${WHY:-the restore stopped}) — it is not a backup (kept as $(basename "$FILE.refused"))"
fi
counts_of "$WORK/scratch.err" > "$WORK/restored.txt"
echo "table | rows in the database | rows restored from the dump"
join -a1 -a2 -e MISSING -o 0,1.2,2.2 "$WORK/live.txt" "$WORK/restored.txt" | awk '{print "  " $1, $2, $3}'
if ! cmp -s "$WORK/live.txt" "$WORK/restored.txt"; then
  mv "$FILE.part" "$FILE.refused"
  refuse "the dump of $NS/$C restores with different row counts — it is not a backup (kept as $(basename "$FILE.refused"))"
fi
TABLES=$(grep -c . "$WORK/live.txt" || true)
ROWS=$(awk '{n += $2} END {print n + 0}' "$WORK/live.txt")
mv "$FILE.part" "$FILE"
SHA=$(sha256sum "$FILE" | cut -d' ' -f1)

# --- the copy in the bucket -------------------------------------------------------------
BUCKET=""; ENDPOINT=""
if [ -f instance/config.yaml ]; then
  read -r BUCKET ENDPOINT < <(python3 - <<'PY'
import re, yaml
obj = (yaml.safe_load(open("instance/config.yaml", encoding="utf-8")) or {}).get("objectStorage") or {}
bucket, endpoint = str((obj.get("buckets") or {}).get("platform") or ""), str(obj.get("endpoint") or "")
ok = re.fullmatch(r"[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]", bucket) and re.fullmatch(r"https://[a-z0-9.-]+(:[0-9]+)?", endpoint)
print(bucket if ok else "-", endpoint if ok else "-")
PY
  ) || true
  [ "$BUCKET" = "-" ] && BUCKET=""
fi
WHERE="$FILE"
if [ -n "$BUCKET" ] && [ -n "${ORBIT_S3_ACCESS_KEY:-}" ] && [ -n "${ORBIT_S3_SECRET_KEY:-}" ]; then
  HOST=${ENDPOINT#https://}; REGION=${HOST%%.*}
  KEY="orbit-backups/$NS/$(basename "$FILE")"
  # the keys go to curl on stdin as a config document, never on its command line
  s3(){ printf 'user = "%s:%s"\n' "$ORBIT_S3_ACCESS_KEY" "$ORBIT_S3_SECRET_KEY" \
          | curl -q -sS -f --max-time 900 -K - --aws-sigv4 "aws:amz:$REGION:s3" "$@"; }
  echo "== copying to s3://$BUCKET/$KEY"
  s3 -T "$FILE" "$ENDPOINT/$BUCKET/$KEY" >/dev/null || refuse "the upload to the bucket failed — the dump is on the volume ($FILE) but not in the bucket"
  s3 -o "$WORK/back.gz" "$ENDPOINT/$BUCKET/$KEY" || refuse "the bucket copy could not be read back"
  BACK=$(sha256sum "$WORK/back.gz" | cut -d' ' -f1)
  [ "$BACK" = "$SHA" ] || refuse "the bucket copy differs from the dump (sha256) — it is not a backup"
  echo "bucket copy: s3://$BUCKET/$KEY sha256 equal ($(echo "$SHA" | cut -c1-16))"
  WHERE="$FILE and s3://$BUCKET/$KEY"
elif [ -n "$BUCKET" ]; then
  echo "bucket copy: skipped — this run was given no bucket keys (ORBIT_S3_ACCESS_KEY, ORBIT_S3_SECRET_KEY)"
fi

# Old dumps of this namespace go after KEEP_DAYS; the one just made never does.
find "$DIR" -maxdepth 1 -name '*.sql.gz' -mtime "+$KEEP_DAYS" ! -name "$(basename "$FILE")" -delete 2>/dev/null || true
find "$DIR" -maxdepth 1 \( -name '*.part' -o -name '*.refused' \) -mtime +7 -delete 2>/dev/null || true

echo "verified: $TABLES table(s), $ROWS row(s), $(stat -c %s "$FILE") bytes, sha256 $(echo "$SHA" | cut -c1-16); kept in $WHERE"
echo "BACKUP OK: $FILE"
