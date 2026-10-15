"""The orbit CLI against the in-process server. Same ASGI transport as the other
tests; no socket, no real XDG directory."""

from __future__ import annotations

import io
import json
import stat
from types import SimpleNamespace

import httpx2
import pytest
from conftest import ISSUER, MCP_URL

from orbitd.client import EXIT_AUTH, EXIT_OK, EXIT_TOOL, EXIT_USAGE, async_main
from orbitd.roles import Role

pytestmark = pytest.mark.anyio

ADMIN_TOOLS = {"orbit_auth_issue", "orbit_auth_revoke", "orbit_auth_retire", "orbit_auth_list",
               "orbit_platform_diff"}
VIEWER_TOOLS = {
    "orbit_project_list",
    "orbit_catalog_get",
    "orbit_catalog_schema",
    "orbit_deploy_status",
    "orbit_deploy_history",
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
}
WRONG_SECRET = "this-secret-must-never-appear-on-stdio"


@pytest.fixture
def xdg(tmp_path, monkeypatch):
    d = tmp_path / "xdg"
    monkeypatch.setenv("XDG_CONFIG_HOME", str(d))
    return d


@pytest.fixture
def factory(app):
    def make(headers=None):
        return httpx2.AsyncClient(
            transport=httpx2.ASGITransport(app=app),
            base_url=ISSUER,
            headers=headers or {},
        )
    return make


async def run(argv, factory, stdin=None) -> int:
    stream = io.StringIO(stdin) if stdin is not None else None
    return await async_main(argv, stdin=stream, http_client_factory=factory)


def creds(client_id: str, secret: str) -> str:
    return json.dumps({"client_id": client_id, "client_secret": secret})


async def login_as(state, factory, role: Role, name: str) -> str:
    secret = state.store.add_principal(name, role, 3600)
    rc = await run(["login", "--url", MCP_URL], factory, stdin=creds(name, secret))
    assert rc == EXIT_OK, "login should succeed with the issued secret"
    return secret


def session_file(xdg):
    return xdg / "orbit" / "mcp.fixture.invalid.json"


def assert_secret_absent(capsys, *extras: str) -> tuple[str, str]:
    out, err = capsys.readouterr()
    blob = out + err
    for extra in extras:
        assert extra not in blob
    return out, err


async def test_login_stores_0600_and_prints_role_not_secret(state, factory, xdg, capsys):
    secret = await login_as(state, factory, Role.viewer, "ci-reader")
    path = session_file(xdg)
    assert path.is_file()
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    stored = json.loads(path.read_text())
    assert stored["url"] == MCP_URL
    assert stored["claimed_role"] == "viewer"
    assert stored["token"] and stored["claimed_expires_at"]
    out, err = assert_secret_absent(capsys, secret, stored["token"])
    assert "viewer" in out
    assert stored["claimed_expires_at"] in out
    assert "role:" in out


async def test_login_from_env_file(state, factory, xdg, tmp_path, monkeypatch, capsys):
    secret = state.store.add_principal("from-env", Role.operator, 3600)
    secret_path = tmp_path / "client.secret"
    secret_path.write_text(secret)
    monkeypatch.setenv("ORBIT_CLIENT_ID", "from-env")
    monkeypatch.setenv("ORBIT_CLIENT_SECRET_FILE", str(secret_path))
    rc = await run(["login", "--url", MCP_URL], factory)
    assert rc == EXIT_OK
    stored = json.loads(session_file(xdg).read_text())
    assert stored["claimed_role"] == "operator"
    assert_secret_absent(capsys, secret, stored["token"])


async def test_wrong_secret_exits_3_and_does_not_print_it(state, factory, xdg, capsys):
    real = state.store.add_principal("ci-reader", Role.viewer, 3600)
    rc = await run(["login", "--url", MCP_URL], factory, stdin=creds("ci-reader", WRONG_SECRET))
    assert rc == EXIT_AUTH
    out, err = assert_secret_absent(capsys, WRONG_SECRET, real)
    assert not session_file(xdg).exists()
    assert err  # some auth error on stderr


