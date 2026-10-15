#!/usr/bin/env bash
S6_COMP=api S6_NAME=slow S6_FAULTS="SLOW_ENDPOINT_MS=4000" S6_WAIT=180
. "$(dirname "$0")/../S06-common.sh"
s6_setup
