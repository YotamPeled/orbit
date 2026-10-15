"""Post-ship assertions: image, rollout, ready, log.

`assert_deployed` is the in-process port of `scripts/assert-deployed.sh`. It talks to
kube, Loki and HTTP through the backends orbitd already holds, never kubectl, and
never raises — a backend error is a failed step whose detail is the failure
envelope's `what_failed`.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlparse, urlunparse

import httpx
import yaml
from pydantic import BaseModel, Field

from . import node_logs
from .backends import BackendError, BackendUnconfigured
from .roles import Step
from .state import AppState
from .tools_viewer._util import kube_call
from .tools_viewer.common import (
    as_dict, deployment_name, find_project, image_matches, image_tag, ns, pick,
    selector,
)

WAITING_REASONS = ("CrashLoopBackOff", "ImagePullBackOff", "ErrImagePull")
LOG_POLL_S = 5.0
LOKI_LIMIT = 1000
LOKI_PATH = "/loki/api/v1/query_range"
HTTP_TIMEOUT = 15.0
STEP_NAMES = ("image", "rollout", "ready", "log")
DEFAULT_PROBE_S = 10
KUBE_REQUEST_TIMEOUT_S = 30.0
VERDICT_OK = "DEPLOYED"
VERDICT_FAIL_PREFIX = "LANDED NOT DEPLOYED"


class _Deadline(Exception):
    """The job's remaining deadline elapsed during assertions."""

Clock = Callable[[], float]
Sleeper = Callable[[float], None]
HttpGet = Callable[[str], int]


class AssertReport(BaseModel):
    steps: list[Step]
    passed: bool
    verdict: str
    evidence: dict[str, Any] = Field(default_factory=dict)


def _kube_timeout(state: AppState, deadline: float | None, now: Clock) -> tuple[float, float]:
    """The (connect, read) pair the kubernetes client honours; a float would be no timeout."""
    cap = KUBE_REQUEST_TIMEOUT_S
    runtime = getattr(state, "deploy", None)
    raw = getattr(runtime, "kube_timeout_s", None) if runtime is not None else None
    if raw is not None:
        try:
            cap = float(raw)
        except (TypeError, ValueError):
            cap = KUBE_REQUEST_TIMEOUT_S
    if cap <= 0:
        cap = KUBE_REQUEST_TIMEOUT_S
    if deadline is None:
        return (cap, cap)
    each = max(1e-3, min(cap, float(deadline) - now()))
    return (each, each)


def _raise_if_expired(deadline: float | None, now: Clock) -> None:
    if deadline is not None and now() > deadline:
        raise _Deadline()