async def test_tools_for_viewer_omits_admin_tools(state, factory, xdg, capsys):
    secret = await login_as(state, factory, Role.viewer, "v")
    capsys.readouterr()
    rc = await run(["tools", "--url", MCP_URL], factory)
    assert rc == EXIT_OK
    out, err = assert_secret_absent(capsys, secret)
    lines = [ln for ln in out.splitlines() if ln.strip()]
    names = {ln.split()[0] for ln in lines}
    assert VIEWER_TOOLS <= names
    assert ADMIN_TOOLS.isdisjoint(names)
    assert {"orbit_deploy_ship", "orbit_deploy_rollback", "orbit_workload_restart"}.isdisjoint(names)
    stored = json.loads(session_file(xdg).read_text())
    assert stored["token"] not in out + err


async def test_tools_names_each_tools_arguments(state, factory, xdg, capsys):
    """QA of v1.0.0, F8: `orbit tools` gave a name and a sentence; a desk user could find
    a tool's argument names (`since`, `tags`, a monitor's body) only by trial and error.
    The listing names them, required first and optional in brackets, and `--json` carries
    each tool's own argument schema."""
    await login_as(state, factory, Role.viewer, "v")
    capsys.readouterr()
    assert await run(["tools", "--url", MCP_URL], factory) == EXIT_OK
    lines = capsys.readouterr().out.splitlines()
    at = next(i for i, ln in enumerate(lines) if ln.split()[:1] == ["orbit_deploy_wait"])
    block = lines[at:at + 3]
    assert any(ln.strip() == "arguments: project env [commit] [timeout_s]" for ln in block), block
    assert await run(["tools", "--url", MCP_URL, "--json"], factory) == EXIT_OK
    listed = {t["name"]: t for t in json.loads(capsys.readouterr().out)["tools"]}
    schema = listed["orbit_logs_query"]["input_schema"]
    assert schema["type"] == "object" and {"project", "env"} <= set(schema["properties"])
    assert set(schema["required"]) <= set(schema["properties"])


async def test_call_viewer_tool_returns_0_and_json(state, factory, xdg, capsys):
    secret = await login_as(state, factory, Role.viewer, "v")
    capsys.readouterr()
    rc = await run(["call", "orbit_platform_health", "--json"], factory)
    assert rc == EXIT_OK
    out, err = assert_secret_absent(capsys, secret)
    body = json.loads(out)
    assert "nodes" in body and "quotas" in body
    stored = json.loads(session_file(xdg).read_text())
    assert stored["token"] not in out + err


async def test_call_as_bare_tool_name(state, factory, xdg, capsys):
    """DESIGN-v2 §3.1: a tool name is a subcommand; no second code path."""
    secret = await login_as(state, factory, Role.viewer, "v")
    capsys.readouterr()
    rc = await run(["orbit_platform_health", "--json"], factory)
    assert rc == EXIT_OK
    out, err = assert_secret_absent(capsys, secret)
    assert "nodes" in json.loads(out)


async def test_call_admin_tool_with_viewer_exits_3_and_names_the_role(state, factory, xdg, capsys):
    secret = await login_as(state, factory, Role.viewer, "v")
    capsys.readouterr()
    rc = await run(["call", "orbit_auth_list", "--json"], factory)
    assert rc == EXIT_AUTH
    out, err = assert_secret_absent(capsys, secret)
    blob = (out + err).lower()
    assert "admin" in blob
    # The server names the role in the 403 body when the SDK client sends Mcp-Name.
    assert "required_role" in err or "admin" in err


