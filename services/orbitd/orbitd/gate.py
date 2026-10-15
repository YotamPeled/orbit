"""The commit gate: orbitd applies nothing from a commit its checks refuse.

git-sync moves the instance checkout to every new commit of main, at a moment of its own
choosing. Nothing reads desired state from that moving checkout. Each new commit is first
written out from git as its own tree (immutable: it is that commit and nothing else), and
the chain of checks (orbitd/checks) runs on that tree. A commit that passes becomes the
last good commit and its tree becomes the one every reader gets (the reconcile loop, a
ship, a monitor change, a feature switch, every viewer tool). A commit that fails is
REFUSED:

  * its tree is thrown away and the last good commit's tree stays the one that is read,
    so what was running keeps running and nothing new is applied;
  * the refusal is recorded (the gate's state file and an audit record), shown by
    orbit_deploy_status, and raised as the alert OrbitCommitRefused until a commit passes;
  * a job waiting for its own commit is told, with the failing check's sentence.

Because readers only ever see an accepted commit's own tree, there is no moment at which
a commit that has arrived but not been judged can be applied.

The state (last good commit, the refusal, recent verdicts) lives in the state directory,
so a restart still knows which commit was last good. With no last good commit at all (a
first start on a refused commit) nothing is applied until one passes.

A check that could not be decided for a reason that is not the commit's (the cluster did
not answer) is tried again on the next evaluation and refuses nothing permanently.

The gate is on only in a build that knows what it is: it has a version and carries its
copy of orbit-core. A development build has neither and says so once; it reads the
checkout directly, as before the gate existed.
"""

from __future__ import annotations

import json
import logging
import shutil
import subprocess
import tempfile
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import UNRELEASED, __version__, checks
from .audit import log_audit
from .backends import BackendError, BackendUnconfigured

log = logging.getLogger("orbitd.gate")

STATE_FILE = "commit-gate.json"
RECENT = 20
ALERT = "OrbitCommitRefused"
ALERT_TTL_S = 600        # each evaluation while refused re-raises the alert for this long
# A commit pinned to another core is what every upgrade looks like between its merge and
# the platform apply that starts the matching orbitd. Refused like any other, and alerted
# only if it is still refused after this long: an upgrade nobody finished.
UPGRADE_GRACE_S = 900
ACTOR = "orbitd"
TOOL = "commit-gate"
TREE_PREFIX = "orbit-tree-"
TREE_MARKER = ".orbit-tree-commit"


@dataclass(frozen=True)
class GateState:
    """What the gate says about the checkout's HEAD right now."""

    enabled: bool
    head: str = ""
    good: str | None = None
    refused: bool = False
    check: str | None = None
    sentence: str | None = None
    at: str | None = None
    undecided: bool = False      # a transient failure: neither accepted nor refused yet
    note: str | None = None

    def dump(self) -> dict[str, Any]:
        out: dict[str, Any] = {"enabled": self.enabled}
        if not self.enabled:
            if self.note:
                out["note"] = self.note
            return out
        out.update({"head": self.head, "good": self.good,
                    "verdict": ("refused" if self.refused else "undecided" if self.undecided else "accepted")})
        for key in ("check", "sentence", "at"):
            value = getattr(self, key)
            if value:
                out[key] = value
        return out

    @property
    def blocks(self) -> bool:
        """True when HEAD must not be applied (refused, or not decided yet)."""
        return self.enabled and (self.refused or self.undecided)


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


