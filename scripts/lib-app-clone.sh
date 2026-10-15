#!/usr/bin/env bash
# Shared GitHub-App clone plumbing for the operator push scripts.
#
# Sourced by scripts/ship.sh and scripts/monitor-commit.sh, never executed.
# The caller provides `fail <step> [detail]` and `ok <step>` (STEP lines on
# stdout, nonzero exit on failure) and a cleanup trap; this file owns the
# TOKEN/AUTH_HEADER/BASIC/CLONE globals those run with. The only secret
# handling here is keeping the token out of argv (http.extraHeader), out of
# tracing (lockdown before minting), and out of failure output (scrub_text).
_APP_CLONE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TOKEN=""
AUTH_HEADER=""
BASIC=""
CLONE=""
# Always main: the environment must not move the landing branch.
MAIN=main

# Tracing (bash -x, BASH_XTRACEFD, inherited SHELLOPTS) would print TOKEN
# and AUTH_HEADER on assignment; the later scrub cannot cover those lines.
app_disable_tracing() {
  set +x
  unset BASH_XTRACEFD 2>/dev/null || true
  unset GIT_TRACE GIT_TRACE_PACKET GIT_TRACE_PERFORMANCE GIT_TRACE_SETUP \
    GIT_CURL_VERBOSE GIT_TRACE_CURL GIT_TRACE_CURL_NO_DATA \
    GIT_TRACE2 GIT_TRACE2_EVENT GIT_TRACE2_PERF 2>/dev/null || true
}

app_mint_token() {
  if TOKEN="$(python3 "$_APP_CLONE_DIR/github-app-token.py")"; then
    ok token
  else
    fail token
  fi
}

clone_url() {
  local u="$1"
  case "$u" in
    git@github.com:*)
      u="${u#git@github.com:}"
      u="${u%.git}"
      printf 'https://github.com/%s.git\n' "$u"
      ;;
    https://*|http://*|file://*|/*|./*)
      printf '%s\n' "$u"
      ;;
    *)
      printf 'https://github.com/%s.git\n' "${u%.git}"
      ;;
  esac
}

# Secrets travel in the environment, never argv. Tracing is already off.
scrub_text() {
  TOKEN="$TOKEN" AUTH_HEADER="${AUTH_HEADER:-}" BASIC="${BASIC:-}" python3 -c '
import os, sys
text = sys.stdin.read()
token = os.environ.get("TOKEN") or ""
for secret in (
    os.environ.get("AUTH_HEADER") or "",
    os.environ.get("BASIC") or "",
    ("x-access-token:" + token) if token else "",
    token,
):
    if secret:
        text = text.replace(secret, "<token>")
sys.stdout.write(text)
'
}

# Shallow-clones $ORBIT_REPO_URL into a fresh $CLONE with the token pinned as
# http.extraHeader, exactly as ship.sh has always done.
app_clone_repo() {
  URL="$(clone_url "$ORBIT_REPO_URL")"
  BASIC="$(printf '%s' "x-access-token:${TOKEN}" | base64 | tr -d '\n')"
  AUTH_HEADER="Authorization: Basic ${BASIC}"
  CLONE="$(mktemp -d "${TMPDIR:-/tmp}/orbit-ship.XXXXXX")"

  if CLONE_OUT="$(
    GIT_CONFIG_COUNT=1 \
    GIT_CONFIG_KEY_0="http.extraHeader" \
    GIT_CONFIG_VALUE_0="$AUTH_HEADER" \
    git clone --quiet --depth 1 --branch "$MAIN" "$URL" "$CLONE" 2>&1
  )"; then
    printf '[http]\n\textraHeader = "%s"\n' "$AUTH_HEADER" >> "$CLONE/.git/config"
    git -C "$CLONE" config commit.gpgsign false
    ok clone
  else
    scrub_text <<<"${CLONE_OUT:-}" >&2
    fail clone
  fi
}
