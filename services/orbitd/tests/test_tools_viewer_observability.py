"""Viewer tools over Loki, Prometheus, Tempo and Alertmanager. Fake backends only."""

from __future__ import annotations

import httpx
import yaml

from orbitd.backends import BODY_LIMIT, HttpBackend
from orbitd.roles import Listing, Registry, Role
from orbitd.tools_viewer import register

from test_tools_viewer_kube import DEMO, seed_catalog

OBS = {
    "orbit_logs_query",
    "orbit_metrics_query",
    "orbit_traces_find",
    "orbit_traces_get",
    "orbit_alerts_active",
    "orbit_monitor_list",
    "orbit_monitor_status",
}

TRACE_ID = "4bf92f3577b34da6a3ce929d0e0e4736"


def invoke(state, tool: str, **kwargs):
    if tool == "orbit_logs_query":
        seed_catalog(state, [DEMO])  # the tool answers only for a declared project/env
    registry = Registry()
    register(registry, state)
    return registry.get(tool).fn(**kwargs)


def test_obs_tools_are_viewer_read_only(fake_backends):
    registry = Registry()
    register(registry, fake_backends)
    assert OBS <= set(registry.names())
    for name in OBS:
        spec = registry.get(name)
        assert spec.role is Role.viewer
        assert spec.annotations.read_only_hint is True
        assert spec.annotations.destructive_hint is False
        assert spec.description


def _loki(*rows: tuple[str, str, str]) -> dict:
    streams: dict[str, list[list[str]]] = {}
    for ts, pod, line in rows:
        streams.setdefault(pod, []).append([ts, line])
    return {
        "status": "success",
        "data": {
            "resultType": "streams",
            "result": [{"stream": {"pod": pod}, "values": values} for pod, values in streams.items()],
        },
    }


def test_logs_query_selector_and_filter(fake_backends):
    state = fake_backends
    state.backends.kube.add_pod(
        namespace="demo-prod", name="unrelated-name", component="web",
        labels={"app.kubernetes.io/instance": "demo", "app.kubernetes.io/name": "web"},
    )
    state.backends.loki.responses["/loki/api/v1/query_range"] = _loki(
        ("100", "unrelated-name", "ready"),
    )
    result = invoke(state, "orbit_logs_query", project="demo", env="prod",
                    component="web", query='|= "error"')
    assert isinstance(result, Listing)
    assert result.items == [{"ts": "100", "pod": "unrelated-name", "line": "ready"}]
    params = state.backends.loki.calls[-1]["params"]
    assert params["query"] == '{namespace="demo-prod", pod=~"unrelated-name"} |= "error"'
    assert "demo-web-.*" not in params["query"]
    assert params["direction"] == "forward"
    assert params["limit"] == 200


def test_logs_query_all_components_omits_pod_matcher(fake_backends):
    state = fake_backends
    invoke(state, "orbit_logs_query", project="demo", env="prod")
    assert state.backends.loki.calls[-1]["params"]["query"] == '{namespace="demo-prod"}'


def _limit_loki(rows):
    """Inclusive start, hard limit — the Loki contract the pagination logic must honour."""

    def handler(call):
        params = call.get("params") or {}
        start = int(params.get("start") or 0)
        limit = int(params.get("limit") or 200)
        matched = [r for r in rows if int(r[0]) >= start][:limit]
        return _loki(*matched)

    return handler


def test_logs_cap_and_cursor_round_trip(fake_backends, monkeypatch):
    monkeypatch.setattr("orbitd.tools_viewer.logs_query.now", lambda: 900.0)
    state = fake_backends
    state.backends.kube.add_pod(
        namespace="demo-prod", name="demo-web-0", component="web",
        labels={"app.kubernetes.io/instance": "demo", "app.kubernetes.io/name": "web"},
    )
    rows = [(str(i), "demo-web-0", f"line-{i}") for i in range(1, 202)]
    state.backends.loki.responses["/loki/api/v1/query_range"] = _limit_loki(rows)
    page = invoke(state, "orbit_logs_query", project="demo", env="prod", component="web", limit=200)
    assert isinstance(page, Listing)
    assert page.returned == 200 and page.truncated is True
    assert page.next_cursor == "200:1"
    assert page.items[0]["ts"] == "1" and page.items[-1]["ts"] == "200"
    again = invoke(state, "orbit_logs_query", project="demo", env="prod",
                   component="web", limit=200, cursor=page.next_cursor)
    assert again.items[0]["ts"] == "201"
    assert again.returned == 1 and again.truncated is False
    assert state.backends.loki.calls[-1]["params"]["start"] == "200"