class CommitGate:
    def __init__(self, state: Any, *, version: str | None = None, core: Path | None = None,
                 state_dir: Path | None = None, scratch: Path | None = None) -> None:
        from .shadow import state_dir_of
        self.state = state
        self.version = __version__ if version is None else version
        self.core = Path(core) if core is not None else checks.core_dir()
        self.path = (Path(state_dir) if state_dir is not None else state_dir_of(state)) / STATE_FILE
        self.scratch = Path(scratch) if scratch is not None else Path(tempfile.gettempdir())
        self._lock = threading.Lock()
        self._note_logged = False
        self.good: str | None = None
        self.refusal: dict[str, Any] | None = None
        self.recent: list[dict[str, Any]] = []
        self._load()

    # ------------------------------------------------------------ whether it runs at all

    def disabled_reason(self) -> str | None:
        if self.version == UNRELEASED:
            return "this build has no version, so the commit gate is off (a development build)"
        if not (self.core / checks.MANIFEST_REL).is_file():
            return f"this build carries no copy of orbit-core at {self.core}, so the commit gate is off"
        return None

    # ------------------------------------------------------------ state file

    def _load(self) -> None:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        if isinstance(data, dict):
            good = data.get("good")
            self.good = good if isinstance(good, str) and good else None
            self.refusal = data.get("refusal") if isinstance(data.get("refusal"), dict) else None
            self.recent = [r for r in data.get("recent") or [] if isinstance(r, dict)][-RECENT:]

    def _save(self) -> None:
        body = json.dumps({"good": self.good, "refusal": self.refusal, "recent": self.recent[-RECENT:]},
                          sort_keys=True)
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(body, encoding="utf-8")
            tmp.replace(self.path)
        except OSError as e:
            log.error("the commit gate could not write its state (%s)", e.__class__.__name__)

    # ------------------------------------------------------------ the evaluation

    def evaluate(self) -> GateState:
        """Decide about the checkout's HEAD (once per commit) and pin or unpin the tree
        desired state is read from. Safe to call often: a commit already decided costs
        one `git rev-parse`."""
        reason = self.disabled_reason()
        if reason is not None:
            if not self._note_logged:
                self._note_logged = True
                log.warning("%s", reason)
            return GateState(enabled=False, note=reason)
        repo = self.state.backends.instance
        try:
            checkout = repo.checkout()
            head = (repo.run_git(["rev-parse", "HEAD"]) or "").strip()
        except (BackendError, BackendUnconfigured):
            return GateState(enabled=True, undecided=True, good=self.good,
                             sentence="the instance checkout cannot be read yet")
        if not head:
            return GateState(enabled=True, undecided=True, good=self.good,
                             sentence="the instance checkout has no commit yet")
        with self._lock:
            if head == self.good:
                tree = self._tree(checkout, head)
                if tree is None:
                    return GateState(enabled=True, head=head, good=self.good, undecided=True,
                                     sentence="the accepted commit's tree could not be written out")
                self._pin(repo, tree, head)
                return GateState(enabled=True, head=head, good=self.good)
            # A refusal stands only for the build that gave it: after an upgrade the new
            # orbitd checks the commit itself (the old one could not: it pinned this core).
            if self.refusal and self.refusal.get("commit") == head and self.refusal.get("by") == self.version:
                self._pin_good(repo, checkout)
                self._raise_alert(self.refusal)
                return self._refused_state(head)
            # The commit under judgment, as its own tree: what is checked is exactly what
            # would be read, whatever git-sync does to the checkout meanwhile.
            tree = self._tree(checkout, head)
            if tree is None:
                self._pin_good(repo, checkout)
                return GateState(enabled=True, head=head, good=self.good, undecided=True,
                                 sentence="the commit's tree could not be written out to be checked")
            verdict = checks.run_chain(checks.Context(
                root=tree, core=self.core, version=self.version, facts=self._facts()))
            if verdict.ok:
                was_refused = self.refusal
                self.good, self.refusal = head, None
                self._record({"commit": head, "verdict": "accepted", "at": _now()})
                self._save()
                self._pin(repo, tree, head)
                log_audit(log, "commit %s accepted by the gate", head[:12], actor=ACTOR, tool=TOOL,
                          verdict="commit_accepted", commit=head)
                if was_refused:
                    self._resolve_alert(was_refused)
                return GateState(enabled=True, head=head, good=head)
            bad = verdict.failed
            assert bad is not None
            self._pin_good(repo, checkout)
            if verdict.transient:
                log.warning("commit %s is not decided yet: %s", head[:12], bad.sentence)
                return GateState(enabled=True, head=head, good=self.good, undecided=True,
                                 check=bad.name, sentence=bad.sentence)
            self.refusal = {"commit": head, "check": bad.name, "sentence": bad.sentence, "at": _now(),
                            "by": self.version,
                            **({"upgrade": True, "since": time.time()} if bad.upgrade else {})}
            self._record({**self.refusal, "verdict": "refused"})
            self._save()
            log_audit(log, "commit %s refused by the %s check", head[:12], bad.name, actor=ACTOR, tool=TOOL,
                      verdict="commit_refused", commit=head, check=bad.name, sentence=bad.sentence)
            self._raise_alert(self.refusal)
            return self._refused_state(head)

    def _refused_state(self, head: str) -> GateState:
        r = self.refusal or {}
        return GateState(enabled=True, head=head, good=self.good, refused=True,
                         check=r.get("check"), sentence=r.get("sentence"), at=r.get("at"))

    def _record(self, row: dict[str, Any]) -> None:
        self.recent = (self.recent + [row])[-RECENT:]

    def _facts(self) -> checks.Facts:
        from .checks import facts
        return facts.from_kube(self.state.backends.kube)

    # ------------------------------------------------------------ the trees

    def _tree(self, checkout: Path, commit: str) -> Path | None:
        """`commit`'s tree, written from git into the scratch directory (once per commit).
        git objects are immutable, so this is that commit whatever the checkout does."""
        target = self.scratch / f"{TREE_PREFIX}{commit[:16]}"
        marker = target / TREE_MARKER
        try:
            if marker.is_file() and marker.read_text(encoding="utf-8").strip() == commit:
                return target
        except OSError:
            pass
        shutil.rmtree(target, ignore_errors=True)
        try:
            target.mkdir(parents=True, exist_ok=True)
            archive = subprocess.Popen(["git", "-C", str(checkout), "archive", "--format=tar", commit],
                                       stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
            extract = subprocess.run(["tar", "-x", "-C", str(target)], stdin=archive.stdout,
                                     capture_output=True, timeout=120)
            archive.wait(timeout=120)
        except (OSError, subprocess.SubprocessError):
            shutil.rmtree(target, ignore_errors=True)
            return None
        if archive.returncode != 0 or extract.returncode != 0:
            shutil.rmtree(target, ignore_errors=True)
            return None
        marker.write_text(commit, encoding="utf-8")
        return target

    def _pin(self, repo: Any, tree: Path, commit: str) -> None:
        """Make `tree` (the tree of `commit`) the one desired state is read from, and drop
        every other tree this gate wrote."""
        repo.pin(tree, commit)
        for old in self.scratch.glob(f"{TREE_PREFIX}*"):
            if old != tree:
                shutil.rmtree(old, ignore_errors=True)

    def _pin_good(self, repo: Any, checkout: Path) -> None:
        """Keep reading the last good commit's tree (writing it out again after a restart).
        With no last good commit, or one that cannot be written out, desired state is
        unreadable on purpose: a reader gets nothing to apply, never the refused commit."""
        tree = self._tree(checkout, self.good) if self.good else None
        if tree is not None:
            self._pin(repo, tree, self.good or "")
            return
        nothing = self.scratch / f"{TREE_PREFIX}none"
        nothing.mkdir(parents=True, exist_ok=True)
        repo.pin(nothing, "")

    # ------------------------------------------------------------ the alert

    def _alert(self, refusal: dict[str, Any], *, ends_in_s: float) -> list[dict[str, Any]]:
        def stamp(t: float) -> str:
            return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(t))
        commit = str(refusal.get("commit") or "")
        return [{
            "labels": {"alertname": ALERT, "severity": "critical", "check": str(refusal.get("check") or ""),
                       "commit": commit[:12]},
            "annotations": {
                "summary": f"orbitd refused commit {commit[:12]} of the instance repository",
                "description": (f"The {refusal.get('check')} check failed: {refusal.get('sentence')} "
                                f"Nothing from this commit is applied and the last good commit keeps "
                                f"running. No deploy, monitor or feature change can land until a commit "
                                f"passes: fix or revert it (`orbit check all` shows the same result at "
                                f"the desk)."),
            },
            "startsAt": str(refusal.get("at") or stamp(time.time())),
            "endsAt": stamp(time.time() + ends_in_s),
        }]

    def _post(self, body: list[dict[str, Any]]) -> None:
        try:
            self.state.backends.alertmanager.post("/api/v2/alerts", json=body)
        except Exception as e:  # the alert is one of three ways a refusal is seen; never fatal
            log.error("the commit gate could not reach Alertmanager (%s)", e.__class__.__name__)

    def _raise_alert(self, refusal: dict[str, Any]) -> None:
        if refusal.get("upgrade") and time.time() - float(refusal.get("since") or 0) < UPGRADE_GRACE_S:
            return                  # an upgrade in progress: visible everywhere, paged only if it stalls
        self._post(self._alert(refusal, ends_in_s=ALERT_TTL_S))

    def _resolve_alert(self, refusal: dict[str, Any]) -> None:
        self._post(self._alert(refusal, ends_in_s=0))


def gate_of(state: Any) -> CommitGate:
    """The one gate of this process."""
    gate = getattr(state, "commit_gate", None)
    if gate is None:
        gate = CommitGate(state)
        state.commit_gate = gate
    return gate
