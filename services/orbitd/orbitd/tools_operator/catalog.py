"""orbit_catalog_set — operator: replace one project's catalog entry.

A catalog change used to be an edit in a laptop checkout and a pull request, outside
orbitd: no role, no confirmation, no audit line, and no way for an agent without a shell.
Here it is one commit by orbitd's GitHub App, like a ship's tag bump: the entry is
spliced into catalog/projects.yaml (every other line of the file stays as it was), the
generator regenerates clusters/ and is the judge of the entry, the commit gate's own
chain of checks runs on the catalog with the entry spliced in BEFORE anything is pushed
(precheck_entry: what the gate would refuse never lands), and the gate runs that chain
again on the landed commit before the reconcile loop applies it."""

from __future__ import annotations

import json
import os
import tempfile
from typing import Annotated, Any

from mcp.server.mcpserver import Context
from mcp_types import InputRequiredResult
from pydantic import Field

from ..backends import BackendError, BackendUnconfigured
from ..deploy import (
    BUDGET_SPENT, actor_of, audit, budget_spent, parse_commit, runtime_of, scrub, start_job,
)
from ..deploy import _mutation, _wait_checkout  # noqa: PLC2701 - the deploy job's own helpers
from ..roles import Mutation, Registry, Role, Step, failure
from ..source import read_source, sourced
from ..state import AppState
from ..tools import confirmation, refused
from ..tools_viewer.common import find_project, not_found
from ..tools_viewer.util import Ident

TOOL = "orbit_catalog_set"
ACTION = "catalog-set"
CHECKOUT_WAIT_S = 180.0
ENTRY_MAX_BYTES = 64 * 1024
CHANGED_MAX = 40


def changed(old: Any, new: Any, at: str = "") -> list[str]:
    """The paths at which two entries differ, for a confirmation a person can read.
    A list of the same length is compared item by item, an item named by its `name`."""
    if isinstance(old, dict) and isinstance(new, dict):
        out: list[str] = []
        for key in list(old) + [k for k in new if k not in old]:
            where = f"{at}.{key}" if at else str(key)
            if key not in new:
                out.append(f"{where} (removed)")
            elif key not in old:
                out.append(f"{where} (added)")
            else:
                out += changed(old[key], new[key], where)
        return out
    if isinstance(old, list) and isinstance(new, list) and len(old) == len(new):
        out = []
        for i, (a, b) in enumerate(zip(old, new)):
            same_name = isinstance(a, dict) and isinstance(b, dict) and a.get("name") and a.get("name") == b.get("name")
            out += changed(a, b, f"{at}[{a['name'] if same_name else i}]")
        return out
    return [] if old == new else [at or "(the entry)"]


def _run_script(state: AppState, verb: str, project: str, *, actor: str, role: str,
                entry: dict[str, Any] | None) -> tuple[Any | None, str]:
    """Run the catalog commit script once: (its result, "") or (None, why it did not
    run). The entry travels in a file the script reads, removed afterwards."""
    runtime = runtime_of(state)
    env = {"ORBIT_ACTION": f"catalog-{verb}", "ORBIT_ACTOR": actor, "ORBIT_ROLE": role}
    tmppath = None
    try:
        if entry is not None:
            fd, tmppath = tempfile.mkstemp(prefix="orbit-catalog-")
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(entry, fh)
            env["CATALOG_ENTRY_FILE"] = tmppath
        try:
            return runtime.runner.run([runtime.catalog_script, verb, project], env), ""
        except Exception as e:
            return None, scrub(f"{e.__class__.__name__}: {e}")
    finally:
        if tmppath:
            try:
                os.unlink(tmppath)
            except OSError:
                pass


def _said(result: Any) -> str:
    """The script's own last sentence (never a `STEP` line), scrubbed and bounded."""
    output = scrub((result.stdout or "") + "\n" + (result.stderr or ""))
    lines = [line for line in output.splitlines() if line.strip() and not line.startswith("STEP ")]
    return (lines[-1] if lines else "the catalog commit script failed")[:600]


PRECHECK_SCRIPT_S = 180
NOT_CHECKED = "no commit gate in this build (a development build), so the entry was not checked"


