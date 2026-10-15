"""The ship / rollback / restart job: catalog, registry, bump, rollout, assertions.

Production talks to ship.sh and the registry through injectable runners; tests
swap in fakes. One running job per project/env. Tokens never on argv.
"""

from __future__ import annotations

import logging
import os
import re
import signal
import subprocess
import sys
import threading
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

import yaml
from mcp.server.auth.middleware.auth_context import get_access_token

from .assertions import assert_deployed
from .audit import guarded_thread, guarded_timer, log_audit
from .backends import BackendError, BackendUnconfigured, InstanceRepo
from .jobs import Jobs
from .roles import CheckoutSource, Mutation, Step, failure
from .source import read_source, sourced
from .state import AppState
from .tools_viewer._util import kube_call
from .tools_viewer.common import (
    CRASH_LOOP_RESTARTS, IMAGE_WAITING, as_dict, deployment_name, desired_tag, find_project, image_matches,
    image_tag, jobs_of, list_pods, ns, pick, pod_is_active, pod_trouble, rfc3339_now, selector,
)

log = logging.getLogger("orbitd.audit")

SHIP_SCRIPT = "/app/scripts/ship.sh"
MONITOR_SCRIPT = "/app/scripts/monitor-commit.sh"
FEATURES_SCRIPT = "/app/scripts/features-commit.sh"
CATALOG_SCRIPT = "/app/scripts/catalog-commit.sh"
DIGEST_SCRIPT = "/app/scripts/github-app-token.py"
RESTART_ANN = "orbit.dev/restarted-at"
ROLLOUT_TIMEOUT_S = 600.0
RUNNER_TIMEOUT_S = 600.0
ASSERT_TIMEOUT_S = 60.0
KUBE_REQUEST_TIMEOUT_S = 30.0
POLL_S = 2.0
# While a rollout is waited on, the new pods are looked at this often. A pod the kubelet
# is backing off from does not become ready by waiting: a crash loop ends the wait at its
# third crash, an image that cannot be pulled (or a config that cannot be built) once it
# has stayed so this long.
POD_CHECK_S = 6.0
CLOCK_SLACK_S = 5.0
CRASH_RESTARTS = CRASH_LOOP_RESTARTS
STUCK_GRACE_S = 45.0
HISTORY_PAGE = 50
CONFIG_SECRET_FILES = (
    "github-app-id",
    "github-app-installation-id",
    "github-app-key.pem",
)
COMMIT_RE = re.compile(r"^COMMIT\s+([0-9a-fA-F]{7,64})\s*$", re.M)
# The word after bearer/basic is a credential unless it is an auth-param name from a
# WWW-Authenticate challenge (RFC 7235/6750) or the word "token" itself: those lines
# hold no credential and must reach the reader intact.
_AUTH_PARAM = r"(?!(?:realm|error|error_description|error_uri|scope|charset|token)\b)"
BEARER_BASIC = re.compile(
    r"(?i)(?:bearer\s+" + _AUTH_PARAM + r"[A-Za-z0-9\-._~+/]+=*"
    r"|basic\s+" + _AUTH_PARAM + r"[A-Za-z0-9+/]+=*)"
)
GITHUB_TOKEN = re.compile(r"\b(?:ghs|ghp)_[A-Za-z0-9_]+\b")
JWT_SHAPE = re.compile(r"\beyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\b")
X_ACCESS = re.compile(r"(?i)x-access-token:[^\s\"']+")
# The shapes a value must have before it may reach argv (ship.sh, the digest script):
# a tag as the tools already require of an operator, an image reference without tag or
# digest. A value from the tags ledger or the catalog is checked against them at the
# argv site, never trusted for having come from git.
TAG_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$"
TAG_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")
IMAGE_RE = re.compile(
    r"[a-z0-9]+(?:(?:[._]|__|-+)[a-z0-9]+)*(?::[0-9]{1,5})?(?:/[a-z0-9]+(?:(?:[._]|__|-+)[a-z0-9]+)*)*"
)
# A credential-file literal shorter than this, or blank, is not scrubbed: it is an id,
# not a secret, and replacing it would rewrite every newline or digit in a diagnostic.
SCRUB_LITERAL_MIN = 8


BUDGET_SPENT = "job budget spent; nothing more written"


def budget_spent(state: "AppState", project: str, env: str) -> bool:
    """True once the job holding this pair's env lock has been given its verdict by the
    budget timer: every write site (bump, patch, each object of an apply or prune, a
    monitor commit) asks before writing, so nothing follows the verdict. A dryRun=All
    preview writes nothing and holds no lock, so the appliers do not ask for one."""
    return runtime_of(state).locks.expired(project, env)


def tag_ok(value: Any) -> bool:
    return isinstance(value, str) and TAG_RE.fullmatch(value) is not None


def image_ok(value: Any) -> bool:
    return isinstance(value, str) and len(value) <= 255 and IMAGE_RE.fullmatch(value) is not None


class DigestError(Exception):
    """The registry did not report a digest for this image:tag."""


class RunnerTimeout(Exception):
    """A ship or digest subprocess exceeded its deadline."""


@dataclass
class RunResult:
    returncode: int
    stdout: str = ""
    stderr: str = ""


def _kill_process_group(proc: subprocess.Popen[str]) -> None:
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError, OSError):
        pass
    try:
        proc.kill()
    except (ProcessLookupError, PermissionError, OSError):
        pass


def _timed_run(argv: list[str], env: dict[str, str], timeout_s: float) -> RunResult:
    try:
        proc = subprocess.Popen(
            argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, env=env, start_new_session=True,
        )
    except FileNotFoundError as e:
        return RunResult(returncode=127, stdout="", stderr=str(e))
    try:
        stdout, stderr = proc.communicate(timeout=timeout_s)
    except subprocess.TimeoutExpired as e:
        _kill_process_group(proc)
        try:
            proc.communicate(timeout=1)
        except (subprocess.TimeoutExpired, OSError):
            pass
        raise RunnerTimeout(
            f"timeout: {argv[0] if argv else 'process'} exceeded {int(timeout_s)}s"
        ) from e
    return RunResult(proc.returncode or 0, stdout or "", stderr or "")


class SubprocessRunner:
    """Process env plus extra_env; secrets stay off argv. Kills the process group on timeout."""

    def __init__(self, timeout_s: float = RUNNER_TIMEOUT_S) -> None:
        self.timeout_s = timeout_s

    def run(self, argv: list[str], extra_env: dict[str, str]) -> RunResult:
        env = os.environ.copy()
        env.update(extra_env)
        timeout = self.timeout_s if self.timeout_s is not None else RUNNER_TIMEOUT_S
        return _timed_run(argv, env, float(timeout))


class ScriptDigest:
    def __init__(self, timeout_s: float = RUNNER_TIMEOUT_S) -> None:
        self.timeout_s = timeout_s

    def lookup(self, image: str, tag: str) -> str:
        env = os.environ.copy()
        try:
            result = _timed_run(
                [sys.executable, DIGEST_SCRIPT, "--digest", image, tag],
                env, float(self.timeout_s),
            )
        except FileNotFoundError as e:
            raise DigestError(str(e)) from e
        except RunnerTimeout as e:
            raise DigestError(str(e)) from e
        if result.returncode == 127 and result.stderr:
            raise DigestError(result.stderr)
        if result.returncode != 0:
            raise DigestError((result.stderr or result.stdout or "registry lookup failed").strip())
        digest = (result.stdout or "").strip()
        if not digest:
            raise DigestError("registry returned an empty digest")
        return digest


