"""How fresh the instance checkout was when a payload was built.

orbitd reads desired state from a git-sync worktree that lags the instance repo
by up to ORBITD_SOURCE_SYNC_PERIOD_S seconds. Payloads that read that checkout
carry a `source` object so a caller can tell a stale read from a current one.
"""

from __future__ import annotations

import json
import math
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from .backends import BackendError, BackendUnconfigured, InstanceRepo
from .roles import CheckoutSource, Mutation
from .state import AppState

DEFAULT_PERIOD_S = 60
LAG_NOTE = "desired state follows the instance repo by up to {n} s"

# git-sync --stat-file JSON and the well-known sidecar names next to a checkout.
_SYNC_KEYS = ("synced", "syncTime", "lastSyncTime", "timestamp")
_SIDECARS = (
    ".git/git-sync",
    "git-sync.json",
    ".git-sync-stats",
)


def period_of(state: AppState) -> int:
    raw = getattr(state.cfg, "source_sync_period_s", DEFAULT_PERIOD_S)
    try:
        n = int(raw)
    except (TypeError, ValueError):
        return DEFAULT_PERIOD_S
    return n if n >= 0 else DEFAULT_PERIOD_S


def sourced(state: AppState, result: Any) -> Any:
    """Stamp `source` on a Mutation that does not already carry it; pass other results through."""
    if not isinstance(result, Mutation):
        return result
    if result.source is not None:
        return result
    return result.model_copy(update={"source": read_source(state)})


def read_source(state: AppState, *, now: float | None = None) -> CheckoutSource:
    """The commit desired state is read from, seconds since the checkout last synced, and
    the configured lag bound.

    With the commit gate on, that commit is the last one the gate accepted (its tree is
    what every reader gets), which is the checkout's HEAD except while HEAD is refused or
    not judged yet. `read_head` is the checkout's own HEAD, whatever the gate says.
    """
    head = read_head(state, now=now)
    pinned = getattr(state.backends.instance, "pinned_commit", None)
    if pinned is None:
        return head
    return CheckoutSource(commit=pinned, age_s=head.age_s, lag_note=head.lag_note)


def read_head(state: AppState, *, now: float | None = None) -> CheckoutSource:
    """HEAD sha of the git-sync checkout, seconds since last sync, and the configured lag
    bound.

    One call reads the checkout once. Prefer git-sync's JSON sidecar; fall back
    to the checkout's HEAD mtime.
    """
    period = period_of(state)
    note = LAG_NOTE.format(n=period)
    clock = time.time() if now is None else float(now)
    try:
        repo = state.backends.instance
        root = repo.checkout() if hasattr(repo, "checkout") else repo.require()
    except BackendUnconfigured:
        return CheckoutSource(commit="", age_s=0, lag_note=note)
    commit = _head_sha(repo)
    age = _age_s(repo, root, clock)
    return CheckoutSource(commit=commit, age_s=age, lag_note=note)


def _head_sha(repo: InstanceRepo) -> str:
    try:
        text = (repo.run_git(["rev-parse", "HEAD"]) or "").strip()
    except BackendError:
        return ""
    return text


def _age_s(repo: InstanceRepo, root: Path, now: float) -> int:
    mtime = _gitsync_mtime(root)
    if mtime is None:
        mtime = _head_mtime(repo, root)
    if mtime is None:
        return 0
    return max(0, int(now - mtime))


def _gitsync_mtime(root: Path) -> float | None:
    candidates = [root / rel for rel in _SIDECARS]
    parent = root.parent
    if parent != root:
        candidates.extend(parent / rel for rel in _SIDECARS)
    for path in candidates:
        if not path.is_file():
            continue
        parsed = _parse_sidecar(path)
        if parsed is not None:
            return parsed
        try:
            return path.stat().st_mtime
        except OSError:
            continue
    return None


def _parse_sidecar(path: Path) -> float | None:
    try:
        text = path.read_text()
    except OSError:
        return None
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return None
    return _parse_synced(data)


def _parse_synced(data: Any) -> float | None:
    if not isinstance(data, dict):
        return None
    for key in _SYNC_KEYS:
        value = data.get(key)
        if value is None:
            continue
        if isinstance(value, bool):
            continue
        if isinstance(value, (int, float)):
            return float(value) if math.isfinite(value) else None
        if isinstance(value, str):
            text = value.strip()
            if not text:
                continue
            try:
                parsed = float(text)
            except ValueError:
                pass
            else:
                # "nan"/"inf" parse as floats; int() of either raises out of every tool
                return parsed if math.isfinite(parsed) else None
            try:
                return datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp()
            except ValueError:
                continue
    return None


def _head_mtime(repo: InstanceRepo, root: Path) -> float | None:
    path = _head_path(repo, root)
    if path is None:
        return None
    try:
        return path.stat().st_mtime
    except OSError:
        return None


def _head_path(repo: InstanceRepo, root: Path) -> Path | None:
    try:
        rel = (repo.run_git(["rev-parse", "--git-path", "HEAD"]) or "").strip()
    except BackendError:
        rel = ""
    if rel:
        candidate = Path(rel)
        if not candidate.is_absolute():
            candidate = root / candidate
        if candidate.is_file():
            return candidate
    for candidate in (root / ".git" / "HEAD", root.parent / ".git" / "HEAD"):
        if candidate.is_file():
            return candidate
    return None


__all__ = [
    "DEFAULT_PERIOD_S", "LAG_NOTE", "CheckoutSource", "period_of", "read_source",
    "sourced",
]
