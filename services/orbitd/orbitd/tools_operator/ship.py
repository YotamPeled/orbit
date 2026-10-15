"""orbit_deploy_ship and orbit_deploy_rollback."""

from __future__ import annotations

from typing import Annotated, Any

from mcp.server.mcpserver import Context
from mcp_types import InputRequiredResult
from pydantic import Field

from ..deploy import (
    TAG_PATTERN, actor_of, audit, execute_ship, plan_steps, runtime_of,
    start_job, validate_catalog,
)
from ..source import sourced
from ..roles import Mutation, Registry, Role, Step, failure
from ..state import AppState
from ..tools import confirmation, refused
from ..tools_viewer._util import ID_RE

TAG_RE = TAG_PATTERN


COMPONENTS_MAX = 32


def register(registry: Registry, state: AppState) -> None:

    @registry.tool(role=Role.operator, read_only=False, destructive=False)
    def orbit_deploy_ship(
        project: Annotated[str, Field(pattern=ID_RE.pattern, description="Catalog project id.")],
        env: Annotated[str, Field(pattern=ID_RE.pattern, description="Environment id (prod, dev, ...).")],
        components: Annotated[list[Annotated[str, Field(pattern=ID_RE.pattern)]], Field(
            min_length=1, max_length=COMPONENTS_MAX,
            description="Catalog component names to ship (each once; at most 32).")],
        tag: Annotated[str, Field(pattern=TAG_RE, description="Image tag to ship.")],
        dry_run: Annotated[bool, Field(description="Return the plan; do not confirm or write.")] = False,
        expect: Annotated[str | None, Field(
            description="If set, must equal the registry digest for the tag.")] = None,
        ctx: Context = None,  # type: ignore[assignment]
    ) -> Mutation | InputRequiredResult:
        """Ship one or more catalog components to `tag`. Confirms, then starts a job
        that runs ship.sh per component, waits for the rollout and asserts. `dry_run`
        returns the plan with no confirmation. `expect`, when given, must match the
        digest the registry reports or the job fails before any write. When
        ORBITD_APPLY is `on` the job's `apply` step, between `bump` and `rollout`,
        waits for the checkout to carry the bump commit and applies the rendered
        objects itself (a failure is `LANDED NOT DEPLOYED apply`); otherwise that
        step is skipped, nothing is applied, and the job is refused with
        `LANDED NOT DEPLOYED apply`. Fails `main_refused` before any write while the
        commit gate refuses the instance repository's newest commit (a bump on top of
        it would be refused too). One running job per project/env; a
        second call is `busy`. The job id is in `evidence.job_id`; reclaim it with
        orbit_job_wait. The Mutation's `source` is {commit, age_s, lag_note} for
        the git-sync checkout this call read.
        Example: orbit_deploy_ship(project="demo", env="dev", components=["web"],
        tag="main-abc1234")."""
        return _ship(state, project, env, components, tag, dry_run, expect, ctx,
                     action="ship", tool="orbit_deploy_ship")

    @registry.tool(role=Role.operator, read_only=False, destructive=True)
    def orbit_deploy_rollback(
        project: Annotated[str, Field(pattern=ID_RE.pattern, description="Catalog project id.")],
        env: Annotated[str, Field(pattern=ID_RE.pattern, description="Environment id (prod, dev, ...).")],
        component: Annotated[str, Field(pattern=ID_RE.pattern, description="Catalog component name.")],
        to: Annotated[str | None, Field(
            pattern=TAG_RE,
            description="Tag to roll back to; default is the previous ledger tag.")] = None,
        dry_run: Annotated[bool, Field(description="Return the plan; do not confirm or write.")] = False,
        ctx: Context = None,  # type: ignore[assignment]
    ) -> Mutation | InputRequiredResult:
        """Roll a component back to `to`, or to the tag before the current one in
        the tags-file history when `to` is omitted. No previous tag is a failure
        envelope. Same job, lock, `apply` step (orbitd applies when ORBITD_APPLY is
        `on`, else skipped and the job refused with nothing applied) and assertion
        path as orbit_deploy_ship.
        The Mutation's `source` is {commit, age_s, lag_note} for the checkout this call read.
        Example: orbit_deploy_rollback(project="demo", env="dev", component="web")."""
        tag = to
        if tag is None:
            try:
                tag = runtime_of(state).ledger.previous_tag(project, env, component)
            except Exception as e:
                return failure(
                    "ledger_error",
                    str(e),
                    next_action="pass to= an explicit tag",
                    retryable=False,
                    project=project, env=env, component=component,
                )
            if not tag:
                audit(actor=actor_of(), tool="orbit_deploy_rollback", project=project,
                      env=env, component=component, tag="-", verdict="no_previous_tag")
                return failure(
                    "no_previous_tag",
                    f"no previous tag for {project}/{env}/{component} in the ledger",
                    next_action="pass to= an explicit tag",
                    retryable=False,
                    project=project, env=env, component=component,
                )
        return _ship(state, project, env, [component], tag, dry_run, None, ctx,
                     action="rollback", tool="orbit_deploy_rollback")


