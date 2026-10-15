"""Area 4 sweep (tools.py, tools_operator/*, tools_viewer/*), round-1 repairs.

Each test pins one repair: cluster text relayed by a viewer tool is scrubbed and
bounded; an environment the catalog does not declare is not_found, never an empty
answer; query strings, cursors, ranges, trace attribute keys and the ship list are
bounded before a backend or argv sees them; a monitor name is anchored; a monitor
mutation with apply off is refused, not applied; a component with nothing deployed
shows no events; log paging carries its skip; an unparseable catalog is a structured
failure naming a position; the auth listing survives a bad row and pages.
"""

from __future__ import annotations

import json
import re

import pytest
import yaml
from conftest import mint, sdk_client

from orbitd.monitors import NAME_RE
from orbitd.roles import Listing, Registry, Role
from orbitd.tools_viewer import register as register_viewer
from orbitd.tools_viewer.common import relayed

from test_monitor_tools import world
from test_monitors import metric
from test_tools_operator import wait_result
from test_tools_viewer_kube import DEMO, DEMO_DB, call, labels, seed_catalog
from test_tools_viewer_observability import _loki

MARKER = "PLANTED-MARKER-AREA4-K9"
BEARER = "Bearer eyJhbGciOiJIUzI1NiJ9.PLANTEDPAYLOADK9.PLANTEDSIG"
GHP = "ghp_PLANTEDGITHUBTOKENK9abcdefghijklmnopqrst"

pytestmark = pytest.mark.anyio


def invoke(state, tool: str, **kwargs):
    registry = Registry()
    register_viewer(registry, state)
    return registry.get(tool).fn(**kwargs)


# ------------------------------------------------------------ R1 relayed text

def test_relayed_text_is_scrubbed_and_bounded():
    out = relayed(f"GET /x Authorization: {BEARER} {GHP}")
    assert "PLANTED" not in out and "<token>" in out
    long = relayed("x" * 10_000)
    assert len(long) < 4200 and long.endswith("…[cut 5904 chars]")
    assert relayed(None) == ""


def test_log_lines_events_and_alerts_reach_the_agent_scrubbed_and_bounded(fake_backends):
    state = fake_backends
    seed_catalog(state, [DEMO])
    state.backends.loki.responses["/loki/api/v1/query_range"] = _loki(
        ("1710000000000000000", "web-0", f"auth {BEARER} {GHP}"),
        ("1710000000000000001", "web-0", "y" * 300_000),
    )
    seed_catalog(state, [DEMO])
    listing = invoke(state, "orbit_logs_query", project="demo", env="prod", since="15m")
    text = json.dumps(listing.model_dump())
    assert "PLANTED" not in text and "<token>" in text
    assert max(len(row["line"]) for row in listing.items) < 4200
    kube = state.backends.kube
    kube.add_pod(namespace="demo-prod", name="web-0", component="web",
                 image="mirror.invalid/demo-web:t1", labels=labels("demo", "web"))
    kube.add_event(namespace="demo-prod", name="e1", reason="BackOff",
                   message=f"pull failed: {BEARER}", involved_name="web-0")
    described = invoke(state, "orbit_workload_describe", project="demo", env="prod", component="web")
    (event,) = described.events
    assert "PLANTED" not in json.dumps(described.model_dump()) and "<token>" in event.message
    state.backends.alertmanager.responses["/api/v2/alerts"] = [{
        "labels": {"alertname": "A", "severity": "page", "note": GHP},
        "annotations": {"summary": f"see {BEARER}"},
        "startsAt": "2026-01-01T00:00:00Z", "status": {"state": "active", "silencedBy": []},
    }]
    alerts = invoke(state, "orbit_alerts_active")
    assert "PLANTED" not in json.dumps(alerts.model_dump())


# ------------------------------------------------------------ R2 env is validated