class GitLedger:
    def __init__(self, instance: InstanceRepo) -> None:
        self.instance = instance

    def current_tag(self, project: str, env: str, component: str) -> str | None:
        doc = self.instance.tags() or {}
        tags = doc.get("tags", doc) if isinstance(doc, dict) else {}
        if not isinstance(tags, dict):
            return None
        value = tags.get(f"{project}/{env}/{component}")
        if not value:
            return None
        if not tag_ok(str(value)):
            log.warning("tags ledger: the current value for %s/%s/%s is not a tag shape; treated as absent",
                        project, env, component)
            return None
        return str(value)

    def previous_tag(self, project: str, env: str, component: str) -> str | None:
        key = f"{project}/{env}/{component}"
        current = None
        skip = 0
        while True:
            page = self.instance.tags_history(limit=HISTORY_PAGE, skip=skip)
            if not page:
                return None
            for entry in page:
                sha = entry.get("commit") or ""
                text = self.instance.show(f"{sha}:catalog/tags.yaml", missing_ok=True)
                if not text:
                    continue
                data = yaml.safe_load(text) or {}
                tags = data.get("tags") if isinstance(data, dict) else None
                if not isinstance(tags, dict):
                    continue
                value = tags.get(key)
                if not value:
                    continue
                value = str(value)
                if not tag_ok(value):
                    raise ValueError(f"the tags ledger carries a value for {key} at {sha[:12]} "
                                     "that is not a tag shape; pass an explicit tag")
                if current is None:
                    current = value
                    continue
                if value != current:
                    return value
            if len(page) < HISTORY_PAGE:
                return None
            skip += len(page)


class EnvLocks:
    def __init__(self) -> None:
        self._mu = threading.Lock()
        self._held: dict[str, str] = {}
        self._expired: set[str] = set()
        self._cancelled: dict[str, str] = {}

    @staticmethod
    def _key(project: str, env: str) -> str:
        return f"{project}/{env}"

    def expire(self, project: str, env: str, job_id: str) -> None:
        """The holder's budget is spent: the lock stays held until the holder exits, and
        the holder is told to write nothing more (see `expired`)."""
        key = self._key(project, env)
        with self._mu:
            if self._held.get(key) == job_id:
                self._expired.add(key)

    def expired(self, project: str, env: str) -> bool:
        with self._mu:
            return self._key(project, env) in self._expired

    def cancel(self, project: str, env: str, job_id: str, by: str) -> bool:
        """Tell the holder to stop: as `expire`, and remembered as a cancellation by `by`.
        False when `job_id` does not hold the pair (it ended, or never held it)."""
        key = self._key(project, env)
        with self._mu:
            if self._held.get(key) != job_id:
                return False
            self._expired.add(key)
            self._cancelled[key] = by
            return True

    def cancelled_by(self, project: str, env: str) -> str | None:
        with self._mu:
            return self._cancelled.get(self._key(project, env))

    def try_acquire(self, project: str, env: str, job_id: str) -> str | None:
        """None if acquired; the holding job id if busy."""
        key = self._key(project, env)
        with self._mu:
            current = self._held.get(key)
            if current is not None:
                return current
            self._held[key] = job_id
            return None

    def release(self, project: str, env: str, job_id: str) -> None:
        key = self._key(project, env)
        with self._mu:
            if self._held.get(key) == job_id:
                del self._held[key]
                self._expired.discard(key)
                self._cancelled.pop(key, None)


def stop_reason(runtime: "DeployRuntime", project: str, env: str) -> str:
    """Why the holder of this pair's lock was told to stop: cancelled, or out of budget."""
    by = runtime.locks.cancelled_by(project, env)
    return f"cancelled by {by}" if by else "job budget spent"


def time_now() -> float:
    return time.time()


def time_sleep(seconds: float) -> None:
    time.sleep(seconds)


@dataclass
class DeployRuntime:
    runner: Any = field(default_factory=SubprocessRunner)
    digest: Any = field(default_factory=ScriptDigest)
    ledger: Any = None
    locks: EnvLocks = field(default_factory=EnvLocks)
    clock: Callable[[], float] = time_now
    sleeper: Callable[[float], None] = time_sleep
    http_get: Callable[[str], int] | None = None
    rollout_timeout_s: float = ROLLOUT_TIMEOUT_S
    runner_timeout_s: float = RUNNER_TIMEOUT_S
    assert_timeout_s: float = ASSERT_TIMEOUT_S
    kube_timeout_s: float = KUBE_REQUEST_TIMEOUT_S
    ship_script: str = SHIP_SCRIPT
    monitor_script: str = MONITOR_SCRIPT
    features_script: str = FEATURES_SCRIPT
    catalog_script: str = CATALOG_SCRIPT


def kube_request_timeout(runtime: DeployRuntime, deadline: float | None = None) -> tuple[float, float]:
    """Per-call kube timeout as the (connect, read) pair the client honours: cap at
    kube_timeout_s, never past the remaining deadline. (A bare float is silently no
    timeout in the kubernetes client, which is how a held connection once outlived
    every deadline here.)"""
    cap = runtime.kube_timeout_s
    try:
        cap_f = float(cap if cap is not None else KUBE_REQUEST_TIMEOUT_S)
    except (TypeError, ValueError):
        cap_f = KUBE_REQUEST_TIMEOUT_S
    if cap_f <= 0:
        cap_f = KUBE_REQUEST_TIMEOUT_S
    if deadline is None:
        return (cap_f, cap_f)
    left = float(deadline) - runtime.clock()
    each = max(1e-3, min(cap_f, left))
    return (each, each)


def _job_budget_s(runtime: DeployRuntime) -> float:
    runner = runtime.runner_timeout_s
    rollout = runtime.rollout_timeout_s
    try:
        runner_f = float(runner if runner is not None else RUNNER_TIMEOUT_S)
    except (TypeError, ValueError):
        runner_f = RUNNER_TIMEOUT_S
    try:
        rollout_f = float(rollout if rollout is not None else ROLLOUT_TIMEOUT_S)
    except (TypeError, ValueError):
        rollout_f = ROLLOUT_TIMEOUT_S
    return max(1e-3, runner_f + rollout_f)


def _phase_deadline(runtime: DeployRuntime) -> float:
    """Remaining budget for rollout wait plus assertions."""
    try:
        rollout_f = float(runtime.rollout_timeout_s)
    except (TypeError, ValueError):
        rollout_f = ROLLOUT_TIMEOUT_S
    try:
        assert_f = float(runtime.assert_timeout_s)
    except (TypeError, ValueError):
        assert_f = ASSERT_TIMEOUT_S
    return runtime.clock() + rollout_f + assert_f


_RUNTIME_LOCK = threading.Lock()


def runtime_of(state: AppState) -> DeployRuntime:
    # One runtime per process: the env locks live on it, so two callers that each
    # built their own would each hold a different lock for the same pair. The
    # server builds it once before any runner starts; the lock covers every other
    # first caller.
    with _RUNTIME_LOCK:
        existing = getattr(state, "deploy", None)
        if existing is None:
            existing = DeployRuntime()
            if existing.ledger is None:
                existing.ledger = GitLedger(state.backends.instance)
            state.deploy = existing
        elif existing.ledger is None:
            existing.ledger = GitLedger(state.backends.instance)
        return existing


def _read_secret_file(path: str) -> list[str]:
    try:
        with open(path, errors="replace") as fh:  # a non-UTF-8 file must not fail every scrub
            text = fh.read()
    except OSError:
        return []
    if not text:
        return []
    out = [text]
    stripped = text.strip()
    if stripped and stripped != text:
        out.append(stripped)
    # a multi-line secret (a PEM key) also travels one line at a time in a log line:
    # every line of it is a literal of its own
    # (PEM armor lines are not secrets: a log line quoting a header stays readable)
    lines = [ln.strip() for ln in text.splitlines()]
    if len(lines) > 1:
        out.extend(ln for ln in lines if ln and not ln.startswith("-----"))
    return out


_LITERAL_CACHE: dict[tuple[tuple[str, tuple[int, int, int] | None], ...], list[str]] = {}
_LITERAL_LOCK = threading.Lock()


