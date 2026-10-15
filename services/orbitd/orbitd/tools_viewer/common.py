"""Shared helpers for viewer tools: namespace, pagination, backend guards, kube objects."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Callable, TypeVar

from pydantic import BaseModel, Field

from orbitd.backends import BackendError, BackendUnconfigured
from orbitd.jobs import Jobs
from orbitd.roles import Listing, failure
from orbitd.state import AppState

T = TypeVar("T")

WORKLOAD_CAP = 100
HISTORY_CAP = 100
HISTORY_DEFAULT = 20
EVENTS_CAP = 20

LABEL_INSTANCE = "app.kubernetes.io/instance"
LABEL_NAME = "app.kubernetes.io/name"


def jobs_of(state: AppState) -> Jobs:
    existing = getattr(state, "jobs", None)
    if existing is None:
        existing = Jobs()
        state.jobs = existing
    return existing


_KUBE_TRANSPORT_NAMES = {
    "ApiException", "MaxRetryError", "ProtocolError", "NewConnectionError",
    "ConnectTimeoutError", "ReadTimeoutError", "ConnectError", "ConnectTimeout",
    "ReadTimeout", "TimeoutException", "ProxyError",
}


def _walk_exc(exc: BaseException):
    seen: set[int] = set()
    cur: BaseException | None = exc
    while cur is not None and id(cur) not in seen:
        seen.add(id(cur))
        yield cur
        cur = cur.__cause__ or cur.__context__


def _is_kube_transport(exc: BaseException) -> bool:
    for cur in _walk_exc(exc):
        if isinstance(cur, (OSError, TimeoutError, ConnectionError)):
            return True
        if cur.__class__.__name__ in _KUBE_TRANSPORT_NAMES:
            return True
    return False


RELAY_MAX = 4096


def relayed(text: Any, *, limit: int = RELAY_MAX) -> str:
    """Cluster text (a log line, an event message, an alert annotation, a backend
    error) on its way into a tool result: credential shapes and the daemon's own held
    secrets are replaced the way the mutation path replaces them (deploy.scrub), and
    the text is cut at `limit` characters with a marker, so one line cannot carry a
    token or fill an agent's context."""
    from orbitd.deploy import scrub  # lazy: deploy imports this module

    value = scrub("" if text is None else str(text))
    if len(value) > limit:
        return value[:limit] + f" …[cut {len(value) - limit} chars]"
    return value


def require_env(entry: dict[str, Any], project: str, env: str) -> Any:
    """The not_found failure when `env` is not one of the project's environments,
    else None. A namespace that does not exist lists as empty, which reads as "no
    pods": the catalog decides whether the pair exists, never the empty answer."""
    if env not in env_names(entry):
        return not_found(f"env {env!r} is not in project {project!r}", project=project, env=env)
    return None


def _kube_failure(exc: BaseException):
    status = None
    reason = None
    for cur in _walk_exc(exc):
        if cur.__class__.__name__ == "ApiException":
            status = getattr(cur, "status", status)
            reason = getattr(cur, "reason", reason) or reason
    body = relayed(reason or str(exc), limit=200)
    if status == 404:
        return failure(
            "not_found",
            body or "kubernetes object not found",
            next_action="check the project, env and object name",
            retryable=False,
            status=status,
        )
    return BackendError(
        status=status, body=body, what_failed="kubernetes API request failed",
        env_var="ORBITD_KUBECONFIG",
    ).as_failure()


def guarded(thunk: Callable[[], T]) -> T | Any:
    """Run a tool body; backend and kube errors become the actionable failure."""
    try:
        return thunk()
    except BackendUnconfigured as e:
        return e.as_failure()
    except BackendError as e:
        return e.as_failure()
    except Exception as e:
        if not _is_kube_transport(e):
            raise
        return _kube_failure(e)


def ns(project: str, env: str) -> str:
    return f"{project}-{env}"


def selector(project: str, component: str) -> str:
    return f"{LABEL_INSTANCE}={project},{LABEL_NAME}={component}"


class ClockedListing(Listing):
    """A Listing plus the server clock, so a caller never needs a shell to get the time."""

    now: str = Field(description="RFC 3339 timestamp of the server clock at the call")


class WorkloadPod(BaseModel):
    pod: str | None = None
    component: str | None = None
    phase: str | None = None
    ready: bool = False
    restarts: int = 0
    last_terminated_reason: str | None = None
    image: str | None = None
    age: int | None = Field(default=None, description="seconds since the pod was created")
    node: str | None = None


