"""orbit_catalog_schema describes a catalog ENTRY (QA of v1.0.0, F7).

It returned the chart's values schema alone: project, env, hostnames... the values the
generator renders, where an author writes id, environments, components. The tool now
carries the entry's own keys, and this holds them to the generator's: a key the
generator learns or drops must be learned or dropped here in the same change."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

from orbitd.roles import Role
from orbitd.tools_viewer import catalog_schema

from conftest import mint, sdk_client

REPO = Path(__file__).resolve().parents[3]


def generator():
    spec = importlib.util.spec_from_file_location("catalog_sync_for_schema", REPO / "scripts" / "catalog-sync.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_entry_shape_is_the_generators_own_key_sets():
    gen = generator()
    assert catalog_schema.ENTRY_KEYS == gen.PROJECT_KEYS
    assert catalog_schema.ENVIRONMENT_KEYS == gen.ENV_KEYS
    assert catalog_schema.COMPONENT_OVERRIDE_KEYS == gen.COMPONENT_OVERRIDE_KEYS
    assert catalog_schema.GENERATOR_COMPONENT_KEYS == gen.GENERATOR_COMPONENT_KEYS
    # and what the sentences say about them names every one of those keys
    overrides = catalog_schema.ENTRY_SHAPE["environments"]["<env>"]["components"]["<component name>"]
    for key in gen.COMPONENT_OVERRIDE_KEYS:
        assert key in overrides, key
    for key in gen.GENERATOR_COMPONENT_KEYS:
        assert f"`{key}`" in catalog_schema.ENTRY_SHAPE["components"], key


@pytest.mark.anyio
async def test_the_tool_returns_the_entry_shape_beside_the_charts_schema(fake_backends, http):
    root = fake_backends.backends.instance_root
    (root / "charts" / "service").mkdir(parents=True)
    (root / "charts" / "service" / "values.schema.json").write_text(json.dumps({"properties": {"components": {}}}))
    async with sdk_client(http, mint(fake_backends, Role.viewer)) as client:
        r = await client.call_tool("orbit_catalog_schema", {})
    assert not r.is_error, r.content
    body = r.structured_content
    assert json.loads(body["body"]) == {"properties": {"components": {}}}
    assert set(body["entry"]) == set(catalog_schema.ENTRY_KEYS)
    assert "hostnames" in body["entry"]["environments"]["<env>"]