async def test_call_yes_issues_principal_secret_once_in_json(state, factory, xdg, capsys):
    secret = await login_as(state, factory, Role.admin, "adm")
    capsys.readouterr()
    rc = await run(
        ["call", "orbit_auth_issue", "--yes", "--json",
         "--arg", "name=desk-viewer", "--arg", "role=viewer"],
        factory,
    )
    assert rc == EXIT_OK
    out, err = capsys.readouterr()
    body = json.loads(out)
    assert body["status"] == "applied"
    issued = body["evidence"]["client_secret"]
    assert out.count(issued) == 1
    assert issued not in err
    assert secret not in out + err
    assert state.store.principal("desk-viewer")["role"] == "viewer"
    stored = json.loads(session_file(xdg).read_text())
    assert stored["token"] not in out + err


async def test_call_no_yields_refused(state, factory, xdg, capsys):
    secret = await login_as(state, factory, Role.admin, "adm")
    capsys.readouterr()
    rc = await run(
        ["call", "orbit_auth_issue", "--no", "--json",
         "--arg", "name=desk-viewer", "--arg", "role=viewer"],
        factory,
    )
    assert rc == EXIT_TOOL
    out, err = capsys.readouterr()
    assert out == ""
    body = json.loads(err.splitlines()[-1])
    assert body["status"] == "refused"
    assert state.store.principal("desk-viewer") is None
    assert secret not in out + err
    assert "client_secret" not in out


async def test_call_issue_as_viewer_exits_3(state, factory, xdg, capsys):
    secret = await login_as(state, factory, Role.viewer, "v")
    capsys.readouterr()
    rc = await run(
        ["call", "orbit_auth_issue", "--yes", "--json",
         "--arg", "name=desk-viewer", "--arg", "role=viewer"],
        factory,
    )
    assert rc == EXIT_AUTH
    out, err = assert_secret_absent(capsys, secret)
    assert "admin" in (out + err).lower()


async def test_call_confirm_nontty_without_yes_or_no_exits_2(state, factory, xdg, capsys):
    secret = await login_as(state, factory, Role.admin, "adm")
    capsys.readouterr()
    rc = await run(
        ["call", "orbit_auth_issue", "--json",
         "--arg", "name=desk-viewer", "--arg", "role=viewer"],
        factory,
        stdin="",
    )
    assert rc == EXIT_USAGE
    out, err = capsys.readouterr()
    assert "--yes" in err and "--no" in err
    assert state.store.principal("desk-viewer") is None
    assert secret not in out + err


async def test_login_without_url_is_usage(factory, capsys):
    rc = await run(["login"], factory, stdin=creds("x", WRONG_SECRET))
    assert rc == EXIT_USAGE
    assert_secret_absent(capsys, WRONG_SECRET)


async def test_help_exits_0():
    assert await async_main(["--help"]) == 0
    assert await async_main(["call", "--help"]) == 0


async def test_help_reaches_the_operator_as_lines(capsys):
    """The desk's own help is not host text: it leaves by the raw stream, so its line
    breaks are line breaks and not the name of a byte. Every command's help, since
    the subparsers take the parent's class."""
    for argv in (["--help"], ["call", "--help"], ["secrets", "--help"]):
        capsys.readouterr()
        assert await async_main(argv) == 0
        out = capsys.readouterr().out
        assert out.count("\n") >= 3, argv
        assert "\\x0a" not in out and "\\n" not in out, argv
        assert out.startswith("usage: orbit"), argv


async def test_an_argument_error_quotes_what_was_typed_under_the_rendering(capsys):
    """argparse's error line repeats the offending argument, which is anyone's text:
    it stays under the rendering, so a terminal control or a line break typed into an
    option reaches the operator as the name of a byte, never as itself."""
    capsys.readouterr()
    assert await async_main(["call", "x", "--unknown=HOST\x1b[31m\nFAKE"]) != 0
    err = capsys.readouterr().err
    assert "unrecognized arguments" in err
    assert "\x1b" not in err and "\nFAKE" not in err
    assert "\\x1b" in err and "\\x0a" in err


