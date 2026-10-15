#!/usr/bin/env python3
"""
Diet three (phase 8 acceptance, 2026-09-12).

Measured after diets one and two: at the 18:00 head compaction the head fell from 95k to
27k series and Prometheus's working set stayed at 720 MiB -- live heap 278 MiB, next
collection set at 485 MiB. The collector's target set the working set, not the series: two
scrapes (the kubelet's /metrics and the API server, one k3s registry served twice) deliver
93k samples each per cycle that the allow-lists drop after parsing. The dashboard server's
datasource sidecar held 72 MiB to watch every namespace for a ConfigMap only the chart
itself ever wrote.

So: the Prometheus resource carries a collector target of 30% over the live heap and a soft
memory limit at half the container limit; the four datasources are provisioned from the
values file with their uids unchanged, and the datasource sidecar is off.

Run as `python3 scripts/test_diet_three.py` or under pytest.
"""
import pathlib
import sys
import unittest

import yaml

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from test_series_diet import _values  # noqa: E402
from test_series_diet_two import render_chart  # noqa: E402

# Every generated panel and every correlation link names one of these, in this order.
UIDS = ["prometheus", "alertmanager", "loki", "tempo"]
LABEL_DATASOURCE = "grafana_datasource"


class CollectorTargetTest(unittest.TestCase):
    def setUp(self):
        self.spec = _values("kube-prometheus-stack.yaml")["prometheus"]["prometheusSpec"]

    def test_the_target_is_thirty_percent_over_the_live_heap(self):
        self.assertEqual(self.spec["runtime"], {"goGC": 30})

    def test_the_soft_limit_is_half_the_container_limit(self):
        self.assertIn({"name": "auto-gomemlimit.ratio", "value": "0.5"}, self.spec["additionalArgs"])
        # Half of the limit is the request: the runtime returns freed pages above what
        # the platform reserved for it, and the hard ceiling is untouched.
        self.assertEqual(self.spec["resources"]["limits"]["memory"], "1536Mi")
        self.assertEqual(self.spec["resources"]["requests"]["memory"], "768Mi")


class DatasourcesFromTheFileTest(unittest.TestCase):
    def setUp(self):
        self.g = _values("kube-prometheus-stack.yaml")["grafana"]
        self.ds = self.g["datasources"]["datasources.yaml"]["datasources"]

    def test_the_datasource_sidecar_is_off_and_nothing_still_feeds_it(self):
        self.assertEqual(self.g["sidecar"]["datasources"], {"enabled": False})
        # additionalDataSources reaches Grafana only through the sidecar's ConfigMap;
        # an entry left there would be silently unread.
        self.assertNotIn("additionalDataSources", self.g)
        self.assertTrue(self.g["sidecar"]["dashboards"]["enabled"])

    def test_the_four_datasources_keep_their_uids(self):
        self.assertEqual([d["uid"] for d in self.ds], UIDS)

    def test_the_default_datasource_matches_what_the_chart_wrote(self):
        prom = self.ds[0]
        self.assertTrue(prom["isDefault"])
        self.assertEqual(prom["jsonData"]["httpMethod"], "POST")
        self.assertEqual(prom["jsonData"]["timeInterval"], self.spec_scrape_interval())
        self.assertEqual(prom["jsonData"]["exemplarTraceIdDestinations"],
                         [{"datasourceUid": "tempo", "name": "trace_id"}])

    def spec_scrape_interval(self) -> str:
        return _values("kube-prometheus-stack.yaml")["prometheus"]["prometheusSpec"]["scrapeInterval"]

    def test_the_correlation_links_still_point_at_each_other(self):
        loki, tempo = self.ds[2], self.ds[3]
        self.assertEqual(loki["jsonData"]["derivedFields"][0]["datasourceUid"], "tempo")
        self.assertEqual(tempo["jsonData"]["tracesToLogsV2"]["datasourceUid"], "loki")
        self.assertEqual(tempo["jsonData"]["tracesToMetrics"]["datasourceUid"], "prometheus")
        self.assertEqual(tempo["jsonData"]["lokiSearch"]["datasourceUid"], "loki")


class RenderedShapeTest(unittest.TestCase):
    """Rendered under the release name the platform uses, so the hand-written datasource
    URLs are held to the Services the chart actually names."""

    @classmethod
    def setUpClass(cls):
        manifest, why = render_chart(release="kube-prometheus-stack")
        if manifest is None:
            raise unittest.SkipTest(why)
        cls.docs = [d for d in yaml.safe_load_all(manifest) if d]

    def _one(self, kind: str, name: str) -> dict:
        return next(d for d in self.docs if d["kind"] == kind and d["metadata"]["name"] == name)

    def test_the_prometheus_resource_carries_the_target_and_the_soft_limit(self):
        spec = self._one("Prometheus", "kube-prometheus-stack-prometheus")["spec"]
        self.assertEqual(spec["runtime"], {"goGC": 30})
        self.assertEqual(spec["additionalArgs"], [{"name": "auto-gomemlimit.ratio", "value": "0.5"}])

    def test_grafana_runs_one_sidecar_and_reads_the_file(self):
        pod = self._one("Deployment", "kube-prometheus-stack-grafana")["spec"]["template"]["spec"]
        self.assertEqual({c["name"] for c in pod["containers"]}, {"grafana", "grafana-sc-dashboard"})
        grafana = next(c for c in pod["containers"] if c["name"] == "grafana")
        self.assertIn("/etc/grafana/provisioning/datasources/datasources.yaml",
                      [m["mountPath"] for m in grafana["volumeMounts"]])
        labelled = [d["metadata"]["name"] for d in self.docs if d["kind"] == "ConfigMap"
                    and LABEL_DATASOURCE in (d["metadata"].get("labels") or {})]
        self.assertEqual(labelled, [])

    def test_the_datasource_urls_name_rendered_services(self):
        cm = self._one("ConfigMap", "kube-prometheus-stack-grafana")
        ds = yaml.safe_load(cm["data"]["datasources.yaml"])["datasources"]
        self.assertEqual([d["uid"] for d in ds], UIDS)
        services = {d["metadata"]["name"] for d in self.docs if d["kind"] == "Service"}
        for entry in ds[:2]:
            host = entry["url"].split("//", 1)[1].split("/", 1)[0]
            name, namespace = host.split(":")[0].split(".")[:2]
            self.assertIn(name, services, entry["url"])
            self.assertEqual(namespace, "monitoring")


if __name__ == "__main__":
    unittest.main()
