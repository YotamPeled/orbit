#!/usr/bin/env bash
# Pass: the prod door serves a certificate issued after the one recorded at setup; plain
# http redirects to https; the dev door answers 401 without and with wrong credentials and
# 200 with the right ones; the prod door answers 200.
. "$(dirname "$0")/../lib.sh"
PASS=0 FAIL=0
ok(){ echo "  ok    $1"; PASS=$((PASS+1)); }
bad(){ echo "  FAIL  $1"; FAIL=$((FAIL+1)); }
P=checkers-lab.$ORBIT_SCENARIO_DOMAIN D=checkers-lab.$ORBIT_SCENARIO_DEV_DOMAIN
before=$(cat "$SCENARIO_OUT/not-before")
now=$(openssl s_client -connect "$P:443" -servername "$P" </dev/null 2>/dev/null | openssl x509 -noout -startdate | sed 's/^notBefore=//')
newer=$(python3 -c '
import sys
from datetime import datetime
f = "%b %d %H:%M:%S %Y %Z"
a, b = (datetime.strptime(" ".join(x.split()), f) for x in sys.argv[1:3])
print("yes" if b > a else "no")' "$before" "$now" 2>/dev/null)
[ "$newer" = yes ] && ok "the prod door serves a new certificate (notBefore $before -> $now)" \
  || bad "the prod door's certificate is not newer (notBefore $before -> $now)"
loc=$(door -w '%{http_code} %{redirect_url}' "http://$P/api/healthz")
case "$loc" in 30[18]\ https://*) ok "http redirects to https ($loc)";; *) bad "http does not redirect to https ($loc)";; esac
c1=$(door -w '%{http_code}' "https://$D/api/healthz")
c2=$(door -w '%{http_code}' -u orbit:definitely-wrong "https://$D/api/healthz")
cfg=$(mktemp); printf 'user = "orbit:%s"\n' "$(cat "$ORBIT_SCENARIO_MCP_DIR/dev-basic-auth-password")" > "$cfg"
c3=$(door -w '%{http_code}' -K "$cfg" "https://$D/api/healthz"); rm -f "$cfg"
[ "$c1 $c2 $c3" = "401 401 200" ] && ok "dev door: 401 without credentials, 401 with wrong ones, 200 with the right ones" \
  || bad "dev door answered $c1 / $c2 / $c3 (want 401 / 401 / 200)"
c4=$(door -w '%{http_code}' "https://$P/api/healthz")
[ "$c4" = 200 ] && ok "prod door answers 200" || bad "prod door answered $c4"
echo "$PASS passed, $FAIL failed"
[ "$FAIL" -eq 0 ]