def _secret_paths() -> list[str]:
    paths: list[str] = []
    token_file = os.environ.get("ORBIT_REGISTRY_TOKEN_FILE") or ""
    if token_file:
        paths.append(token_file)
    config_dir = os.environ.get("ORBIT_CONFIG_DIR") or ""
    if config_dir:
        paths.extend(os.path.join(config_dir, name) for name in CONFIG_SECRET_FILES)
    return paths


def _stat_key(path: str) -> tuple[int, int, int] | None:
    try:
        st = os.stat(path)
    except OSError:
        return None
    return (st.st_ino, st.st_size, st.st_mtime_ns)


def _secret_literals() -> list[str]:
    """The daemon's held secret values, read once per (path, inode, size, mtime): a scrub
    of a thousand log lines stats the credential files, it does not re-read them."""
    paths = _secret_paths()
    key = tuple((p, _stat_key(p)) for p in paths)
    with _LITERAL_LOCK:
        hit = _LITERAL_CACHE.get(key)
        if hit is not None:
            return hit
    values: list[str] = []
    for path in paths:
        values.extend(_read_secret_file(path))
    seen: set[str] = set()
    unique: list[str] = []
    for value in values:
        if value.strip() and len(value) >= SCRUB_LITERAL_MIN and value not in seen:
            seen.add(value)
            unique.append(value)
    unique.sort(key=len, reverse=True)
    with _LITERAL_LOCK:
        _LITERAL_CACHE.clear()
        _LITERAL_CACHE[key] = unique
    return unique


def scrub(text: str) -> str:
    """Strip bearer/basic, GitHub tokens, JWTs, x-access-token, and held secret files."""
    if not text:
        return text
    for secret in _secret_literals():
        text = text.replace(secret, "<token>")
    text = X_ACCESS.sub("x-access-token:<token>", text)
    text = BEARER_BASIC.sub("<token>", text)
    text = GITHUB_TOKEN.sub("<token>", text)
    text = JWT_SHAPE.sub("<token>", text)
    return text


def scrub_value(value: Any) -> Any:
    if isinstance(value, str):
        return scrub(value)
    if isinstance(value, dict):
        return {k: scrub_value(v) for k, v in value.items()}
    if isinstance(value, list):
        return [scrub_value(v) for v in value]
    if isinstance(value, tuple):
        return tuple(scrub_value(v) for v in value)
    return value


def _mutation(*, status: str, steps: list[Step], commit: str | None = None,
              evidence: dict[str, Any] | None = None,
              source: CheckoutSource | None = None) -> Mutation:
    cleaned: list[Step] = []
    for step in steps:
        detail = None if step.detail is None else scrub(step.detail)
        cleaned.append(Step(name=step.name, status=step.status, detail=detail))
    return Mutation(
        status=status,  # type: ignore[arg-type]
        steps=cleaned,
        commit=commit,
        evidence=scrub_value(evidence or {}),
        source=source,
    )


def _component_slots(components: list[dict[str, Any]]) -> list[dict[str, Any]]:
    slots: list[dict[str, Any]] = []
    for item in components:
        name = str(item.get("name") or "")
        if name:
            slots.append({"name": name, "stage": "not_reached"})
    return slots


def _slot(evidence: dict[str, Any], name: str) -> dict[str, Any]:
    slots = evidence.get("components")
    if not isinstance(slots, list):
        slots = []
        evidence["components"] = slots
    for item in slots:
        if isinstance(item, dict) and item.get("name") == name:
            return item
    item: dict[str, Any] = {"name": name, "stage": "not_reached"}
    slots.append(item)
    return item


def _set_stage(evidence: dict[str, Any], name: str, stage: str, **extra: Any) -> dict[str, Any]:
    slot = _slot(evidence, name)
    slot["stage"] = stage
    for key, value in extra.items():
        if value is not None:
            slot[key] = value
    return slot


def parse_commit(text: str) -> str | None:
    found = None
    for match in COMMIT_RE.finditer(text or ""):
        found = match.group(1)
    return found


def bump_fail_detail(output: str) -> str:
    """Prefer ship.sh's `bump FAIL rc=` line so a push failure and a missing
    COMMIT line read differently. Fall back to the scrubbed output."""
    text = (output or "").strip()
    for line in reversed(text.splitlines()):
        if line.startswith("bump FAIL rc="):
            return line
    return text or "ship.sh failed"


def actor_of() -> str:
    token = get_access_token()
    if token is None:
        return "-"
    return token.subject or "-"


def audit(*, actor: str, tool: str, project: str, env: str,
          component: str, tag: str, verdict: str) -> None:
    # Values are scrubbed of secrets but otherwise raw: the sink renders them.
    # The decision travels as a field the caller cannot occupy; what was done
    # stays in its own fields where it may say anything.
    actor, tool, project, env, component, tag, verdict = (
        scrub(actor), scrub(tool), scrub(project), scrub(env),
        scrub(component), scrub(tag), scrub(verdict))
    log_audit(log, "deploy %s %s/%s %s %s: %s", tool, project, env, component, tag,
              verdict, actor=actor, tool=tool, project=project, env=env,
              component=component, tag=tag, verdict=verdict)


def _as_int(value: Any) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def guard_namespace(project: str, env: str, namespace: str) -> Mutation | None:
    expected = ns(project, env)
    if namespace != expected:
        return Mutation(
            status="failed",
            steps=[Step(name="catalog", status="failed",
                        detail=f"namespace {namespace!r} is not {expected!r} derived from the catalog")],
        )
    return None


@dataclass
class CatalogHit:
    project_id: str
    env: str
    namespace: str
    components: list[dict[str, Any]]


def validate_catalog(state: AppState, project: str, env: str,
                     component_names: list[str]) -> CatalogHit | Mutation:
    entry = find_project(state, project)
    if entry is None:
        return Mutation(status="failed", steps=[
            Step(name="catalog", status="failed", detail=f"unknown project {project!r}")])
    environments = entry.get("environments") or {}
    if not isinstance(environments, dict) or env not in environments:
        return Mutation(status="failed", steps=[
            Step(name="catalog", status="failed", detail=f"unknown environment {env!r}")])
    envcfg = environments.get(env) or {}
    if not isinstance(envcfg, dict):
        envcfg = {}
    if env != "prod" and not envcfg.get("enabled"):
        return Mutation(status="failed", steps=[
            Step(name="catalog", status="failed", detail=f"environment {env!r} is not enabled")])
    namespace = ns(project, env)
    guarded = guard_namespace(project, env, namespace)
    if guarded is not None:
        return guarded
    catalog_ns = envcfg.get("namespace")
    if catalog_ns and str(catalog_ns) != namespace:
        return Mutation(status="failed", steps=[
            Step(name="catalog", status="failed",
                 detail=f"namespace {catalog_ns!r} is not {namespace!r} derived from the catalog")])
    by_name: dict[str, dict[str, Any]] = {}
    for item in entry.get("components") or []:
        if isinstance(item, dict) and item.get("name"):
            by_name[str(item["name"])] = item
        elif isinstance(item, str):
            by_name[item] = {"name": item}
    resolved: list[dict[str, Any]] = []
    for name in component_names:
        comp = by_name.get(name)
        if comp is None:
            return Mutation(status="failed", steps=[
                Step(name="catalog", status="failed", detail=f"unknown component {name!r}")])
        comp_envs = comp.get("environments")
        if comp_envs is not None and env not in list(comp_envs):
            return Mutation(status="failed", steps=[
                Step(name="catalog", status="failed",
                     detail=f"component {name!r} is not in environment {env!r}")])
        resolved.append(comp)
    return CatalogHit(project_id=project, env=env, namespace=namespace, components=resolved)


