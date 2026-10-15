#!/usr/bin/env python3
"""
Series diet, round two (phase 8 acceptance, 2026-09-12).

Measured after the cutover (evidence/phase8-after/series-by-job.md): the apiserver job held
40k of the 75k head series, and every reader of its histograms was one of the chart's four
API-server SLO rule groups. The stack was 371 MiB over the 2 GiB ceiling.

So: the four SLO groups are off; the apiserver scrape keeps an allow-list of what the rules
that stay read; the kubelet scrape also drops the scheduler, kine, CRD-openapi, feature-gate,
rest-client size and prober duration families; Grafana's own metrics are not scraped.

The reader list below was taken from `helm template` of kube-prometheus-stack 88.2.0 with the
platform values, grepping every PrometheusRule expression for apiserver_/aggregator_ families
(2026-09-12). A test at the end re-renders when helm is on the machine and holds the list to
the chart, so a chart bump that adds a reader fails here rather than silently going blind.

Run as `python3 scripts/test_series_diet_two.py` or under pytest.
"""
import pathlib
import re
import shutil
import subprocess
import sys
import tempfile
import unittest

import yaml

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from test_series_diet import APPS, _dropped, _values  # noqa: E402

SLO_GROUPS = ("kubeApiserverAvailability", "kubeApiserverBurnrate",
              "kubeApiserverHistogram", "kubeApiserverSlos")

# What the rule groups that stay on read from the apiserver job (rendered 2026-09-12).
STILL_READ = [
    "apiserver_client_certificate_expiration_seconds_bucket",
    "apiserver_client_certificate_expiration_seconds_count",
    "apiserver_request_terminations_total",
    "apiserver_request_total",
    "aggregator_unavailable_apiservice",
    "aggregator_unavailable_apiservice_total",
    "rest_client_requests_total",  # KubeClientErrors selects it on job="apiserver" (review r99b)
    "up",
]
# The heavy families, by measured series count, that the allow-list must not let through.
HEAVY = {
    "apiserver_request_duration_seconds_bucket": 9490,
    "apiserver_request_sli_duration_seconds_bucket": 5680,
    "etcd_request_duration_seconds_bucket": 4740,
    "workqueue_work_duration_seconds_bucket": 1078,
    "workqueue_queue_duration_seconds_bucket": 1078,
    "scheduler_plugin_execution_duration_seconds_bucket": 777,
    "apiserver_admission_controller_admission_duration_seconds_bucket": 434,
}
KUBELET_ROUND_TWO_DROPS = [
    "scheduler_plugin_execution_duration_seconds_bucket",
    "scheduler_event_handling_duration_seconds_bucket",
    "kine_sql_time_seconds_bucket",
    "kubernetes_feature_enabled",
    "apiextensions_openapi_v2_regeneration_count",
    "rest_client_request_size_bytes_bucket",
    "rest_client_response_size_bytes_bucket",
]
# The probes endpoint (/metrics/probes) is a separate scrape with its own list; a rule on
# the /metrics list does not reach it (review r99, 2026-09-12).
PROBES_MUST_DROP = ["prober_probe_duration_seconds_bucket", "prober_probe_duration_seconds_sum"]
PROBES_MUST_KEEP = ["prober_probe_total"]
KUBELET_STILL_READ = ["rest_client_requests_total",
                      "rest_client_request_duration_seconds_bucket", "kubelet_running_pods"]

# Every selector in a rule expression: the name before `{`. A prefix list was the
# first form of this and it was blind to rest_client_requests_total (review r99b), so
# it names no prefixes: what a rule selects on the apiserver job is what must be kept.
SELECTOR = re.compile(r"\b([a-z_][a-z0-9_:]*)\s*\{([^}]*)\}")
# A recording rule's output is read by its colon-name, braces or not (`sum(apiserver_request:burnrate1h)`).
RECORDED_REF = re.compile(r"\b([a-z_][a-z0-9_]*:[a-z0-9_:]+)\b")


def render_chart(release: str = "kps", app: str = "kube-prometheus-stack.yaml") -> tuple[str | None, str]:
    """`helm template` of the app's chart with the platform values, as one string. The
    cached archive first (no network, so a sandboxed review can run a row), then the
    repository alias, then the URL form; a machine with none of them says so in the
    second element and the first is None."""
    if shutil.which("helm") is None:
        return None, "helm is not on this machine"
    doc = next(d for d in yaml.safe_load_all(
        re.sub(r"\{\{[^}]*\}\}", "x", (APPS / app).read_text()))
        if d and d.get("kind") == "Application")
    src = doc["spec"]["source"]
    proc = None
    with tempfile.TemporaryDirectory() as tmp:
        values = pathlib.Path(tmp) / "values.yaml"
        values.write_text(src["helm"]["values"])
        cached = pathlib.Path.home() / ".cache/helm/repository" / f"{src['chart']}-{src['targetRevision']}.tgz"
        for chart in (str(cached), f"prometheus-community/{src['chart']}", src["chart"]):
            if chart == str(cached) and not cached.is_file():
                continue
            argv = ["helm", "template", release, chart, "-n", "monitoring", "-f", str(values)]
            if chart != str(cached):
                argv += ["--version", str(src["targetRevision"])]
            if chart == src["chart"]:
                argv += ["--repo", src["repoURL"]]
            proc = subprocess.run(argv, capture_output=True, text=True, timeout=300)
            if proc.returncode == 0:
                return proc.stdout, ""
    return None, f"helm could not render the chart here: {(proc.stderr if proc else '')[-200:]}"


