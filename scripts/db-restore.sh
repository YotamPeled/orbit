#!/usr/bin/env bash
# Replace a database's data with an earlier state: a backup file, or (prod, with the
# backups feature) a point in time. The orbit_db_restore tool runs this in a Job on the
# cluster; the orbit-restore-db skill drives that tool.
#
#   scripts/db-restore.sh <project> <env> --from <backup file name | latest> [--yes]
#   scripts/db-restore.sh <project> <env> --to <RFC3339 UTC time>           [--yes]
#
# 1. Names what it would restore (the backup file, or the moment) and refuses what it
#    cannot: no such database, no such file, a file name that is not one of this
#    database's backups.
# 2. With --yes, FIRST backs up the live database as it is now (scripts/db-backup.sh:
#    dumped, restored into a scratch database, row counts equal). A backup that is
#    refused refuses the restore: the live database is not touched. This is the way
#    back if the restore itself was the mistake, and its file name is printed.
# 3. --from: loads the file into a scratch database on the same server, prints per table
#    the live row count beside the backup's, then replaces the live data in one
#    transaction (pg_dump --clean --if-exists from the scratch copy | psql
#    --single-transaction). --to: hands over to scripts/pitr-restore.sh.
# 4. Proves it: the live counts equal the backup's, every relation belongs to the
#    database's owner. Prints `RESTORED: ...` as its last line.
#
# Without --yes it stops after step 3's comparison (step 1 for --to) and changes nothing.
# A table that exists live and not in the backup is left alone: only what the backup
# holds is replaced.
set -euo pipefail
usage(){ echo "usage: db-restore.sh <project> <env> (--from <backup file|latest> | --to <RFC3339 UTC>) [--yes]"; exit 2; }
[ $# -ge 2 ] || usage
P=$1; ENV=$2; shift 2
FROM=""; TO=""; YES=0
while [ $# -gt 0 ]; do
  case "$1" in
    --from) FROM="${2:-}"; shift 2;;
    --to) TO="${2:-}"; shift 2;;
    --yes) YES=1; shift;;
    *) echo "unknown argument (not repeated)"; exit 2;;
  esac
done
case "$P" in ""|-*|*[!a-z0-9-]*) usage;; esac
case "$ENV" in ""|-*|*[!a-z0-9-]*) usage;; esac
{ [ -n "$FROM" ] && [ -z "$TO" ]; } || { [ -z "$FROM" ] && [ -n "$TO" ]; } || usage
HERE="$(cd "$(dirname "$0")" && pwd)"
NS="$P-$ENV"; C="$P-db"
DIR="${ORBIT_BACKUP_DIR:-${ORBIT_CONFIG_DIR:-$HOME/.config/orbit}/backups}/$NS"

refuse(){ echo "RESTORE REFUSED: $1"; exit 2; }
kubectl -n "$NS" get clusters.postgresql.cnpg.io "$C" >/dev/null 2>&1 || refuse "$NS has no database $C"

if [ -n "$TO" ]; then
  [[ "$TO" =~ ^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(\.[0-9]+)?Z$ ]] \
    || refuse "--to must be an RFC3339 UTC instant like 2026-10-04T15:00:00Z"
  [ "$ENV" = prod ] || refuse "a point in time exists only for a prod database (its write-ahead log is archived to the bucket); restore $NS from a backup file (--from)"
  if [ "$YES" != 1 ]; then
    exec "$HERE/pitr-restore.sh" "$P" --to "$TO"
  fi
  echo "== first: a backup of the live database as it is now"
  "$HERE/db-backup.sh" "$NS" "$C" || refuse "the live database could not be backed up first, so it is not changed"
  exec "$HERE/pitr-restore.sh" "$P" --to "$TO" --yes
fi

