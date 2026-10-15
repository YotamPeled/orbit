"""Area-2b sweep, round 1 (2026-09-14): the catalog generator refuses by description.

One test per root cause: (R1) a refusal never repeats a catalog value that does not fit
its field's shape; (R2) the catalog's shape is checked once before the render loop, so a
scalar where a list belongs, a mistyped key, a component the chart would refuse and a
malformed document are refusals, not silent renders or tracebacks; (R3) a dashboard uid
is a claimed identity and never collides. Fixtures come from test_catalog_quota.Sandbox.
"""
from __future__ import annotations

import json
import pathlib
import sys
import unittest

import yaml

_BYTECODE, sys.dont_write_bytecode = sys.dont_write_bytecode, True
from test_catalog_quota import Sandbox  # noqa: E402
sys.dont_write_bytecode = _BYTECODE

MARKER = "PLANTED9d3f"
ESC = "\x1b[31m"


class GeneratorCase(unittest.TestCase):
    def setUp(self):
        self.sb = Sandbox()
        self.addCleanup(self.sb.close)

    def catalog(self) -> dict:
        return yaml.safe_load((self.sb.root / "catalog" / "projects.yaml").read_text())

    def write_catalog(self, cat) -> None:
        (self.sb.root / "catalog" / "projects.yaml").write_text(yaml.safe_dump(cat, sort_keys=False))

    def write_catalog_text(self, text: str) -> None:
        (self.sb.root / "catalog" / "projects.yaml").write_text(text)

    def instance(self) -> dict:
        return yaml.safe_load((self.sb.root / "instance" / "config.yaml").read_text())

    def write_instance(self, inst) -> None:
        (self.sb.root / "instance" / "config.yaml").write_text(yaml.safe_dump(inst, sort_keys=False))

    def generate(self, mutate=None):
        if mutate is not None:
            cat = self.catalog()
            mutate(cat)
            self.write_catalog(cat)
        proc = self.sb.generate()
        return proc, proc.stdout + proc.stderr

    def assert_refused(self, proc, out, *fragments):
        self.assertNotEqual(proc.returncode, 0, out)
        self.assertNotIn("Traceback", out, out)
        for fragment in fragments:
            self.assertIn(fragment, out, out)
        self.assertNotIn(MARKER, out, out)
        self.assertNotIn(MARKER.lower(), out, out)
        self.assertNotIn("\x1b", out, out)
        self.assertFalse(list((self.sb.root / "clusters" / "apps").glob("*.yaml")), out)

    @staticmethod
    def prod(cat, index=0):
        env = cat["projects"][index]["environments"]
        env["prod"] = env.get("prod") or {}
        return env["prod"]


