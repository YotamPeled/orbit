"""orbit_size_recommend through the SDK client over a seeded fake Prometheus (docs/V1.md
M4b, proofs 1 and 4): a viewer gets the advice, the numbers are the hand-computed ones, an
unknown project is not_found, Prometheus down is a clean failure, and nothing is written."""

from __future__ import annotations

from orbitd import sizing
import subprocess

import pytest
from conftest import mint, sdk_client

from orbitd.backends import BackendError
from orbitd.roles import Role
from orbitd.tools_viewer.size_recommend import queries

from test_tools_operator import install
from test_tools_viewer_kube import seed_catalog

pytestmark = pytest.mark.anyio

MI = 1024 * 1024
WEEK_S = 2015 * 300          # first sample 2015 five-minute steps ago: 2016 samples, no gaps

CATALOG = {
    "id": "demo",
    "environments": {"prod": {"branch": "main"}, "dev": {"branch": "dev", "enabled": True}},
    "components": [
        {"name": "web", "kind": "web"},
        {"name": "worker", "kind": "worker"},
        {"name": "engine", "kind": "web"},
        {"name": "nightly", "kind": "cron", "schedule": "0 3 * * *"},   # nothing running now
    ],
}

# What Prometheus holds for demo-prod, by quantity then by `container` label. Values are
# strings, as the API sends them. `otel-init` is a container that is no catalog component.
SEED = {
    "running": {"web": "2", "worker": "1", "engine": "1", "otel-init": "1"},
    "memory_request": {"web": str(512 * MI), "worker": str(256 * MI), "engine": str(256 * MI), "otel-init": str(64 * MI)},
    "memory_limit": {"web": str(512 * MI), "worker": str(256 * MI), "engine": str(256 * MI)},
    "cpu_request": {"web": "0.05", "worker": "0.1", "engine": "0.1"},
    "cpu_limit": {},
    "memory_peak": {"web": str(100 * MI), "worker": str(250 * MI), "engine": str(236 * MI), "otel-init": str(9 * MI)},
    "memory_median": {"web": str(90 * MI), "worker": str(200 * MI), "engine": str(232 * MI)},
    "cpu_p95": {"web": "0.04", "worker": "0.3", "engine": "0.02"},
    "cpu_mean": {"web": "0.012", "worker": "0.2", "engine": "0.011"},
    "cpu_peak": {"web": "0.06", "worker": "0.5", "engine": "0.09"},
    "samples": {"web": "2016", "worker": "2016", "engine": "2016"},
    "history_s": {"web": str(WEEK_S), "worker": str(WEEK_S), "engine": str(WEEK_S)},
    "restarts": {"web": "0", "worker": "2.0004", "engine": "0"},
    "oom_kills": {"worker": "2.0004"},
}


def vector(values: dict[str, str]) -> dict:
    return {"status": "success", "data": {"resultType": "vector", "result": [
        {"metric": {"container": name}, "value": [1789215303.4, value]} for name, value in values.items()]}}


def seed_prometheus(state, seed=SEED, namespace="demo-prod", window_days=7) -> None:
    by_query = {expr: key for key, expr in queries(namespace, window_days).items()}

    def answer(request):
        return vector(seed.get(by_query.get(request["params"]["query"], ""), {}))

    state.backends.prom.responses["/api/v1/query"] = answer


def head(state) -> str:
    return subprocess.run(["git", "-C", str(state.backends.instance_root), "rev-parse", "HEAD"],
                          capture_output=True, text=True, check=True).stdout.strip()


async def ask(http, state, arguments, role=Role.viewer):
    async with sdk_client(http, mint(state, role)) as client:
        return await client.call_tool("orbit_size_recommend", arguments)


