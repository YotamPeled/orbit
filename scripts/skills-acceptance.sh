#!/usr/bin/env bash
# The throwaway-session acceptance of the operator skills (docs/SKILLS.md, "The acceptance rule").
#
#   scripts/skills-acceptance.sh [--auto-confirm] <mcp-config> <skills-dir> <out-dir>
#
# Builds an empty project under <out-dir>/session whose only content is .claude/skills/<name>
# (a COPY of <skills-dir>/*, never the repository), and runs one headless Claude Code
# session per question from it, with nothing but those skills and the one MCP server the
# config names. The session is told the question and one sentence; nothing about the
# cluster, the roles or the tools. What it does with that is the evidence.
#
# Inputs (environment; none of them lives in this repository):
#   ACCEPTANCE_MODEL      the model the session runs on (required)
#   ACCEPTANCE_QUESTIONS  a file: one question per line as `<slug><TAB><question>`;
#                         blank lines and lines starting with # are skipped (required)
#   ACCEPTANCE_ONLY       optional: space-separated question numbers to run (1-based)
#   ACCEPTANCE_ROUND      optional: a round label; outputs become <n>-<slug>-<round>.*
#   ACCEPTANCE_NEVER_CONFIRM  optional, with --auto-confirm: space-separated KEY=VALUE rules;
#                         a confirmation whose arguments match one is refused, never answered
#                         (the rail that keeps an acceptance run out of an environment)
#
# Outputs, per question n: <out-dir>/<n>-<slug>.jsonl (the raw stream) and
# <out-dir>/<n>-<slug>.md (every tool call with its arguments, every result cut to 40
# lines, the final answer, the cost).
#
# `--auto-confirm`: orbitd's mutating tools confirm through the input-required round, which
# headless Claude Code cannot answer (it reports "Handler returned an invalid result"). With
# the flag the session's MCP server is scripts/mcp-confirm-proxy.py, a stdio proxy in front
# of the server the config names, which answers each confirmation once with `confirm: true`
# and records every round it met in <out-dir>/<n>-<slug>.confirmations.jsonl; the rendering
# counts them. Without the flag the session talks to the config's server directly. The proxy
# runs under ACCEPTANCE_PYTHON, or services/orbitd/.venv/bin/python, or python3 (it needs the
# pinned SDK).
#
# The MCP config is a credential: the script refuses one that is world-readable and never
# prints its contents, its URL or its headers. The session directory is created 0700.
set -euo pipefail

