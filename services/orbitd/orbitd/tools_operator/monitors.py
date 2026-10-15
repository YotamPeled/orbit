"""orbit_monitor_upsert, orbit_monitor_delete, orbit_monitor_silence and orbit_monitor_unsilence."""

from __future__ import annotations

import os
import re
import tempfile
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from typing import Annotated, Any

import yaml
from mcp.server.mcpserver import Context
from mcp_types import InputRequiredResult
from pydantic import Field

from .. import monitors_apply
from ..applier import MODE_ON, apply_mode
from ..backends import BackendError, BackendUnconfigured
from ..deploy import (
    _apply_deadline,
    _mutation,
    _wait_checkout,
    actor_of,
    audit,
    bump_fail_detail,
    parse_commit,
    runtime_of,
    scrub,
    BUDGET_SPENT, budget_spent, start_job,
)
from ..monitors import NAME_RE, render_monitors, validate
from ..monitors_apply import UNIT
from ..roles import Mutation, Registry, Role, Step, failure
from ..source import read_source, sourced
from ..state import AppState
from ..tools import confirmation, refused

TOOL_UPSERT = "orbit_monitor_upsert"
TOOL_DELETE = "orbit_monitor_delete"
TOOL_SILENCE = "orbit_monitor_silence"
TOOL_UNSILENCE = "orbit_monitor_unsilence"

ACTION_UPSERT = "monitor-upsert"
ACTION_DELETE = "monitor-delete"

SILENCES_PATH = "/api/v2/silences"
DURATION_RE = re.compile(r"[1-9][0-9]*[mhd]")
DURATION_S = {"m": 60, "h": 3600, "d": 86400}
MIN_SILENCE_S = 60
MAX_SILENCE_S = 7 * 86400


