"""Tokens and the two endpoints that mint and describe them. Every 401 here is one the
bearer middleware must produce from the verifier saying no; a test that passed with the
verifier returning a token for anything would be the bug this file exists to catch."""

import time

import jwt
import pytest
from conftest import ISSUER, MCP_URL, mint, rpc, sdk_client
from mcp.shared.auth import ProtectedResourceMetadata

from orbitd import auth
from orbitd.roles import ALL_SCOPES, Role

pytestmark = pytest.mark.anyio


async def test_no_token_is_401_with_a_resource_metadata_challenge(http):
    async with http() as h:
        r = await rpc(h, "tools/list")
    assert r.status_code == 401
    www = r.headers["www-authenticate"]
    assert www.startswith("Bearer ")
    assert f'resource_metadata="{ISSUER}/.well-known/oauth-protected-resource/mcp"' in www


async def test_client_credentials_issue_then_viewer_call_succeeds(state, http):
    secret = state.store.add_principal("ci-reader", Role.viewer, 3600)
    async with http() as h:
        r = await h.post("/token", data={"grant_type": "client_credentials"}, auth=("ci-reader", secret))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["token_type"] == "Bearer"
    assert body["scope"] == "orbit:viewer"
    assert body["expires_in"] == 3600
    assert r.headers["cache-control"] == "no-store"
    claims = jwt.decode(body["access_token"], options={"verify_signature": False})
    assert claims["aud"] == MCP_URL and claims["sub"] == "ci-reader" and claims["iss"] == ISSUER
    assert claims["exp"] - claims["iat"] == 3600 and claims["jti"]
    async with sdk_client(http, body["access_token"]) as client:
        result = await client.call_tool("orbit_platform_health", {})
    assert not result.is_error and "nodes" in result.structured_content
    # The ledger knows the token, so orbit_auth_list can name it for revocation.
    assert claims["jti"] in state.store.load()["tokens"]


async def test_client_secret_post_is_accepted_too(state, http):
    secret = state.store.add_principal("poster", Role.operator, 600)
    async with http() as h:
        r = await h.post("/token", data={"grant_type": "client_credentials", "client_id": "poster",
                                         "client_secret": secret, "scope": "orbit:viewer"})
    assert r.status_code == 200
    assert r.json()["scope"] == "orbit:viewer"       # narrowed on request


@pytest.mark.parametrize("case,expect", [
    ("bad-secret", (401, "invalid_client")),
    ("unknown-client", (401, "invalid_client")),
    ("no-auth", (401, "invalid_client")),
    ("wrong-grant", (400, "unsupported_grant_type")),
    ("escalate-scope", (400, "invalid_scope")),
    ("wrong-resource", (400, "invalid_target")),
    ("json-body", (400, "invalid_request")),
])
async def test_token_endpoint_refusals(state, http, case, expect):
    secret = state.store.add_principal("p", Role.viewer, 600)
    form = {"grant_type": "client_credentials"}
    basic = ("p", secret)
    if case == "bad-secret":
        basic = ("p", "nope")
    if case == "unknown-client":
        basic = ("q", secret)
    if case == "wrong-grant":
        form["grant_type"] = "authorization_code"
    if case == "escalate-scope":
        form["scope"] = "orbit:admin"
    if case == "wrong-resource":
        form["resource"] = "https://other.invalid/mcp"
    async with http() as h:
        if case == "no-auth":
            r = await h.post("/token", data=form)
        elif case == "json-body":
            r = await h.post("/token", json=form, auth=basic)
        else:
            r = await h.post("/token", data=form, auth=basic)
    assert (r.status_code, r.json()["error"]) == expect, r.text
    if r.status_code == 401:
        assert r.headers["www-authenticate"] == "Basic"


async def test_expired_token_is_401(state, http):
    token = mint(state, Role.admin, exp=int(time.time()) - auth.CLOCK_LEEWAY - 5)
    async with http(token) as h:
        r = await rpc(h, "tools/list")
    assert r.status_code == 401


async def test_revoked_token_is_401_without_a_restart(state, http):
    token, claims = state.tokens.mint("someone", Role.admin)
    async with http(token) as h:
        assert (await rpc(h, "tools/list")).status_code == 200
        state.revocations.add(claims["jti"])
        r = await rpc(h, "tools/list")
    assert r.status_code == 401


async def test_wrong_audience_is_401(state, http):
    token = mint(state, Role.admin, aud="https://other.invalid/mcp")
    async with http(token) as h:
        assert (await rpc(h, "tools/list")).status_code == 401


async def test_wrong_issuer_is_401(state, http):
    token = mint(state, Role.admin, iss="https://other.invalid")
    async with http(token) as h:
        assert (await rpc(h, "tools/list")).status_code == 401


async def test_token_signed_by_another_key_is_401(state, http, tmp_path):
    other = tmp_path / "other.pem"
    auth.generate_key(str(other))
    claims = {"iss": ISSUER, "aud": MCP_URL, "sub": "x", "iat": int(time.time()),
              "exp": int(time.time()) + 600, "jti": "abcdefgh", "scope": "orbit:admin"}
    token = jwt.encode(claims, auth.SigningKey(str(other)).pem, algorithm="EdDSA")
    async with http(token) as h:
        assert (await rpc(h, "tools/list")).status_code == 401


async def test_token_without_an_orbit_scope_is_401(state, http):
    token = mint(state, Role.admin, scopes=["something:else"])
    async with http(token) as h:
        assert (await rpc(h, "tools/list")).status_code == 401


async def test_discovery_documents_are_valid(state, http):
    async with http() as h:
        root = await h.get("/.well-known/oauth-protected-resource")
        path_form = await h.get("/.well-known/oauth-protected-resource/mcp")
        as_doc = await h.get("/.well-known/oauth-authorization-server")
        jwks = await h.get("/.well-known/jwks.json")
        healthz = await h.get("/healthz")
    assert root.status_code == path_form.status_code == as_doc.status_code == jwks.status_code == 200
    assert root.json() == path_form.json()
    rs = ProtectedResourceMetadata.model_validate(root.json())     # the SDK's RFC 9728 model
    assert str(rs.resource) == MCP_URL
    assert [str(u) for u in rs.authorization_servers] == [ISSUER]
    assert rs.scopes_supported == ALL_SCOPES
    doc = as_doc.json()
    assert doc["issuer"] == ISSUER
    assert doc["token_endpoint"] == ISSUER + "/token"
    assert doc["grant_types_supported"] == ["client_credentials"]
    assert "authorization_endpoint" not in doc
    assert set(doc["token_endpoint_auth_methods_supported"]) == {"client_secret_basic", "client_secret_post"}
    key = jwks.json()["keys"][0]
    assert (key["kty"], key["crv"], key["alg"]) == ("OKP", "Ed25519", "EdDSA")
    # The published key verifies what the server signs.
    token = mint(state, Role.viewer)
    public = jwt.PyJWK(key).key
    assert jwt.decode(token, public, algorithms=["EdDSA"], audience=MCP_URL)["sub"] == "someone"
    assert healthz.json()["protocol"] == "2026-07-28"