class RefusalsDescribe(GeneratorCase):
    """R1: the value that failed its shape check is described, never repeated."""

    def test_a_hostname_carrying_userinfo_is_not_repeated(self):
        proc, out = self.generate(lambda cat: self.prod(cat).__setitem__(
            "hostnames", [f"https://admin:{MARKER}@example.org"]))
        self.assert_refused(proc, out, "hostnames[0]", "not a DNS name", "not of a hostname shape")

    def test_a_database_parameter_key_with_a_newline_cannot_forge_a_line(self):
        def mutate(cat):
            cat["projects"][0]["database"]["parameters"] = {f"work_mem\nFAKE-ROW: {MARKER} all checks passed\n": "4MB"}
        proc, out = self.generate(mutate)
        self.assert_refused(proc, out, "additional properties")
        self.assertNotIn("FAKE-ROW", out)

    def test_a_component_name_with_an_escape_is_described(self):
        proc, out = self.generate(lambda cat: cat["projects"][0]["components"][0].__setitem__(
            "name", f"{ESC}{MARKER}\nSTEP bump OK\nweb"))
        self.assert_refused(proc, out, "`name` must match")
        self.assertNotIn("STEP bump OK", out)

    def agent_runtime(self):
        # the agent plane needs the instance pin, or the generator refuses before it
        # reads any agent field at all
        inst = self.instance()
        inst["agentRuntime"] = {"image": "ghcr.io/example/agent", "tag": "main-0000000"}
        self.write_instance(inst)

    def test_a_token_shaped_agent_value_is_described(self):
        self.agent_runtime()
        def mutate(cat):
            cat["agents"] = [{"name": "sweeper", "prompt": "sweep",
                              "maxTurns": f"ghp_{MARKER}1234567890abcdefghijklmn"}]
        proc, out = self.generate(mutate)
        self.assert_refused(proc, out, "maxTurns must be an integer")
        self.assertNotIn("ghp_", out, out)

    def test_an_unknown_agent_key_is_described_not_repeated(self):
        self.agent_runtime()
        def mutate(cat):
            cat["agents"] = [{"name": "sweeper", "prompt": "sweep",
                              f"ghp_{MARKER}1234567890abcdefghijklmn": 1}]
        proc, out = self.generate(mutate)
        self.assert_refused(proc, out, "unknown keys")
        self.assertNotIn("ghp_", out, out)

        def trigger(cat):
            cat["agents"] = [{"name": "sweeper", "prompt": "sweep",
                              "triggers": [{"type": "a2a", f"FAKE-ROW: {MARKER}\nok": 1}]}]
        proc, out = self.generate(trigger)
        self.assert_refused(proc, out, "trigger type", "unknown keys")
        self.assertNotIn("FAKE-ROW", out, out)

    def test_mixed_type_unknown_keys_are_a_refusal_not_a_traceback(self):
        # PyYAML reads a bare `on:` as the boolean True; sorting it beside a str key
        # must not raise
        proc, out = self.generate(lambda cat: self.prod(cat).update({True: True, "hostname": "x.example.org"}))
        self.assert_refused(proc, out, "unknown environment key")
        def component_only(cat):
            self.prod(cat).pop(True, None); self.prod(cat).pop("hostname", None)
            cat["projects"][0]["components"][0].update({7: 1, "memroy": "1Gi"})
        proc, out = self.generate(component_only)
        self.assert_refused(proc, out, "unknown component key")

    def test_a_retention_value_with_a_newline_is_described(self):
        inst = self.instance()
        inst["objectStorage"] = {"buckets": {"platform": "fixture-bucket"},
                                 "endpoint": "https://s3.example.org",
                                 "retention": f"30x\nFAKE-ROW: {MARKER} retention accepted"}
        self.write_instance(inst)
        proc, out = self.generate()
        self.assert_refused(proc, out, "objectStorage.retention")
        self.assertNotIn("FAKE-ROW", out)

    def test_a_cidr_that_parsed_is_still_repeated(self):
        # the rule describes only what does not fit: a range the operator wrote is the
        # one thing that lets them find the line
        inst = self.instance()
        inst["egressTargets"] = {"s3": ["0.0.0.0/0"]}
        self.write_instance(inst)
        proc, out = self.generate()
        self.assertNotEqual(proc.returncode, 0, out)
        self.assertIn("'0.0.0.0/0'", out)


