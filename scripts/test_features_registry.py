"""The features registry (template/provisioning/features.yaml) against the tree it describes
(docs/V1.md M2c): every platform file has exactly one owner, every name the registry
gives exists, and an instance rendered with a feature off carries none of it."""
import json
import os
import pathlib
import re
import shutil
import subprocess
import sys
import tempfile
import unittest

import yaml

import orbit_features as features
import test_instance_init as init_tests

REPO = pathlib.Path(__file__).resolve().parent.parent
TEMPLATE = REPO / "template"
STAGED = TEMPLATE / "provisioning" / "clusters"
STATIC = TEMPLATE / "clusters"
REGISTRY = features.load_registry(TEMPLATE)
OPTIONAL = [name for name, spec in REGISTRY.items() if not spec["always"]]


def dependents(name: str) -> set:
    """Every optional feature that cannot be on without `name`, directly or through another."""
    found, grew = set(), True
    while grew:
        grew = False
        for other, spec in REGISTRY.items():
            if other not in found and other != name and (set(spec["requires"]) & (found | {name})):
                found.add(other); grew = True
    return found


def platform_files() -> list:
    """Every YAML file an instance carries under clusters/platform, staged or static."""
    found = set()
    for base in (STAGED, STATIC):
        for f in (base / "platform").rglob("*.yaml"):
            found.add(f.relative_to(base).as_posix())
    return sorted(found)


def filled(text: str) -> str:
    """A staged file with its `{{tokens}}` stood in for, so it parses the way its
    rendered copy does."""
    return init_tests.TOKEN_RE.sub("x", text)


def applications() -> dict:
    """{Application name: file} for every platform Application the template ships."""
    apps = {}
    for base in (STAGED, STATIC):
        for f in sorted((base / "platform" / "apps").glob("*.yaml")):
            for doc in yaml.safe_load_all(filled(features.apply_blocks(
                    f.read_text(encoding="utf-8"), {n: True for n in REGISTRY}))):
                if isinstance(doc, dict) and doc.get("kind") == "Application":
                    apps[doc["metadata"]["name"]] = f.relative_to(base).as_posix()
    return apps


def registered_tools() -> set:
    """Every MCP tool orbitd registers, read from the decorated function names."""
    names = set()
    for f in (REPO / "services" / "orbitd" / "orbitd").rglob("*.py"):
        text = f.read_text(encoding="utf-8")
        for m in re.finditer(r"@registry\.tool\([^)]*\)\s*(?:async\s+)?def\s+(orbit_[a-z0-9_]+)", text):
            names.add(m.group(1))
    return names


class EveryPlatformFileHasOneOwner(unittest.TestCase):
    def test_no_file_is_unowned_or_owned_twice(self):
        for rel in platform_files():
            with self.subTest(file=rel):
                self.assertEqual(len(features.owners(rel, REGISTRY)), 1,
                                 f"{rel} is owned by {features.owners(rel, REGISTRY) or 'no feature'}: "
                                 f"name it under exactly one feature's paths in provisioning/features.yaml")

    def test_every_path_the_registry_names_exists(self):
        files = platform_files()
        for name, spec in REGISTRY.items():
            for path in spec["paths"]:
                with self.subTest(feature=name, path=path):
                    self.assertTrue(any(f == path or f.startswith(path + "/") for f in files),
                                    f"feature {name} owns {path}, which no template file is under")

    def test_an_optional_features_files_are_staged(self):
        """A static file is copied once and never re-rendered, so nothing could take it
        back out of an instance when its feature is turned off."""
        for name in OPTIONAL:
            for rel in platform_files():
                if features.owners(rel, REGISTRY) == [name]:
                    with self.subTest(feature=name, file=rel):
                        self.assertTrue((STAGED / rel).is_file(),
                                        f"{rel} belongs to the optional feature {name} but is static "
                                        f"(template/clusters): move it under template/provisioning/clusters")

    def test_a_block_only_sits_in_a_staged_file(self):
        for f in (STATIC / "platform").rglob("*.yaml"):
            with self.subTest(file=f.relative_to(STATIC).as_posix()):
                self.assertEqual(features.block_features(f.read_text(encoding="utf-8")), set(),
                                 "a static file is never rendered, so its feature block would never be cut")


