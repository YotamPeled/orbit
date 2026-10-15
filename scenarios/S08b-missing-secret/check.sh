#!/usr/bin/env bash
# The answer must name the missing variable and say it is missing, from evidence it read.
. "$(dirname "$0")/../S06-common.sh"
s6_check 'PAYMENT_KEY' 'missing|not set|refus|CrashLoopBackOff|exit(ed)? (code )?1|log'