def rfc3339_now() -> str:
    """Server clock as RFC 3339 (UTC, trailing Z)."""
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def clocked(listing: Listing, now: str | None = None) -> ClockedListing:
    return ClockedListing(now=now or rfc3339_now(), **listing.model_dump())


def paginate(items: list[Any], *, cursor: str | None, limit: int, cap: int) -> Listing:
    limit = min(max(int(limit), 1), cap)
    start = 0
    if cursor:
        try:
            start = max(int(cursor), 0)
        except (TypeError, ValueError):
            start = 0
    window = items[start:start + limit]
    next_cursor = str(start + limit) if (start + limit) < len(items) else None
    return Listing.of(window, total=len(items), next_cursor=next_cursor)


def as_dict(obj: Any) -> dict[str, Any]:
    if obj is None:
        return {}
    if isinstance(obj, dict):
        return obj
    to_dict = getattr(obj, "to_dict", None)
    if callable(to_dict):
        data = to_dict()
        return data if isinstance(data, dict) else {}
    data = getattr(obj, "_data", None)
    return data if isinstance(data, dict) else {}


def pick(d: dict[str, Any] | None, *keys: str, default: Any = None) -> Any:
    d = d or {}
    for key in keys:
        if key in d and d[key] is not None:
            return d[key]
    return default


def not_found(what: str, **evidence: Any) -> Any:
    return failure(
        "not_found",
        what,
        next_action="pass an identifier another tool returned, or list first",
        retryable=False,
        **evidence,
    )


def projects_doc(state: AppState) -> dict[str, Any]:
    doc = state.backends.instance.projects() or {}
    return doc if isinstance(doc, dict) else {}


def project_entries(state: AppState) -> list[dict[str, Any]]:
    projects = projects_doc(state).get("projects") or []
    return [p for p in projects if isinstance(p, dict)]


def find_project(state: AppState, project: str) -> dict[str, Any] | None:
    for entry in project_entries(state):
        if entry.get("id") == project:
            return entry
    return None


def env_names(entry: dict[str, Any]) -> list[str]:
    environments = entry.get("environments") or {}
    if isinstance(environments, dict):
        return list(environments)
    if isinstance(environments, list):
        out = []
        for item in environments:
            if isinstance(item, str):
                out.append(item)
            elif isinstance(item, dict) and item.get("name"):
                out.append(str(item["name"]))
        return out
    return []


def component_names(entry: dict[str, Any]) -> list[str]:
    components = entry.get("components") or []
    names = []
    for item in components:
        if isinstance(item, str):
            names.append(item)
        elif isinstance(item, dict) and item.get("name"):
            names.append(str(item["name"]))
    return names


def tags_tree(state: AppState) -> dict[str, Any]:
    doc = state.backends.instance.tags() or {}
    if not isinstance(doc, dict):
        return {}
    tree = doc.get("tags", doc)
    return tree if isinstance(tree, dict) else {}


def tag_key(project: str, env: str, component: str) -> str:
    return f"{project}/{env}/{component}"


def desired_tag(state: AppState, project: str, env: str, component: str) -> str | None:
    """Look up catalog/tags.yaml's flat `project/env/component` key. Missing means default."""
    tree = tags_tree(state)
    value = tree.get(tag_key(project, env, component))
    if value is None or value == "":
        return None
    return str(value)


def image_tag(image: str) -> str:
    if not image:
        return ""
    last = image.rsplit("/", 1)[-1]
    if "@sha256:" in image:
        return image.rsplit("@", 1)[-1]
    if ":" in last:
        return last.rsplit(":", 1)[-1]
    return image


def image_repo(ref: str) -> str:
    """Repository portion of an image reference (tag and digest stripped)."""
    s = (ref or "").strip()
    if "://" in s:
        s = s.split("://", 1)[1]
    if "@" in s:
        return s.split("@", 1)[0]
    slash = s.rfind("/")
    colon = s.rfind(":")
    if colon > slash:
        return s[:colon]
    return s


def image_matches(img: str, want: str, image_id: str = "") -> bool:
    """Exact `repo:tag`, or digest-form of that repo whose imageID names the same repo.

    Same rules as `scripts/assert-deployed.sh` `image_matches`.
    """
    if img == want:
        return True
    exp_repo = image_repo(want)
    if not exp_repo:
        return False
    prefix = exp_repo + "@sha256:"
    if not img.startswith(prefix):
        return False
    digest = img[len(prefix):]
    if not digest:
        return False
    for ch in digest:
        if ch not in "0123456789abcdefABCDEF":
            return False
    return image_repo(image_id) == exp_repo