def register(registry: Registry, state: AppState) -> None:

    @registry.tool(role=Role.operator, read_only=False, destructive=False)
    def orbit_monitor_upsert(
        monitor: Annotated[dict[str, Any], Field(
            description="Monitor body: {name, project, env, kind, condition, severity, summary, ...}. "
                        "`notify: [\"telegram\"]` sends its alert to the human chat (the "
                        "`alerts-telegram` feature); left out, nobody is messaged.")],
        dry_run: Annotated[bool, Field(
            description="Render and return the object refs; do not confirm or write.")] = False,
        ctx: Context = None,  # type: ignore[assignment]
    ) -> Mutation | InputRequiredResult:
        """Create or replace one monitor file, then apply the monitors unit. Validates
        the body against the catalog and the instance's `lokiRuler` first: an invalid
        monitor is a tool error naming the field, before any write. Who hears a
        monitor fire is the body's `notify`: `["telegram"]` sends the alert to the
        human chat (needs the `alerts-telegram` feature); left out or `[]`, the alert
        is recorded by orbitd (orbit_monitor_status, orbit_alerts_active) and nobody
        is messaged. `evidence.reaches` says which of the two this body is. `dry_run` returns
        the rendered objects' refs in `evidence` with no confirmation. Otherwise
        confirms (the confirmation names the monitor), then starts a job under the
        monitors lock with steps `confirm, validate, commit, apply`: `commit` writes
        the body through the monitor commit script (`monitor-commit.sh upsert <name>`,
        `ORBIT_ACTION=monitor-upsert`, `ORBIT_ACTOR=<actor>`, `MONITOR_FILE` carrying
        the body), `apply` waits for the checkout to carry that commit and applies the
        monitors unit when ORBITD_APPLY is `on`, else that step is skipped. One running
        job per monitors lock; the job id is in `evidence.job_id`, reclaim it with
        orbit_job_wait. The Mutation's `source` is {commit, age_s, lag_note}.
        Example: orbit_monitor_upsert(monitor={"name": "queue-backlog", ...})."""
        return _upsert(state, monitor, dry_run, ctx)

    @registry.tool(role=Role.operator, read_only=False, destructive=True)
    def orbit_monitor_delete(
        name: Annotated[str, Field(pattern=NAME_RE.pattern,
                                   description="Monitor name; the file monitors/<name>.yaml.")],
        dry_run: Annotated[bool, Field(
            description="Render and return the object refs; do not confirm or write.")] = False,
        ctx: Context = None,  # type: ignore[assignment]
    ) -> Mutation | InputRequiredResult:
        """Delete one monitor file, then prune exactly that monitor's objects.
        An unknown name is `not_found`, unless objects it once rendered are still in the
        cluster (a delete that did not finish): then the call confirms naming them and
        its job prunes them, with `commit` skipped and `evidence.left_behind` true.
        `dry_run` returns the rendered objects' refs
        in `evidence` with no confirmation. Otherwise confirms (the confirmation names
        every object that will be deleted), then starts a job under the monitors lock
        with steps `confirm, commit, prune`: `commit` runs the monitor commit script
        (`monitor-commit.sh delete <name>`, `ORBIT_ACTION=monitor-delete`, no
        `MONITOR_FILE`), `prune` waits for the checkout to carry that commit and
        deletes only the refs whose `orbit.local/monitor` label is this monitor when
        ORBITD_APPLY is `on`, else that step is skipped. One running job per monitors
        lock; the job id is in `evidence.job_id`, reclaim it with orbit_job_wait.
        Example: orbit_monitor_delete(name="queue-backlog")."""
        return _delete(state, name, dry_run, ctx)

    @registry.tool(role=Role.operator, read_only=False, destructive=False)
    def orbit_monitor_silence(
        name: Annotated[str, Field(pattern=NAME_RE.pattern,
                                   description="Monitor / alert name to silence.")],
        duration: Annotated[str, Field(
            description="How long: <n>[mhd], from 1 minute to 7 days (e.g. 2h).")],
        reason: Annotated[str, Field(description="Why this silence exists; recorded on it.")],
    ) -> Mutation:
        """Silence one monitor's alert in Alertmanager for `duration`. An unknown name
        is `not_found`; `duration` must match `^[1-9][0-9]*[mhd]$` and fall between 1
        minute and 7 days; `reason` must be non-empty. No confirmation: the call POSTs
        one silence to /api/v2/silences with exact `alertname=<name>` and
        `orbit_monitor=<name>` matchers, from
        now until now plus `duration`, `createdBy` the caller and `comment` the reason;
        the silence id comes back in `evidence.silence_id`. The call is audited.
        orbit_monitor_status lists a monitor's silences in force; orbit_monitor_unsilence
        lifts them before they run out.
        Example: orbit_monitor_silence(name="queue-backlog", duration="2h",
        reason="load test in progress")."""
        return _silence(state, name, duration, reason)

    @registry.tool(role=Role.operator, read_only=False, destructive=False)
    def orbit_monitor_unsilence(
        name: Annotated[str, Field(pattern=NAME_RE.pattern,
                                   description="Monitor / alert name whose silences to lift.")],
    ) -> Mutation:
        """Lift every silence in force for one monitor, so its alert is heard again
        now instead of when the silence runs out. An unknown name is `not_found`. No
        confirmation: the call reads Alertmanager's silences, takes those in force
        whose matcher is exactly `orbit_monitor=<name>` (what orbit_monitor_silence
        writes) and expires each (DELETE /api/v2/silence/<id>). `applied` with
        `evidence.lifted` the ids, `[]` when the monitor was not silenced. If the
        monitor is firing, whoever its `notify` names is messaged again within
        Alertmanager's next group interval. The call is audited. Operator or admin.
        Example: orbit_monitor_unsilence(name="queue-backlog")."""
        return _unsilence(state, name)


# ------------------------------------------------------------------ shared

def _desired(state: AppState, tool: str, actor: str):
    """The catalog and ruler wiring monitor validation renders against."""
    try:
        catalog = state.backends.instance.projects()
        wired = monitors_apply._loki_ruler_wired(state.backends.instance.require())
    except (BackendError, BackendUnconfigured) as e:
        audit(actor=actor, tool=tool, project=UNIT, env="", component="-", tag="-",
              verdict="failed")
        return None, None, e.as_failure()
    if not isinstance(catalog, dict):
        audit(actor=actor, tool=tool, project=UNIT, env="", component="-", tag="-",
              verdict="failed")
        return None, None, failure(
            "catalog_error", "catalog/projects.yaml is not a mapping",
            next_action="repair the catalog file", retryable=False)
    return catalog, wired, None