# --- from a backup file -------------------------------------------------------------------
# The bucket, as db-backup.sh finds it: every backup made on a platform with a bucket has
# a copy at s3://<platform bucket>/orbit-backups/<namespace>/. The volume the files lie
# on belongs to the node, and a node that was rebuilt (or lost) comes back with an empty
# one: until 2026-10-08 a restore looked on the volume only, so the one day the bucket
# copy was needed, "there is no backup on this platform" was the answer (docs/V1.md §8).
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
HAVE_BUCKET=0
if [ -n "$BUCKET" ] && [ -n "${ORBIT_S3_ACCESS_KEY:-}" ] && [ -n "${ORBIT_S3_SECRET_KEY:-}" ]; then
  HAVE_BUCKET=1
  S3HOST=${ENDPOINT#https://}; REGION=${S3HOST%%.*}
  # the keys go to curl on stdin as a config document, never on its command line
  s3(){ printf 'user = "%s:%s"\n' "$ORBIT_S3_ACCESS_KEY" "$ORBIT_S3_SECRET_KEY" \
          | curl -q -sS -f --max-time 900 -K - --aws-sigv4 "aws:amz:$REGION:s3" "$@"; }
fi
# the names of this database's backups in the bucket (a listing that fails is an empty one:
# the volume still answers, and a named backup is asked for by name below)
in_bucket(){
  [ "$HAVE_BUCKET" = 1 ] || return 0
  s3 "$ENDPOINT/$BUCKET?list-type=2&prefix=orbit-backups/$NS/$C-" 2>/dev/null \
    | python3 -c 'import re, sys
for k in re.findall(r"<Key>([^<]+)</Key>", sys.stdin.read()):
    print(k.rsplit("/", 1)[-1])' 2>/dev/null | grep -E "^$C-[0-9]{8}T[0-9]{6}Z\.sql\.gz$" || true
}
if [ "$FROM" = latest ]; then
  FROM=$( { ls -1 "$DIR" 2>/dev/null || true; in_bucket; } | grep -E "^$C-[0-9]{8}T[0-9]{6}Z\.sql\.gz$" | sort -u | tail -1 || true)
  [ -n "$FROM" ] || refuse "there is no backup of $NS/$C to restore, on this platform's volume or in its bucket (orbit_db_backup makes one)"
fi
[[ "$FROM" =~ ^[a-z0-9-]+-[0-9]{8}T[0-9]{6}Z\.sql\.gz$ ]] && [ "${FROM%-*}" = "$C" ] \
  || refuse "that is not the name of a backup of $NS/$C (they look like $C-20261004T150000Z.sql.gz)"
FILE="$DIR/$FROM"
if [ ! -f "$FILE" ]; then
  [ "$HAVE_BUCKET" = 1 ] || refuse "there is no backup $FROM of $NS/$C on this platform"
  KEY="orbit-backups/$NS/$FROM"
  echo "== $FROM is not on this platform's volume: fetching s3://$BUCKET/$KEY"
  mkdir -p "$DIR"
  rm -f "$FILE.part"
  s3 -o "$FILE.part" "$ENDPOINT/$BUCKET/$KEY" 2>/dev/null \
    || { rm -f "$FILE.part"; refuse "there is no backup $FROM of $NS/$C on this platform's volume or in its bucket (s3://$BUCKET/$KEY)"; }
  gzip -t "$FILE.part" 2>/dev/null || { rm -f "$FILE.part"; refuse "the bucket's copy of $FROM is not a readable gzip file"; }
  mv "$FILE.part" "$FILE"
  echo "fetched from the bucket: s3://$BUCKET/$KEY sha256 $(sha256sum "$FILE" | cut -c1-16)"
fi
gzip -t "$FILE" 2>/dev/null || refuse "the backup $FROM is not a readable gzip file"

POD=$(kubectl -n "$NS" get pod -l cnpg.io/cluster="$C",cnpg.io/instanceRole=primary -o jsonpath='{.items[0].metadata.name}' 2>/dev/null || true)
[ -n "$POD" ] || refuse "$NS/$C has no running primary"
DB=$(kubectl -n "$NS" get clusters.postgresql.cnpg.io "$C" -o jsonpath='{.spec.bootstrap.initdb.database}')
case "$DB" in *[!a-z0-9_-]*|-*|"") refuse "$NS/$C names no plain database";; esac
OWNER=$(kubectl -n "$NS" get clusters.postgresql.cnpg.io "$C" -o jsonpath='{.spec.bootstrap.initdb.owner}')
case "$OWNER" in *[!a-z0-9_-]*|-*|"") refuse "$NS/$C names no plain owner";; esac

counts(){ # <database> -> "schema.table rows" lines, sorted
  kubectl -n "$NS" exec -i "$POD" -c postgres -- psql -q -At -d "$1" 2>&1 >/dev/null <<'SQL' | sed -n 's/^NOTICE:  ORBIT-COUNT //p' | sort
DO $$ DECLARE r record; n bigint; BEGIN
  FOR r IN SELECT schemaname, tablename FROM pg_tables WHERE schemaname NOT IN ('pg_catalog','information_schema') LOOP
    EXECUTE format('SELECT count(*) FROM %I.%I', r.schemaname, r.tablename) INTO n;
    RAISE NOTICE 'ORBIT-COUNT %.% %', r.schemaname, r.tablename, n;
  END LOOP; END $$;
SQL
}

