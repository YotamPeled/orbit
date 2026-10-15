"""
The panel checker, round 1 repairs. Fake backends only, no cluster, no network.

WHY. The checker exists to tell a quiet component from a broken query. The review found
two ways it fails at exactly that:

  * It treats HTTP 400 as "the query is broken" and re-raises every other failure. Real
    Prometheus answers a PARSE error with 400 but an EXECUTION error — a bad regular
    expression, a query touching too many samples — with 422. So one bad panel out of
    thirty aborts the whole report and the operator is told nothing about the other
    twenty-nine, which is the opposite of a tool built to survey every panel.
  * The suite's only "broken query" test hands the checker a JSON body saying
    `status: error`. The real HTTP backend raises before such a body can be returned, so
    the test covers a branch that cannot execute and leaves the live path uncovered.

These tests use the double the way the real backend behaves: a refused query RAISES.
"""

from __future__ import annotations

import json

import pytest

from orbitd.backends import BackendError
from orbitd.roles import Listing, Registry
from orbitd.tools_viewer import register

TOOL = "orbit_dashboards_check"


def invoke(state, **kwargs):
    registry = Registry()
    register(registry, state)
    return registry.get(TOOL).fn(**kwargs)


def dashboard(pid: str, env: str, panels: list[tuple[str, str]]) -> dict:
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
        "apiVersion": "v1", "kind": "ConfigMap",
        "metadata": {
            "name": f"grafana-dashboard-red-{pid}-{env}",
            "namespace": "monitoring",
            "labels": {"grafana_dashboard": "1", "orbit.local/project": pid,
                       "orbit.local/env": env},
        },
        "data": {f"red-{pid}-{env}.json": json.dumps(body)},
    }


def raising(failures: dict[str, tuple[int, str]], healthy: set[str]):
    """A fake Prometheus that behaves like the real HTTP backend: a query the server
    refuses raises BackendError with the server's status, it never returns a body."""
    def answer(call):
        query = (call.get("params") or {}).get("query", "")
        if query in failures:
            status, body = failures[query]
            raise BackendError(status=status, body=body,
                               what_failed="prometheus query failed")
        n = 1 if query in healthy else 0
        return {"status": "success", "data": {
            "resultType": "vector",
            "result": [{"metric": {}, "value": [1, "1"]}] * n}}
    return answer


@pytest.fixture
def two_panels(fake_backends):
    """One panel Prometheus refuses and one that answers, on one dashboard."""
    state = fake_backends
    good = 'sum(rate(traefik_service_requests_total{service="a-prod-a-web-http@kubernetes"}[5m]))'
    bad = 'label_replace(up, "a", "$1", "instance", "(")'
    state.backends.kube.add_object(dashboard("a", "prod",
                                             [("Healthy", good), ("Refused", bad)]))
    state.good, state.bad = good, bad
    return state


class TestExecutionErrors:
    """A query the server executes and fails is a broken panel, not a dead report."""

    @pytest.mark.parametrize("status", [400, 422])
    def test_a_refused_query_is_one_broken_row(self, two_panels, status):
        two_panels.backends.prom.responses["/api/v1/query"] = raising(
            {two_panels.bad: (status, "parse error: unexpected end of input")},
            {two_panels.good})
        result = invoke(two_panels)
        assert isinstance(result, Listing), (
            f"a panel the server answered with {status} took the whole report down; "
            "the operator learns nothing about the other panels")
        rows = {r["panel"]: r for r in result.items}
        assert rows["Refused"]["status"] == "broken"
        assert rows["Refused"].get("reason")

    @pytest.mark.parametrize("status", [400, 422])
    def test_the_healthy_panel_beside_it_still_reports(self, two_panels, status):
        two_panels.backends.prom.responses["/api/v1/query"] = raising(
            {two_panels.bad: (status, "bad regexp")}, {two_panels.good})
        rows = {r["panel"]: r for r in invoke(two_panels).items}
        assert rows["Healthy"]["status"] == "ok"

    def test_the_reason_carries_what_the_server_said(self, two_panels):
        two_panels.backends.prom.responses["/api/v1/query"] = raising(
            {two_panels.bad: (422, "invalid parameter \"query\": bad regexp")},
            {two_panels.good})
        rows = {r["panel"]: r for r in invoke(two_panels).items}
        assert "regexp" in rows["Refused"]["reason"], (
            "the row says a panel is broken without saying why, so a generator defect "
            "cannot be told from a Prometheus that is merely overloaded")


class TestTheDoubleMatchesTheBackend:
    """The path the real backend takes is the path the suite must exercise."""

    def test_a_query_the_server_refuses_reaches_the_checker_as_a_raise(self, two_panels):
        """HttpBackend raises for any status at or above 400, so a checker that only
        understands an error BODY understands nothing that can actually happen."""
        two_panels.backends.prom.responses["/api/v1/query"] = raising(
            {two_panels.bad: (400, "parse error")}, {two_panels.good})
        rows = {r["panel"]: r for r in invoke(two_panels).items}
        assert rows["Refused"]["status"] == "broken"

    @pytest.mark.parametrize("status", [500, 503])
    def test_a_backend_that_is_down_is_not_reported_as_a_generator_defect(
            self, two_panels, status):
        """Prometheus refusing to serve says nothing about the query. Reported as
        `broken` it would read as thirty defects in the generator, which is the exact
        confusion this tool exists to end — so it stays a tool failure."""
        two_panels.backends.prom.responses["/api/v1/query"] = raising(
            {two_panels.bad: (status, "no healthy upstream")}, {two_panels.good})
        result = invoke(two_panels)
        assert not isinstance(result, Listing), (
            f"a Prometheus answering {status} was reported as a broken panel")


class TestNoDataIsStillNoData:
    """The repair must not turn a quiet component into a defect report."""

    def test_an_empty_answer_is_not_broken(self, fake_backends):
        state = fake_backends
        quiet = 'sum(rate(traces_spanmetrics_calls_total{service="a-importer"}[5m]))'
        state.backends.kube.add_object(dashboard("a", "prod", [("Worker", quiet)]))
        state.backends.prom.responses["/api/v1/query"] = raising({}, set())
        rows = {r["panel"]: r for r in invoke(state).items}
        assert rows["Worker"]["status"] == "no_data"
        assert not rows["Worker"].get("reason")