@pytest.mark.parametrize("name, args", [
    ("orbit_deploy_status", {"project": "demo", "env": "prd"}),
    ("orbit_deploy_history", {"project": "demo", "env": "prd"}),
    ("orbit_workload_list", {"project": "demo", "env": "prd"}),
    ("orbit_workload_describe", {"project": "demo", "env": "prd", "component": "web"}),
    ("orbit_db_status", {"project": "demo", "env": "prd"}),
])
async def test_an_environment_the_catalog_does_not_declare_is_not_found(fake_backends, http, name, args):
    state = fake_backends
    seed_catalog(state, [dict(DEMO, database={"size": "1Gi"})])
    r = await call(http, state, name, args)
    assert r.is_error, name
    assert r.structured_content["code"] == "not_found"
    assert r.structured_content["what_failed"] == "env 'prd' is not in project 'demo'"


@pytest.mark.parametrize("env", ["../../kube-system", "demo\x00x", "x" * 300, "Prod"])
async def test_an_environment_not_of_id_shape_never_reaches_the_cluster(fake_backends, http, env):
    state = fake_backends
    seed_catalog(state, [DEMO])
    r = await call(http, state, "orbit_workload_list", {"project": "demo", "env": env})
    # refused by the parameter's shape at the surface, before the tool body runs
    assert r.is_error and r.structured_content is None


# ------------------------------------------------------------ R3 bounded inputs

async def test_logs_query_for_an_undeclared_env_is_not_found(fake_backends, http):
    seed_catalog(fake_backends, [DEMO])
    async with sdk_client(http, mint(fake_backends, Role.viewer)) as client:
        r = await client.call_tool("orbit_logs_query", {"project": "demo", "env": "prd"})
        assert r.is_error and r.structured_content["code"] == "not_found"
        assert r.structured_content["what_failed"] == "env 'prd' is not in project 'demo'"
        r = await client.call_tool("orbit_logs_query", {"project": "kube", "env": "system"})
        assert r.is_error and r.structured_content["code"] == "not_found"
    assert fake_backends.backends.loki.calls == []


async def test_log_query_and_cursor_shapes_are_refused_at_the_surface(fake_backends, http):
    seed_catalog(fake_backends, [DEMO])
    async with sdk_client(http, mint(fake_backends, Role.viewer)) as client:
        for args in ({"query": "} | label_format x=`Q`\n{namespace=\"kube-system\"}"},
                     {"query": "|= \"e\"" * 600}, {"cursor": "NOT-A-TIMESTAMP"}):
            r = await client.call_tool("orbit_logs_query", {"project": "demo", "env": "prod", **args})
            assert r.is_error, args
    assert fake_backends.backends.loki.calls == []


async def test_metrics_range_is_bounded(fake_backends, http):
    fake_backends.backends.prom.responses["/api/v1/query_range"] = {"status": "success", "data": {"result": []}}
    async with sdk_client(http, mint(fake_backends, Role.viewer)) as client:
        r = await client.call_tool("orbit_metrics_query", {"query": "up", "start": "0", "end": "9999999999"})
        assert r.is_error and r.structured_content["code"] == "invalid_argument"
        r = await client.call_tool("orbit_metrics_query", {"query": "up", "start": "1710003600", "end": "1710000000"})
        assert r.is_error
        r = await client.call_tool("orbit_metrics_query", {"query": "up", "start": "x; drop", "end": "1"})
        assert r.is_error and r.structured_content is None  # refused by the parameter's shape
        r = await client.call_tool("orbit_metrics_query", {"query": "up", "start": "1710000000", "end": "1710003600", "step": "15s"})
        assert not r.is_error
    assert len(fake_backends.backends.prom.calls) == 1


