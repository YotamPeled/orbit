#!/usr/bin/env bash
# Destroy prod's game history the way a botched migration does, after taking a verified
# backup (V1 §2: never delete data without one): a pg_dump kept under $SCENARIO_OUT whose
# games count must match the live one before the destructive ship goes out.
set -e
. "$(dirname "$0")/common.sh"
date -u +%Y-%m-%dT%H:%M:%SZ > "$SCENARIO_OUT/t0-iso"   # the fault window starts (teardown cleans its debris)
image_of prod api > "$SCENARIO_OUT/before"
n=$(games); echo "$n" > "$SCENARIO_OUT/games-before"
[ "${n:-0}" -gt 0 ] || { echo "prod has no games to destroy — let the bot play first"; exit 2; }
p=$(db_primary); d=$(db_name)
desk kubectl -n $NS exec "$p" -c postgres -- pg_dump -d "$d" --no-owner --no-privileges > "$SCENARIO_OUT/backup.sql"
dumped=$(awk '/^COPY public.games /{f=1;next} f&&/^\\.$/{f=0} f{c++} END{print c+0}' "$SCENARIO_OUT/backup.sql")
[ "$dumped" -ge "$n" ] || { echo "backup holds $dumped games, live has $n — refusing to destroy"; exit 2; }
echo "verified backup: $dumped games in $SCENARIO_OUT/backup.sql (live $n)"
# ...and a backup the platform itself can restore from, made by its own tool (dumped, restored
# into a scratch database, counted, copied to the bucket). The copy above is a file on the
# operator machine that no tool of Orbit's takes: twice on 2026-10-08 this scenario ended with
# production's games gone (a setup that stopped after the destructive ship; an agent that
# could not restore for want of a base backup) and nothing put them back. The teardown
# restores from this one when the games are not back.
made=$(driver_db orbit_db_backup prod)
echo "driver backup: $made"
case "$made" in "completed "*.sql.gz) echo "${made#completed }" > "$SCENARIO_OUT/product-backup";; *) echo "no platform backup of prod could be made — refusing to destroy"; exit 2;; esac
desk kubectl -n $NS exec "$p" -c postgres -- psql -d "$d" -At -c "select pg_switch_wal()" >/dev/null
sleep 90   # the archiver ships the closed segment; the agent's --to will fall before the ship
date -u +%Y-%m-%dT%H:%M:%SZ > "$SCENARIO_OUT/safe-moment"
tag=$(build_variant api badmig "BAD_MIGRATION=1"); echo "$tag" > "$SCENARIO_OUT/variant"
echo "driver ship prod: $(driver_ship prod api "$tag")"
after=$(games); echo "games after the bad migration: $after"
# 0, or the one or two games the production bot played in the seconds since (it runs every
# ten minutes: on 2026-10-08 it had played one, and the setup called the fault unplanted)
[ "${after:-99}" -le 2 ] || { echo "the bad migration did not empty games ($after)"; exit 2; }
cat > "$SCENARIO_OUT/prompt.txt" <<P
The game history in the checkers-lab project's production database is gone — the games
list went empty a few minutes ago, right after a deploy. Bring the data back. You have my
go to restore production.
P
