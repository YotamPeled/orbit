"""The role gate: one registry consulted before dispatch, reached over two transports.

A call above the caller's role is refused in the protocol with an isError result naming
the role the call needs and the role held; a name the registry does not declare is
refused the same way, naming no role and no scope, since no token could carry it."""


import pytest
from conftest import PROTOCOL_REVISION, mint, rpc, sdk_client
from mcp_types import HEADER_MISMATCH

from orbitd import server as server_module
from orbitd.roles import Listing, Mutation, Registry, Role, role_of_scopes
from orbitd.server import build_app

pytestmark = pytest.mark.anyio

ADMIN_TOOLS = {"orbit_auth_issue", "orbit_auth_revoke", "orbit_auth_list", "orbit_platform_diff",
               "orbit_features_set"}
OPERATOR_TOOLS = {"orbit_deploy_ship", "orbit_deploy_rollback", "orbit_workload_restart"}
VIEWER_TOOLS = {
    "orbit_project_list",
    "orbit_catalog_get",
    "orbit_catalog_schema",
    "orbit_deploy_status",
    "orbit_deploy_history",
    "orbit_deploy_wait",
    "orbit_workload_list",
    "orbit_workload_describe",
    "orbit_db_status",
    "orbit_job_status",
    "orbit_job_wait",
    "orbit_logs_query",
    "orbit_metrics_query",
    "orbit_traces_find",
    "orbit_traces_get",
    "orbit_alerts_active",
    "orbit_monitor_list",
    "orbit_monitor_status",
    "orbit_platform_health",
    "orbit_platform_budget",
    "orbit_size_recommend",
    "orbit_render_get",
    "orbit_ledger_get",
    "orbit_features_list",
}


def test_roles_nest():
    assert Role.viewer.scopes == ["orbit:viewer"]
    assert Role.operator.scopes == ["orbit:viewer", "orbit:operator"]
    assert Role.admin.scopes == ["orbit:viewer", "orbit:operator", "orbit:admin"]
    assert role_of_scopes(["orbit:operator", "orbit:viewer"]) is Role.operator
    assert role_of_scopes(["other"]) is None


async def test_viewer_list_hides_admin_tools_and_carries_cache_hints(state, http):
    async with sdk_client(http, mint(state, Role.viewer)) as client:
        listed = await client.list_tools()
    names = {t.name for t in listed.tools}
    assert VIEWER_TOOLS <= names
    assert ADMIN_TOOLS.isdisjoint(names)
    assert OPERATOR_TOOLS.isdisjoint(names)
    assert listed.ttl_ms == 60_000 and listed.cache_scope == "private"
    health = next(t for t in listed.tools if t.name == "orbit_platform_health")
    assert health.annotations.read_only_hint is True and health.annotations.open_world_hint is False
    assert health.output_schema is not None and health.input_schema["type"] == "object"


async def test_admin_list_shows_everything_with_annotations(state, http):
    async with sdk_client(http, mint(state, Role.admin)) as client:
        listed = await client.list_tools()
    by_name = {t.name: t for t in listed.tools}
    assert set(by_name) >= ADMIN_TOOLS | VIEWER_TOOLS | OPERATOR_TOOLS
    assert by_name["orbit_deploy_rollback"].annotations.destructive_hint is True
    assert by_name["orbit_workload_restart"].annotations.destructive_hint is True
    assert by_name["orbit_deploy_ship"].annotations.read_only_hint is False
    assert by_name["orbit_auth_revoke"].annotations.destructive_hint is True
    assert by_name["orbit_auth_issue"].annotations.destructive_hint is False
    assert by_name["orbit_auth_issue"].annotations.read_only_hint is False
    assert by_name["orbit_auth_list"].annotations.read_only_hint is True
    assert by_name["orbit_platform_diff"].annotations.read_only_hint is True
    assert by_name["orbit_platform_diff"].annotations.destructive_hint is False
    # The mutating envelope is what the tool advertises.
    assert set(by_name["orbit_auth_issue"].output_schema["properties"]) >= {"status", "steps", "commit", "evidence"}


# ------------------------------------------------- a refusal, now that it is in the protocol
# These cases used to assert an HTTP 403 and read the challenge header. There were two gates
# answering the same question, and the one a client library reaches answered with a transport
# error that ended the session -- so an agent was told nothing it could act on. The refusals
# now come back in the protocol, which is where the surface always built the documented
# envelope. The properties below are the ones those cases existed for; only the tier moved.


def refusal(response):
    """The failure envelope from an in-protocol refusal, with the two things every one of
    them must be: answered, and marked as an error."""
    assert response.status_code == 200, (
        "the refusal left before the protocol, so a client library gets a transport error "
        f"with nothing to act on: {response.status_code} {response.text[:200]}")
    result = response.json()["result"]
    assert result.get("isError"), (
        "the call was not refused at all: " + response.text[:300])
    structured = result.get("structuredContent")
    if isinstance(structured, dict) and "code" in structured:
        return structured
    return json.loads(result["content"][0]["text"])