async def test_a_trace_tag_key_that_is_not_an_attribute_name_is_refused(fake_backends, http):
    fake_backends.backends.tempo.responses["/api/search"] = {"traces": []}
    async with sdk_client(http, mint(fake_backends, Role.viewer)) as client:
        r = await client.call_tool("orbit_traces_find", {"service": "web", "tags": {
            'a="1" && resource.service.name': "OTHER"}})
        assert r.is_error and r.structured_content["code"] == "invalid_argument"
        r = await client.call_tool("orbit_traces_find", {"service": "web", "tags": {"http.status_code": "500"}})
        assert not r.is_error
    (only,) = fake_backends.backends.tempo.calls
    assert only["params"]["q"] == '{resource.service.name="web" && span.http.status_code="500"}'


async def test_ship_refuses_a_repeated_or_oversized_component_list(fake_backends, http):
    world(fake_backends)
    async with sdk_client(http, mint(fake_backends, Role.operator)) as client:
        r = await client.call_tool("orbit_deploy_ship", {"project": "demo", "env": "prod", "tag": "t1",
                                                         "components": ["web"] * 3, "dry_run": True})
        assert r.is_error and r.structured_content["code"] == "invalid_argument"
        r = await client.call_tool("orbit_deploy_ship", {"project": "demo", "env": "prod", "tag": "t1",
                                                         "components": [f"c{i}" for i in range(40)], "dry_run": True})
        assert r.is_error
    assert fake_backends.deploy.runner.calls == []


def test_the_monitor_name_pattern_is_anchored():
    assert re.search(NAME_RE.pattern, "; touch /tmp/a ;b") is None
    assert re.search(NAME_RE.pattern, "../../../etc/passwd-a") is None
    assert NAME_RE.fullmatch("queue-backlog") and re.search(NAME_RE.pattern, "queue-backlog")
    assert not NAME_RE.fullmatch("queue-backlog\n")


async def test_delete_and_silence_refuse_a_name_a_file_declares_in_another_shape(fake_backends, http, caplog):
    bad = metric(name="../../../etc/orbit-a")
    runner = world(fake_backends, monitors=[dict(bad, name="queue-backlog")])
    root = fake_backends.backends.instance_root
    (root / "monitors" / "bad.yaml").write_text(yaml.safe_dump(bad))
    from orbitd.tools_operator.monitors import _delete, _silence
    import logging
    with caplog.at_level(logging.INFO, logger="orbitd.audit"):
        r = _delete(fake_backends, "../../../etc/orbit-a", True, None)
        assert r.is_error and "../" not in r.structured_content["what_failed"]
        r = _silence(fake_backends, "../../../etc/orbit-a", "1h", "why")
        assert r.is_error
    assert runner.calls == [] and fake_backends.backends.alertmanager.calls == []
    rows = [rec.getMessage() for rec in caplog.records if rec.name == "orbitd.audit"]
    assert len(rows) == 2 and all("failed" in row and "../" not in row for row in rows)


# ------------------------------------------------------------ R4 refused when apply is off

@pytest.mark.parametrize("mode", ["off", "dry-run"])
async def test_monitor_upsert_and_delete_with_apply_off_are_refused_not_applied(fake_backends, http, mode):
    runner = world(fake_backends, mode=mode, monitors=[metric(name="old-one")])
    async with sdk_client(http, mint(fake_backends, Role.operator), confirm=True) as client:
        running = await client.call_tool("orbit_monitor_upsert", {"monitor": metric()})
        result, _ = await wait_result(client, running)
        assert result["status"] == "refused"
        assert result["evidence"]["verdict"] == "LANDED NOT APPLIED" and result["commit"]
        apply = next(s for s in result["steps"] if s["name"] == "apply")
        assert apply["status"] == "skipped" and "ORBITD_APPLY=on" in apply["detail"]
        running = await client.call_tool("orbit_monitor_delete", {"name": "old-one"})
        result, _ = await wait_result(client, running)
        assert result["status"] == "refused"
        assert result["evidence"]["verdict"] == "LANDED NOT PRUNED"
        prune = next(s for s in result["steps"] if s["name"] == "prune")
        assert prune["status"] == "skipped" and "nothing was pruned" in prune["detail"]
    assert len(runner.calls) == 2


