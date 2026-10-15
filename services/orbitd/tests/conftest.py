"""One fixture world per test: a fresh key, an empty store, an empty revocation list,
and a canonical MCP URL on a host that exists nowhere. Requests go through the real
ASGI app (auth middleware, transport, gate) over httpx2's in-process transport."""

from __future__ import annotations

import contextlib
from collections.abc import AsyncIterator
from typing import Any

import pytest

try:
    import httpx2
    from mcp import Client
    from mcp.client.streamable_http import streamable_http_client
    from mcp_types import CLIENT_CAPABILITIES_META_KEY, PROTOCOL_VERSION_META_KEY, ElicitResult

    from orbitd import auth
    from orbitd.config import Config
    from orbitd.roles import Role
    from orbitd.server import PROTOCOL_REVISION, build_app
    from orbitd.state import AppState
    from orbitd.testing import FakeBackends
except ImportError:
    # The root conftest's pytest_configure builds .venv and re-execs; nothing below runs here.
    pass

MCP_URL = "https://mcp.fixture.invalid/mcp"
ISSUER = "https://mcp.fixture.invalid"


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture
def cfg(tmp_path) -> Config:
    key = tmp_path / "signing.pem"
    auth.generate_key(str(key))
    return Config(mcp_url=MCP_URL, signing_key_path=str(key), principals_path=str(tmp_path / "principals.json"),
                  revocations_path=str(tmp_path / "revoked.txt"), issuer_url=ISSUER,
                  allowed_hosts=("mcp.fixture.invalid",), allowed_origins=(ISSUER,))


@pytest.fixture
def state(cfg) -> AppState:
    return AppState.build(cfg)


@pytest.fixture
def fake_backends(cfg, tmp_path) -> AppState:
    """AppState wired to in-memory backends. Viewer-tool tests use only this fixture."""
    return AppState.build(cfg, backends=FakeBackends.build(tmp_path))


@pytest.fixture
async def app(fake_backends):
    application = build_app(fake_backends)
    async with application.router.lifespan_context(application):
        yield application


@pytest.fixture
def http(app):
    """Raw httpx2 client factory: `http(token)`; no token means no Authorization header."""

    def make(token: str | None = None, **headers: str) -> httpx2.AsyncClient:
        h = dict(headers)
        if token:
            h["Authorization"] = f"Bearer {token}"
        return httpx2.AsyncClient(transport=httpx2.ASGITransport(app=app), base_url=ISSUER, headers=h)

    return make


def mint(state: AppState, role: Role, sub: str = "someone", **override: Any) -> str:
    token, _ = state.tokens.mint(sub, role, **override)
    return token


def envelope(method: str, params: dict[str, Any] | None = None, *, id: int = 1) -> dict[str, Any]:
    """A 2026-07-28 request body: the per-request `_meta` envelope replaces `initialize`."""
    p = dict(params or {})
    p["_meta"] = {PROTOCOL_VERSION_META_KEY: PROTOCOL_REVISION, CLIENT_CAPABILITIES_META_KEY: {}}
    return {"jsonrpc": "2.0", "id": id, "method": method, "params": p}


def mcp_headers(method: str, name: str | None = None) -> dict[str, str]:
    h = {"MCP-Protocol-Version": PROTOCOL_REVISION, "Mcp-Method": method,
         "Accept": "application/json, text/event-stream", "Content-Type": "application/json"}
    if name:
        h["Mcp-Name"] = name
    return h


async def rpc(client: httpx2.AsyncClient, method: str, params: dict[str, Any] | None = None) -> httpx2.Response:
    name = (params or {}).get("name")
    return await client.post("/mcp", json=envelope(method, params), headers=mcp_headers(method, name))


def answer(confirm: bool | None):
    """An elicitation callback: True/False answers the form, None declines."""

    async def callback(_context, params) -> ElicitResult:
        if confirm is None:
            return ElicitResult(action="decline")
        return ElicitResult(action="accept", content={"confirm": confirm})

    return callback


@contextlib.asynccontextmanager
async def sdk_client(http, token: str, confirm: bool | None = None) -> AsyncIterator[Client]:
    """The official SDK client, negotiating via server/discover, over the same ASGI app."""
    async with http(token) as h:
        transport = streamable_http_client(MCP_URL, http_client=h)
        async with Client(transport, elicitation_callback=answer(confirm)) as client:
            yield client
