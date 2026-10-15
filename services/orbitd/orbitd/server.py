"""The process: one Starlette app carrying the MCP endpoint (stateless Streamable HTTP,
revision 2026-07-28), the two OAuth metadata documents, the token endpoint, the JWKS
and a health probe.

Composition, outermost first:
  AuthenticationMiddleware(BearerAuthBackend)  every request; a bad token is simply no user
  AuthContextMiddleware                        exposes the token to handlers (get_access_token)
  <mcp path>: RequireAuthMiddleware            401 + WWW-Authenticate when there is no user
              StreamableHTTPASGIApp            the SDK transport; HeaderMismatch, Origin, dispatch
                RoleGate (handler tier)        the only gate: tools/list filtered,
                                               tools/call gated with an in-protocol
                                               isError, one audit line
"""

from __future__ import annotations

import contextlib
import hmac
import json
import logging
import sys
from collections.abc import AsyncIterator, Sequence
from pathlib import Path
from typing import Any, Protocol

import anyio
from mcp.server.auth.middleware.auth_context import AuthContextMiddleware
from mcp.server.auth.middleware.bearer_auth import BearerAuthBackend, RequireAuthMiddleware
from mcp.server.auth.settings import AuthSettings
from mcp.server.caching import CacheHint
from mcp.server.mcpserver import MCPServer
from mcp.server.request_state import RequestStateSecurity
from mcp.server.streamable_http_manager import StreamableHTTPASGIApp
from mcp.server.subscriptions import InMemorySubscriptionBus, ToolsListChanged
from mcp.server.transport_security import TransportSecuritySettings
from mcp_types import LATEST_PROTOCOL_VERSION
from pydantic import AnyHttpUrl
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.middleware.authentication import AuthenticationMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, PlainTextResponse, Response
from starlette.routing import Route

from . import __version__, auth, tools, tools_operator, tools_viewer
from .alert_log import TransitionState, alert_log_of
from .audit import configure_logging
from .config import Config
from .roles import Registry, Role, RoleGate
from .state import AppState

log = logging.getLogger("orbitd")

PROTOCOL_REVISION = "2026-07-28"
assert LATEST_PROTOCOL_VERSION == PROTOCOL_REVISION, f"mcp SDK revision drifted to {LATEST_PROTOCOL_VERSION}"

INSTRUCTIONS = (
    "orbitd is the control surface of an Orbit platform instance. Every tool is one verb with "
    "typed input; mutating tools return {status, steps, commit, evidence} and lists return "
    "{items, next_cursor}. Roles are viewer < operator < admin; tools/list shows only what your "
    "token may call. Destructive tools ask for confirmation through an elicitation before applying."
)

TOOLS_LIST_TTL_MS = 60_000
STOP_TIMEOUT_S = 10.0


class Runner(Protocol):
    """A background loop the process owns: the reconcile and shadow runners."""

    def stop(self, timeout: float = ...) -> bool: ...


def stop_runners(runners: Sequence[Runner | None], timeout: float = STOP_TIMEOUT_S) -> None:
    """Signal every runner and wait (bounded) for its thread; idempotent, None is skipped."""
    for runner in runners:
        if runner is not None:
            runner.stop(timeout=timeout)


MAX_ALERT_BODY = 1024 * 1024


def _webhook_authorized(request: Request, token_file: str) -> bool:
    """The webhook bearer, compared in constant time with the file's stripped
    content. The file is re-read on every request so rotation needs no restart.
    An MCP token is not it."""
    scheme, sep, credential = request.headers.get("authorization", "").partition(" ")
    if not sep or scheme.lower() != "bearer" or not credential:
        return False
    try:
        expected = Path(token_file).read_text(encoding="utf-8").strip()
    except OSError:
        return False
    if not expected:
        return False
    try:
        return hmac.compare_digest(credential, expected)
    except TypeError:
        return False


def _webhook_transition(alert: Any) -> tuple[str, TransitionState, str] | None:
    """The (monitor, state, at) an alert records, or None when it records nothing:
    alerts without a string orbit_monitor label or with another status are ignored."""
    if not isinstance(alert, dict):
        return None
    labels = alert.get("labels")
    if not isinstance(labels, dict):
        return None
    name = labels.get("orbit_monitor")
    if not isinstance(name, str) or not name:
        return None
    status = alert.get("status")
    if status == "firing":
        stamp = alert.get("startsAt")
    elif status == "resolved":
        stamp = alert.get("endsAt")
    else:
        return None
    at = stamp if isinstance(stamp, str) else ""
    return (name, status, at)


