"""The commit gate inside orbitd (docs/V1.md M2c): a refused commit is never read as
desired state, the last good commit's tree stands in for it, the refusal is recorded,
shown and alerted, a restart still knows the last good commit, and a job waiting for its
commit is told why it will not be applied."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

from orbitd import checks
from orbitd import deploy as deploy_module
from orbitd.checks import facts as facts_module
from orbitd.gate import ALERT, STATE_FILE, CommitGate
from orbitd.shadow import catalog_pairs

from test_checks import REPO, VERSION, build_instance  # noqa: F401

GIT = ["git", "-c", "user.name=t", "-c", "user.email=t@invalid", "-c", "commit.gpgsign=false"]


@pytest.fixture(scope="module")
def good_instance(tmp_path_factory) -> Path:
    return build_instance(tmp_path_factory.mktemp("gate-orbitd") / "instance")


def git(root: Path, *args: str) -> str:
    return subprocess.run(GIT + ["-C", str(root), *args], capture_output=True, text=True, check=True).stdout.strip()


def commit(root: Path, message: str) -> str:
    git(root, "add", "-A")
    git(root, "commit", "-q", "-m", message)
    return git(root, "rev-parse", "HEAD")


@pytest.fixture
def world(fake_backends, good_instance, tmp_path, monkeypatch):
    """orbitd's state over a real git checkout of a good instance, and a gate on it."""
    state = fake_backends
    root = Path(shutil.copytree(good_instance, tmp_path / "checkout", symlinks=True))
    subprocess.run(GIT + ["init", "-q", "--initial-branch=main", str(root)], check=True, capture_output=True)
    head = commit(root, "a good instance")
    state.backends.instance.root = root
    monkeypatch.setattr(facts_module, "from_kube", lambda kube: facts_module.fixed(8000, 2000))

    def gate() -> CommitGate:
        g = CommitGate(state, version=VERSION, core=REPO, state_dir=tmp_path / "state", scratch=tmp_path / "scratch")
        (tmp_path / "scratch").mkdir(exist_ok=True)
        state.commit_gate = g
        return g

    return state, root, head, gate


def break_it(root: Path, message: str = "a rendered file edited by hand") -> str:
    path = root / "clusters" / "platform" / "apps" / "traefik.yaml"
    path.write_text(path.read_text().replace("replicas: 1", "replicas: 3", 1))
    return commit(root, message)


def alerts(state) -> list[dict]:
    return [c for c in state.backends.alertmanager.calls if c.get("path") == "/api/v2/alerts"]


def test_a_good_commit_is_accepted_and_read_from_its_own_tree(world, tmp_path):
    state, root, head, gate = world
    g = gate()
    verdict = g.evaluate()
    assert verdict.enabled and not verdict.blocks and verdict.good == head
    repo = state.backends.instance
    assert repo.pinned is not None and repo.pinned_commit == head and repo.checkout() == root
    read_from = repo.require()
    assert read_from != root, "desired state is read from the accepted commit's own tree, not the moving checkout"
    tracked = git(root, "ls-files").splitlines()
    assert tracked and all((read_from / rel).read_bytes() == (root / rel).read_bytes() for rel in tracked)
    saved = json.loads((tmp_path / "state" / STATE_FILE).read_text())
    assert saved["good"] == head and saved["refusal"] is None
    assert alerts(state) == []


def test_a_refused_commit_is_never_read_and_the_last_good_tree_stands_in(world, tmp_path, monkeypatch):
    state, root, head, gate = world
    g = gate()
    g.evaluate()
    bad = break_it(root)
    verdict = g.evaluate()
    assert verdict.refused and verdict.blocks and verdict.head == bad and verdict.good == head
    assert verdict.check == "scaffold" and "traefik.yaml" in verdict.sentence
    # desired state is read from the last good commit's tree, not from the checkout
    read_from = state.backends.instance.require()
    assert read_from != root and state.backends.instance.checkout() == root
    assert "replicas: 1" in (read_from / "clusters" / "platform" / "apps" / "traefik.yaml").read_text()
    assert "replicas: 3" in (root / "clusters" / "platform" / "apps" / "traefik.yaml").read_text()
    assert catalog_pairs(state) == [("demo", "prod")]
    # the refusal is on file, with the sentence, and the alert is raised with it
    saved = json.loads((tmp_path / "state" / STATE_FILE).read_text())
    assert saved["good"] == head and saved["refusal"]["commit"] == bad and saved["refusal"]["check"] == "scaffold"
    assert saved["recent"][-1]["verdict"] == "refused"
    (raised,) = alerts(state)
    alert = raised["json"][0]
    assert alert["labels"]["alertname"] == ALERT and alert["labels"]["check"] == "scaffold"
    assert alert["labels"]["commit"] == bad[:12] and "traefik.yaml" in alert["annotations"]["description"]
    assert alert["endsAt"] > alert["startsAt"]
    # asked again about the same commit: the checks do not run again, the alert is kept up
    monkeypatch.setattr(checks, "run_chain", lambda *a, **k: pytest.fail("the chain ran twice for one commit"))
    again = g.evaluate()
    assert again.refused and again.sentence == verdict.sentence
    assert len(alerts(state)) == 2


