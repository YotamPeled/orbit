"""The converge engine (docs/V1.md M2c): what it does for each way the checkout and the
cluster can disagree, in what order, and what it records. The cluster is a fake that
writes down every operation; the real operations are exercised on the test box."""

from __future__ import annotations

import re
import shutil
from pathlib import Path

import pytest
import yaml

from orbitd import features_converge as converge
from orbitd import features_edit
from orbitd import orbit_features as rules

from test_features_gate import dependents, without

REPO = Path(__file__).resolve().parents[3]
REGISTRY_FILE = REPO / "template" / "provisioning" / "features.yaml"
REGISTRY = rules.load_registry(REPO / "template")
OPTIONAL = [name for name, spec in REGISTRY.items() if not spec["always"]]
BUCKET = {"objectStorage": {"endpoint": "https://s3.fixture.invalid", "buckets": {"platform": "fixture"}}}
ALL_ON = {name: True for name in OPTIONAL}


class Cluster:
    """A fake cluster: which apps are applied, the record, and a log of operations."""

    def __init__(self, applied=(), record=None, fail=()):
        self.apps = set(applied)
        self.record = record
        self.fail = dict(fail)          # {(action, app): error}
        self.log: list[tuple[str, str]] = []

    def applied(self, app):
        return app in self.apps

    def _do(self, action, app):
        self.log.append((action, app))
        return self.fail.get((action, app))

    def apply(self, app, *, prune):
        err = self._do("apply-prune" if prune else "apply", app)
        if err is None:
            self.apps.add(app)
        return err

    def retire(self, app):
        err = self._do("retire", app)
        if err is None:
            self.apps.discard(app)
        return err

    def wait_ready(self, app, timeout_s):
        return self._do("wait-ready", app)

    def read_record(self):
        return self.record

    def write_record(self, record):
        self.record = record


def checkout(tmp_path: Path, block) -> Path:
    root = tmp_path / "instance"
    (root / "provisioning").mkdir(parents=True, exist_ok=True)
    (root / "instance").mkdir(exist_ok=True)
    shutil.copy(REGISTRY_FILE, root / "provisioning" / "features.yaml")
    config = dict(BUCKET)
    if block is not None:
        config["features"] = block
    (root / "instance" / "config.yaml").write_text(yaml.safe_dump(config))
    return root


def owned(*names):
    return [app for name in names for app in REGISTRY[name]["apps"]]


def every_owned():
    return owned(*OPTIONAL)


def test_a_cluster_that_already_matches_is_left_alone_and_recorded(tmp_path):
    cluster = Cluster(applied=every_owned())
    code, report = converge.run(checkout(tmp_path, None), cluster)
    assert code == converge.EXIT_OK and report["ok"]
    assert cluster.log == [], "an instance from before features, fully applied, needs nothing done"
    assert cluster.record["features"] == ALL_ON
    assert all(row["action"] == "none" for row in report["features"].values())


@pytest.mark.parametrize("feature", OPTIONAL)
def test_turning_a_feature_off_reapplies_its_consumers_then_retires_its_apps(tmp_path, feature):
    """A feature goes off together with whatever needs it (backups needs databases)."""
    going = [name for name in OPTIONAL if name == feature or name in dependents(feature)]
    cluster = Cluster(applied=every_owned(), record={"features": dict(ALL_ON)})
    code, report = converge.run(checkout(tmp_path, without(feature)), cluster)
    assert code == converge.EXIT_OK, report
    actions = [action for action, _ in cluster.log]
    first_retire = actions.index("retire") if "retire" in actions else len(actions)
    assert set(actions[:first_retire]) <= {"apply-prune"} and set(actions[first_retire:]) <= {"retire"}, (
        "consumers let go first; then the apps are retired")
    consumers = [app for action, app in cluster.log if action == "apply-prune"]
    assert sorted(consumers) == sorted({app for name in going for app in REGISTRY[name]["consumers"]})
    assert len(consumers) == len(set(consumers)), "a consumer shared by two features is re-applied once"
    retired = [app for action, app in cluster.log if action == "retire"]
    assert retired == [app for name in going for app in reversed(REGISTRY[name]["apps"])], "last applied first"
    gone = {app for name in going for app in REGISTRY[name]["apps"]}
    assert cluster.apps & set(every_owned()) == set(every_owned()) - gone, "no other feature's app was retired"
    assert cluster.record["features"] == without(feature)
    assert all(report["features"][name]["action"] == "disable" for name in going)