class EveryNameTheRegistryGivesExists(unittest.TestCase):
    def test_every_app_is_an_application_in_a_file_the_feature_owns(self):
        apps = applications()
        for name, spec in REGISTRY.items():
            for app in spec["apps"]:
                with self.subTest(feature=name, app=app):
                    self.assertIn(app, apps, f"feature {name} names the app {app}, which no file declares")
                    self.assertEqual(features.owners(apps[app], REGISTRY), [name],
                                     f"app {app} is declared in {apps[app]}, which {name} does not own")

    def test_every_application_belongs_to_a_feature(self):
        owned = {app for spec in REGISTRY.values() for app in spec["apps"]}
        self.assertEqual(sorted(set(applications()) - owned), [],
                         "these Applications are in no feature's `apps`")

    def test_every_tool_is_registered_and_owned_once(self):
        tools = registered_tools()
        self.assertGreater(len(tools), 20, "the tool scan found too few tools to be believed")
        seen = {}
        for name, spec in REGISTRY.items():
            for tool in spec["tools"]:
                with self.subTest(feature=name, tool=tool):
                    self.assertIn(tool, tools, f"feature {name} names the tool {tool}, which orbitd does not register")
                    self.assertNotIn(tool, seen, f"{tool} is owned by {seen.get(tool)} and {name}")
                    seen[tool] = name


class TheVerifyStagesKeyOnRegistryFeatures(unittest.TestCase):
    def test_every_feature_variable_a_stage_reads_is_a_feature(self):
        """A stage row keyed on FEATURE_<NAME> for a name the registry does not have
        would read 0 forever and key its rows off on every instance."""
        known = {n.upper().replace("-", "_") for n in OPTIONAL} | {"APPS"}
        for stage in sorted((REPO / "scripts" / "verify").glob("stage*.sh")):
            used = set(re.findall(r"FEATURE_([A-Z_]+)", stage.read_text(encoding="utf-8")))
            with self.subTest(stage=stage.name):
                self.assertEqual(sorted(used - known), [])


class ReadingFeatures(unittest.TestCase):
    BUCKET = {"objectStorage": {"endpoint": "https://x.example", "buckets": {"platform": "b"}}}

    def test_without_a_features_block_every_feature_with_its_needs_met_is_on(self):
        on = features.states(dict(self.BUCKET), REGISTRY)
        self.assertTrue(all(on.values()), on)
        on = features.states({}, REGISTRY)
        for name in OPTIONAL:
            self.assertEqual(on[name], "bucket" not in REGISTRY[name]["requires"], name)

    def test_with_a_features_block_only_what_it_turns_on_is_on(self):
        on = features.states({**self.BUCKET, "features": {"logs": True}}, REGISTRY)
        self.assertEqual({n for n in OPTIONAL if on[n]}, {"logs"})
        on = features.states({**self.BUCKET, "features": None}, REGISTRY)
        self.assertEqual({n for n in OPTIONAL if on[n]}, set(), "an empty block is the minimal platform")
        self.assertTrue(on["core"])

    def test_a_feature_without_its_requirement_is_refused_by_name(self):
        for name in OPTIONAL:
            if "bucket" in REGISTRY[name]["requires"]:
                with self.subTest(feature=name), self.assertRaises(features.FeatureError) as cm:
                    features.states({"features": {name: True}}, REGISTRY)
                self.assertIn(f"features.{name}", str(cm.exception))
                self.assertIn("bucket", str(cm.exception))

    def test_what_is_not_a_switch_is_refused(self):
        for block, word in (({"lgos": True}, "lgos"), ({"core": False}, "core"),
                            ({"logs": "yes"}, "true or false"), (["logs"], "mapping")):
            with self.subTest(block=block), self.assertRaises(features.FeatureError) as cm:
                features.states({**self.BUCKET, "features": block}, REGISTRY)
            self.assertIn(word, str(cm.exception))


