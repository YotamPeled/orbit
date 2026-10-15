"""orbit_deploy_reconcile — admin: plan, apply and prune one pair through the applier."""

from __future__ import annotations

import uuid
from typing import Annotated, Any

from mcp.server.mcpserver import Context
from mcp_types import InputRequiredResult
from pydantic import Field

from .. import applier
from ..deploy import actor_of, audit, busy_failure, runtime_of
from ..roles import Mutation, Registry, Role, Step
from ..source import sourced
from ..state import AppState
from ..tools import confirmation, refused
from ..tools_viewer._util import ID_RE
from ..tools_viewer.common import env_names, find_project, guarded, not_found

TOOL = "orbit_deploy_reconcile"


def register(registry: Registry, state: AppState) -> None:

    @registry.tool(role=Role.admin, read_only=False, destructive=True)
    def orbit_deploy_reconcile(
        project: Annotated[str, Field(pattern=ID_RE.pattern, description="Catalog project id.")],
        env: Annotated[str, Field(pattern=ID_RE.pattern, description="Environment id (prod, dev, ...).")],
        dry_run: Annotated[bool, Field(
            description="Plan and send every request with dryRun=All; nothing is written.")] = True,
        ctx: Context = None,  # type: ignore[assignment]
    ) -> Mutation | InputRequiredResult:
        """Render the desired objects for a project/environment, server-side apply
        them in install order with field manager `orbit`, then delete the prune set:
        the previous inventory (ConfigMap orbit-inventory-<project>-<env> in `orbit`)
        minus the render, after the hard rules (never a TLS Secret, a Namespace or a
        PersistentVolumeClaim; only kinds the generator or chart writes; only the
        pair's namespaces; only live objects labelled for the pair). `dry_run` (the
        default) sends every request with dryRun=All and reports what would happen;
        `dry_run=false` confirms (the confirmation names every object to be deleted:
        apiVersion, kind, namespace, name), holds the pair's job lock, applies, writes
        the inventory (this run's render plus previous entries this run did not
        delete), then prunes, then writes the inventory without the entries this run
        deleted. Refused unless ORBITD_APPLY is `dry-run` or `on`;
        `dry-run` forces dry_run. `evidence` carries `objects` (apply order),
        `prune` (named in full, before any result), `withheld` (dropped objects a rule
        kept, with the rule), `skipped` (rendered objects the applier never writes:
        its own orbit-* RBAC grant), `apply.results` and `prune_run.results` per
        object (`applied|unchanged|failed`, `deleted|absent|withheld|failed`), and
        `inventory`. Prune re-reads each object and deletes with resourceVersion/uid
        preconditions; one that changed since the plan is `withheld`, not deleted.
        Apply stops at the first failure; nothing is rolled back. Admin only.
        Example: orbit_deploy_reconcile(project="demo", env="dev", dry_run=true)."""
        return guarded(lambda: _run(state, project, env, dry_run, ctx))