def precheck_entry(state: AppState, project: str, entry: dict[str, Any]) -> tuple[bool, str]:
    """Would the commit gate accept the catalog with this entry? (accepted, sentence).

    The answer the gate would give AFTER the commit landed, asked before anything is
    written: the tree orbitd reads now (the last accepted commit) is copied, the entry
    spliced in with this build's catalog editor, the manifests regenerated with this
    build's generator, and the gate's own chain of checks run on the result, in this
    process, as the gate runs it. Refused: (False, the editor's, the generator's or the
    failing check's sentence). Accepted: (True, ""). With no gate (a development build has
    no version and no copy of core) nothing can be checked: (True, NOT_CHECKED).

    Before this, a change the generator accepted and the gate refused (more memory than
    the node has, two components on one path, a secret nobody declared) landed on main
    and was refused there, and a refused main blocks every deploy of every project
    (QA of v1.0.0, F26). It runs in this process and not as a command because orbitd's
    memory limit has no room for a second interpreter beside it."""
    import shutil
    import subprocess
    import sys
    from pathlib import Path

    from .. import checks
    from ..gate import gate_of
    gate = gate_of(state)
    if gate.disabled_reason() is not None:
        return True, NOT_CHECKED
    root = state.backends.instance.require()
    gate.scratch.mkdir(parents=True, exist_ok=True)
    scratch = Path(tempfile.mkdtemp(prefix="orbit-precheck-", dir=str(gate.scratch)))
    try:
        tree = checks.copy_with_core(Path(root), scratch / "tree", gate.core)
        (tree / checks.LOCK_REL).exists()         # the tree is a checkout, or the chain says so
        env = checks.script_env(scratch)

        def run(argv: list[str], stdin: str | None = None) -> tuple[int, str]:
            try:
                proc = subprocess.run(argv, cwd=tree, env=env, input=stdin, capture_output=True, text=True,
                                      timeout=PRECHECK_SCRIPT_S)
            except subprocess.TimeoutExpired:
                return 124, f"it did not finish in {PRECHECK_SCRIPT_S}s"
            lines = [ln for ln in (proc.stdout + "\n" + proc.stderr).splitlines() if ln.strip()]
            return proc.returncode, (lines[-1] if lines else "")

        rc, said = run([sys.executable, "scripts/catalog-edit.py", "set", project], json.dumps(entry))
        if rc != 0:
            return False, checks.one_sentence(scrub(said)) or "the catalog editor refused the entry"
        rc, said = run([sys.executable, "scripts/catalog-sync.py"])
        if rc != 0:
            return False, checks.one_sentence(scrub(said)) or "the generator refused the catalog"
        verdict = checks.run_chain(checks.Context(root=tree, core=gate.core, version=gate.version,
                                                  facts=gate._facts()))
        if verdict.ok:
            return True, ""
        bad = verdict.failed
        assert bad is not None
        return False, f"{bad.name}: {scrub(bad.sentence or '')}"
    except Exception as e:      # a check that cannot run is not a pass
        return False, scrub(f"the entry could not be checked ({e.__class__.__name__}: {e})")[:600]
    finally:
        shutil.rmtree(scratch, ignore_errors=True)


def check_catalog(state: AppState, project: str, entry: dict[str, Any]) -> Step:
    """The `check` step of a dry run: the editor, the generator and the commit gate's
    chain on the catalog with the entry spliced in. Nothing is committed or pushed."""
    accepted, said = precheck_entry(state, project, entry)
    if not accepted:
        return Step(name="check", status="failed", detail=said)
    if said:
        return Step(name="check", status="skipped", detail=said)
    return Step(name="check", status="ok",
                detail="the generator and the commit gate's checks accept this entry; nothing was written")


def commit_catalog(state: AppState, verb: str, project: str, *, actor: str, role: str,
                   entry: dict[str, Any] | None, steps: list[Step]) -> str | None:
    """Run the catalog commit script and wait for the checkout to carry its commit and
    for the gate to accept it. Appends the `commit` and `checkout` steps; returns the
    commit, or None when a step failed (the failed step is the last one). A `set` is
    checked first (precheck_entry): what the gate would refuse is not pushed."""
    runtime = runtime_of(state)
    if verb == "set" and entry is not None:
        accepted, said = precheck_entry(state, project, entry)
        if not accepted:
            steps.append(Step(name="commit", status="failed", detail=said))
            return None
    result, why = _run_script(state, verb, project, actor=actor, role=role, entry=entry)
    if result is None:
        steps.append(Step(name="commit", status="failed", detail=why))
        return None
    commit = parse_commit(result.stdout or "")
    if result.returncode != 0 or not commit:
        steps.append(Step(name="commit", status="failed", detail=_said(result)))
        return None
    steps.append(Step(name="commit", status="ok", detail=commit))
    lag = _wait_checkout(state, commit, deadline=runtime.clock() + CHECKOUT_WAIT_S)
    if lag is not None:
        # checkout lag, or the commit gate refused the commit: its sentence is the detail
        steps.append(Step(name="checkout", status="failed", detail=lag.detail))
        return None
    steps.append(Step(name="checkout", status="ok"))
    return commit


