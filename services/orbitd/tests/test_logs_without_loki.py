"""With the logs feature off there is no Loki (docs/V1.md M2c): the logs tool and the deploy
step's log assertion read the node's own pod logs through the Kubernetes API, so an agent
can still name why a pod crashed and a ship can still prove its new pod spoke."""

from __future__ import annotations

import time

import pytest

from orbitd import node_logs
from orbitd.features import enable_hint

from test_assertions import POD, SINCE, _by_name, _run, _seed
from test_features_gate import OPTIONAL, write_features
from test_sweep_area4 import invoke
from test_tools_viewer_kube import DEMO, labels, seed_catalog

pytestmark = pytest.mark.anyio

NS = "demo-prod"


def stamp(seconds_ago: float, nanos: int = 0) -> str:
    t = time.time() - seconds_ago
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(t)) + f".{nanos:09d}Z"


def logs_off(state) -> None:
    write_features(state, {name: name != "logs" for name in OPTIONAL})


def world(state, *, restarts: int = 0):
    seed_catalog(state, [DEMO])
    kube = state.backends.kube
    pod = kube.add_pod(namespace=NS, name="demo-web-1", component="web", restarts=restarts,
                       labels=labels("demo", "web"))
    kube.add_pod(namespace=NS, name="demo-worker-1", component="worker", labels=labels("demo", "worker"))
    return kube, pod


# ---------------------------------------------------------------- the reader itself

def test_a_node_log_is_read_line_by_line_with_its_stamp():
    rows = node_logs.parse("2026-10-05T12:00:00.5Z first\n2026-10-05T12:00:01.000000002+00:00 second\n  continued\n")
    assert [line for _, line in rows] == ["first", "second", "  continued"]
    assert rows[0][0] % 1_000_000_000 == 500_000_000
    assert rows[1][0] - rows[0][0] == 500_000_002
    assert rows[2][0] == rows[1][0], "an unstamped line keeps the stamp before it"


def test_only_a_chain_of_line_filters_is_a_query_the_node_can_answer():
    f = node_logs.line_filters('|= "error" != "debug" |~ "code=5\\\\d\\\\d"')
    assert node_logs.keeps("error code=503", f)
    assert not node_logs.keeps("error debug code=503", f)
    assert not node_logs.keeps("error code=200", f)
    assert node_logs.line_filters(None) == [] and node_logs.line_filters("  ") == []
    for query in ('| json', '|= "x" | line_format "{{.a}}"', 'rate({a="b"}[5m])', '|~ "("'):
        with pytest.raises(node_logs.UnsupportedQuery):
            node_logs.line_filters(query)


# ---------------------------------------------------------------- orbit_logs_query

def test_logs_off_the_tool_answers_from_the_node_and_says_so(fake_backends):
    state = fake_backends
    kube, _ = world(state)
    kube.pod_logs[(NS, "demo-web-1")] = f"{stamp(60)} listening on 8080\n{stamp(30)} error: db refused\n"
    kube.pod_logs[(NS, "demo-worker-1")] = f"{stamp(45)} worker up\n"
    logs_off(state)
    out = invoke(state, "orbit_logs_query", project="demo", env="prod")
    assert [r["line"] for r in out.items] == ["listening on 8080", "worker up", "error: db refused"]
    assert {r["pod"] for r in out.items} == {"demo-web-1", "demo-worker-1"}
    assert "logs feature is off" in out.note and "node" in out.note
    assert state.backends.loki.calls == [], "Loki was asked although the logs feature is off"

    one = invoke(state, "orbit_logs_query", project="demo", env="prod", component="web", query='|= "error"')
    assert [r["line"] for r in one.items] == ["error: db refused"]


def test_logs_off_a_crashed_containers_last_run_is_there(fake_backends):
    """The line that says why a pod crashed is in the run BEFORE the restart."""
    state = fake_backends
    kube, _ = world(state, restarts=3)
    kube.pod_logs[(NS, "demo-web-1", "web", True)] = f"{stamp(120)} FATAL: cannot reach the database\n"
    kube.pod_logs[(NS, "demo-web-1", "web", False)] = f"{stamp(20)} starting\n"
    logs_off(state)
    out = invoke(state, "orbit_logs_query", project="demo", env="prod", component="web")
    assert [(r["line"], r.get("previous", False)) for r in out.items] == [
        ("FATAL: cannot reach the database", True), ("starting", False)]
    assert all(r["container"] == "web" for r in out.items)
    asked = [(c.get("container"), bool(c.get("previous"))) for c in kube.pod_log_calls if c["name"] == "demo-web-1"]
    assert ("web", True) in asked and ("web", False) in asked
    assert all(c["tail_lines"] <= node_logs.TAIL_LINES and c["limit_bytes"] <= node_logs.LIMIT_BYTES
               for c in kube.pod_log_calls), "a read from the node must be bounded"


