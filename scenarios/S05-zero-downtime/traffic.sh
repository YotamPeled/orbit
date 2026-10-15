#!/usr/bin/env bash
# Constant traffic at the prod door until <stop-file> exists (or 45 minutes pass): one
# request at a time, each logged as "<epoch> <http code>". A request that gets no answer
# within 10 s is logged as 000 and counts as failed.
#   traffic.sh <url> <log> <stop-file>
url=$1 log=$2 stop=$3
until=$(( $(date +%s) + 2700 ))
while [ ! -e "$stop" ] && [ "$(date +%s)" -lt "$until" ]; do
  code=$(curl -s -o /dev/null -w '%{http_code}' --max-time 10 "$url")
  echo "$(date +%s) $code" >> "$log"
  sleep 0.2
done
