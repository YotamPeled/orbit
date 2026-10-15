"""The PromQL orbit_size_recommend sends, evaluated by promtool over synthetic series.

The tool's own tests answer each query from a table, which proves the mapping and not the
queries. Here the exact expressions `queries()` builds run in Prometheus's own evaluator
against two days of one-minute samples, and each must return the number worked out by
hand: the peak across a component's pods, the p95 of the 5-minute CPU rate, the history
of a container that started late, restarts, and both signatures of an out-of-memory kill.

What this cannot prove offline is that a live cluster's series carry these label names;
that is the live half of the proof (docs/V1.md M4b, proof 2).

promtool is in the local test image (scripts/local-tests.sh). Where it is missing this
skips, saying so; a check that cannot run is never reported as a pass.
"""

from __future__ import annotations

import os
import pathlib
import shutil
import subprocess

import pytest
import yaml

from orbitd.tools_viewer.size_recommend import queries

PROMTOOL = shutil.which("promtool")
MI = 1024 * 1024
NS = 'namespace="demo-prod"'
END = 2880            # minutes of samples: two days at one a minute
EVAL = f"{END}m"

# web: two pods (100 Mi and 150 Mi; 6.25m and 12.5m of CPU), there all along; one of them has
# two cAdvisor series, as after a restart. worker: one pod that started 880 minutes ago
# (200 Mi, 50m), restarted once and last exited 137. engine: restarted three times, last
# reason OOMKilled. The pod-level (container="") and pause (container="POD") series and
# another namespace must never count. In promtool's notation `_x2000` is 2000 empty
# minutes and `0+3x880` a counter gaining 3 a minute (3 CPU-seconds per 60 s is 50m).
SERIES = {
    f'container_memory_working_set_bytes{{{NS},pod="web-0",container="web",id="/a"}}': f"{100 * MI}x{END}",
    f'container_memory_working_set_bytes{{{NS},pod="web-0",container="web",id="/b"}}': f"{90 * MI}x{END}",
    f'container_memory_working_set_bytes{{{NS},pod="web-1",container="web",id="/c"}}': f"{150 * MI}x{END}",
    f'container_memory_working_set_bytes{{{NS},pod="web-1",container="",id="/pod"}}': f"{999 * MI}x{END}",
    f'container_memory_working_set_bytes{{{NS},pod="web-1",container="POD",id="/p"}}': f"{999 * MI}x{END}",
    'container_memory_working_set_bytes{namespace="other-prod",pod="web-0",container="web",id="/o"}': f"{999 * MI}x{END}",
    f'container_memory_working_set_bytes{{{NS},pod="worker-0",container="worker",id="/w"}}': f"_x2000 {200 * MI}x880",
    f'container_cpu_usage_seconds_total{{{NS},pod="web-0",container="web",id="/a"}}': f"0+0.375x{END}",
    f'container_cpu_usage_seconds_total{{{NS},pod="web-1",container="web",id="/c"}}': f"0+0.75x{END}",
    f'container_cpu_usage_seconds_total{{{NS},pod="web-1",container="",id="/pod"}}': f"0+60x{END}",
    f'container_cpu_usage_seconds_total{{{NS},pod="worker-0",container="worker",id="/w"}}': "_x2000 0+3x880",
    f'kube_pod_container_resource_requests{{{NS},pod="web-0",container="web",resource="memory"}}': f"{256 * MI}x{END}",
    f'kube_pod_container_resource_requests{{{NS},pod="web-1",container="web",resource="memory"}}': f"{256 * MI}x{END}",
    f'kube_pod_container_resource_requests{{{NS},pod="worker-0",container="worker",resource="memory"}}': f"{384 * MI}x{END}",
    f'kube_pod_container_resource_limits{{{NS},pod="web-0",container="web",resource="memory"}}': f"{256 * MI}x{END}",
    f'kube_pod_container_resource_limits{{{NS},pod="web-1",container="web",resource="memory"}}': f"{256 * MI}x{END}",
    f'kube_pod_container_resource_limits{{{NS},pod="worker-0",container="worker",resource="memory"}}': f"{512 * MI}x{END}",
    f'kube_pod_container_resource_requests{{{NS},pod="web-0",container="web",resource="cpu"}}': f"0.05x{END}",
    f'kube_pod_container_resource_requests{{{NS},pod="web-1",container="web",resource="cpu"}}': f"0.05x{END}",
    f'kube_pod_container_resource_requests{{{NS},pod="worker-0",container="worker",resource="cpu"}}': f"0.1x{END}",
    f'kube_pod_container_resource_limits{{{NS},pod="worker-0",container="worker",resource="cpu"}}': f"0.5x{END}",
    f'kube_pod_container_status_restarts_total{{{NS},pod="web-0",container="web"}}': "0x2000 2x880",
    f'kube_pod_container_status_restarts_total{{{NS},pod="web-1",container="web"}}': f"0x{END}",
    f'kube_pod_container_status_restarts_total{{{NS},pod="worker-0",container="worker"}}': "0x2500 1x380",
    f'kube_pod_container_status_restarts_total{{{NS},pod="engine-0",container="engine"}}': "0x2500 3x380",
    # ready: both web pods all along; the worker for the second half of its 880 minutes;
    # the engine never
    f'kube_pod_container_status_ready{{{NS},pod="web-0",container="web"}}': f"1x{END}",
    f'kube_pod_container_status_ready{{{NS},pod="web-1",container="web"}}': f"1x{END}",
    f'kube_pod_container_status_ready{{{NS},pod="worker-0",container="worker"}}': "_x2000 0x439 1x439",
    f'kube_pod_container_status_ready{{{NS},pod="engine-0",container="engine"}}': f"0x{END}",
    # web-0 restarted twice and exited 0 both times: restarts, not kills
    f'kube_pod_container_status_last_terminated_exitcode{{{NS},pod="web-0",container="web"}}': f"0x{END}",
    f'kube_pod_container_status_last_terminated_exitcode{{{NS},pod="worker-0",container="worker"}}': f"137x{END}",
    f'kube_pod_container_status_last_terminated_reason{{{NS},pod="engine-0",container="engine",reason="OOMKilled"}}': f"1x{END}",
    f'kube_pod_container_status_last_terminated_reason{{{NS},pod="web-0",container="web",reason="Completed"}}': f"1x{END}",
}