@pytest.mark.parametrize("feature", OPTIONAL)
def test_turning_a_feature_on_applies_its_apps_then_its_consumers_then_waits(tmp_path, feature):
    others = [app for name in OPTIONAL if name != feature for app in REGISTRY[name]["apps"]]
    cluster = Cluster(applied=others, record={"features": {**ALL_ON, feature: False}})
    code, report = converge.run(checkout(tmp_path, dict(ALL_ON)), cluster)
    assert code == converge.EXIT_OK, report
    applies = [("apply", app) for app in REGISTRY[feature]["apps"]]
    consumers = [("apply-prune", app) for app in REGISTRY[feature]["consumers"]]
    waits = [("wait-ready", app) for app in REGISTRY[feature]["apps"]]
    assert cluster.log == applies + consumers + waits
    assert cluster.record["features"] == ALL_ON


def test_one_on_and_one_off_in_one_run_share_a_consumer_once(tmp_path):
    a, b = OPTIONAL[0], OPTIONAL[1]
    cluster = Cluster(applied=owned(b), record={"features": {**ALL_ON, a: False}})
    code, _ = converge.run(checkout(tmp_path, {**ALL_ON, b: False}), cluster)
    assert code == converge.EXIT_OK
    actions = [action for action, _ in cluster.log]
    first_retire = actions.index("retire")
    assert set(actions[:first_retire]) <= {"apply", "apply-prune"}, "nothing is retired before every apply is done"
    shared = set(REGISTRY[a]["consumers"]) & set(REGISTRY[b]["consumers"])
    assert shared, "the two fixtures share no consumer, so this test proves nothing"
    for app in shared:
        assert cluster.log.count(("apply-prune", app)) == 1


def test_a_failed_step_fails_the_run_and_leaves_that_feature_unrecorded(tmp_path):
    feature = OPTIONAL[0]
    first_app = REGISTRY[feature]["apps"][0]
    others = [app for name in OPTIONAL if name != feature for app in REGISTRY[name]["apps"]]
    cluster = Cluster(applied=others, record={"features": {**ALL_ON, feature: False}},
                      fail={("apply", first_app): "the chart did not render"})
    code, report = converge.run(checkout(tmp_path, dict(ALL_ON)), cluster)
    assert code == converge.EXIT_FAILED and report["ok"] is False
    assert report["failed"] == [feature]
    assert cluster.record["features"][feature] is False, "a feature whose step failed must not be recorded as on"
    assert ("wait-ready", first_app) not in cluster.log
    failed_step = next(s for s in report["steps"] if s["app"] == first_app and s["action"] == "apply")
    assert failed_step["ok"] is False and failed_step["error"] == "the chart did not render"
    # the next run picks it up again and finishes
    cluster.fail = {}
    cluster.log.clear()
    code, _ = converge.run(checkout(tmp_path, dict(ALL_ON)), cluster)
    assert code == converge.EXIT_OK and cluster.record["features"] == ALL_ON
    assert ("apply", first_app) in cluster.log


def test_a_feature_recorded_on_with_an_app_missing_is_applied_again(tmp_path):
    """The record says on, the cluster lost an app (a run was interrupted, or someone
    retired it by hand): the feature is not reported converged, it is converged."""
    feature = OPTIONAL[0]
    missing = REGISTRY[feature]["apps"][-1]
    cluster = Cluster(applied=[a for a in every_owned() if a != missing], record={"features": dict(ALL_ON)})
    code, report = converge.run(checkout(tmp_path, dict(ALL_ON)), cluster)
    assert code == converge.EXIT_OK
    assert ("apply", missing) in cluster.log
    assert report["features"][feature]["action"] == "enable"


def test_a_feature_recorded_off_with_an_app_still_applied_is_retired(tmp_path):
    feature = OPTIONAL[0]
    left = REGISTRY[feature]["apps"][0]
    cluster = Cluster(applied=[left], record={"features": {**ALL_ON, feature: False}})
    code, _ = converge.run(checkout(tmp_path, {name: False for name in OPTIONAL}), cluster)
    assert code == converge.EXIT_OK
    assert ("retire", left) in cluster.log and left not in cluster.apps


def test_a_dry_run_reports_and_changes_nothing(tmp_path):
    feature = OPTIONAL[0]
    cluster = Cluster(applied=every_owned(), record={"features": dict(ALL_ON)})
    code, report = converge.run(checkout(tmp_path, without(feature)), cluster, dry_run=True)
    assert code == converge.EXIT_OK and cluster.log == []
    assert cluster.record == {"features": dict(ALL_ON)}
    assert [s["action"] for s in report["steps"]].count("retire") == len(REGISTRY[feature]["apps"])


def test_a_checkout_whose_features_cannot_be_read_is_refused_and_nothing_is_done(tmp_path):
    cluster = Cluster(applied=every_owned(), record={"features": dict(ALL_ON)})
    code, report = converge.run(checkout(tmp_path, {"no-such-feature": True}), cluster)
    assert code == converge.EXIT_REFUSED and "no-such-feature" in report["refused"]
    assert cluster.log == [] and cluster.record == {"features": dict(ALL_ON)}