async def test_a_viewer_gets_the_hand_computed_advice(fake_backends, http):
    seed_catalog(fake_backends, [CATALOG])
    seed_prometheus(fake_backends)
    r = await ask(http, fake_backends, {"project": "demo", "env": "prod"})
    assert not r.is_error, r.content
    body = r.structured_content
    assert (body["project"], body["env"], body["namespace"], body["window_days"]) == ("demo", "prod", "demo-prod", 7)
    assert (body["total"], body["returned"], body["truncated"], body["next_cursor"]) == (3, 3, False, None)
    # catalog order; the cron with nothing running and the non-component container are absent
    assert [i["component"] for i in body["items"]] == ["web", "worker", "engine"]
    web, worker, engine = body["items"]

    # web: peak 100 Mi x 1.15 = 115 -> 128 Mi against 512; p95 40m x 1.15 = 46 -> 50m against 50m
    assert web == {
        "component": "web", "containers": 2, "history_days": 7.0, "oom_kills": 0, "restarts": 0,
        "memory": {
            "request_mi": 512, "limit_mi": 512, "peak_mi": 100, "median_mi": 90,
            "verdict": "lower",
            # a suggestion to LOWER memory carries what the numbers cannot tell (docs/V1.md §8)
            "reason": "peak 100 Mi over 7.0 d plus 15% is 128 Mi, below the current 512 Mi" + sizing.HEAP_CAVEAT,
            "suggestion": {"request_mi": 128, "limit_mi": 128},
        },
        "cpu": {
            "request_m": 50, "limit_m": None, "p95_m": 40.0, "mean_m": 12.0, "peak_m": 60.0,
            "verdict": "leave",
            "reason": "p95 40m over 7.0 d plus 15% is 50m, within 10% of the current 50m",
            "suggestion": {"request_m": 50, "limit_m": None},
        },
    }

    # worker: killed twice at 256 Mi -> max(307.2, 356) = 356 -> 368 Mi; p95 300m x 1.15 = 345 -> 350m
    assert (worker["oom_kills"], worker["restarts"]) == (2, 2)
    assert worker["memory"]["verdict"] == "raise"
    assert worker["memory"]["suggestion"] == {"request_mi": 368, "limit_mi": 368}
    assert "killed for memory 2 time(s)" in worker["memory"]["reason"]
    assert (worker["memory"]["peak_mi"], worker["memory"]["median_mi"]) == (250, 200)
    assert worker["cpu"]["verdict"] == "raise"
    assert worker["cpu"]["suggestion"] == {"request_m": 350, "limit_m": None}
    assert (worker["cpu"]["p95_m"], worker["cpu"]["mean_m"], worker["cpu"]["peak_m"]) == (300.0, 200.0, 500.0)

    # engine: median 232 of 256 Mi is 90.6%, never killed: no suggestion, and the reason says what to read
    assert engine["memory"]["verdict"] == "no_suggestion" and engine["memory"]["suggestion"] is None
    for phrase in ("memory use does not show the need", "error rate", "heap metrics", "consider raising it"):
        assert phrase in engine["memory"]["reason"]
    # its CPU is judged on its own: p95 20m x 1.15 = 23 -> 30m against 100m
    assert engine["cpu"]["verdict"] == "lower" and engine["cpu"]["suggestion"]["request_m"] == 30

    # no suggestion below the highest usage seen
    for item in (web, worker):
        assert item["memory"]["suggestion"]["limit_mi"] >= item["memory"]["peak_mi"]


