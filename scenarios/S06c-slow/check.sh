#!/usr/bin/env bash
. "$(dirname "$0")/../S06-common.sh"
s6_check 'slow|latency|SLOW_ENDPOINT|seconds|delay' 'p95|p99|duration|trace|span|histogram|[0-9](\.[0-9]+)? ?s\b'
