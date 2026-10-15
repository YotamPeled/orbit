"""precheck_entry, on a real instance tree with a real commit gate (QA of v1.0.0, F26).

orbit_catalog_set used to push whatever the generator accepted. What the gate then
refused (more memory than the node has, two components on one path, a secret nobody
declared) was refused on main, and a refused main blocks every deploy of every project.
The gate's verdict is asked before anything is pushed: the tree orbitd reads is copied,
the entry spliced in with this build's editor, the manifests regenerated with this
build's generator, and the gate's own chain run on the result.

Nothing is faked here but the node's size: the instance is scaffolded from the template,
the editor, the generator and the chart are the repository's own."""

from __future__ import annotations

import copy
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

from orbitd.checks import facts as facts_module
from orbitd.gate import CommitGate
from orbitd.tools_operator import catalog as catalog_tool

from test_checks import PROJECT, REPO, VERSION, build_instance  # noqa: F401
from test_gate import commit

GIT = ["git", "-c", "user.name=t", "-c", "user.email=t@invalid"]


@pytest.fixture(scope="module")
def good_instance(tmp_path_factory) -> Path:
    return build_instance(tmp_path_factory.mktemp("precheck") / "instance")


@pytest.fixture
def world(fake_backends, good_instance, tmp_path, monkeypatch):
    state = fake_backends
    root = Path(shutil.copytree(good_instance, tmp_path / "checkout", symlinks=True))
    subprocess.run(GIT + ["init", "-q", "--initial-branch=main", str(root)], check=True, capture_output=True)
    commit(root, "a good instance")
    state.backends.instance.root = root
    monkeypatch.setattr(facts_module, "from_kube", lambda kube: facts_module.fixed(8000, 2000))
    (tmp_path / "scratch").mkdir()
    state.commit_gate = CommitGate(state, version=VERSION, core=REPO, state_dir=tmp_path / "state",
                                   scratch=tmp_path / "scratch")
    assert not state.commit_gate.evaluate().blocks
    return state, root, tmp_path / "scratch"


def entry(**component) -> dict:
    out = copy.deepcopy(PROJECT)
    out["components"][0].update(component)
    return out


def tree_of(root: Path) -> dict[str, bytes]:
    return {p.relative_to(root).as_posix(): p.read_bytes()
            for p in sorted(root.rglob("*")) if p.is_file() and ".git" not in p.parts}


def test_an_entry_the_gate_accepts_is_accepted_and_nothing_is_written(world):
    state, root, scratch = world
    before = tree_of(root)
    assert catalog_tool.precheck_entry(state, "demo", entry(replicas=2, memory="512Mi")) == (True, "")
    assert tree_of(root) == before, "the check wrote into the tree orbitd reads"
    assert not list(scratch.glob("orbit-precheck-*")), "the check left its copy behind"


def test_more_memory_than_the_node_has_is_refused_by_the_fit_check_before_any_push(world):
    """The generator accepts this entry: only the gate, which knows the node, refuses it."""
    state, root, _ = world
    accepted, said = catalog_tool.precheck_entry(state, "demo", entry(memory="64Gi"))
    assert accepted is False
    assert said.startswith("fit: ") and "the node has 8000 Mi to give" in said
    assert yaml.safe_load((root / "catalog" / "projects.yaml").read_text())["projects"][0]["components"][0]["memory"] == "256Mi"


def test_what_the_chart_refuses_is_refused_by_the_render_check(world):
    """Two web components on one path: the generator's own rules pass it or refuse it; the
    chart refuses it for certain. Either way it is refused here, with a sentence."""
    state, _, _ = world
    two = entry()
    two["components"].append(dict(two["components"][0], name="web2", image="ghcr.io/fixture-org/demo-web2"))
    accepted, said = catalog_tool.precheck_entry(state, "demo", two)
    assert accepted is False and "path" in said, said


def test_what_the_generator_refuses_is_refused_in_its_sentence(world):
    state, _, _ = world
    accepted, said = catalog_tool.precheck_entry(state, "demo", entry(kind="daemon"))
    assert accepted is False and "kind" in said and "daemon" in said


def test_an_entry_for_a_project_that_is_not_there_is_refused_by_the_editor(world):
    state, _, _ = world
    accepted, said = catalog_tool.precheck_entry(state, "ghost", dict(entry(), id="ghost"))
    assert accepted is False and said


def test_a_cluster_that_does_not_answer_is_not_a_pass(world, monkeypatch):
    from orbitd.checks import FactsUnavailable

    def unavailable(kube):
        def raise_() -> int:
            raise FactsUnavailable("the cluster did not answer")
        from orbitd.checks import Facts
        return Facts(node_allocatable_bytes=raise_, platform_requests_bytes=raise_)

    state, _, _ = world
    monkeypatch.setattr(facts_module, "from_kube", unavailable)
    accepted, said = catalog_tool.precheck_entry(state, "demo", entry(replicas=2))
    assert accepted is False and "could not be checked" in said


def test_a_build_with_no_gate_says_it_checked_nothing(fake_backends):
    """A development build has no version and no copy of core: it cannot check, and says
    so instead of calling the entry fine."""
    assert catalog_tool.precheck_entry(fake_backends, "demo", entry()) == (True, catalog_tool.NOT_CHECKED)