def assert_deployed(
    state: AppState,
    project: str,
    env: str,
    component: str,
    tag: str,
    since: Any,
    *,
    timeout_s: float,
    clock: Clock | None = None,
    sleeper: Sleeper | None = None,
    http_get: HttpGet | None = None,
    deadline: float | None = None,
) -> AssertReport:
    """Four read-only assertions that the cluster is running `tag` for this component.

    `since` is the ship time (RFC3339 or unix seconds). `timeout_s` is the Loki poll
    window (the spec's 60 s). `clock` / `sleeper` / `http_get` are injectable so tests
    do not wait or go on the wire.
    """
    evidence: dict[str, Any] = {}
    steps: list[Step] = []
    now = clock or _now
    sleep = sleeper or _sleep
    getter = http_get or _http_status
    try:
        since_unix = _parse_since(since)
    except (TypeError, ValueError):
        return _report([_step("image", False, "invalid since")], evidence, failed="args")

    current = "image"
    try:
        _raise_if_expired(deadline, now)
        kube = state.backends.kube
        namespace = ns(project, env)
        rto = _kube_timeout(state, deadline, now)
        pods = _pod_set(kube, namespace, project, component, _request_timeout=rto)
        evidence["pods"] = _pod_images(pods, component)

        expected = _expected_image_ref(state, project, component, tag)
        ok, detail = _assert_image(pods, component, tag, namespace, project, expected)
        if not ok:
            return _report([_step("image", False, detail)], evidence, failed="image")
        steps.append(_step("image", True, detail))

        current = "rollout"
        _raise_if_expired(deadline, now)
        rto = _kube_timeout(state, deadline, now)
        ok, detail, counters = _assert_rollout(
            kube, namespace, project, component, pods, _request_timeout=rto,
        )
        evidence["deployment"] = counters
        if not ok:
            return _report(steps + [_step("rollout", False, detail)], evidence, failed="rollout")
        steps.append(_step("rollout", True, detail))

        current = "ready"
        _raise_if_expired(deadline, now)
        rto = _kube_timeout(state, deadline, now)
        ok, detail, ready_ev = _assert_ready(
            kube, namespace, project, component, pods, now, sleep,
            _request_timeout=rto, deadline=deadline,
        )
        evidence["ready"] = ready_ev
        if not ok:
            return _report(steps + [_step("ready", False, detail)], evidence, failed="ready")
        steps.append(_step("ready", True, detail))

        current = "log"
        _raise_if_expired(deadline, now)
        rto = _kube_timeout(state, deadline, now)
        pods_after = _pod_set(
            kube, namespace, project, component, _request_timeout=rto,
        )
        ok, detail, log_ev = _assert_log(
            state, project, env, component, namespace, pods_after, since_unix,
            timeout_s, now, sleep, getter, deadline=deadline,
        )
        evidence.update(log_ev)
        if not ok:
            return _report(steps + [_step("log", False, detail)], evidence, failed="log")
        steps.append(_step("log", True, detail))
        return _report(steps, evidence)
    except _Deadline:
        # the step that ran out of budget is named, not a fixed one: a rollout that
        # passed stays ok, and the operator looks at the window that was cut short
        return _report(
            steps + [_step(current, False, f"timeout: the job's deadline elapsed during {current}")],
            evidence, failed=current,
        )
    except (BackendError, BackendUnconfigured) as e:
        return _report(steps + [_step(current, False, _what_failed(e))], evidence, failed=current)
    except Exception as e:
        return _report(steps + [_step(current, False, str(e))], evidence, failed=current)


# ------------------------------------------------------------------ report


def _step(name: str, ok: bool, detail: str | None = None) -> Step:
    return Step(name=name, status="ok" if ok else "failed", detail=detail)


def _report(done: list[Step], evidence: dict[str, Any], *, failed: str | None = None) -> AssertReport:
    seen = {s.name for s in done}
    steps = list(done)
    for name in STEP_NAMES:
        if name not in seen:
            steps.append(Step(name=name, status="skipped"))
    order = {name: i for i, name in enumerate(STEP_NAMES)}
    steps.sort(key=lambda s: order.get(s.name, len(STEP_NAMES)))
    if failed is None:
        return AssertReport(steps=steps, passed=True, verdict=VERDICT_OK, evidence=evidence)
    return AssertReport(
        steps=steps, passed=False,
        verdict=f"{VERDICT_FAIL_PREFIX} {failed}",
        evidence=evidence,
    )


def _what_failed(exc: BackendError | BackendUnconfigured) -> str:
    result = exc.as_failure()
    content = getattr(result, "structured_content", None) or {}
    if isinstance(content, dict) and content.get("what_failed"):
        return str(content["what_failed"])
    return str(getattr(exc, "what_failed", None) or exc)


# ------------------------------------------------------------------ kube objects


def _pod_set(kube: Any, namespace: str, project: str, component: str,
             *, _request_timeout: Any = (KUBE_REQUEST_TIMEOUT_S, KUBE_REQUEST_TIMEOUT_S)) -> list[Any]:
    """Chart-label pods whose deletionTimestamp is empty. Nothing else is filtered."""
    listed = kube_call(
        kube.core.list_namespaced_pod, namespace,
        label_selector=selector(project, component),
        _request_timeout=_request_timeout,
    )
    items = list(getattr(listed, "items", None) or [])
    out = []
    for pod in items:
        meta = as_dict(pick(as_dict(pod), "metadata") or {})
        if pick(meta, "deletionTimestamp", "deletion_timestamp"):
            continue
        out.append(pod)
    return out