class Blocks(unittest.TestCase):
    TEXT = "a\n  # orbit:feature logs\nb\n    # orbit:feature traces\nc\n    # orbit:end traces\nd\n  # orbit:end logs\ne\n"

    def test_a_block_is_kept_only_while_every_feature_around_it_is_on(self):
        cut = lambda logs, traces: features.apply_blocks(self.TEXT, {"logs": logs, "traces": traces})
        self.assertEqual(cut(True, True), "a\nb\nc\nd\ne\n")
        self.assertEqual(cut(True, False), "a\nb\nd\ne\n")
        self.assertEqual(cut(False, True), "a\ne\n")
        self.assertEqual(cut(False, False), "a\ne\n")

    def test_a_block_that_cannot_be_read_is_refused_with_its_line(self):
        on = {"logs": True, "traces": True}
        for text, word in (("# orbit:feature logs\nx\n", "never closed"),
                           ("# orbit:end logs\n", "not open"),
                           ("# orbit:feature nope\nx\n# orbit:end nope\n", "names no feature"),
                           ("# orbit:feature logs\n# orbit:feature traces\n# orbit:end logs\n", "not open")):
            with self.subTest(text=text), self.assertRaises(features.FeatureError) as cm:
                features.apply_blocks(text, on, "f.yaml")
            self.assertIn(word, str(cm.exception))

    def test_every_staged_file_reads_with_every_combination(self):
        """Each staged file, cut for every on/off combination of the features it carries
        blocks for, is still YAML."""
        import itertools
        for f in sorted(STAGED.rglob("*.yaml")):
            text = f.read_text(encoding="utf-8")
            named = sorted(features.block_features(text))
            for combo in itertools.product([True, False], repeat=len(named)):
                on = {n: True for n in REGISTRY} | dict(zip(named, combo))
                with self.subTest(file=f.relative_to(STAGED).as_posix(), on=dict(zip(named, combo))):
                    list(yaml.safe_load_all(filled(features.apply_blocks(text, on, str(f)))))


