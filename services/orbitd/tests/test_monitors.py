"""Monitor files (`monitors/<name>.yaml`) render into the objects Prometheus, blackbox and
the Loki ruler already evaluate. Pure: no cluster, no checkout. A bad file is reported and
skipped; it never blocks the other monitors."""

from __future__ import annotations

import copy

import pytest
import yaml

from orbitd.monitors import render_monitors, validate

CATALOG = {"version": 1, "projects": [
    {"id": "demo", "environments": {"dev": {}, "prod": {}}},
]}
BLACKBOX = "blackbox-exporter.monitoring.svc:9115"


def metric(**kw):
    body = {"name": "queue-backlog", "project": "demo", "env": "prod", "kind": "metric",
            "query": "max(q_oldest_seconds)",
            "condition": {"comparison": ">", "threshold": 120, "for": "5m"},
            "severity": "page", "notify": ["telegram"],
            "summary": "Queue backed up", "runbook": "https://runbooks.invalid/q"}
    body.update(kw)
    return body


def probe(**kw):
    body = {"name": "api-door", "project": "demo", "env": "dev", "kind": "probe",
            "target": "https://api.fixture.invalid/healthz", "module": "http_2xx",
            "condition": {"for": "2m"}, "severity": "warn", "notify": [],
            "summary": "API door down"}
    body.update(kw)
    return body


def log(**kw):
    body = {"name": "error-burst", "project": "demo", "env": "prod", "kind": "log",
            "query": 'sum(count_over_time({namespace="demo-prod"} |= "error" [5m]))',
            "condition": {"comparison": ">", "threshold": 10, "for": "5m"},
            "severity": "info", "notify": ["telegram"], "summary": "Error burst"}
    body.update(kw)
    return body


def files(*bodies):
    return {f"{b['name']}.yaml": b for b in bodies}


def by_kind(objects, kind):
    return [o for o in objects if o["kind"] == kind]


def only_rule(obj):
    groups = obj["spec"]["groups"]
    assert len(groups) == 1 and len(groups[0]["rules"]) == 1
    return groups[0]["rules"][0]


def test_metric_monitor_renders_one_prometheusrule():
    out = render_monitors(files(metric()), catalog=CATALOG)
    assert out.errors == {}
    (rule_obj,) = out.objects
    assert rule_obj["apiVersion"] == "monitoring.coreos.com/v1"
    assert rule_obj["kind"] == "PrometheusRule"
    assert rule_obj["metadata"]["name"] == "orbit-monitor-queue-backlog"
    assert rule_obj["metadata"]["namespace"] == "monitoring"
    assert rule_obj["metadata"]["labels"]["orbit.local/monitor"] == "queue-backlog"
    assert rule_obj["spec"]["groups"][0]["name"] == "orbit.monitor.queue-backlog"
    rule = only_rule(rule_obj)
    assert rule["alert"] == "queue-backlog"
    assert rule["expr"] == "(max(q_oldest_seconds)) > 120"
    assert rule["for"] == "5m"
    assert rule["labels"] == {"severity": "critical", "orbit_severity": "page",
                              "orbit_monitor": "queue-backlog", "orbit_notify": "telegram",
                              "project": "demo", "env": "prod"}
    assert rule["annotations"] == {"summary": "Queue backed up",
                                   "runbook_url": "https://runbooks.invalid/q"}


@pytest.mark.parametrize("op", [">", ">=", "<", "<=", "==", "!="])
def test_every_comparison_renders_verbatim(op):
    out = render_monitors(files(metric(condition={"comparison": op, "threshold": 0.5,
                                                  "for": "1m"})), catalog=CATALOG)
    assert only_rule(out.objects[0])["expr"] == f"(max(q_oldest_seconds)) {op} 0.5"


@pytest.mark.parametrize("severity,mapped", [("page", "critical"), ("warn", "warning"),
                                             ("info", "info")])
def test_severity_maps_onto_the_stock_inhibition_levels(severity, mapped):
    out = render_monitors(files(metric(severity=severity)), catalog=CATALOG)
    labels = only_rule(out.objects[0])["labels"]
    assert (labels["severity"], labels["orbit_severity"]) == (mapped, severity)