def _meta_name(pod: Any) -> str:
    return str(pick(as_dict(pick(as_dict(pod), "metadata") or {}), "name") or "")


def _seq(value: Any) -> list[Any]:
    if not value:
        return []
    return list(value) if not isinstance(value, list) else value


def _container_statuses(pod: Any) -> list[Any]:
    status = as_dict(pick(as_dict(pod), "status") or {})
    return _seq(pick(status, "containerStatuses", "container_statuses")) + _seq(
        pick(status, "initContainerStatuses", "init_container_statuses")
    )


def _named_containers(pod: Any, component: str) -> list[dict[str, Any]]:
    spec = as_dict(pick(as_dict(pod), "spec") or {})
    return [
        as_dict(c) for c in _seq(pick(spec, "containers"))
        if str(pick(as_dict(c), "name") or "") == component
    ]


def _is_ready(pod: Any) -> bool:
    """Ready only when a Pod condition `type=Ready` is `status=True`. No fallback."""
    status = as_dict(pick(as_dict(pod), "status") or {})
    for raw in _seq(pick(status, "conditions")):
        cond = as_dict(raw)
        if pick(cond, "type") == "Ready":
            return pick(cond, "status") == "True"
    return False


def _restart_count(pod: Any) -> int:
    n = 0
    for raw in _container_statuses(pod):
        try:
            n += int(pick(as_dict(raw), "restartCount", "restart_count", default=0) or 0)
        except (TypeError, ValueError):
            pass
    return n


def _created_unix(pod: Any) -> int:
    ts = pick(as_dict(pick(as_dict(pod), "metadata") or {}),
              "creationTimestamp", "creation_timestamp")
    if not ts:
        return 0
    text = str(ts).strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(text)
    except (TypeError, ValueError):
        return 0
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp())


def _pod_images(pods: list[Any], component: str) -> list[dict[str, str]]:
    rows = []
    for pod in pods:
        name = _meta_name(pod)
        named = _named_containers(pod, component)
        image = str(pick(named[0], "image") or "") if named else ""
        rows.append({"name": name, "image": image})
    return rows


def _as_int(value: Any) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


# ------------------------------------------------------------------ A / B image


def _expected_image_ref(state: AppState, project: str, component: str, tag: str) -> str:
    """Catalog image repository plus the shipped tag. Empty when the catalog has none."""
    try:
        entry = find_project(state, project) or {}
    except Exception:
        entry = {}
    if not isinstance(entry, dict):
        entry = {}
    for item in entry.get("components") or []:
        if isinstance(item, dict) and item.get("name") == component:
            image = str(item.get("image") or "").strip()
            if image:
                return f"{image}:{tag}"
            break
    return ""


def _container_image_id(pod: Any, component: str) -> str:
    status = as_dict(pick(as_dict(pod), "status") or {})
    for raw in _seq(pick(status, "containerStatuses", "container_statuses")):
        cs = as_dict(raw)
        if str(pick(cs, "name") or "") == component:
            return str(pick(cs, "imageID", "image_id") or "")
    return ""


def _assert_image(
    pods: list[Any], component: str, tag: str, namespace: str, project: str,
    expected: str,
) -> tuple[bool, str]:
    if not pods:
        sel = selector(project, component)
        return False, f"empty pod set for {sel} in {namespace}"
    bad: list[str] = []
    for pod in pods:
        name = _meta_name(pod) or "?"
        named = _named_containers(pod, component)
        if not named:
            bad.append(f"{name}=")
            continue
        for container in named:
            image = str(pick(container, "image") or "")
            image_id = _container_image_id(pod, component)
            if expected:
                if not image_matches(image, expected, image_id):
                    bad.append(f"{name}={image}")
            elif image_tag(image) != tag:
                bad.append(f"{name}={image}")
    if bad:
        return False, ";".join(bad)
    return True, f"{len(pods)} pod(s) tag={tag}"


