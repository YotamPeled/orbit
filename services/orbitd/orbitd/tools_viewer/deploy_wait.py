"""orbit_deploy_wait — block until orbitd has applied a commit of the instance repository to
one project environment, or say why it will not."""

from __future__ import annotations

import subprocess
import time
from typing import Annotated, Any

from pydantic import BaseModel, Field

from ..roles import Registry, Role
from ..source import read_head
from ..state import AppState
from .common import find_project, guarded, not_found, require_env
from .util import Ident

COMMIT_PATTERN = r"^[0-9a-f]{7,40}$"
POLL_S = 3.0


class DeployWait(BaseModel):
    status: str = Field(description="applied, waiting, failed, refused, or off")
    project: str
    env: str
    commit: str = Field(description="the commit waited for")
    applied_commit: str | None = Field(default=None, description="the checkout commit of the reconcile loop's last tick for the pair")
    last_run: str | None = None
    applied: int | None = None
    failed: int | None = None
    waited_s: int = 0
    detail: str
    gate: dict[str, Any] | None = None


def carries(state: AppState, commit: str, tip: str) -> bool:
    """True when `tip` is `commit` or a descendant of it, by the checkout's own history."""
    from ..deploy import checkout_carries
    return checkout_carries(state, commit, tip)


def register(registry: Registry, state: AppState) -> None:

    @registry.tool(role=Role.viewer, read_only=True, idempotent=True)
    def orbit_deploy_wait(
        project: Annotated[Ident, Field(description="Project id from orbit_project_list.")],
        env: Annotated[Ident, Field(description="Environment name, e.g. prod or dev.")],
        commit: Annotated[str | None, Field(
            pattern=COMMIT_PATTERN,
            description="The instance repository commit to wait for (7 to 40 hex characters), e.g. the merge "
                        "commit of a catalog pull request. Omitted: the newest commit orbitd's checkout has now.")] = None,
        timeout_s: Annotated[int, Field(
            ge=0, le=120, description="Seconds to wait. Capped at 120; call again while `status` is `waiting`.")] = 60,
    ) -> DeployWait:
        """Wait until orbitd has applied a commit of the instance repository to one project
        environment. A catalog change lands as a merge; orbitd's checkout follows the
        repository within about a minute and its reconcile loop applies the pair on its
        next tick. This is the wait for that: it returns `applied` once the loop's last
        tick for the pair ran on a checkout that contains `commit` with no failed object
        (`applied_commit`, `last_run`, `applied`). `waiting` means the timeout ran out
        first: call it again. `failed` means the tick that carried the commit failed on
        `failed` object(s): read orbit_deploy_status and orbit-diagnose. `refused` means
        the commit gate refused the checkout's newest commit, so nothing from it will be
        applied: `gate.check` and `gate.sentence` say why, and the fix is a new commit.
        `off` means orbitd applies nothing for this pair (ORBITD_APPLY is off, or the
        pair is not in ORBITD_APPLY_PAIRS). `detail` is the result as one sentence.
        Given a `commit` that adds the project or the environment (an onboarding merge),
        it waits for orbitd's checkout to reach that commit before it looks the pair up:
        `waiting` until then, `not_found` only once the checkout holds the commit and
        the pair is still not in the catalog.
        Example: orbit_deploy_wait(project="demo", env="dev", commit="0123abc", timeout_s=120)."""
        return guarded(lambda: _wait(state, project, env, commit, timeout_s))