def test_the_next_good_commit_is_applied_and_the_alert_resolves(world):
    state, root, head, gate = world
    g = gate()
    g.evaluate()
    break_it(root)
    assert g.evaluate().refused
    git(root, "revert", "--no-edit", "HEAD")
    fixed = git(root, "rev-parse", "HEAD")
    verdict = g.evaluate()
    assert not verdict.blocks and verdict.good == fixed
    assert state.backends.instance.pinned_commit == fixed
    resolved = alerts(state)[-1]["json"][0]
    assert resolved["labels"]["alertname"] == ALERT and resolved["endsAt"] <= resolved["endsAt"]
    assert resolved["endsAt"] < alerts(state)[0]["json"][0]["endsAt"], "resolving ends the alert now"
    trees = sorted(p.name for p in Path(g.scratch).glob("orbit-tree-*"))
    assert trees == [f"orbit-tree-{fixed[:16]}"], "only the accepted commit's tree is kept"


def test_a_restart_still_knows_the_last_good_commit_and_the_refusal(world, monkeypatch):
    state, root, head, gate = world
    first = gate()
    first.evaluate()
    bad = break_it(root)
    assert first.evaluate().refused
    state.backends.instance.pin(None)           # the process is gone; so is its pin
    shutil.rmtree(first.scratch)
    second = gate()                             # a new process, the same state directory
    assert second.good == head and second.refusal["commit"] == bad
    monkeypatch.setattr(checks, "run_chain", lambda *a, **k: pytest.fail("a restart re-ran the checks for a decided commit"))
    verdict = second.evaluate()
    assert verdict.refused and verdict.good == head
    read_from = state.backends.instance.require()
    assert "replicas: 1" in (read_from / "clusters" / "platform" / "apps" / "traefik.yaml").read_text()


def test_a_first_start_on_a_refused_commit_applies_nothing(world):
    state, root, head, gate = world
    bad = break_it(root)
    g = gate()
    verdict = g.evaluate()
    assert verdict.refused and verdict.good is None and verdict.head == bad
    read_from = state.backends.instance.require()
    assert read_from != root and not (read_from / "catalog").exists(), "there is no last good tree to read"
    from orbitd.reconcile import ReconcileRunner
    assert ReconcileRunner(state, dry_run=False, period_s=180).run_once() == []


def test_a_check_that_could_not_be_decided_refuses_nothing_for_good(world, monkeypatch):
    state, root, head, gate = world
    g = gate()
    g.evaluate()
    catalog = yaml.safe_load((root / "catalog" / "projects.yaml").read_text())
    catalog["projects"][0]["blurb"] = "A changed line."
    (root / "catalog" / "projects.yaml").write_text(yaml.safe_dump(catalog, sort_keys=False))
    subprocess.run(["python3", "scripts/catalog-sync.py"], cwd=root, check=True, capture_output=True)
    newer = commit(root, "a harmless catalog change")
    monkeypatch.setattr(facts_module, "from_kube", lambda kube: checks.no_facts("the cluster did not answer"))
    verdict = g.evaluate()
    assert verdict.undecided and verdict.blocks and not verdict.refused
    assert g.refusal is None and alerts(state) == [], "a cluster that did not answer is not a refused commit"
    assert state.backends.instance.pinned is not None, "an undecided commit is not applied either"
    monkeypatch.setattr(facts_module, "from_kube", lambda kube: facts_module.fixed(8000, 2000))
    verdict = g.evaluate()
    assert not verdict.blocks and verdict.good == newer


def test_a_build_that_does_not_know_what_it_is_has_no_gate(world, tmp_path):
    state, root, head, _ = world
    break_it(root)
    for kwargs in ({"version": "unreleased", "core": REPO}, {"version": VERSION, "core": tmp_path / "no-core"}):
        g = CommitGate(state, state_dir=tmp_path / "s", scratch=tmp_path, **kwargs)
        verdict = g.evaluate()
        assert not verdict.enabled and not verdict.blocks and verdict.note
        assert state.backends.instance.pinned is None


def test_a_job_waiting_for_its_commit_is_told_why_it_is_not_applied(world):
    state, root, head, gate = world
    g = gate()
    g.evaluate()
    assert deploy_module._gate_step(state) is None
    bad = break_it(root)
    step = deploy_module._gate_step(state)
    assert step is not None and step.name == "apply" and step.status == "failed"
    assert f"the commit gate refused {bad[:12]} (scaffold)" in step.detail and "traefik.yaml" in step.detail
    assert "the last good commit keeps running" in step.detail


