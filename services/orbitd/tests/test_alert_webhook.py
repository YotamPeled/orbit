"""POST /v1/alertmanager: Alertmanager's webhook receiver for monitor alerts. It checks its
own bearer (a file, re-read on every request so rotation needs no restart), records each
monitor's firing and resolved transitions, and orbit_monitor_status reports the latest."""

from __future__ import annotations

import json
from dataclasses import replace

import httpx2
import pytest
from conftest import ISSUER, mint, sdk_client

from orbitd.roles import Role
from orbitd.server import build_app
from orbitd.state import AppState
from orbitd.testing import FakeBackends

from test_monitors import CATALOG, metric

pytestmark = pytest.mark.anyio

PATH = "/v1/alertmanager"
TOKEN = "webhook-token-one"
FIRING_AT = "2026-09-10T10:00:00Z"
RESOLVED_AT = "2026-09-10T10:07:00Z"


@pytest.fixture
def token_file(tmp_path):
    path = tmp_path / "alert-webhook.token"
    path.write_text(TOKEN + "\n")
    return path


@pytest.fixture
def fake_backends(cfg, tmp_path, token_file) -> AppState:
    """Overrides conftest's: a world with one monitor and the webhook configured."""
    backends = FakeBackends.build(tmp_path, projects=CATALOG,
                                  monitors={"queue-backlog": metric()})
    return AppState.build(replace(cfg, alert_webhook_token_file=str(token_file)),
                          backends=backends)


def alert(status: str, *, monitor: str | None = "queue-backlog", fingerprint="fp1") -> dict:
    labels = {"alertname": monitor or "Watchdog", "severity": "critical"}
    if monitor:
        labels["orbit_monitor"] = monitor
    return {"status": status, "labels": labels, "annotations": {"summary": "s"},
            "startsAt": FIRING_AT,
            "endsAt": RESOLVED_AT if status == "resolved" else "0001-01-01T00:00:00Z",
            "fingerprint": fingerprint}


def payload(*alerts: dict) -> dict:
    return {"version": "4", "status": alerts[0]["status"] if alerts else "firing",
            "receiver": "orbitd", "groupLabels": {}, "commonLabels": {},
            "alerts": list(alerts)}


async def post(http, body, token: str | None = TOKEN, raw: bytes | None = None):
    headers = {"Content-Type": "application/json"}
    async with http(token) as client:
        return await client.post(PATH, content=raw if raw is not None else json.dumps(body),
                                 headers=headers)


async def status_of(fake_backends, http, name="queue-backlog") -> dict:
    async with sdk_client(http, mint(fake_backends, Role.viewer)) as client:
        r = await client.call_tool("orbit_monitor_status", {"name": name})
    assert not r.is_error, r.content
    (item,) = r.structured_content["items"]
    return item


async def test_without_a_configured_token_file_the_route_does_not_exist(cfg, tmp_path):
    state = AppState.build(cfg, backends=FakeBackends.build(tmp_path / "plain"))
    app = build_app(state)
    async with httpx2.AsyncClient(transport=httpx2.ASGITransport(app=app), base_url=ISSUER,
                                  headers={"Authorization": f"Bearer {TOKEN}"}) as client:
        r = await client.post(PATH, content=json.dumps(payload(alert("firing"))))
    assert r.status_code == 404


@pytest.mark.parametrize("token", [None, "wrong", TOKEN + "x", ""])
async def test_a_missing_or_wrong_bearer_is_401_and_records_nothing(fake_backends, http, token):
    r = await post(http, payload(alert("firing")), token=token)
    assert r.status_code == 401
    assert (await status_of(fake_backends, http))["last_transition"] is None


async def test_an_mcp_token_is_not_the_webhook_token(fake_backends, http):
    r = await post(http, payload(alert("firing")), token=mint(fake_backends, Role.admin))
    assert r.status_code == 401