def test_running_it_twice_does_nothing_the_second_time(tmp_path):
    feature = OPTIONAL[0]
    cluster = Cluster(applied=every_owned(), record={"features": dict(ALL_ON)})
    root = checkout(tmp_path, without(feature))
    converge.run(root, cluster)
    cluster.log.clear()
    code, report = converge.run(root, cluster)
    assert code == converge.EXIT_OK and cluster.log == []
    assert all(row["action"] == "none" for row in report["features"].values())


# ---------------------------------------------------------------- the registry's consumers

def test_every_app_that_changes_with_a_feature_is_one_of_its_consumers():
    """Derived from the tree: an Application whose own file carries a feature's block, or
    whose directory holds a file the feature owns or a file with its block, changes when
    the feature is switched, and must be re-applied then."""
    staged = REPO / "template" / "provisioning" / "clusters"
    all_on = {name: True for name in REGISTRY}
    directory_apps: dict[str, str] = {}
    # {Application: (its file, the features that have a block inside ITS document)}: one
    # file can declare two Applications, and a block changes only the one it sits in
    app_docs: dict[str, tuple[str, set[str]]] = {}
    for f in sorted((staged / "platform" / "apps").glob("*.yaml")):
        for chunk in re.split(r"(?m)^---[ \t]*$", f.read_text(encoding="utf-8")):
            text = rules.apply_blocks(chunk, all_on)
            doc = yaml.safe_load(re.sub(r"\{\{[A-Za-z0-9_.]+\}\}", "x", text))
            if isinstance(doc, dict) and doc.get("kind") == "Application":
                name = doc["metadata"]["name"]
                app_docs[name] = (f.relative_to(staged).as_posix(), rules.block_features(chunk))
                path = ((doc.get("spec") or {}).get("source") or {}).get("path")
                if path and str(path).startswith("clusters/"):
                    directory_apps[name] = str(path)[len("clusters/"):].rstrip("/")
    for feature in OPTIONAL:
        derived = set()
        for f in sorted(staged.rglob("*.yaml")):
            rel = f.relative_to(staged).as_posix()
            text = f.read_text(encoding="utf-8")
            touches = (feature in rules.block_features(text) or rules.owners(rel, REGISTRY) == [feature])
            value = "{{features.%s}}" % feature in text      # a chart told on or off by a value
            if not touches and not value:
                continue
            for app, (file, blocks) in app_docs.items():
                if file == rel and (feature in blocks or (value and len([a for a, (f2, _) in app_docs.items() if f2 == rel]) == 1)):
                    derived.add(app)
            for app, directory in directory_apps.items():
                if rel.startswith(directory + "/"):
                    derived.add(app)
        declared = set(REGISTRY[feature]["consumers"])
        assert derived <= declared, f"{feature}: {sorted(derived - declared)} change with it and are not its consumers"
        # `services` holds what the generator writes for a feature (tenant objects); the
        # tree scan above cannot see that, so it is the one consumer allowed beyond it
        assert declared - derived <= {"services"}, f"{feature}: {sorted(declared - derived)} is declared and nothing shows why"
        assert not declared & set(REGISTRY[feature]["apps"])


# ---------------------------------------------------------------- the config edit

CONFIG = """# the instance
instance:
  name: fixture   # short id
objectStorage:
  endpoint: https://s3.fixture.invalid
  buckets: {platform: fixture}
contacts:
  telegram: true
"""


def test_the_first_switch_writes_every_current_state_and_changes_only_the_one_asked_for():
    text, want = features_edit.set_feature(CONFIG, REGISTRY, OPTIONAL[0], False)
    assert want == {**ALL_ON, OPTIONAL[0]: False}
    assert text.startswith(CONFIG), "every existing line stays as it was"
    assert yaml.safe_load(text)["features"] == want
    assert "# short id" in text


def test_an_existing_block_is_rewritten_in_place_and_nothing_else_moves():
    start = CONFIG.replace("contacts:", "features:\n  logs: true   # keep the logs\n  traces: false\ncontacts:")
    text, want = features_edit.set_feature(start, REGISTRY, "traces", True)
    assert want["traces"] is True and want["logs"] is True
    before, after = start.split("features:")[0], text.split("features:")[0]
    assert before == after
    assert text.endswith("contacts:\n  telegram: true\n")
    assert yaml.safe_load(text)["features"] == want
    inline, _ = features_edit.set_feature(CONFIG + "features: {logs: true}\n", REGISTRY, "logs", False)
    assert yaml.safe_load(inline)["features"]["logs"] is False