# ------------------------------------------------------------ R5 no events for nothing

def test_a_component_with_nothing_deployed_shows_no_events(fake_backends):
    seed_catalog(fake_backends, [DEMO])
    kube = fake_backends.backends.kube
    kube.add_pod(namespace="demo-prod", name="worker-0", component="worker", image="i:1")
    kube.add_event(namespace="demo-prod", name="e1", reason="BackOff", message="worker crashed",
                   involved_name="worker-0")
    described = invoke(fake_backends, "orbit_workload_describe", project="demo", env="prod", component="web")
    assert described.pods.items == [] and described.events == []


# ------------------------------------------------------------ R6 cursor carry

def test_log_paging_carries_the_skip_across_pages_that_share_one_stamp(fake_backends):
    state = fake_backends
    seed_catalog(state, [DEMO])
    rows = [("1710000000000000000", "web-0", f"line {i}") for i in range(300)]
    calls: list[dict] = []

    def handler(call):
        calls.append(call)
        n = int(call["params"]["limit"])
        return _loki(*rows[:n])

    state.backends.loki.responses["/loki/api/v1/query_range"] = handler
    seed_catalog(state, [DEMO])
    first = invoke(state, "orbit_logs_query", project="demo", env="prod", limit=100)
    assert first.next_cursor == "1710000000000000000:100"
    second = invoke(state, "orbit_logs_query", project="demo", env="prod", limit=100, cursor=first.next_cursor)
    assert second.next_cursor == "1710000000000000000:200"
    assert [r["line"] for r in second.items][:2] == ["line 100", "line 101"]
    third = invoke(state, "orbit_logs_query", project="demo", env="prod", limit=100, cursor=second.next_cursor)
    assert [r["line"] for r in third.items][:1] == ["line 200"]


# ------------------------------------------------------------ R7 unparseable catalog

async def test_an_unparseable_catalog_is_a_structured_failure_naming_a_position_only(fake_backends, http, caplog):
    state = fake_backends
    root = state.backends.instance_root
    (root / "catalog").mkdir(exist_ok=True)
    (root / "catalog" / "projects.yaml").write_text(f"projects: !{MARKER} [ ]\n")
    for name, args in (("orbit_project_list", {}), ("orbit_deploy_status", {"project": "demo", "env": "prod"})):
        r = await call(http, state, name, args)
        assert r.is_error, name
        body = r.structured_content
        assert body is not None and body["code"] != "unexpected"
        assert "projects.yaml" in body["what_failed"] and "line 1" in json.dumps(body)
        assert MARKER not in json.dumps(body)
    assert MARKER not in caplog.text


# ------------------------------------------------------------ R8 auth listing

async def test_auth_list_survives_a_bad_token_row_and_pages(fake_backends, http):
    state = fake_backends
    import json as _json
    for i in range(12):
        state.tokens.mint(f"p{i}", Role.viewer)
    store = state.store.load()
    store["tokens"]["broken"] = {"sub": "x"}
    store["tokens"]["notadict"] = "x"
    state.store.save(store)
    async with sdk_client(http, mint(state, Role.admin, "adm")) as client:
        first = await client.call_tool("orbit_auth_list", {"limit": 5})
        assert not first.is_error
        tokens = first.structured_content["tokens"]
        assert len(tokens["items"]) == 5 and tokens["next_cursor"] == "5" and tokens["total"] == 15
        rest = await client.call_tool("orbit_auth_list", {"limit": 500, "cursor": "5"})
        rows = {t["jti"]: t for t in rest.structured_content["tokens"]["items"]}
        assert rows["broken"]["role"] == "unreadable" and rows["notadict"]["sub"] == "unreadable"
        assert rest.structured_content["tokens"]["next_cursor"] is None