def _main_refused(state: AppState) -> Any | None:
    """The gate's verdict when it has refused the checkout's newest commit, else None."""
    from ..gate import gate_of
    try:
        verdict = gate_of(state).evaluate()
    except Exception:
        return None
    return verdict if (verdict.enabled and verdict.refused) else None


def _ship(state: AppState, project: str, env: str, components: list[str], tag: str,
          dry_run: bool, expect: str | None, ctx: Context | None,
          *, action: str, tool: str) -> Any:
    actor = actor_of()
    joined = ",".join(components)
    if len(set(components)) != len(components) or len(components) > COMPONENTS_MAX:
        audit(actor=actor, tool=tool, project=project, env=env, component=joined[:256], tag=tag,
              verdict="failed")
        return failure("invalid_argument", "components must be unique and at most 32",
                       retryable=False, components=len(components))
    try:
        hit = validate_catalog(state, project, env, components)
    except Exception as e:
        from ..backends import BackendError, BackendUnconfigured
        if isinstance(e, (BackendError, BackendUnconfigured)):
            audit(actor=actor, tool=tool, project=project, env=env,
                  component=joined, tag=tag, verdict="failed")
            return e.as_failure()
        raise
    if isinstance(hit, Mutation):
        audit(actor=actor, tool=tool, project=project, env=env,
              component=joined, tag=tag, verdict="failed")
        return sourced(state, hit)
    blocked = _main_refused(state)
    if blocked is not None:
        # A tag bump lands on top of main. On a main the commit gate has refused, the
        # bump is refused with it: the ship used to write its commit, wait, and end
        # `LANDED NOT DEPLOYED apply`, one more commit on a main nothing is applied from.
        audit(actor=actor, tool=tool, project=project, env=env,
              component=joined, tag=tag, verdict="failed")
        return failure(
            "main_refused",
            f"the commit gate refused the newest commit of the instance repository "
            f"({blocked.head[:12]}, check {blocked.check}): {blocked.sentence} Nothing can be shipped until a "
            f"commit passes; nothing was written",
            next_action="undo the refused change (orbit_catalog_set with the entry orbit_catalog_get returns "
                        "restores the last accepted one; anything else is a revert in the instance repository), "
                        "wait for orbit_deploy_status to show gate.verdict `accepted`, then retry",
            retryable=True, project=project, env=env, commit=blocked.head, check=blocked.check,
        )
    if dry_run:
        plan = plan_steps(kind="ship", project=project, env=env,
                          components=components, tag=tag, expect=expect)
        audit(actor=actor, tool=tool, project=project, env=env,
              component=joined, tag=tag, verdict="dry_run")
        return sourced(state, Mutation(status="dry_run", steps=plan))
    what = "Ship" if action == "ship" else "Roll back"
    decision = confirmation(ctx, f"{what} {joined} of {project}/{env} to tag {tag}?")
    if isinstance(decision, InputRequiredResult):
        return decision
    if not decision:
        audit(actor=actor, tool=tool, project=project, env=env,
              component=joined, tag=tag, verdict="refused")
        return refused(f"{action} of {joined} on {project}/{env} was not confirmed")
    opening = [
        Step(name="confirm", status="ok"),
        Step(name="catalog", status="ok", detail=f"{project}/{env} ns={hit.namespace}"),
    ]
    comps = hit.components

    def run() -> Mutation:
        return execute_ship(
            state, project=project, env=env, components=comps, tag=tag,
            action=action, actor=actor, expect=expect, opening=opening,
        )

    return sourced(state, start_job(
        state, project=project, env=env, fn=run, actor=actor, tool=tool,
        component=joined, tag=tag, opening=opening,
    ))