PRE=""
if [ "$YES" = 1 ]; then
  echo "== first: a backup of the live database as it is now"
  OUT=$("$HERE/db-backup.sh" "$NS" "$C") || { printf '%s\n' "$OUT" | tail -3; refuse "the live database could not be backed up first, so it is not changed"; }
  printf '%s\n' "$OUT" | tail -2
  PRE=$(printf '%s\n' "$OUT" | sed -n 's/^BACKUP OK: //p' | tail -1)
  [ -n "$PRE" ] && [ -f "$PRE" ] || refuse "the backup of the live database left no file, so the live database is not changed"
fi

SCRATCH="orbit_restore_$(date -u +%s)_$$"
MADE=0
finish(){
  [ "$MADE" = 1 ] && { kubectl -n "$NS" exec "$POD" -c postgres -- psql -q -d postgres -c "DROP DATABASE IF EXISTS \"$SCRATCH\"" >/dev/null 2>&1 \
    || echo "the scratch database $SCRATCH in $NS/$C could not be dropped: DROP DATABASE \"$SCRATCH\" removes it"; }
  return 0
}
trap finish EXIT
echo "== loading $FROM into a scratch database"
kubectl -n "$NS" exec "$POD" -c postgres -- psql -q -v ON_ERROR_STOP=1 -d postgres -c "CREATE DATABASE \"$SCRATCH\"" >/dev/null
MADE=1
gunzip -c "$FILE" | kubectl -n "$NS" exec -i "$POD" -c postgres -- psql -q -v ON_ERROR_STOP=1 -d "$SCRATCH" >/dev/null 2>&1 \
  || refuse "the backup $FROM does not load — the live database is not changed"
WANT=$(counts "$SCRATCH")
[ -n "$WANT" ] || refuse "the backup $FROM holds no table — the live database is not changed"
echo "table | rows live now | rows in $FROM"
join -a1 -a2 -e MISSING -o 0,1.2,2.2 <(counts "$DB") <(printf '%s\n' "$WANT") | awk '{print "  " $1, $2, $3}'
if [ "$YES" != 1 ]; then
  echo "nothing changed on the live database. With --yes its data is replaced by $FROM (what was written since is lost; a backup of the live state is taken first)."
  exit 0
fi

echo "== replacing the live data (one transaction)"
# Both ends run in the database's own pod: nothing of the data leaves it.
kubectl -n "$NS" exec "$POD" -c postgres -- bash -c \
  'set -o pipefail; pg_dump -d "$1" --clean --if-exists | psql -q --single-transaction -v ON_ERROR_STOP=1 -d "$2" >/dev/null' _ "$SCRATCH" "$DB" \
  || { echo "the load into the live database stopped; the transaction was rolled back and the live data is as it was (its backup: $(basename "$PRE"))"; exit 1; }
GOT=$(counts "$DB")
echo "table | rows live after the restore"
printf '%s\n' "$GOT" | sed 's/^/  /'
# only the tables the backup holds are compared: a table it does not hold was left alone
MISS=$(join -a1 -e MISSING -o 0,1.2,2.2 <(printf '%s\n' "$WANT") <(printf '%s\n' "$GOT") | awk '$2 != $3 {print $1}')
if [ -n "$MISS" ]; then
  echo "RESTORE NOT PROVEN: after the load these tables do not hold the backup's rows: $(echo $MISS) (the state before the restore is in $(basename "$PRE"))"
  exit 1
fi
WRONG=$(kubectl -n "$NS" exec -i "$POD" -c postgres -- psql -At -d "$DB" -v owner="$OWNER" <<'SQL'
SELECT n.nspname || '.' || c.relname || ' (' || pg_get_userbyid(c.relowner) || ')'
FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
WHERE n.nspname NOT IN ('pg_catalog', 'information_schema') AND n.nspname NOT LIKE 'pg\_%'
  AND c.relkind IN ('r', 'p', 'S', 'v', 'm') AND pg_get_userbyid(c.relowner) <> :'owner';
SQL
)
if [ -n "$WRONG" ]; then
  echo "LOADED, BUT NOT OWNED BY $OWNER — the application cannot use these: $(echo $WRONG)"
  echo "fix: ALTER TABLE/SEQUENCE ... OWNER TO \"$OWNER\" for each, then check the application"
  exit 1
fi
echo "every relation is owned by $OWNER; the state before the restore is kept as $(basename "$PRE")"
echo "RESTORED: $NS/$C holds the data of $FROM"