def _wait(state: AppState, project: str, env: str, commit: str | None, timeout_s: int,
          *, clock: Any = time.monotonic, sleep: Any = time.sleep) -> Any:
    # With a refused commit and no last good one there is no catalog to look the pair up
    # in; that is the answer, before anything else is read.
    try:
        from ..gate import gate_of
        first = gate_of(state).evaluate()
        if first.refused and first.good is None:
            return DeployWait(
                status="refused", project=project, env=env, commit=commit or first.head, waited_s=0,
                detail=(f"the commit gate refused {first.head[:12]} ({first.check}): {first.sentence} "
                        f"There is no last good commit, so nothing is applied; land a commit that passes."),
                gate=first.dump())
    except Exception:
        pass
    started = clock()
    deadline = started + max(0, int(timeout_s))

    def known() -> Any:
        """None when the pair is in the catalog orbitd reads now, else the failure."""
        entry = find_project(state, project)
        if entry is None:
            return not_found(f"project {project!r} is not in the catalog", project=project)
        return require_env(entry, project, env)

    missing = known()
    if missing is not None and commit:
        # The commit waited for may be the one that ADDS the project or the environment:
        # the merge of an onboarding pull request, which is when the skill says to call
        # this. Until orbitd's checkout has reached it (about a minute) the catalog it
        # reads does not hold the pair yet; that is a wait, not "not in the catalog"
        # (QA of v1.0.0, F2).
        def reached() -> bool:
            from ..source import read_source
            try:
                return carries(state, commit, read_source(state).commit)
            except Exception:
                return False

        while not reached() and clock() < deadline:
            sleep(min(POLL_S, max(0.0, deadline - clock())))
        missing = known()
        if missing is not None and not reached():
            return DeployWait(
                status="waiting", project=project, env=env, commit=commit, waited_s=int(clock() - started),
                detail=(f"orbitd's checkout has not reached {commit[:12]} yet (it follows the repository within "
                        f"about a minute), and {project}/{env} is not in the catalog it reads now: call again"))
    if missing is not None:
        return missing
    want = commit or read_head(state).commit

    def answer(status: str, detail: str, tick: Any = None, gate: Any = None) -> DeployWait:
        source = getattr(tick, "source", None)
        return DeployWait(
            status=status, project=project, env=env, commit=want,
            applied_commit=getattr(source, "commit", None) or None,
            last_run=getattr(tick, "last_run", None), applied=getattr(tick, "applied", None),
            failed=getattr(tick, "failed", None), waited_s=int(clock() - started), detail=detail, gate=gate)

    if not want:
        return answer("waiting", "orbitd's checkout has no commit yet")
    runner = getattr(state, "reconcile", None)
    if runner is None:
        return answer("off", "orbitd's reconcile loop is off (ORBITD_APPLY=off): nothing applies this commit")
    while True:
        # What the loop already did comes first: a commit it applied stays applied even
        # when a later commit is refused.
        tick = runner.latest(project, env)
        if tick is not None:
            if str(tick.mode or "").startswith("off"):
                return answer("off", f"{project}/{env} is not in ORBITD_APPLY_PAIRS: orbitd does not apply this pair",
                              tick)
            tip = tick.source.commit if tick.source is not None else ""
            if carries(state, want, tip):
                if tick.failed:
                    return answer("failed",
                                  f"orbitd's tick at {tick.last_run} on {tip[:12]} failed on {tick.failed} object(s): "
                                  f"read orbit_deploy_status", tick)
                return answer("applied",
                              f"{project}/{env} carries {want[:12]} (orbitd's tick at {tick.last_run} on {tip[:12]}, "
                              f"{tick.applied} object(s) applied)", tick)
        gate = None
        try:
            from ..gate import gate_of
            verdict = gate_of(state).evaluate()
            gate = verdict.dump() if verdict.enabled else None
            if verdict.refused and carries(state, want, verdict.head):
                return answer("refused",
                              f"the commit gate refused {verdict.head[:12]} ({verdict.check}): {verdict.sentence} "
                              f"Nothing from it is applied; land a commit that passes.", tick, gate)
        except Exception:
            gate = None
        if clock() >= deadline:
            tip = (tick.source.commit if tick is not None and tick.source is not None else "") or "(no tick yet)"
            return answer("waiting", f"orbitd's last tick for {project}/{env} ran on {tip[:12]}, not yet on {want[:12]}",
                          tick, gate)
        sleep(min(POLL_S, max(0.0, deadline - clock())))
