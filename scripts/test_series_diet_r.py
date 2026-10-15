#!/usr/bin/env python3
"""
Series diet, review round 1 (fnd-ensnafw).

- ChartDefaultsCarriedTest: a ServiceMonitor metricRelabelings list REPLACES the chart's default
  list, so the diet's drop rules must carry the chart's own defaults forward. kube-prometheus-stack
  88.2.0 drops most `le` buckets of the API-server and etcd duration histograms on the apiserver
  job, and most buckets of the storage-operation histograms on the kubelet job. The 61k apiserver
  series were measured WITH those defaults in place; losing them adds series back.
- LivePlatformCheckTest: Tempo no longer draws a service graph and Grafana no longer asks for a
  service map, so the live platform check must stop asserting either.
"""
import importlib.util
import pathlib
import re
import sys
import unittest

HERE = pathlib.Path(__file__).resolve().parent
ROOT = HERE.parent
_SPEC = importlib.util.spec_from_file_location("test_series_diet", HERE / "test_series_diet.py")
_SD = importlib.util.module_from_spec(_SPEC)
sys.modules["test_series_diet"] = _SD
_SPEC.loader.exec_module(_SD)


def _dropped(relabelings, labels: dict) -> bool:
    """Prometheus relabeling of one series: each rule joins its sourceLabels with its separator
    (default ';'), anchors its regex, and drops (or keeps) on the match."""
    for r in relabelings or []:
        action = (r.get("action") or "replace").lower()
        if action not in ("drop", "keep"):
            continue
        src = r.get("sourceLabels") or r.get("source_labels") or []
        value = (r.get("separator") or ";").join(labels.get(l, "") for l in src)
        hit = re.fullmatch(r.get("regex", "(.*)"), value) is not None
        if (action == "drop" and hit) or (action == "keep" and not hit):
            return True
    return False


DURATION = ("apiserver_request_duration_seconds_bucket", "apiserver_request_sli_duration_seconds_bucket",
            "apiserver_request_slo_duration_seconds_bucket", "etcd_request_duration_seconds_bucket")
STORAGE = ("storage_operation_duration_seconds_bucket", "csi_operations_seconds_bucket")


class ChartDefaultsCarriedTest(unittest.TestCase):
    def setUp(self):
        self.v = _SD._values("kube-prometheus-stack.yaml")

    def test_apiserver_keeps_dropping_the_charts_duration_buckets(self):
        rel = self.v["kubeApiServer"]["serviceMonitor"]["metricRelabelings"]
        for name in DURATION:
            for le in ("0.15", "0.2", "2", "45", "50"):
                self.assertTrue(_dropped(rel, {"__name__": name, "le": le}), f"{name} le={le} came back")

    def test_apiserver_scrapes_no_bucket_the_burn_rate_rules_read_because_they_are_off(self):
        """Round two (2026-09-12): the SLO groups are off, so the buckets they read are not
        scraped either; a bucket that came back would be a series nobody reads."""
        rel = self.v["kubeApiServer"]["serviceMonitor"]["metricRelabelings"]
        rules = self.v["defaultRules"]["rules"]
        for group in ("kubeApiserverAvailability", "kubeApiserverBurnrate",
                      "kubeApiserverHistogram", "kubeApiserverSlos"):
            self.assertIs(rules.get(group), False, f"{group} is on")
        for name in ("apiserver_request_sli_duration_seconds_bucket", "apiserver_request_duration_seconds_bucket"):
            for le in ("1", "5", "30", "+Inf"):
                self.assertTrue(_dropped(rel, {"__name__": name, "le": le}), f"{name} le={le} scraped")

    def test_apiserver_still_drops_size_and_watch_histograms(self):
        rel = self.v["kubeApiServer"]["serviceMonitor"]["metricRelabelings"]
        for name in _SD.APISERVER_MUST_DROP:
            self.assertTrue(_dropped(rel, {"__name__": name, "le": "1"}), name)

    def test_kubelet_keeps_dropping_the_charts_storage_buckets(self):
        rel = self.v["kubelet"]["serviceMonitor"]["metricRelabelings"]
        for name in STORAGE:
            for le in ("0.25", "2.5", "15", "25", "120", "600"):
                self.assertTrue(_dropped(rel, {"__name__": name, "le": le}), f"{name} le={le} came back")
            self.assertFalse(_dropped(rel, {"__name__": name, "le": "+Inf"}), f"{name} +Inf dropped")


class LivePlatformCheckTest(unittest.TestCase):
    def test_the_live_check_no_longer_expects_a_service_graph_or_map(self):
        text = (ROOT / "scripts" / "verify" / "stage5-platform.sh").read_text()
        hits = [n for n, line in enumerate(text.splitlines(), 1)
                if re.search(r"traces_service_graph|serviceMap|service_map", line)]
        self.assertEqual(hits, [], "stage5-platform.sh lines still assert the retired service graph")

    def test_the_live_check_still_asserts_span_metrics(self):
        text = (ROOT / "scripts" / "verify" / "stage5-platform.sh").read_text()
        self.assertIn('count(traces_spanmetrics_calls_total{service="traefik"})', text)


if __name__ == "__main__":
    unittest.main()
