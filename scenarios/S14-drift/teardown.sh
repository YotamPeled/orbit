#!/usr/bin/env bash
# Never leave the hand edit behind if the check failed.
. "$(dirname "$0")/../lib.sh"
desk kubectl -n checkers-lab-prod set env deploy/checkers-lab-api HAND_EDIT- 2>/dev/null || true