def test_logs_off_a_query_the_node_cannot_answer_is_refused_with_the_way_out(fake_backends):
    state = fake_backends
    world(state)
    logs_off(state)
    out = invoke(state, "orbit_logs_query", project="demo", env="prod", query="| json | status >= 500")
    body = out.structured_content
    assert out.is_error and body["code"] == "FEATURE_OFF" and body["evidence"]["feature"] == "logs"
    assert enable_hint("logs") in body["next_action"]


def test_logs_off_old_lines_are_left_out_and_a_long_answer_pages(fake_backends):
    state = fake_backends
    kube, _ = world(state)
    kube.pod_logs[(NS, "demo-web-1")] = (f"{stamp(7200)} two hours ago\n"
                                         + "".join(f"{stamp(300 - i, i)} line {i}\n" for i in range(5)))
    kube.pod_logs[(NS, "demo-worker-1")] = ""
    logs_off(state)
    first = invoke(state, "orbit_logs_query", project="demo", env="prod", since="15m", limit=2)
    assert [r["line"] for r in first.items] == ["line 0", "line 1"]
    assert first.truncated and first.next_cursor
    second = invoke(state, "orbit_logs_query", project="demo", env="prod", since="15m", limit=10,
                    cursor=first.next_cursor)
    assert [r["line"] for r in second.items] == ["line 2", "line 3", "line 4"]
    assert not second.truncated


def test_logs_on_the_tool_still_asks_loki(fake_backends):
    state = fake_backends
    world(state)
    write_features(state, {name: True for name in OPTIONAL})
    out = invoke(state, "orbit_logs_query", project="demo", env="prod")
    assert state.backends.loki.calls, "with logs on the answer comes from Loki"
    assert out.note is None
    assert state.backends.kube.pod_log_calls == []


# ---------------------------------------------------------------- the deploy step

def test_logs_off_the_deploy_step_proves_the_new_pod_spoke_from_the_node(fake_backends):
    state = fake_backends
    _seed(state)
    logs_off(state)
    stamp_ns = SINCE * 1_000_000_000 + 5
    state.backends.kube.pod_logs[("demo-prod", POD)] = "2024-03-09T16:00:00.000000005Z listening\n"
    report, _ = _run(state)
    assert report.passed is True, report
    assert _by_name(report)["log"].status == "ok"
    assert report.evidence["log"] == {"pod": POD, "ts": str(stamp_ns), "source": "node"}
    assert "node log" in (_by_name(report)["log"].detail or "")
    assert state.backends.loki.calls == []


def test_logs_off_a_new_pod_that_wrote_nothing_is_not_deployed(fake_backends):
    state = fake_backends
    _seed(state)
    logs_off(state)
    state.backends.kube.pod_logs[("demo-prod", POD)] = ""
    report, _ = _run(state, timeout_s=0)
    assert report.verdict == "LANDED NOT DEPLOYED log"
    assert "no log line on the node" in (_by_name(report)["log"].detail or "")

    # a line from BEFORE the ship does not vouch for it
    state.backends.kube.pod_logs[("demo-prod", POD)] = "2024-03-09T15:59:00Z old line\n"
    report, _ = _run(state, timeout_s=0)
    assert report.verdict == "LANDED NOT DEPLOYED log"


def test_a_log_that_is_one_json_document_is_read_as_it_was_written():
    """The real client is asked for the raw body: it would otherwise turn a log that is one
    JSON document into a Python repr. The fake answers the same way the real one does for
    `_preload_content=False` (an object with the bytes)."""
    from types import SimpleNamespace
    seen = {}

    class Core:
        def read_namespaced_pod_log(self, name, namespace, **kwargs):
            seen.update(kwargs)
            return SimpleNamespace(data=b'{"ok": true, "steps": []}\n')

    text = node_logs.read_text(SimpleNamespace(core=Core()), "pod", "ns", container="converge")
    assert seen["_preload_content"] is False and seen["container"] == "converge"
    assert text == '{"ok": true, "steps": []}\n'
    from orbitd import features_job
    assert features_job.last_json_object(text) == {"ok": True, "steps": []}