def _body_name(monitor: Any) -> str:
    if isinstance(monitor, dict):
        name = monitor.get("name")
        if isinstance(name, str) and name:
            return name
    return "-"


def _ref_lines(objects: list[dict[str, Any]]) -> list[str]:
    """Rendered objects as `kind/namespace/name` refs, in render order."""
    lines = []
    for obj in objects:
        meta = obj.get("metadata") or {}
        lines.append(f"{obj.get('kind')}/{meta.get('namespace')}/{meta.get('name')}")
    return lines


def _full_lines(objects: list[dict[str, Any]]) -> list[str]:
    """Rendered objects named in full, for a destructive confirmation."""
    return [f"{obj.get('apiVersion') or 'v1'} {obj.get('kind')} "
            f"{(obj.get('metadata') or {}).get('namespace') or '(cluster)'}/"
            f"{(obj.get('metadata') or {}).get('name')}"
            for obj in objects]


# ------------------------------------------------------------------ upsert

def _reaches(monitor: dict[str, Any]) -> str:
    """Who hears this monitor fire, in one sentence, for the caller that did not ask.

    Seen on the test box (docs/V1.md §8): an agent asked for "a monitor that alerts"
    made one with no `notify`; it fired and nobody was messaged, and nothing the
    agent had read said so."""
    notify = monitor.get("notify") if isinstance(monitor, dict) else None
    if isinstance(notify, list) and "telegram" in notify:
        return "when it fires, its alert is sent to the human chat (telegram)"
    return ("when it fires, its alert is recorded by orbitd and nobody is messaged "
            "(`notify` is empty; `notify: [\"telegram\"]` sends it to the human chat)")


def _upsert(state: AppState, monitor: dict[str, Any], dry_run: bool,
            ctx: Context | None) -> Any:
    actor = actor_of()
    catalog, wired, failed = _desired(state, TOOL_UPSERT, actor)
    if failed is not None:
        return failed
    assert catalog is not None and wired is not None
    name = _body_name(monitor)
    filename = f"{name}.yaml" if name != "-" else "monitor.yaml"
    problems = validate(monitor, catalog=catalog, filename=filename if name != "-" else None,
                        loki_ruler_wired=wired)
    if problems:
        audit(actor=actor, tool=TOOL_UPSERT, project=UNIT, env="", component=name, tag="-",
              verdict="failed")
        return failure(
            "invalid_monitor", "; ".join(problems),
            next_action="fix the named fields and retry", retryable=False,
            monitor=name, problems=problems)
    rendered = render_monitors({filename: monitor}, catalog=catalog, loki_ruler_wired=wired)
    if rendered.errors:
        detail = "; ".join(f"{f}: {m}" for f, m in sorted(rendered.errors.items()))
        audit(actor=actor, tool=TOOL_UPSERT, project=UNIT, env="", component=name, tag="-",
              verdict="failed")
        return failure(
            "invalid_monitor", detail,
            next_action="fix the named fields and retry", retryable=False, monitor=name)
    refs = _ref_lines(rendered.objects)
    if dry_run:
        audit(actor=actor, tool=TOOL_UPSERT, project=UNIT, env="", component=name, tag="-",
              verdict="dry_run")
        return sourced(state, Mutation(
            status="dry_run",
            steps=[Step(name="validate", status="ok",
                        detail=f"monitor {name!r} renders {len(refs)} object(s); {_reaches(monitor)}")],
            evidence={"monitor": name, "objects": refs, "reaches": _reaches(monitor)}))
    decision = confirmation(
        ctx, f"Upsert monitor {name!r}? This writes monitors/{name}.yaml and applies "
             f"{len(refs)} object(s): {', '.join(refs)}.")
    if isinstance(decision, InputRequiredResult):
        return decision
    if not decision:
        audit(actor=actor, tool=TOOL_UPSERT, project=UNIT, env="", component=name, tag="-",
              verdict="refused")
        return refused(f"upsert of monitor {name!r} was not confirmed")
    opening = [
        Step(name="confirm", status="ok"),
        Step(name="validate", status="ok", detail=f"monitor {name!r} is valid"),
    ]

    def run() -> Mutation:
        return _execute_upsert(state, monitor=monitor, name=name, refs=refs, actor=actor)

    return sourced(state, start_job(
        state, project=UNIT, env="", fn=run, actor=actor, tool=TOOL_UPSERT,
        component=name, tag="-", opening=opening))