def register(registry: Registry, state: AppState) -> None:

    @registry.tool(role=Role.operator, read_only=False, destructive=True)
    def orbit_catalog_set(
        project: Annotated[Ident, Field(description="Project id from orbit_project_list.")],
        entry: Annotated[dict[str, Any], Field(
            description="The project's complete new catalog entry: what orbit_catalog_get(project) returns in "
                        "`data`, with your changes made. Its `id` must be the project.")],
        dry_run: Annotated[bool, Field(
            description="Report which fields would change and whether the generator and the commit gate's "
                        "checks accept the entry; do not confirm or write.")] = False,
        ctx: Context = None,  # type: ignore[assignment]
    ) -> Mutation | InputRequiredResult:
        """Replace one project's catalog entry: replicas, memory, a per-environment
        override, hostnames, a component added or removed. Read the entry with
        orbit_catalog_get, change it, and pass the whole entry back; orbit_catalog_schema
        says what an entry may hold. Fails `not_found` for a project that is not in the
        catalog (adding a project is onboarding, removing one is orbit_project_remove).
        `refused` before any write when the entry's `id` is not the project. An entry
        equal to the current one is `applied` with `evidence.unchanged`. `dry_run`
        returns `evidence.changed`, the paths that differ, and checks the entry with no
        confirmation and no write: its one step `check` runs this build's catalog
        editor, its generator and the commit gate's checks on the catalog with the
        entry spliced in (up to a minute), `dry_run` when they accept it, `failed` with
        the refusing sentence in the step when they do not.
        Otherwise confirms (the confirmation lists those paths), then starts a job under
        the project's lock with steps `confirm, commit, checkout`: `commit` runs the
        catalog commit script (`catalog-commit.sh set <project>`, `ORBIT_ACTION=catalog-set`,
        `ORBIT_ACTOR=<actor>`), which splices the entry into catalog/projects.yaml leaving
        every other line of the file as it was, regenerates the manifests and lands one
        commit with the trailers `Orbit-Action`, `Orbit-Actor`, `Orbit-Role` and
        `Orbit-Project`. Before the script runs, the entry is checked as the dry run
        checks it: an entry the generator or one of the gate's checks refuses (an
        unknown field, more memory than the node has, a secret nobody declared) fails
        this step with the refusing sentence and nothing is pushed. `checkout` waits for this orbitd to
        check that commit out and for its commit gate to accept it; a commit the gate
        refuses fails this step with the failing check's sentence, and nothing of it
        is applied. When the gate has refused main's newest commit (the entry orbitd
        reads is then the last accepted one), passing that last accepted entry back is
        not `unchanged`: it is committed over the refused one, which is how a refused
        catalog change is undone (`evidence.restores` names the refused commit).
        What a change REMOVES (a component, a custom domain) is not deleted by the
        loop: it is listed in orbit_deploy_status `reconcile.would_prune` until an
        admin runs orbit_deploy_reconcile.
        `applied` means the commit is accepted: orbitd's reconcile loop then
        applies it to each of the project's environments, which takes a few minutes.
        Wait for that with orbit_deploy_wait(project, env, commit) per environment. The
        job id is in `evidence.job_id`; reclaim it with orbit_job_wait. Comments written
        inside the old entry are not kept. Operator or admin.
        Example: orbit_catalog_set(project="demo", entry={...}, dry_run=true)."""
        return _set(state, project, entry, dry_run, ctx)


def _refused_head(state: AppState) -> tuple[str, str] | None:
    """(commit, failing check) when the commit gate has refused the checkout's newest
    commit; None when it accepted it, cannot decide yet, or is off."""
    from ..gate import gate_of
    try:
        verdict = gate_of(state).evaluate()
    except Exception:
        return None
    if verdict.enabled and verdict.refused and verdict.head:
        return verdict.head, str(verdict.check or "a check")
    return None


