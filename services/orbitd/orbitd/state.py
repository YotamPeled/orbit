"""What every tool and route shares: config, key, store, tokens, backends. Built once in server.py."""

from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass, field
from typing import Any

from .alert_log import AlertLog
from .auth import Revocations, SigningKey, Store, Tokens, generate_key
from .backends import Backends
from .config import Config
from .features import Features

log = logging.getLogger("orbitd")


@dataclass
class AppState:
    cfg: Config
    key: SigningKey
    store: Store
    revocations: Revocations
    tokens: Tokens
    backends: Backends
    started_at: float = field(default_factory=time.time)
    alert_log: AlertLog = field(default_factory=AlertLog)
    features: Features = field(init=False)
    # the MCP subscription bus, set when the server is built (server.build_server)
    subscriptions: Any = field(default=None, init=False)

    def __post_init__(self) -> None:
        # which optional features are on, read from the instance checkout
        self.features = Features(self.backends.instance)

    @classmethod
    def build(cls, cfg: Config, *, backends: Backends | None = None) -> AppState:
        path = cfg.signing_key_path
        if not os.path.exists(path):
            parent = os.path.dirname(path)
            if parent:
                os.makedirs(parent, exist_ok=True)
            generate_key(path)
            log.info("generated signing key %s", path)
        key = SigningKey(path)
        store = Store(cfg.principals_path)
        revocations = Revocations(cfg.revocations_path)
        return cls(cfg=cfg, key=key, store=store, revocations=revocations,
                   tokens=Tokens(cfg, key, store, revocations),
                   backends=backends if backends is not None else Backends.from_config(cfg))
