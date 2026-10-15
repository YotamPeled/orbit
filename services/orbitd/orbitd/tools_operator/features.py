"""orbit_features_set — admin: switch one optional feature of the platform on or off."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated, Any

from mcp.server.mcpserver import Context
from mcp_types import InputRequiredResult
from pydantic import Field

from .. import features as features_module
from .. import features_converge as converge
from .. import features_job
from .. import orbit_features as rules
from ..backends import BackendError, BackendUnconfigured
from ..deploy import (
    BUDGET_SPENT, actor_of, audit, budget_spent, parse_commit, runtime_of, scrub, start_job,
)
from ..deploy import _mutation, _wait_checkout  # noqa: PLC2701 - the deploy job's own helpers
from ..roles import Mutation, Registry, Role, Step, failure
from ..source import read_source, sourced
from ..state import AppState
from ..tools import confirmation, refused

TOOL = "orbit_features_set"
UNIT = "features"
ACTION = "features-set"
FEATURES_SCRIPT = "/app/scripts/features-commit.sh"
FEATURE_PATTERN = r"^[a-z][a-z0-9-]{0,39}$"
CHECKOUT_WAIT_S = 180.0
# The converge Job's own deadline, plus the time to schedule it and read its report.
CONVERGE_WAIT_S = features_job.ACTIVE_DEADLINE_S + 120.0


def register(registry: Registry, state: AppState) -> None:

    @registry.tool(role=Role.admin, read_only=False, destructive=True)
    def orbit_features_set(
        feature: Annotated[str, Field(pattern=FEATURE_PATTERN,
                                      description="A feature of the platform, as orbit_features_list names it (logs, traces, ...).")],
        enabled: Annotated[bool, Field(description="true turns the feature on, false turns it off.")],
        dry_run: Annotated[bool, Field(
            description="Report what would be applied and retired; do not confirm or write.")] = False,
        ctx: Context = None,  # type: ignore[assignment]
    ) -> Mutation | InputRequiredResult:
        """Turn one optional feature of the platform on or off. Validates first, against
        the features registry and instance/config.yaml in the checkout: an unknown
        feature, a feature turned on without what it requires (logs and traces need
        objectStorage.buckets.platform) or one turned off while another feature needs
        it is `refused` with a sentence saying which, before any write. A feature
        already in the wanted state on a cluster that already matches is `applied`
        with `evidence.unchanged`. `dry_run` returns the steps a converge would take in
        `evidence.steps` with no confirmation. Otherwise confirms (the confirmation
        names the Applications that will be applied or retired), then starts a job under
        the features lock with steps `confirm, validate, commit, checkout, converge`:
        `commit` runs the features commit script (`features-commit.sh <feature>
        on|off`, `ORBIT_ACTION=features-set`, `ORBIT_ACTOR=<actor>`), which rewrites
        the `features:` block, re-renders the tree and lands one commit carrying the
        trailers `Orbit-Action`, `Orbit-Actor`, `Orbit-Role` and `Orbit-Feature`;
        `checkout` waits for this orbitd's checkout to carry that commit; `converge`
        starts a Kubernetes Job in namespace `orbit` that applies the Applications of a
        feature turned on, re-applies with prune the Applications that change with it,
        retires the Applications of a feature turned off, and waits for the workloads
        of a feature turned on to be ready. One running job per features lock; the job
        id is in `evidence.job_id`, reclaim it with orbit_job_wait (a converge can take
        several minutes). `evidence.converge` carries the Job's own report: per feature
        `{desired, was, action}`, and per step `{action, app, for, ok, error}`. A
        converge that fails leaves the commit landed: orbitd's reconcile loop starts
        another converge while the cluster is behind the checkout. Admin only.
        Example: orbit_features_set(feature="traces", enabled=false)."""
        return _set(state, feature, enabled, dry_run, ctx)


def _fail(state: AppState, actor: str, feature: str, code: str, what: str, **kw: Any) -> Any:
    audit(actor=actor, tool=TOOL, project=UNIT, env="", component=feature, tag="-", verdict="failed")
    return failure(code, what, feature=feature, **kw)


def _set(state: AppState, feature: str, enabled: bool, dry_run: bool, ctx: Context | None) -> Any:
    actor = actor_of()
    word = "on" if enabled else "off"
    try:
        root = Path(state.backends.instance.require())
    except BackendUnconfigured as e:
        audit(actor=actor, tool=TOOL, project=UNIT, env="", component=feature, tag="-", verdict="failed")
        return e.as_failure()
    try:
        registry = rules.load_registry(root)
    except rules.RegistryMissing:
        return _fail(state, actor, feature, "unsupported",
                     "this instance's core predates features: its checkout carries no provisioning/features.yaml",
                     next_action="upgrade the instance's core (the orbit-upgrade skill), then switch features")
    except rules.FeatureError as e:
        return _fail(state, actor, feature, "refused", str(e))
    try:
        text = (root / features_module.CONFIG_REL).read_text(encoding="utf-8")
        _, want = rules.set_feature(text, registry, feature, enabled)
        current = features_module.read(root).on
    except OSError:
        return _fail(state, actor, feature, "backend_error", "instance/config.yaml cannot be read in the checkout",
                     retryable=True)
    except rules.FeatureError as e:
        return _fail(state, actor, feature, "refused", str(e),
                     next_action="call orbit_features_list to see each feature and what it requires")
    desired = dict(current)
    desired.update(want)
    try:
        plan = converge.make_plan(registry, desired, converge.ClusterOps(root, state.backends.kube),
                                  assumed=converge.assumed_states(root, registry))
    except (BackendError, BackendUnconfigured) as e:
        audit(actor=actor, tool=TOOL, project=UNIT, env="", component=feature, tag="-", verdict="failed")
        return e.as_failure()
    plan_steps = [step.dump() for step in plan.steps]
    evidence: dict[str, Any] = {"feature": feature, "enabled": enabled, "features": want, "steps": plan_steps}
    validated = Step(name="validate", status="ok", detail=f"{feature} {word}: " + ", ".join(
        f"{name}={'on' if on else 'off'}" for name, on in want.items()))
    if current.get(feature) == enabled and not plan.changed:
        audit(actor=actor, tool=TOOL, project=UNIT, env="", component=feature, tag=word, verdict="applied")
        evidence["unchanged"] = True
        return sourced(state, Mutation(status="applied", steps=[validated], evidence=evidence))
    if dry_run:
        audit(actor=actor, tool=TOOL, project=UNIT, env="", component=feature, tag=word, verdict="dry_run")
        return sourced(state, Mutation(status="dry_run", steps=[validated], evidence=evidence))
    settings = features_job.JobSettings.from_env()
    if settings is None:
        return _fail(state, actor, feature, "backend_unconfigured",
                     "this orbitd cannot start a converge Job: ORBITD_IMAGE, ORBITD_GIT_SYNC_IMAGE and "
                     "ORBITD_INSTANCE_REPO are not all set",
                     next_action="switch the feature from the desk: `orbit features set`, then `orbit features converge`")
    apply_apps = [s.app for s in plan.steps if s.action == "apply"]
    retire_apps = [s.app for s in plan.steps if s.action == "retire"]
    reapply = [s.app for s in plan.steps if s.action == "apply-prune"]
    parts = [f"Turn the {feature} feature {word.upper()} on this platform?"]
    if apply_apps:
        parts.append("Applies: " + ", ".join(apply_apps) + ".")
    if retire_apps:
        parts.append("Retires (their pods stop and their objects are deleted; data already in the "
                     "bucket stays): " + ", ".join(retire_apps) + ".")
    if reapply:
        parts.append("Re-applies with prune: " + ", ".join(reapply) + ".")
    verdict = confirmation(ctx, " ".join(parts)) if ctx is not None else True
    if isinstance(verdict, InputRequiredResult):
        return verdict
    if not verdict:
        audit(actor=actor, tool=TOOL, project=UNIT, env="", component=feature, tag=word, verdict="refused")
        return refused(f"{feature} {word} was not confirmed")
    opening = [Step(name="confirm", status="ok"), validated]

    def run() -> Mutation:
        return _execute(state, feature=feature, word=word, actor=actor, settings=settings,
                        evidence=dict(evidence), opening=list(opening))

    runtime = runtime_of(state)
    return sourced(state, start_job(
        state, project=UNIT, env="", fn=run, actor=actor, tool=TOOL, component=feature, tag=word,
        opening=opening, budget_s=float(runtime.runner_timeout_s) + CHECKOUT_WAIT_S + CONVERGE_WAIT_S + 60.0,
        timeout_step=("converge", "TIMED OUT")))


def _execute(state: AppState, *, feature: str, word: str, actor: str,
             settings: features_job.JobSettings, evidence: dict[str, Any], opening: list[Step]) -> Mutation:
    runtime = runtime_of(state)
    steps = list(opening)
    source = read_source(state)

    def done(status: str, commit: str | None = None) -> Mutation:
        return _mutation(status=status, steps=steps, commit=commit, evidence=evidence, source=read_source(state))

    if budget_spent(state, UNIT, ""):
        steps.append(Step(name="commit", status="failed", detail=BUDGET_SPENT))
        return done("failed")
    try:
        result = runtime.runner.run([runtime.features_script, feature, word],
                                    {"ORBIT_ACTION": ACTION, "ORBIT_ACTOR": actor})
    except Exception as e:
        steps.append(Step(name="commit", status="failed", detail=scrub(f"{e.__class__.__name__}: {e}")))
        return done("failed")
    output = scrub((result.stdout or "") + "\n" + (result.stderr or ""))
    commit = parse_commit(result.stdout or "")
    if result.returncode != 0 or not commit:
        lines = [line for line in output.splitlines() if line.strip() and not line.startswith("STEP ")]
        steps.append(Step(name="commit", status="failed",
                          detail=(lines[-1] if lines else "the features commit script failed")[:600]))
        return done("failed", commit)
    steps.append(Step(name="commit", status="ok", detail=commit))
    lag = _wait_checkout(state, commit, deadline=runtime.clock() + CHECKOUT_WAIT_S)
    if lag is not None:
        steps.append(Step(name="checkout", status="failed", detail=lag.detail))
        return done("failed", commit)
    steps.append(Step(name="checkout", status="ok", detail=source.lag_note if source else None))
    state.features.snapshot(fresh=True)
    kube = state.backends.kube
    try:
        running = features_job.active(kube)
        if running is not None:
            # a converge started by the reconcile loop for an earlier commit: let it end,
            # then run this commit's
            name = (running.get("metadata") or {}).get("name") or ""
            features_job.wait(kube, name, clock=runtime.clock, sleep=runtime.sleeper, timeout_s=CONVERGE_WAIT_S)
        name = features_job.start(kube, settings, commit)
        evidence["converge_job"] = name
        outcome, report = features_job.wait(kube, name, clock=runtime.clock, sleep=runtime.sleeper,
                                            timeout_s=CONVERGE_WAIT_S)
    except features_job.ConvergeUnavailable as e:
        steps.append(Step(name="converge", status="failed", detail=str(e)))
        return done("failed", commit)
    except Exception as e:
        steps.append(Step(name="converge", status="failed", detail=scrub(f"{e.__class__.__name__}: {e}"[:600])))
        return done("failed", commit)
    if report is not None:
        evidence["converge"] = {"ok": report.get("ok"), "features": report.get("features"),
                                "steps": report.get("steps"), "failed": report.get("failed")}
    if outcome != "succeeded" or (report is not None and report.get("ok") is False):
        bad = next((s for s in (report or {}).get("steps") or [] if s.get("ok") is False), None)
        detail = (f"{bad.get('action')} {bad.get('app')}: {bad.get('error')}" if bad
                  else f"the converge Job {name} {outcome}")
        steps.append(Step(name="converge", status="failed", detail=str(detail)[:600]))
        evidence["verdict"] = "LANDED NOT CONVERGED"
        return done("failed", commit)
    ran = [s for s in (report or {}).get("steps") or [] if s.get("ok")]
    steps.append(Step(name="converge", status="ok", detail=f"{len(ran)} step(s) by Job {name}"))
    return done("applied", commit)