def test_no_notify_channel_is_labelled_none_and_runbook_is_optional():
    body = metric(notify=[])
    del body["runbook"]
    out = render_monitors(files(body), catalog=CATALOG)
    rule = only_rule(out.objects[0])
    assert rule["labels"]["orbit_notify"] == "none"
    assert rule["annotations"] == {"summary": "Queue backed up"}


def test_probe_monitor_renders_a_probe_and_a_rule_on_its_job():
    out = render_monitors(files(probe()), catalog=CATALOG)
    assert out.errors == {}
    (p,) = by_kind(out.objects, "Probe")
    assert p["metadata"]["name"] == "orbit-monitor-api-door"
    assert p["metadata"]["namespace"] == "monitoring"
    assert p["metadata"]["labels"]["orbit.local/monitor"] == "api-door"
    assert p["spec"] == {"jobName": "orbit-monitor-api-door",
                         "prober": {"url": BLACKBOX}, "module": "http_2xx",
                         "targets": {"staticConfig": {
                             "static": ["https://api.fixture.invalid/healthz"]}}}
    (r,) = by_kind(out.objects, "PrometheusRule")
    rule = only_rule(r)
    assert rule["alert"] == "api-door"
    assert rule["expr"] == 'probe_success{job="orbit-monitor-api-door"} == 0'
    assert rule["for"] == "2m"
    assert rule["labels"]["severity"] == "warning"
    assert rule["labels"]["orbit_notify"] == "none"


def test_a_probe_monitor_is_refused_while_uptime_is_off():
    """The prober belongs to the uptime feature: with it off a probe monitor would look
    declared and never run. Other kinds do not need it."""
    problems = validate(probe(), catalog=CATALOG, uptime_on=False)
    assert len(problems) == 1 and "needs the uptime feature, which is off" in problems[0], problems
    assert validate(probe(), catalog=CATALOG, uptime_on=True) == []
    assert validate(metric(), catalog=CATALOG, uptime_on=False) == []
    out = render_monitors(files(probe(), metric()), catalog=CATALOG, uptime_on=False)
    assert [o["kind"] for o in out.objects] == ["PrometheusRule"], "only the metric monitor renders"
    assert len(out.errors) == 1


def test_a_monitor_that_notifies_telegram_is_refused_while_the_feature_is_off():
    problems = validate(metric(), catalog=CATALOG, telegram_on=False)
    assert len(problems) == 1 and "the alerts-telegram feature is off" in problems[0], problems
    assert validate(metric(notify=[]), catalog=CATALOG, telegram_on=False) == []
    assert validate(metric(), catalog=CATALOG, telegram_on=True) == []


def test_probe_module_defaults_to_http_2xx():
    body = probe()
    del body["module"]
    out = render_monitors(files(body), catalog=CATALOG)
    assert by_kind(out.objects, "Probe")[0]["spec"]["module"] == "http_2xx"


def test_log_monitors_share_one_loki_ruler_configmap_when_the_ruler_is_wired():
    second = log(name="auth-fail", query='sum(count_over_time({namespace="demo-prod"} |= "401" [5m]))')
    out = render_monitors(files(log(), second, metric()), catalog=CATALOG,
                          loki_ruler_wired=True)
    assert out.errors == {}
    (cm,) = by_kind(out.objects, "ConfigMap")
    assert cm["metadata"]["name"] == "orbit-monitors-logs"
    assert cm["metadata"]["namespace"] == "loki"
    (text,) = cm["data"].values()
    doc = yaml.safe_load(text)
    (group,) = doc["groups"]
    assert group["name"] == "orbit.monitors.logs"
    assert [r["alert"] for r in group["rules"]] == ["auth-fail", "error-burst"]
    burst = group["rules"][1]
    assert burst["expr"] == ('(sum(count_over_time({namespace="demo-prod"} |= "error" [5m])))'
                             ' > 10')
    assert burst["for"] == "5m"
    assert burst["labels"]["severity"] == "info"
    assert burst["labels"]["orbit_monitor"] == "error-burst"
    assert len(by_kind(out.objects, "PrometheusRule")) == 1


def test_a_log_monitor_without_a_wired_ruler_is_refused_and_renders_nothing():
    out = render_monitors(files(log(), metric()), catalog=CATALOG)
    assert set(out.errors) == {"error-burst.yaml"}
    assert "lokiRuler" in out.errors["error-burst.yaml"]
    assert by_kind(out.objects, "ConfigMap") == []
    assert len(by_kind(out.objects, "PrometheusRule")) == 1


