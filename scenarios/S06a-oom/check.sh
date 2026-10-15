#!/usr/bin/env bash
. "$(dirname "$0")/../S06-common.sh"
s6_check 'OOM|out of memory|memory leak|LEAK_MB_PER_MIN|leak' 'OOMKilled|exit(ed)? (code )?137|137|restart|memory (limit|usage)|256Mi'
