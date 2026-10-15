"""Runtime configuration. Every instance-specific value comes from the environment;
nothing here names a domain, an org or a cluster."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from urllib.parse import urlparse

THIRTY_DAYS = 30 * 24 * 3600


@dataclass(frozen=True)
class Config:
    mcp_url: str                      # canonical MCP URL, the token audience (RFC 8707), e.g. https://mcp.example/mcp
    signing_key_path: str             # Ed25519 private key, PEM
    principals_path: str              # JSON store: principals and the issued-token ledger
    revocations_path: str             # one revoked jti per line (the ConfigMap mount, later)
    issuer_url: str                   # RFC 8414 issuer; defaults to the MCP URL's origin
    token_ttl: int = THIRTY_DAYS      # default access-token lifetime, seconds
    allowed_hosts: tuple[str, ...] = ()
    allowed_origins: tuple[str, ...] = ()
    port: int = 8080
    # Cluster and observability backends. None are required at import; a tool that
    # needs one that is unset returns the actionable failure `backend_unconfigured`
    # naming the variable. ORBITD_KUBECONFIG absent means in-cluster, not unset.
    instance_dir: str | None = None
    prometheus_url: str | None = None
    loki_url: str | None = None
    tempo_url: str | None = None
    alertmanager_url: str | None = None
    alert_webhook_token_file: str | None = None  # ORBITD_ALERT_WEBHOOK_TOKEN_FILE; unset means no webhook route
    kubeconfig: str | None = None
    state_dir: str | None = None  # ORBITD_STATE_DIR; default: parent of the signing key
    source_sync_period_s: int = 60  # ORBITD_SOURCE_SYNC_PERIOD_S; git-sync lag bound
    apply_mode: str = "off"  # ORBITD_APPLY: off | dry-run | on; the applier's hard switch
    reconcile_period_s: int = 180  # ORBITD_RECONCILE_PERIOD_S; seconds between reconcile ticks
    apply_pairs: tuple[str, ...] = ()  # ORBITD_APPLY_PAIRS: project/env; empty = every catalog pair
    extra: dict[str, str] = field(default_factory=dict)

    @property
    def origin(self) -> str:
        p = urlparse(self.mcp_url)
        return f"{p.scheme}://{p.netloc}"

    @property
    def mcp_path(self) -> str:
        return urlparse(self.mcp_url).path or "/"


def _split(value: str | None) -> tuple[str, ...]:
    return tuple(v.strip() for v in (value or "").split(",") if v.strip())


def _optional(env, key: str) -> str | None:
    value = (env.get(key) or "").strip()
    return value or None


def _optional_url(env, key: str) -> str | None:
    value = _optional(env, key)
    return value.rstrip("/") if value else None


def _period_s(raw: str | None, default: int = 60) -> int:
    text = (raw or "").strip()
    if not text:
        return default
    try:
        n = int(text)
    except ValueError:
        return default
    return n if n >= 0 else default


APPLY_MODES = ("off", "dry-run", "on")
RECONCILE_PERIOD_S = 180


def _apply_mode(raw: str | None) -> str:
    text = (raw or "").strip().lower()
    if not text:
        return "off"
    if text not in APPLY_MODES:
        raise SystemExit(f"orbitd: ORBITD_APPLY must be one of {', '.join(APPLY_MODES)}: {raw!r}")
    return text


def from_env(env: dict[str, str] | None = None) -> Config:
    env = os.environ if env is None else env
    missing = [k for k in ("ORBITD_MCP_URL", "ORBITD_SIGNING_KEY", "ORBITD_PRINCIPALS", "ORBITD_REVOCATIONS")
               if not env.get(k)]
    if missing:
        raise SystemExit("orbitd: missing required environment: " + ", ".join(missing))
    mcp_url = env["ORBITD_MCP_URL"].rstrip("/")
    p = urlparse(mcp_url)
    if p.scheme not in ("http", "https") or not p.netloc:
        raise SystemExit(f"orbitd: ORBITD_MCP_URL is not an absolute http(s) URL: {mcp_url!r}")
    origin = f"{p.scheme}://{p.netloc}"
    return Config(
        mcp_url=mcp_url,
        signing_key_path=env["ORBITD_SIGNING_KEY"],
        principals_path=env["ORBITD_PRINCIPALS"],
        revocations_path=env["ORBITD_REVOCATIONS"],
        issuer_url=(env.get("ORBITD_ISSUER_URL") or origin).rstrip("/"),
        token_ttl=int(env.get("ORBITD_TOKEN_TTL_SECONDS") or THIRTY_DAYS),
        allowed_hosts=_split(env.get("ORBITD_ALLOWED_HOSTS")) or (p.netloc,),
        allowed_origins=_split(env.get("ORBITD_ALLOWED_ORIGINS")) or (origin,),
        port=int(env.get("PORT") or 8080),
        instance_dir=_optional(env, "ORBITD_INSTANCE_DIR"),
        prometheus_url=_optional_url(env, "ORBITD_PROMETHEUS_URL"),
        loki_url=_optional_url(env, "ORBITD_LOKI_URL"),
        tempo_url=_optional_url(env, "ORBITD_TEMPO_URL"),
        alertmanager_url=_optional_url(env, "ORBITD_ALERTMANAGER_URL"),
        alert_webhook_token_file=_optional(env, "ORBITD_ALERT_WEBHOOK_TOKEN_FILE"),
        kubeconfig=_optional(env, "ORBITD_KUBECONFIG"),
        state_dir=_optional(env, "ORBITD_STATE_DIR"),
        source_sync_period_s=_period_s(env.get("ORBITD_SOURCE_SYNC_PERIOD_S")),
        apply_mode=_apply_mode(env.get("ORBITD_APPLY")),
        reconcile_period_s=_period_s(env.get("ORBITD_RECONCILE_PERIOD_S"), RECONCILE_PERIOD_S),
        apply_pairs=_split(env.get("ORBITD_APPLY_PAIRS")),
    )