# ============================================================ round 2 (auditor findings)

def _mint_at(state, sub: str, iso: str) -> str:
    """Mint one token and rewrite its stored iat: the store stamps every mint with now()."""
    state.tokens.mint(sub, Role.viewer)
    store = state.store.load()
    rows = [j for j, t in store["tokens"].items() if isinstance(t, dict) and t.get("sub") == sub]
    jti = rows[-1]
    store["tokens"][jti]["iat"] = iso
    state.store.save(store)
    return jti


async def test_auth_list_is_newest_first_by_the_stored_rfc3339_iat(fake_backends, http):
    state = fake_backends
    old = _mint_at(state, "old", "2026-01-01T00:00:00+00:00")
    mid = _mint_at(state, "mid", "2026-06-01T00:00:00+00:00")
    new = _mint_at(state, "new", "2026-09-01T00:00:00Z")
    async with sdk_client(http, mint(state, Role.admin, "adm")) as client:
        r = await client.call_tool("orbit_auth_list", {"limit": 500})
        rows = r.structured_content["tokens"]["items"]
        subs = [t["sub"] for t in rows]
        assert subs.index("new") < subs.index("mid") < subs.index("old")
        assert rows[0]["sub"] == "adm"  # minted last, at the wall clock
        assert subs.index("old") > subs.index("adm")


def test_scrub_leaves_a_challenge_header_and_the_word_token_alone():
    from orbitd.deploy import scrub
    kept = ('401 Unauthorized: Basic realm="orbit", charset="UTF-8"; '
            'Bearer error="invalid_token", scope="read"; bearer token missing from request')
    assert scrub(kept) == kept
    assert "<token>" in scrub("Authorization: Basic YTpi") and "<token>" in scrub(BEARER)


def test_secret_literals_are_read_once_per_file_version(tmp_path, monkeypatch):
    from orbitd import deploy
    token = tmp_path / "ghcr-pull-token"
    token.write_text("PLANTED-REGISTRY-K9-ONE\n")
    monkeypatch.setenv("ORBIT_REGISTRY_TOKEN_FILE", str(token))
    monkeypatch.delenv("ORBIT_CONFIG_DIR", raising=False)
    opened: list[str] = []
    real = deploy._read_secret_file

    def counting(path):
        opened.append(path)
        return real(path)
    monkeypatch.setattr(deploy, "_read_secret_file", counting)
    deploy._LITERAL_CACHE.clear()
    for _ in range(50):
        assert deploy.scrub("x PLANTED-REGISTRY-K9-ONE y") == "x <token> y"
    assert len(opened) == 1
    token.write_text("PLANTED-REGISTRY-K9-TWO-LONGER\n")
    assert deploy.scrub("PLANTED-REGISTRY-K9-TWO-LONGER") == "<token>"
    assert deploy.scrub("PLANTED-REGISTRY-K9-ONE") == "PLANTED-REGISTRY-K9-ONE"
    assert len(opened) == 2


def test_backend_error_body_is_scrubbed_at_the_one_site():
    from orbitd.backends import BackendError
    e = BackendError(status=500, body=f"upstream said {BEARER} and {GHP} " + "z" * 400)
    assert BEARER not in e.body and GHP not in e.body and len(e.body) <= 200
    assert BEARER not in json.dumps(e.as_failure().model_dump()) and GHP not in e.as_failure().model_dump_json()


def test_ledger_tags_at_a_commit_that_does_not_parse_answers_no_tags(tmp_path):
    import subprocess
    from orbitd.backends import InstanceRepo
    from orbitd.tools_viewer.ledger_get import _tags_at
    root = tmp_path / "instance"
    (root / "catalog").mkdir(parents=True)
    (root / "catalog" / "tags.yaml").write_text(f"tags: [{MARKER}\n")
    subprocess.run(["git", "-C", str(root), "init", "-b", "main"], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(root), "add", "-A"], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(root), "-c", "user.name=dev", "-c", "user.email=dev@example.test",
                    "-c", "commit.gpgsign=false", "commit", "-qm", "broken"], check=True, capture_output=True)
    inst = InstanceRepo(str(root))
    sha = inst.tags_history(limit=1)[0]["commit"]
    assert _tags_at(inst, sha) == {}


