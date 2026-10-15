"""Bounded `helm template` and the shape checks every chart source passes first.

Both renderers (`render` for a project/env pair, `platform` for the platform layer)
hand Application fields to a subprocess. A field reaches argv only through these
checks: a release, namespace or Application name is a DNS label; a chart name and a
target revision have no leading dash (so nothing is read as a flag); a repository
URL is http, https or oci with a host and no userinfo; a local chart, source path or
value file resolves under the instance root. The subprocess itself is bounded in
time and in output: a chart that never answers, or answers without end, is a render
failure, never a held lock or an exhausted process.
"""

from __future__ import annotations

import os
import re
import selectors
import subprocess
import tempfile
import time
from pathlib import Path
from urllib.parse import urlsplit

HELM = "helm"
HELM_TIMEOUT = 300.0
HELM_OUTPUT_MAX = 32 * 1024 * 1024
RENDER_DOCS_MAX = 5000

DNS_LABEL_RE = re.compile(r"[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?")
CHART_RE = re.compile(r"[A-Za-z0-9_][A-Za-z0-9._-]{0,127}(/[A-Za-z0-9_][A-Za-z0-9._-]{0,127})*")
REVISION_RE = re.compile(r"[A-Za-z0-9_][A-Za-z0-9._+-]{0,127}")
REPO_SCHEMES = ("http", "https", "oci")
_HOST = r"[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?(\.[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?)*"
NETLOC_RE = re.compile(_HOST + r"(:[0-9]{1,5})?")
REPO_PATH_RE = re.compile(r"[A-Za-z0-9._~/-]*")


class HelmFailure(Exception):
    """`helm template` did not produce a usable render. The message never repeats an
    Application field: stderr may, and is cut to a bounded prefix."""


def dns_label(value: object) -> bool:
    return isinstance(value, str) and DNS_LABEL_RE.fullmatch(value) is not None


def chart_name(value: object) -> bool:
    return isinstance(value, str) and CHART_RE.fullmatch(value) is not None


def revision(value: object) -> bool:
    return isinstance(value, str) and REVISION_RE.fullmatch(value) is not None


def remote_repo(value: str) -> bool:
    """An http(s) or oci repository URL with a host and nothing that argv or a
    fetch could misread: no userinfo, query or fragment."""
    try:
        parts = urlsplit(value)
    except ValueError:
        return False
    if parts.scheme not in REPO_SCHEMES or not NETLOC_RE.fullmatch(parts.netloc.lower()):
        return False
    if not REPO_PATH_RE.fullmatch(parts.path):
        return False
    return not parts.query and not parts.fragment


def under_root(root: Path, path: Path) -> bool:
    """`path` (symlinks followed) is the instance root or inside it."""
    try:
        base = root.resolve()
        target = path.resolve()
    except (OSError, RuntimeError):
        return False
    return target == base or base in target.parents


def tree_under_root(root: Path, directory: Path) -> bool:
    """Every file under `directory` (symlinks followed, as helm follows them when it
    reads a chart) resolves inside the instance root."""
    try:
        for path in directory.rglob("*"):
            if path.is_symlink() or path.is_file():
                if not under_root(root, path):
                    return False
    except OSError:
        return False
    return True


def run_helm(argv: list[str], *, cwd: Path, env: dict[str, str],
             timeout: float = HELM_TIMEOUT, max_output: int = HELM_OUTPUT_MAX) -> str:
    """Run `helm template` and return its stdout text. Output is read in bounded
    pieces: past `max_output` bytes or `timeout` seconds the process is killed and
    the render fails. stderr goes to a file so a chatty chart cannot stall it."""
    deadline = time.monotonic() + timeout
    chunks: list[bytes] = []
    total = 0
    with tempfile.TemporaryFile() as err:
        try:
            proc = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=err,
                                    stdin=subprocess.DEVNULL, cwd=str(cwd), env=env)
        except FileNotFoundError as e:
            raise HelmFailure(f"helm template failed: {e}") from e
        assert proc.stdout is not None
        fd = proc.stdout.fileno()
        # a poll-based selector: select() refuses descriptors past FD_SETSIZE, which
        # a long-running daemon holding many sockets reaches
        watch = selectors.DefaultSelector()
        watch.register(fd, selectors.EVENT_READ)
        try:
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise HelmFailure(f"helm template gave no complete answer within {int(timeout)}s")
                if not watch.select(remaining):
                    continue
                piece = os.read(fd, 1 << 16)
                if not piece:
                    break
                total += len(piece)
                if total > max_output:
                    raise HelmFailure(f"helm template output exceeded {max_output} bytes")
                chunks.append(piece)
            try:
                proc.wait(timeout=max(deadline - time.monotonic(), 0.0))
            except subprocess.TimeoutExpired as e:
                raise HelmFailure(f"helm template gave no complete answer within {int(timeout)}s") from e
        except BaseException:
            proc.kill()
            proc.wait()
            raise
        finally:
            watch.close()
            proc.stdout.close()
        err.seek(0)
        stderr = err.read(2000).decode(errors="replace")
    if proc.returncode != 0:
        raise HelmFailure(stderr or "helm template failed")
    return b"".join(chunks).decode(errors="replace")


def documents_bounded(count: int) -> None:
    if count > RENDER_DOCS_MAX:
        raise HelmFailure(f"render produced {count} documents; at most {RENDER_DOCS_MAX}")
