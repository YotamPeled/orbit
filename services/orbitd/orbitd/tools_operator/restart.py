"""orbit_workload_restart."""

from __future__ import annotations

from typing import Annotated, Any

from mcp.server.mcpserver import Context
from mcp_types import InputRequiredResult
from pydantic import Field

from ..deploy import (
    actor_of, audit, execute_restart, plan_steps, resolve_restart_tag,
    start_job, validate_catalog,
)
from ..source import sourced
from ..roles import Mutation, Registry, Role, Step, failure
from ..state import AppState
from ..tools import confirmation, refused
from ..tools_viewer._util import ID_RE
from ..tools_viewer.common import ns


def register(registry: Registry, state: AppState) -> None:

    @registry.tool(role=Role.operator, read_only=False, destructive=True)
    def orbit_workload_restart(
        project: Annotated[str, Field(pattern=ID_RE.pattern, description="Catalog project id.")],
        env: Annotated[str, Field(pattern=ID_RE.pattern, description="Environment id (prod, dev, ...).")],
        component: Annotated[str, Field(pattern=ID_RE.pattern, description="Catalog component name.")],
        dry_run: Annotated[bool, Field(description="Return the plan; do not confirm or write.")] = False,
        ctx: Context = None,  # type: ignore[assignment]
    ) -> Mutation | InputRequiredResult:
        """Restart a workload by patching the Deployment pod-template annotation
        `orbit.dev/restarted-at` (RFC 3339). Nothing else changes. Confirms, then
        starts a job that patches (that patch is the restart: it replaces the
        pods), applies the rendered objects itself when
        ORBITD_APPLY is `on` (step `apply`; skipped otherwise, but the patch
        already landed so the job still waits for the rollout and asserts).
        `dry_run`
        returns the plan with no confirmation. One running job per project/env.
        Fails `not_restartable` for a `kind: cron` component: a scheduled job has no
        pod between runs, and every run starts fresh.
        The Mutation's `source` is {commit, age_s, lag_note} for the checkout this call read.
        Example: orbit_workload_restart(project="demo", env="dev", component="web")."""
        actor = actor_of()
        try:
            hit = validate_catalog(state, project, env, [component])
        except Exception as e:
            from ..backends import BackendError, BackendUnconfigured
            if isinstance(e, (BackendError, BackendUnconfigured)):
                audit(actor=actor, tool="orbit_workload_restart", project=project,
                      env=env, component=component, tag="-", verdict="failed")
                return e.as_failure()
            raise
        if isinstance(hit, Mutation):
            audit(actor=actor, tool="orbit_workload_restart", project=project,
                  env=env, component=component, tag="-", verdict="failed")
            return sourced(state, hit)
        kind = str((hit.components[0] if hit.components else {}).get("kind") or "web")
        if kind == "cron":
            # A scheduled job is a CronJob: there is no Deployment to patch and no pod
            # between runs. The patch failed with "kubernetes request failed" and no
            # reason (QA of v1.0.0, F36).
            audit(actor=actor, tool="orbit_workload_restart", project=project,
                  env=env, component=component, tag="-", verdict="failed")
            schedule = str((hit.components[0] or {}).get("schedule") or "")
            return failure(
                "not_restartable",
                f"{component} of {project}/{env} is a scheduled job (kind cron"
                f"{', schedule ' + repr(schedule) if schedule else ''}): it has no running pod to restart; "
                f"each run starts a fresh pod from the current image and settings",
                next_action="nothing to restart: a change (orbit_deploy_ship, orbit_catalog_set) takes effect "
                            "at its next scheduled run; orbit_workload_describe shows its last runs",
                retryable=False, project=project, env=env, component=component, kind=kind,
            )
        if dry_run:
            plan = plan_steps(kind="restart", project=project, env=env,
                              components=[component], tag=None)
            audit(actor=actor, tool="orbit_workload_restart", project=project,
                  env=env, component=component, tag="-", verdict="dry_run")
            return sourced(state, Mutation(status="dry_run", steps=plan))
        decision = confirmation(
            ctx, f"Restart {component} of {project}/{env} in {ns(project, env)}?")
        if isinstance(decision, InputRequiredResult):
            return decision
        if not decision:
            audit(actor=actor, tool="orbit_workload_restart", project=project,
                  env=env, component=component, tag="-", verdict="refused")
            return refused(f"restart of {component} on {project}/{env} was not confirmed")
        tag = resolve_restart_tag(state, project, env, component)
        if not tag:
            return failure(
                "no_tag",
                f"no tag to assert for {project}/{env}/{component}",
                next_action="ship a tag first or pass a running image",
                retryable=False,
                project=project, env=env, component=component,
            )
        opening = [
            Step(name="confirm", status="ok"),
            Step(name="catalog", status="ok", detail=f"{project}/{env} ns={hit.namespace}"),
        ]

        def run() -> Mutation:
            return execute_restart(
                state, project=project, env=env, component=component,
                tag=tag, opening=opening,
            )

        return sourced(state, start_job(
            state, project=project, env=env, fn=run, actor=actor,
            tool="orbit_workload_restart", component=component, tag=tag,
            opening=opening,
        ))
