"""Which optional features are on for this instance, as orbitd reads it.

The registry (provisioning/features.yaml) and the instance's `features:` block are both
in the instance checkout; `orbit_features` is the same reading the scaffolder and the
verify stages use (a byte-for-byte copy of scripts/verify/orbit_features.py, held equal
by scripts/test_features_registry.py). The role gate asks this module which tools exist
right now: a tool owned by a feature that is off is not listed and cannot be called.

A checkout that cannot be read (no registry: a tree from before features; or a config the
reading refuses) gates nothing new: the last good reading stays, and before any reading
every tool is offered. `problem` says which, for the status tools.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

import yaml

from . import orbit_features as rules
from .backends import InstanceRepo

CONFIG_REL = "instance/config.yaml"
# How long one reading of the checkout is reused. git-sync moves the checkout at most
# once a minute; a tools/list arrives far more often.
TTL_S = 5.0


def enable_hint(feature: str) -> str:
    """How a feature that is off gets turned on, for a refusal's next_action. One
    sentence in one place: every refusal that names a feature says the same thing."""
    return (f"an admin turns it on: `orbit features enable {feature}` "
            f"(the orbit_features_set tool, feature={feature}, enabled=true)")


@dataclass(frozen=True)
class Snapshot:
    registry: dict[str, Any] = field(default_factory=dict)
    on: dict[str, bool] = field(default_factory=dict)
    explicit: bool = False
    problem: str | None = None

    def off_owner(self, tool: str) -> str | None:
        """The feature that owns `tool` and is off, or None (the tool is offered)."""
        owner = rules.tool_owner(tool, self.registry) if self.registry else None
        if owner is not None and not self.on.get(owner, True):
            return owner
        return None

    def hidden_tools(self) -> frozenset[str]:
        return frozenset(tool for name, spec in self.registry.items()
                         if not spec["always"] and not self.on.get(name, True)
                         for tool in spec["tools"])


def read(root: Path) -> Snapshot:
    """One reading of a checkout. Raises rules.FeatureError when it cannot be read."""
    registry = rules.load_registry(root)
    try:
        config = yaml.safe_load((root / CONFIG_REL).read_text(encoding="utf-8")) or {}
    except OSError as e:
        raise rules.FeatureError(f"{CONFIG_REL} cannot be read ({e.__class__.__name__})") from e
    except yaml.YAMLError as e:
        raise rules.FeatureError(f"{CONFIG_REL} is not YAML ({e.__class__.__name__})") from e
    if not isinstance(config, dict):
        raise rules.FeatureError(f"{CONFIG_REL} is not a mapping")
    return Snapshot(registry=registry, on=rules.states(config, registry),
                    explicit=rules.is_explicit(config))


class Features:
    """The current reading, cached for TTL_S. Thread-safe: tools run on worker threads."""

    def __init__(self, repo: InstanceRepo, *, clock: Callable[[], float] = time.monotonic) -> None:
        self._repo = repo
        self._clock = clock
        self._lock = threading.Lock()
        self._at: float | None = None
        self._snapshot = Snapshot()

    def snapshot(self, *, fresh: bool = False) -> Snapshot:
        with self._lock:
            now = self._clock()
            if not fresh and self._at is not None and now - self._at < TTL_S:
                return self._snapshot
            self._at = now
            root = self._repo.root
            if root is None:
                self._snapshot = Snapshot()
                return self._snapshot
            try:
                self._snapshot = read(Path(root))
            except rules.RegistryMissing:
                # a tree vendored before features existed: everything it has is on
                self._snapshot = Snapshot()
            except rules.FeatureError as e:
                # keep what was last read; say why the checkout's own reading failed
                self._snapshot = Snapshot(registry=self._snapshot.registry, on=self._snapshot.on,
                                          explicit=self._snapshot.explicit, problem=str(e))
            return self._snapshot

    def off_owner(self, tool: str) -> str | None:
        return self.snapshot().off_owner(tool)