def test_loki_500_with_5kb_body_is_actionable_failure_truncated_to_200(fake_backends):
    payload = "z" * 5120

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text=payload)

    fake_backends.backends.loki = HttpBackend(
        "http://loki.invalid", "ORBITD_LOKI_URL",
        transport=httpx.MockTransport(handler),
    )
    result = invoke(fake_backends, "orbit_logs_query", project="demo", env="prod")
    assert result.is_error
    content = result.structured_content
    assert content["code"] == "backend_error"
    assert content["evidence"]["status"] == 500
    assert content["evidence"]["body"] == "z" * BODY_LIMIT
    assert len(content["evidence"]["body"]) == BODY_LIMIT
    dumped = str(content)
    assert "z" * 201 not in dumped
    # Log lines are attacker-influenceable: the failure names the backend, not a line.
    assert "what_failed" in content and "line-" not in content["what_failed"]


def test_metrics_instant_when_no_range(fake_backends):
    state = fake_backends
    state.backends.prom.responses["/api/v1/query"] = {
        "status": "success",
        "data": {"resultType": "vector", "result": [
            {"metric": {"__name__": "up", "job": "web"}, "value": [11, "1"]},
        ]},
    }
    result = invoke(state, "orbit_metrics_query", query="up")
    assert isinstance(result, Listing)
    assert result.truncated is False
    assert result.items == [{"metric": {"__name__": "up", "job": "web"}, "values": [[11, "1"]]}]
    assert state.backends.prom.calls[-1]["path"] == "/api/v1/query"


def test_metrics_scalar_answer_is_one_unlabelled_series(fake_backends):
    """`time()` answers with resultType scalar: a bare [ts, value] pair, not a list of
    series. The tool dropped it and the clock the operator skill promises was empty."""
    state = fake_backends
    state.backends.prom.responses["/api/v1/query"] = {
        "status": "success",
        "data": {"resultType": "scalar", "result": [1789215303.445, "1789215303.445"]},
    }
    result = invoke(state, "orbit_metrics_query", query="time()")
    assert result.items == [{"metric": {}, "values": [[1789215303.445, "1789215303.445"]]}]
    assert result.total == 1 and result.truncated is False


def test_metrics_range_cap_11000_sets_truncated(fake_backends):
    values_a = [[i, "1"] for i in range(6000)]
    values_b = [[i, "2"] for i in range(6000)]
    state = fake_backends
    state.backends.prom.responses["/api/v1/query_range"] = {
        "status": "success",
        "data": {"resultType": "matrix", "result": [
            {"metric": {"job": "a"}, "values": values_a},
            {"metric": {"job": "b"}, "values": values_b},
        ]},
    }
    result = invoke(state, "orbit_metrics_query", query="up", start="1", end="2", step="15s")
    assert isinstance(result, Listing)
    assert result.truncated is True
    points = sum(len(s["values"]) for s in result.items)
    assert points == 11_000
    assert len(result.items[0]["values"]) == 6000
    assert len(result.items[1]["values"]) == 5000
    assert state.backends.prom.calls[-1]["path"] == "/api/v1/query_range"


def test_traces_find_and_get_share_trace_id(fake_backends):
    state = fake_backends
    state.backends.tempo.responses["/api/search"] = {
        "traces": [{
            "traceID": TRACE_ID,
            "rootServiceName": "web",
            "rootTraceName": "GET /",
            "startTimeUnixNano": "1000",
            "durationMs": 42,
        }],
    }
    found = invoke(state, "orbit_traces_find", service="web", since="1h")
    assert found.items[0]["trace_id"] == TRACE_ID
    assert found.items[0]["root_service"] == "web"
    assert found.items[0]["duration_ms"] == 42
    params = state.backends.tempo.calls[-1]["params"]
    assert "tags" not in params
    assert params["q"] == '{resource.service.name="web"}'

    state.backends.tempo.responses[f"/api/traces/{TRACE_ID}"] = {
        "batches": [{
            "resource": {"attributes": [
                {"key": "service.name", "value": {"stringValue": "web"}},
            ]},
            "scopeSpans": [{"spans": [{
                "spanId": "aaaabbbbccccdddd",
                "parentSpanId": "",
                "name": "GET /",
                "startTimeUnixNano": "1000000000",
                "endTimeUnixNano": "2000000000",
                "status": {"code": "STATUS_CODE_OK"},
            }]}],
        }],
    }
    got = invoke(state, "orbit_traces_get", trace_id=TRACE_ID)
    assert got.items[0]["span_id"] == "aaaabbbbccccdddd"
    assert got.items[0]["service"] == "web"
    assert got.items[0]["name"] == "GET /"
    assert got.items[0]["duration_ms"] == 1000
    assert got.items[0]["status"] == "ok"