def plan_steps(*, kind: str, project: str, env: str, components: list[str],
               tag: str | None, expect: str | None = None) -> list[Step]:
    steps = [
        Step(name="confirm", status="skipped", detail="dry_run"),
        Step(name="catalog", status="ok", detail=f"{project}/{env} ns={ns(project, env)}"),
    ]
    if kind == "restart":
        steps.append(Step(name="restart", status="skipped",
                          detail=f"would patch {RESTART_ANN}"))
        for name in ("rollout", "image", "rollout", "ready", "log"):
            steps.append(Step(name=name, status="skipped"))
        return steps
    detail = f"tag={tag}"
    if expect:
        detail += f" expect={expect}"
    steps.append(Step(name="registry", status="skipped", detail=detail))
    for component in components:
        steps.append(Step(name="bump", status="skipped", detail=component))
        steps.append(Step(name="rollout", status="skipped", detail=component))
        steps.append(Step(name="image", status="skipped"))
        steps.append(Step(name="rollout", status="skipped"))
        steps.append(Step(name="ready", status="skipped"))
        steps.append(Step(name="log", status="skipped"))
    return steps


def busy_failure(project: str, env: str, job_id: str):
    # The reconcile loop (and orbit_reconcile) take the same pair lock for a few seconds
    # while they apply; their holder is not a job, so orbit_job_wait cannot find it and
    # "wait with orbit_job_wait" sent an agent after an id that does not exist (docs/V1.md
    # §8, scenario S04). Say what holds the pair and how long to wait instead.
    if job_id == "reconcile-loop" or job_id.startswith("reconcile-"):
        return failure(
            "busy",
            f"the reconcile loop is applying {project}/{env} right now",
            next_action="retry in about 15 seconds; this is not a job orbit_job_wait can wait on",
            retryable=True,
            holder="reconcile",
        )
    return failure(
        "busy",
        f"a job is already running for {project}/{env}",
        next_action="wait with orbit_job_wait then retry; a ship, rollback or restart that is stuck waiting "
                    "for its rollout can be stopped with orbit_job_cancel",
        retryable=True,
        job_id=job_id,
    )


def start_job(state: AppState, *, project: str, env: str, fn: Callable[[], Mutation],
              actor: str, tool: str, component: str, tag: str,
              opening: list[Step], budget_s: float | None = None,
              timeout_step: tuple[str, str] | None = None) -> Mutation:
    """`budget_s` and `timeout_step` are for a job that is not a ship: its own budget, and
    the (step name, verdict) its timeout is reported as. A ship keeps the defaults."""
    jobs = jobs_of(state)
    runtime = runtime_of(state)
    job_id = uuid.uuid4().hex
    held = runtime.locks.try_acquire(project, env, job_id)
    if held is not None:
        # A refusal leaves no record: the table is for jobs that ran.
        audit(actor=actor, tool=tool, project=project, env=env,
              component=component, tag=tag, verdict="busy")
        return busy_failure(project, env, held)  # type: ignore[return-value]
    job = jobs.create(status="running", job_id=job_id)
    job.meta = {"project": project, "env": env, "tool": tool, "component": component, "tag": tag, "actor": actor}
    thread = guarded_thread(
        target=_run_job,
        kwargs={"state": state, "job_id": job.id, "project": project, "env": env,
                "fn": fn, "actor": actor, "tool": tool, "component": component, "tag": tag,
                "budget_s": budget_s, "timeout_step": timeout_step},
        daemon=True, name=f"orbit-job-{job.id[:8]}",
    )
    thread.start()
    audit(actor=actor, tool=tool, project=project, env=env,
          component=component, tag=tag, verdict="running")
    return Mutation(status="running", steps=opening, evidence={"job_id": job.id})


def failed_at(mutation: Mutation) -> tuple[str, str]:
    """(step, detail) of the step a job that did not get its work done stopped at: the last
    step that is neither ok nor skipped, else the last step. Scrubbed and cut: it goes to
    the log, where the audit line carries only the word `failed` (on the rebuilt test box
    a ship back failed four seconds in and nothing anywhere said at which step, once the
    in-memory job record was gone; docs/V1.md §8)."""
    steps = list(mutation.steps or [])
    step = next((s for s in reversed(steps) if s.status not in ("ok", "skipped")), steps[-1] if steps else None)
    if step is None:
        return "-", ""
    return scrub(str(step.name)), scrub(str(step.detail or ""))[:300]


def _run_job(*, state: AppState, job_id: str, project: str, env: str,
             fn: Callable[[], Mutation], actor: str, tool: str,
             component: str, tag: str, budget_s: float | None = None,
             timeout_step: tuple[str, str] | None = None) -> None:
    jobs: Jobs = jobs_of(state)
    runtime = runtime_of(state)
    settled = threading.Event()
    settle_mu = threading.Lock()

    def settle(mutation: Mutation) -> None:
        with settle_mu:
            if settled.is_set():
                return
            settled.set()
            # a job whose work was a dry run (orbit_db_restore's comparison) completed
            # when it produced its answer; `failed` is for work that did not get done
            terminal = "completed" if mutation.status in ("applied", "dry_run") else "failed"
            by = runtime.locks.cancelled_by(project, env)
            if by and terminal == "failed":
                # orbit_job_cancel stopped it: the record says so, and what had landed
                terminal = "cancelled"
                mutation = _mutation(
                    status="failed", commit=mutation.commit, source=mutation.source,
                    steps=list(mutation.steps) + [Step(
                        name="cancel", status="ok",
                        detail=f"cancelled by {by}: the job stopped waiting and wrote nothing more. What it "
                               f"had already written stays (a tag bump that landed is in the repository "
                               f"and applied): roll back, or ship another tag")],
                    evidence={**dict(mutation.evidence or {}), "cancelled_by": by})
            jobs.complete(job_id, status=terminal, result=mutation.model_dump())
            audit(actor=actor, tool=tool, project=project, env=env,
                  component=component, tag=tag, verdict=mutation.status)
            if terminal == "failed":
                step, detail = failed_at(mutation)
                logging.getLogger("orbitd.jobs").warning(
                    "job %s (%s %s/%s) ended %s at step %s: %s", job_id, scrub(tool), scrub(project), scrub(env),
                    mutation.status, step, detail)

    def budget_spent() -> None:
        # The verdict is given now; the env lock is not. It stays with this job until
        # the worker thread exits, and the worker writes nothing more once it sees the
        # expiry (execute_ship / execute_restart check before every bump, apply and
        # patch), so no second job can race a ship.sh that is still running.
        runtime.locks.expire(project, env, job_id)
        step_name, verdict = timeout_step or ("rollout", "LANDED NOT DEPLOYED rollout")
        settle(_mutation(
            status="failed",
            steps=[Step(name=step_name, status="failed", detail="timeout")],
            evidence={"verdict": verdict},
            source=read_source(state),
        ))

    timer = guarded_timer(
        interval=_job_budget_s(runtime) if budget_s is None else max(1e-3, float(budget_s)),
        function=budget_spent,
        name=f"orbit-job-{job_id[:8]}-budget",
        daemon=True,
    )
    timer.start()
    try:
        try:
            mutation = fn()
        except RunnerTimeout as e:
            mutation = _mutation(
                status="failed",
                steps=[Step(name="bump", status="failed", detail=str(e))],
                source=read_source(state),
            )
        except Exception as e:
            mutation = _mutation(
                status="failed",
                steps=[Step(name="job", status="failed", detail=str(e))],
                source=read_source(state),
            )
        if not isinstance(mutation, Mutation):
            mutation = _mutation(
                status="failed",
                steps=[Step(name="job", status="failed", detail="non-mutation result")],
                source=read_source(state),
            )
        else:
            mutation = _mutation(
                status=mutation.status, steps=list(mutation.steps),
                commit=mutation.commit, evidence=dict(mutation.evidence or {}),
                source=mutation.source or read_source(state),
            )
        settle(mutation)
    finally:
        timer.cancel()
        if not settled.is_set():
            settle(_mutation(
                status="failed",
                steps=[Step(name="job", status="failed", detail="unterminated")],
                source=read_source(state),
            ))
        runtime.locks.release(project, env, job_id)


