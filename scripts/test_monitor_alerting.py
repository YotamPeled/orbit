#!/usr/bin/env python3
"""The platform half of monitors: where a monitor alert is delivered, and the wiring that
lets orbitd apply monitor objects and receive their transitions.

Routing is asserted by SIMULATING Alertmanager over the template's config with
scripts/verify/am-routes.py (first match wins, `continue`, fail-closed matcher parsing),
never by grepping for strings. A monitor alert carries the labels orbitd's renderer puts
on it: orbit_monitor, orbit_notify (telegram|none), severity, orbit_severity, project, env.
"""
from __future__ import annotations

import importlib.util
import pathlib
import re
import unittest

import yaml

ROOT = pathlib.Path(__file__).resolve().parent.parent
PROV = ROOT / "template" / "provisioning" / "clusters" / "platform"
KPS = PROV / "apps" / "kube-prometheus-stack.yaml"
LOKI = PROV / "apps" / "loki.yaml"
RBAC = PROV / "config" / "orbitd" / "rbac.yaml"
DEPLOY = PROV / "config" / "orbitd" / "deployment.yaml"
NETPOL = ROOT / "template" / "provisioning" / "clusters" / "platform" / "config" / "monitoring" / "netpol.yaml"

SECRET = "orbit-alert-webhook"
WEBHOOK_URL = "http://orbitd.orbit.svc.cluster.local/v1/alertmanager"
AM_TOKEN_FILE = f"/etc/alertmanager/secrets/{SECRET}/token"
ORBITD_TOKEN_DIR = "/etc/orbit-alert-webhook"

_spec = importlib.util.spec_from_file_location("am_routes", ROOT / "scripts" / "verify" / "am-routes.py")
am = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(am)


def docs(path: pathlib.Path) -> list[dict]:
    text = re.sub(r"\{\{[^{}]*\}\}", "PLACEHOLDER", path.read_text())
    return [d for d in yaml.safe_load_all(text) if isinstance(d, dict)]


def monitor_alert(notify="telegram", severity="critical", orbit_severity="page"):
    return {"alertname": "queue-backlog", "severity": severity,
            "orbit_severity": orbit_severity, "orbit_monitor": "queue-backlog",
            "orbit_notify": notify, "project": "demo", "env": "prod"}


class MonitorRoutingTest(unittest.TestCase):
    def setUp(self):
        self.values = am.load_am("app", KPS)
        self.cfg = self.values["config"]

    def receivers(self, labels) -> list[str]:
        return am.receivers_for(self.cfg, labels)

    def test_a_telegram_monitor_alert_reaches_telegram_and_orbitd(self):
        for sev, osev in (("critical", "page"), ("warning", "warn"), ("info", "info")):
            with self.subTest(severity=sev):
                self.assertEqual(sorted(self.receivers(monitor_alert("telegram", sev, osev))),
                                 ["orbitd", "telegram"])

    def test_a_monitor_alert_without_telegram_reaches_orbitd_only(self):
        self.assertEqual(self.receivers(monitor_alert("none")), ["orbitd"])

    def test_the_agent_plane_and_watchdog_routes_are_unchanged(self):
        agent = am.alert_labels(am.SAMPLE)
        self.assertEqual(self.receivers(agent), ["orbit-intake"])
        self.assertEqual(self.receivers({"alertname": "Watchdog", "severity": "none"}), ["null"])

    def test_a_platform_alert_still_pages_telegram_only(self):
        labels = {"alertname": "KubePodCrashLooping", "severity": "warning", "namespace": "x"}
        self.assertEqual(self.receivers(labels), ["telegram"])

    def test_the_orbitd_receiver_posts_to_the_webhook_with_a_file_bearer(self):
        (recv,) = [r for r in self.cfg["receivers"] if r.get("name") == "orbitd"]
        (hook,) = recv["webhook_configs"]
        self.assertEqual(hook["url"], WEBHOOK_URL)
        self.assertTrue(hook.get("send_resolved"))
        auth = hook["http_config"]["authorization"]
        self.assertEqual(auth.get("credentials_file"), AM_TOKEN_FILE)
        self.assertNotIn("credentials", auth, "the bearer must come from a file, never inline")

    def test_alertmanager_mounts_the_webhook_secret(self):
        self.assertIn(SECRET, self.values["alertmanagerSpec"]["secrets"])


