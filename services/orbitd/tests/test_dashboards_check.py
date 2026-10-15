"""
The panel checker — `orbit_dashboards_check`. Fake backends only, no cluster, no network.

WHY IT EXISTS. Phase 8 deletes the eBPF instrumentation floor and rebuilds every generated
dashboard on Traefik's own metrics and on span-metrics. The acceptance for that phase is
"every panel returns a non-empty series", and nobody can check thirty panels by eye after
every change. So the platform gets a tool that runs every panel's query and says what came
back.

THE DISTINCTION THAT MAKES IT USEFUL. An empty panel has two very different causes, and the
tool must never blur them:

  * the query is fine and the component genuinely emits nothing yet (a worker before the
    product emits spans) — that is `no_data`, and it names the component so a human knows who
    to chase;
  * the query is wrong — a renamed metric, a bad label, a syntax error — and Prometheus says
    so. That is `broken`, and it is a defect in the generator.

A checker that reported both as "empty" would let a renamed metric sit unnoticed behind a
worker that was expected to be quiet, which is exactly the failure this phase can produce.
"""

from __future__ import annotations

import json

import pytest

from orbitd.roles import Listing, Registry, Role
from orbitd.tools_viewer import register

TOOL = "orbit_dashboards_check"


def invoke(state, tool: str = TOOL, **kwargs):
    registry = Registry()
    register(registry, state)
    return registry.get(tool).fn(**kwargs)


def dashboard(pid: str, env: str, panels: list[tuple[str, str]]) -> dict:
    """A generated dashboard ConfigMap as the sidecar consumes it: one JSON blob per
    dashboard, panels each with a title and one target expression."""
    body = {
        "uid": f"orbit-red-{pid}-{env}",
        "title": f"RED — {pid} ({env})",
        "panels": [
            {"id": i, "title": title, "type": "timeseries",
             "targets": [{"expr": expr, "refId": "A"}]}
            for i, (title, expr) in enumerate(panels, start=1)
        ],
    }
    return {
        "apiVersion": "v1",
        "kind": "ConfigMap",
        "metadata": {
            "name": f"grafana-dashboard-red-{pid}-{env}",
            "namespace": "monitoring",
            "labels": {"grafana_dashboard": "1", "orbit.local/project": pid,
                       "orbit.local/env": env},
        },
        "data": {f"red-{pid}-{env}.json": json.dumps(body)},
    }


def prom(series_for: dict[str, int], broken: set[str] = frozenset()):
    """A fake Prometheus: a query in `series_for` returns that many series, a query in
    `broken` answers the way Prometheus answers a bad expression, anything else is empty."""
    def answer(call):
        query = (call.get("params") or {}).get("query", "")
        if query in broken:
            return {"status": "error", "errorType": "bad_data",
                    "error": f"parse error: unknown function in {query[:40]}"}
        n = series_for.get(query, 0)
        return {"status": "success", "data": {
            "resultType": "vector",
            "result": [{"metric": {"i": str(i)}, "value": [1, "1"]} for i in range(n)],
        }}
    return answer


@pytest.fixture
def wired(fake_backends):
    """One project with a live web panel and a worker panel nothing emits yet."""
    state = fake_backends
    live = "sum(rate(traefik_service_requests_total{service=\"demo-prod-demo-web-http@kubernetes\"}[5m]))"
    quiet = "sum(rate(traces_spanmetrics_calls_total{service=\"demo-importer\"}[5m]))"
    state.backends.kube.add_object(dashboard("demo", "prod",
                                             [("Requests/s", live), ("Worker calls", quiet)]))
    state.backends.prom.responses["/api/v1/query"] = prom({live: 3})
    state.live, state.quiet = live, quiet
    return state


class TestRegistration:
    def test_it_is_a_read_only_viewer_tool(self, fake_backends):
        registry = Registry()
        register(registry, fake_backends)
        assert TOOL in registry.names()
        spec = registry.get(TOOL)
        assert spec.role is Role.viewer
        assert spec.annotations.read_only_hint is True
        assert spec.annotations.destructive_hint is False
        assert spec.description


class TestClassification:
    def test_a_panel_with_series_is_ok(self, wired):
        result = invoke(wired)
        assert isinstance(result, Listing)
        rows = {r["panel"]: r for r in result.items}
        assert rows["Requests/s"]["status"] == "ok"

    def test_a_valid_query_with_no_series_is_no_data(self, wired):
        rows = {r["panel"]: r for r in invoke(wired).items}
        assert rows["Worker calls"]["status"] == "no_data"

    def test_a_query_prometheus_rejects_is_broken(self, fake_campaign):
        rows = {r["panel"]: r for r in invoke(fake_campaign).items}
        assert rows["Broken"]["status"] == "broken"
        assert "parse error" in rows["Broken"]["reason"]

    def test_no_data_is_not_reported_as_broken(self, wired):
        rows = {r["panel"]: r for r in invoke(wired).items}
        assert rows["Worker calls"]["status"] != "broken"
        assert not rows["Worker calls"].get("reason", "")

    def test_every_row_names_where_it_came_from(self, wired):
        for row in invoke(wired).items:
            assert row["dashboard"] == "orbit-red-demo-prod"
            assert row["project"] == "demo"
            assert row["env"] == "prod"
            assert row["panel"]


