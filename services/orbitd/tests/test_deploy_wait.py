"""orbit_deploy_wait (docs/V1.md M2c): the wait an agent uses after a catalog change is
merged. It ends on what the reconcile loop actually did for the pair, and on a commit the
gate refused it returns the refusal instead of waiting out its timeout."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from orbitd.checks import facts as facts_module
from orbitd.gate import CommitGate
from orbitd.reconcile import ReconcileTick
from orbitd.roles import CheckoutSource
from orbitd.tools_viewer import deploy_wait

from test_checks import REPO, VERSION, build_instance  # noqa: F401
from test_gate import break_it, commit, git

GIT = ["git", "-c", "user.name=t", "-c", "user.email=t@invalid"]


@pytest.fixture(scope="module")
def good_instance(tmp_path_factory) -> Path:
    return build_instance(tmp_path_factory.mktemp("wait") / "instance")


class Loop:
    """The reconcile loop as the tool sees it: the last tick of each pair."""

    def __init__(self) -> None:
        self.ticks: dict[tuple[str, str], ReconcileTick] = {}

    def latest(self, project: str, env: str):
        return self.ticks.get((project, env))

    def tick(self, commit_id: str, *, failed: int = 0, applied: int = 2, mode: str = "on") -> None:
        self.ticks[("demo", "prod")] = ReconcileTick(
            last_run="2026-10-06T10:00:00Z", applied=applied, failed=failed, mode=mode,
            source=CheckoutSource(commit=commit_id, age_s=1, lag_note="test"))


class Clock:
    def __init__(self, on_sleep=None) -> None:
        self.t = 0.0
        self.on_sleep = on_sleep
        self.sleeps = 0

    def now(self) -> float:
        return self.t

    def sleep(self, seconds: float) -> None:
        self.sleeps += 1
        self.t += max(seconds, 0.001)
        if self.on_sleep:
            self.on_sleep(self.sleeps)


@pytest.fixture
def world(fake_backends, good_instance, tmp_path, monkeypatch):
    state = fake_backends
    root = Path(shutil.copytree(good_instance, tmp_path / "checkout", symlinks=True))
    subprocess.run(GIT + ["init", "-q", "--initial-branch=main", str(root)], check=True, capture_output=True)
    first = commit(root, "a good instance")
    state.backends.instance.root = root
    monkeypatch.setattr(facts_module, "from_kube", lambda kube: facts_module.fixed(8000, 2000))
    (tmp_path / "scratch").mkdir()
    state.commit_gate = CommitGate(state, version=VERSION, core=REPO, state_dir=tmp_path / "state",
                                   scratch=tmp_path / "scratch")
    state.commit_gate.evaluate()          # orbitd has seen the good commit, as a running one has
    state.reconcile = Loop()
    return state, root, first


def test_with_no_good_commit_at_all_a_refusal_is_still_the_answer(fake_backends, good_instance, tmp_path, monkeypatch):
    state = fake_backends
    root = Path(shutil.copytree(good_instance, tmp_path / "checkout", symlinks=True))
    subprocess.run(GIT + ["init", "-q", "--initial-branch=main", str(root)], check=True, capture_output=True)
    commit(root, "a good instance")
    bad = break_it(root)
    state.backends.instance.root = root
    monkeypatch.setattr(facts_module, "from_kube", lambda kube: facts_module.fixed(8000, 2000))
    (tmp_path / "scratch").mkdir()
    state.commit_gate = CommitGate(state, version=VERSION, core=REPO, state_dir=tmp_path / "state",
                                   scratch=tmp_path / "scratch")
    state.reconcile = Loop()
    out = wait(state, Clock(), commit=bad)
    assert out.status == "refused" and "no last good commit" in out.detail and out.gate["check"] == "scaffold"


def wait(state, clock, **kw):
    args = {"project": "demo", "env": "prod", "commit": None, "timeout_s": 60, **kw}
    return deploy_wait._wait(state, args["project"], args["env"], args["commit"], args["timeout_s"],
                             clock=clock.now, sleep=clock.sleep)


def harmless_commit(root: Path) -> str:
    (root / "NOTES.md").write_text("a note\n")
    return commit(root, "a note")


def test_it_returns_applied_once_a_tick_ran_on_a_checkout_that_contains_the_commit(world):
    state, root, first = world
    newer = harmless_commit(root)
    state.reconcile.tick(first)                                   # the loop is still on the old commit
    clock = Clock(on_sleep=lambda n: state.reconcile.tick(newer) if n == 3 else None)
    out = wait(state, clock, commit=newer)
    assert out.status == "applied" and out.applied_commit == newer and out.commit == newer
    assert clock.sleeps == 3 and "carries" in out.detail
    # a tick on a LATER commit carries an earlier one too
    assert wait(state, Clock(), commit=first).status == "applied"
    assert wait(state, Clock(), commit=first[:10]).status == "applied", "a short id names the same commit"


def test_without_a_commit_it_waits_for_the_newest_one_the_checkout_has(world):
    state, root, first = world
    newer = harmless_commit(root)
    state.reconcile.tick(first)
    out = wait(state, Clock(), timeout_s=5)
    assert out.status == "waiting" and out.commit == newer and out.applied_commit == first
    assert first[:12] in out.detail and newer[:12] in out.detail


def test_a_timeout_says_waiting_and_how_far_the_loop_is(world):
    state, root, first = world
    newer = harmless_commit(root)
    clock = Clock()
    out = wait(state, clock, commit=newer, timeout_s=10)
    assert out.status == "waiting" and out.applied_commit is None and "no tick yet" in out.detail
    assert 9 <= out.waited_s <= 12


def test_a_tick_that_failed_on_that_commit_is_failed_not_applied(world):
    state, root, first = world
    state.reconcile.tick(first, failed=2)
    out = wait(state, Clock(), commit=first)
    assert out.status == "failed" and out.failed == 2 and "failed on 2 object(s)" in out.detail


def test_a_commit_the_gate_refused_returns_the_refusal_instead_of_waiting(world):
    state, root, first = world
    state.reconcile.tick(first)
    bad = break_it(root)
    clock = Clock()
    out = wait(state, clock, commit=bad, timeout_s=120)
    assert out.status == "refused", out
    assert clock.sleeps == 0, "a refused commit is known at once: the wait must not run out its timeout"
    assert out.gate["verdict"] == "refused" and out.gate["check"] == "scaffold"
    assert "traefik.yaml" in out.detail and "Nothing from it is applied" in out.detail
    # a commit landed on top of the refused one is refused with it; an earlier, applied one is not
    later = harmless_commit(root)
    assert wait(state, Clock(), commit=later, timeout_s=5).status == "refused"
    assert wait(state, Clock(), commit=first, timeout_s=5).status == "applied"
    git(root, "revert", "--no-edit", "HEAD~1")
    fixed = git(root, "rev-parse", "HEAD")
    state.reconcile.tick(fixed)
    assert wait(state, Clock(), commit=fixed, timeout_s=5).status == "applied"


def test_a_loop_that_is_off_says_so_at_once(world):
    state, root, first = world
    state.reconcile.tick(first, mode="off (pair not enabled)")
    assert wait(state, Clock(), commit=first).status == "off"
    state.reconcile = None
    out = wait(state, Clock(), commit=first)
    assert out.status == "off" and "ORBITD_APPLY=off" in out.detail


def test_an_unknown_pair_is_not_found(world):
    state, root, first = world
    out = wait(state, Clock(), project="nope")
    assert out.is_error and out.structured_content["code"] == "not_found"
    out = wait(state, Clock(), env="staging")
    assert out.is_error


def test_carries_reads_ancestry_from_the_checkout(world):
    state, root, first = world
    newer = harmless_commit(root)
    assert deploy_wait.carries(state, first, newer) and deploy_wait.carries(state, newer, newer)
    assert not deploy_wait.carries(state, newer, first)
    assert not deploy_wait.carries(state, "", newer) and not deploy_wait.carries(state, first, "")
    assert not deploy_wait.carries(SimpleNamespace(backends=SimpleNamespace(instance=None)), "abc1234", "def5678")


# ------------------------------------------- a job's own wait: the checkout went past its commit

def test_a_job_whose_commit_was_followed_by_another_does_not_wait_out_its_deadline(world, monkeypatch):
    """The checkout follows the repository once a minute: a second commit inside that
    minute and the checkout's HEAD is never the first. The first job's wait must end on
    "the checkout carries my commit", not time out as lag (docs/V1.md §8)."""
    from orbitd import deploy
    from orbitd.roles import CheckoutSource as Source

    state, root, first = world
    (root / "note.txt").write_text("someone else's change\n", encoding="utf-8")
    second = commit(root, "another agent's commit, 15 s later")
    state.commit_gate.evaluate()
    monkeypatch.setattr(deploy, "read_source", lambda _state: Source(commit=second, age_s=1, lag_note="test"))
    clock = Clock()
    monkeypatch.setattr(deploy, "runtime_of", lambda _state: SimpleNamespace(clock=clock.now, sleeper=clock.sleep))
    lag = deploy._wait_checkout(state, first, deadline=540.0)
    assert lag is None, lag
    assert clock.sleeps == 0


def test_a_checkout_that_never_gets_the_commit_is_still_lag(world, monkeypatch):
    from orbitd import deploy
    from orbitd.roles import CheckoutSource as Source

    state, root, first = world
    never = "0123456789abcdef0123456789abcdef01234567"
    monkeypatch.setattr(deploy, "read_source", lambda _state: Source(commit=first, age_s=1, lag_note="test"))
    clock = Clock()
    monkeypatch.setattr(deploy, "runtime_of", lambda _state: SimpleNamespace(clock=clock.now, sleeper=clock.sleep))
    lag = deploy._wait_checkout(state, never, deadline=30.0)
    assert lag is not None and lag.status == "failed"
    assert lag.detail == f"checkout lag: HEAD {first} is not {never}"


def test_an_older_commit_is_not_carried_by_a_checkout_behind_it(world):
    from orbitd import deploy

    state, root, first = world
    (root / "note.txt").write_text("later\n", encoding="utf-8")
    second = commit(root, "later")
    assert deploy.checkout_carries(state, first, second) is True
    assert deploy.checkout_carries(state, second, first) is False



# ---------------------------------------------------------------- a commit that adds the pair

def test_a_commit_that_adds_the_project_is_waited_for_not_called_missing(world, monkeypatch):
    """QA of v1.0.0, F2: right after an onboarding merge, which is when the skill says to
    call this, the answer was `not_found: project is not in the catalog` for about a
    minute: the checkout had not reached the merge yet."""
    state, root, first = world
    merge = "f" * 40                      # the onboarding merge: not in the checkout yet
    arrived = {"yes": False}
    real_find, real_carries = deploy_wait.find_project, deploy_wait.carries

    def find(state_, project):
        if project == "newcomer":
            return {"id": "newcomer", "environments": {"prod": {}}} if arrived["yes"] else None
        return real_find(state_, project)

    def carries(state_, commit_id, tip):
        if commit_id == merge:
            return arrived["yes"]
        return real_carries(state_, commit_id, tip)

    monkeypatch.setattr(deploy_wait, "find_project", find)
    monkeypatch.setattr(deploy_wait, "carries", carries)
    # the checkout does not get there within the timeout: a wait, to be called again
    clock = Clock()
    out = wait(state, clock, project="newcomer", commit=merge, timeout_s=9)
    assert out.status == "waiting" and "has not reached ffffffffffff" in out.detail and "call again" in out.detail
    assert clock.sleeps >= 3, "it answered without waiting"
    # it gets there during the wait, and the loop then applies the pair
    def on_sleep(n):
        if n == 2:
            arrived["yes"] = True
            state.reconcile.ticks[("newcomer", "prod")] = ReconcileTick(
                last_run="2026-10-06T10:00:00Z", applied=14, failed=0, mode="on",
                source=CheckoutSource(commit=merge, age_s=1, lag_note="test"))
    out = wait(state, Clock(on_sleep=on_sleep), project="newcomer", commit=merge)
    assert out.status == "applied" and out.applied == 14


def test_a_project_that_is_missing_once_the_commit_is_there_is_not_found(world):
    state, root, first = world
    out = deploy_wait._wait(state, "ghost", "prod", first, 30, clock=Clock().now, sleep=Clock().sleep)
    assert out.is_error and out.structured_content["code"] == "not_found"
    # and with no commit named there is nothing to wait for: the answer is immediate
    clock = Clock()
    out = deploy_wait._wait(state, "ghost", "prod", None, 30, clock=clock.now, sleep=clock.sleep)
    assert out.is_error and clock.sleeps == 0