class OrbitdWiringTest(unittest.TestCase):
    def test_orbitd_reads_the_webhook_token_from_an_optional_secret_mount(self):
        (dep,) = [d for d in docs(DEPLOY) if d.get("kind") == "Deployment"]
        spec = dep["spec"]["template"]["spec"]
        (orbitd,) = [c for c in spec["containers"] if c["name"] == "orbitd"]
        env = {e["name"]: e.get("value") for e in orbitd["env"]}
        self.assertEqual(env.get("ORBITD_ALERT_WEBHOOK_TOKEN_FILE"), f"{ORBITD_TOKEN_DIR}/token")
        mounts = {m["name"]: m for m in orbitd["volumeMounts"]}
        self.assertEqual(mounts["alert-webhook"]["mountPath"], ORBITD_TOKEN_DIR)
        self.assertTrue(mounts["alert-webhook"].get("readOnly"))
        vols = {v["name"]: v for v in spec["volumes"]}
        self.assertEqual(vols["alert-webhook"]["secret"]["secretName"], SECRET)
        self.assertTrue(vols["alert-webhook"]["secret"].get("optional"),
                        "orbitd must start before the token exists; the route answers 401")

    def test_orbitd_may_write_the_monitors_loki_configmap_and_nothing_else_there(self):
        rbac = docs(RBAC)
        roles = [d for d in rbac if d.get("kind") == "Role"
                 and d["metadata"].get("namespace") == "loki"]
        self.assertEqual(len(roles), 1, "one Role for orbitd in loki")
        (role,) = roles
        for rule in role["rules"]:
            self.assertEqual(rule.get("apiGroups"), [""])
            self.assertEqual(rule.get("resources"), ["configmaps"])
        verbs = {v for r in role["rules"] for v in r["verbs"]}
        self.assertTrue({"create", "patch", "delete"} <= verbs)
        self.assertFalse({"*", "escalate", "bind", "impersonate"} & verbs)
        bindings = [d for d in rbac if d.get("kind") == "RoleBinding"
                    and d["metadata"].get("namespace") == "loki"
                    and d["roleRef"]["name"] == role["metadata"]["name"]]
        self.assertEqual(len(bindings), 1)
        self.assertEqual(bindings[0]["subjects"],
                         [{"kind": "ServiceAccount", "name": "orbitd", "namespace": "orbit"}])

    def test_orbitd_may_reach_alertmanager(self):
        (pol,) = [d for d in docs(NETPOL) if d["metadata"]["name"] == "alertmanager-ingress"]
        admitted = False
        for rule in pol["spec"].get("ingress") or []:
            ports = {p.get("port") for p in rule.get("ports") or []}
            for peer in rule.get("from") or []:
                ns = ((peer.get("namespaceSelector") or {}).get("matchLabels") or {})
                if ns.get("kubernetes.io/metadata.name") == "orbit" and 9093 in ports:
                    admitted = True
        self.assertTrue(admitted, "orbit_monitor_silence and orbit_alerts_active call :9093")


class LokiRulesMountTest(unittest.TestCase):
    def test_the_ruler_reads_both_rule_configmaps_optionally(self):
        (app,) = docs(LOKI)
        vals = app["spec"]["source"]["helm"].get("valuesObject") or yaml.safe_load(
            re.sub(r"\{\{[^{}]*\}\}", "PLACEHOLDER", app["spec"]["source"]["helm"]["values"]))
        single = vals["singleBinary"]
        mounts = [m for m in single["extraVolumeMounts"] if m["mountPath"] == "/etc/loki/rules/fake"]
        self.assertEqual(len(mounts), 1)
        (vol,) = [v for v in single["extraVolumes"] if v["name"] == mounts[0]["name"]]
        sources = {s["configMap"]["name"]: s["configMap"] for s in vol["projected"]["sources"]}
        self.assertEqual(set(sources), {"orbit-agents-logs-triggers", "orbit-monitors-logs"})
        for name, cm in sources.items():
            self.assertTrue(cm.get("optional"), f"{name} must be optional: an absent one would wedge Loki")


if __name__ == "__main__":
    unittest.main()
