"""The admin tools and their confirmation step. Refused means nothing changed on disk;
accepted means the store or the revocation file changed and the change is observable
through the auth path itself (a revoked token gets 401, an issued secret mints)."""

import json

import pytest
from conftest import MCP_URL, ISSUER, http as _http, mint, rpc, sdk_client
from mcp.shared.exceptions import MCPError
from mcp_types import ElicitResult, InputRequiredResult

from orbitd import cli
from orbitd.auth import Store
from orbitd.roles import Role

pytestmark = pytest.mark.anyio


async def test_issue_declined_changes_nothing(state, http):
    async with sdk_client(http, mint(state, Role.admin), confirm=None) as client:
        r = await client.call_tool("orbit_auth_issue", {"name": "ci", "role": "operator"})
    assert not r.is_error
    assert r.structured_content["status"] == "refused"
    assert state.store.principal("ci") is None


async def test_issue_answered_false_is_refused(state, http):
    async with sdk_client(http, mint(state, Role.admin), confirm=False) as client:
        r = await client.call_tool("orbit_auth_issue", {"name": "ci", "role": "operator"})
    assert r.structured_content["status"] == "refused"
    assert state.store.principal("ci") is None


async def test_issue_confirmed_registers_a_principal_whose_secret_mints(state, http):
    async with sdk_client(http, mint(state, Role.admin), confirm=True) as client:
        r = await client.call_tool("orbit_auth_issue", {"name": "ci", "role": "operator", "ttl": 7200})
    assert not r.is_error, r.content
    body = r.structured_content
    assert body["status"] == "applied"
    assert [s["name"] for s in body["steps"]] == ["confirm", "store"]
    secret = body["evidence"]["client_secret"]
    assert state.store.principal("ci")["role"] == "operator"
    assert "client_secret" not in json.dumps(state.store.load())      # only the hash is kept
    async with http() as h:
        tok = await h.post("/token", data={"grant_type": "client_credentials"}, auth=("ci", secret))
    assert tok.status_code == 200 and tok.json()["scope"] == "orbit:viewer orbit:operator"
    assert tok.json()["expires_in"] == 7200
    # Second issue of the same name is a failed mutation, not a crash.
    async with sdk_client(http, mint(state, Role.admin), confirm=True) as client:
        again = await client.call_tool("orbit_auth_issue", {"name": "ci", "role": "viewer"})
    assert again.structured_content["status"] == "failed"


async def test_issue_rejects_a_bad_role_or_name_before_asking(state, http):
    async with sdk_client(http, mint(state, Role.admin), confirm=True) as client:
        bad_role = await client.call_tool("orbit_auth_issue", {"name": "ci", "role": "god"})
        bad_name = await client.call_tool("orbit_auth_issue", {"name": "Not Valid", "role": "viewer"})
    assert bad_role.is_error and bad_name.is_error
    assert state.store.load()["principals"] == {}


async def test_revoke_confirmed_makes_the_token_401_and_list_shows_it(state, http):
    victim, claims = state.tokens.mint("victim", Role.viewer)
    async with http(victim) as h:
        assert (await rpc(h, "tools/list")).status_code == 200
    async with sdk_client(http, mint(state, Role.admin, "adm"), confirm=True) as client:
        r = await client.call_tool("orbit_auth_revoke", {"jti": claims["jti"]})
        listing = await client.call_tool("orbit_auth_list", {})
    assert r.structured_content["status"] == "applied"
    assert r.structured_content["evidence"] == {"jti": claims["jti"], "known_token": True, "sub": "victim"}
    async with http(victim) as h:
        assert (await rpc(h, "tools/list")).status_code == 401
    tokens = {t["jti"]: t for t in listing.structured_content["tokens"]["items"]}
    assert tokens[claims["jti"]]["revoked"] is True and tokens[claims["jti"]]["sub"] == "victim"
    assert listing.structured_content["tokens"]["next_cursor"] is None
    assert listing.structured_content["principals"]["items"] == []


async def test_revoke_declined_leaves_the_token_valid(state, http):
    victim, claims = state.tokens.mint("victim", Role.viewer)
    async with sdk_client(http, mint(state, Role.admin, "adm"), confirm=None) as client:
        r = await client.call_tool("orbit_auth_revoke", {"jti": claims["jti"]})
    assert r.structured_content["status"] == "refused"
    async with http(victim) as h:
        assert (await rpc(h, "tools/list")).status_code == 200


async def test_first_round_is_an_input_required_result_with_one_elicitation(state, http):
    async with sdk_client(http, mint(state, Role.admin)) as client:
        first = await client.session.call_tool("orbit_auth_revoke", {"jti": "abcdefghij"}, allow_input_required=True)
    assert isinstance(first, InputRequiredResult)
    assert list(first.input_requests) == ["confirm"]
    req = first.input_requests["confirm"]
    assert req.method == "elicitation/create" and "abcdefghij" in req.params.message
    assert req.params.requested_schema["required"] == ["confirm"]
    assert first.request_state           # sealed by the SDK; opaque to the client


