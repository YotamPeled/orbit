"""The platform alert rules under promtool (docs/V1.md M2b: every rule has an offline
`promtool test rules` case in CI).

The rules ship as a PrometheusRule (template/provisioning/.../monitoring/rules.yaml);
promtool reads plain rule files, so the test extracts spec.groups beside the cases in
scripts/testdata/rules/orbit-rules.test.yaml and runs promtool over them. It also refuses
an alert that no case names, so a new rule cannot ship untested, and proves the cases bite
by running them against a deliberately broken rule.

promtool is required in CI (the tests workflow installs it). Elsewhere a missing promtool
skips, saying so; a check that cannot run is never reported as a pass.
"""
from __future__ import annotations

import os
import pathlib
import shutil
import subprocess
import tempfile
import unittest

import yaml

ROOT = pathlib.Path(__file__).resolve().parent.parent
RULES = ROOT / "template" / "provisioning" / "clusters" / "platform" / "monitoring" / "rules.yaml"
CASES = ROOT / "scripts" / "testdata" / "rules" / "orbit-rules.test.yaml"
PROMTOOL = shutil.which("promtool")


def groups(text: str) -> list[dict]:
    doc = yaml.safe_load(text)
    assert doc.get("kind") == "PrometheusRule", "rules.yaml is not a PrometheusRule"
    return doc["spec"]["groups"]


def alert_names(gs: list[dict]) -> set[str]:
    return {r["alert"] for g in gs for r in g.get("rules", []) if "alert" in r}


def run_promtool(gs: list[dict]) -> subprocess.CompletedProcess:
    with tempfile.TemporaryDirectory() as td:
        d = pathlib.Path(td)
        (d / "orbit-rules.yaml").write_text(yaml.safe_dump({"groups": gs}, sort_keys=False), encoding="utf-8")
        shutil.copy(CASES, d / CASES.name)
        return subprocess.run([PROMTOOL, "test", "rules", CASES.name], cwd=d,
                              capture_output=True, text=True, timeout=120)


class CoverageTest(unittest.TestCase):
    def test_every_alert_has_a_case(self):
        cases = yaml.safe_load(CASES.read_text(encoding="utf-8"))
        named = {t["alertname"] for test in cases["tests"] for t in test.get("alert_rule_test", [])}
        missing = sorted(alert_names(groups(RULES.read_text(encoding="utf-8"))) - named)
        self.assertEqual(missing, [], f"alert rules with no promtool case: {missing}")

    def test_every_alert_fires_in_some_case(self):
        cases = yaml.safe_load(CASES.read_text(encoding="utf-8"))
        fired = {t["alertname"] for test in cases["tests"] for t in test.get("alert_rule_test", [])
                 if t.get("exp_alerts")}
        silent = sorted(alert_names(groups(RULES.read_text(encoding="utf-8"))) - fired)
        self.assertEqual(silent, [], f"alerts no case expects to fire: {silent}")


class PromtoolTest(unittest.TestCase):
    def setUp(self):
        if PROMTOOL is None:
            if os.environ.get("CI"):
                self.fail("promtool is not installed in CI: the rule tests CANNOT run, which is not a pass")
            self.skipTest("promtool is not installed here")

    def test_the_cases_pass_against_the_shipped_rules(self):
        proc = run_promtool(groups(RULES.read_text(encoding="utf-8")))
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("SUCCESS", proc.stdout + proc.stderr)

    def test_the_cases_catch_the_memory_budget_going_back_to_working_set(self):
        # the false alarm docs/V1.md §8 fixed: on working set the over-budget case still fires
        # but the tenant-excluded case is computed from a series the inputs do not carry, so
        # the expected alert disappears and promtool must fail
        gs = groups(RULES.read_text(encoding="utf-8"))
        for g in gs:
            for r in g.get("rules", []):
                if r.get("alert") == "OrbitPlatformMemoryOverBudget":
                    r["expr"] = r["expr"].replace("container_memory_rss", "container_memory_working_set_bytes")
        proc = run_promtool(gs)
        self.assertNotEqual(proc.returncode, 0, "the cases did not notice the rule reading working set")

    def test_the_cases_catch_a_threshold_change(self):
        gs = groups(RULES.read_text(encoding="utf-8"))
        for g in gs:
            for r in g.get("rules", []):
                if r.get("alert") == "NodeDiskLow":
                    r["expr"] = r["expr"].replace("< 0.30", "< 0.10")
        proc = run_promtool(gs)
        self.assertNotEqual(proc.returncode, 0, "the cases did not notice NodeDiskLow's threshold moving")

    def test_the_cases_catch_the_memory_at_limit_rule_firing_on_a_spike_or_without_a_tenant(self):
        # docs/V1.md M4b: each half of OrbitWorkloadMemoryAtLimit is held by a case
        def broken(change) -> subprocess.CompletedProcess:
            gs = groups(RULES.read_text(encoding="utf-8"))
            for g in gs:
                for r in g.get("rules", []):
                    if r.get("alert") == "OrbitWorkloadMemoryAtLimit":
                        before = dict(r)
                        change(r)
                        self.assertNotEqual(r, before, "the mutation did not touch the rule")
            return run_promtool(gs)

        def shorter(r): r["for"] = "3m"
        def lower(r): r["expr"] = r["expr"].replace("> 0.9", "> 0.3")
        def everyone(r): r["expr"] = r["expr"].replace('{label_orbit_local_project!=""}', "")

        self.assertNotEqual(broken(shorter).returncode, 0, "the cases did not notice a spike paging")
        self.assertNotEqual(broken(lower).returncode, 0, "the cases did not notice the threshold moving")
        self.assertNotEqual(broken(everyone).returncode, 0, "the cases did not notice platform namespaces paging")


if __name__ == "__main__":
    unittest.main()