async def test_it_asks_prometheus_exactly_these_questions(fake_backends, http):
    seed_catalog(fake_backends, [CATALOG])
    seed_prometheus(fake_backends, window_days=3)
    r = await ask(http, fake_backends, {"project": "demo", "env": "prod", "window_days": 3})
    assert not r.is_error, r.content
    assert r.structured_content["window_days"] == 3
    calls = fake_backends.backends.prom.calls
    assert {(c["method"], c["path"]) for c in calls} == {("GET", "/api/v1/query")}
    sent = [c["params"]["query"] for c in calls]
    assert sent == list(queries("demo-prod", 3).values())
    mem = 'container_memory_working_set_bytes{namespace="demo-prod",container!="",container!="POD"}'
    cpu = 'rate(container_cpu_usage_seconds_total{namespace="demo-prod",container!="",container!="POD"}[5m])'
    restarts = 'increase(kube_pod_container_status_restarts_total{namespace="demo-prod"}[3d])'
    assert queries("demo-prod", 3) == {
        "running": f"count by (container) ({mem})",
        "memory_request": 'max by (container) (kube_pod_container_resource_requests{namespace="demo-prod",resource="memory"})',
        "memory_limit": 'max by (container) (kube_pod_container_resource_limits{namespace="demo-prod",resource="memory"})',
        "cpu_request": 'max by (container) (kube_pod_container_resource_requests{namespace="demo-prod",resource="cpu"})',
        "cpu_limit": 'max by (container) (kube_pod_container_resource_limits{namespace="demo-prod",resource="cpu"})',
        "memory_peak": f"max by (container) (max_over_time({mem}[3d]))",
        "memory_median": f"max by (container) (quantile_over_time(0.5, {mem}[3d]))",
        "cpu_p95": f"max by (container) (quantile_over_time(0.95, {cpu}[3d:5m]))",
        "cpu_mean": f"max by (container) (avg_over_time({cpu}[3d:5m]))",
        "cpu_peak": f"max by (container) (max_over_time({cpu}[3d:5m]))",
        "samples": f"count_over_time((max by (container) ({mem}))[3d:5m])",
        "history_s": f"time() - min_over_time(timestamp(max by (container) ({mem}))[3d:5m])",
        "restarts": f"sum by (container) ({restarts})",
        "ready_fraction": (
            'sum by (container) (sum_over_time(kube_pod_container_status_ready{namespace="demo-prod"}[3d])) / '
            'sum by (container) (count_over_time(kube_pod_container_status_ready{namespace="demo-prod"}[3d]))'
        ),
        # restart by restart: each minute's restarts, when the last exit at that minute
        # was a kill (QA of v1.0.0, F21: asked once over the window, four clean restarts
        # before one kill were five kills)
        "oom_kills": (
            'sum by (container) (sum_over_time((('
            '(kube_pod_container_status_restarts_total{namespace="demo-prod"} '
            '- kube_pod_container_status_restarts_total{namespace="demo-prod"} offset 1m) '
            'or kube_pod_container_status_restarts_total{namespace="demo-prod"}) '
            'and on (namespace, pod, container) '
            '(kube_pod_container_status_last_terminated_reason{namespace="demo-prod",reason="OOMKilled"} == 1 '
            'or kube_pod_container_status_last_terminated_exitcode{namespace="demo-prod"} == 137))[3d:1m]))'
        ),
    }


async def test_an_unknown_project_or_env_is_not_found(fake_backends, http):
    seed_catalog(fake_backends, [CATALOG])
    seed_prometheus(fake_backends)
    r = await ask(http, fake_backends, {"project": "missing", "env": "prod"})
    assert r.is_error and r.structured_content["code"] == "not_found"
    assert r.structured_content["evidence"] == {"project": "missing"}
    r = await ask(http, fake_backends, {"project": "demo", "env": "staging"})
    assert r.is_error and r.structured_content["code"] == "not_found"
    assert fake_backends.backends.prom.calls == [], "the catalog decides before Prometheus is asked"


async def test_a_window_outside_one_to_fourteen_days_is_refused(fake_backends, http):
    seed_catalog(fake_backends, [CATALOG])
    seed_prometheus(fake_backends)
    for days in (0, 15):
        r = await ask(http, fake_backends, {"project": "demo", "env": "prod", "window_days": days})
        assert r.is_error, days
    assert fake_backends.backends.prom.calls == []