usage() { echo "usage: $0 [--auto-confirm] <mcp-config> <skills-dir> <out-dir>" >&2; exit 2; }
auto_confirm=0
if [ "${1:-}" = "--auto-confirm" ]; then auto_confirm=1; shift; fi
[ $# -eq 3 ] || usage
cfg=$1 skills=$2 out=$3
here=$(cd "$(dirname "$0")" && pwd)
proxy=$here/mcp-confirm-proxy.py

[ -f "$cfg" ] || { echo "skills-acceptance: mcp config is not a file" >&2; exit 2; }
case "$(uname -s)" in
  MINGW*|MSYS*|CYGWIN*)
    # Windows keeps no POSIX mode bits (chmod 600 reads back as 644), so the mode check
    # cannot be made there; the config must live in the user's own profile instead.
    case "$(cd "$(dirname "$cfg")" && pwd -W 2>/dev/null || pwd)" in
      "$(cd "$HOME" && pwd -W 2>/dev/null || pwd)"*) ;;
      *) echo "skills-acceptance: on Windows the mcp config must live under your user profile ($HOME)" >&2; exit 2;;
    esac;;
  *)
    mode=$(stat -c %a "$cfg")
    if [ $(( 8#$mode & 8#007 )) -ne 0 ]; then
      echo "skills-acceptance: refusing a world-readable mcp config (mode $mode); chmod 600 it" >&2
      exit 2
    fi;;
esac
# The host's python for the harness's own (stdlib) steps: python3 where it is real, else
# python (Windows ships a python3 that only opens the Store).
PY=python3
"$PY" -c 'import sys' >/dev/null 2>&1 || PY=python
export PYTHONIOENCODING=utf-8   # transcripts carry non-ASCII; a cp1252 console cannot print them
"$PY" -c 'import sys' >/dev/null 2>&1 || { echo "skills-acceptance: no working python3 or python" >&2; exit 2; }
[ -d "$skills" ] || { echo "skills-acceptance: skills dir not found: $skills" >&2; exit 2; }
: "${ACCEPTANCE_MODEL:?skills-acceptance: ACCEPTANCE_MODEL is not set}"
: "${ACCEPTANCE_QUESTIONS:?skills-acceptance: ACCEPTANCE_QUESTIONS (a questions file) is not set}"
[ -f "$ACCEPTANCE_QUESTIONS" ] || { echo "skills-acceptance: questions file not found" >&2; exit 2; }
command -v claude >/dev/null || { echo "skills-acceptance: claude is not on PATH" >&2; exit 2; }

cfg=$(readlink -f "$cfg")
mkdir -p "$out"
out=$(readlink -f "$out")
server_name=
proxy_py=
if [ "$auto_confirm" -eq 1 ]; then
  [ -f "$proxy" ] || { echo "skills-acceptance: proxy not found: $proxy" >&2; exit 2; }
  proxy_py=${ACCEPTANCE_PYTHON:-}
  if [ -z "$proxy_py" ]; then
    # The venv path itself, never resolved: a venv interpreter found through its symlink loses the venv.
    if [ -x "$here/../services/orbitd/.venv/bin/python" ]; then proxy_py=$(cd "$here/../services/orbitd/.venv/bin" && pwd)/python
    else proxy_py=$(command -v "$PY"); fi
  fi
  # The config's one server name; the proxy carries the same name so the session sees the
  # same `mcp__<name>__*` tools. Read, never printed.
  server_name=$("$PY" -c 'import json,sys; s=json.load(open(sys.argv[1])).get("mcpServers") or {}; print(next(iter(s), ""))' "$cfg")
  [ -n "$server_name" ] || { echo "skills-acceptance: the mcp config names no server" >&2; exit 2; }
  mkdir -p "$out/proxy"
  chmod 700 "$out/proxy"
fi
session=$out/session
rm -rf "$session"
mkdir -p "$session/.claude/skills"
chmod 700 "$session"
n_skills=0
for d in "$skills"/*/; do
  d=${d%/}
  [ -f "$d/SKILL.md" ] || continue
  cp -r "$d" "$session/.claude/skills/$(basename "$d")"
  n_skills=$((n_skills + 1))
done
[ "$n_skills" -gt 0 ] || { echo "skills-acceptance: no <name>/SKILL.md under $skills" >&2; exit 2; }
echo "skills-acceptance: session at $session with $n_skills skills"

suffix=${ACCEPTANCE_ROUND:+-$ACCEPTANCE_ROUND}
preamble="You have the Orbit operator skills in this project and one MCP server named orbit. Use them."

render() {  # <jsonl> <md> <question> <confirmations-jsonl>
  "$PY" - "$1" "$2" "$3" "$4" <<'PY'
import json, os, sys
src, dst, question, record = sys.argv[1:5]
CUT = 40

def text_of(content):
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for b in content:
            if isinstance(b, dict) and b.get("type") == "text":
                parts.append(b.get("text", ""))
            elif isinstance(b, dict):
                parts.append(json.dumps(b, indent=1))
            else:
                parts.append(str(b))
        return "\n".join(parts)
    return json.dumps(content, indent=1)

def cut(s):
    lines = s.splitlines()
    if len(lines) <= CUT:
        return s
    return "\n".join(lines[:CUT]) + f"\n… ({len(lines) - CUT} more lines cut)"

events = []
for ln in open(src, encoding="utf-8"):
    ln = ln.strip()
    if ln:
        try:
            events.append(json.loads(ln))
        except json.JSONDecodeError:
            pass

init = next((e for e in events if e.get("type") == "system" and e.get("subtype") == "init"), {})
result = next((e for e in events if e.get("type") == "result"), {})
calls, order, skills_used, texts = [], [], [], []
pending = {}
for e in events:
    t = e.get("type")
    if t == "assistant":
        for b in e.get("message", {}).get("content", []):
            if b.get("type") == "tool_use":
                name = b.get("name", "?")
                pending[b.get("id")] = len(calls)
                calls.append({"name": name, "input": b.get("input"), "result": None, "error": False})
                if name == "Skill":
                    skills_used.append(str((b.get("input") or {}).get("skill")))
                else:
                    order.append(name)
            elif b.get("type") == "text" and b.get("text", "").strip():
                texts.append(b["text"])
    elif t == "user":
        content = e.get("message", {}).get("content", [])
        if isinstance(content, list):
            for b in content:
                if isinstance(b, dict) and b.get("type") == "tool_result" and b.get("tool_use_id") in pending:
                    c = calls[pending[b["tool_use_id"]]]
                    c["result"] = text_of(b.get("content"))
                    c["error"] = bool(b.get("is_error"))

servers = ", ".join(f"{s.get('name')}={s.get('status')}" for s in init.get("mcp_servers", [])) or "none"
confirmations = []
if record and os.path.isfile(record):
    for ln in open(record, encoding="utf-8"):
        ln = ln.strip()
        if ln:
            try:
                confirmations.append(json.loads(ln))
            except json.JSONDecodeError:
                pass
answered = [c for c in confirmations if c.get("answered") is True]
unanswered = [c for c in confirmations if c.get("answered") is False]
def _round(c):
    return f"{c.get('tool')} ({'; '.join(c.get('messages') or [])})"
confirm_line = "none"
if confirmations:
    confirm_line = (f"{len(answered)} answered `confirm: true` by the harness"
                    + (f", {len(unanswered)} unanswered" if unanswered else "")
                    + ": " + "; ".join(_round(c) for c in confirmations))
mcp_tools = sorted(x for x in init.get("tools", []) if str(x).startswith("mcp__"))
final = result.get("result") if isinstance(result.get("result"), str) else (texts[-1] if texts else "")

md = [f"# {question}", ""]
md += [f"- model: `{init.get('model', '?')}`",
       f"- mcp servers: {servers}",
       f"- mcp tools offered: {len(mcp_tools)}",
       f"- skills loaded: {', '.join(init.get('skills', [])) or 'none'}",
       f"- skills invoked: {', '.join(skills_used) or 'none'}",
       f"- tools in order: {' → '.join(order) or 'none'}",
       f"- turns: {result.get('num_turns', '?')}; cost: {result.get('total_cost_usd', '?')} USD; "
       f"duration: {result.get('duration_ms', '?')} ms; result: {result.get('subtype', '?')}"
       f"{' (is_error)' if result.get('is_error') else ''}",
       f"- permission denials: {len(result.get('permission_denials') or [])}",
       f"- confirmation rounds met by the proxy: {confirm_line}",
       ""]
for i, c in enumerate(calls, 1):
    md += [f"## call {i}: {c['name']}", "", "```json", json.dumps(c["input"], indent=1), "```", ""]
    if c["result"] is None:
        md += ["_no result recorded_", ""]
    else:
        md += [f"result{' (is_error)' if c['error'] else ''}:", "", "```", cut(c["result"]), "```", ""]
md += ["## final answer", "", final or "_(none)_", ""]
open(dst, "w", encoding="utf-8").write("\n".join(md))
print(f"  tools: {' → '.join(order) or 'none'}; skills: {', '.join(skills_used) or 'none'}; "
      f"cost {result.get('total_cost_usd', '?')} USD; {result.get('subtype', '?')}")
PY
}

n=0
total=0
while IFS= read -r line || [ -n "$line" ]; do
  case "$line" in ''|'#'*) continue ;; esac
  n=$((n + 1))
  if [ -n "${ACCEPTANCE_ONLY:-}" ]; then
    case " $ACCEPTANCE_ONLY " in *" $n "*) ;; *) continue ;; esac
  fi
  slug=${line%%$'\t'*}
  question=${line#*$'\t'}
  [ "$slug" != "$line" ] && [ -n "$slug" ] && [ -n "$question" ] || {
    echo "skills-acceptance: question $n is not '<slug><TAB><question>'" >&2; exit 2; }
  stem=$out/$n-$slug$suffix
  echo "skills-acceptance: question $n ($slug) → $stem.jsonl"
  mcp_cfg=$cfg
  record=$stem.confirmations.jsonl
  rm -f "$record"
  if [ "$auto_confirm" -eq 1 ]; then
    # A stdio proxy per question: same server name; the real config is read by the proxy only.
    mcp_cfg=$out/proxy/$n-$slug$suffix.mcp.json
    : >"$mcp_cfg"; chmod 600 "$mcp_cfg"
    "$PY" - "$mcp_cfg" "$server_name" "$proxy_py" "$proxy" "$cfg" "$record" ${ACCEPTANCE_NEVER_CONFIRM:-} <<'PY'
import json, sys
dst, name, py, proxy, cfg, record = sys.argv[1:7]
args = [proxy, "--config", cfg, "--server", name, "--record", record, "--auto-confirm"]
for rule in sys.argv[7:]:
    args += ["--never-confirm", rule]
spec = {"type": "stdio", "command": py, "args": args}
json.dump({"mcpServers": {name: spec}}, open(dst, "w"), indent=1)
PY
  fi
  rc=0
  ( cd "$session" && claude -p \
      --setting-sources project \
      --mcp-config "$mcp_cfg" --strict-mcp-config \
      --allowedTools 'mcp__orbit__*' ${ACCEPTANCE_EXTRA_TOOLS:-} \
      --output-format stream-json --verbose \
      --model "$ACCEPTANCE_MODEL" \
      "$question $preamble" </dev/null >"$stem.jsonl" 2>"$stem.stderr" ) || rc=$?
  [ -s "$stem.stderr" ] || rm -f "$stem.stderr"
  [ "$rc" -eq 0 ] || echo "skills-acceptance: claude exited $rc on question $n (stream kept)" >&2
  render "$stem.jsonl" "$stem.md" "$question" "$record"
  total=$((total + 1))
done <"$ACCEPTANCE_QUESTIONS"
[ "$total" -gt 0 ] || { echo "skills-acceptance: no question ran" >&2; exit 2; }
echo "skills-acceptance: $total question(s) done under $out"