async def test_viewer_calling_admin_tool_is_refused_naming_admin(state, http):
    """A caller whose role is too low is told which role would carry the call and which
    one they hold, so the next action is one they can actually take."""
    async with http(mint(state, Role.viewer)) as h:
        r = await rpc(h, "tools/call", {"name": "orbit_auth_list", "arguments": {}})
    body = refusal(r)
    assert body["code"] == "FORBIDDEN", body
    assert body["evidence"]["required_role"] == "admin"
    assert body["evidence"]["held_role"] == "viewer"
    assert "orbit:admin" in body["next_action"], body


async def test_operator_holds_viewer_but_not_admin(state, http):
    token = mint(state, Role.operator)
    async with http(token) as h:
        ok = await rpc(h, "tools/call", {"name": "orbit_platform_health", "arguments": {}})
        no = await rpc(h, "tools/call", {"name": "orbit_auth_list", "arguments": {}})
    assert ok.status_code == 200 and not ok.json()["result"].get("isError")
    assert refusal(no)["evidence"]["required_role"] == "admin"


async def test_viewer_calling_operator_tool_is_refused_naming_operator(state, http):
    async with http(mint(state, Role.viewer)) as h:
        r = await rpc(h, "tools/call", {"name": "orbit_deploy_ship", "arguments": {
            "project": "demo", "env": "prod", "components": ["web"], "tag": "t",
        }})
    body = refusal(r)
    assert body["evidence"]["required_role"] == "operator"
    assert body["evidence"]["held_role"] == "viewer"


async def test_header_naming_another_tool_cannot_bypass_the_gate(state, http):
    """Mcp-Name says the viewer tool, the body says the admin tool: the transport
    refuses the disagreement (-32020) before anything runs."""
    body = {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
            "params": {"name": "orbit_auth_list", "arguments": {},
                       "_meta": {"io.modelcontextprotocol/protocolVersion": PROTOCOL_REVISION,
                                 "io.modelcontextprotocol/clientCapabilities": {}}}}
    headers = {"MCP-Protocol-Version": PROTOCOL_REVISION, "Mcp-Method": "tools/call",
               "Mcp-Name": "orbit_platform_health", "Accept": "application/json, text/event-stream"}
    async with http(mint(state, Role.viewer)) as h:
        r = await h.post("/mcp", json=body, headers=headers)
    assert r.status_code == 400
    assert r.json()["error"]["code"] == HEADER_MISMATCH


async def test_handler_gate_names_the_role_when_no_routing_headers_exist(state, http):
    """A handshake-era request has no Mcp-Name header for the HTTP gate to read; the
    handler-tier gate refuses it with an actionable isError naming the role."""
    body = {"jsonrpc": "2.0", "id": 7, "method": "tools/call",
            "params": {"name": "orbit_auth_list", "arguments": {}}}
    async with http(mint(state, Role.viewer)) as h:
        r = await h.post("/mcp", json=body, headers={"Accept": "application/json, text/event-stream",
                                                     "MCP-Protocol-Version": "2025-06-18"})
    assert r.status_code == 200, r.text
    result = r.json()["result"]
    assert result["isError"] is True
    failure = result["structuredContent"]
    assert failure["code"] == "FORBIDDEN" and failure["evidence"]["required_role"] == "admin"
    assert "orbit:admin" in failure["next_action"]


@pytest.mark.parametrize("role", list(Role))
async def test_unknown_tool_name_is_refused_naming_no_role_or_scope_for_every_role(state, http, role):
    """Default deny without the lie: a name the registry does not declare is refused
    before dispatch for every role, and the refusal names no role and no scope — no
    token could carry the call through, so none is pointed at. It says the tool is
    not offered and points at tools/list instead."""
    async with http(mint(state, role)) as h:
        r = await rpc(h, "tools/call", {"name": "orbit_nope", "arguments": {}})
    body = refusal(r)
    assert body["code"] == "UNKNOWN_TOOL", body
    assert "required_role" not in body.get("evidence", {}), body
    assert "orbit:" not in r.text, (
        "a scope was named for a call no token could carry: " + r.text[:300])
    assert "not offered" in body["what_failed"] and "tools/list" in body["next_action"]
    assert "www-authenticate" not in r.headers, (
        "the surface still sends a client off to get authorized for a name no "
        "authorization makes callable")


SDK_ONLY_TOOL = "orbit_unclassified"


