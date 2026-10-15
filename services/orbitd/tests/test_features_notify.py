"""A changed tool list is announced (docs/V1.md M2c): a client holding a
`subscriptions/listen` stream open is told when a feature is switched, and its next
`tools/list` differs by exactly that feature's tools.

The first test runs the server as the container runs it and a real SDK client over a real
socket: an in-process transport buffers a response whole, and a stream is the subject here.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import anyio
import pytest
import yaml

try:
    import httpx2
    from mcp import Client
    from mcp.client.streamable_http import streamable_http_client
    from mcp.server.subscriptions import ToolsListChanged
except ImportError:  # the root conftest builds the venv and re-execs
    httpx2 = None

from orbitd import orbit_features as rules
from orbitd import server as server_module
from orbitd.features import Features
from orbitd.backends import InstanceRepo

from test_real_server_stream import Server

pytestmark = pytest.mark.anyio

REPO = Path(__file__).resolve().parents[3]
REGISTRY_FILE = REPO / "template" / "provisioning" / "features.yaml"
REGISTRY = rules.load_registry(REPO / "template")
OPTIONAL = [name for name, spec in REGISTRY.items() if not spec["always"]]
FEATURE = next(name for name in OPTIONAL if REGISTRY[name]["tools"])
BUCKET = {"objectStorage": {"endpoint": "https://s3.fixture.invalid", "buckets": {"platform": "fixture"}}}


def checkout(root: Path, block: dict) -> Path:
    (root / "provisioning").mkdir(parents=True, exist_ok=True)
    (root / "instance").mkdir(exist_ok=True)
    shutil.copy(REGISTRY_FILE, root / "provisioning" / "features.yaml")
    (root / "instance" / "config.yaml").write_text(yaml.safe_dump({**BUCKET, "features": block}))
    return root


async def test_an_open_session_is_told_and_its_next_list_differs_by_the_features_tools(tmp_path, monkeypatch):
    if httpx2 is None:
        pytest.skip("the environment is being built")
    instance = checkout(tmp_path / "instance", {name: True for name in OPTIONAL})
    monkeypatch.setenv("ORBITD_INSTANCE_DIR", str(instance))
    server = Server(tmp_path)
    try:
        server.wait_until_listening()
        headers = {"Authorization": f"Bearer {server.token}"}
        async with httpx2.AsyncClient(headers=headers, trust_env=False, timeout=60) as http:
            async with Client(streamable_http_client(f"{server.base}/mcp", http_client=http)) as client:
                before = {t.name for t in (await client.list_tools()).tools}
                async with client.listen(tools_list_changed=True) as sub:
                    assert sub.honored.tools_list_changed, "the server did not agree to announce tool list changes"
                    checkout(instance, {name: name != FEATURE for name in OPTIONAL})
                    with anyio.fail_after(server_module.FEATURES_WATCH_PERIOD_S * 3):
                        event = await sub.__anext__()
                    assert isinstance(event, ToolsListChanged), event
                    after = {t.name for t in (await client.list_tools()).tools}
                    assert before - after == set(REGISTRY[FEATURE]["tools"]) & before
                    assert after - before == set()
                    # and back on: told again, and the list is whole again
                    checkout(instance, {name: True for name in OPTIONAL})
                    with anyio.fail_after(server_module.FEATURES_WATCH_PERIOD_S * 3):
                        await sub.__anext__()
                    assert {t.name for t in (await client.list_tools()).tools} == before
    finally:
        server.stop()


class Recorder:
    def __init__(self) -> None:
        self.events: list[object] = []

    async def publish(self, event: object) -> None:
        self.events.append(event)


class World:
    """watch_features' world with the clock taken out: each `sleep` runs the next step of a
    script (what happens to the checkout during that period), then the watch reads."""

    def __init__(self, root: Path, steps: list) -> None:
        self.features = Features(InstanceRepo(str(root)))
        self.subscriptions = Recorder()
        self._steps = list(steps)

    async def sleep(self, _period: float) -> None:
        if not self._steps:
            raise anyio.get_cancelled_exc_class()()
        self._steps.pop(0)()


async def run_watch(root: Path, steps: list) -> list[object]:
    world = World(root, steps)
    with pytest.raises(BaseException):
        await server_module.watch_features(world, period_s=0, sleep=world.sleep)
    return world.subscriptions.events


async def test_ten_switches_inside_one_period_are_at_most_one_announcement(tmp_path):
    root = checkout(tmp_path / "i", {name: True for name in OPTIONAL})

    def flip_ten_times_ending_off() -> None:
        for i in range(10):
            checkout(root, {name: (name != FEATURE) if i % 2 == 0 else True for name in OPTIONAL})
        checkout(root, {name: name != FEATURE for name in OPTIONAL})

    def flip_ten_times_ending_where_it_started() -> None:
        for i in range(10):
            checkout(root, {name: True if i % 2 == 0 else (name != FEATURE) for name in OPTIONAL})

    events = await run_watch(root, [flip_ten_times_ending_off])
    assert len(events) == 1 and isinstance(events[0], ToolsListChanged)
    events = await run_watch(root, [flip_ten_times_ending_where_it_started])
    assert events == [], "the list ended where it started: nothing to announce"


async def test_nothing_is_announced_while_nothing_changes_and_an_unreadable_checkout_does_not_end_the_watch(tmp_path):
    root = checkout(tmp_path / "i", {name: True for name in OPTIONAL})
    config = root / "instance" / "config.yaml"

    def nothing() -> None:
        pass

    def break_config() -> None:
        config.write_text("features: [not, a, mapping\n")

    def turn_off() -> None:
        checkout(root, {name: name != FEATURE for name in OPTIONAL})

    assert await run_watch(root, [nothing, nothing, nothing]) == []
    events = await run_watch(root, [break_config, nothing, turn_off])
    assert len(events) == 1, "the watch must survive a broken config and announce the next real change"