def age_seconds(timestamp: str | None) -> int | None:
    if not timestamp:
        return None
    text = str(timestamp).replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return max(0, int((datetime.now(timezone.utc) - dt).total_seconds()))


def list_pods(kube: Any, namespace: str, label_selector: str | None = None) -> list[Any]:
    kwargs: dict[str, Any] = {}
    if label_selector:
        kwargs["label_selector"] = label_selector
    listed = kube.core.list_namespaced_pod(namespace, **kwargs)
    return list(getattr(listed, "items", None) or [])


def list_deployments(kube: Any, namespace: str) -> list[Any]:
    listed = kube.apps.list_namespaced_deployment(namespace)
    return list(getattr(listed, "items", None) or [])


def deployment_name(project: str, component: str) -> str:
    return f"{project}-{component}"


def find_deployment(kube: Any, namespace: str, project: str, component: str) -> Any | None:
    wanted = {deployment_name(project, component), component}
    for dep in list_deployments(kube, namespace):
        name = pick(as_dict(as_dict(dep).get("metadata")), "name")
        if name in wanted:
            return dep
    return None


def pod_is_active(pod: Any) -> bool:
    """False for Succeeded/Failed and for pods with a deletionTimestamp."""
    data = as_dict(pod)
    meta = as_dict(pick(data, "metadata") or {})
    if pick(meta, "deletionTimestamp", "deletion_timestamp"):
        return False
    status = as_dict(pick(data, "status") or {})
    return pick(status, "phase") not in {"Succeeded", "Failed"}


# A container the kubelet has stopped trying to start for now, or cannot start at all:
# what `kubectl get pods` shows in STATUS. A pod in one of these is not on its way.
STUCK_WAITING = frozenset({
    "CrashLoopBackOff", "ImagePullBackOff", "ErrImagePull", "InvalidImageName",
    "CreateContainerConfigError", "CreateContainerError", "RunContainerError",
})
IMAGE_WAITING = frozenset({"ImagePullBackOff", "ErrImagePull", "InvalidImageName"})
# From this many restarts on, each after an exit that was not a clean one, a container is
# crash-looping: the kubelet's own back-off has begun (10 s, 20 s, 40 s, ...).
CRASH_LOOP_RESTARTS = 2


def pod_trouble(pod: Any) -> dict[str, Any] | None:
    """What is wrong with a pod that is not serving, in the kubelet's own words; None
    for a pod whose containers are all ready (or that has no container status yet).

    {pod, container, reason, restarts, last_exit, stuck, sentence}. `stuck` is true for a
    container the kubelet is backing off from (CrashLoopBackOff, an image it cannot
    pull, a config it cannot build): such a pod does not become ready by waiting.
    A container that runs and is not ready yet (its readiness probe has not passed) is
    reported with `stuck` false: that can be a slow start. A container that has restarted
    twice or more after failing exits is crash-looping (`reason: CrashLoopBackOff`,
    `stuck` true) whatever state it is in at the instant it is read."""
    data = as_dict(pod)
    meta = as_dict(pick(data, "metadata") or {})
    status = as_dict(pick(data, "status") or {})
    name = str(pick(meta, "name") or "")
    found: dict[str, Any] | None = None
    statuses = list(pick(status, "initContainerStatuses", "init_container_statuses") or []) \
        + list(pick(status, "containerStatuses", "container_statuses") or [])
    for raw in statuses:
        cs = as_dict(raw)
        if pick(cs, "ready", default=False):
            continue
        state = as_dict(pick(cs, "state") or {})
        waiting = as_dict(pick(state, "waiting") or {})
        terminated = as_dict(pick(state, "terminated") or {})
        if terminated and str(pick(terminated, "reason") or "") == "Completed":
            continue                               # an init container that finished
        last = as_dict(pick(as_dict(pick(cs, "lastState", "last_state") or {}), "terminated") or {})
        restarts = int(pick(cs, "restartCount", "restart_count", default=0) or 0)
        reason = str(pick(waiting, "reason") or "")
        exit_reason = str(pick(last, "reason") or pick(terminated, "reason") or "")
        exit_code = pick(last, "exitCode", "exit_code")
        if exit_code is None:
            exit_code = pick(terminated, "exitCode", "exit_code")
        last_exit = (f"{exit_reason or 'exited'} (exit code {exit_code})" if exit_code is not None
                     else (exit_reason or None))
        container = str(pick(cs, "name") or "")
        stuck = reason in STUCK_WAITING
        # A container that keeps exiting is in a crash loop whatever it is doing at this
        # instant: between two crashes it reads `terminated`, then `waiting`
        # (CrashLoopBackOff), then `running` for the seconds it lasts. Read at the wrong
        # moment, a pod on its third crash was "waiting (Waiting)" and its rollout
        # "progressing" (v1.0.1-rc1 on the test box). What it has done says more than
        # what it is doing: restarts, and a last exit that was not a clean one.
        failed_exit = exit_code not in (None, 0)
        crashing = failed_exit and restarts >= 1
        if stuck:
            said = f"pod {name}: container {container} is in {reason}"
            if restarts:
                said += f" after {restarts} restart(s)"
            if last_exit:
                said += f", last exit {last_exit}"
        elif crashing:
            reason = "CrashLoopBackOff" if restarts >= CRASH_LOOP_RESTARTS else "Crashing"
            stuck = restarts >= CRASH_LOOP_RESTARTS
            said = (f"pod {name}: container {container} keeps crashing: {restarts} restart(s), "
                    f"last exit {last_exit}")
        elif terminated:
            reason = "Exited"
            said = f"pod {name}: container {container} exited ({last_exit or 'no exit status'}) and is being restarted"
        elif pick(state, "running") is not None:
            reason = "NotReady"
            said = f"pod {name}: container {container} is running but not ready (its readiness check has not passed)"
            if restarts:
                said += f", {restarts} restart(s)"
        else:
            reason = reason or "Waiting"
            said = f"pod {name}: container {container} is waiting ({reason})"
        row = {"pod": name, "container": container, "reason": reason, "restarts": restarts,
               "last_exit": last_exit, "stuck": stuck, "sentence": said}
        if stuck:
            return row
        found = found or row
    if found is None and str(pick(status, "phase") or "") == "Pending" and not statuses:
        conditions = pick(status, "conditions") or []
        for raw in conditions:
            cond = as_dict(raw)
            if pick(cond, "type") == "PodScheduled" and str(pick(cond, "status")) == "False":
                why = str(pick(cond, "reason") or "Unschedulable")
                return {"pod": name, "container": "", "reason": why, "restarts": 0, "last_exit": None,
                        "stuck": True, "sentence": f"pod {name} cannot be scheduled ({why}): "
                                                   f"{str(pick(cond, 'message') or '')[:200]}".rstrip(": ")}
    return found