@pytest.fixture
async def app(fake_backends, monkeypatch):
    """The real app plus one tool registered through the SDK only (`MCPServer.add_tool`),
    with no registry declaration: exactly the reviewer's probe."""
    real = server_module.build_server

    def with_undeclared_tool(st, registry=None):
        srv, reg = real(st, registry)

        def orbit_unclassified() -> str:
            return "UNCLASSIFIED_EXECUTED"

        srv.add_tool(orbit_unclassified, name=SDK_ONLY_TOOL, description="registered without a role")
        assert reg.get(SDK_ONLY_TOOL) is None
        return srv, reg

    monkeypatch.setattr(server_module, "build_server", with_undeclared_tool)
    application = build_app(fake_backends)
    async with application.router.lifespan_context(application):
        yield application


@pytest.mark.parametrize("role", list(Role))
async def test_sdk_only_tool_is_refused_on_the_routing_header_path_for_every_role(state, http, role):
    async with http(mint(state, role)) as h:
        r = await rpc(h, "tools/call", {"name": SDK_ONLY_TOOL, "arguments": {}})
    body = refusal(r)
    assert body["code"] == "UNKNOWN_TOOL", body
    assert "required_role" not in body.get("evidence", {}), body
    assert "orbit:" not in r.text, r.text[:300]
    assert "UNCLASSIFIED_EXECUTED" not in r.text, (
        "a tool the registry never declared ran anyway")


@pytest.mark.parametrize("role", list(Role))
async def test_sdk_only_tool_is_refused_at_the_handler_tier_for_every_role(state, http, role):
    """No Mcp-Name header, so only the handler-tier gate stands between the body and
    dispatch; it refuses with the not-offered isError shape — no role, no scope —
    and the tool never runs."""
    body = {"jsonrpc": "2.0", "id": 9, "method": "tools/call", "params": {"name": SDK_ONLY_TOOL, "arguments": {}}}
    async with http(mint(state, role)) as h:
        r = await h.post("/mcp", json=body, headers={"Accept": "application/json, text/event-stream",
                                                     "MCP-Protocol-Version": "2025-06-18"})
    assert r.status_code == 200, r.text
    result = r.json()["result"]
    assert result["isError"] is True
    failure = result["structuredContent"]
    assert failure["code"] == "UNKNOWN_TOOL"
    assert "required_role" not in failure["evidence"] and "required_scope" not in failure["evidence"]
    assert "orbit:" not in r.text and "tools/list" in failure["next_action"]
    assert "UNCLASSIFIED_EXECUTED" not in r.text


@pytest.mark.parametrize("role", list(Role))
async def test_sdk_only_tool_is_hidden_from_list_and_refused_through_the_sdk_client(state, http, role):
    """This case used to require the client library to raise, which is what a transport
    error does: it pinned the behaviour that cost an agent its session. The tool is still
    hidden and still refused; what changed is that the caller is left able to act on it."""
    async with sdk_client(http, mint(state, role)) as client:
        listed = await client.list_tools()
        result = await client.call_tool(SDK_ONLY_TOOL, {})
        still_answers = await client.list_tools()
    assert SDK_ONLY_TOOL not in {t.name for t in listed.tools}
    assert result.is_error, "a tool the registry never declared was not refused"
    assert "UNCLASSIFIED_EXECUTED" not in str(result.content), (
        "a tool the registry never declared ran anyway")
    assert still_answers.tools, "the refusal ended the session"


async def test_declared_tools_still_work_beside_the_sdk_only_one(state, http):
    async with http(mint(state, Role.viewer)) as h:
        ok = await rpc(h, "tools/call", {"name": "orbit_platform_health", "arguments": {}})
    assert ok.status_code == 200 and not ok.json()["result"].get("isError")


def test_registry_refuses_what_the_design_forbids():
    reg = Registry()
    with pytest.raises(ValueError, match="must match"):
        @reg.tool(role=Role.viewer, read_only=True)
        def not_prefixed() -> Listing:
            """x"""
    with pytest.raises(TypeError, match="Mutation envelope"):
        @reg.tool(role=Role.operator, read_only=False)
        def orbit_thing_do() -> dict:
            """x"""
    with pytest.raises(ValueError, match="no description"):
        @reg.tool(role=Role.viewer, read_only=True)
        def orbit_thing_get() -> Listing:
            pass
    with pytest.raises(ValueError, match="read-only and destructive"):
        @reg.tool(role=Role.viewer, read_only=True, destructive=True)
        def orbit_thing_list() -> Listing:
            """x"""

    @reg.tool(role=Role.operator, read_only=False, destructive=True)
    def orbit_thing_drop() -> Mutation:
        """x"""
        return Mutation(status="applied")

    assert reg.get("orbit_thing_drop").role is Role.operator
    assert reg.get("orbit_thing_drop").annotations.destructive_hint is True