# ------------------------------------------------------------------ C rollout


def _assert_rollout(
    kube: Any, namespace: str, project: str, component: str, pods: list[Any],
    *, _request_timeout: Any = (KUBE_REQUEST_TIMEOUT_S, KUBE_REQUEST_TIMEOUT_S),
) -> tuple[bool, str, dict[str, Any]]:
    name = deployment_name(project, component)
    dep = kube_call(
        kube.apps.read_namespaced_deployment, name, namespace,
        _request_timeout=_request_timeout,
    )
    data = as_dict(dep)
    meta = as_dict(pick(data, "metadata") or {})
    status = as_dict(pick(data, "status") or {})
    generation = _as_int(pick(meta, "generation"))
    observed = _as_int(pick(status, "observedGeneration", "observed_generation"))
    updated = _as_int(pick(status, "updatedReplicas", "updated_replicas"))
    ready = _as_int(pick(status, "readyReplicas", "ready_replicas"))
    replicas = _as_int(pick(status, "replicas"))
    available = _as_int(pick(status, "availableReplicas", "available_replicas"))
    counters = {
        "generation": generation, "observedGeneration": observed,
        "updatedReplicas": updated, "readyReplicas": ready,
        "replicas": replicas, "availableReplicas": available,
    }
    if generation is None or observed is None or observed != generation:
        return False, f"observedGeneration!=generation on {name} (GENERATION {observed} {generation})", counters
    if updated is None or ready is None or replicas is None or available is None:
        return False, f"replica fields missing on {name} (MISSING {updated} {ready} {replicas} {available})", counters
    if not (updated == ready == replicas == available):
        return False, (
            f"updatedReplicas/readyReplicas/replicas/availableReplicas "
            f"MISMATCH {updated} {ready} {replicas} {available}"
        ), counters
    oom: list[str] = []
    waiting: list[str] = []
    for pod in pods:
        pname = _meta_name(pod) or "?"
        for raw in _container_statuses(pod):
            cs = as_dict(raw)
            cname = str(pick(cs, "name") or "?")
            last = as_dict(pick(cs, "lastState", "last_state") or {})
            terminated = as_dict(pick(last, "terminated") or {})
            if pick(terminated, "reason") == "OOMKilled":
                oom.append(f"{pname}:{cname}")
            state = as_dict(pick(cs, "state") or {})
            wait = as_dict(pick(state, "waiting") or {})
            reason = pick(wait, "reason")
            if reason in WAITING_REASONS:
                waiting.append(f"{pname}:{cname}:{reason}")
    if oom:
        return False, f"OOMKilled lastState: {','.join(oom)}", counters
    if waiting:
        return False, f"waiting: {','.join(waiting)}", counters
    return True, f"{replicas}=={replicas}=={replicas}, no OOMKilled/CrashLoopBackOff/ImagePullBackOff/ErrImagePull", counters


# ------------------------------------------------------------------ D ready


def _probe_period(pods: list[Any], component: str) -> tuple[int, bool]:
    """Return (period_s, no_probe). Default 10 when the probe is absent."""
    seen = False
    periods: list[int] = []
    for pod in pods:
        for container in _named_containers(pod, component):
            probe = pick(container, "readinessProbe", "readiness_probe")
            if not probe:
                continue
            seen = True
            raw = pick(as_dict(probe), "periodSeconds", "period_seconds")
            if raw is None:
                continue
            n = _as_int(raw)
            if n is not None:
                periods.append(n)
    if not seen:
        return DEFAULT_PROBE_S, True
    return (periods[0] if periods else DEFAULT_PROBE_S), False


def _ready_snapshot(pods: list[Any]) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for pod in pods:
        name = _meta_name(pod)
        if not name:
            continue
        out[name] = {"ready": _is_ready(pod), "restarts": _restart_count(pod)}
    return out