# Over the last day ([1d], evaluated at the last sample). A range reaches back to, and
# not including, its start: at one point per 5 minutes a day holds 288 points, the
# oldest 86100 s ago. The worker's first point is 880 minutes old: 177 points, 52800 s.
EXPECTED = {
    "running": {"web": 3, "worker": 1},
    "memory_request": {"web": 256 * MI, "worker": 384 * MI},
    "memory_limit": {"web": 256 * MI, "worker": 512 * MI},
    "cpu_request": {"web": 0.05, "worker": 0.1},
    "cpu_limit": {"worker": 0.5},
    "memory_peak": {"web": 150 * MI, "worker": 200 * MI},
    "memory_median": {"web": 150 * MI, "worker": 200 * MI},
    "cpu_p95": {"web": 0.0125, "worker": 0.05},
    "cpu_mean": {"web": 0.0125, "worker": 0.05},
    "cpu_peak": {"web": 0.0125, "worker": 0.05},
    "samples": {"web": 288, "worker": 177},
    "history_s": {"web": 86100, "worker": 52800},
    "restarts": {"web": 2, "worker": 1, "engine": 3},
    "ready_fraction": {"web": 1, "worker": 0.5, "engine": 0},
}


# The kill count walks its window minute by minute, and promtool's evaluator holds at
# most 10 000 samples at once (the real Prometheus: fifty million). So it is asked over
# the last four hours of a fixture of its own, where everything happens in those hours:
#   worker  one restart, last exit 137 (the k3s signature)                       1 kill
#   engine  three restarts in one scrape, last reason OOMKilled                  3 kills
#   mixed   four restarts after clean exits, twenty minutes apart, then a kill   1 kill
#           (QA of v1.0.0, F21: read as five)
#   burst   three restarts inside five minutes, the third after a kill           1 kill
#           (v1.0.1-rc1 on the test box, at a 5-minute step: read as three)
#   young   a pod four minutes old, killed once                                  1 kill
#   web     two restarts after clean exits                                       none
KILL_WINDOW = "4h"
KILL_SERIES = {
    f'kube_pod_container_status_restarts_total{{{NS},pod="worker-0",container="worker"}}': "0x2750 1x129",
    f'kube_pod_container_status_last_terminated_exitcode{{{NS},pod="worker-0",container="worker"}}': f"137x{END}",
    f'kube_pod_container_status_restarts_total{{{NS},pod="engine-0",container="engine"}}': "0x2760 3x119",
    f'kube_pod_container_status_last_terminated_reason{{{NS},pod="engine-0",container="engine",reason="OOMKilled"}}': "_x2760 1x119",
    f'kube_pod_container_status_restarts_total{{{NS},pod="mixed-0",container="mixed"}}': "0x2700 1x19 2x19 3x19 4x19 5x99",
    f'kube_pod_container_status_last_terminated_reason{{{NS},pod="mixed-0",container="mixed",reason="Completed"}}': "_x2700 1x79 0x99",
    f'kube_pod_container_status_last_terminated_reason{{{NS},pod="mixed-0",container="mixed",reason="OOMKilled"}}': "_x2700 0x79 1x99",
    f'kube_pod_container_status_last_terminated_exitcode{{{NS},pod="mixed-0",container="mixed"}}': "_x2700 0x79 137x99",
    f'kube_pod_container_status_restarts_total{{{NS},pod="burst-0",container="burst"}}': "0x2700 1x1 2x1 3x175",
    f'kube_pod_container_status_last_terminated_reason{{{NS},pod="burst-0",container="burst",reason="Completed"}}': "_x2700 1x3 0x175",
    f'kube_pod_container_status_last_terminated_reason{{{NS},pod="burst-0",container="burst",reason="OOMKilled"}}': "_x2700 0x3 1x175",
    f'kube_pod_container_status_last_terminated_exitcode{{{NS},pod="burst-0",container="burst"}}': "_x2700 0x3 137x175",
    f'kube_pod_container_status_restarts_total{{{NS},pod="young-0",container="young"}}': "_x2876 0 1x2",
    f'kube_pod_container_status_last_terminated_exitcode{{{NS},pod="young-0",container="young"}}': "_x2877 137x2",
    f'kube_pod_container_status_restarts_total{{{NS},pod="web-0",container="web"}}': "0x2800 2x79",
    f'kube_pod_container_status_last_terminated_exitcode{{{NS},pod="web-0",container="web"}}': f"0x{END}",
    f'kube_pod_container_status_last_terminated_reason{{{NS},pod="web-0",container="web",reason="Completed"}}': f"1x{END}",
}
KILLS = {"worker": 1, "engine": 3, "mixed": 1, "burst": 1, "young": 1}