def _execute_upsert(state: AppState, *, monitor: dict[str, Any], name: str,
                    refs: list[str], actor: str) -> Mutation:
    runtime = runtime_of(state)
    since = runtime.clock()
    source = read_source(state)
    steps = [
        Step(name="confirm", status="ok"),
        Step(name="validate", status="ok", detail=f"monitor {name!r} is valid; {_reaches(monitor)}"),
    ]
    evidence: dict[str, Any] = {"monitor": name, "objects": refs, "reaches": _reaches(monitor)}
    if budget_spent(state, UNIT, ""):
        steps.append(Step(name="commit", status="failed", detail=BUDGET_SPENT))
        return _mutation(status="failed", steps=steps, evidence=evidence, source=source)
    fd, tmppath = tempfile.mkstemp(prefix="orbit-monitor-")
    try:
        with os.fdopen(fd, "w") as fh:
            fh.write(yaml.safe_dump(dict(monitor), sort_keys=False))
        try:
            result = runtime.runner.run(
                [runtime.monitor_script, "upsert", name],
                {"ORBIT_ACTION": ACTION_UPSERT, "ORBIT_ACTOR": actor,
                 "MONITOR_FILE": tmppath})
        except Exception as e:
            steps.append(Step(name="commit", status="failed",
                              detail=scrub(f"{e.__class__.__name__}: {e}")))
            return _mutation(status="failed", steps=steps, evidence=evidence, source=source)
    finally:
        try:
            os.unlink(tmppath)
        except OSError:
            pass
    output = scrub((result.stdout or "") + "\n" + (result.stderr or ""))
    parsed = parse_commit(result.stdout or "")
    if result.returncode != 0:
        steps.append(Step(name="commit", status="failed", detail=bump_fail_detail(output)))
        return _mutation(status="failed", steps=steps, commit=parsed,
                         evidence=evidence, source=source)
    if not parsed:
        steps.append(Step(name="commit", status="failed", detail="no COMMIT line"))
        return _mutation(status="failed", steps=steps, evidence=evidence, source=source)
    steps.append(Step(name="commit", status="ok", detail=parsed))
    mode = apply_mode(state)
    if mode != MODE_ON:
        # the commit landed but this orbitd applied nothing (off: no loop runs;
        # dry-run: the loop only previews): refuse rather than report applied
        steps.append(Step(name="apply", status="skipped",
                          detail=f"ORBITD_APPLY={mode}: the commit landed, nothing was applied; "
                                 f"set ORBITD_APPLY=on for this orbitd to apply it"))
        evidence["verdict"] = "LANDED NOT APPLIED"
        return _mutation(status="refused", steps=steps, commit=parsed,
                         evidence=evidence, source=source)
    lag = _wait_checkout(state, parsed, deadline=_apply_deadline(runtime, since))
    if lag is not None:
        steps.append(lag)
        return _mutation(status="failed", steps=steps, commit=parsed,
                         evidence=evidence, source=source)
    try:
        plan = monitors_apply.plan_monitors(state)
        applied = monitors_apply.apply_monitors(state, plan, dry_run=False)
    except Exception as e:
        steps.append(Step(name="apply", status="failed",
                          detail=scrub(f"{e.__class__.__name__}: {e}"[:1000])))
        return _mutation(status="failed", steps=steps, commit=parsed,
                         evidence=evidence, source=source)
    if not applied.ok:
        bad = next((r for r in applied.results if r.result == "failed"), None)
        where = (f"{bad.ref.kind} {bad.ref.namespace}/{bad.ref.name}: {bad.reason}"
                 if bad is not None else "apply failed")
        steps.append(Step(name="apply", status="failed", detail=where))
        return _mutation(status="failed", steps=steps, commit=parsed,
                         evidence=evidence, source=source)
    counts: dict[str, int] = {}
    for r in applied.results:
        counts[r.result] = counts.get(r.result, 0) + 1
    steps.append(Step(
        name="apply", status="ok",
        detail=f"{counts.get('applied', 0)} applied, {counts.get('unchanged', 0)} unchanged, "
               f"render_sha={plan.render_sha}"))
    return _mutation(status="applied", steps=steps, commit=parsed,
                     evidence=evidence, source=source)


