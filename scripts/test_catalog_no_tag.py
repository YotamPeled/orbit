"""A component nobody has shipped yet is written with NO tag (docs/V1.md §8).

Until 7594615 the generator wrote `tag: latest` for a component missing from
catalog/tags.yaml, so the chart's rule that idles an untagged component (zero replicas,
a suspended CronJob — test_service_chart.UntaggedComponentTest) never saw one: prod's
untagged engine and bot pulled `:latest`, and a stuck bot Job blocked the CronJob. Runs
the real catalog-sync.py on the quota testdata catalog (no component tagged), then once
more with one component tagged.
"""
from __future__ import annotations

import unittest

import yaml

from test_catalog_quota import Sandbox


def component_values(box: Sandbox, project: str, env: str) -> dict[str, dict]:
    """name -> the values the generator wrote for that component in the pair's app."""
    found: dict[str, dict] = {}
    for path in sorted((box.root / "clusters").rglob("*.yaml")):
        for doc in yaml.safe_load_all(path.read_text(encoding="utf-8")):
            if not isinstance(doc, dict) or doc.get("kind") != "Application":
                continue
            src = (doc.get("spec") or {}).get("source") or {}
            helm = src.get("helm") or {}
            raw = helm.get("values")
            vals = yaml.safe_load(raw) if isinstance(raw, str) else (helm.get("valuesObject") or {})
            if not isinstance(vals, dict) or vals.get("project") not in (None, project):
                continue
            if vals.get("env") not in (None, env) or (doc["metadata"].get("name") or "").find(f"{project}-{env}") < 0:
                continue
            for c in vals.get("components") or []:
                found[c["name"]] = c
    return found


class NoTagBeforeTheFirstShip(unittest.TestCase):
    def setUp(self):
        self.box = Sandbox()
        self.addCleanup(self.box.close)

    def generate(self):
        proc = self.box.generate()
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)

    def test_an_unshipped_component_carries_no_tag(self):
        self.generate()
        comps = component_values(self.box, "widgets", "prod")
        self.assertIn("web", comps, f"no rendered widgets-prod components found: {sorted(comps)}")
        for name, c in comps.items():
            self.assertNotIn("tag", c, f"{name}: an unshipped component was written with tag {c.get('tag')!r}")

    def test_a_shipped_component_carries_its_tag_and_its_sibling_none(self):
        (self.box.root / "catalog" / "tags.yaml").write_text(
            "tags:\n  widgets/prod/web: main-abc1234\n", encoding="utf-8")
        self.generate()
        comps = component_values(self.box, "widgets", "prod")
        self.assertEqual(comps["web"].get("tag"), "main-abc1234")
        self.assertNotIn("tag", comps["importer"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
