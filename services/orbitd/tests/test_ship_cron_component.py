"""A `kind: cron` component ships: the CronJob's job template carrying the tag is the
landing, and the pod assertions are reported skipped with the reason, never faked.

Found shipping checkers-lab (docs/V1.md M1): the ship waited for a Deployment named like
the cron component, failed with "kubernetes request failed", and every ship that
included a scheduled job read LANDED NOT DEPLOYED rollout although it had landed."""

from __future__ import annotations

import pytest

from orbitd.deploy import execute_ship
from orbitd.roles import Step

from test_reconcile import FakeSource, OLD, rendered, set_mode, use_render
from test_tools_operator import COMMIT, ENV, PROJECT, TAG, install, seed_world

CRON = {"name": "bot", "kind": "cron", "image": "mirror.invalid/demo-bot", "schedule": "*/10 * * * *"}


def _cronjob(image: str) -> dict:
    return {
        "apiVersion": "batch/v1", "kind": "CronJob",
        "metadata": {"name": f"{PROJECT}-bot", "namespace": f"{PROJECT}-{ENV}"},
        "spec": {"schedule": "*/10 * * * *", "jobTemplate": {"spec": {"template": {"spec": {
            "containers": [{"name": "bot", "image": image}]}}}}},
    }


def _ship(state):
    return execute_ship(
        state, project=PROJECT, env=ENV, components=[CRON], tag=TAG, action="ship",
        actor="desk-op", expect=None, opening=[Step(name="confirm", status="ok")],
    )


def _ready(monkeypatch, state):
    set_mode(state, "on")
    use_render(monkeypatch, rendered())
    monkeypatch.setattr("orbitd.deploy.read_source", FakeSource(OLD, COMMIT, stale=0))


@pytest.mark.anyio
async def test_a_cron_component_lands_when_its_cronjob_carries_the_tag(fake_backends, monkeypatch):
    _ready(monkeypatch, fake_backends)
    install(fake_backends)
    kube = seed_world(fake_backends)
    kube.objects.append(_cronjob(f"mirror.invalid/demo-bot:{TAG}"))
    result = _ship(fake_backends)
    assert result.status == "applied", result
    assert result.evidence["verdict"] == "DEPLOYED"
    rollout = [s for s in result.steps if s.name == "rollout"]
    assert rollout and rollout[0].status == "ok" and "CronJob demo-bot" in (rollout[0].detail or "")
    skipped = {s.name: s for s in result.steps if s.status == "skipped"}
    for check in ("image", "ready", "log"):
        assert check in skipped and "next scheduled run" in (skipped[check].detail or ""), check
    assert [c["stage"] for c in result.evidence["components"]] == ["rolled_out"]


@pytest.mark.anyio
async def test_a_cronjob_still_on_the_old_tag_is_not_a_landing(fake_backends, monkeypatch):
    _ready(monkeypatch, fake_backends)
    rt, clock = install(fake_backends)

    def sleeper(seconds: float):
        clock.t += seconds

    rt.sleeper = sleeper
    rt.rollout_timeout_s = 10.0
    kube = seed_world(fake_backends)
    kube.objects.append(_cronjob("mirror.invalid/demo-bot:old"))
    result = _ship(fake_backends)
    assert result.status == "failed"
    assert result.evidence["verdict"] == "LANDED NOT DEPLOYED rollout"
    rollout = [s for s in result.steps if s.name == "rollout"][0]
    assert rollout.status == "failed" and "template=" in (rollout.detail or "")


@pytest.mark.anyio
async def test_a_missing_cronjob_times_out_naming_it(fake_backends, monkeypatch):
    _ready(monkeypatch, fake_backends)
    rt, clock = install(fake_backends)

    def sleeper(seconds: float):
        clock.t += seconds

    rt.sleeper = sleeper
    rt.rollout_timeout_s = 6.0
    seed_world(fake_backends)
    result = _ship(fake_backends)
    assert result.status == "failed"
    rollout = [s for s in result.steps if s.name == "rollout"][0]
    assert "CronJob demo-bot not found" in (rollout.detail or "")