# ------------------------------------------------------------------ delete

def _delete(state: AppState, name: str, dry_run: bool, ctx: Context | None) -> Any:
    actor = actor_of()
    if not NAME_RE.fullmatch(name):
        # the name becomes an argv word of the commit script: shape first, whatever
        # the surface accepted; the attempt is audited, the name never is
        audit(actor=actor, tool=TOOL_DELETE, project=UNIT, env="", component="-", tag="-",
              verdict="failed")
        return failure("not_found", "a monitor name is a DNS label", retryable=False)
    try:
        bodies = state.backends.instance.monitors()
    except (BackendError, BackendUnconfigured) as e:
        audit(actor=actor, tool=TOOL_DELETE, project=UNIT, env="", component=name, tag="-",
              verdict="failed")
        return e.as_failure()
    body = next((b for b in bodies
                 if isinstance(b, dict) and b.get("name") == name), None)
    if body is None:
        left = _left_behind(state, name)
        if left:
            return _delete_left_behind(state, name, left, dry_run, ctx, actor)
        audit(actor=actor, tool=TOOL_DELETE, project=UNIT, env="", component=name, tag="-",
              verdict="failed")
        return failure(
            "not_found", f"monitor {name!r} is not defined",
            next_action="call orbit_monitor_list to see defined monitors",
            retryable=False, name=name)
    catalog, wired, failed = _desired(state, TOOL_DELETE, actor)
    if failed is not None:
        return failed
    assert catalog is not None and wired is not None
    rendered = render_monitors({f"{name}.yaml": body}, catalog=catalog, loki_ruler_wired=wired)
    refs = _ref_lines(rendered.objects)
    if dry_run:
        audit(actor=actor, tool=TOOL_DELETE, project=UNIT, env="", component=name, tag="-",
              verdict="dry_run")
        return sourced(state, Mutation(
            status="dry_run",
            steps=[Step(name="plan", status="ok",
                        detail=f"monitor {name!r} renders {len(refs)} object(s)")],
            evidence={"monitor": name, "objects": refs}))
    full = _full_lines(rendered.objects)
    head = (f"Delete monitor {name!r} (monitors/{name}.yaml) and "
            f"{len(full)} object(s)?")
    message = head + (" Nothing is applied, nothing will be deleted."
                      if not full else " Objects to delete:\n" + "\n".join(f"  - {l}" for l in full))
    decision = confirmation(ctx, message)
    if isinstance(decision, InputRequiredResult):
        return decision
    if not decision:
        audit(actor=actor, tool=TOOL_DELETE, project=UNIT, env="", component=name, tag="-",
              verdict="refused")
        return refused(f"delete of monitor {name!r} was not confirmed")
    opening = [Step(name="confirm", status="ok")]

    def run() -> Mutation:
        return _execute_delete(state, name=name, refs=refs, actor=actor)

    return sourced(state, start_job(
        state, project=UNIT, env="", fn=run, actor=actor, tool=TOOL_DELETE,
        component=name, tag="-", opening=opening))