async def test_retry_with_an_answer_but_no_state_is_refused(state, http):
    yes = {"confirm": ElicitResult(action="accept", content={"confirm": True})}
    async with sdk_client(http, mint(state, Role.admin)) as client:
        r = await client.session.call_tool("orbit_auth_revoke", {"jti": "abcdefghij"}, input_responses=yes)
    assert r.structured_content["status"] == "refused"
    assert "abcdefghij" not in state.revocations.read()


async def test_state_is_bound_to_the_arguments_and_the_principal(state, http):
    """The confirmation for one jti cannot apply to another, and a second admin
    cannot spend a confirmation minted for the first."""
    yes = {"confirm": ElicitResult(action="accept", content={"confirm": True})}
    async with sdk_client(http, mint(state, Role.admin, "first")) as client:
        first = await client.session.call_tool("orbit_auth_revoke", {"jti": "abcdefghij"}, allow_input_required=True)
        with pytest.raises(MCPError):
            await client.session.call_tool("orbit_auth_revoke", {"jti": "zzzzzzzzzz"},
                                           input_responses=yes, request_state=first.request_state)
    async with sdk_client(http, mint(state, Role.admin, "second")) as client:
        with pytest.raises(MCPError):
            await client.session.call_tool("orbit_auth_revoke", {"jti": "abcdefghij"},
                                           input_responses=yes, request_state=first.request_state)
    assert state.revocations.read() == set()


def state_of(cfg):
    from orbitd.state import AppState
    return AppState.build(cfg)


async def test_bootstrap_refuses_when_any_admin_exists_even_under_a_new_name(cfg, state, monkeypatch, capsys, tmp_path):
    """An admin issued through the tool (not by bootstrap) still makes bootstrap refuse:
    the check is 'any admin in the store', not 'this --name in the store'."""
    state.store.add_principal("issued-admin", Role.admin, cfg.token_ttl)
    env = {"ORBITD_MCP_URL": MCP_URL, "ORBITD_SIGNING_KEY": cfg.signing_key_path,
           "ORBITD_PRINCIPALS": cfg.principals_path, "ORBITD_REVOCATIONS": cfg.revocations_path}
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    assert cli.main(["bootstrap"]) == 2
    assert cli.main(["bootstrap", "--name", "another"]) == 2
    err = capsys.readouterr().err
    assert "already done" in err and "issued-admin" in err
    assert capsys.readouterr().out == ""
    assert state.store.admins() == ["issued-admin"]
    # A non-admin principal alone does not count as bootstrap having happened.
    Store(cfg.principals_path).save({"principals": {}, "tokens": {}})
    state.store.add_principal("ci", Role.operator, cfg.token_ttl)
    assert cli.main(["bootstrap"]) == 0
    assert state.store.admins() == ["admin"]


async def test_second_admin_comes_only_from_issue_with_confirmation(state, http):
    async with sdk_client(http, mint(state, Role.admin, "root"), confirm=None) as client:
        declined = await client.call_tool("orbit_auth_issue", {"name": "second-admin", "role": "admin"})
    assert declined.structured_content["status"] == "refused" and state.store.admins() == []
    async with sdk_client(http, mint(state, Role.admin, "root"), confirm=True) as client:
        r = await client.call_tool("orbit_auth_issue", {"name": "second-admin", "role": "admin"})
    assert r.structured_content["status"] == "applied"
    assert state.store.admins() == ["second-admin"]
    async with http() as h:
        tok = await h.post("/token", data={"grant_type": "client_credentials"},
                           auth=("second-admin", r.structured_content["evidence"]["client_secret"]))
    assert tok.status_code == 200 and "orbit:admin" in tok.json()["scope"]


async def test_bootstrap_prints_one_admin_credential_once(cfg, http, monkeypatch, capsys, tmp_path):
    key = tmp_path / "fresh.pem"
    env = {"ORBITD_MCP_URL": MCP_URL, "ORBITD_SIGNING_KEY": str(key), "ORBITD_PRINCIPALS": cfg.principals_path,
           "ORBITD_REVOCATIONS": cfg.revocations_path}
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    assert cli.main(["bootstrap"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["client_id"] == "admin" and out["role"] == "admin" and out["token_endpoint"] == ISSUER + "/token"
    assert key.exists() and (key.stat().st_mode & 0o777) == 0o600
    assert cli.main(["bootstrap"]) == 2                       # once means once
    assert cli.main(["bootstrap", "--name", "second-admin"]) == 2   # regardless of --name
    assert "orbit_auth_issue" in capsys.readouterr().err
    assert state_of(cfg).store.admins() == ["admin"]
    # The principal store now holds an admin whose secret mints a token that reaches an admin tool.
    async with http() as h:
        tok = await h.post("/token", data={"grant_type": "client_credentials"}, auth=("admin", out["client_secret"]))
    assert tok.status_code == 200
    async with sdk_client(http, tok.json()["access_token"]) as client:
        r = await client.call_tool("orbit_auth_list", {})
    assert [p["name"] for p in r.structured_content["principals"]["items"]] == ["admin"]
