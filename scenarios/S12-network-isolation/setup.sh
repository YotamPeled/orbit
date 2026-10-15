#!/usr/bin/env bash
# The driver probes the network from where a tenant's pods stand (throwaway curl pods in
# checkers-lab-dev), then a viewer agent is asked to explain the isolation from evidence.
# Each line of probes.txt: <key> <http code>:<curl exit> <ready endpoints of the target>.
# A target with no ready endpoint also refuses, so "no connection" only counts when the
# target was up (the check reads the third column).
set -e
here=$(cd "$(dirname "$0")" && pwd)
. "$here/../lib.sh"
. "$here/probe.sh"
# The count of a target's ready endpoints, asked up to three times when the answer is none:
# a desk command whose tunnel did not open answers nothing, which counted as "0 endpoints"
# and made a row that had been judged ("no connection") unjudgeable (the rebuilt box,
# 2026-10-08: the prod database's Service, with its one endpoint, read as 0).
ready(){
  local n=0 try
  for try in 1 2 3; do
    n=$(desk kubectl -n "$1" get endpoints "$2" -o jsonpath='{.subsets[*].addresses[*].ip}' 2>/dev/null | wc -w | tr -d ' ')
    [ "${n:-0}" -gt 0 ] && break
    sleep 5
  done
  echo "${n:-0}"
}
line(){ # <key> <target-ns> <target-svc> <labels> <url>
  echo "$1 $(probe checkers-lab-dev "$4" "$5") $(ready "$2" "$3")"
}
NARROW='{"app.kubernetes.io/name":"engine","orbit.local/egress":"declared"}'
{
  line control-sibling       checkers-lab-dev  checkers-lab-engine '{}'      http://checkers-lab-engine:8080/healthz
  line dev-to-prod-api       checkers-lab-prod checkers-lab-api    '{}'      http://checkers-lab-api.checkers-lab-prod.svc:8080/healthz
  line dev-to-prod-db        checkers-lab-prod checkers-lab-db-rw  '{}'      http://checkers-lab-db-rw.checkers-lab-prod.svc:5432/
  line to-prometheus         monitoring kube-prometheus-stack-prometheus   '{}' http://kube-prometheus-stack-prometheus.monitoring.svc:9090/-/ready
  line to-alertmanager       monitoring kube-prometheus-stack-alertmanager '{}' http://kube-prometheus-stack-alertmanager.monitoring.svc:9093/-/ready
  line to-grafana            monitoring kube-prometheus-stack-grafana      '{}' http://kube-prometheus-stack-grafana.monitoring.svc:80/api/health
  line to-loki               loki loki                                     '{}' http://loki.loki.svc:3100/ready
  line to-orbitd             orbit orbitd                                  '{}' http://orbitd.orbit.svc:80/healthz
  line narrowed-to-sibling   checkers-lab-dev  checkers-lab-engine "$NARROW" http://checkers-lab-engine:8080/healthz
  line narrowed-to-internet  default kubernetes                    "$NARROW" https://example.com/
  line open-to-internet      default kubernetes                    '{}'      https://example.com/
} > "$SCENARIO_OUT/probes.txt"
cat "$SCENARIO_OUT/probes.txt"
cat > "$SCENARIO_OUT/prompt.txt" <<P
Is the checkers-lab project's dev environment isolated from its production environment and
from the Orbit platform's internals, and which of its components may reach the internet?
Show me the evidence you based that on. Do not change anything.
P