def test_traces_get_caps_at_500_spans(fake_backends):
    spans = [{
        "spanId": f"{i:016x}",
        "name": "op",
        "startTimeUnixNano": "0",
        "endTimeUnixNano": "1000000",
        "status": {"code": 1},
    } for i in range(501)]
    fake_backends.backends.tempo.responses[f"/api/traces/{TRACE_ID}"] = {
        "batches": [{"resource": {"serviceName": "web"}, "scopeSpans": [{"spans": spans}]}],
    }
    result = invoke(fake_backends, "orbit_traces_get", trace_id=TRACE_ID)
    assert result.returned == 500 and result.truncated is True
    assert result.total == 501


def test_unconfigured_tempo_names_orbitd_tempo_url(fake_backends):
    fake_backends.backends.tempo = HttpBackend(None, "ORBITD_TEMPO_URL")
    result = invoke(fake_backends, "orbit_traces_find", since="1h")
    assert result.is_error
    content = result.structured_content
    assert content["code"] == "backend_unconfigured"
    assert content["evidence"]["variable"] == "ORBITD_TEMPO_URL"
    assert "ORBITD_TEMPO_URL" in content["what_failed"]
    again = invoke(fake_backends, "orbit_traces_get", trace_id=TRACE_ID)
    assert again.is_error and again.structured_content["evidence"]["variable"] == "ORBITD_TEMPO_URL"


def test_alerts_active_skips_silenced(fake_backends):
    fake_backends.backends.alertmanager.responses["/api/v2/alerts"] = [
        {
            "labels": {"alertname": "QueueBacklog", "severity": "page"},
            "annotations": {"summary": "queue is backed up"},
            "startsAt": "2026-01-01T00:00:00Z",
            "status": {"state": "active", "silencedBy": []},
        },
        {
            "labels": {"alertname": "SilencedOne", "severity": "warn"},
            "annotations": {"summary": "ignored"},
            "startsAt": "2026-01-01T00:00:00Z",
            "status": {"state": "suppressed", "silencedBy": ["silence-1"]},
        },
    ]
    result = invoke(fake_backends, "orbit_alerts_active")
    assert isinstance(result, Listing)
    assert [a["name"] for a in result.items] == ["QueueBacklog"]
    assert result.items[0]["severity"] == "page"
    assert result.items[0]["summary"] == "queue is backed up"
    assert result.items[0]["description"] == ""
    params = fake_backends.backends.alertmanager.calls[-1]["params"]
    assert params["active"] == "true" and params["silenced"] == "false"


def _write_monitor(state, name: str, **fields: str) -> None:
    body = {"name": name, "project": "demo", "env": "prod", "kind": "metric",
            "severity": "page", "summary": "queue is backed up", **fields}
    path = state.backends.instance.require() / "monitors" / f"{name}.yaml"
    path.write_text(yaml.safe_dump(body))


def test_monitor_list_and_status(fake_backends):
    state = fake_backends
    _write_monitor(state, "queue-backlog")
    _write_monitor(state, "idle-probe", kind="probe", severity="warn", summary="probe failed")
    listed = invoke(state, "orbit_monitor_list")
    names = {m["name"] for m in listed.items}
    assert names == {"queue-backlog", "idle-probe"}
    assert all({"project", "env", "kind", "severity", "summary"} <= m.keys() for m in listed.items)

    state.backends.prom.responses["/api/v1/rules"] = {
        "status": "success",
        "data": {"groups": [{"name": "g", "rules": [
            {"type": "alerting", "name": "queue-backlog", "state": "firing"},
            {"type": "alerting", "name": "idle-probe", "state": "pending"},
        ]}]},
    }
    one = invoke(state, "orbit_monitor_status", name="queue-backlog")
    assert one.items[0]["state"] == "firing"
    all_status = invoke(state, "orbit_monitor_status")
    by_name = {m["name"]: m["state"] for m in all_status.items}
    assert by_name == {"queue-backlog": "firing", "idle-probe": "pending"}


def test_monitor_status_unknown_name_is_not_found(fake_backends):
    result = invoke(fake_backends, "orbit_monitor_status", name="no-such-monitor")
    assert result.is_error
    content = result.structured_content
    assert content["code"] == "not_found"
    assert content["evidence"]["name"] == "no-such-monitor"


