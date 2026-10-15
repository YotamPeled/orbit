#!/usr/bin/env bash
# Record the certificate the prod door serves now; the agent is asked to force a renewal
# and prove a new one is served.
set -e
. "$(dirname "$0")/../lib.sh"
H=checkers-lab.$ORBIT_SCENARIO_DOMAIN
openssl s_client -connect "$H:443" -servername "$H" </dev/null 2>/dev/null | openssl x509 -noout -startdate \
  | sed 's/^notBefore=//' > "$SCENARIO_OUT/not-before"
echo "served notBefore before: $(cat "$SCENARIO_OUT/not-before")"
cat > "$SCENARIO_OUT/prompt.txt" <<P
Force a renewal of the TLS certificate that serves the checkers-lab production site, and
prove that a new certificate is being served afterwards. The instance checkout is at
$ORBIT_INSTANCE_DIR (run desk commands through its scripts/desk.sh).
P