def _seq(value: Any) -> list[Any]:
    if not value:
        return []
    return list(value) if not isinstance(value, list) else value


def _template_images(dep: Any) -> list[str]:
    data = as_dict(dep)
    spec = as_dict(pick(data, "spec") or {})
    template = as_dict(pick(spec, "template") or {})
    tspec = as_dict(pick(template, "spec") or {})
    images: list[str] = []
    for raw in _seq(pick(tspec, "containers")):
        image = str(pick(as_dict(raw), "image") or "")
        if image:
            images.append(image)
    return images


def _template_carries(images: list[str], tag: str, repository: str | None) -> bool:
    want = f"{repository}:{tag}" if repository else None
    for img in images:
        if want and image_matches(img, want, image_id=img):
            return True
        if not want and image_tag(img) == tag:
            return True
    return False


def _counters_equal(status: dict[str, Any]) -> tuple[bool, dict[str, int | None]]:
    updated = _as_int(pick(status, "updatedReplicas", "updated_replicas"))
    ready = _as_int(pick(status, "readyReplicas", "ready_replicas"))
    replicas = _as_int(pick(status, "replicas"))
    available = _as_int(pick(status, "availableReplicas", "available_replicas"))
    counters = {
        "updatedReplicas": updated, "readyReplicas": ready,
        "replicas": replicas, "availableReplicas": available,
    }
    ok = (
        updated is not None and ready is not None
        and replicas is not None and available is not None
        and updated == ready == replicas == available
    )
    return ok, counters


def snapshot_deployment(state: AppState, project: str, env: str,
                        component: str, *, deadline: float | None = None,
                        ) -> tuple[int | None, list[str]]:
    runtime = runtime_of(state)
    namespace = ns(project, env)
    name = deployment_name(project, component)
    try:
        dep = kube_call(
            state.backends.kube.apps.read_namespaced_deployment, name, namespace,
            _request_timeout=kube_request_timeout(runtime, deadline),
        )
    except (BackendError, BackendUnconfigured, Exception):
        return None, []
    data = as_dict(dep)
    meta = as_dict(pick(data, "metadata") or {})
    return _as_int(pick(meta, "generation")), _template_images(dep)