def _execute_delete(state: AppState, *, name: str, refs: list[str], actor: str) -> Mutation:
    runtime = runtime_of(state)
    since = runtime.clock()
    source = read_source(state)
    steps = [Step(name="confirm", status="ok")]
    evidence: dict[str, Any] = {"monitor": name, "objects": refs}
    if budget_spent(state, UNIT, ""):
        steps.append(Step(name="commit", status="failed", detail=BUDGET_SPENT))
        return _mutation(status="failed", steps=steps, evidence=evidence, source=source)
    try:
        result = runtime.runner.run(
            [runtime.monitor_script, "delete", name],
            {"ORBIT_ACTION": ACTION_DELETE, "ORBIT_ACTOR": actor})
    except Exception as e:
        steps.append(Step(name="commit", status="failed",
                          detail=scrub(f"{e.__class__.__name__}: {e}")))
        return _mutation(status="failed", steps=steps, evidence=evidence, source=source)
    output = scrub((result.stdout or "") + "\n" + (result.stderr or ""))
    parsed = parse_commit(result.stdout or "")
    if result.returncode != 0:
        steps.append(Step(name="commit", status="failed", detail=bump_fail_detail(output)))
        return _mutation(status="failed", steps=steps, commit=parsed,
                         evidence=evidence, source=source)
    if not parsed:
        steps.append(Step(name="commit", status="failed", detail="no COMMIT line"))
        return _mutation(status="failed", steps=steps, evidence=evidence, source=source)
    steps.append(Step(name="commit", status="ok", detail=parsed))
    mode = apply_mode(state)
    if mode != MODE_ON:
        # the file is gone from the repo but the objects stay in the cluster, and
        # nothing else prunes: refuse rather than report applied
        steps.append(Step(name="prune", status="skipped",
                          detail=f"ORBITD_APPLY={mode}: the commit landed, nothing was pruned; "
                                 f"set ORBITD_APPLY=on and delete again, or prune by hand"))
        evidence["verdict"] = "LANDED NOT PRUNED"
        return _mutation(status="refused", steps=steps, commit=parsed,
                         evidence=evidence, source=source)
    lag = _wait_checkout(state, parsed, deadline=_apply_deadline(runtime, since))
    if lag is not None:
        steps.append(lag.model_copy(update={"name": "prune"}))
        return _mutation(status="failed", steps=steps, commit=parsed,
                         evidence=evidence, source=source)
    return _prune_step(state, name, steps=steps, commit=parsed, evidence=evidence, source=source)


def _prune_step(state: AppState, name: str, *, steps: list[Step], commit: str | None,
                evidence: dict[str, Any], source: Any) -> Mutation:
    """Delete the objects monitor `name` rendered that the repository no longer declares.
    The monitors that remain are applied first: a log monitor has no object of its own to
    delete, its rule leaves the shared ConfigMap when that is rendered without it."""
    try:
        plan = monitors_apply.plan_monitors(state)
        applied = monitors_apply.apply_monitors(state, plan, dry_run=False)
        if not applied.ok:
            bad = next((r for r in applied.results if r.result == "failed"), None)
            where = (f"{bad.ref.kind} {bad.ref.namespace}/{bad.ref.name}: {bad.reason}"
                     if bad is not None else "apply failed")
            steps.append(Step(name="prune", status="failed",
                              detail=f"the monitors that remain could not be applied ({where})"))
            return _mutation(status="failed", steps=steps, commit=commit, evidence=evidence, source=source)
        narrowed = replace(
            plan, prune=monitors_apply.prune_refs_for_monitor(state, plan, name))
        pruned = monitors_apply.prune_monitors(state, narrowed, dry_run=False)
    except Exception as e:
        steps.append(Step(name="prune", status="failed",
                          detail=scrub(f"{e.__class__.__name__}: {e}"[:1000])))
        return _mutation(status="failed", steps=steps, commit=commit,
                         evidence=evidence, source=source)
    if not pruned.ok:
        bad = next((r for r in pruned.results if r.result == "failed"), None)
        where = (f"{bad.ref.kind} {bad.ref.namespace}/{bad.ref.name}: {bad.reason}"
                 if bad is not None else "prune failed")
        steps.append(Step(name="prune", status="failed", detail=where))
        return _mutation(status="failed", steps=steps, commit=commit,
                         evidence=evidence, source=source)
    counts: dict[str, int] = {}
    for r in pruned.results:
        counts[r.result] = counts.get(r.result, 0) + 1
    steps.append(Step(
        name="prune", status="ok",
        detail=f"{counts.get('deleted', 0)} deleted, {counts.get('absent', 0)} absent"))
    return _mutation(status="applied", steps=steps, commit=commit,
                     evidence=evidence, source=source)


def _left_behind(state: AppState, name: str) -> list[str]:
    """`kind/namespace/name` of the objects monitor `name` rendered that are still in the
    cluster although its file is gone from the repository: a delete whose prune never
    ran (it failed, or orbitd restarted under it). The reconcile loop never prunes, so
    such a rule would go on evaluating and alerting for a monitor nobody can list
    (docs/V1.md §8: two of them on the test box, one two days old)."""
    try:
        plan = monitors_apply.plan_monitors(state)
        refs = monitors_apply.prune_refs_for_monitor(state, plan, name)
    except Exception:
        return []
    return [f"{ref.kind}/{ref.namespace}/{ref.name}" for ref in refs]


