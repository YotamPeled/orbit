#!/usr/bin/env bash
# An api build that refuses to start without a secret the environment does not carry.
S6_COMP=api S6_NAME=needsecret S6_FAULTS="REQUIRE_SECRET=PAYMENT_KEY" S6_WAIT=60
. "$(dirname "$0")/../S06-common.sh"
s6_setup
