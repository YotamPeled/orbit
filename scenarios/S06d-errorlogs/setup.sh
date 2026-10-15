#!/usr/bin/env bash
S6_COMP=engine S6_NAME=logerr S6_FAULTS="LOG_ERRORS=60" S6_WAIT=120
. "$(dirname "$0")/../S06-common.sh"
s6_setup
