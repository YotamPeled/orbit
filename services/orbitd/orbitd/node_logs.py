"""Pod logs read from the node through the Kubernetes API.

This is what the platform has when the logs feature is off (no Loki, no Alloy): the lines
the kubelet still holds for pods that still exist, for each container's current run and
the run before it. A deleted pod's lines are gone, and nothing here is searchable beyond
a line filter. The logs tool and the deploy step's log assertion both read through here.
"""

from __future__ import annotations

import datetime as dt
import re
from typing import Any




def as_dict(obj: Any) -> dict[str, Any]:
    """A kube object or a dict, as a dict (the client's models carry to_dict)."""
    if isinstance(obj, dict):
        return obj
    to_dict = getattr(obj, "to_dict", None)
    if callable(to_dict):
        data = to_dict()
        return data if isinstance(data, dict) else {}
    data = getattr(obj, "_data", None)
    return data if isinstance(data, dict) else {}


# One container's read is bounded both ways: a chatty pod cannot fill orbitd's memory.
TAIL_LINES = 2000
LIMIT_BYTES = 2 * 1024 * 1024
_STAMP = re.compile(r"^(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})(?:\.(\d{1,9}))?(Z|[+-]\d{2}:\d{2}) ?(.*)$")
# A LogQL line-filter chain, the only part of a query the node can answer:
#   |= "text"   != "text"   |~ "regex"   !~ "regex"
_FILTER = re.compile(r'\s*(\|=|!=|\|~|!~)\s*"((?:[^"\\]|\\.)*)"')


class UnsupportedQuery(ValueError):
    """The query asks for more than a line filter."""


def line_filters(query: str | None) -> list[tuple[str, Any]]:
    """Parse a chain of line filters; raise UnsupportedQuery for anything else."""
    out: list[tuple[str, Any]] = []
    rest = (query or "").strip()
    while rest:
        m = _FILTER.match(rest)
        if not m:
            raise UnsupportedQuery(rest[:80])
        op, text = m.group(1), re.sub(r"\\(.)", r"\1", m.group(2))
        if op in ("|~", "!~"):
            try:
                out.append((op, re.compile(text)))
            except re.error as e:
                raise UnsupportedQuery(f"regex: {e}") from e
        else:
            out.append((op, text))
        rest = rest[m.end():].strip()
    return out


def keeps(line: str, filters: list[tuple[str, Any]]) -> bool:
    for op, what in filters:
        if op == "|=" and what not in line:
            return False
        if op == "!=" and what in line:
            return False
        if op == "|~" and not what.search(line):
            return False
        if op == "!~" and what.search(line):
            return False
    return True


def unix_nanos(stamp: str, fraction: str | None, zone: str) -> int:
    moment = dt.datetime.fromisoformat(stamp + ("+00:00" if zone == "Z" else zone))
    return int(moment.timestamp()) * 1_000_000_000 + int((fraction or "0").ljust(9, "0"))


def parse(text: str) -> list[tuple[int, str]]:
    """`kubectl logs --timestamps` text as (unix nanos, line); an unstamped line (a
    continuation the runtime split) takes the stamp of the line before it."""
    rows: list[tuple[int, str]] = []
    last = 0
    for raw in (text or "").splitlines():
        m = _STAMP.match(raw)
        if m:
            last = unix_nanos(m.group(1), m.group(2), m.group(3))
            rows.append((last, m.group(4)))
        elif raw.strip():
            rows.append((last, raw))
    return rows


def containers_of(pod: Any) -> list[tuple[str, bool]]:
    """(container, has a previous run) for every container of a pod, init containers first."""
    doc = as_dict(pod)
    spec = as_dict(doc.get("spec") or {})
    status = as_dict(doc.get("status") or {})
    restarted: dict[str, bool] = {}
    for key in ("initContainerStatuses", "init_container_statuses", "containerStatuses", "container_statuses"):
        for cs in status.get(key) or []:
            cs = as_dict(cs)
            count = cs.get("restartCount", cs.get("restart_count", 0)) or 0
            restarted[str(cs.get("name"))] = int(count) > 0
    names: list[str] = []
    for key in ("initContainers", "init_containers", "containers"):
        for c in spec.get(key) or []:
            name = as_dict(c).get("name")
            if name and str(name) not in names:
                names.append(str(name))
    return [(name, restarted.get(name, False)) for name in names]


def read_container(kube: Any, namespace: str, pod: str, container: str | None, *,
                   previous: bool = False, since_seconds: int | None = None) -> list[tuple[int, str]]:
    """One container run's lines. A run that does not exist (no previous container, a
    container that never started) is no lines, not an error."""
    kwargs: dict[str, Any] = {"timestamps": True, "tail_lines": TAIL_LINES, "limit_bytes": LIMIT_BYTES}
    if container:
        kwargs["container"] = container
    if previous:
        kwargs["previous"] = True
    if since_seconds is not None:
        kwargs["since_seconds"] = max(1, int(since_seconds))
    try:
        text = read_text(kube, pod, namespace, **kwargs)
    except Exception as e:
        if getattr(e, "status", None) in (400, 404):
            return []
        raise
    return parse(text)


def read_text(kube: Any, pod: str, namespace: str, **kwargs: Any) -> str:
    """A pod log as the text the container wrote. Asked for RAW: left to itself the
    Kubernetes client decodes a log that happens to be one JSON document into a Python
    object and hands back its repr (single quotes), which is no longer what was logged
    (seen on the test box: a converge Job's report could not be read back)."""
    raw = kube.core.read_namespaced_pod_log(pod, namespace, _preload_content=False, **kwargs)
    data = getattr(raw, "data", None)
    if isinstance(data, (bytes, bytearray)):
        return bytes(data).decode("utf-8", "replace")
    return raw if isinstance(raw, str) else str(raw or "")


def read_pod(kube: Any, namespace: str, pod: Any, *, since_seconds: int | None = None
             ) -> list[dict[str, Any]]:
    """Every line the node holds for a pod: each container's previous run (when it
    restarted), then its current one."""
    name = str(as_dict(as_dict(pod).get("metadata") or {}).get("name") or "")
    rows: list[dict[str, Any]] = []
    if not name:
        return rows
    for container, restarted in containers_of(pod) or [("", False)]:
        runs = [True, False] if restarted else [False]
        for previous in runs:
            for ts, line in read_container(kube, namespace, name, container or None,
                                           previous=previous, since_seconds=since_seconds):
                row: dict[str, Any] = {"ts": str(ts), "pod": name, "line": line}
                if container:
                    row["container"] = container
                if previous:
                    row["previous"] = True
                rows.append(row)
    return rows
