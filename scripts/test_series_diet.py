#!/usr/bin/env python3
"""
Phase 8 series diet (DESIGN-v2 §7), sized from what the live instance measured on 2026-09-10.

Prometheus held 188k head series. The kubelet job scraped 96k of them, 89k from /metrics: on
k3s the kubelet, API server and etcd are one process, so the kubelet endpoint re-exports every
API-server and etcd histogram the apiserver job already scrapes. The apiserver job's own 61k
are dominated by request/response size and watch histograms that no rule reads. The eBPF floor, which the
design blamed, was 2.8k.

So the diet is:
- drop the API-server, etcd and workqueue families from the kubelet /metrics scrape (they are
  duplicates), keeping the kubelet's own families the default rules and dashboards read;
- drop the size and watch histograms from the apiserver scrape (round one); round two,
  2026-09-12 (test_series_diet_two.py), turns the four API-server SLO rule groups off and
  keeps only what the remaining rules read, by allow-list;
- Prometheus retention 7d, request 768Mi; Tempo generates span-metrics only (service-graphs off,
  and Grafana's Tempo datasource stops asking for a service map), request 384Mi.

Run as `python3 scripts/test_series_diet.py` or under pytest.
"""
import pathlib
import re
import unittest

import yaml

ROOT = pathlib.Path(__file__).resolve().parent.parent
APPS = ROOT / "template" / "provisioning" / "clusters" / "platform" / "apps"

KUBELET_MUST_DROP = [
    "apiserver_request_duration_seconds_bucket",
    "apiserver_request_sli_duration_seconds_bucket",
    "apiserver_request_body_size_bytes_bucket",
    "apiserver_response_sizes_bucket",
    "apiserver_watch_list_duration_seconds_bucket",
    "apiserver_watch_cache_read_wait_seconds_bucket",
    "apiserver_watch_events_sizes_bucket",
    "apiserver_request_total",
    "etcd_request_duration_seconds_bucket",
    "workqueue_work_duration_seconds_bucket",
    "workqueue_queue_duration_seconds_bucket",
]
KUBELET_MUST_KEEP = [
    "up",
    "kubelet_running_pods",
    "kubelet_running_containers",
    "kubelet_node_name",
    "kubelet_pleg_relist_duration_seconds_bucket",
    "kubelet_pod_worker_duration_seconds_bucket",
    "kubelet_volume_stats_available_bytes",
    "kubelet_volume_stats_capacity_bytes",
    "kubelet_volume_stats_inodes_free",
    "kubelet_certificate_manager_client_ttl_seconds",
    "kubelet_server_expiration_renew_errors",
    "rest_client_requests_total",
    "rest_client_request_duration_seconds_bucket",
    "storage_operation_duration_seconds_bucket",
    "storage_operation_errors_total",
    "volume_manager_total_volumes",
    "process_cpu_seconds_total",
    "process_resident_memory_bytes",
    "go_goroutines",
    "prober_probe_total",
]
APISERVER_MUST_DROP = [
    # Round two: the histograms only the four SLO rule groups read, and the families the
    # k3s process re-exports on this job.
    "apiserver_request_duration_seconds_bucket",
    "apiserver_request_duration_seconds_count",
    "apiserver_request_sli_duration_seconds_bucket",
    "apiserver_request_sli_duration_seconds_count",
    "apiserver_request_slo_duration_seconds_bucket",
    "etcd_request_duration_seconds_bucket",
    "etcd_requests_total",
    "workqueue_work_duration_seconds_bucket",
    "workqueue_queue_duration_seconds_bucket",
    "workqueue_depth",
    "scheduler_plugin_execution_duration_seconds_bucket",
    "apiserver_admission_controller_admission_duration_seconds_bucket",
    "apiserver_current_inflight_requests",
    "apiserver_request_body_size_bytes_bucket",
    "apiserver_response_sizes_bucket",
    "apiserver_watch_list_duration_seconds_bucket",
    "apiserver_watch_cache_read_wait_seconds_bucket",
    "apiserver_watch_events_sizes_bucket",
]
APISERVER_MUST_KEEP = [
    # Round two (2026-09-12): the four API-server SLO rule groups are off, so the SLI and
    # duration histograms, the workqueue and scheduler families went with them. What stays
    # is what the kubernetes-system-apiserver group and the general rules read.
    "up",
    "apiserver_request_total",
    "apiserver_request_terminations_total",
    "apiserver_client_certificate_expiration_seconds_bucket",
    "apiserver_client_certificate_expiration_seconds_count",
    "aggregator_unavailable_apiservice",
    "aggregator_unavailable_apiservice_total",
    "rest_client_requests_total",
    "process_cpu_seconds_total",
    "process_resident_memory_bytes",
    "go_goroutines",
]

_TOKEN = re.compile(r"\{\{[^}]*\}\}")


def _values(app: str) -> dict:
    """The Helm values of a platform Application, with {{tokens}} blanked so it parses."""
    text = _TOKEN.sub("x", (APPS / app).read_text())
    doc = next(d for d in yaml.safe_load_all(text) if d and d.get("kind") == "Application")
    helm = doc["spec"]["source"]["helm"]
    values = helm.get("valuesObject") or helm.get("values")
    return yaml.safe_load(values) if isinstance(values, str) else values


