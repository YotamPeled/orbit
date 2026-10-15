#!/usr/bin/env bash
S6_COMP=api S6_NAME=errors S6_FAULTS="ERROR_RATE=0.6" S6_WAIT=180
. "$(dirname "$0")/../S06-common.sh"
s6_setup
