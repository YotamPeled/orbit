"""MCP tools follow the features (docs/V1.md M2c): a tool owned by a feature that is off is
not listed and cannot be called, for every role and every feature the registry declares.

Table-driven over the registry the template ships, so a feature added there is covered
here without a new test."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest
import yaml

from orbitd import orbit_features as rules
from orbitd.features import TTL_S, Features, Snapshot, enable_hint
from orbitd.roles import Role

from conftest import mint, rpc

pytestmark = pytest.mark.anyio

REPO = Path(__file__).resolve().parents[3]
REGISTRY_FILE = REPO / "template" / "provisioning" / "features.yaml"
REGISTRY = rules.load_registry(REPO / "template")
OPTIONAL = [name for name, spec in REGISTRY.items() if not spec["always"]]
WITH_TOOLS = [name for name in OPTIONAL if REGISTRY[name]["tools"]]
BUCKET = {"objectStorage": {"endpoint": "https://s3.fixture.invalid", "buckets": {"platform": "fixture"}}}


def dependents(name: str) -> set:
    """Every optional feature that cannot be on without `name`, directly or through another."""
    found, grew = set(), True
    while grew:
        grew = False
        for other, spec in REGISTRY.items():
            if other not in found and other != name and (set(spec["requires"]) & (found | {name})):
                found.add(other)
                grew = True
    return found


def without(name: str) -> dict:
    """Every optional feature on, except `name` and whatever needs it."""
    off = dependents(name) | {name}
    return {other: other not in off for other in OPTIONAL}


def write_features(state, block, *, registry: bool = True) -> None:
    """Give the fixture's instance checkout a registry and a `features:` block (None: no
    block at all), and make orbitd read it again."""
    root = Path(state.backends.instance.root)
    (root / "provisioning").mkdir(exist_ok=True)
    if registry:
        shutil.copy(REGISTRY_FILE, root / "provisioning" / "features.yaml")
    else:
        (root / "provisioning" / "features.yaml").unlink(missing_ok=True)
    config = dict(BUCKET)
    if block is not None:
        config["features"] = block
    (root / "instance").mkdir(exist_ok=True)
    (root / "instance" / "config.yaml").write_text(yaml.safe_dump(config))
    state.features.snapshot(fresh=True)


async def listed(http, token: str) -> set[str]:
    async with http(token) as h:
        r = await rpc(h, "tools/list")
    assert r.status_code == 200, r.text
    return {t["name"] for t in r.json()["result"]["tools"]}


def test_the_package_copy_of_the_rules_is_the_scripts_copy():
    """orbitd imports its own copy (an installed package cannot reach scripts/); the
    scaffolder and the verify stages read scripts/verify's. One reading, two places."""
    assert ((REPO / "services" / "orbitd" / "orbitd" / "orbit_features.py").read_bytes()
            == (REPO / "scripts" / "verify" / "orbit_features.py").read_bytes())


def test_the_registry_has_a_feature_that_owns_tools():
    assert WITH_TOOLS, "no optional feature owns a tool, so nothing below tests anything"


@pytest.mark.parametrize("role", list(Role))
@pytest.mark.parametrize("feature", WITH_TOOLS)
async def test_tools_list_differs_by_exactly_the_features_tools(fake_backends, http, role, feature):
    state = fake_backends
    token = mint(state, role)
    write_features(state, {name: True for name in OPTIONAL})
    everything = await listed(http, token)
    write_features(state, without(feature))
    reduced = await listed(http, token)
    owned = {tool for name in dependents(feature) | {feature} for tool in REGISTRY[name]["tools"]}
    assert everything - reduced == owned & everything, (
        f"turning {feature} off must remove exactly the tools it (and what needs it) owns that a {role.value} may call")
    assert reduced - everything == set()
    assert owned & everything, f"a {role.value} sees none of {feature}'s tools even with it on"


