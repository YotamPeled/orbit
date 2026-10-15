#!/usr/bin/env bash
. "$(dirname "$0")/../S06-common.sh"
s6_check 'LOG_ERRORS|error log|simulated failure|upstream refused' 'Loki|log line|logs_query|level|error'