def test_metric_labels_trace_names_and_alert_stamps_are_relayed(fake_backends):
    from test_tools_viewer_observability import invoke
    state = fake_backends
    state.backends.prom.responses["/api/v1/query"] = {
        "status": "success", "data": {"resultType": "vector", "result": [
            {"metric": {"job": f"web {BEARER}", GHP: "v" * 5000}, "value": [11, "1"]}]}}
    series = invoke(state, "orbit_metrics_query", query="up").items[0]["metric"]
    assert BEARER not in series["job"] and all(GHP not in k for k in series)
    assert all(len(v) < 1200 for v in series.values())
    state.backends.tempo.responses["/api/search"] = {"traces": [{
        "traceID": "0" * 32, "rootServiceName": f"svc {GHP}", "rootTraceName": "GET /" + "a" * 1000,
        "startTimeUnixNano": "1000", "durationMs": 1}]}
    hit = invoke(state, "orbit_traces_find", service="web", since="1h").items[0]
    assert GHP not in hit["root_service"] and len(hit["root_name"]) < 300
    state.backends.alertmanager.responses["/api/v2/alerts"] = [{
        "labels": {"alertname": "x"}, "annotations": {}, "status": {"state": "active"},
        "startsAt": f"2026-09-14T00:00:00Z {GHP}"}]
    alert = invoke(state, "orbit_alerts_active").items[0]
    assert GHP not in alert["starts_at"]


def test_dashboard_names_and_db_phase_are_relayed(fake_backends):
    from test_dashboards_check import dashboard, prom
    from test_tools_viewer_observability import invoke
    state = fake_backends
    cm = dashboard("demo", "prod", [(f"Requests {BEARER}", "up")])
    body = json.loads(cm["data"][next(iter(cm["data"]))])
    body["uid"] = f"uid {GHP}"
    cm["data"] = {f"red-{GHP}.json": json.dumps(body), f"broken-{GHP}.json": "{not json"}
    state.backends.kube.add_object(cm)
    state.backends.prom.responses["/api/v1/query"] = prom({"up": 1})
    rows = invoke(state, "orbit_dashboards_check").items
    assert len(rows) == 2 and any(r["status"] == "broken" for r in rows)
    assert all(BEARER not in json.dumps(r) and GHP not in json.dumps(r) for r in rows)
    seed_catalog(state, [DEMO_DB])
    cluster = state.backends.kube.add_cluster(namespace="demo-prod", name="demo-db",
                                              phase=f"Cluster in healthy state {BEARER}")
    cluster["status"]["currentPrimary"] = f"demo-db-1 {GHP}"
    registry = Registry()
    register_viewer(registry, state)
    db = registry.get("orbit_db_status").fn(project="demo", env="prod")
    assert BEARER not in db.phase and GHP not in db.primary


def test_log_pod_label_is_relayed(fake_backends):
    from test_tools_viewer_observability import invoke
    state = fake_backends
    seed_catalog(state, [DEMO])
    state.backends.loki.responses["/loki/api/v1/query_range"] = _loki(("1", f"web-0 {GHP} " + "p" * 900, "hello"))
    rows = invoke(state, "orbit_logs_query", project="demo", env="prod").items
    assert rows and GHP not in rows[0]["pod"] and len(rows[0]["pod"]) < 300