class AnInstanceRenderedWithAFeatureOff(unittest.TestCase):
    """The scaffolder over a real fixture, each optional feature off and on."""

    def setUp(self):
        self.tmp = pathlib.Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def render(self, block, name="i"):
        config = dict(init_tests.BASE_CONFIG)
        config["objectStorage"] = {"provider": "hetzner", "endpoint": "https://fsn1.example.com",
                                   "buckets": {"platform": "fixture-platform"}}
        if block is not None:
            config["features"] = block
        root = self.tmp / name
        root.mkdir()
        fx = init_tests.InitFixture(root, config)
        proc = fx.run()
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        return fx, proc

    def test_off_none_of_its_files_or_lines_exist_and_on_all_do(self):
        everything = {n: True for n in OPTIONAL}
        full, _ = self.render(everything, "full")
        for name in OPTIONAL:
            # a feature goes off together with whatever needs it (backups needs databases)
            fx, _ = self.render({**everything, **{n: False for n in dependents(name) | {name}}}, name)
            owned = [rel for rel in platform_files() if features.owners(rel, REGISTRY) == [name]]
            staged_texts = [f.read_text(encoding="utf-8") for f in sorted(STAGED.rglob("*.yaml"))]
            in_blocks = any(name in features.block_features(text) for text in staged_texts)
            as_value = any("{{features.%s}}" % name in text for text in staged_texts)
            self.assertTrue(owned or in_blocks or as_value,
                            f"feature {name} owns no file, no block and no value: switching it changes nothing")
            for rel in owned:
                with self.subTest(feature=name, file=rel):
                    self.assertTrue(full.rendered(rel).is_file(), f"{rel} is not rendered with {name} on")
                    self.assertFalse(fx.rendered(rel).exists(), f"{rel} is rendered with {name} off")
            # every line the feature owns inside another feature's file is gone, and
            # nothing else in that file moved
            for src in sorted(STAGED.rglob("*.yaml")):
                rel = src.relative_to(STAGED).as_posix()
                text = src.read_text(encoding="utf-8")
                if name not in features.block_features(text) or not fx.rendered(rel).exists():
                    continue
                with self.subTest(feature=name, shared_file=rel):
                    on_lines = full.rendered(rel).read_text().splitlines()
                    off_lines = fx.rendered(rel).read_text().splitlines()
                    self.assertLess(len(off_lines), len(on_lines))
                    self.assertFalse([l for l in off_lines if l not in on_lines],
                                     "turning a feature off added lines")
                    list(yaml.safe_load_all(fx.rendered(rel).read_text()))

    def test_no_marker_line_reaches_the_rendered_tree(self):
        fx, _ = self.render({n: True for n in OPTIONAL})
        for f in (fx.root / "clusters").rglob("*.yaml"):
            self.assertNotIn("# orbit:feature", f.read_text(), f)
            self.assertNotIn("# orbit:end", f.read_text(), f)

    def test_turning_a_feature_off_removes_what_it_rendered_and_says_why(self):
        fx, _ = self.render(None)
        self.assertTrue(fx.rendered("platform/apps/tempo.yaml").is_file())
        fx.write_config({"features": {"logs": True}})
        proc = fx.run()
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertFalse(fx.rendered("platform/apps/tempo.yaml").exists())
        self.assertTrue(fx.rendered("platform/apps/loki.yaml").is_file())
        self.assertIn("the traces feature is off", proc.stdout)

    def test_a_feature_turned_on_without_its_bucket_is_refused_and_nothing_is_written(self):
        config = dict(init_tests.BASE_CONFIG)
        config.pop("objectStorage", None)
        config["features"] = {"logs": True}
        root = self.tmp / "nobucket"
        root.mkdir()
        fx = init_tests.InitFixture(root, config)
        before = fx.snapshot()
        proc = fx.run()
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("features.logs is on but it needs a bucket", proc.stdout + proc.stderr)
        self.assertEqual(fx.snapshot(), before, "a refused config still wrote files")

    def uptime_off(self, name, mutate):
        config = dict(init_tests.BASE_CONFIG)
        config["features"] = {n: False for n in OPTIONAL}
        root = self.tmp / name
        root.mkdir()
        fx = init_tests.InitFixture(root, config)
        self.assertEqual(fx.run().returncode, 0)
        mutate(fx)
        before = fx.snapshot()
        proc = fx.run()
        return fx, before, proc

    def test_a_probe_with_uptime_off_is_refused_before_anything_is_written(self):
        """An extra probe, or a probe monitor, with no prober to run it is a check that
        looks declared and never runs."""
        def extra(fx):
            fx.write_config({"monitoring": {"probes": [{"url": "https://x.fixture.org/health", "module": "http_2xx"}]}})

        def monitor(fx):
            (fx.root / "monitors").mkdir(exist_ok=True)
            (fx.root / "monitors" / "door.yaml").write_text(yaml.safe_dump(
                {"name": "door", "kind": "probe", "target": "https://x.fixture.org/health"}))
            (fx.root / "monitors" / "rate.yaml").write_text(yaml.safe_dump({"name": "rate", "kind": "metric"}))

        for name, mutate, says in (("extra", extra, "monitoring.probes is declared and the uptime feature is off"),
                                   ("monitor", monitor, "1 probe monitor(s) need it (monitors/door.yaml)")):
            with self.subTest(case=name):
                fx, before, proc = self.uptime_off(name, mutate)
                self.assertNotEqual(proc.returncode, 0, proc.stdout)
                self.assertIn(says, proc.stdout + proc.stderr)
                self.assertEqual(fx.snapshot(), before, "a refused config still wrote files")

    def test_a_monitor_that_notifies_telegram_is_refused_while_the_feature_is_off(self):
        def monitor(fx):
            (fx.root / "monitors").mkdir(exist_ok=True)
            (fx.root / "monitors" / "rate.yaml").write_text(yaml.safe_dump(
                {"name": "rate", "kind": "metric", "notify": ["telegram"]}))
            (fx.root / "monitors" / "quiet.yaml").write_text(yaml.safe_dump(
                {"name": "quiet", "kind": "metric", "notify": []}))
        fx, before, proc = self.uptime_off("notify", monitor)
        self.assertNotEqual(proc.returncode, 0, proc.stdout)
        self.assertIn("the alerts-telegram feature is off and 1 monitor(s) notify telegram (monitors/rate.yaml)",
                      proc.stdout + proc.stderr)
        self.assertEqual(fx.snapshot(), before, "a refused config still wrote files")

    KPS = "platform/apps/kube-prometheus-stack.yaml"

    def alertmanager(self, fx):
        app = yaml.safe_load(fx.rendered(self.KPS).read_text())
        values = yaml.safe_load(app["spec"]["source"]["helm"]["values"]) if "helm" in app["spec"].get("source", {}) \
            else yaml.safe_load(app["spec"]["sources"][0]["helm"]["values"])
        return values["alertmanager"]

    def test_telegram_off_needs_no_chat_and_renders_no_notifier(self):
        """Off: the human receiver keeps its name and its delivery record, loses the
        notifier and the token mount; and no chat has to be declared."""
        on, _ = self.render({n: True for n in OPTIONAL}, "tg-on")
        am = self.alertmanager(on)
        human = {r["name"]: r for r in am["config"]["receivers"]}["telegram"]
        self.assertIn("telegram_configs", human)
        self.assertIn("alertmanager-telegram", am["alertmanagerSpec"]["secrets"])

        config = dict(init_tests.BASE_CONFIG)
        config["contacts"] = {**config.get("contacts", {}), "telegram": False}
        config["contacts"].pop("telegramChatId", None)
        config["features"] = {n: False for n in OPTIONAL}
        root = self.tmp / "tg-off"
        root.mkdir()
        off = init_tests.InitFixture(root, config)
        proc = off.run()
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        am = self.alertmanager(off)
        human = {r["name"]: r for r in am["config"]["receivers"]}["telegram"]
        self.assertNotIn("telegram_configs", human)
        self.assertTrue(human["webhook_configs"], "the delivery record stays")
        self.assertEqual(am["config"]["route"]["receiver"], "telegram")
        self.assertEqual(am["alertmanagerSpec"]["secrets"], ["alertmanager-intake", "orbit-alert-webhook"])

    def test_telegram_on_without_a_chat_is_refused(self):
        config = dict(init_tests.BASE_CONFIG)
        config["contacts"] = {**config.get("contacts", {}), "telegram": False}
        config["features"] = {"alerts-telegram": True}
        root = self.tmp / "tg-nochat"
        root.mkdir()
        fx = init_tests.InitFixture(root, config)
        before = fx.snapshot()
        proc = fx.run()
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("the alerts-telegram feature is on and contacts.telegram is not true", proc.stdout + proc.stderr)
        self.assertEqual(fx.snapshot(), before, "a refused config still wrote files")

    def test_a_metric_monitor_needs_no_uptime(self):
        def monitor(fx):
            (fx.root / "monitors").mkdir(exist_ok=True)
            (fx.root / "monitors" / "rate.yaml").write_text(yaml.safe_dump({"name": "rate", "kind": "metric"}))
        _, _, proc = self.uptime_off("metric", monitor)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)

    def test_the_registry_missing_is_refused(self):
        root = self.tmp / "noreg"
        root.mkdir()
        fx = init_tests.InitFixture(root, dict(init_tests.BASE_CONFIG))
        (root / "provisioning" / "features.yaml").unlink()
        proc = fx.run()
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("provisioning/features.yaml is missing", proc.stdout + proc.stderr)


