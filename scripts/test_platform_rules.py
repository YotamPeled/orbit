#!/usr/bin/env python3
"""Platform default monitors (DESIGN-v2 §7): the alerts every instance ships without an
agent declaring anything. Node reservation headroom, quota headroom, certificate expiry,
WAL archive, restarts and OOMKills, ingress 5xx rate, and the platform memory budget.

Some of these already exist (the orbit PrometheusRule and the chart's stock rules); these
tests hold them in place and pin the three that are new. PromQL is not evaluated here: the
live acceptance runs every expression against the instance's Prometheus."""
from __future__ import annotations

import pathlib
import re
import unittest

import yaml

ROOT = pathlib.Path(__file__).resolve().parent.parent
RULES = ROOT / "template" / "provisioning" / "clusters" / "platform" / "monitoring" / "rules.yaml"
KPS = ROOT / "template" / "provisioning" / "clusters" / "platform" / "apps" / "kube-prometheus-stack.yaml"


def alerts() -> dict[str, dict]:
    doc = yaml.safe_load(RULES.read_text())
    out = {}
    for group in doc["spec"]["groups"]:
        for rule in group["rules"]:
            if "alert" in rule:
                out[rule["alert"]] = rule
    return out


def kps_values() -> dict:
    app = yaml.safe_load(KPS.read_text())
    text = re.sub(r"\{\{[^}]*\}\}", "placeholder", app["spec"]["source"]["helm"]["values"])
    return yaml.safe_load(text)


def expr(rule) -> str:
    return " ".join(str(rule["expr"]).split())


def minutes(duration: str) -> int:
    m = re.fullmatch(r"(\d+)([mh])", duration)
    assert m, duration
    return int(m.group(1)) * (60 if m.group(2) == "h" else 1)


class PlatformDefaultsTest(unittest.TestCase):
    def test_every_default_the_design_lists_is_present(self):
        have = alerts()
        for name in ("NodeDiskLow", "PlatformContainerRestarted", "OrbitCertificateExpiringSoon",
                     "OrbitWALArchiveStalled", "OrbitNodeReservationHigh",
                     "OrbitIngress5xxRateHigh", "OrbitPlatformMemoryOverBudget"):
            self.assertIn(name, have)

    def test_quota_headroom_comes_from_the_charts_stock_rules(self):
        rules = kps_values()["defaultRules"]
        self.assertTrue(rules["create"])
        self.assertNotEqual((rules.get("rules") or {}).get("kubernetesResources"), False)
        for name in ("KubeQuotaAlmostFull", "KubeQuotaFullyUsed"):
            self.assertFalse((rules.get("disabled") or {}).get(name), name)

    def test_every_orbit_rule_has_a_known_severity_and_a_summary(self):
        for name, rule in alerts().items():
            self.assertIn(rule["labels"]["severity"], ("critical", "warning", "info"), name)
            self.assertTrue(rule["annotations"]["summary"], name)

    def test_node_reservation_headroom(self):
        rule = alerts()["OrbitNodeReservationHigh"]
        e = expr(rule)
        self.assertIn("kube_node_status_allocatable", e)
        self.assertIn("kube_pod_container_resource_requests", e)
        self.assertRegex(e, r"by \(node")
        self.assertIn('"memory"', e)
        self.assertIn('"cpu"', e)
        self.assertRegex(e, r">\s*0\.9\b")
        self.assertEqual(rule["labels"]["severity"], "warning")
        self.assertGreaterEqual(minutes(rule["for"]), 15)

    def test_ingress_5xx_rate(self):
        rule = alerts()["OrbitIngress5xxRateHigh"]
        e = expr(rule)
        self.assertIn("traefik_service_requests_total", e)
        self.assertIn('code=~"5.."', e)
        self.assertRegex(e, r"by \(service\)")
        self.assertRegex(e, r">\s*0\.05\b")
        # a traffic floor, so one failed request out of two does not page
        self.assertRegex(e, r"\band\b")
        self.assertEqual(rule["labels"]["severity"], "warning")
        self.assertGreaterEqual(minutes(rule["for"]), 10)

    def test_platform_memory_budget_counts_platform_namespaces_only(self):
        rule = alerts()["OrbitPlatformMemoryOverBudget"]
        e = expr(rule)
        # RSS, never the working set: the working set counts page cache, which a reboot
        # refills and which made this alert fire for memory nobody held (docs/V1.md §8)
        self.assertIn("container_memory_rss", e)
        self.assertNotIn("working_set", e)
        self.assertIn("2147483648", e)
        # tenant namespaces carry orbit.local/project; they are not the platform
        self.assertIn("kube_namespace_labels", e)
        self.assertIn("label_orbit_local_project", e)
        self.assertEqual(rule["labels"]["severity"], "warning")
        # a breach must persist half an hour before anyone hears of it: the averaging
        # window plus the hold. A boot's WAL replay is minutes of real RSS over budget
        # (docs/V1.md §8), so the window averages it away instead of going pending.
        window = re.search(r"avg_over_time\(.*\)\[(\d+)m:\d+[sm]\]\)", e, re.S)
        self.assertIsNotNone(window, "the budget is judged on an average, not the bare sum")
        self.assertGreaterEqual(int(window.group(1)), 10)
        self.assertGreaterEqual(int(window.group(1)) + minutes(rule["for"]), 30)

    def test_kube_state_metrics_exports_the_tenant_label_on_namespaces(self):
        ksm = kps_values()["kube-state-metrics"]
        allow = ksm.get("metricLabelsAllowlist") or []
        entries = [a for a in allow if a.startswith("namespaces=[")]
        self.assertEqual(len(entries), 1, allow)
        self.assertIn("orbit.local/project", entries[0])

    def test_a_tenant_workload_at_its_memory_limit_is_named_by_project_env_and_component(self):
        # docs/V1.md M4b: a runtime that fills its limit fails inside it with no kill and
        # no restart, so nothing else here fires. promtool evaluates it
        # (test_rules_promtool.py); this holds its shape and what it depends on.
        rule = alerts()["OrbitWorkloadMemoryAtLimit"]
        e = expr(rule)
        self.assertIn("container_memory_working_set_bytes", e)
        self.assertIn('kube_pod_container_resource_limits{resource="memory"}', e)
        self.assertIn("> 0.9", e)
        self.assertEqual(minutes(rule["for"]), 15)
        self.assertEqual(rule["labels"]["severity"], "warning")
        # tenants only, and the labels an agent needs to call a tool with
        self.assertIn('kube_namespace_labels{label_orbit_local_project!=""}', e)
        for label in ('"project"', '"env"', '"component"'):
            self.assertIn(label, e)
        self.assertIn("orbit_size_recommend", rule["annotations"]["description"])
        # the env label reaches Prometheus only because kube-state-metrics is told to export it
        allow = kps_values()["kube-state-metrics"]["metricLabelsAllowlist"]
        self.assertIn("orbit.local/env", "".join(allow))


if __name__ == "__main__":
    unittest.main()