class CatalogShape(GeneratorCase):
    """R2: the shape of every catalog, tags and instance value is checked before any
    grammar check and before any output is staged."""

    def test_a_scalar_hostnames_is_refused_not_read_character_by_character(self):
        proc, out = self.generate(lambda cat: self.prod(cat).__setitem__("hostnames", "shop"))
        self.assert_refused(proc, out, "`hostnames` must be a list", "one character at a time")

    def test_a_scalar_dns_zone_list_is_refused(self):
        inst = self.instance()
        inst["domains"]["dnsZones"] = "managed.example.org"
        self.write_instance(inst)
        proc, out = self.generate()
        self.assert_refused(proc, out, "`dnsZones` must be a list")

    def test_a_bare_custom_domain_entry_is_refused_with_a_description(self):
        proc, out = self.generate(lambda cat: self.prod(cat).__setitem__("customDomains", ["api.example.org"]))
        self.assert_refused(proc, out, "`customDomains` entry must be a mapping with a string `host`")

    def test_an_unknown_environment_key_is_refused(self):
        proc, out = self.generate(lambda cat: self.prod(cat).__setitem__("hostname", ["shop.example.org"]))
        self.assert_refused(proc, out, "unknown environment key 'hostname'")

    def test_a_component_name_the_chart_rejects_is_refused(self):
        proc, out = self.generate(lambda cat: cat["projects"][0]["components"][0].__setitem__("name", "Web-API"))
        self.assert_refused(proc, out, "widgets/components[0]: `name` must match", "'Web-API'")

    def test_two_components_sharing_a_name_are_refused(self):
        proc, out = self.generate(lambda cat: cat["projects"][0]["components"][1].__setitem__("name", "web"))
        self.assert_refused(proc, out, "two components are named 'web'")

    def test_component_values_the_chart_schema_rejects_are_refused_first(self):
        for key, value, fragment in (
                ("image", "nginx:1.27", "does not match the chart's pattern"),
                ("tag", "v1 --oops", "does not match the chart's pattern"),
                ("port", "8080", "must be of type integer"),
                ("replicas", -3, "below the chart's minimum"),
                ("kind", "bogus", "is not one of")):
            with self.subTest(key=key):
                self.setUp()
                proc, out = self.generate(lambda cat: cat["projects"][0]["components"][0].__setitem__(key, value))
                self.assert_refused(proc, out, f"widgets/web: `{key}`", fragment)

    def test_an_unknown_component_key_is_refused(self):
        proc, out = self.generate(lambda cat: cat["projects"][0]["components"][0].__setitem__("memroy", "1Gi"))
        self.assert_refused(proc, out, "widgets/web: unknown component key 'memroy'")

    def test_a_bad_tag_in_the_tags_file_is_refused(self):
        (self.sb.root / "catalog" / "tags.yaml").write_text("tags:\n  widgets/prod/web: 'v1 --oops'\n")
        proc, out = self.generate()
        self.assert_refused(proc, out, "catalog/tags.yaml: widgets/prod/web: the tag", "not a valid image tag")

    def test_malformed_documents_are_refused_without_a_traceback(self):
        cases = {
            "empty file": "",
            "no projects key": "version: 3\n",
            "projects as a scalar": "projects: widgets\n",
            "project without id": "projects:\n  - name: Widgets\n",
            "environments as a list": "projects:\n  - id: widgets\n    environments: [prod]\n",
            "component without name": "projects:\n  - id: widgets\n    environments: {prod: {}}\n    components: [{kind: web}]\n",
            "database as a scalar": "projects:\n  - id: widgets\n    database: big\n",
            "secrets as a scalar": "projects:\n  - id: widgets\n    secrets: app\n",
        }
        for label, text in cases.items():
            with self.subTest(label=label):
                self.setUp()
                self.write_catalog_text(text)
                proc, out = self.generate()
                self.assertNotEqual(proc.returncode, 0, out)
                self.assertNotIn("Traceback", out, out)
                self.assertIn("must ", out, out)

    def test_a_list_tags_file_is_refused_without_a_traceback(self):
        (self.sb.root / "catalog" / "tags.yaml").write_text("- widgets/prod/web\n")
        proc, out = self.generate()
        self.assert_refused(proc, out, "catalog/tags.yaml must be a mapping")

    def test_the_fixture_catalog_still_renders(self):
        proc, out = self.generate()
        self.assertEqual(proc.returncode, 0, out)
        self.assertTrue((self.sb.root / "clusters" / "apps" / "widgets-prod.yaml").exists())