BAD = [
    ("name-not-dns", lambda b: b.update(name="Queue_Backlog"), "name"),
    ("unknown-project", lambda b: b.update(project="nope"), "project"),
    ("unknown-env", lambda b: b.update(env="staging"), "env"),
    ("bad-kind", lambda b: b.update(kind="trace"), "kind"),
    ("bad-comparison", lambda b: b["condition"].update(comparison="=>"), "comparison"),
    ("repeated-unit", lambda b: b["condition"].update({"for": "5m5m"}), "for"),
    ("unordered-units", lambda b: b["condition"].update({"for": "30m1h"}), "for"),
    ("threshold-string", lambda b: b["condition"].update(threshold="120"), "threshold"),
    ("threshold-bool", lambda b: b["condition"].update(threshold=True), "threshold"),
    ("bad-severity", lambda b: b.update(severity="critical"), "severity"),
    ("bad-notify", lambda b: b.update(notify=["slack"]), "notify"),
    ("no-summary", lambda b: b.pop("summary"), "summary"),
    ("no-query", lambda b: b.pop("query"), "query"),
]


@pytest.mark.parametrize("case,mutate,word", BAD, ids=[c[0] for c in BAD])
def test_an_invalid_monitor_is_reported_and_skipped_alone(case, mutate, word):
    bad = metric()
    mutate(bad)
    good = metric(name="fine")
    bad_files = {"queue-backlog.yaml": bad, **files(good)}
    out = render_monitors(bad_files, catalog=CATALOG)
    assert set(out.errors) == {"queue-backlog.yaml"}
    assert word in out.errors["queue-backlog.yaml"]
    assert [o["metadata"]["name"] for o in out.objects] == ["orbit-monitor-fine"]
    assert validate(bad, catalog=CATALOG, filename="queue-backlog.yaml")


def test_the_name_must_equal_the_filename():
    out = render_monitors({"other.yaml": metric()}, catalog=CATALOG)
    assert "other" in out.errors["other.yaml"] and out.objects == []


@pytest.mark.parametrize("target", ["ftp://x.invalid/", "api.fixture.invalid", ""])
def test_a_probe_target_must_be_an_http_url(target):
    out = render_monitors(files(probe(target=target)), catalog=CATALOG)
    assert "target" in out.errors["api-door.yaml"] and out.objects == []


def test_a_file_that_is_not_a_mapping_is_reported():
    out = render_monitors({"junk.yaml": ["a", "b"], **files(metric())}, catalog=CATALOG)
    assert set(out.errors) == {"junk.yaml"}
    assert len(out.objects) == 1


def test_a_valid_body_validates_clean():
    assert validate(metric(), catalog=CATALOG, filename="queue-backlog.yaml") == []
    assert validate(log(), catalog=CATALOG, loki_ruler_wired=True) == []
    assert validate(log(), catalog=CATALOG)


def test_output_is_sorted_and_independent_of_file_order():
    bodies = [metric(), probe(), metric(name="aaa-first"), log()]
    a = render_monitors(files(*bodies), catalog=CATALOG, loki_ruler_wired=True)
    b = render_monitors(files(*reversed(bodies)), catalog=CATALOG, loki_ruler_wired=True)
    assert a.objects == b.objects
    keys = [(o["kind"], o["metadata"]["namespace"], o["metadata"]["name"]) for o in a.objects]
    assert keys == sorted(keys)


def test_rendering_does_not_mutate_its_input():
    body = metric()
    frozen = copy.deepcopy(body)
    render_monitors(files(body), catalog=CATALOG)
    assert body == frozen


def test_every_object_is_marked_as_a_monitor_object():
    out = render_monitors(files(metric(), probe(), log()), catalog=CATALOG,
                          loki_ruler_wired=True)
    assert all(o["metadata"]["labels"]["orbit.local/component"] == "monitors"
               for o in out.objects)


# ------------------------------------------------------------------ review round 1