def test_a_switch_that_cannot_be_made_is_refused_in_one_sentence():
    no_bucket = "instance:\n  name: fixture\nfeatures: {}\n"
    with pytest.raises(rules.FeatureError) as e:
        features_edit.set_feature(no_bucket, REGISTRY, "logs", True)
    assert "bucket" in str(e.value)
    for feature, word in (("core", "always on"), ("nope", "not a feature")):
        with pytest.raises(rules.FeatureError) as e:
            features_edit.set_feature(CONFIG, REGISTRY, feature, False)
        assert word in str(e.value)
    with pytest.raises(rules.FeatureError):
        features_edit.set_feature("- not\n- a mapping\n", REGISTRY, "logs", False)


def test_a_gated_converge_applies_nothing_from_a_checkout_the_checks_refuse(tmp_path, monkeypatch, capsys):
    """The converge Job runs gated: whatever commit it was handed, it checks the checkout
    itself before applying anything from it."""
    from types import SimpleNamespace

    from orbitd import checks, features_cli
    cluster = Cluster(applied=every_owned(), record={"features": dict(ALL_ON)})
    root = checkout(tmp_path, {**ALL_ON, OPTIONAL[0]: False})
    args = SimpleNamespace(instance_dir=str(root), dry_run=False, ready_timeout="5", gated=True,
                           ops=cluster, kube=object(), kubeconfig=None)
    monkeypatch.setattr(checks, "core_dir", lambda env=None: REPO)
    refused = checks.Verdict(ok=False, results=[checks.Result(name="scaffold", ok=False, sentence="clusters/x differs", seconds=0.1)])
    monkeypatch.setattr(checks, "run_chain", lambda ctx, only=None: refused)
    monkeypatch.setattr("orbitd.checks.facts.from_kube", lambda kube: checks.no_facts())
    code = features_cli.cmd_converge(args)
    out = capsys.readouterr()
    assert code == converge.EXIT_REFUSED and cluster.log == [], "a refused checkout must change nothing"
    assert "the scaffold check refuses this checkout: clusters/x differs" in out.out + out.err
    monkeypatch.setattr(checks, "run_chain", lambda ctx, only=None: checks.Verdict(ok=True, results=[]))
    assert features_cli.cmd_converge(args) == converge.EXIT_OK and cluster.log, "an accepted checkout converges"


def test_a_feature_that_owns_no_application_is_taken_to_be_what_the_instance_rendered(tmp_path):
    """Grafana lives inside the metrics stack's chart: no Application of its own to look
    for. On an instance that never recorded its features it is taken to have been on (it
    was rendered), so naming it on changes nothing and turning it off re-applies its
    consumers."""
    ownerless = [name for name in OPTIONAL if not REGISTRY[name]["apps"]]
    if not ownerless:
        pytest.skip("every optional feature owns an Application")
    feature = ownerless[0]
    cluster = Cluster(applied=every_owned())
    code, _ = converge.run(checkout(tmp_path, dict(ALL_ON)), cluster)
    assert code == converge.EXIT_OK and cluster.log == []
    cluster = Cluster(applied=every_owned())
    (tmp_path / "second").mkdir()
    code, report = converge.run(checkout(tmp_path / "second", {**ALL_ON, feature: False}), cluster)
    assert code == converge.EXIT_OK
    assert cluster.log == [("apply-prune", app) for app in REGISTRY[feature]["consumers"]]
    assert report["features"][feature] == {"desired": False, "was": True, "action": "disable"}
    assert cluster.record["features"][feature] is False


def test_a_freshly_applied_pod_template_without_its_checksum_annotation_is_not_a_change():
    """The applier strips Helm's `checksum/*` pod-template annotations before it applies,
    so an object it has just created carries no annotations map at all, while the render
    still does. That is not drift (seen on the test box: tempo read as changed right after
    a converge applied it, and `make verify` stage6 went red)."""
    from orbitd.shadow import _changed_diffs
    rendered = {"spec": {"template": {"metadata": {"labels": {"app": "tempo"},
                                                   "annotations": {"checksum/config": "5a8e"}}}}}
    live = {"spec": {"template": {"metadata": {"labels": {"app": "tempo"}}}}}
    assert _changed_diffs(rendered, live, kind="StatefulSet", api_version="apps/v1") == []
    # a real annotation the live object lacks is still a change, checksum beside it or not
    rendered["spec"]["template"]["metadata"]["annotations"]["prometheus.io/scrape"] = "true"
    diffs = _changed_diffs(rendered, live, kind="StatefulSet", api_version="apps/v1")
    assert [d.path for d in diffs] == ["/spec/template/metadata/annotations"]
    # and a checksum on the object's OWN metadata is not the pod template's
    assert _changed_diffs({"metadata": {"annotations": {"checksum/x": "1"}}}, {"metadata": {}},
                          kind="StatefulSet", api_version="apps/v1") != []