class ApiserverAllowListTest(unittest.TestCase):
    def setUp(self):
        self.v = _values("kube-prometheus-stack.yaml")
        self.rel = self.v["kubeApiServer"]["serviceMonitor"]["metricRelabelings"]

    def test_the_four_slo_groups_are_off(self):
        rules = self.v["defaultRules"]["rules"]
        for group in SLO_GROUPS:
            self.assertIs(rules.get(group), False, f"defaultRules.rules.{group} is not false")

    def test_the_scrape_is_an_allow_list(self):
        keeps = [r for r in self.rel if (r.get("action") or "").lower() == "keep"]
        self.assertEqual(len(keeps), 1, "one keep rule, so a family not named is not scraped")
        self.assertEqual(keeps[0].get("sourceLabels"), ["__name__"])

    def test_what_the_remaining_rules_read_is_kept(self):
        lost = [n for n in STILL_READ if _dropped(self.rel, n)]
        self.assertEqual(lost, [], "the allow-list drops a family a rule that stays on reads")

    def test_the_heavy_families_are_not_scraped(self):
        through = {n: c for n, c in HEAVY.items() if not _dropped(self.rel, n)}
        self.assertEqual(through, {}, "measured-heavy families still scraped")


class KubeletRoundTwoTest(unittest.TestCase):
    def setUp(self):
        self.rel = _values("kube-prometheus-stack.yaml")["kubelet"]["serviceMonitor"]["metricRelabelings"]

    def test_the_re_exports_and_unread_histograms_are_dropped(self):
        kept = [n for n in KUBELET_ROUND_TWO_DROPS if not _dropped(self.rel, n)]
        self.assertEqual(kept, [])

    def test_what_the_rules_read_survives(self):
        lost = [n for n in KUBELET_STILL_READ if _dropped(self.rel, n)]
        self.assertEqual(lost, [])

    def test_the_probes_endpoint_drops_the_duration_histogram_on_its_own_list(self):
        sm = _values("kube-prometheus-stack.yaml")["kubelet"]["serviceMonitor"]
        probes = sm.get("probesMetricRelabelings")
        self.assertTrue(probes, "kubelet.serviceMonitor.probesMetricRelabelings is empty")
        kept = [n for n in PROBES_MUST_DROP if not _dropped(probes, n)]
        self.assertEqual(kept, [], "the probes scrape still keeps the duration histogram")
        lost = [n for n in PROBES_MUST_KEEP if _dropped(probes, n)]
        self.assertEqual(lost, [], "the probes scrape drops what the readiness rule reads")
        self.assertFalse(_dropped(self.rel, "prober_probe_duration_seconds_bucket"),
                         "a prober rule on the /metrics list reaches nothing; it belongs on the probes list")


class GrafanaSelfScrapeTest(unittest.TestCase):
    def test_grafanas_own_metrics_are_not_scraped(self):
        g = _values("kube-prometheus-stack.yaml")["grafana"]
        self.assertIs((g.get("serviceMonitor") or {}).get("enabled"), False)


class RenderedRulesReadNothingTheAllowListDropsTest(unittest.TestCase):
    """Render the chart with the platform values and check every rule expression that
    survives against the allow-list. Needs helm and the chart repository on the machine;
    without them this row says so instead of passing on nothing."""

    def test_no_rendered_rule_reads_a_family_the_apiserver_scrape_drops(self):
        manifest, why = render_chart()
        if manifest is None:
            self.skipTest(f"{why}; STILL_READ was rendered 2026-09-12")
        rel = _values("kube-prometheus-stack.yaml")["kubeApiServer"]["serviceMonitor"]["metricRelabelings"]
        readers: dict = {}
        recorded: set = set()
        selected: list = []
        groups = 0
        for d in yaml.safe_load_all(manifest):
            if not d or d.get("kind") != "PrometheusRule":
                continue
            for g in d["spec"]["groups"]:
                groups += 1
                for r in g["rules"]:
                    if r.get("record"):
                        # a chart record name may end in a colon (`node_namespace_pod:kube_pod_info:`)
                        recorded.add(str(r["record"]).rstrip(":"))
                    expr = r.get("expr", "")
                    for name in RECORDED_REF.findall(expr):
                        selected.append((g["name"], name.rstrip(":")))
                    for name, labels in SELECTOR.findall(expr):
                        if ":" not in name and 'job="apiserver"' in labels.replace(" ", ""):
                            selected.append((g["name"], name))
        self.assertTrue(selected, "no rendered rule selects on the apiserver job at all")
        for group, name in selected:
            if ":" in name:
                # a recording rule's output: some group that stays on must produce it
                if name not in recorded:
                    readers.setdefault(name, set()).add(group)
            elif _dropped(rel, name):
                readers.setdefault(name, set()).add(group)
        self.assertGreater(groups, 10, "the render carried no rule groups")
        self.assertEqual(readers, {}, "a rendered rule selects, on the apiserver job, a series nothing produces")


if __name__ == "__main__":
    unittest.main()