def _assert_ready(
    kube: Any, namespace: str, project: str, component: str, pods: list[Any],
    now: Clock, sleep: Sleeper,
    *, _request_timeout: Any = (KUBE_REQUEST_TIMEOUT_S, KUBE_REQUEST_TIMEOUT_S),
    deadline: float | None = None,
) -> tuple[bool, str, dict[str, Any]]:
    snap1 = _ready_snapshot(pods)
    evidence: dict[str, Any] = {"snapshot1": snap1}
    if not snap1:
        return False, "empty pod set to probe", evidence
    period, no_probe = _probe_period(pods, component)
    evidence["period_s"] = period
    sleep_for = float(period)
    if deadline is not None:
        left = float(deadline) - now()
        if left <= 0:
            raise _Deadline()
        sleep_for = min(sleep_for, left)
    sleep(sleep_for)
    _raise_if_expired(deadline, now)
    pods2 = _pod_set(
        kube, namespace, project, component, _request_timeout=_request_timeout,
    )
    snap2 = _ready_snapshot(pods2)
    evidence["snapshot2"] = snap2
    if not snap2:
        return False, f"empty pod set after {period}s", evidence
    for name in snap1:
        if name not in snap2:
            return False, f"{name} left the set after {period}s", evidence
    for name in snap2:
        if name not in snap1:
            return False, f"{name} joined the set after {period}s", evidence
    for name, first in snap1.items():
        second = snap2[name]
        if not first["ready"] or not second["ready"]:
            if no_probe:
                return False, f"no readinessProbe, waited {period}s, Ready lost: {name}", evidence
            return False, f"not Ready: {name}", evidence
        if first["restarts"] != second["restarts"]:
            return False, f"restartCount {name} {first['restarts']}->{second['restarts']}", evidence
    if no_probe:
        return True, f"no readinessProbe, waited {period}s, Ready ({len(snap1)} pod(s))", evidence
    return True, f"Ready across {period}s ({len(snap1)} pod(s))", evidence


# ------------------------------------------------------------------ E log + web


REUSED_LOG_WINDOW_S = 900
REUSED_LOG_WAIT_S = 5.0