def pod_row(pod: Any) -> dict[str, Any]:
    data = as_dict(pod)
    meta = as_dict(pick(data, "metadata") or {})
    spec = as_dict(pick(data, "spec") or {})
    status = as_dict(pick(data, "status") or {})
    labels = pick(meta, "labels") or {}
    containers = pick(spec, "containers") or []
    first = as_dict(containers[0] if containers else {})
    statuses = pick(status, "containerStatuses", "container_statuses") or []
    ready = True if statuses else False
    restarts = 0
    last_reason = None
    image = pick(first, "image")
    for raw in statuses:
        cs = as_dict(raw)
        ready = ready and bool(pick(cs, "ready", default=False))
        restarts += int(pick(cs, "restartCount", "restart_count", default=0) or 0)
        last = as_dict(pick(cs, "lastState", "last_state") or {})
        terminated = as_dict(pick(last, "terminated") or {})
        if last_reason is None:
            last_reason = pick(terminated, "reason")
        image = pick(cs, "image") or image
    return WorkloadPod(
        pod=pick(meta, "name"),
        component=(labels or {}).get(LABEL_NAME),
        phase=pick(status, "phase"),
        ready=bool(ready),
        restarts=restarts,
        last_terminated_reason=last_reason,
        image=image,
        age=age_seconds(pick(meta, "creationTimestamp", "creation_timestamp")),
        node=pick(spec, "nodeName", "node_name"),
    ).model_dump()


__all__ = [
    "WORKLOAD_CAP", "HISTORY_CAP", "HISTORY_DEFAULT", "EVENTS_CAP",
    "LABEL_INSTANCE", "LABEL_NAME",
    "ClockedListing", "WorkloadPod", "rfc3339_now", "clocked",
    "jobs_of", "guarded", "ns", "selector", "paginate", "as_dict", "pick",
    "not_found", "projects_doc", "project_entries", "find_project",
    "env_names", "component_names", "tags_tree", "tag_key", "desired_tag", "image_tag",
    "age_seconds", "list_pods", "list_deployments", "deployment_name",
    "find_deployment", "pod_is_active", "pod_row", "pod_trouble", "STUCK_WAITING", "IMAGE_WAITING",
    "CRASH_LOOP_RESTARTS",
]