STAGE5 = REPO / "scripts" / "verify" / "stage5-platform.sh"


def stage5_between(start: str, end: str) -> str:
    text = STAGE5.read_text(encoding="utf-8")
    a = text.index(start)
    return text[a:text.index(end, a)]


class TheVerifyStageReadsTheSameFeatures(unittest.TestCase):
    """stage5's own reading of `features:` (the block that sets FEATURE_<NAME>), run as the
    stage runs it, in a tree that carries the registry."""

    def setUp(self):
        self.tmp = pathlib.Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        (self.tmp / "provisioning").mkdir()
        shutil.copy(TEMPLATE / "provisioning" / "features.yaml", self.tmp / "provisioning" / "features.yaml")
        (self.tmp / "instance").mkdir()
        block = stage5_between("GATEOUT=$(python3 - ", "\nPY\n")
        self.code = block[block.index("\n") + 1:]

    def read(self, config: dict) -> dict:
        (self.tmp / "instance" / "config.yaml").write_text(yaml.safe_dump(config))
        proc = subprocess.run([sys.executable, "-", "instance/config.yaml"], input=self.code,
                              cwd=self.tmp, capture_output=True, text=True)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        return dict(line.split("=", 1) for line in proc.stdout.splitlines())

    BUCKET = {"objectStorage": {"endpoint": "https://x.example", "buckets": {"platform": "b"}}}

    def test_no_block_follows_the_bucket(self):
        self.assertEqual(self.read({})["FEATURE_LOGS"], "0")
        got = self.read(dict(self.BUCKET))
        self.assertEqual((got["FEATURE_LOGS"], got["FEATURE_TRACES"]), ("1", "1"))

    def test_a_block_is_read_feature_by_feature_with_each_ones_apps(self):
        got = self.read({**self.BUCKET, "features": {"logs": True}})
        self.assertEqual((got["FEATURE_LOGS"], got["FEATURE_TRACES"]), ("1", "0"))
        triples = got["FEATURE_APPS"].strip("'").split()
        self.assertIn("logs:loki:1", triples)
        self.assertIn("traces:tempo:0", triples)
        self.assertEqual({t.split(":")[1] for t in triples},
                         {app for n in OPTIONAL for app in REGISTRY[n]["apps"]})

    def test_a_block_that_cannot_be_read_is_flagged_not_guessed(self):
        got = self.read({"features": {"logs": True}})        # on, and no bucket
        self.assertEqual(got.get("FEATURES_BAD"), "1")
        self.assertNotIn("FEATURE_LOGS", got)

    def test_a_tree_without_a_registry_reads_as_before_features(self):
        (self.tmp / "provisioning" / "features.yaml").unlink()
        got = self.read(dict(self.BUCKET))
        self.assertEqual((got["FEATURE_LOGS"], got["FEATURE_TRACES"]), ("1", "1"))