def _assert_log(
    state: AppState, project: str, env: str, component: str, namespace: str,
    pods: list[Any], since_unix: int, timeout_s: float,
    now: Clock, sleep: Sleeper, http_get: HttpGet,
    *, deadline: float | None = None,
) -> tuple[bool, str, dict[str, Any]]:
    evidence: dict[str, Any] = {}
    created = {_meta_name(p): _created_unix(p) for p in pods if _meta_name(p)}
    if not created:
        return False, "no pods in the set to query", evidence
    deploy = deployment_name(project, component)
    query = '{namespace="%s", pod=~"%s-.*"}' % (namespace, deploy)
    # A roll that created no pod — a rollback to the ReplicaSet whose pod never stopped
    # serving because the bad version never became ready — leaves only pods older than
    # the ship. "A line from a pod born since the ship" can then never hold, and the
    # rollback read LANDED NOT DEPLOYED log although it had landed (docs/V1.md §8, S04).
    # For reused pods a recent line from those very pods is looked for, and named when
    # there is one. When there is none the pods have simply been quiet: they were not
    # restarted, so they owe no new line, and the steps before this one already proved
    # them Ready. Requiring a line failed a good rollback of a service with no traffic,
    # "no Loki line from the reused pods in the last 15 min", twice on the test box
    # (v1.0.1-rc2: a dev service nobody had called for an hour). A set that does hold a
    # new pod still owes a line from it.
    reused = all(born < since_unix for born in created.values())
    window = since_unix - REUSED_LOG_WINDOW_S if reused else since_unix
    if reused:
        timeout_s = min(float(timeout_s), REUSED_LOG_WAIT_S)     # the lines sought are in the past
    # With the logs feature off there is no Loki to ask: the same proof (a line from one
    # of these pods since the ship) is read from the node through the Kubernetes API.
    from_node = state.features.snapshot().on.get("logs", True) is False
    if from_node:
        hit = _poll_node(
            state, namespace, pods, created, window, timeout_s, now, sleep, deadline=deadline,
            born_floor=0 if reused else since_unix,
        )
    else:
        hit = _poll_loki(
            state, query, created, window, timeout_s, now, sleep, deadline=deadline,
            born_floor=0 if reused else since_unix,
        )
    where = "node log" if from_node else "Loki"
    if hit is None and not reused:
        if from_node:
            return False, f"no log line on the node from new pods in {int(timeout_s)}s", evidence
        return False, f"no Loki line from new pods in {int(timeout_s)}s ({query})", evidence
    if hit is None:
        evidence["log"] = {"reused": True, "quiet": True, **({"source": "node"} if from_node else {})}
        seen = (f"the pods were not restarted and wrote nothing in the last "
                f"{REUSED_LOG_WINDOW_S // 60} min (quiet, and Ready)")
    else:
        evidence["log"] = {"pod": hit[0], "ts": hit[1], **({"reused": True} if reused else {}),
                           **({"source": "node"} if from_node else {})}
        seen = f"{where} {hit[0]}"

    try:
        kind, catalog = _catalog_component(state, project, env, component)
    except (BackendError, BackendUnconfigured) as e:
        return False, _what_failed(e), evidence
    except Exception as e:
        return False, f"catalog read error: {e}", evidence
    if catalog.get("error"):
        return False, f"catalog read error: {catalog['error']}", evidence

    if kind != "web":
        return True, f"{seen}, http skipped (kind {kind})", evidence

    url = _web_url(project, env, component, namespace, catalog)
    if not url:
        return False, "kind web has no public hostname in the catalog", evidence
    try:
        status = int(http_get(url))
    except (BackendError, BackendUnconfigured) as e:
        return False, _what_failed(e), evidence
    except Exception as e:
        evidence["http"] = {"url": _redact_url(url), "status": None}
        return False, f"{_redact_url(url)} failed: {e}", evidence
    evidence["http"] = {"url": _redact_url(url), "status": status}
    if status != 200:
        return False, f"{_redact_url(url)} returned {status}", evidence
    return True, f"{seen}, {_redact_url(url)} 200", evidence


def _poll_node(
    state: AppState, namespace: str, pods: list[Any], created: dict[str, int], since_unix: int,
    timeout_s: float, now: Clock, sleep: Sleeper,
    *, deadline: float | None = None, born_floor: int | None = None,
) -> tuple[str, str] | None:
    """_poll_loki's question asked of the node: the first line stamped at or after
    `since_unix` from a pod of the set born at or after the floor, as (pod, unix nanos)."""
    floor = since_unix if born_floor is None else born_floor
    kube = state.backends.kube
    node_deadline: float | None = None
    while True:
        t = now()
        _raise_if_expired(deadline, now)
        if node_deadline is None:
            node_deadline = float(since_unix) + float(timeout_s)
            if node_deadline <= t:
                node_deadline = t + float(timeout_s)
            if deadline is not None:
                node_deadline = min(node_deadline, float(deadline))
        if t >= since_unix:
            for pod in pods:
                name = _meta_name(pod)
                if not name or created.get(name, 0) < floor:
                    continue
                rows = node_logs.read_pod(kube, namespace, pod,
                                          since_seconds=int(t - since_unix) + 1)
                rows = [r for r in rows if int(r["ts"]) >= int(since_unix) * 1_000_000_000]
                if rows:
                    first = min(rows, key=lambda r: int(r["ts"]))
                    return name, first["ts"]
            if t >= node_deadline:
                return None
        sleep_for = LOG_POLL_S
        if deadline is not None:
            sleep_for = min(sleep_for, max(0.0, float(deadline) - t))
        sleep(sleep_for)


