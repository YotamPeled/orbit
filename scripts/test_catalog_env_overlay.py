"""An environment overlay may carry `env` (docs/V1.md §8, S08a): a credential one
environment needs and another must not reference — a dev-only secret — had no way to be
declared, because a component's env renders into every environment. The overlay merges
key by key over the component's env; null removes a key there; nothing else moves."""
from __future__ import annotations

import pathlib
import sys
import unittest

import yaml

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from test_catalog_quota import Sandbox  # noqa: E402


def web_env(box: Sandbox, app: str) -> dict:
    (doc,) = box.docs(app)
    values = doc["spec"]["source"]["helm"]["valuesObject"]
    web = next(c for c in values["components"] if c["name"] == "web")
    return web.get("env") or {}


class EnvOverlayTest(unittest.TestCase):
    def setUp(self):
        self.box = Sandbox()
        self.addCleanup(self.box.close)
        path = self.box.root / "catalog" / "projects.yaml"
        self.catalog = yaml.safe_load(path.read_text())
        self.widgets = next(p for p in self.catalog["projects"] if p["id"] == "widgets")
        web = next(c for c in self.widgets["components"] if c["name"] == "web")
        web["env"] = {"LOG_LEVEL": "info", "FEATURE_X": "on"}
        self.widgets["secrets"] = [{"name": "widgets-app", "keys": ["API_TOKEN"]}]

    def generate(self, dev_overlay):
        self.widgets["environments"]["dev"]["components"] = {"web": dev_overlay}
        (self.box.root / "catalog" / "projects.yaml").write_text(yaml.safe_dump(self.catalog, sort_keys=False))
        return self.box.generate()

    def test_a_dev_only_secret_reference_renders_in_dev_and_not_in_prod(self):
        proc = self.generate({"env": {"API_TOKEN": {"secret": "widgets-app", "key": "API_TOKEN"}}})
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        dev, prod = web_env(self.box, "widgets-dev.yaml"), web_env(self.box, "widgets-prod.yaml")
        self.assertEqual(dev, {"LOG_LEVEL": "info", "FEATURE_X": "on",
                               "API_TOKEN": {"secret": "widgets-app", "key": "API_TOKEN"}})
        self.assertEqual(prod, {"LOG_LEVEL": "info", "FEATURE_X": "on"})

    def test_an_overlay_value_replaces_and_a_null_removes_in_that_env_only(self):
        proc = self.generate({"env": {"LOG_LEVEL": "debug", "FEATURE_X": None}, "memory": "384Mi"})
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertEqual(web_env(self.box, "widgets-dev.yaml"), {"LOG_LEVEL": "debug"})
        self.assertEqual(web_env(self.box, "widgets-prod.yaml"), {"LOG_LEVEL": "info", "FEATURE_X": "on"})

    def test_an_env_overlay_that_is_not_a_mapping_is_refused(self):
        proc = self.generate({"env": ["API_TOKEN"]})
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("widgets/dev/components/web: `env` must be of type object", proc.stdout + proc.stderr)


if __name__ == "__main__":
    unittest.main(verbosity=2)