class DeclaredIsApplied(unittest.TestCase):
    """stage5's feature rows against a cluster that has, or has not, each app's inventory."""

    ROWS = stage5_between('echo "== features: what is declared is what is applied =="',
                          'echo "== logs: Loki serves what Alloy collects =="')

    def rows(self, apps: str, applied: str, liveoff="0", bad_block="0", retired: str = "",
             backups="0", plugin="0") -> str:
        script = (
            'ok(){ echo "ok $1"; }; bad(){ echo "FAIL $1"; }; keyoff_live(){ echo "ok KEYOFF $1"; }\n'
            'kubectl(){ for a in $APPLIED; do [ "$5" = "orbit-platform-inventory-$a" ] && { echo "apps/v1/Deployment/$a/$a"; echo "/v1/Namespace//$a"; return 0; }; done; '
            'for a in $RETIRED; do [ "$5" = "orbit-platform-inventory-$a" ] && { echo "/v1/Namespace//$a"; return 0; }; done; '
            '[ "$2 $5" = "cnpg-system barman-cloud" ] && [ "$PLUGIN" = 1 ] && return 0; return 1; }\n'
            + self.ROWS)
        proc = subprocess.run(["bash", "-c", script], capture_output=True, text=True,
                              env={"PATH": "/usr/bin:/bin", "FEATURE_APPS": apps, "APPLIED": applied, "RETIRED": retired,
                                   "LIVEOFF": liveoff, "FEATURES_BAD": bad_block, "FEATURE_BACKUPS": backups, "PLUGIN": plugin})
        self.assertEqual(proc.returncode, 0, proc.stderr)
        return proc.stdout

    def test_on_and_applied_and_off_and_absent_pass(self):
        out = self.rows("logs:loki:1 logs:alloy:1 traces:tempo:0", "loki alloy")
        self.assertNotIn("FAIL", out)
        self.assertEqual(("\n" + out).count("\nok "), 4, out)

    def test_backups_is_asked_of_its_plugin_both_ways(self):
        """backups owns no Application: its workload is the barman-cloud plugin."""
        apps = "logs:loki:1"
        self.assertIn("ok feature backups is on and its plugin", self.rows(apps, "loki", backups="1", plugin="1"))
        self.assertIn("ok feature backups is off and its plugin", self.rows(apps, "loki", backups="0", plugin="0"))
        self.assertIn("FAIL feature backups is on but its plugin (barman-cloud in cnpg-system) is not deployed",
                      self.rows(apps, "loki", backups="1", plugin="0"))
        self.assertIn("FAIL feature backups is off but its plugin (barman-cloud in cnpg-system) is still deployed",
                      self.rows(apps, "loki", backups="0", plugin="1"))

    def test_a_feature_that_is_off_with_its_app_still_applied_is_red(self):
        out = self.rows("traces:tempo:0 traces:opentelemetry-operator:0", "tempo")
        self.assertIn("FAIL feature traces is off but its app tempo is still applied", out)
        self.assertIn("ok feature traces is off and its app opentelemetry-operator is not applied", out)

    def test_a_retired_app_whose_inventory_keeps_only_its_namespace_is_not_applied(self):
        out = self.rows("traces:tempo:0 logs:loki:1", "loki", retired="tempo")
        self.assertIn("ok feature traces is off and its app tempo is not applied", out)
        self.assertNotIn("FAIL", out)
        out = self.rows("traces:tempo:1", "", retired="tempo")
        self.assertIn("FAIL feature traces is on but its app tempo is not applied", out)

    def test_a_feature_that_is_on_with_its_app_missing_is_red(self):
        out = self.rows("logs:loki:1 logs:alloy:1", "loki")
        self.assertIn("FAIL feature logs is on but its app alloy is not applied", out)

    def test_no_row_passes_silently_when_nothing_can_be_asked(self):
        self.assertIn("KEYOFF", self.rows("logs:loki:1", "", liveoff="1"))
        self.assertNotIn("FAIL", self.rows("logs:loki:1", "", bad_block="1"))
        self.assertIn("key off", self.rows("logs:loki:1", "", bad_block="1"))