async def test_firing_then_resolved_is_recorded_and_reported(fake_backends, http):
    r = await post(http, payload(alert("firing")))
    assert r.status_code == 200
    assert r.json()["recorded"] == 1
    assert (await status_of(fake_backends, http))["last_transition"] == {
        "state": "firing", "at": FIRING_AT}
    r = await post(http, payload(alert("resolved")))
    assert r.status_code == 200
    assert (await status_of(fake_backends, http))["last_transition"] == {
        "state": "resolved", "at": RESOLVED_AT}


async def test_a_repeated_notification_is_not_a_new_transition(fake_backends, http):
    from orbitd.alert_log import alert_log_of
    for _ in range(3):
        assert (await post(http, payload(alert("firing")))).status_code == 200
    await post(http, payload(alert("resolved")))
    history = alert_log_of(fake_backends).history("queue-backlog")
    assert [h.state for h in history] == ["firing", "resolved"]


async def test_alerts_without_a_monitor_label_are_ignored(fake_backends, http):
    r = await post(http, payload(alert("firing", monitor=None)))
    assert r.status_code == 200
    assert r.json()["recorded"] == 0


async def test_malformed_and_oversized_bodies_are_refused(fake_backends, http):
    assert (await post(http, None, raw=b"{not json")).status_code == 400
    assert (await post(http, None, raw=json.dumps({"alerts": "x"}).encode())).status_code == 400
    big = json.dumps(payload(alert("firing"))).encode() + b" " * (1024 * 1024 + 1)
    assert (await post(http, None, raw=big)).status_code == 413
    assert (await status_of(fake_backends, http))["last_transition"] is None


async def test_the_token_file_is_reread_so_rotation_needs_no_restart(fake_backends, http, token_file):
    assert (await post(http, payload(alert("firing")))).status_code == 200
    token_file.write_text("webhook-token-two\n")
    assert (await post(http, payload(alert("resolved")), token=TOKEN)).status_code == 401
    assert (await post(http, payload(alert("resolved")), token="webhook-token-two")).status_code == 200


async def test_a_monitor_never_notified_has_no_transition(fake_backends, http):
    assert (await status_of(fake_backends, http))["last_transition"] is None


# ------------------------------------------------------------------ review round (r22h)

async def test_an_oversized_streamed_body_is_refused_without_reading_it_all(fake_backends, http):
    """The limit is enforced while the body streams in: a bearer holder cannot make orbitd
    buffer an arbitrarily large request before the 413."""
    pulled = 0
    chunk = b"x" * (1024 * 1024)

    async def body():
        nonlocal pulled
        for _ in range(8):
            pulled += 1
            yield chunk

    async with http(TOKEN) as client:
        r = await client.post(PATH, content=body(), headers={"Content-Type": "application/json"})
    assert r.status_code == 413
    assert pulled <= 2, f"read {pulled} MiB before refusing"


async def test_a_declared_oversized_length_is_refused_before_reading(fake_backends, http):
    async with http(TOKEN) as client:
        r = await client.post(PATH, content=b"{}", headers={
            "Content-Type": "application/json", "Content-Length": str(64 * 1024 * 1024)})
    assert r.status_code in (400, 413)


def test_the_log_keeps_a_bounded_number_of_monitors_most_recent_first():
    from orbitd.alert_log import MAX_MONITORS, AlertLog
    assert 0 < MAX_MONITORS <= 1000
    log = AlertLog()
    for i in range(MAX_MONITORS + 50):
        assert log.record(f"m{i}", "firing", FIRING_AT)
    assert len(log.names()) == MAX_MONITORS
    assert log.latest(f"m{MAX_MONITORS + 49}") is not None
    assert log.latest("m0") is None
    # a transition refreshes a monitor, so an active one is not the one evicted
    log.record(f"m{50}", "resolved", RESOLVED_AT)
    log.record("fresh", "firing", FIRING_AT)
    assert log.latest("m50") is not None