def _run(state: AppState, project: str, env: str, dry_run: bool, ctx: Context | None) -> Any:
    actor = actor_of()
    entry = find_project(state, project)
    if entry is None:
        return not_found(f"project {project!r} is not in the catalog", project=project)
    if env not in env_names(entry):
        return not_found(f"env {env!r} is not in project {project!r}", project=project, env=env)
    mode = applier.apply_mode(state)
    plan = applier.plan(state, project, env)
    evidence = _plan_evidence(plan, mode=mode)
    steps = [
        Step(name="catalog", status="ok", detail=f"{project}/{env} ns={plan.namespace}"),
        Step(name="plan", status="ok",
             detail=f"{len(plan.refs)} objects to apply, {len(plan.prune)} to prune, "
                    f"{len(plan.withheld)} withheld, {len(plan.skipped)} skipped"),
    ]
    if mode == applier.MODE_OFF:
        audit(actor=actor, tool=TOOL, project=project, env=env, component="-", tag="-",
              verdict="refused")
        why = f"ORBITD_APPLY=off: apply is disabled on this orbitd; set ORBITD_APPLY=dry-run or on"
        steps.append(Step(name="apply", status="skipped", detail=why))
        steps.append(Step(name="prune", status="skipped", detail=why))
        return sourced(state, Mutation(status="refused", steps=steps, evidence=evidence,
                                       source=plan.source))
    if not applier.pair_enabled(state, project, env) and not dry_run:
        audit(actor=actor, tool=TOOL, project=project, env=env, component="-", tag="-",
              verdict="refused")
        why = applier.PAIR_NOT_ENABLED_DETAIL
        steps.append(Step(name="apply", status="skipped", detail=why))
        steps.append(Step(name="prune", status="skipped", detail=why))
        return sourced(state, Mutation(status="refused", steps=steps, evidence=evidence,
                                       source=plan.source))
    effective_dry = dry_run or mode == applier.MODE_DRY_RUN
    if not effective_dry:
        decision = confirmation(ctx, _confirmation_text(plan))
        if isinstance(decision, InputRequiredResult):
            return decision
        if not decision:
            audit(actor=actor, tool=TOOL, project=project, env=env, component="-", tag="-",
                  verdict="refused")
            return sourced(state, refused(f"reconcile of {project}/{env} was not confirmed"))
    runtime = runtime_of(state)
    holder = f"reconcile-{uuid.uuid4().hex[:12]}"
    if not effective_dry:
        held = runtime.locks.try_acquire(project, env, holder)
        if held is not None:
            audit(actor=actor, tool=TOOL, project=project, env=env, component="-", tag="-",
                  verdict="busy")
            return busy_failure(project, env, held)
    try:
        applied = applier.apply(state, plan, dry_run=effective_dry)
        evidence["apply"] = applied.model_dump(mode="json")
        steps.append(Step(
            name="apply", status="ok" if applied.ok else "failed",
            detail=_summary(applied.results, applied.dry_run)))
        if applied.inventory is not None:
            evidence["inventory"]["written"] = True
            steps.append(Step(name="inventory", status="ok", detail=applied.inventory.line))
        if not applied.ok:
            steps.append(Step(name="prune", status="skipped", detail="apply failed"))
            verdict = "failed"
            status = "failed"
        else:
            pruned = applier.prune(state, plan, dry_run=effective_dry)
            evidence["prune_run"] = pruned.model_dump(mode="json")
            steps.append(Step(
                name="prune", status="ok" if pruned.ok else "failed",
                detail=_summary(pruned.results, pruned.dry_run)))
            if not applied.dry_run:
                deleted = {r.ref.key for r in pruned.results if r.result == "deleted"}
                applier.write_inventory(state, plan, deleted=deleted)
            status = "failed" if not pruned.ok else ("dry_run" if applied.dry_run else "applied")
            verdict = status
    finally:
        if not effective_dry:
            runtime.locks.release(project, env, holder)
    audit(actor=actor, tool=TOOL, project=project, env=env, component="-", tag="-", verdict=verdict)
    return sourced(state, Mutation(status=status, steps=steps,  # type: ignore[arg-type]
                                   commit=plan.source.commit or None,
                                   evidence=evidence, source=plan.source))


def _confirmation_text(plan: applier.Plan) -> str:
    """Names every object prune will delete, so the confirmer sees the set, not a count."""
    head = (f"Apply {len(plan.refs)} objects and delete {len(plan.prune)} for "
            f"{plan.project}/{plan.env} (render {plan.render_sha[:12]})?")
    if not plan.prune:
        return head + " Nothing will be deleted."
    lines = [f"  - {r.api_version or 'v1'} {r.kind} {r.namespace or '(cluster)'}/{r.name}"
             for r in plan.prune]
    return head + " Objects to delete:\n" + "\n".join(lines)


def _plan_evidence(plan: applier.Plan, *, mode: str) -> dict[str, Any]:
    inv = applier.inventory_ref(plan.project, plan.env)
    return {
        "project": plan.project, "env": plan.env, "namespace": plan.namespace,
        "mode": mode, "render_sha": plan.render_sha,
        "counts": {"objects": len(plan.refs), "prune": len(plan.prune),
                   "withheld": len(plan.withheld), "previous": len(plan.previous),
                   "skipped": len(plan.skipped)},
        "objects": [r.model_dump() for r in plan.refs],
        "prune": [r.model_dump() for r in plan.prune],
        "withheld": [w.model_dump() for w in plan.withheld],
        "skipped": [w.model_dump() for w in plan.skipped],
        "inventory": {"name": inv.name, "namespace": inv.namespace,
                      "found": plan.inventory_found, "written": False},
    }


def _summary(results: list[applier.ObjectResult], dry: bool) -> str:
    counts: dict[str, int] = {}
    for r in results:
        counts[r.result] = counts.get(r.result, 0) + 1
    text = ", ".join(f"{n} {k}" for k, n in sorted(counts.items())) or "nothing to do"
    for r in results:
        if r.result == "applied" and r.reason and "a hand edit:" in r.reason:
            text += f"; {r.ref.kind} {r.ref.namespace}/{r.ref.name}: {r.reason}"
    failed = next((r for r in results if r.result == "failed"), None)
    if failed is not None:
        text += f"; stopped at {failed.ref.kind} {failed.ref.namespace}/{failed.ref.name}: {failed.reason}"
    return f"dryRun=All: {text}" if dry else text
