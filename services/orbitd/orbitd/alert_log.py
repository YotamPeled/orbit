"""Alertmanager transition log: when each monitor last fired or resolved.

Alertmanager POSTs to /v1/alertmanager; the handler records one transition per
alert carrying a string `orbit_monitor` label. The log keeps the last 20
transitions per monitor (a notification repeating the latest state is not a new
transition) and `orbit_monitor_status` reads the latest back out.
"""

from __future__ import annotations

import re
import threading
from collections import deque
from dataclasses import dataclass
from typing import Any, Literal

TransitionState = Literal["firing", "resolved"]

MAX_HISTORY = 20
MAX_MONITORS = 512
# A monitor name is a DNS label (the shape monitors.py renders); a stamp is RFC 3339
# as Alertmanager writes it. The webhook is reachable with one shared secret, so a
# name or stamp of another shape is not kept: the log is read back by a tool.
MONITOR_NAME_RE = re.compile(r"[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?")
STAMP_RE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(\.[0-9]{1,9})?(Z|[+-][0-9]{2}:[0-9]{2})")


@dataclass(frozen=True)
class Transition:
    state: TransitionState
    at: str  # the alert's startsAt (firing) or endsAt (resolved)


class AlertLog:
    """Thread-safe per-monitor rings of transitions, oldest first on read."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._rings: dict[str, deque[Transition]] = {}
        self._touched: dict[str, int] = {}
        self._seq = 0

    def record(self, name: str, state: TransitionState, at: str) -> bool:
        """Append unless the latest transition already has this state. A name that
        is not a monitor name records nothing; a stamp that is not RFC 3339 is kept
        as the empty string (the transition itself still counts)."""
        if not isinstance(name, str) or not MONITOR_NAME_RE.fullmatch(name):
            return False
        if state not in ("firing", "resolved"):
            return False
        if not isinstance(at, str) or not STAMP_RE.fullmatch(at):
            at = ""
        with self._lock:
            ring = self._rings.get(name)
            if ring is None:
                if len(self._rings) >= MAX_MONITORS:
                    oldest = min(self._touched, key=self._touched.get)
                    del self._rings[oldest]
                    del self._touched[oldest]
                ring = self._rings[name] = deque(maxlen=MAX_HISTORY)
            self._seq += 1
            self._touched[name] = self._seq
            if ring and ring[-1].state == state:
                return False
            ring.append(Transition(state=state, at=at))
            return True

    def names(self) -> list[str]:
        """Monitor names with a recorded transition, most recently recorded first."""
        with self._lock:
            return sorted(self._rings, key=self._touched.get, reverse=True)  # type: ignore[arg-type]

    def history(self, name: str) -> list[Transition]:
        with self._lock:
            return list(self._rings.get(name, ()))

    def latest(self, name: str) -> Transition | None:
        with self._lock:
            ring = self._rings.get(name)
            return ring[-1] if ring else None


def alert_log_of(state: Any) -> AlertLog:
    """The process's log; it hangs off AppState (see orbitd.state)."""
    log = getattr(state, "alert_log", None)
    if isinstance(log, AlertLog):
        return log
    fresh = AlertLog()
    try:
        state.alert_log = fresh
    except (AttributeError, TypeError):
        pass
    return fresh