class ChartShapeBeyondTheComponent(GeneratorCase):
    """Round 2: the overlay, `secrets[]` and `database` face the chart schema too, and
    the shape pass refuses (does not traceback on) the documents the breaker built."""

    def test_an_overlay_value_the_chart_refuses_is_refused_first(self):
        for value, fragment in (("2", "must be of type integer"), (-3, "below the chart's minimum"),
                                ("two", "must be of type integer")):
            with self.subTest(value=value):
                def mutate(cat, value=value):
                    comp = cat["projects"][0]["components"][0]["name"]
                    self.prod(cat)["components"] = {comp: {"replicas": value}}
                proc, out = self.generate(mutate)
                self.assert_refused(proc, out, "replicas", fragment)

    def test_a_secret_entry_the_chart_refuses_is_refused_first(self):
        for entry, fragment in (({"name": 5}, "secrets[0].name"), ({"name": "app", "kys": ["A"]}, "unknown key"),
                                ({"keys": ["A"]}, "must carry `name`")):
            with self.subTest(entry=entry):
                proc, out = self.generate(lambda cat, entry=entry: cat["projects"][0].__setitem__("secrets", [entry]))
                self.assert_refused(proc, out, fragment)

    def test_a_database_value_the_chart_refuses_is_refused_first(self):
        proc, out = self.generate(lambda cat: cat["projects"][0].__setitem__("database", {"size": [1]}))
        self.assert_refused(proc, out, "database.size", "must be of type string")
        proc, out = self.generate(lambda cat: cat["projects"][0].__setitem__(
            "database", {"size": "1Gi", "parameters": {"max_connections": 60}}))
        self.assertEqual(proc.returncode, 0, out)

    def test_a_start_anchored_chart_pattern_keeps_search_semantics(self):
        # `metrics.path` is `^/` in the chart; a full match would refuse every live path
        def mutate(cat):
            cat["projects"][0]["components"][0]["metrics"] = {"port": 9090, "path": "/metrics"}
        proc, out = self.generate(mutate)
        self.assertEqual(proc.returncode, 0, out)

    def test_a_trailing_newline_is_refused_as_helm_would(self):
        # Python's `$` matches before a trailing newline; helm's Go matcher does not
        original = dict(self.catalog()["projects"][0]["components"][0])
        for key, value in (("image", "ghcr.io/example/web\n"), ("probePath", "/healthz\n")):
            with self.subTest(key=key):
                def mutate(cat, key=key, value=value):
                    comp = cat["projects"][0]["components"][0]
                    comp.clear()
                    comp.update(original)
                    comp[key] = value
                proc, out = self.generate(mutate)
                self.assert_refused(proc, out, key, "does not match the chart's pattern")
        proc, out = self.generate(lambda cat: cat["projects"][0].__setitem__("secrets", [{"name": "app\n"}]))
        self.assert_refused(proc, out, "secrets[0].name", "does not match the chart's pattern")

    def test_a_component_without_an_image_is_refused(self):
        proc, out = self.generate(lambda cat: cat["projects"][0]["components"][0].pop("image"))
        self.assert_refused(proc, out, "must carry `image`")

    def test_egress_keeps_its_own_refusal(self):
        proc, out = self.generate(lambda cat: cat["projects"][0]["components"][0].__setitem__(
            "egress", {"hosts": ["api.example.org"]}))
        self.assert_refused(proc, out, "egress.hosts", "shared egress proxy")

    def test_a_showcase_project_without_a_name_is_refused(self):
        def mutate(cat):
            cat["projects"][0].pop("name", None)
            cat["projects"][0]["showcase"] = True
        proc, out = self.generate(mutate)
        self.assert_refused(proc, out, "must carry a `name`")

    def test_a_non_string_environment_name_is_refused(self):
        proc, out = self.generate(lambda cat: cat["projects"][0]["environments"].__setitem__(True, {"enabled": True}))
        self.assert_refused(proc, out, "environment name")

    def test_a_bad_hostname_is_located_by_key_and_index(self):
        proc, out = self.generate(lambda cat: self.prod(cat).__setitem__(
            "hostnames", ["a.example.org", f"bad_{MARKER}.example.org"]))
        self.assert_refused(proc, out, "hostnames[1]", "not a DNS name")
        proc, out = self.generate(lambda cat: self.prod(cat).update(
            {"hostnames": ["a.example.org"], "customDomains": [{"host": f"shop_{MARKER}.example.com"}]}))
        self.assert_refused(proc, out, "customDomains[0].host", "not a DNS name")


