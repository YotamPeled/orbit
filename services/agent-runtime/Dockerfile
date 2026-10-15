# orbit-agent-runtime: the SDK image every Orbit agent runs on.
#
# Two stages. The Claude Agent SDK is a Python wrapper that drives the `claude` CLI,
# and that CLI ships as a platform-native binary — so Node is a BUILD dependency only.
# The earlier single-stage build installed Debian's nodejs (v20) against a CLI
# declaring engines >=22 and kept node+npm in the runtime; this keeps neither, which
# removes both the unsupported-runtime mismatch and ~1.2GB of attack surface from a pod
# that holds a subscription token (1.66GB -> ~0.5GB).
#
# Both bases are pinned BY DIGEST and the CLI by exact version, so the image CI
# publishes is the image CI tested — a floating `python:3.13-slim` tag meant the tested
# and published artifacts were only probably the same.
#
# Refresh procedure: bump the digests, CLAUDE_CODE_VERSION and the claude-agent-sdk pin
# (below) together, rebuild, and re-run services/agent-runtime/smoke.sh before merging.
#
# The SDK pin and the CLI version are one protocol pair, not two independent knobs. The
# CLI streams typed messages to the Python SDK, which PARSES them — and an SDK older than
# the CLI's protocol crashes on a message type it does not model. That is a live incident,
# not a hypothetical: CLI >=2.1.49 emits a `rate_limit_event` stream message, and
# claude-agent-sdk 0.1.1's parser RAISED MessageParseError on it ("Unknown message type"),
# killing every task mid-run. Fixed upstream in 0.1.40 (unknown types are skipped, not
# raised) and modelled explicitly since; the pin below is well ahead of that floor. When
# bumping the CLI, bump the SDK to a build that speaks its protocol. app.py also carries a
# belt-and-braces guard so a FUTURE unknown type degrades gracefully instead of crashing.

# node:22-bookworm-slim
FROM node@sha256:83f487e0a63425e5b4d146fb5e5be574bcbe1b7b843d3ebafdd95eaf7767a7e5 AS cli
ARG CLAUDE_CODE_VERSION=2.1.250
# --ignore-scripts, then ONE named script run explicitly. The package's postinstall is
# genuinely required (it selects and wires the platform-native binary), but "run every
# lifecycle script this dependency tree declares, as root" is not the same decision as
# "run this one". install.cjs is the deliberate exception, recorded here.
RUN npm install -g --ignore-scripts "@anthropic-ai/claude-code@${CLAUDE_CODE_VERSION}" \
 && node /usr/local/lib/node_modules/@anthropic-ai/claude-code/install.cjs \
 && /usr/local/bin/claude -v

# python:3.13-slim — same Debian bookworm base as the CLI stage, so the native binary
# copied below links against the same glibc.
FROM python@sha256:7ce4b6dfe35e55397b7cda544f8a13f191b7ae28dc5aad71fe664dbc9bc2623f

COPY --from=cli /usr/local/lib/node_modules/@anthropic-ai /usr/local/lib/node_modules/@anthropic-ai
RUN ln -s /usr/local/lib/node_modules/@anthropic-ai/claude-code/bin/claude.exe \
          /usr/local/bin/claude \
 && /usr/local/bin/claude -v \
 && pip install --no-cache-dir claude-agent-sdk==0.1.81

# Make the SDK use the CLI THIS IMAGE PINS, not the one it ships. Since ~0.1.39 the SDK's
# platform wheels VENDOR their own `claude` binary under `_bundled/`, and `_find_cli()`
# returns that BEFORE it ever consults PATH. Left in place it would silently override the
# version pinned and copied in the `cli` stage above — the exact "tested != published"
# drift the digest/version pins exist to prevent — and it also blinds the smoke test,
# whose security assertions read the argv of a `claude` stub interposed on PATH. Removing
# `_bundled/` drops the SDK back to `shutil.which("claude")`, i.e. the pinned
# /usr/local/bin/claude here and the stub under PATH in the smoke test. The check FAILS
# THE BUILD if a future SDK relocates discovery, so this never regresses unnoticed.
RUN python3 - <<'PY'
import pathlib, shutil, claude_agent_sdk
bundled = pathlib.Path(claude_agent_sdk.__file__).parent / "_bundled"
if bundled.exists():
    shutil.rmtree(bundled)
from claude_agent_sdk._internal.transport.subprocess_cli import SubprocessCLITransport as T
t = T.__new__(T)
assert t._find_bundled_cli() is None, "SDK still resolves a bundled CLI after removal"
resolved = t._find_cli()
assert resolved == "/usr/local/bin/claude", \
    f"SDK would run {resolved!r}, not the image-pinned /usr/local/bin/claude"
print("SDK CLI discovery pinned to", resolved)
PY

# Non-root, same uid convention as every other platform workload. The SDK writes its
# session/config under $HOME, so it must be writable.
RUN useradd --uid 10001 --create-home orbit
USER 10001
ENV HOME=/home/orbit

# Skills mount point: the core skills library is mounted here as one directory per
# skill (each holding a SKILL.md). This is `user` scope for the SDK's settings
# discovery — app.py must name setting_sources=["user"] or the mount is inert.
ENV ORBIT_SKILLS_DIR=/home/orbit/.claude/skills
# The model's working directory, deliberately NOT $HOME: $HOME is the SDK's own config
# directory, and a tool's blast radius should not start inside ~/.claude.
ENV ORBIT_AGENT_WORKDIR=/home/orbit/work
RUN mkdir -p /home/orbit/.claude/skills /home/orbit/work

COPY app.py /app/app.py
EXPOSE 8080
ENTRYPOINT ["python3", "-u", "/app/app.py"]