def test_alerts_active_relays_the_rules_description(fake_backends):
    """The description is where a rule says what to do next. It was dropped whenever the
    rule had a summary, so S06e's agent read "engine is at its memory limit" and never the
    sentence that names orbit_size_recommend (docs/V1.md §8, 2026-10-09). A rule with only
    a description keeps it as its summary, once, as before."""
    fake_backends.backends.alertmanager.responses["/api/v2/alerts"] = [
        {"labels": {"alertname": "OrbitWorkloadMemoryAtLimit", "severity": "warning"},
         "annotations": {"summary": "engine is at its memory limit",
                         "description": "Look at its error rate, then ask orbit_size_recommend."},
         "startsAt": "2026-01-01T00:00:00Z", "status": {"state": "active", "silencedBy": []}},
        {"labels": {"alertname": "OnlyDescribed", "severity": "warning"},
         "annotations": {"description": "the one sentence it has"},
         "startsAt": "2026-01-01T00:00:00Z", "status": {"state": "active", "silencedBy": []}},
    ]
    first, second = invoke(fake_backends, "orbit_alerts_active").items
    assert first["summary"] == "engine is at its memory limit"
    assert first["description"] == "Look at its error rate, then ask orbit_size_recommend."
    assert second["summary"] == "the one sentence it has" and second["description"] == ""


# ---------------------------------------------------------------- QA of v1.0.0: F11, F19

def _trace(i: int, name: str) -> dict:
    return {"traceID": f"{i:032x}", "rootServiceName": "web", "rootTraceName": name,
            "startTimeUnixNano": "1000", "durationMs": 3}


def test_traces_find_leaves_the_health_checks_out_unless_asked(fake_backends):
    """F11: every kubelet probe and every scrape is traced, so a search with no tag was
    twenty `GET /readyz`. They are left out, and more traces are asked for to make up."""
    state = fake_backends
    probes = [_trace(i, name) for i, name in enumerate(
        ["GET /readyz", "GET /healthz", "HTTP GET /metrics", "GET /readyz?verbose=1", "GET /healthz/"] * 3)]
    real = [_trace(100 + i, name) for i, name in enumerate(["GET /api/entries", "POST /api/entries", "GET /"])]
    state.backends.tempo.responses["/api/search"] = {"traces": probes + real}
    found = invoke(state, "orbit_traces_find", service="web", since="1h", limit=5)
    assert [h["root_name"] for h in found.items] == ["GET /api/entries", "POST /api/entries", "GET /"]
    assert state.backends.tempo.calls[-1]["params"]["limit"] == 30, "it asked for no more than it would return"
    everything = invoke(state, "orbit_traces_find", service="web", since="1h", limit=5, probes=True)
    assert [h["root_name"] for h in everything.items][:2] == ["GET /readyz", "GET /healthz"]
    assert state.backends.tempo.calls[-1]["params"]["limit"] == 5


def test_traces_find_knows_the_probe_paths_the_catalog_declares(fake_backends):
    from test_tools_viewer_kube import seed_catalog
    state = fake_backends
    seed_catalog(state, [{"id": "demo", "environments": {"prod": {}}, "components": [
        {"name": "web", "kind": "web", "image": "x/y", "probePath": "/up", "readinessPath": "/api/ready",
         "metrics": {"port": 8080, "path": "/internal/metrics"}}]}])
    state.backends.tempo.responses["/api/search"] = {"traces": [
        _trace(1, "GET /up"), _trace(2, "GET /api/ready"), _trace(3, "GET /internal/metrics"),
        _trace(4, "GET /upload"), _trace(5, "GET /api/readyness")]}
    found = invoke(state, "orbit_traces_find", since="1h")
    assert [h["root_name"] for h in found.items] == ["GET /upload", "GET /api/readyness"]


def test_a_range_of_too_many_points_is_refused_with_the_step_that_fits(fake_backends):
    """F19: a week at the default 15 s step was answered "check the query syntax"."""
    state = fake_backends
    before = len(state.backends.prom.calls)
    result = invoke(state, "orbit_metrics_query", query="up", start="1710000000", end=str(1710000000 + 7 * 86400))
    assert result.is_error
    body = result.structured_content
    assert body["code"] == "invalid_argument"
    assert "40321 points per series" in body["what_failed"] and "11000" in body["what_failed"]
    assert body["evidence"]["step_that_fits"] == "1m" and 'step="1m"' in body["next_action"]
    assert len(state.backends.prom.calls) == before, "Prometheus was asked a question it refuses"
    ok = invoke(state, "orbit_metrics_query", query="up", start="1710000000", end=str(1710000000 + 7 * 86400), step="1m")
    assert not getattr(ok, "is_error", False)
    day = invoke(state, "orbit_metrics_query", query="up", start="1710000000", end=str(1710000000 + 86400))
    assert not getattr(day, "is_error", False), "a day at 15 s is 5761 points and fits"