def _poll_loki(
    state: AppState, query: str, created: dict[str, int], since_unix: int,
    timeout_s: float, now: Clock, sleep: Sleeper,
    *, deadline: float | None = None, born_floor: int | None = None,
) -> tuple[str, str] | None:
    floor = since_unix if born_floor is None else born_floor
    state.backends.loki.require()
    loki_deadline: float | None = None
    while True:
        t = now()
        _raise_if_expired(deadline, now)
        if loki_deadline is None:
            loki_deadline = float(since_unix) + float(timeout_s)
            if loki_deadline <= t:
                loki_deadline = t + float(timeout_s)
            if deadline is not None:
                loki_deadline = min(loki_deadline, float(deadline))
        if t < since_unix:
            sleep(LOG_POLL_S)
            continue
        start_ns = int(since_unix) * 1_000_000_000
        end_ns = int(t) * 1_000_000_000
        match = _query_loki_pages(state, query, created, floor, start_ns, end_ns)
        if match is not None:
            return match
        if t >= loki_deadline:
            return None
        sleep_for = LOG_POLL_S
        if deadline is not None:
            sleep_for = min(sleep_for, max(0.0, float(deadline) - t))
        sleep(sleep_for)


def _query_loki_pages(
    state: AppState, query: str, created: dict[str, int], since_unix: int,
    start_ns: int, end_ns: int,
) -> tuple[str, str] | None:
    while True:
        data = state.backends.loki.get(LOKI_PATH, params={
            "query": query,
            "start": str(start_ns),
            "end": str(end_ns),
            "limit": LOKI_LIMIT,
            "direction": "forward",
        })
        if not isinstance(data, dict) or data.get("status") != "success":
            raise BackendError(
                status=None, body=str(data)[:200],
                what_failed=f"Loki query_range failed for {query}",
                env_var="ORBITD_LOKI_URL", retryable=True,
            )
        match, n_values, last_ts = _loki_match(data, created, since_unix)
        if match is not None:
            return match
        if n_values < LOKI_LIMIT or not last_ts:
            return None
        start_ns = last_ts + 1


def _loki_match(
    data: dict[str, Any], created: dict[str, int], since_unix: int,
) -> tuple[tuple[str, str] | None, int, int]:
    n_values = 0
    last_ts = 0
    streams = ((data.get("data") or {}).get("result") or []) if isinstance(data.get("data"), dict) else []
    for stream in streams:
        if not isinstance(stream, dict):
            continue
        labels = stream.get("stream") or {}
        pod = str(labels.get("pod") or labels.get("pod_name") or "")
        for pair in stream.get("values") or []:
            if not isinstance(pair, (list, tuple)) or len(pair) < 2:
                continue
            n_values += 1
            try:
                ts = int(pair[0])
            except (TypeError, ValueError):
                ts = 0
            if ts > last_ts:
                last_ts = ts
            born = created.get(pod)
            if born is None or born < since_unix:
                continue
            return (pod, str(pair[0])), n_values, last_ts
    return None, n_values, last_ts


def _catalog_component(
    state: AppState, project: str, env: str, component: str,
) -> tuple[str, dict[str, Any]]:
    try:
        entry = find_project(state, project)
    except (BackendError, BackendUnconfigured):
        raise
    except Exception as e:
        return "web", {"error": str(e)}
    if entry is None:
        return "web", {}
    kind = "web"
    comp: dict[str, Any] = {}
    for item in entry.get("components") or []:
        if isinstance(item, dict) and item.get("name") == component:
            kind = str(item.get("kind") or "web")
            comp = item
            break
    environments = entry.get("environments") or {}
    envcfg = environments.get(env) if isinstance(environments, dict) else {}
    if not isinstance(envcfg, dict):
        envcfg = {}
    return kind, {"component": comp, "env": envcfg, "rendered_hosts": _rendered_hosts(state, project, env)}


