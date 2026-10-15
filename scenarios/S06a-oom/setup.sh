#!/usr/bin/env bash
S6_COMP=engine S6_NAME=leak S6_FAULTS="LEAK_MB_PER_MIN=300" S6_WAIT=240
. "$(dirname "$0")/../S06-common.sh"
s6_setup
