"""Operator tools: ship, rollback, restart, cancel; and the admin reconcile. Registered once from server.py."""

from __future__ import annotations

from orbitd.roles import Registry
from orbitd.state import AppState

from . import cancel, catalog, features, guardrails, monitors, reconcile, restart, ship


def register(registry: Registry, state: AppState) -> None:
    ship.register(registry, state)
    restart.register(registry, state)
    reconcile.register(registry, state)
    monitors.register(registry, state)
    features.register(registry, state)
    guardrails.register(registry, state)
    catalog.register(registry, state)
    cancel.register(registry, state)