async def test_prometheus_down_is_a_clean_failure(fake_backends, http):
    seed_catalog(fake_backends, [CATALOG])
    fake_backends.backends.prom.responses["/api/v1/query"] = BackendError(
        status=503, body="service unavailable", what_failed="ORBITD_PROMETHEUS_URL returned 503",
        env_var="ORBITD_PROMETHEUS_URL")
    r = await ask(http, fake_backends, {"project": "demo", "env": "prod"})
    assert r.is_error
    body = r.structured_content
    assert body["code"] == "backend_error" and body["retryable"] is True
    assert body["evidence"]["variable"] == "ORBITD_PROMETHEUS_URL"


async def test_a_query_prometheus_refuses_is_a_failure_not_an_empty_answer(fake_backends, http):
    seed_catalog(fake_backends, [CATALOG])
    fake_backends.backends.prom.responses["/api/v1/query"] = {
        "status": "error", "errorType": "execution", "error": "query timed out in expression evaluation"}
    r = await ask(http, fake_backends, {"project": "demo", "env": "prod"})
    assert r.is_error
    assert r.structured_content["code"] == "backend_error"
    assert "timed out" in r.structured_content["evidence"]["body"]


async def test_values_prometheus_cannot_give_are_no_suggestion_not_an_error(fake_backends, http):
    """A component running for an hour, and one whose every number is NaN."""
    seed_catalog(fake_backends, [CATALOG])
    seed = {key: dict(values) for key, values in SEED.items()}
    seed["history_s"]["web"] = "3600"
    seed["samples"]["web"] = "13"
    for key in seed:
        if key != "running":
            seed[key]["engine"] = "NaN"
    seed_prometheus(fake_backends, seed)
    r = await ask(http, fake_backends, {"project": "demo", "env": "prod"})
    assert not r.is_error, r.content
    web, _, engine = r.structured_content["items"]
    assert web["history_days"] == 0.0
    for resource in ("memory", "cpu"):
        assert web[resource]["verdict"] == "no_suggestion" and web[resource]["suggestion"] is None
        assert "not enough history: 1.0 h found, 24 h needed" in web[resource]["reason"]
        assert engine[resource]["verdict"] == "no_suggestion" and engine[resource]["suggestion"] is None
    assert engine["memory"]["peak_mi"] is None and engine["cpu"]["p95_m"] is None


async def test_nothing_running_is_an_empty_listing(fake_backends, http):
    seed_catalog(fake_backends, [CATALOG])
    seed_prometheus(fake_backends, {})
    r = await ask(http, fake_backends, {"project": "demo", "env": "dev"})
    assert not r.is_error, r.content
    assert r.structured_content["items"] == [] and r.structured_content["total"] == 0


async def test_it_is_read_only_and_writes_nothing(fake_backends, http, caplog):
    seed_catalog(fake_backends, [CATALOG])
    seed_prometheus(fake_backends)
    rt, _ = install(fake_backends)
    kube = fake_backends.backends.kube
    before = head(fake_backends)
    async with sdk_client(http, mint(fake_backends, Role.viewer)) as client:
        listed = await client.list_tools()
        with caplog.at_level("INFO"):
            r = await client.call_tool("orbit_size_recommend", {"project": "demo", "env": "prod"})
    assert not r.is_error, r.content
    tool = next(t for t in listed.tools if t.name == "orbit_size_recommend")
    assert tool.annotations.read_only_hint is True and tool.annotations.destructive_hint is False
    assert tool.annotations.idempotent_hint is True
    # no commit, no script, no cluster write, no confirmation asked
    assert rt.runner.calls == []
    assert head(fake_backends) == before
    assert kube.mutations == [] and kube.patches == [] and kube.apply_calls == [] and kube.delete_calls == []
    assert {c["method"] for c in fake_backends.backends.prom.calls} == {"GET"}
    # the audit line a read tool leaves: the call, the role, and that it answered
    lines = [rec for rec in caplog.records if rec.name == "orbitd.audit"
             and "orbit_size_recommend" in rec.getMessage()]
    assert [rec.getMessage() for rec in lines] == ["tool orbit_size_recommend ok"]
