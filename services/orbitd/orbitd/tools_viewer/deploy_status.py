"""orbit_deploy_status — desired tags vs running images, per component."""

from __future__ import annotations

from typing import Annotated, Any

from pydantic import BaseModel, Field

from orbitd.roles import CheckoutSource, Registry, Role
from orbitd.state import AppState

from .util import Ident
from .common import (
    require_env,
    as_dict, component_names, desired_tag, find_deployment, find_project, guarded,
    image_tag, list_pods, not_found, ns, pick, pod_is_active, pod_row, pod_trouble, rfc3339_now,
    selector,
)


class ComponentDeploy(BaseModel):
    component: str
    kind: str = Field(default="web", description="the catalog kind: web, worker or cron")
    desired_tag: str | None = None
    running_images: list[str] = Field(
        default_factory=list,
        description="spec.containers[*].image of each active pod (app containers, not init), in spec order",
    )
    running_digests: list[str] = Field(
        default_factory=list,
        description="status.containerStatuses[*].imageID of each active pod (the digest the kubelet pulled)",
    )
    drift: bool = Field(
        default=False,
        description="true when desired_tag is set and not every active pod runs it: no pod carries it, "
                    "or pods of another tag are still running beside it (a rollout that is under way, "
                    "or stuck: see `rollout`); for a cron component, when the CronJob's job template "
                    "does not carry it (between runs no pod exists, which is not drift)",
    )
    rollout: str | None = Field(
        default=None,
        description="the Deployment's rollout: `complete` (every replica updated, ready and available), "
                    "`progressing` (new pods are starting), or `stuck` (a new pod is in CrashLoopBackOff, "
                    "cannot pull its image or cannot start, or the Deployment passed its progress deadline: "
                    "it will not finish by waiting). null for a cron component and when there is no Deployment",
    )
    rollout_detail: str | None = Field(
        default=None,
        description="when `rollout` is not complete: the pod that holds it up, in the kubelet's words",
    )
    template_images: list[str] = Field(
        default_factory=list,
        description="cron components only: the CronJob's job template images, the thing a ship changes",
    )
    pod_age: int | None = Field(
        default=None, description="seconds since the pod was created",
    )
    generation: int | None = None
    observed_generation: int | None = None
    pods: list[str] = Field(default_factory=list)


class ReconcileStatus(BaseModel):
    """The reconcile loop's last tick for this pair (orbitd.reconcile.ReconcileTick)."""

    last_run: str = Field(description="RFC 3339 time the tick ran")
    applied: int = 0
    unchanged: int = 0
    failed: int = 0
    would_prune: list[str] = Field(
        default_factory=list,
        description="group/version/Kind/namespace/name of every object the loop would prune but never does")
    source: CheckoutSource | None = None
    mode: str | None = Field(
        default=None,
        description="on, dry-run, or 'off (pair not enabled)' when ORBITD_APPLY_PAIRS excludes the pair")
    hand_edits: list[str] = Field(
        default_factory=list,
        description="hand edits the last tick reverted: object, fields by key, field manager. A field "
                    "added by hand, a field of orbit's changed by hand (a scale, an image), and an "
                    "object deleted by hand that the tick restored")
    recent_hand_edits: list[str] = Field(
        default_factory=list,
        description="'<tick time> <edit>' for every hand edit the loop reverted in the ticks it "
                    "still holds (about the last hour), oldest first — the record of a kubectl "
                    "change the loop already undid")


class GateStatus(BaseModel):
    """What the commit gate says about the instance repository's newest commit."""

    enabled: bool
    head: str | None = Field(default=None, description="the newest commit of the instance repository")
    good: str | None = Field(default=None, description="the last commit the gate accepted: the one that is applied")
    verdict: str | None = Field(default=None, description="accepted, refused, or undecided")
    check: str | None = Field(default=None, description="the check that failed")
    sentence: str | None = Field(default=None, description="why, in one sentence")
    at: str | None = None
    note: str | None = None


class DeployStatus(BaseModel):
    project: str
    env: str
    namespace: str
    components: list[ComponentDeploy]
    now: str = Field(description="RFC 3339 timestamp of the server clock at the call")
    reconcile: ReconcileStatus | None = Field(
        default=None,
        description="the reconcile loop's last tick for this pair; null when the loop is off "
                    "(ORBITD_APPLY=off) or has not reached the pair yet")
    gate: GateStatus | None = Field(
        default=None,
        description="the commit gate's verdict on the instance repository's newest commit; a refused "
                    "commit is not applied and `good` is the commit that keeps running")


