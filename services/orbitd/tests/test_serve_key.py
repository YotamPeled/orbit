"""Serve-path AppState.build creates the signing key when a fresh volume has none."""

from __future__ import annotations

import json
import logging
import stat
from pathlib import Path

import httpx2
import pytest
from conftest import ISSUER, MCP_URL, rpc

from orbitd import cli
from orbitd.config import Config
from orbitd.roles import Role
from orbitd.server import build_app
from orbitd.state import AppState

pytestmark = pytest.mark.anyio


def _cfg(tmp_path) -> Config:
    data = tmp_path / "var" / "lib" / "orbitd"
    return Config(
        mcp_url=MCP_URL,
        signing_key_path=str(data / "signing-key.pem"),
        principals_path=str(data / "principals.json"),
        revocations_path=str(data / "revoked.txt"),
        issuer_url=ISSUER,
        allowed_hosts=("mcp.fixture.invalid",),
        allowed_origins=(ISSUER,),
    )


async def test_serve_build_creates_key_on_empty_volume(tmp_path, caplog, monkeypatch, capsys):
    cfg = _cfg(tmp_path)
    caplog.set_level(logging.INFO, logger="orbitd")
    first = AppState.build(cfg)
    key = Path(cfg.signing_key_path)
    assert key.is_file()
    assert stat.S_IMODE(key.stat().st_mode) == 0o600
    assert caplog.text.count("generated signing key") == 1
    assert first.store.load()["principals"] == {}
    assert first.revocations.read() == set()

    app = build_app(first)
    async with app.router.lifespan_context(app):
        async with httpx2.AsyncClient(transport=httpx2.ASGITransport(app=app), base_url=ISSUER) as h:
            healthz = await h.get("/healthz")
            unauth = await rpc(h, "tools/list")
    assert healthz.status_code == 200 and healthz.json()["status"] == "ok"
    assert unauth.status_code == 401

    second = AppState.build(cfg)
    assert second.key.kid == first.key.kid
    assert caplog.text.count("generated signing key") == 1

    for k, v in {
        "ORBITD_MCP_URL": MCP_URL,
        "ORBITD_SIGNING_KEY": cfg.signing_key_path,
        "ORBITD_PRINCIPALS": cfg.principals_path,
        "ORBITD_REVOCATIONS": cfg.revocations_path,
    }.items():
        monkeypatch.setenv(k, v)
    assert cli.main(["bootstrap"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["client_id"] == "admin" and out["role"] == "admin"
    assert second.store.check_secret("admin", out["client_secret"])
    assert second.store.admins() == ["admin"]
    token, claims = second.tokens.mint("admin", Role.admin)
    assert claims["sub"] == "admin"
    assert token


def test_unreadable_existing_key_fails_loudly(tmp_path):
    cfg = _cfg(tmp_path)
    key = Path(cfg.signing_key_path)
    key.parent.mkdir(parents=True)
    key.write_bytes(b"not-an-ed25519-pem")
    key.chmod(0o600)
    with pytest.raises(ValueError):
        AppState.build(cfg)
    assert key.read_bytes() == b"not-an-ed25519-pem"