def _created_epoch(pod: Any) -> float | None:
    stamp = pick(as_dict(pick(as_dict(pod), "metadata") or {}), "creationTimestamp", "creation_timestamp")
    if stamp is None:
        return None
    if hasattr(stamp, "timestamp"):
        return float(stamp.timestamp())
    try:
        from datetime import datetime
        return datetime.fromisoformat(str(stamp).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def new_pod_troubles(state: AppState, project: str, env: str, component: str,
                     *, tag: str, repository: str | None = None,
                     since: float | None = None, timeout: Any = None) -> list[dict[str, Any]]:
    """pod_trouble() of every active pod of the component whose spec carries `tag` and,
    with `since`, that was created at or after it: the pods THIS rollout created.
    Unreadable is no trouble known: [].

    `since` matters when the tag does not change: a restart, or the same tag shipped
    again. The pods being replaced carry the tag too, and one of them crash-looping is
    often why the restart was asked for; it is not the rollout failing."""
    kwargs = {"_request_timeout": timeout} if timeout is not None else {}
    try:
        listed = kube_call(state.backends.kube.core.list_namespaced_pod, ns(project, env),
                           label_selector=selector(project, component), **kwargs)
    except Exception:
        return []
    out: list[dict[str, Any]] = []
    for pod in list(getattr(listed, "items", None) or []):
        if not pod_is_active(pod):
            continue
        spec = as_dict(pick(as_dict(pod), "spec") or {})
        images = [str(pick(as_dict(c), "image") or "") for c in _seq(pick(spec, "containers"))]
        if not _template_carries([i for i in images if i], tag, repository):
            continue
        if since is not None:
            born = _created_epoch(pod)
            if born is not None and born < since - CLOCK_SLACK_S:
                continue
        trouble = pod_trouble(pod)
        if trouble is not None:
            out.append(trouble)
    return out


ROLLOUT_STUCK_NEXT = ("the version that was running keeps serving; read the new pod's log "
                      "(orbit_logs_query, orbit_workload_describe), then ship a fixed tag or "
                      "orbit_deploy_rollback")


def wait_rollout(state: AppState, project: str, env: str, component: str,
                 *, tag: str, repository: str | None = None,
                 before_generation: int | None = None,
                 deadline: float | None = None, since: float | None = None) -> Step:
    """`since` is when the job that caused the rollout began: only pods created from
    then on are looked at for trouble (see new_pod_troubles)."""
    runtime = runtime_of(state)
    namespace = ns(project, env)
    guarded = guard_namespace(project, env, namespace)
    if guarded is not None:
        return Step(name="rollout", status="failed", detail=guarded.steps[0].detail)
    name = deployment_name(project, component)
    kube = state.backends.kube
    loop_deadline = runtime.clock() + runtime.rollout_timeout_s
    kube_deadline = deadline if deadline is not None else loop_deadline
    if deadline is not None:
        loop_deadline = min(loop_deadline, deadline)
    last = "rollout not observed"
    troubles: list[dict[str, Any]] = []
    first_seen: dict[tuple[str, str], float] = {}
    next_pod_check = runtime.clock() + POD_CHECK_S

    def timed_out() -> Step:
        # what the new pods were doing when the wait ran out, in the kubelet's words:
        # "timeout" alone sent a reader to the cluster to find a crash loop that had
        # been there for ten minutes (QA of v1.0.0, F16)
        why = "; ".join(t["sentence"] for t in troubles[:3])
        return Step(name="rollout", status="failed",
                    detail=f"timeout: {why + '. ' + ROLLOUT_STUCK_NEXT + '. ' if why else ''}{last}")

    while True:
        now = runtime.clock()
        if now >= loop_deadline:
            return timed_out()
        if runtime.locks.expired(project, env):
            return Step(name="rollout", status="failed", detail=f"{stop_reason(runtime, project, env)}: {last}")
        try:
            dep = kube_call(
                kube.apps.read_namespaced_deployment, name, namespace,
                _request_timeout=kube_request_timeout(runtime, kube_deadline),
            )
        except BackendError as e:
            return Step(name="rollout", status="failed", detail=str(e.what_failed))
        data = as_dict(dep)
        meta = as_dict(pick(data, "metadata") or {})
        status = as_dict(pick(data, "status") or {})
        generation = _as_int(pick(meta, "generation"))
        observed = _as_int(pick(status, "observedGeneration", "observed_generation"))
        images = _template_images(dep)
        carries = _template_carries(images, tag, repository)
        counters_ok, counters = _counters_equal(status)
        gen_ok = generation is not None and observed == generation
        stale = (
            before_generation is not None
            and generation == before_generation
            and not carries
        )
        last = (
            f"observedGeneration={observed} generation={generation} "
            f"before={before_generation} template={images[:3]!s} "
            f"counters={counters}"
        )
        if carries and gen_ok and counters_ok and not stale:
            return Step(name="rollout", status="ok", detail=last)
        now = runtime.clock()
        if carries and now >= next_pod_check:
            next_pod_check = now + POD_CHECK_S
            troubles = new_pod_troubles(state, project, env, component, tag=tag, repository=repository,
                                        since=since, timeout=kube_request_timeout(runtime, kube_deadline))
            seen = {(t["pod"], t["reason"]) for t in troubles if t["stuck"]}
            first_seen = {key: first_seen.get(key, now) for key in seen}
            for t in troubles:
                if not t["stuck"]:
                    continue
                crashed = t["reason"] == "CrashLoopBackOff" and t["restarts"] >= CRASH_RESTARTS
                stayed = now - first_seen[(t["pod"], t["reason"])] >= STUCK_GRACE_S
                if crashed or (stayed and t["reason"] != "CrashLoopBackOff"):
                    what = ("the new version crashes at start" if t["reason"] == "CrashLoopBackOff"
                            else "the new version's image cannot be pulled" if t["reason"] in IMAGE_WAITING
                            else "the new version's pod cannot start")
                    return Step(name="rollout", status="failed",
                                detail=f"{what}: {t['sentence']}. Not waited out: this does not mend by "
                                       f"waiting. {ROLLOUT_STUCK_NEXT}. {last}")
        if now >= loop_deadline:
            return timed_out()
        runtime.sleeper(min(POLL_S, max(0.0, loop_deadline - now)))


CRON_ASSERTIONS = ("image", "rollout", "ready", "log")


def wait_cron_rollout(state: AppState, project: str, env: str, component: str,
                      *, tag: str, repository: str | None = None,
                      deadline: float | None = None) -> Step:
    """A `kind: cron` component has no Deployment to roll: the ship lands when the
    CronJob's job template carries the tag. Its pods exist only from the next scheduled
    run, so the pod assertions cannot be made at ship time (the caller reports them
    skipped, saying so). Read through the dynamic client: batch/v1 CronJob."""
    runtime = runtime_of(state)
    namespace = ns(project, env)
    guarded = guard_namespace(project, env, namespace)
    if guarded is not None:
        return Step(name="rollout", status="failed", detail=guarded.steps[0].detail)
    name = deployment_name(project, component)
    loop_deadline = runtime.clock() + runtime.rollout_timeout_s
    kube_deadline = deadline if deadline is not None else loop_deadline
    if deadline is not None:
        loop_deadline = min(loop_deadline, deadline)
    last = "CronJob not observed"
    while True:
        now = runtime.clock()
        if now >= loop_deadline:
            return Step(name="rollout", status="failed", detail=f"timeout: {last}")
        if runtime.locks.expired(project, env):
            return Step(name="rollout", status="failed", detail=f"{stop_reason(runtime, project, env)}: {last}")
        try:
            resource = state.backends.kube.dynamic.resources.get(api_version="batch/v1", kind="CronJob")
            cron = as_dict(resource.get(
                name=name, namespace=namespace,
                _request_timeout=kube_request_timeout(runtime, kube_deadline),
            ))
        except BackendError as e:
            return Step(name="rollout", status="failed", detail=str(e.what_failed))
        except Exception as e:
            if getattr(e, "status", None) == 404 or e.__class__.__name__ in {"NotFoundError", "ResourceNotFoundError"}:
                last = f"CronJob {name} not found in {namespace}"
            else:
                return Step(name="rollout", status="failed", detail="kubernetes request failed")
        else:
            spec = as_dict(pick(cron, "spec") or {})
            job = as_dict(pick(spec, "jobTemplate", "job_template") or {})
            images = _template_images(job)
            schedule = str(pick(spec, "schedule") or "")
            last = f"CronJob {name} schedule={schedule!r} template={images[:3]!s}"
            if _template_carries(images, tag, repository):
                return Step(name="rollout", status="ok", detail=last)
        now = runtime.clock()
        if now >= loop_deadline:
            return Step(name="rollout", status="failed", detail=f"timeout: {last}")
        runtime.sleeper(min(POLL_S, max(0.0, loop_deadline - now)))


def _assert_steps(state: AppState, project: str, env: str, component: str,
                  tag: str, since: float, steps: list[Step],
                  evidence: dict[str, Any], *, deadline: float | None = None,
                  ) -> Mutation | None:
    runtime = runtime_of(state)
    report = assert_deployed(
        state, project, env, component, tag, since,
        timeout_s=runtime.assert_timeout_s,
        clock=runtime.clock, sleeper=runtime.sleeper, http_get=runtime.http_get,
        deadline=deadline,
    )
    stage = "asserted" if report.passed else "failed"
    slot = _set_stage(evidence, component, stage)
    for key, value in report.evidence.items():
        if key != "components":
            slot[key] = value
    landed = evidence.get("components")
    evidence.update(report.evidence)
    if landed is not None:
        evidence["components"] = landed
    evidence["verdict"] = report.verdict
    steps.extend(report.steps)
    if not report.passed:
        return Mutation(status="failed", steps=steps, evidence=evidence)
    return None


def _skip_later(evidence: dict[str, Any], components: list[dict[str, Any]],
                start: int) -> None:
    skipped: list[str] = []
    for later in components[start:]:
        name = str(later.get("name") or "")
        if not name:
            continue
        skipped.append(name)
        _slot(evidence, name)
    if skipped:
        evidence["skipped"] = skipped


def _bind_runner_timeout(runtime: DeployRuntime) -> None:
    timeout = runtime.runner_timeout_s
    runner = runtime.runner
    if hasattr(runner, "timeout_s"):
        runner.timeout_s = timeout
    digest = runtime.digest
    if hasattr(digest, "timeout_s"):
        digest.timeout_s = timeout


def _apply_deadline(runtime: DeployRuntime, since: float) -> float:
    """How long the checkout wait may take: the job budget less the rollout and
    assertion phase, so a lag failure lands before the job timer's."""
    try:
        rollout_f = float(runtime.rollout_timeout_s)
    except (TypeError, ValueError):
        rollout_f = ROLLOUT_TIMEOUT_S
    try:
        assert_f = float(runtime.assert_timeout_s)
    except (TypeError, ValueError):
        assert_f = ASSERT_TIMEOUT_S
    return since + _job_budget_s(runtime) - rollout_f - assert_f


def _commit_reached(head: str, commit: str) -> bool:
    head = (head or "").strip().lower()
    commit = (commit or "").strip().lower()
    if not head or not commit:
        return False
    return head == commit or head.startswith(commit) or commit.startswith(head)


def checkout_carries(state: AppState, commit: str, head: str) -> bool:
    """True when the checkout's `head` is `commit` or a descendant of it.

    A job used to wait for the checkout's HEAD to BE its commit. The checkout follows the
    repository about once a minute, so when a second commit landed inside that minute the
    checkout went straight past the first: its job waited out its whole deadline (nine
    minutes, holding its lock) and failed with "checkout lag", although the checkout had
    carried its commit all along. Measured on the test box (docs/V1.md §8): a monitor
    delete followed 15 s later by a ship; the delete never pruned its rule. Two agents
    working at once is the ordinary case, so the question is ancestry, asked of the
    checkout's own history."""
    if _commit_reached(head, commit):
        return True
    if not (head or "").strip() or not (commit or "").strip():
        return False
    try:
        checkout = state.backends.instance.checkout()
        proc = subprocess.run(["git", "-C", str(checkout), "merge-base", "--is-ancestor", commit.strip(), head.strip()],
                              capture_output=True, timeout=20)
    except Exception:
        return False
    return proc.returncode == 0


def _checkout_head(state: AppState) -> str:
    """The newest commit the checkout holds: what a job waits to see its own commit in.
    (With a tree pinned by the gate, read_source names the pinned commit instead.)"""
    if getattr(state.backends.instance, "pinned", None) is not None:
        from .source import read_head
        return read_head(state).commit
    return read_source(state).commit


def _gate_step(state: AppState) -> Step | None:
    """None when the checkout's HEAD may be applied; the failed `apply` step when the
    commit gate refused it (or cannot decide yet), carrying the failing check's sentence."""
    from .gate import gate_of
    verdict = gate_of(state).evaluate()
    if not verdict.blocks:
        return None
    if verdict.refused:
        return Step(name="apply", status="failed",
                    detail=f"the commit gate refused {verdict.head[:12]} ({verdict.check}): {verdict.sentence} "
                           f"Nothing was applied; the last good commit keeps running.")
    return Step(name="apply", status="failed",
                detail=f"the commit gate could not decide about {verdict.head[:12]} yet: {verdict.sentence}")


def _wait_checkout(state: AppState, commit: str, *, deadline: float) -> Step | None:
    """None once the git-sync checkout carries `commit` (its HEAD is the commit or comes
    after it); the failed `apply` step on lag."""
    runtime = runtime_of(state)
    while True:
        head = _checkout_head(state)
        if checkout_carries(state, commit, head):
            return _gate_step(state)
        now = runtime.clock()
        if now >= deadline:
            return Step(name="apply", status="failed",
                        detail=f"checkout lag: HEAD {head or '(none)'} is not {commit}")
        runtime.sleeper(min(POLL_S, max(0.0, deadline - now)))


def apply_step(state: AppState, project: str, env: str, *, commit: str | None,
               deadline: float) -> Step:
    """The `apply` step of ship, rollback and restart. With ORBITD_APPLY=on: wait for
    the checkout to carry `commit` (when given), then plan and apply the pair through
    the applier. Any other mode records the step skipped and applies nothing; the
    caller refuses the job, naming the switch or the pair list in the detail."""
    from . import applier

    mode = applier.apply_mode(state)
    if mode in (applier.MODE_ON, applier.MODE_DRY_RUN) and not applier.pair_enabled(state, project, env):
        return Step(name="apply", status="skipped", detail=applier.PAIR_NOT_ENABLED_DETAIL)
    if mode != applier.MODE_ON:
        return Step(name="apply", status="skipped",
                    detail=f"ORBITD_APPLY={mode}: apply is disabled on this orbitd; "
                           f"set ORBITD_APPLY=on")
    if commit:
        lag = _wait_checkout(state, commit, deadline=deadline)
        if lag is not None:
            return lag
    if budget_spent(state, project, env):
        return Step(name="apply", status="failed", detail=BUDGET_SPENT)
    try:
        plan = applier.plan(state, project, env)
        result = applier.apply(state, plan, dry_run=False)
    except Exception as e:
        return Step(name="apply", status="failed",
                    detail=f"{e.__class__.__name__}: {scrub(str(e) or '')}"[:1000])
    counts: dict[str, int] = {}
    for r in result.results:
        counts[r.result] = counts.get(r.result, 0) + 1
    if not result.ok:
        bad = next((r for r in result.results if r.result == "failed"), None)
        where = f"{bad.ref.kind} {bad.ref.namespace}/{bad.ref.name}: {bad.reason}" if bad else "apply failed"
        return Step(name="apply", status="failed", detail=where)
    return Step(name="apply", status="ok",
                detail=f"{counts.get('applied', 0)} applied, {counts.get('unchanged', 0)} unchanged, "
                       f"render_sha={plan.render_sha}")


def execute_ship(state: AppState, *, project: str, env: str,
                 components: list[dict[str, Any]], tag: str, action: str,
                 actor: str, expect: str | None, opening: list[Step]) -> Mutation:
    runtime = runtime_of(state)
    _bind_runner_timeout(runtime)
    since = runtime.clock()
    source = read_source(state)
    steps = list(opening)
    evidence: dict[str, Any] = {"components": _component_slots(components)}
    digest = None
    if not tag_ok(tag):
        # The tag reaches ship.sh's argv below; a value from the tags ledger (a
        # rollback without `to`) is checked here the way the tools check an operator's.
        steps.append(Step(name="catalog", status="failed",
                          detail="tag is not a tag shape; pass an explicit tag"))
        return _mutation(status="failed", steps=steps, evidence=evidence, source=source)
    for comp in components:
        image = str(comp.get("image") or "")
        if not image:
            steps.append(Step(name="registry", status="failed",
                              detail=f"component {comp.get('name')!r} has no image"))
            return _mutation(status="failed", steps=steps, evidence=evidence, source=source)
        if not image_ok(image):
            steps.append(Step(name="catalog", status="failed",
                              detail=f"component {comp.get('name')!r} image is not an image reference shape"))
            return _mutation(status="failed", steps=steps, evidence=evidence, source=source)
        try:
            digest = runtime.digest.lookup(image, tag)
        except (DigestError, RunnerTimeout) as e:
            # Named: a ship of three components said "manifest not found" and no more, and
            # the agent that had mistyped one image looked for the fault in the registry
            # (the test box, 2026-10-12).
            steps.append(Step(name="registry", status="failed",
                              detail=f"{image}:{tag} (component {comp.get('name')}): {e}"))
            return _mutation(status="failed", steps=steps, evidence=evidence, source=source)
        if expect is not None and digest != expect:
            steps.append(Step(
                name="registry", status="failed",
                detail=f"expect {expect} != {digest}",
            ))
            return _mutation(status="failed", steps=steps, evidence=evidence, source=source)
    evidence["digest"] = digest
    steps.append(Step(name="registry", status="ok", detail=digest))
    commit = None
    for index, comp in enumerate(components):
        name = str(comp.get("name"))
        repository = str(comp.get("image") or "") or None
        if runtime.locks.expired(project, env):
            steps.append(Step(name="bump", status="failed",
                              detail=f"job budget spent before {name}; nothing more written"))
            _skip_later(evidence, components, index)
            if evidence.get("verdict") == "DEPLOYED":
                evidence.pop("verdict", None)
            return _mutation(status="failed", steps=steps, commit=commit,
                             evidence=evidence, source=source)
        argv = [runtime.ship_script, project, env, name, tag]
        extra_env = {"ORBIT_ACTION": action, "ORBIT_ACTOR": actor}
        try:
            result = runtime.runner.run(argv, extra_env)
        except RunnerTimeout as e:
            steps.append(Step(name="bump", status="failed", detail=str(e)))
            _skip_later(evidence, components, index + 1)
            if evidence.get("verdict") == "DEPLOYED":
                evidence.pop("verdict", None)
            return _mutation(status="failed", steps=steps, commit=commit,
                             evidence=evidence, source=source)
        except Exception as e:
            steps.append(Step(name="bump", status="failed", detail=str(e)))
            _skip_later(evidence, components, index + 1)
            if evidence.get("verdict") == "DEPLOYED":
                evidence.pop("verdict", None)
            return _mutation(status="failed", steps=steps, commit=commit,
                             evidence=evidence, source=source)
        output = scrub((result.stdout or "") + "\n" + (result.stderr or ""))
        parsed = parse_commit(result.stdout or "")
        if parsed:
            commit = parsed
            _slot(evidence, name)["commit"] = parsed
        if result.returncode != 0:
            steps.append(Step(name="bump", status="failed",
                              detail=bump_fail_detail(output)))
            _skip_later(evidence, components, index + 1)
            if evidence.get("verdict") == "DEPLOYED":
                evidence.pop("verdict", None)
            return _mutation(status="failed", steps=steps, commit=commit,
                             evidence=evidence, source=source)
        if not parsed:
            steps.append(Step(name="bump", status="failed", detail="no COMMIT line"))
            _skip_later(evidence, components, index + 1)
            if evidence.get("verdict") == "DEPLOYED":
                evidence.pop("verdict", None)
            return _mutation(status="failed", steps=steps, commit=commit,
                             evidence=evidence, source=source)
        _slot(evidence, name)["commit"] = parsed
        steps.append(Step(name="bump", status="ok", detail=name))
        if runtime.locks.expired(project, env):
            steps.append(Step(name="apply", status="failed",
                              detail="job budget spent after the bump; nothing more written"))
            _set_stage(evidence, name, "failed")
            evidence["verdict"] = "LANDED NOT DEPLOYED apply"
            _skip_later(evidence, components, index + 1)
            return _mutation(status="failed", steps=steps, commit=commit,
                             evidence=evidence, source=source)
        applied = apply_step(state, project, env, commit=parsed,
                             deadline=_apply_deadline(runtime, since))
        steps.append(applied)
        if applied.status == "failed":
            _set_stage(evidence, name, "failed")
            evidence["verdict"] = "LANDED NOT DEPLOYED apply"
            _skip_later(evidence, components, index + 1)
            return _mutation(status="failed", steps=steps, commit=commit,
                             evidence=evidence, source=source)
        if applied.status == "skipped":
            # The bump landed in git but this orbitd applied nothing: like the
            # reconcile path for the same condition, refuse rather than report
            # applied. The step detail names ORBITD_APPLY or ORBITD_APPLY_PAIRS.
            _set_stage(evidence, name, "refused")
            evidence["verdict"] = "LANDED NOT DEPLOYED apply"
            _skip_later(evidence, components, index + 1)
            return _mutation(status="refused", steps=steps, commit=commit,
                             evidence=evidence, source=source)
        phase_deadline = _phase_deadline(runtime)
        if str(comp.get("kind") or "web") == "cron":
            rolled = wait_cron_rollout(
                state, project, env, name, tag=tag, repository=repository,
                deadline=phase_deadline,
            )
            steps.append(rolled)
            if rolled.status != "ok":
                _set_stage(evidence, name, "failed")
                evidence["verdict"] = "LANDED NOT DEPLOYED rollout"
                _skip_later(evidence, components, index + 1)
                return _mutation(status="failed", steps=steps, commit=commit,
                                 evidence=evidence, source=source)
            # No pod exists until the next scheduled run, so the four pod assertions are
            # reported, not faked: skipped, with the reason. The CronJob carrying the tag
            # is the whole of what a ship can prove for a cron component.
            for check in CRON_ASSERTIONS:
                steps.append(Step(name=check, status="skipped",
                                  detail="kind cron: no pod until the next scheduled run"))
            _set_stage(evidence, name, "rolled_out")
            evidence.setdefault("verdict", "DEPLOYED")
            continue
        before_generation, _ = snapshot_deployment(
            state, project, env, name, deadline=phase_deadline,
        )
        rolled = wait_rollout(
            state, project, env, name,
            tag=tag, repository=repository, before_generation=before_generation,
            deadline=phase_deadline, since=since,
        )
        steps.append(rolled)
        if rolled.status != "ok":
            _set_stage(evidence, name, "failed")
            evidence["verdict"] = "LANDED NOT DEPLOYED rollout"
            _skip_later(evidence, components, index + 1)
            return _mutation(status="failed", steps=steps, commit=commit,
                             evidence=evidence, source=source)
        _set_stage(evidence, name, "rolled_out")
        failed = _assert_steps(
            state, project, env, name, tag, since, steps, evidence,
            deadline=phase_deadline,
        )
        if failed is not None:
            _skip_later(failed.evidence or evidence, components, index + 1)
            return _mutation(
                status=failed.status, steps=list(failed.steps),
                commit=commit, evidence=dict(failed.evidence or {}),
                source=source,
            )
    return _mutation(status="applied", steps=steps, commit=commit,
                     evidence=evidence, source=source)


def execute_restart(state: AppState, *, project: str, env: str, component: str,
                    tag: str, opening: list[Step]) -> Mutation:
    runtime = runtime_of(state)
    since = runtime.clock()
    source = read_source(state)
    steps = list(opening)
    evidence: dict[str, Any] = {
        "components": [{"name": component, "stage": "not_reached"}],
    }
    namespace = ns(project, env)
    guarded = guard_namespace(project, env, namespace)
    if guarded is not None:
        return sourced(state, guarded)
    name = deployment_name(project, component)
    phase_deadline = _phase_deadline(runtime)
    before_generation, _ = snapshot_deployment(
        state, project, env, component, deadline=phase_deadline,
    )
    ts = rfc3339_now()
    body = {"spec": {"template": {"metadata": {"annotations": {RESTART_ANN: ts}}}}}
    if runtime.locks.expired(project, env):
        steps.append(Step(name="restart", status="failed",
                          detail="job budget spent before the patch; nothing written"))
        return _mutation(status="failed", steps=steps, evidence=evidence, source=source)
    try:
        kube_call(
            state.backends.kube.apps.patch_namespaced_deployment, name, namespace, body,
            _request_timeout=kube_request_timeout(runtime, phase_deadline),
        )
    except BackendError as e:
        steps.append(Step(name="restart", status="failed", detail=str(e.what_failed)))
        return _mutation(status="failed", steps=steps, evidence=evidence, source=source)
    steps.append(Step(name="restart", status="ok", detail=f"{RESTART_ANN}={ts}"))
    evidence["restarted_at"] = ts
    applied = apply_step(state, project, env, commit=None,
                         deadline=_apply_deadline(runtime, since))
    steps.append(applied)
    if applied.status == "failed":
        _set_stage(evidence, component, "failed")
        evidence["verdict"] = "LANDED NOT DEPLOYED apply"
        return _mutation(status="failed", steps=steps, evidence=evidence, source=source)
    # A skipped apply does not stop a restart the way it stops a ship: the
    # pod-template patch above already landed, and that patch IS the restart --
    # it replaces the pods with no applier and no switch. Refusing here would
    # tell the operator nothing was applied while the workload is being
    # replaced, and leave the rollout unobserved. The skipped step stays in the
    # record; the rollout below is waited on and asserted.
    rolled = wait_rollout(
        state, project, env, component,
        tag=tag, before_generation=before_generation,
        deadline=phase_deadline, since=since,
    )
    steps.append(rolled)
    if rolled.status != "ok":
        _set_stage(evidence, component, "failed")
        evidence["verdict"] = "LANDED NOT DEPLOYED rollout"
        return _mutation(status="failed", steps=steps, evidence=evidence, source=source)
    _set_stage(evidence, component, "rolled_out")
    failed = _assert_steps(
        state, project, env, component, tag, since, steps, evidence,
        deadline=phase_deadline,
    )
    if failed is not None:
        return _mutation(
            status=failed.status, steps=list(failed.steps),
            commit=failed.commit, evidence=dict(failed.evidence or {}),
            source=source,
        )
    return _mutation(status="applied", steps=steps, evidence=evidence, source=source)


def resolve_restart_tag(state: AppState, project: str, env: str, component: str) -> str | None:
    runtime = runtime_of(state)
    tag = None
    if runtime.ledger is not None:
        tag = runtime.ledger.current_tag(project, env, component)
    if not tag:
        tag = desired_tag(state, project, env, component)
    if tag:
        return tag
    kube = state.backends.kube
    namespace = ns(project, env)
    for pod in list_pods(kube, namespace, selector(project, component)):
        data = as_dict(pod)
        spec = as_dict(pick(data, "spec") or {})
        for raw in pick(spec, "containers") or []:
            container = as_dict(raw)
            if str(pick(container, "name") or "") == component:
                image = str(pick(container, "image") or "")
                got = image_tag(image)
                if got:
                    return got
    return None


__all__ = [
    "SHIP_SCRIPT", "MONITOR_SCRIPT", "FEATURES_SCRIPT", "RESTART_ANN", "DigestError", "RunnerTimeout", "RunResult",
    "DeployRuntime", "runtime_of", "scrub", "parse_commit", "bump_fail_detail",
    "TAG_PATTERN", "TAG_RE", "IMAGE_RE", "tag_ok", "image_ok", "BUDGET_SPENT", "budget_spent",
    "actor_of", "audit",
    "validate_catalog", "plan_steps", "busy_failure", "start_job", "execute_ship",
    "execute_restart", "resolve_restart_tag", "GitLedger", "EnvLocks", "apply_step",
    "SubprocessRunner", "wait_rollout", "sourced", "new_pod_troubles", "stop_reason",
]