def _delete_left_behind(state: AppState, name: str, left: list[str], dry_run: bool,
                        ctx: Context | None, actor: str) -> Any:
    if dry_run:
        audit(actor=actor, tool=TOOL_DELETE, project=UNIT, env="", component=name, tag="-",
              verdict="dry_run")
        return sourced(state, Mutation(
            status="dry_run",
            steps=[Step(name="plan", status="ok",
                        detail=f"monitor {name!r} is gone from the repository; {len(left)} object(s) "
                               f"it rendered are still in the cluster")],
            evidence={"monitor": name, "objects": left, "left_behind": True}))
    decision = confirmation(
        ctx, f"Monitor {name!r} is not defined any more, but {len(left)} object(s) it rendered are "
             f"still in the cluster (a delete that did not finish). Delete them?\n"
             + "\n".join(f"  - {line}" for line in left))
    if isinstance(decision, InputRequiredResult):
        return decision
    if not decision:
        audit(actor=actor, tool=TOOL_DELETE, project=UNIT, env="", component=name, tag="-",
              verdict="refused")
        return refused(f"delete of what monitor {name!r} left behind was not confirmed")
    opening = [Step(name="confirm", status="ok")]

    def run() -> Mutation:
        steps = [Step(name="confirm", status="ok"),
                 Step(name="commit", status="skipped", detail="the file is already gone from the repository")]
        evidence: dict[str, Any] = {"monitor": name, "objects": left, "left_behind": True}
        source = read_source(state)
        mode = apply_mode(state)
        if mode != MODE_ON:
            steps.append(Step(name="prune", status="skipped",
                              detail=f"ORBITD_APPLY={mode}: nothing was pruned; set ORBITD_APPLY=on and delete again"))
            return _mutation(status="refused", steps=steps, evidence=evidence, source=source)
        return _prune_step(state, name, steps=steps, commit=None, evidence=evidence, source=source)

    return sourced(state, start_job(
        state, project=UNIT, env="", fn=run, actor=actor, tool=TOOL_DELETE,
        component=name, tag="-", opening=opening))


# ------------------------------------------------------------------ silence

def _silence_seconds(duration: Any) -> int | None:
    """Seconds for `<n>[mhd]`, or None when the shape or the 1m..7d range refuses it."""
    if not isinstance(duration, str) or not DURATION_RE.fullmatch(duration):
        return None
    total = int(duration[:-1]) * DURATION_S[duration[-1]]
    if total < MIN_SILENCE_S or total > MAX_SILENCE_S:
        return None
    return total