def _fail(actor: str, project: str, code: str, what: str, **kw: Any) -> Any:
    audit(actor=actor, tool=TOOL, project=project, env="", component="-", tag="-", verdict="failed")
    return failure(code, what, project=project, **kw)


def _set(state: AppState, project: str, entry: dict[str, Any], dry_run: bool, ctx: Context | None) -> Any:
    actor = actor_of()
    try:
        current = find_project(state, project)
    except (BackendError, BackendUnconfigured) as e:
        return e.as_failure()
    if current is None:
        return not_found(f"project {project!r} is not in the catalog", project=project)
    if not isinstance(entry, dict) or entry.get("id") != project:
        return _fail(actor, project, "refused",
                     f"the entry's `id` is not {project!r}: pass the project's own complete entry (an id cannot change)",
                     next_action="read the entry with orbit_catalog_get, change it, and pass all of it back")
    try:
        size = len(json.dumps(entry))
    except (TypeError, ValueError):
        return _fail(actor, project, "refused", "the entry holds a value that is not plain data")
    if size > ENTRY_MAX_BYTES:
        return _fail(actor, project, "refused", f"the entry is larger than {ENTRY_MAX_BYTES} bytes")
    paths = changed(current, entry)
    evidence: dict[str, Any] = {"project": project, "changed": paths[:CHANGED_MAX]}
    if len(paths) > CHANGED_MAX:
        evidence["changed_more"] = len(paths) - CHANGED_MAX
    # `current` is the entry of the last commit the gate ACCEPTED. When the gate has
    # refused main's newest commit, an entry equal to `current` is not what main holds:
    # answering `unchanged` then wrote nothing, main stayed refused and every deploy of
    # every project kept failing, while the skill said to "set the entry back with this
    # same tool" (QA of v1.0.0, F26). The script compares with main itself.
    refused_head = _refused_head(state)
    if not paths and refused_head is None:
        audit(actor=actor, tool=TOOL, project=project, env="", component="-", tag="-", verdict="applied")
        evidence["unchanged"] = True
        return sourced(state, Mutation(status="applied", steps=[], evidence=evidence))
    if refused_head is not None:
        evidence["restores"] = refused_head[0][:12]
    if dry_run:
        step = check_catalog(state, project, entry)
        said = "failed" if step.status == "failed" else "dry_run"
        audit(actor=actor, tool=TOOL, project=project, env="", component="-", tag="-", verdict=said)
        return sourced(state, _mutation(status=said, steps=[step], evidence=evidence, source=read_source(state)))
    shown = ", ".join(paths[:CHANGED_MAX]) + (f" and {len(paths) - CHANGED_MAX} more" if len(paths) > CHANGED_MAX else "")
    if refused_head is not None:
        over = (f" The commit gate refused main's newest commit {refused_head[0][:12]} ({refused_head[1]}), so "
                f"the entry compared with is the last accepted one; this entry is committed over the refused one.")
        shown = (shown or "none against the last accepted entry") + "." + over
    verdict = confirmation(ctx, (
        f"Change project {project}'s catalog entry? Changes: {shown}. The generator and the commit gate's "
        f"checks judge the entry before "
        f"anything is pushed; once the commit is accepted orbitd applies it to every environment of the "
        f"project, production included.")) if ctx is not None else True
    if isinstance(verdict, InputRequiredResult):
        return verdict
    if not verdict:
        audit(actor=actor, tool=TOOL, project=project, env="", component="-", tag="-", verdict="refused")
        return refused(f"the change to {project}'s catalog entry was not confirmed")
    opening = [Step(name="confirm", status="ok")]

    def run() -> Mutation:
        steps = list(opening)
        if budget_spent(state, project, ""):
            steps.append(Step(name="commit", status="failed", detail=BUDGET_SPENT))
            return _mutation(status="failed", steps=steps, commit=None, evidence=evidence, source=read_source(state))
        commit = commit_catalog(state, "set", project, actor=actor, role="operator", entry=entry, steps=steps)
        return _mutation(status="applied" if commit else "failed", steps=steps, commit=commit,
                         evidence=dict(evidence), source=read_source(state))

    runtime = runtime_of(state)
    return sourced(state, start_job(
        state, project=project, env="", fn=run, actor=actor, tool=TOOL, component="-", tag="catalog",
        opening=opening, budget_s=float(runtime.runner_timeout_s) + CHECKOUT_WAIT_S + 60.0,
        timeout_step=("checkout", "TIMED OUT")))