def test_each_line_of_a_multi_line_secret_file_is_a_literal(tmp_path, monkeypatch):
    from orbitd import deploy
    cfg = tmp_path / "cfg"
    cfg.mkdir()
    body = "PLANTEDKEYLINEONE-K9-ABCDEFGH\nPLANTEDKEYLINETWO-K9-ABCDEFGH\n"
    (cfg / "github-app-key.pem").write_text("-----BEGIN PRIVATE KEY-----\n" + body + "-----END PRIVATE KEY-----\n")
    monkeypatch.setenv("ORBIT_CONFIG_DIR", str(cfg))
    monkeypatch.delenv("ORBIT_REGISTRY_TOKEN_FILE", raising=False)
    deploy._LITERAL_CACHE.clear()
    out = deploy.scrub("key line seen: PLANTEDKEYLINETWO-K9-ABCDEFGH end")
    assert "PLANTEDKEYLINETWO" not in out and out.endswith(" end")
    armor = "expected a -----BEGIN PRIVATE KEY----- header, got -----END PRIVATE KEY-----"
    assert deploy.scrub(armor) == armor


def test_db_status_backup_stamps_and_wal_are_relayed(fake_backends):
    state = fake_backends
    seed_catalog(state, [DEMO_DB])
    cluster = state.backends.kube.add_cluster(namespace="demo-prod", name="demo-db")
    cluster["status"].update({
        "firstRecoverabilityPoint": f"2026-01-01T00:00:00Z {BEARER}",
        "lastSuccessfulBackup": f"2026-01-02T00:00:00Z {GHP}",
        "conditions": [{"type": "ContinuousArchiving", "status": f"True {BEARER}" + "w" * 900}],
    })
    registry = Registry()
    register_viewer(registry, state)
    db = registry.get("orbit_db_status").fn(project="demo", env="prod")
    assert BEARER not in db.first_recoverability_point and GHP not in db.last_successful_backup
    assert BEARER not in db.wal_archive and len(db.wal_archive) < 300


async def test_auth_list_token_rows_survive_a_surrogate_in_a_principal_name(fake_backends, http):
    state = fake_backends
    state.tokens.mint("bad", Role.viewer)
    store = state.store.load()
    for row in store["tokens"].values():
        if isinstance(row, dict) and row.get("sub") == "bad":
            row["sub"] = "bad\ud800name"
    state.store.save(store)
    async with sdk_client(http, mint(state, Role.admin, "adm")) as client:
        r = await client.call_tool("orbit_auth_list", {})
        assert not r.is_error, r.content
        subs = [t["sub"] for t in r.structured_content["tokens"]["items"]]
        assert "bad\ufffdname" in subs


def test_backend_error_scrubs_a_held_literal_that_straddles_the_cut(tmp_path, monkeypatch):
    from orbitd import deploy
    from orbitd.backends import BODY_LIMIT, BackendError
    token = tmp_path / "ghcr-pull-token"
    token.write_text("PLANTED-REGISTRY-STRADDLE-K9-0123456789\n")
    monkeypatch.setenv("ORBIT_REGISTRY_TOKEN_FILE", str(token))
    monkeypatch.delenv("ORBIT_CONFIG_DIR", raising=False)
    deploy._LITERAL_CACHE.clear()
    e = BackendError(status=500, body="r" * (BODY_LIMIT - 12) + "PLANTED-REGISTRY-STRADDLE-K9-0123456789 tail")
    assert "PLANTED-REG" not in e.body and len(e.body) <= BODY_LIMIT


def test_a_non_utf8_credential_file_does_not_fail_the_scrub(tmp_path, monkeypatch):
    from orbitd import deploy
    token = tmp_path / "ghcr-pull-token"
    token.write_bytes(b"\xff\xfe\x00PLANTED-BINARY-SECRET-K9")
    monkeypatch.setenv("ORBIT_REGISTRY_TOKEN_FILE", str(token))
    monkeypatch.delenv("ORBIT_CONFIG_DIR", raising=False)
    deploy._LITERAL_CACHE.clear()
    assert deploy.scrub("hello") == "hello"