class WithoutGrafanaTheLogQuestionGoesToLokiItself(unittest.TestCase):
    """stage5 asks Loki through Grafana's datasource proxy while the dashboards feature is
    on, and through the API server's service proxy when it is off. Either way the request
    carries the stream selector and the window, never a log line."""

    HELPERS = stage5_between("  LRANGE_GRAFANA(){", "  LFIND(){")

    def ask(self, dashboards: str) -> tuple[str, str]:
        tmp = pathlib.Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        (tmp / "bin").mkdir()
        (tmp / "bin" / "kubectl").write_text('#!/bin/sh\nprintf "%s\\n" "$*" > "$ARGV_LOG"\nprintf "%s" "FROM-LOKI"\n')
        (tmp / "bin" / "curl").write_text('#!/bin/sh\ncat > /dev/null\nprintf "%s" "FROM-GRAFANA"\n')
        for b in ("kubectl", "curl"):
            (tmp / "bin" / b).chmod(0o755)
        (tmp / "selector").write_text('{namespace="demo-prod",pod="web-1"}')
        script = ('grafpinned(){ return 0; }; grafauth(){ echo "user = \\"admin:x\\""; }; GRAFPIN=(); GRAF=http://127.0.0.1:9\n'
                  + self.HELPERS + '\nLRANGE "$SEL" 100 200\n')
        proc = subprocess.run(["bash", "-c", script], capture_output=True, text=True,
                              env={"PATH": str(tmp / "bin") + os.pathsep + os.environ.get("PATH", "/usr/bin:/bin"), "SEL": str(tmp / "selector"),
                                   "ARGV_LOG": str(tmp / "argv"), "FEATURE_DASHBOARDS": dashboards})
        self.assertEqual(proc.returncode, 0, proc.stderr)
        argv = (tmp / "argv").read_text() if (tmp / "argv").exists() else ""
        return proc.stdout, argv

    def test_with_dashboards_on_grafana_is_asked(self):
        out, argv = self.ask("1")
        self.assertEqual(out, "FROM-GRAFANA")
        self.assertEqual(argv, "", "kubectl was asked although Grafana is there")

    def test_with_dashboards_off_loki_is_asked_through_the_api_server(self):
        import urllib.parse
        out, argv = self.ask("0")
        self.assertEqual(out, "FROM-LOKI")
        self.assertIn("get --raw /api/v1/namespaces/loki/services/loki:3100/proxy/loki/api/v1/query_range?", argv)
        query = urllib.parse.parse_qs(argv.split("?", 1)[1].strip())
        self.assertEqual(query["query"], ['{namespace="demo-prod",pod="web-1"}'])
        self.assertEqual((query["start"], query["end"], query["direction"]), (["100000000000"], ["200000000000"], ["backward"]))