async def test_call_error_payload_exits_nonzero_without_stdout(
        state, factory, xdg, capsys, monkeypatch):
    """A non-error result carrying an `error` payload is still a failure."""
    await login_as(state, factory, Role.viewer, "v")
    capsys.readouterr()

    async def fake_call(client, tool, arguments, yes, no, stdin):
        return SimpleNamespace(
            structured_content={"error": "insufficient_scope", "required_role": "admin"},
            is_error=False,
        )

    monkeypatch.setattr("orbitd.client._call_with_confirmation", fake_call)
    rc = await run(["call", "orbit_auth_list", "--json"], factory)
    assert rc == EXIT_TOOL
    out, err = capsys.readouterr()
    assert out == ""
    body = json.loads(err)
    assert body["error"] == "insufficient_scope"
    assert body["required_role"] == "admin"


async def test_an_argument_the_schema_refuses_arrives_as_the_envelope(state, factory, xdg, capsys, monkeypatch):
    """The protocol library refuses a bad argument before the tool runs, as an error
    result whose only content is a sentence and no structured document. The desk
    shapes it into the documented envelope (code invalid_arguments, a usage exit) in
    both forms; it printed `{}` and `failed: None` before (stranger session two)."""
    await login_as(state, factory, Role.viewer, "v")
    capsys.readouterr()
    text = ("Error executing tool orbit_deploy_status: 1 validation error for "
            "orbit_deploy_statusArguments\nenv\n  Input should be a valid string [type=string_type]")

    async def fake_call(client, tool, arguments, yes, no, stdin):
        return SimpleNamespace(structured_content=None, is_error=True,
                               content=[SimpleNamespace(type="text", text=text)])

    monkeypatch.setattr("orbitd.client._call_with_confirmation", fake_call)
    rc = await run(["call", "orbit_deploy_status", "--json", '{"project": "esc", "env": 123}'], factory)
    assert rc == EXIT_USAGE
    out, err = capsys.readouterr()
    assert out == ""
    body = json.loads(err)
    assert body["code"] == "invalid_arguments"
    assert "validation error" in body["what_failed"]
    assert body["next_action"].startswith("pass the arguments")
    assert body["retryable"] is False

    rc = await run(["call", "orbit_deploy_status", "--arg", "project=esc"], factory)
    assert rc == EXIT_USAGE
    out, err = capsys.readouterr()
    assert out == ""
    assert err.startswith("orbit: orbit_deploy_status refused (invalid_arguments): ")
    assert "; next: pass the arguments" in err
    assert "None" not in err and err.strip() != "{}"


async def test_an_error_result_with_no_document_and_no_validation_text_is_a_tool_error(
        state, factory, xdg, capsys, monkeypatch):
    await login_as(state, factory, Role.viewer, "v")
    capsys.readouterr()

    async def fake_call(client, tool, arguments, yes, no, stdin):
        return SimpleNamespace(structured_content=None, is_error=True,
                               content=[SimpleNamespace(type="text", text="Error executing tool x: boom")])

    monkeypatch.setattr("orbitd.client._call_with_confirmation", fake_call)
    rc = await run(["call", "orbit_platform_health", "--json"], factory)
    assert rc == EXIT_TOOL
    body = json.loads(capsys.readouterr()[1])
    assert body["code"] == "tool_error" and body["what_failed"].endswith("boom")


async def test_call_failed_status_exits_nonzero(state, factory, xdg, capsys, monkeypatch):
    """A non-error result with `status: failed` is still a failure."""
    await login_as(state, factory, Role.viewer, "v")
    capsys.readouterr()

    async def fake_call(client, tool, arguments, yes, no, stdin):
        return SimpleNamespace(
            structured_content={"status": "failed", "steps": []},
            is_error=False,
        )

    monkeypatch.setattr("orbitd.client._call_with_confirmation", fake_call)
    rc = await run(["call", "orbit_platform_health", "--json"], factory)
    assert rc == EXIT_TOOL
    out, err = capsys.readouterr()
    assert out == ""
    assert json.loads(err)["status"] == "failed"