def build_server(state: AppState, registry: Registry | None = None) -> tuple[MCPServer, Registry]:
    cfg = state.cfg
    # Where a change of the tool list is announced to open `subscriptions/listen` streams
    # (watch_features publishes on it). In-process: orbitd runs as one replica.
    state.subscriptions = InMemorySubscriptionBus()
    registry = registry or Registry()
    tools.register(registry, state)
    server = MCPServer(
        "orbitd", version=__version__, instructions=INSTRUCTIONS,
        token_verifier=state.tokens,
        auth=AuthSettings(issuer_url=AnyHttpUrl(cfg.issuer_url), resource_server_url=AnyHttpUrl(cfg.mcp_url),
                          validate_token_resource=True, required_scopes=[Role.viewer.scope]),
        request_state_security=RequestStateSecurity(keys=[state.key.request_state_secret]),
        # tools/list varies by the token presented, so the hint is private and short.
        cache_hints={"tools/list": CacheHint(ttl_ms=TOOLS_LIST_TTL_MS, scope="private")},
        middleware=[RoleGate(registry, features=state.features)],
        subscriptions=state.subscriptions,
    )
    tools_viewer.register(registry, state, server)
    tools_operator.register(registry, state)
    registry.mount(server)
    return server, registry


# How often the instance checkout is read again for a change of the tool list. It is also
# the debounce: however often `features:` flips inside one period, at most one
# notification leaves, and none when the list ends where it started.
FEATURES_WATCH_PERIOD_S = 10.0


async def watch_features(state: AppState, *, period_s: float | None = None,
                         sleep: Any = anyio.sleep) -> None:
    """Announce a changed tool list to every open `subscriptions/listen` stream.

    The tools offered follow the features that are on (roles.RoleGate); a commit that
    turns a feature on or off reaches the checkout through git-sync, and this loop is what
    notices. A client told `tools/list_changed` asks for the list again and finds its own
    role's view of it."""
    period = FEATURES_WATCH_PERIOD_S if period_s is None else period_s

    def hidden() -> frozenset[str]:
        return state.features.snapshot(fresh=True).hidden_tools()

    seen = await anyio.to_thread.run_sync(hidden)
    while True:
        await sleep(period)
        try:
            now = await anyio.to_thread.run_sync(hidden)
        except Exception:  # boundary: an unreadable checkout must not end the watch
            log.exception("reading the features of the instance checkout failed")
            continue
        if now != seen:
            log.info("the tool list changed with the features: %d tool(s) now off (was %d)",
                     len(now), len(seen))
            seen = now
            await state.subscriptions.publish(ToolsListChanged())