class TheTelegramRowsFollowTheFeature(unittest.TestCase):
    """stage5's first Telegram row against a running Alertmanager config, feature on and off."""

    ROWS = stage5_between('  if [ "$FEATURE_ALERTS_TELEGRAM" = 1 ]; then\n  python3 -c "',
                          "  # The workstation-copy comparison is gone")

    def rows(self, feature: str, receivers) -> str:
        tmp = pathlib.Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        status = {"config": {"original": yaml.safe_dump({"receivers": receivers})}} if receivers is not None else {}
        (tmp / "amstatus.json").write_text(json.dumps(status))
        script = 'ok(){ echo "ok $1"; }; bad(){ echo "FAIL $1"; }\n' + self.ROWS
        proc = subprocess.run(["bash", "-c", script], capture_output=True, text=True,
                              env={"PATH": os.environ["PATH"], "TMP": str(tmp), "FEATURE_ALERTS_TELEGRAM": feature})
        self.assertEqual(proc.returncode, 0, proc.stderr)
        return proc.stdout

    WITH = [{"name": "null"}, {"name": "telegram", "telegram_configs": [{"bot_token_file": "/etc/x"}],
                               "webhook_configs": [{"url": "http://alert-recorder.monitoring.svc:8080/"}]}]
    WITHOUT = [{"name": "null"}, {"name": "telegram", "webhook_configs": [{"url": "http://alert-recorder.monitoring.svc:8080/"}]}]

    def test_on_the_notifier_must_be_there(self):
        self.assertIn("ok Alertmanager notifies Telegram by reading a file", self.rows("1", self.WITH))
        self.assertIn("FAIL Alertmanager's Telegram config", self.rows("1", self.WITHOUT))

    def test_off_the_rows_key_off_saying_why_and_a_notifier_left_behind_is_red(self):
        out = self.rows("0", self.WITHOUT)
        self.assertIn("ok the alerts-telegram feature is off — the three Telegram rows key off", out)
        self.assertIn("ok the alerts-telegram feature is off and the running Alertmanager config has no Telegram notifier", out)
        self.assertNotIn("FAIL", out)
        self.assertIn("FAIL the alerts-telegram feature is off but the running Alertmanager config still has a Telegram notifier",
                      self.rows("0", self.WITH))

    def test_off_a_config_that_cannot_be_read_is_not_a_pass(self):
        self.assertIn("FAIL the alerts-telegram feature is off and the running Alertmanager config could not be read",
                      self.rows("0", None))


class SkillsSayWhichFeatureAToolNeeds(unittest.TestCase):
    """A tool owned by an optional feature is not there on every platform. A skill that
    tells an agent to use one says which feature it needs, in the words "the `<name>`
    feature", so an agent that finds the tool missing knows why and what to ask for."""

    SKILLS = sorted((REPO / "skills").glob("*/SKILL.md"))

    def owned(self):
        return {tool: name for name in OPTIONAL for tool in REGISTRY[name]["tools"]}

    def missing(self, text: str) -> list:
        return sorted({f"{tool} (the `{feature}` feature)" for tool, feature in self.owned().items()
                       if re.search(r"\b%s\b" % re.escape(tool), text) and f"the `{feature}` feature" not in text})

    def test_there_are_skills_and_owned_tools(self):
        self.assertTrue(self.SKILLS)
        self.assertTrue(self.owned())
        self.assertTrue(any(tool in s.read_text(encoding="utf-8") for s in self.SKILLS for tool in self.owned()),
                        "no skill names a feature-owned tool, so the rule below tests nothing")

    def test_every_skill_that_names_a_feature_tool_names_the_feature(self):
        for skill in self.SKILLS:
            with self.subTest(skill=skill.parent.name):
                self.assertEqual(self.missing(skill.read_text(encoding="utf-8")), [],
                                 f"{skill.parent.name} tells an agent to use a tool without saying which feature it needs")

    def test_the_rule_bites(self):
        tool, feature = sorted(self.owned().items())[0]
        self.assertEqual(self.missing(f"Call `{tool}` and read the answer."), [f"{tool} (the `{feature}` feature)"])
        self.assertEqual(self.missing(f"Call `{tool}`; it exists while the `{feature}` feature is on."), [])
        self.assertEqual(self.missing(f"`{tool}_more` is another tool."), [], "a longer name is another tool")

    def test_the_features_skill_describes_every_optional_feature(self):
        text = (REPO / "skills" / "orbit-features" / "SKILL.md").read_text(encoding="utf-8")
        for name in OPTIONAL:
            with self.subTest(feature=name):
                self.assertIn(f"\n- `{name}`: ", text, f"orbit-features does not describe the {name} feature")


if __name__ == "__main__":
    unittest.main()