def _mi(q: str) -> float:
    m = re.fullmatch(r"(\d+(?:\.\d+)?)(Mi|Gi)", str(q))
    if not m:
        raise AssertionError(f"memory quantity {q!r} is not in Mi or Gi")
    return float(m.group(1)) * (1024 if m.group(2) == "Gi" else 1)


def _dropped(relabelings, name: str) -> bool:
    """Whether a metric name is dropped by a Prometheus metric relabeling list: a `drop`
    on __name__ (regex anchored, as Prometheus anchors it) or a `keep` it does not match."""
    for r in relabelings or []:
        src = r.get("sourceLabels") or r.get("source_labels") or []
        if src != ["__name__"]:
            continue
        rx = re.compile(r.get("regex", "(.*)"))
        hit = rx.fullmatch(name) is not None
        action = (r.get("action") or "replace").lower()
        if action == "drop" and hit:
            return True
        if action == "keep" and not hit:
            return True
    return False


class PrometheusTest(unittest.TestCase):
    def setUp(self):
        self.v = _values("kube-prometheus-stack.yaml")
        self.spec = self.v["prometheus"]["prometheusSpec"]

    def test_retention_is_seven_days(self):
        self.assertEqual(self.spec["retention"], "7d")
        self.assertIn("retentionSize", self.spec, "the byte bound stays")

    def test_request_is_768Mi_with_burst_room(self):
        res = self.spec["resources"]
        self.assertEqual(res["requests"]["memory"], "768Mi")
        self.assertGreaterEqual(_mi(res["limits"]["memory"]), 1024,
                                "a limit at the request would OOM on a compaction spike")

    def test_kubelet_scrape_drops_the_duplicated_apiserver_families(self):
        sm = self.v["kubelet"]["serviceMonitor"]
        rel = sm.get("metricRelabelings")
        self.assertTrue(rel, "kubelet.serviceMonitor.metricRelabelings is empty")
        kept = [n for n in KUBELET_MUST_DROP if not _dropped(rel, n)]
        self.assertEqual(kept, [], "kubelet /metrics still keeps duplicated families")
        lost = [n for n in KUBELET_MUST_KEEP if _dropped(rel, n)]
        self.assertEqual(lost, [], "kubelet /metrics drops a family a default rule reads")

    def test_kubelet_target_relabelings_still_drop_ipv6(self):
        sm = self.v["kubelet"]["serviceMonitor"]
        for key in ("relabelings", "cAdvisorRelabelings", "probesRelabelings", "resourceRelabelings"):
            rules = sm.get(key) or []
            self.assertTrue(any(r.get("action") == "drop" and "__address__" in (r.get("sourceLabels") or [])
                                for r in rules), f"{key} lost the IPv6 drop")
            self.assertTrue(any(r.get("targetLabel") == "metrics_path" for r in rules),
                            f"{key} lost the metrics_path label")

    def test_cadvisor_scrape_keeps_the_memory_and_oom_series(self):
        rel = self.v["kubelet"]["serviceMonitor"].get("cAdvisorMetricRelabelings")
        for n in ("container_memory_working_set_bytes", "container_oom_events_total",
                  "container_cpu_usage_seconds_total"):
            self.assertFalse(_dropped(rel, n), n)

    def test_apiserver_scrape_drops_size_and_watch_histograms(self):
        rel = ((self.v.get("kubeApiServer") or {}).get("serviceMonitor") or {}).get("metricRelabelings")
        self.assertTrue(rel, "kubeApiServer.serviceMonitor.metricRelabelings is empty")
        kept = [n for n in APISERVER_MUST_DROP if not _dropped(rel, n)]
        self.assertEqual(kept, [], "apiserver scrape still keeps unread histograms")
        lost = [n for n in APISERVER_MUST_KEEP if _dropped(rel, n)]
        self.assertEqual(lost, [], "apiserver scrape drops a family a default rule reads")


class TempoTest(unittest.TestCase):
    def setUp(self):
        self.v = _values("tempo.yaml")["tempo"]

    def test_generator_emits_span_metrics_only(self):
        procs = self.v["overrides"]["defaults"]["metrics_generator"]["processors"]
        self.assertEqual(procs, ["span-metrics"])

    def test_request_is_384Mi_and_the_flush_ceiling_stays(self):
        res = self.v["resources"]
        self.assertEqual(res["requests"]["memory"], "384Mi")
        self.assertGreaterEqual(_mi(res["limits"]["memory"]), 1024,
                                "the block-flush peak was measured at 509Mi; keep the 1Gi ceiling")

    def test_grafana_no_longer_asks_tempo_for_a_service_map(self):
        g = _values("kube-prometheus-stack.yaml")["grafana"]
        # Diet three moved the datasources from the sidecar's additionalDataSources to
        # the provisioning file; the entry is the same.
        entries = g["datasources"]["datasources.yaml"]["datasources"]
        tempo = [d for d in entries if d.get("type") == "tempo"]
        self.assertEqual(len(tempo), 1)
        self.assertNotIn("serviceMap", tempo[0].get("jsonData") or {})


if __name__ == "__main__":
    unittest.main()