@pytest.mark.parametrize("feature", WITH_TOOLS)
async def test_a_call_to_a_tool_whose_feature_is_off_says_so_and_how_to_turn_it_on(fake_backends, http, feature):
    state = fake_backends
    write_features(state, without(feature))
    for tool in REGISTRY[feature]["tools"]:
        async with http(mint(state, Role.admin)) as h:
            r = await rpc(h, "tools/call", {"name": tool, "arguments": {}})
        assert r.status_code == 200, r.text
        result = r.json()["result"]
        assert result["isError"] is True
        body = result["structuredContent"]
        assert body["code"] == "FEATURE_OFF", body
        assert body["evidence"]["feature"] == feature
        assert body["next_action"] == enable_hint(feature)
        assert f"orbit features enable {feature}" in body["next_action"]
        assert "Traceback" not in r.text


async def test_the_same_refusal_reaches_a_handshake_era_client(fake_backends, http):
    """A 2025-06-18 request carries no per-request envelope; the gate answers it the same."""
    state = fake_backends
    feature = WITH_TOOLS[0]
    tool = REGISTRY[feature]["tools"][0]
    write_features(state, without(feature))
    headers = {"Accept": "application/json, text/event-stream", "MCP-Protocol-Version": "2025-06-18"}
    async with http(mint(state, Role.admin)) as h:
        call = await h.post("/mcp", headers=headers, json={
            "jsonrpc": "2.0", "id": 7, "method": "tools/call", "params": {"name": tool, "arguments": {}}})
        lst = await h.post("/mcp", headers=headers, json={"jsonrpc": "2.0", "id": 8, "method": "tools/list"})
    assert call.json()["result"]["structuredContent"]["code"] == "FEATURE_OFF", call.text
    assert tool not in {t["name"] for t in lst.json()["result"]["tools"]}


async def test_the_role_is_checked_before_the_feature(fake_backends, http):
    """A token that could not call the tool anyway is told about its role, and learns
    nothing about which features are on."""
    state = fake_backends
    registry_roles = {"orbit_auth_list": "admin"}
    write_features(state, {})
    async with http(mint(state, Role.viewer)) as h:
        r = await rpc(h, "tools/call", {"name": "orbit_auth_list", "arguments": {}})
    assert r.json()["result"]["structuredContent"]["code"] == "FORBIDDEN", registry_roles


async def test_a_checkout_without_a_registry_offers_every_tool(fake_backends, http):
    state = fake_backends
    token = mint(state, Role.admin)
    write_features(state, {name: True for name in OPTIONAL})
    everything = await listed(http, token)
    write_features(state, {}, registry=False)
    assert await listed(http, token) == everything
    assert state.features.snapshot().problem is None


async def test_a_features_block_that_cannot_be_read_keeps_the_last_good_reading(fake_backends, http):
    state = fake_backends
    token = mint(state, Role.admin)
    feature = WITH_TOOLS[0]
    write_features(state, without(feature))
    before = await listed(http, token)
    write_features(state, {"no-such-feature": True})
    snap = state.features.snapshot()
    assert snap.problem and "no-such-feature" in snap.problem
    assert await listed(http, token) == before, "an unreadable block must not change what is offered"


def test_a_reading_is_reused_for_its_ttl_and_then_taken_again(tmp_path):
    from orbitd.backends import InstanceRepo
    root = tmp_path / "instance"
    (root / "provisioning").mkdir(parents=True)
    (root / "instance").mkdir()
    shutil.copy(REGISTRY_FILE, root / "provisioning" / "features.yaml")
    feature = WITH_TOOLS[0]
    tool = REGISTRY[feature]["tools"][0]
    now = [100.0]
    features = Features(InstanceRepo(str(root)), clock=lambda: now[0])
    (root / "instance" / "config.yaml").write_text(yaml.safe_dump({**BUCKET, "features": {feature: True}}))
    assert features.off_owner(tool) is None
    (root / "instance" / "config.yaml").write_text(yaml.safe_dump({**BUCKET, "features": {}}))
    now[0] += TTL_S / 2
    assert features.off_owner(tool) is None, "inside the TTL the earlier reading stands"
    now[0] += TTL_S
    assert features.off_owner(tool) == feature
    assert tool in features.snapshot().hidden_tools()


def test_a_snapshot_with_no_registry_gates_nothing():
    assert Snapshot().off_owner("orbit_traces_find") is None
    assert Snapshot().hidden_tools() == frozenset()