def _rfc3339(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace(
        "+00:00", "Z")


def _silence(state: AppState, name: str, duration: str, reason: str) -> Any:
    actor = actor_of()
    if not NAME_RE.fullmatch(name):
        audit(actor=actor, tool=TOOL_SILENCE, project=UNIT, env="", component="-", tag="-",
              verdict="failed")
        return failure("not_found", "a monitor name is a DNS label", retryable=False)

    def denied(code: str, what: str, **evidence: Any) -> Any:
        audit(actor=actor, tool=TOOL_SILENCE, project=UNIT, env="", component=name, tag="-",
              verdict="failed")
        return failure(code, what, retryable=False, name=name, **evidence)

    try:
        bodies = state.backends.instance.monitors()
    except (BackendError, BackendUnconfigured) as e:
        audit(actor=actor, tool=TOOL_SILENCE, project=UNIT, env="", component=name, tag="-",
              verdict="failed")
        return e.as_failure()
    if not any(isinstance(b, dict) and b.get("name") == name for b in bodies):
        return denied(
            "not_found", f"monitor {name!r} is not defined",
            next_action="call orbit_monitor_list to see defined monitors")
    seconds = _silence_seconds(duration)
    if seconds is None:
        return denied(
            "invalid_duration",
            f"monitor `duration` {duration!r} must match ^[1-9][0-9]*[mhd]$ and fall "
            f"between 1 minute and 7 days",
            next_action="pass a duration like 30m, 2h or 7d")
    if not isinstance(reason, str) or not reason.strip():
        return denied(
            "invalid_reason", "monitor `reason` must be a non-empty string",
            next_action="say why this silence exists")
    try:
        now = float(runtime_of(state).clock())
    except Exception:
        now = datetime.now(timezone.utc).timestamp()
    start = datetime.fromtimestamp(now, tz=timezone.utc)
    payload = {
        "matchers": [{"name": "alertname", "value": name,
                      "isRegex": False, "isEqual": True},
                     {"name": "orbit_monitor", "value": name,
                      "isRegex": False, "isEqual": True}],
        "startsAt": _rfc3339(start),
        "endsAt": _rfc3339(start + timedelta(seconds=seconds)),
        "createdBy": actor,
        "comment": reason,
    }
    try:
        state.backends.alertmanager.require()
        response = state.backends.alertmanager.post(SILENCES_PATH, json=payload)
    except (BackendError, BackendUnconfigured) as e:
        audit(actor=actor, tool=TOOL_SILENCE, project=UNIT, env="", component=name, tag="-",
              verdict="failed")
        return e.as_failure()
    silence_id = _silence_id(response)
    if not silence_id:
        return denied("silence_failed", "Alertmanager did not return a silence id",
                      next_action="retry the silence")
    audit(actor=actor, tool=TOOL_SILENCE, project=UNIT, env="", component=name, tag="-",
          verdict="applied")
    return sourced(state, Mutation(
        status="applied",
        steps=[Step(name="silence", status="ok",
                    detail=f"silenced {name!r} for {duration}")],
        evidence={"monitor": name, "duration": duration, "silence_id": silence_id}))


def _unsilence(state: AppState, name: str) -> Any:
    actor = actor_of()

    def said(verdict: str) -> None:
        audit(actor=actor, tool=TOOL_UNSILENCE, project=UNIT, env="",
              component=name if NAME_RE.fullmatch(name) else "-", tag="-", verdict=verdict)

    if not NAME_RE.fullmatch(name):
        said("failed")
        return failure("not_found", "a monitor name is a DNS label", retryable=False)
    try:
        bodies = state.backends.instance.monitors()
    except (BackendError, BackendUnconfigured) as e:
        said("failed")
        return e.as_failure()
    if not any(isinstance(b, dict) and b.get("name") == name for b in bodies):
        said("failed")
        return failure("not_found", f"monitor {name!r} is not defined", retryable=False, name=name,
                       next_action="call orbit_monitor_list to see defined monitors")
    from ..tools_viewer.monitor_status import active_silences
    try:
        state.backends.alertmanager.require()
    except BackendUnconfigured as e:
        said("failed")
        return e.as_failure()
    silenced = active_silences(state)
    if silenced is None:
        said("failed")
        return failure("backend_error", "Alertmanager did not list its silences", retryable=True, name=name,
                       next_action="retry in a moment")
    lifted: list[str] = []
    for row in silenced.get(name, []):
        silence_id = row.get("id") or ""
        if not re.fullmatch(r"[0-9a-fA-F-]{8,64}", silence_id):
            continue                      # an id goes into a URL path: its shape first
        try:
            state.backends.alertmanager.delete(f"/api/v2/silence/{silence_id}")
        except (BackendError, BackendUnconfigured) as e:
            said("failed")
            return failure("backend_error", f"Alertmanager did not expire silence {silence_id}: "
                                            f"{getattr(e, 'what_failed', '') or 'request failed'}",
                           retryable=True, name=name, lifted=lifted, next_action="retry: the rest are still in force")
        lifted.append(silence_id)
    said("applied")
    return sourced(state, Mutation(
        status="applied",
        steps=[Step(name="unsilence", status="ok",
                    detail=(f"lifted {len(lifted)} silence(s) of {name!r}" if lifted
                            else f"monitor {name!r} was not silenced"))],
        evidence={"monitor": name, "lifted": lifted}))


def _silence_id(response: Any) -> str:
    if isinstance(response, str) and response.strip():
        return response.strip()
    if isinstance(response, dict):
        for key in ("silenceID", "silenceId", "silence_id", "id"):
            value = response.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
    return ""


__all__ = ["register"]