class DashboardUid(GeneratorCase):
    """R3: a uid that does not fit Grafana's 40 is digest-backed, and every uid is a
    claimed identity."""

    def uid(self, pid, env):
        docs = list(yaml.safe_load_all((self.sb.root / "clusters" / "apps" / f"dashboard-{pid}-{env}.yaml").read_text()))
        doc = [d for d in docs if d][0]
        return json.loads(next(iter(doc["data"].values())))["uid"]

    def test_two_environments_of_a_long_project_id_keep_distinct_uids(self):
        pid = "customer-portal-notifications"          # 29 chars: the readable uid would truncate
        def mutate(cat):
            cat["projects"][0]["id"] = pid
        proc, out = self.generate(mutate)
        self.assertEqual(proc.returncode, 0, out)
        prod, dev = self.uid(pid, "prod"), self.uid(pid, "dev")
        self.assertNotEqual(prod, dev)
        for uid in (prod, dev):
            self.assertLessEqual(len(uid), 40)
            self.assertRegex(uid, r"^orbit-red-[0-9a-f]{30}$")

    def test_a_digest_collision_between_two_dashboard_uids_is_a_render_error(self):
        # the claim is the backstop behind the digest: inject a constant digest through
        # the render's seam and the two environments must refuse, naming both
        import contextlib, importlib.util, io
        pid = "customer-portal-notifications"
        cat = self.catalog(); cat["projects"][0]["id"] = pid; self.write_catalog(cat)
        root = self.sb.root
        spec = importlib.util.spec_from_file_location(f"catalog_sync_uid_{id(self)}", root / "scripts" / "catalog-sync.py")
        module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
        module.ROOT, module.CATALOG, module.TAGS = root, root / "catalog" / "projects.yaml", root / "catalog" / "tags.yaml"
        module.APPS_DIR, module.ORBIT_DIR, module.INSTANCE = root / "clusters" / "apps", root / "clusters" / "orbit", root / "instance" / "config.yaml"
        module._INST_CACHE = None
        err = io.StringIO()
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(err):
            with self.assertRaises(SystemExit) as cm:
                module.main(probe_digest=lambda payload: "0" * 64)
        message = str(cm.exception.code) + err.getvalue()
        self.assertIn("generated identity collision for Grafana dashboard uid", message)
        self.assertIn("'prod'", message); self.assertIn("'dev'", message)

    def test_a_short_project_id_keeps_the_readable_uid(self):
        proc, out = self.generate()
        self.assertEqual(proc.returncode, 0, out)
        self.assertEqual(self.uid("widgets", "prod"), "orbit-red-widgets-prod")


class Stage3DashboardRow(unittest.TestCase):
    """The verify stage's dashboard row accepts the generator's uid rule and nothing wider."""

    def row(self):
        text = (pathlib.Path(__file__).resolve().parent / "verify" / "stage3.sh").read_text()
        start = text.index("for f in clusters/apps/dashboard-*.yaml; do")
        start = text.index('python3 -c "\n', start) + len('python3 -c "\n')
        return text[start:text.index('\n" "$f" 2>/dev/null', start)]

    def run_row(self, stem, uid):
        import subprocess, tempfile
        d = pathlib.Path(tempfile.mkdtemp(prefix="stage3-dash-"))
        self.addCleanup(__import__("shutil").rmtree, d, True)
        f = d / f"dashboard-{stem}.yaml"
        f.write_text(yaml.safe_dump({"kind": "ConfigMap", "metadata": {"labels": {"grafana_dashboard": "1"}},
                                     "data": {"d.json": json.dumps({"uid": uid, "panels": [{"id": 1}]})}}))
        return subprocess.run([sys.executable, "-c", self.row(), str(f)], capture_output=True, text=True, timeout=60).returncode

    def test_a_digest_backed_uid_for_a_long_stem_passes_and_an_overlong_readable_one_fails(self):
        stem = "customer-portal-notifications-prod"
        self.assertEqual(self.run_row(stem, "orbit-red-" + "0123456789abcdef0123456789abcd"), 0)
        self.assertNotEqual(self.run_row(stem, f"orbit-red-{stem}"), 0)
        self.assertNotEqual(self.run_row("widgets-prod", "orbit-red-" + "0123456789abcdef0123456789abcd"), 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