@pytest.mark.parametrize("target", [
    "https://kubernetes.default.svc/",
    "https://kubernetes.default.svc.cluster.local/api",
    "http://blackbox-exporter.monitoring.svc:9115/probe",
    "http://grafana.internal/",
    "http://intranet/",
    "http://localhost:9090/",
    "http://127.0.0.1/",
    "http://10.0.0.5/",
    "http://172.16.0.1/",
    "http://192.168.1.1/",
    "http://100.64.0.1/",
    "http://169.254.169.254/latest/meta-data/",
    "http://[::1]/",
    "http://[fd00::1]/",
    "http://[fe80::1]/",
    "http://0.0.0.0/",
])
def test_a_probe_may_not_point_inside_the_cluster_or_the_host(target):
    out = render_monitors(files(probe(target=target)), catalog=CATALOG)
    assert "target" in out.errors.get("api-door.yaml", ""), target
    assert out.objects == []


@pytest.mark.parametrize("target", ["https://api.fixture.invalid/healthz",
                                    "https://93.184.216.34/", "http://example.com:8080/x?y=1"])
def test_a_public_probe_target_is_accepted(target):
    out = render_monitors(files(probe(target=target)), catalog=CATALOG)
    assert out.errors == {}


@pytest.mark.parametrize("query", ["(", "up) > 0 #", "up > 0 # comment", "up\n> 0", ")(", "up)"])
def test_a_query_that_could_escape_its_parentheses_is_refused(query):
    out = render_monitors(files(metric(query=query), metric(name="fine")), catalog=CATALOG)
    assert "query" in out.errors.get("queue-backlog.yaml", ""), query
    assert [o["metadata"]["name"] for o in out.objects] == ["orbit-monitor-fine"]


@pytest.mark.parametrize("query", [
    'sum(rate(x{path="("}[5m]))',
    "sum(rate(x{path='#'}[5m]))",
    'max(x{a=`)`})',
    'count(up{job="a\\"(b"})',
])
def test_parentheses_and_hashes_inside_string_literals_are_fine(query):
    out = render_monitors(files(metric(query=query)), catalog=CATALOG)
    assert out.errors == {}, out.errors


def test_a_log_query_with_a_paren_in_its_line_filter_is_fine():
    body = log(query='sum(count_over_time({namespace="demo-prod"} |= "(" [5m]))')
    out = render_monitors(files(body), catalog=CATALOG, loki_ruler_wired=True)
    assert out.errors == {}, out.errors


MALFORMED = [
    ("env-list", lambda b: b.update(env=[])),
    ("project-map", lambda b: b.update(project={})),
    ("name-int", lambda b: b.update(name=5)),
    ("kind-list", lambda b: b.update(kind=["metric"])),
    ("condition-str", lambda b: b.update(condition="x > 1")),
    ("condition-none", lambda b: b.update(condition=None)),
    ("notify-str", lambda b: b.update(notify="telegram")),
    ("notify-nested", lambda b: b.update(notify=[["telegram"]])),
    ("summary-int", lambda b: b.update(summary=5)),
    ("runbook-list", lambda b: b.update(runbook=["x"])),
    ("query-int", lambda b: b.update(query=5)),
    ("severity-list", lambda b: b.update(severity=["page"])),
    ("comparison-list", lambda b: b["condition"].update(comparison=[">"])),
    ("for-int", lambda b: b["condition"].update({"for": 5})),
]


@pytest.mark.parametrize("case,mutate", MALFORMED, ids=[c[0] for c in MALFORMED])
def test_a_malformed_field_is_one_file_error_never_an_exception(case, mutate):
    bad = metric()
    mutate(bad)
    out = render_monitors({"queue-backlog.yaml": bad, **files(metric(name="fine"))},
                          catalog=CATALOG)
    assert set(out.errors) == {"queue-backlog.yaml"}
    assert [o["metadata"]["name"] for o in out.objects] == ["orbit-monitor-fine"]
    assert validate(bad, catalog=CATALOG, filename="queue-backlog.yaml")


@pytest.mark.parametrize("target", ["http://[", "http://[::1", 5, ["https://x.invalid/"], None])
def test_a_malformed_probe_target_is_one_file_error(target):
    out = render_monitors({**files(probe(target=target)), **files(metric(name="fine"))},
                          catalog=CATALOG)
    assert "api-door.yaml" in out.errors
    assert [o["metadata"]["name"] for o in out.objects] == ["orbit-monitor-fine"]
    assert validate(probe(target=target), catalog=CATALOG)


