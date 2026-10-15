"""Whole-app sweep, area 5 (the instance template): static checks over template/ that the
adversarial rounds pinned. Run as `python3 -m pytest -q scripts/test_sweep_area5.py`."""
from __future__ import annotations

import pathlib
import unittest

import yaml

ROOT = pathlib.Path(__file__).resolve().parent.parent
NETPOL = ROOT / "template" / "provisioning" / "clusters" / "platform" / "config" / "monitoring" / "netpol.yaml"


def policies() -> list[dict]:
    return [d for d in yaml.safe_load_all(NETPOL.read_text()) if isinstance(d, dict)]


class MonitoringNetpolTest(unittest.TestCase):
    def test_the_exporters_answer_only_from_inside_the_namespace(self):
        """kube-state-metrics lists every namespace, pod, image, ingress host and Secret name
        in the cluster and blackbox-exporter fetches any URL it is asked to: neither may be
        reachable from a tenant pod (terminal-round finding, area 5)."""
        by_name = {p["metadata"]["name"]: p for p in policies()}
        self.assertIn("exporters-ingress", by_name)
        pol = by_name["exporters-ingress"]
        self.assertEqual(pol["metadata"]["namespace"], "monitoring")
        (expr,) = pol["spec"]["podSelector"]["matchExpressions"]
        self.assertEqual((expr["key"], expr["operator"]), ("app.kubernetes.io/name", "In"))
        self.assertLessEqual({"kube-state-metrics", "prometheus-blackbox-exporter"}, set(expr["values"]))
        self.assertEqual(pol["spec"]["policyTypes"], ["Ingress"])
        self.assertEqual(pol["spec"]["ingress"], [{"from": [{"podSelector": {}}]}],
                         "the exporters admit same-namespace callers and nothing else")

    def test_every_policy_here_is_ingress_only_and_namespaced_to_monitoring(self):
        for pol in policies():
            self.assertEqual(pol["kind"], "NetworkPolicy")
            self.assertEqual(pol["metadata"]["namespace"], "monitoring", pol["metadata"]["name"])
            self.assertEqual(pol["spec"]["policyTypes"], ["Ingress"], pol["metadata"]["name"])

    def test_the_header_does_not_claim_the_exporters_hold_no_data(self):
        self.assertNotIn("they hold no data", NETPOL.read_text())


if __name__ == "__main__":
    unittest.main()