def test_deploy_status_shows_the_gate(world):
    from test_sweep_area4 import invoke
    state, root, head, gate = world
    g = gate()
    out = invoke(state, "orbit_deploy_status", project="demo", env="prod")
    assert out.gate.enabled and out.gate.verdict == "accepted" and out.gate.good == head
    bad = break_it(root)
    out = invoke(state, "orbit_deploy_status", project="demo", env="prod")
    assert out.gate.verdict == "refused" and out.gate.head == bad and out.gate.good == head
    assert out.gate.check == "scaffold" and "traefik.yaml" in out.gate.sentence
    assert g.refusal["commit"] == bad


def test_a_commit_at_another_core_is_refused_quietly_and_alerted_only_if_it_stalls(world, monkeypatch):
    """Every upgrade passes through this: the merge pins the new core, and the orbitd still
    running is the old one until the platform is applied."""
    import orbitd.gate as gate_module
    state, root, head, gate = world
    g = gate()
    g.evaluate()
    (root / "orbit-core.lock").write_text("url: x\nref: v9.9.10\n")
    newer = commit(root, "pin the next core")
    verdict = g.evaluate()
    assert verdict.refused and verdict.check == "core" and "pins orbit-core v9.9.10" in verdict.sentence
    assert state.backends.instance.pinned is not None, "nothing from the unverifiable commit is applied"
    assert alerts(state) == [], "an upgrade in progress is not paged"
    g.evaluate()
    assert alerts(state) == []
    monkeypatch.setattr(gate_module, "UPGRADE_GRACE_S", 0)
    g.evaluate()
    assert len(alerts(state)) == 1 and alerts(state)[0]["json"][0]["labels"]["check"] == "core"
    assert g.refusal["commit"] == newer


def test_the_next_build_checks_a_commit_the_last_one_could_not(world, tmp_path):
    """The upgrade, end to end: the old orbitd refuses the commit that pins the new core;
    the platform is applied; the new orbitd, started on the same state directory, must
    check that commit itself and not repeat the old one's refusal (seen on the test box,
    v0.34.0-rc1 -> rc2: the new build answered "checked by v0.34.0-rc1" for ever)."""
    state, root, head, gate = world
    old = gate()
    old.evaluate()
    (root / "orbit-core.lock").write_text("url: x\nref: v9.9.10\n")
    newer = commit(root, "pin the next core")
    assert old.evaluate().refused
    state.backends.instance.pin(None)
    new = CommitGate(state, version="v9.9.10", core=REPO, state_dir=tmp_path / "state", scratch=tmp_path / "scratch")
    verdict = new.evaluate()
    # this fixture's vendored files are v9.9.9's and equal REPO's, so v9.9.10 accepts them
    assert not verdict.refused and verdict.good == newer, verdict
    assert state.backends.instance.pinned_commit == newer
    # and a refusal the SAME build gave is still not re-run (the earlier test pins that)
    assert new.refusal is None


def test_a_commit_that_arrived_and_is_not_judged_yet_is_never_read(world):
    """The race seen on the test box (v0.34.0-rc3): git-sync moved the checkout to a bad
    commit between the gate's look and the reconcile loop's read, and the loop's tick ran
    on the bad commit. Readers get the accepted commit's own tree, so a checkout that has
    moved on changes nothing until the gate has judged where it moved to."""
    from orbitd.source import read_head, read_source
    state, root, head, gate = world
    g = gate()
    g.evaluate()
    bad = break_it(root)                       # the checkout moves; nobody has evaluated yet
    repo = state.backends.instance
    assert read_head(state).commit == bad
    assert read_source(state).commit == head, "the commit desired state is read from is still the accepted one"
    assert "replicas: 1" in (repo.require() / "clusters" / "platform" / "apps" / "traefik.yaml").read_text()
    assert catalog_pairs(state) == [("demo", "prod")]
    # and once judged, it stays the accepted one
    assert g.evaluate().refused
    assert read_source(state).commit == head and read_head(state).commit == bad


def test_a_restart_writes_the_last_good_tree_out_again(world, tmp_path):
    state, root, head, gate = world
    first = gate()
    first.evaluate()
    harmless = root / "NOTES.md"
    harmless.write_text("a note\n")
    newer = commit(root, "a note")
    assert first.evaluate().good == newer
    state.backends.instance.pin(None)
    shutil.rmtree(first.scratch)               # a new pod: the scratch directory is empty
    second = gate()
    verdict = second.evaluate()
    assert not verdict.blocks and state.backends.instance.pinned_commit == newer
    assert (state.backends.instance.require() / "NOTES.md").read_text() == "a note\n"