def build_app(state: AppState, *, runners: Sequence[Runner | None] = ()) -> Starlette:
    """The ASGI app. `runners` are the background loops the lifespan owns: its shutdown
    stops them (signal, then a bounded join) after the MCP session manager exits."""
    cfg = state.cfg
    server, registry = build_server(state)
    security = TransportSecuritySettings(enable_dns_rebinding_protection=True,
                                         allowed_hosts=list(cfg.allowed_hosts),
                                         allowed_origins=list(cfg.allowed_origins))
    # The SDK's own app builder is the only public way to create the session manager;
    # its routes are discarded because the gate has to sit inside RequireAuth, and the
    # RFC 9728 document has to list every scope, not just the required one.
    server.streamable_http_app(streamable_http_path=cfg.mcp_path, json_response=True, stateless_http=True,
                               transport_security=security, host="0.0.0.0")
    manager = server.session_manager
    resource_metadata_url = auth.rs_metadata_url(cfg)
    mcp_endpoint = RequireAuthMiddleware(
        StreamableHTTPASGIApp(manager),
        required_scopes=[Role.viewer.scope], resource_metadata_url=AnyHttpUrl(resource_metadata_url))

    async def healthz(_: Request) -> JSONResponse:
        return JSONResponse({"status": "ok", "version": __version__, "protocol": PROTOCOL_REVISION})

    async def alertmanager_webhook(request: Request) -> Response:
        """Alertmanager's receiver: bearer-authenticated, records monitor transitions."""
        path = cfg.alert_webhook_token_file
        if not path:
            return PlainTextResponse("Not Found", status_code=404)
        if not _webhook_authorized(request, path):
            return Response(status_code=401)
        try:
            declared = int((request.headers.get("content-length") or "").strip() or "0")
        except ValueError:
            declared = 0
        if declared > MAX_ALERT_BODY:
            return JSONResponse({"detail": "body too large"}, status_code=413)
        chunks: list[bytes] = []
        total = 0
        async for chunk in request.stream():
            total += len(chunk)
            if total > MAX_ALERT_BODY:
                return JSONResponse({"detail": "body too large"}, status_code=413)
            chunks.append(chunk)
        raw = b"".join(chunks)
        try:
            payload = json.loads(raw)
        except ValueError:
            return JSONResponse({"detail": "invalid JSON"}, status_code=400)
        if not isinstance(payload, dict) or not isinstance(payload.get("alerts"), list):
            return JSONResponse({"detail": "expected an object with an alerts list"}, status_code=400)
        log = alert_log_of(request.app.state.orbitd)
        recorded = 0
        for item in payload["alerts"]:
            transition = _webhook_transition(item)
            if transition is not None and log.record(*transition):
                recorded += 1
        return JSONResponse({"recorded": recorded})

    async def features_api(request: Request) -> JSONResponse:
        """GET /api/features: the features listing (what orbit_features_list returns) as
        plain JSON for a page that is not an MCP client. Read-only; any valid token."""
        scopes = getattr(getattr(request, "auth", None), "scopes", None) or []
        if Role.viewer.scope not in scopes:
            return JSONResponse({"error": "unauthorized"}, status_code=401,
                                headers={"WWW-Authenticate": "Bearer"})
        from .tools_viewer.features_list import listing
        body = await anyio.to_thread.run_sync(lambda: listing(state).model_dump(mode="json"))
        return JSONResponse(body, headers={"Cache-Control": "no-store"})

    def document(payload: dict) -> Route:  # type: ignore[type-arg]
        async def handle(_: Request) -> JSONResponse:
            return JSONResponse(payload, headers={"Cache-Control": "public, max-age=300"})
        return handle  # type: ignore[return-value]

    routes = [
        Route(cfg.mcp_path, endpoint=mcp_endpoint, methods=["POST", "GET", "DELETE"]),
        Route("/healthz", healthz, methods=["GET"]),
        Route("/v1/alertmanager", alertmanager_webhook, methods=["POST"]),
        Route("/api/features", features_api, methods=["GET"]),
        Route(auth.AS_METADATA_PATH, document(auth.as_metadata(cfg)), methods=["GET"]),
        Route(auth.JWKS_PATH, document(auth.jwks(state.key)), methods=["GET"]),
        Route(auth.TOKEN_PATH, auth.token_endpoint(state.tokens), methods=["POST"]),
    ] + [Route(p, document(auth.rs_metadata(cfg)), methods=["GET"]) for p in auth.rs_metadata_paths(cfg)]

    middleware = [
        Middleware(AuthenticationMiddleware,
                   backend=BearerAuthBackend(state.tokens, resource_server_url=AnyHttpUrl(cfg.mcp_url))),
        Middleware(AuthContextMiddleware),
    ]
    @contextlib.asynccontextmanager
    async def lifespan(_app: Starlette) -> AsyncIterator[None]:
        try:
            async with manager.run(), anyio.create_task_group() as tasks:
                tasks.start_soon(watch_features, state)
                yield
                tasks.cancel_scope.cancel()
        finally:
            await anyio.to_thread.run_sync(stop_runners, runners)

    app = Starlette(routes=routes, middleware=middleware, lifespan=lifespan)
    app.state.orbitd = state
    app.state.registry = registry
    app.state.runners = tuple(runners)
    return app


def app_from_env() -> Starlette:
    from .config import from_env
    return build_app(AppState.build(from_env()))


def serve(cfg: Config) -> None:
    import uvicorn

    from .reconcile import start_reconcile_runner
    from .shadow import start_daily_runner

    from .deploy import runtime_of

    configure_logging(sys.stdout)
    state = AppState.build(cfg)
    runtime_of(state)  # the one DeployRuntime, built before any runner or request
    runners: list[Any] = [start_daily_runner(state)]
    try:
        runners.append(start_reconcile_runner(state))
        # log_config=None: uvicorn's default logging configuration replaces the
        # handlers on its access and error loggers and switches their
        # propagation off, bypassing the sink installed above for the life of
        # the process. With no configuration the root handler stands and both
        # loggers propagate through it, so the access line is a record too.
        uvicorn.run(build_app(state, runners=runners), host="0.0.0.0", port=cfg.port, log_level="info",
                    log_config=None, proxy_headers=True, forwarded_allow_ips="*")
    finally:
        # The lifespan already stopped them on a clean shutdown; this covers a failed
        # startup, a bind error and any exception out of uvicorn. Idempotent.
        stop_runners(runners)
