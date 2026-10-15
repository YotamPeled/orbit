"""orbit_monitor_list — declared monitors from the instance repo."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel

from ..roles import Listing, Registry, Role
from ..state import AppState
from .util import run


class Monitor(BaseModel):
    name: str
    project: str
    env: str
    kind: str
    severity: str
    summary: str
    notify: list[str] = []


def register(registry: Registry, state: AppState) -> None:

    @registry.tool(role=Role.viewer, read_only=True, idempotent=True)
    def orbit_monitor_list() -> Listing:
        """List monitor definitions from the instance repo.

        Reads monitors/*.yaml via the instance backend. Each item is {name, project,
        env, kind, severity, summary, notify}; `notify` is who is messaged when it
        fires (`["telegram"]`, or empty: nobody). name is what orbit_monitor_status
        accepts, and that tool returns the whole definition (query, condition, target).
        Example: orbit_monitor_list() returns {items: [{name: "queue-backlog",
        project: "demo", env: "prod", kind: "metric", ...}, ...]}."""

        def _go() -> Listing:
            state.backends.instance.require()
            items = [_item(body) for body in state.backends.instance.monitors()
                     if isinstance(body, dict) and body.get("name")]
            return Listing.of([i.model_dump() for i in items])

        return run(_go)  # type: ignore[return-value]


def _item(body: dict[str, Any]) -> Monitor:
    return Monitor(
        name=str(body.get("name") or ""),
        project=str(body.get("project") or ""),
        env=str(body.get("env") or ""),
        kind=str(body.get("kind") or ""),
        severity=str(body.get("severity") or ""),
        summary=str(body.get("summary") or ""),
        notify=[str(n) for n in body.get("notify") or []] if isinstance(body.get("notify"), list) else [],
    )