def register(registry: Registry, state: AppState) -> None:

    @registry.tool(role=Role.viewer, read_only=True, idempotent=True)
    def orbit_deploy_status(
        project: Annotated[Ident, Field(description="Project id from orbit_project_list.")],
        env: Annotated[Ident, Field(description="Environment name, e.g. prod or dev.")],
    ) -> DeployStatus:
        """Compare desired image tags from catalog/tags.yaml with the images the pod
        spec pins in namespace `<project>-<env>`. Pods are selected by the chart labels
        `app.kubernetes.io/instance=<project>` and `app.kubernetes.io/name=<component>`.
        `running_images` is every `spec.containers[*].image` on those pods (app
        containers, not init), in spec order. The kubelet's
        `status.containerStatuses[].image` is ignored: that field names whichever tag
        the node cache holds for the digest. `running_digests` is every
        `status.containerStatuses[*].imageID` so a reader can still see the digest
        that actually runs. Drift is true when the desired tag is set and not every
        active pod runs it: no pod carries it, or pods of another tag still run beside
        it. `rollout` says which: `complete`, `progressing` (new pods are starting) or
        `stuck` (a new pod is in CrashLoopBackOff, cannot pull its image or cannot
        start: it will not finish by waiting), with `rollout_detail` naming the pod and
        the reason. For a `kind: cron` component (a scheduled job, with a pod only
        while a run lasts) drift is judged on the CronJob's job template instead, listed as
        `template_images`. `pod_age` is seconds since the pod was created (the youngest
        running pod). `now` is the server clock as RFC 3339. `reconcile` is the
        reconcile loop's last tick for the pair ({last_run, applied, unchanged, failed,
        would_prune, source, hand_edits}), null when ORBITD_APPLY is off or no tick has
        run yet; `recent_hand_edits` lists every hand edit (a kubectl change) the loop
        reverted in about the last hour — object, fields by key and the field manager
        that wrote them, never a value — so a change already undone can still be named:
        a field added by hand, a field changed by hand (`kubectl scale`, `kubectl set
        image`), and an object deleted by hand, which the loop restores.
        `gate` is the commit gate: `verdict` is `accepted` when the instance repository's
        newest commit (`head`) passed every check and is what orbitd applies; `refused`
        when a check failed, with `check` and its `sentence`, in which case nothing from
        `head` is applied, `good` is the commit that keeps running, and no ship, monitor
        or feature change can land until a commit passes; `undecided` when a check could
        not be decided yet (the cluster did not answer).
        Example: orbit_deploy_status(project="demo", env="prod")."""
        return guarded(lambda: _status(state, project, env))


def _status(state: AppState, project: str, env: str) -> DeployStatus:
    entry = find_project(state, project)
    if entry is None:
        return not_found(f"project {project!r} is not in the catalog", project=project)
    if (missing := require_env(entry, project, env)) is not None:
        return missing
    kube = state.backends.kube
    namespace = ns(project, env)
    rows: list[ComponentDeploy] = []
    kinds = {str(c.get("name")): str(c.get("kind") or "web")
             for c in entry.get("components") or [] if isinstance(c, dict)}
    for component in component_names(entry):
        rows.append(_component(state, kube, project, env, namespace, component,
                               kinds.get(component, "web")))
    return DeployStatus(
        project=project, env=env, namespace=namespace, components=rows, now=rfc3339_now(),
        reconcile=_reconcile(state, project, env),
        gate=_gate(state),
    )


def _gate(state: AppState) -> GateStatus | None:
    """The gate's current verdict; evaluating is one `git rev-parse` for a commit already
    decided, and decides a new one."""
    try:
        from ..gate import gate_of
        return GateStatus(**gate_of(state).evaluate().dump())
    except Exception:
        return None


def _reconcile(state: AppState, project: str, env: str) -> ReconcileStatus | None:
    runner = getattr(state, "reconcile", None)
    if runner is None:
        return None
    tick = runner.latest(project, env)
    if tick is None:
        return None
    recent = [f"{t.last_run} {e}" for t in runner.history(project, env) for e in t.hand_edits]
    return ReconcileStatus(**tick.model_dump(), recent_hand_edits=recent[-10:])


def _spec_images(pod: Any) -> list[str]:
    """Every `spec.containers[*].image` (app containers, not init), in spec order."""
    spec = as_dict(pick(as_dict(pod), "spec") or {})
    images: list[str] = []
    for raw in pick(spec, "containers") or []:
        image = pick(as_dict(raw), "image")
        if image:
            images.append(str(image))
    return images


def _status_image_ids(pod: Any) -> list[str]:
    """Every `status.containerStatuses[*].imageID` (the digest the kubelet pulled)."""
    status = as_dict(pick(as_dict(pod), "status") or {})
    ids: list[str] = []
    for raw in pick(status, "containerStatuses", "container_statuses") or []:
        image_id = pick(as_dict(raw), "imageID", "image_id")
        if image_id:
            ids.append(str(image_id))
    return ids