def test_a_malformed_catalog_entry_does_not_crash_validation():
    catalog = {"projects": [None, {"id": "demo", "environments": ["dev"]},
                            {"id": "demo", "environments": {"dev": {}, "prod": {}}}]}
    out = render_monitors(files(metric()), catalog=catalog)
    assert isinstance(out.errors, dict)


# ------------------------------------------------------------------ review round 2
# One accepted form instead of a list of forbidden spellings: an ASCII DNS name whose last
# label is alphabetic, or a canonical IP literal (dotted decimal without leading zeros, or
# bracketed IPv6), then the public-address rules. Anything else is refused.

@pytest.mark.parametrize("target", [
    "http://0177.0.0.1/",                 # octal
    "http://0x7f.0.0.1/",                 # hex
    "http://127.1/",                      # short form
    "http://2130706433/",                 # one decimal number
    "http://010.0.0.1/",                  # leading zero
    "http://kubernetes.default.svc。cluster。local/",   # fullwidth dots
    "http://xn--kubernetes-.default.svc.cluster.local/",
    "http://bücher.example/",        # non-ASCII name
    "http://example.com./",               # trailing dot
    "http://KUBERNETES.DEFAULT.SVC/",     # case
    "http://example.123/",                # numeric TLD
    "http://[::ffff:127.0.0.1]/",         # IPv4-mapped IPv6
    "http://[::ffff:7f00:1]/",
    "http://[64:ff9b::a00:1]/",           # NAT64 of 10.0.0.1
    "http://-bad.example/",
    "http://bad-.example/",
    "http://exa_mple.com/",
    "http://%31%32%37.0.0.1/",            # percent-encoded
    "http://example.com:garbage/",
    "http://example.com:65536/",
    "http://example.com:0/",
    "http://example.com:/",
    "http://" + "a" * 64 + ".example/",   # label over 63
])
def test_a_probe_target_outside_the_one_accepted_form_is_refused(target):
    out = render_monitors(files(probe(target=target)), catalog=CATALOG)
    assert "target" in out.errors.get("api-door.yaml", ""), target
    assert out.objects == []


@pytest.mark.parametrize("target", [
    "https://example.com/",
    "https://api.example.co.uk:8443/health?x=1",
    "http://93.184.216.34/",
    "https://[2606:2800:220:1:248:1893:25c8:1946]/",
    "https://sub-domain.example.org/",
])
def test_canonical_public_targets_are_accepted(target):
    out = render_monitors(files(probe(target=target)), catalog=CATALOG)
    assert out.errors == {}, out.errors


# ------------------------------------------------------------------ review round 3
# The raw string is what the Probe carries, so the raw string is what is validated: any
# control character or whitespace is refused rather than cleaned away by the parser.

@pytest.mark.parametrize("target", [
    "https://exa\nmple.com/", "https://exa\tmple.com/", "https://example.com/\r",
    "\x00https://example.com/", " https://example.com/", "https://example.com/ ",
    "https://example.com/a b", "https://example.com/\x7f", "https://example.com/ ",
])
def test_a_target_with_control_characters_or_whitespace_is_refused(target):
    out = render_monitors(files(probe(target=target)), catalog=CATALOG)
    assert "target" in out.errors.get("api-door.yaml", ""), repr(target)
    assert out.objects == []


@pytest.mark.parametrize("target", ["http://224.0.0.1/", "http://239.255.255.250/",
                                    "http://[ff02::1]/", "http://[ff0e::1]/"])
def test_a_multicast_target_is_refused(target):
    out = render_monitors(files(probe(target=target)), catalog=CATALOG)
    assert "target" in out.errors.get("api-door.yaml", ""), target


# A refused target is named by field, never echoed: it may carry a credential, and the
# message reaches tool output and the reconcile log.

@pytest.mark.parametrize("target", ["https://user:REVIEW_SECRET@example.com/",
                                    "http://10.0.0.1/?token=REVIEW_SECRET",
                                    "https://exa\nmple.com/REVIEW_SECRET"])
def test_a_refused_target_is_not_echoed_in_the_error(target):
    out = render_monitors(files(probe(target=target)), catalog=CATALOG)
    err = out.errors.get("api-door.yaml", "")
    assert "target" in err
    assert "REVIEW_SECRET" not in err
    assert all("REVIEW_SECRET" not in p for p in validate(probe(target=target), catalog=CATALOG))