def _rendered_hosts(state: AppState, project: str, env: str) -> list[str]:
    """The hostnames the generator gave this pair: `hostnames` in the generated
    Application's values. An environment that names none in the catalog still has its
    default `<project>.<domain>` there, and that is the door the chart publishes: the
    first prod ship of a project that kept the default was failed for having "no public
    hostname in the catalog" while its site answered (QA of v1.0.0, F1)."""
    try:
        from .tools_viewer.render_get import read_render
        docs = [d for d in yaml.safe_load_all(read_render(state, project, env)) if isinstance(d, dict)]
    except Exception:
        return []
    for doc in docs:
        if doc.get("kind") != "Application":
            continue
        values = ((((doc.get("spec") or {}).get("source") or {}).get("helm") or {}).get("valuesObject")) or {}
        hosts = values.get("hostnames") if isinstance(values, dict) else None
        return [str(h) for h in hosts] if isinstance(hosts, list) else []
    return []


def _web_url(
    project: str, env: str, component: str, namespace: str, catalog: dict[str, Any],
) -> str | None:
    comp = catalog.get("component") or {}
    envcfg = catalog.get("env") or {}
    hosts = _public_hosts(envcfg, catalog.get("rendered_hosts")) if env == "prod" else []
    if not hosts:
        # Not prod (its door asks for credentials), or a prod environment that declares
        # no public door (`expose: false`, `hostnames: []`): the component's own Service,
        # inside the cluster. A prod web component without a door used to fail every ship
        # with "no public hostname in the catalog".
        service = f"{project}-{component}"
        port = _as_int(comp.get("port")) or 80
        path = str(comp.get("readinessPath") or comp.get("probePath") or "/")
        if not path.startswith("/"):
            path = "/" + path
        return f"http://{service}.{namespace}.svc:{port}{path}"
    # The component's own door: the root when it serves `/`, else its path plus its
    # probePath (catalog-sync door_path's rule). A component served under /api has no
    # router at `/`, so the root read Traefik's 404 and halted a good prod ship after its
    # first component (S02 on the test box, docs/V1.md §8).
    path = str(comp.get("path") or "/")
    if path.rstrip("/"):
        probe = str(comp.get("probePath") or "/")
        return f"https://{hosts[0]}{path.rstrip('/')}{probe if probe.startswith('/') else '/' + probe}"
    return f"https://{hosts[0]}/"


def _public_hosts(envcfg: dict[str, Any], rendered: list[str] | None = None) -> list[str]:
    """The environment's public hostnames, the catalog's own first. With none written
    there (and no `hostnames: []`, which declares no door), the generator's default is
    the door: `rendered` is what it wrote into the pair's values."""
    if envcfg.get("expose") is False:
        return []
    hosts = list(envcfg.get("hostnames") or [])
    if not hosts and "hostnames" not in envcfg:
        hosts = list(rendered or [])
    for item in envcfg.get("customDomains") or []:
        if isinstance(item, dict) and item.get("host"):
            hosts.append(str(item["host"]))
    seen: set[str] = set()
    out: list[str] = []
    for host in hosts:
        h = str(host).lower().rstrip(".")
        if h and h not in seen:
            seen.add(h)
            out.append(h)
    return out


def _redact_url(url: str) -> str:
    parsed = urlparse(url)
    if parsed.username or parsed.password:
        host = parsed.hostname or ""
        if parsed.port:
            host = f"{host}:{parsed.port}"
        netloc = host
        return urlunparse(parsed._replace(netloc=netloc))
    return url


def _http_status(url: str) -> int:
    try:
        response = httpx.get(url, timeout=HTTP_TIMEOUT, follow_redirects=False)
    except httpx.RequestError as e:
        raise BackendError(
            status=None, body=str(e),
            what_failed=f"HTTP GET {_redact_url(url)} failed",
            retryable=True,
        ) from e
    return int(response.status_code)


def _parse_since(since: Any) -> int:
    if isinstance(since, bool):
        raise ValueError("invalid since")
    if isinstance(since, (int, float)):
        return int(since)
    text = str(since).strip()
    if not text:
        raise ValueError("invalid since")
    if text.isdigit():
        return int(text)
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    dt = datetime.fromisoformat(text)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp())


def _now() -> float:
    return datetime.now(timezone.utc).timestamp()


def _sleep(seconds: float) -> None:
    import time
    time.sleep(seconds)


__all__ = ["AssertReport", "assert_deployed"]
