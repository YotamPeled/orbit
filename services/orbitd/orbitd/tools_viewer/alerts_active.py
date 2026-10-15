"""orbit_alerts_active — Alertmanager /api/v2/alerts, active and not silenced."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel

from ..roles import Listing, Registry, Role
from ..state import AppState
from .common import ClockedListing, clocked, relayed
from .util import run


class ActiveAlert(BaseModel):
    name: str
    severity: str
    labels: dict[str, str]
    starts_at: str
    summary: str
    # What the rule's author wrote about the cause and the next step. Until v0.34.28 it
    # was dropped whenever the rule also had a summary, which is every rule the platform
    # ships: the sentence "then ask orbit_size_recommend" never reached the agent it was
    # written for (S06e, 2026-10-09, docs/V1.md §8).
    description: str = ""


def register(registry: Registry, state: AppState) -> None:

    @registry.tool(role=Role.viewer, read_only=True, idempotent=True)
    def orbit_alerts_active() -> ClockedListing:
        """List alerts currently firing in Alertmanager.

        GET /api/v2/alerts restricted to active, not-silenced alerts. Each item is
        {name, severity, labels, starts_at, summary, description}; name is the
        alertname label, and description is the rule's own account of the cause and
        the next step (empty when the rule has none).
        `now` is the server clock as RFC 3339.
        Example: orbit_alerts_active() returns {items: [{name: "Watchdog",
        severity: "none", ...}, ...]}."""

        def _go() -> ClockedListing:
            state.backends.alertmanager.require()
            data = state.backends.alertmanager.get(
                "/api/v2/alerts",
                params={"active": "true", "silenced": "false", "inhibited": "false"},
            )
            items = [_item(row) for row in _rows(data) if _active(row)]
            return clocked(Listing.of([i.model_dump() for i in items]))

        return run(_go)  # type: ignore[return-value]


def _rows(data: object) -> list[dict[str, Any]]:
    if isinstance(data, list):
        return [row for row in data if isinstance(row, dict)]
    if isinstance(data, dict):
        alerts = data.get("data") or data.get("alerts") or []
        if isinstance(alerts, list):
            return [row for row in alerts if isinstance(row, dict)]
    return []


def _active(row: dict[str, Any]) -> bool:
    status = row.get("status") or {}
    if isinstance(status, dict):
        if status.get("silencedBy"):
            return False
        state = status.get("state")
        if state and state not in ("active", "unprocessed"):
            return False
    return True


def _item(row: dict[str, Any]) -> ActiveAlert:
    labels = {relayed(k, limit=256): relayed(v, limit=1024) for k, v in (row.get("labels") or {}).items()}
    annotations = row.get("annotations") or {}
    summary = description = ""
    if isinstance(annotations, dict):
        summary = relayed(annotations.get("summary") or annotations.get("description") or "", limit=1024)
        if annotations.get("summary"):
            description = relayed(annotations.get("description") or "", limit=2048)
    return ActiveAlert(
        name=labels.get("alertname") or labels.get("alert") or "",
        severity=labels.get("severity") or "",
        labels=labels,
        starts_at=relayed(row.get("startsAt") or row.get("starts_at") or "", limit=64),
        summary=summary,
        description=description,
    )