def promtool_cases() -> dict:
    asked = queries("demo-prod", 1)
    assert set(asked) == set(EXPECTED) | {"oom_kills"}, "a query was added or removed without its expected answer"

    def samples(expected: dict) -> list[dict]:
        return [{"labels": f'{{container="{name}"}}', "value": value} for name, value in expected.items()]

    return {
        "rule_files": [],
        "evaluation_interval": "1m",
        "fuzzy_compare": True,
        "tests": [
            {"interval": "1m",
             "input_series": [{"series": s, "values": v} for s, v in SERIES.items()],
             "promql_expr_test": [{"expr": asked[key], "eval_time": EVAL, "exp_samples": samples(EXPECTED[key])}
                                  for key in asked if key != "oom_kills"]},
            {"interval": "1m",
             "input_series": [{"series": s, "values": v} for s, v in KILL_SERIES.items()],
             "promql_expr_test": [{"expr": queries("demo-prod", 1, window=KILL_WINDOW)["oom_kills"],
                                   "eval_time": EVAL, "exp_samples": samples(KILLS)}]},
        ],
    }


def run_promtool(cases: dict, tmp_path: pathlib.Path) -> subprocess.CompletedProcess:
    (tmp_path / "size.test.yaml").write_text(yaml.safe_dump(cases, sort_keys=False), encoding="utf-8")
    return subprocess.run([PROMTOOL, "test", "rules", "size.test.yaml"], cwd=tmp_path,
                          capture_output=True, text=True, timeout=120)


@pytest.fixture
def promtool():
    if PROMTOOL is None:
        if os.environ.get("CI"):
            pytest.fail("promtool is not installed: the queries CANNOT be evaluated, which is not a pass")
        pytest.skip("promtool is not installed here")


def test_every_query_returns_the_hand_computed_number(promtool, tmp_path):
    proc = run_promtool(promtool_cases(), tmp_path)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "SUCCESS" in proc.stdout + proc.stderr


def test_the_evaluation_bites(promtool, tmp_path):
    """One wrong expectation fails the run: a pass above is a comparison, not a parse."""
    cases = promtool_cases()
    for check in cases["tests"][0]["promql_expr_test"]:
        if check["expr"] == queries("demo-prod", 1)["memory_peak"]:
            check["exp_samples"][0]["value"] += MI
    proc = run_promtool(cases, tmp_path)
    assert proc.returncode != 0, "promtool accepted a peak one Mi off"
