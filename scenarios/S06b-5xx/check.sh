#!/usr/bin/env bash
. "$(dirname "$0")/../S06-common.sh"
s6_check '500|5xx|server error|ERROR_RATE|error rate' 'traefik|requests|rate|log|Loki|Prometheus|metric'