@pytest.fixture
def fake_campaign(fake_backends):
    """A dashboard carrying one query Prometheus refuses."""
    state = fake_backends
    bad = "sum(rate(nosuchfunc(x)[5m]))"
    state.backends.kube.add_object(dashboard("demo", "prod", [("Broken", bad)]))
    state.backends.prom.responses["/api/v1/query"] = prom({}, broken={bad})
    return state


class TestSelection:
    def test_it_checks_every_generated_dashboard_by_default(self, fake_backends):
        state = fake_backends
        a = "sum(rate(traefik_service_requests_total{service=\"a-prod-a-web-http@kubernetes\"}[5m]))"
        b = "sum(rate(traefik_service_requests_total{service=\"b-dev-b-web-http@kubernetes\"}[5m]))"
        state.backends.kube.add_object(dashboard("a", "prod", [("A", a)]))
        state.backends.kube.add_object(dashboard("b", "dev", [("B", b)]))
        state.backends.prom.responses["/api/v1/query"] = prom({a: 1, b: 1})
        assert {r["project"] for r in invoke(state).items} == {"a", "b"}

    def test_a_project_and_env_narrow_it(self, fake_backends):
        state = fake_backends
        a = "sum(rate(traefik_service_requests_total{service=\"a-prod-a-web-http@kubernetes\"}[5m]))"
        b = "sum(rate(traefik_service_requests_total{service=\"b-dev-b-web-http@kubernetes\"}[5m]))"
        state.backends.kube.add_object(dashboard("a", "prod", [("A", a)]))
        state.backends.kube.add_object(dashboard("b", "dev", [("B", b)]))
        state.backends.prom.responses["/api/v1/query"] = prom({a: 1, b: 1})
        rows = invoke(state, project="a", env="prod").items
        assert {r["project"] for r in rows} == {"a"}

    def test_a_hand_made_dashboard_is_not_checked(self, fake_backends):
        """Only what the catalog generates is the tool's business — a human's own dashboard
        is not a defect when it is empty."""
        state = fake_backends
        hand = dashboard("demo", "prod", [("Hand", "up")])
        hand["metadata"]["name"] = "grafana-dashboard-someones-own"
        hand["metadata"]["labels"].pop("orbit.local/project")
        state.backends.kube.add_object(hand)
        state.backends.prom.responses["/api/v1/query"] = prom({})
        assert invoke(state).items == []


class TestSummary:
    def test_the_counts_say_what_needs_attention(self, wired):
        result = invoke(wired)
        assert result.total == 2
        counts = {}
        for row in result.items:
            counts[row["status"]] = counts.get(row["status"], 0) + 1
        assert counts == {"ok": 1, "no_data": 1}

    def test_it_returns_no_series_data(self, wired):
        """The tool answers a question about panels, not a metrics dump: a caller that
        wants values has orbit_metrics_query."""
        for row in invoke(wired).items:
            assert "values" not in row and "value" not in row


class TestQueries:
    def test_each_panel_target_is_actually_queried(self, wired):
        invoke(wired)
        asked = [c["params"]["query"] for c in wired.backends.prom.calls
                 if c["path"] == "/api/v1/query"]
        assert wired.live in asked and wired.quiet in asked

    def test_a_dashboard_that_is_not_json_is_reported_not_raised(self, fake_backends):
        state = fake_backends
        cm = dashboard("demo", "prod", [("A", "up")])
        cm["data"]["red-demo-prod.json"] = "{not json"
        state.backends.kube.add_object(cm)
        state.backends.prom.responses["/api/v1/query"] = prom({})
        rows = invoke(state).items
        assert rows and rows[0]["status"] == "broken"


class TestTheListingArrivesAsTheClientReturnsIt:
    """The fake above answers with plain dicts. The kubernetes dynamic client answers with
    its own ResourceInstance, whose items are ResourceField objects: not dicts, readable by
    attribute or by `to_dict()`. The checker read the fake's shape, skipped every real one,
    and returned no rows on every cluster it ever ran on. This row drives the client's own
    class, built from the fake's listing, so the shape is the library's and not ours."""

    @staticmethod
    def _through_the_client(state):
        from kubernetes.dynamic.resource import ResourceInstance
        resources = state.backends.kube.dynamic.resources
        real_get = resources.get

        class AsTheClientAnswers:
            def __init__(self, resource):
                self._resource = resource

            def get(self, **kwargs):
                return ResourceInstance(None, self._resource.get(**kwargs))

        resources.get = lambda **kwargs: AsTheClientAnswers(real_get(**kwargs))
        return state

    def test_the_fake_and_the_client_disagree_on_shape(self, wired):
        # Anti-vacuity: the wrapped listing's items really are not dicts.
        state = self._through_the_client(wired)
        resource = state.backends.kube.dynamic.resources.get(api_version="v1", kind="ConfigMap")
        items = resource.get(namespace="monitoring", label_selector="grafana_dashboard=1").items
        assert items and not any(isinstance(cm, dict) for cm in items)

    def test_every_generated_dashboard_is_still_found(self, wired):
        rows = {r["panel"]: r for r in invoke(self._through_the_client(wired)).items}
        assert rows["Requests/s"]["status"] == "ok"
        assert rows["Worker calls"]["status"] == "no_data"
        assert rows["Requests/s"]["dashboard"] == "orbit-red-demo-prod"