def _cron_template_images(kube: Any, namespace: str, name: str) -> list[str] | None:
    """The CronJob's job template container images, or None when it cannot be read."""
    try:
        resource = kube.dynamic.resources.get(api_version="batch/v1", kind="CronJob")
        cron = as_dict(resource.get(name=name, namespace=namespace))
    except Exception:
        return None
    job = as_dict(pick(as_dict(pick(cron, "spec") or {}), "jobTemplate", "job_template") or {})
    tspec = as_dict(pick(as_dict(pick(as_dict(pick(job, "spec") or {}), "template") or {}), "spec") or {})
    return [str(pick(as_dict(c), "image")) for c in pick(tspec, "containers") or [] if pick(as_dict(c), "image")]


def _component(state: AppState, kube: Any, project: str, env: str,
               namespace: str, component: str, kind: str = "web") -> ComponentDeploy:
    desired = desired_tag(state, project, env, component)
    pods = [p for p in list_pods(kube, namespace, selector(project, component)) if pod_is_active(p)]
    rows = [pod_row(p) for p in pods]
    images: list[str] = []
    digests: list[str] = []
    for p in pods:
        images.extend(_spec_images(p))
        digests.extend(_status_image_ids(p))
    ages = [r["age"] for r in rows if r.get("age") is not None]
    # Per pod: a pod runs the desired tag when one of its app containers carries it. One
    # pod of another tag beside the new ones is a rollout that has not finished; called
    # "no drift" it hid a new version crash-looping beside the old one for ten minutes
    # (QA of v1.0.0, F17).
    carrying = [any(desired == img or image_tag(img) == desired for img in _spec_images(p)) for p in pods]
    drift = bool(desired) and not (carrying and all(carrying))
    template: list[str] = []
    if kind == "cron":
        # A scheduled job has a pod only while a run lasts: judge the CronJob, which is
        # what a ship changes, not the absence of a pod between runs.
        template = _cron_template_images(kube, namespace, f"{project}-{component}") or []
        drift = bool(desired) and not any(desired == img or image_tag(img) == desired for img in template)
    dep = find_deployment(kube, namespace, project, component)
    generation = observed = None
    rollout = rollout_detail = None
    if dep is not None:
        data = as_dict(dep)
        generation = _as_int(pick(as_dict(pick(data, "metadata") or {}), "generation"))
        observed = _as_int(pick(as_dict(pick(data, "status") or {}),
                                "observedGeneration", "observed_generation"))
        if kind != "cron":
            rollout, rollout_detail = _rollout(data, pods, generation, observed)
    return ComponentDeploy(
        component=component,
        kind=kind,
        template_images=template,
        desired_tag=desired,
        running_images=images,
        running_digests=digests,
        drift=drift,
        rollout=rollout,
        rollout_detail=rollout_detail,
        pod_age=min(ages) if ages else None,
        generation=generation,
        observed_generation=observed,
        pods=[r["pod"] for r in rows if r.get("pod")],
    )


def _rollout(dep: dict[str, Any], pods: list[Any], generation: int | None,
             observed: int | None) -> tuple[str, str | None]:
    """(`complete` | `progressing` | `stuck`, what holds it up) for one Deployment."""
    spec = as_dict(pick(dep, "spec") or {})
    status = as_dict(pick(dep, "status") or {})
    want = _as_int(pick(spec, "replicas"))
    want = 1 if want is None else want
    counts = {key: _as_int(pick(status, key, snake)) or 0 for key, snake in (
        ("replicas", "replicas"), ("updatedReplicas", "updated_replicas"),
        ("readyReplicas", "ready_replicas"), ("availableReplicas", "available_replicas"))}
    settled = (generation is not None and observed == generation
               and counts["updatedReplicas"] == counts["readyReplicas"] == counts["availableReplicas"]
               == counts["replicas"] == want)
    if settled:
        return "complete", None
    troubles = [t for t in (pod_trouble(p) for p in pods) if t is not None]
    stuck = next((t for t in troubles if t["stuck"]), None)
    if stuck is not None:
        return "stuck", stuck["sentence"]
    for raw in pick(status, "conditions") or []:
        cond = as_dict(raw)
        if pick(cond, "type") == "Progressing" and str(pick(cond, "status")) == "False":
            return "stuck", (f"the Deployment stopped progressing ({pick(cond, 'reason')}): "
                             f"{str(pick(cond, 'message') or '')[:200]}")
    waiting = troubles[0]["sentence"] if troubles else None
    return "progressing", waiting or (f"{counts['readyReplicas']} of {want} replica(s) ready, "
                                      f"{counts['updatedReplicas']} updated")


def _as_int(value: Any) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None
