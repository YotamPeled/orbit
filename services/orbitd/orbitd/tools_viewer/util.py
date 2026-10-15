"""Shared helpers for viewer tools. Not a tool."""

from __future__ import annotations

import re
import time
from collections.abc import Callable
from typing import Annotated, TypeVar

from mcp_types import CallToolResult
from pydantic import Field

from ..backends import BackendError, BackendUnconfigured

LOGS_CAP = 200
# A query string from the agent: one line, bounded. It is appended to a selector the
# tool built (LogQL) or sent whole (PromQL); the backend parses it, the bound keeps
# it from being a payload.
QUERY_MAX = 2048
QUERY_PATTERN = r"^[^\r\n\x00-\x08\x0b\x0c\x0e-\x1f]{1,2048}$"
PROMQL_PATTERN = r"^[^\r\n\x00-\x08\x0b\x0c\x0e-\x1f]{1,4096}$"
CURSOR_PATTERN = r"^[0-9]{1,20}(:[0-9]{1,6})?$"
EPOCH_OR_RFC3339 = (r"^([0-9]{1,10}(\.[0-9]{1,9})?|"
                    r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(\.[0-9]{1,9})?(Z|[+-][0-9]{2}:[0-9]{2}))$")
RANGE_MAX_S = 30 * 86400
METRICS_CAP = 11_000
TRACES_FIND_CAP = 20
TRACES_SPAN_CAP = 500

IDENT_PATTERN = r"^[a-z0-9][a-z0-9-]{0,62}$"
Ident = Annotated[str, Field(pattern=IDENT_PATTERN, min_length=1, max_length=63)]
Duration = Annotated[str, Field(
    pattern=r"^[0-9]+(ms|s|m|h|d|w)$",
    description="one number and one unit (`15m`, `3h`, `2d`), no compounds",
)]

_UNITS = {"ms": 0.001, "s": 1.0, "m": 60.0, "h": 3600.0, "d": 86400.0, "w": 604800.0}
_DURATION_RE = re.compile(r"^([0-9]+)(ms|s|m|h|d|w)$")

T = TypeVar("T")


def duration_seconds(value: str) -> float:
    match = _DURATION_RE.fullmatch(value.strip())
    if match is None:
        raise ValueError(f"invalid duration {value!r}")
    return int(match.group(1)) * _UNITS[match.group(2)]


def now() -> float:
    return time.time()


def run(fn: Callable[[], T]) -> T | CallToolResult:
    try:
        return fn()
    except BackendUnconfigured as e:
        return e.as_failure()
    except BackendError as e:
        return e.as_failure()


def ts_int(value: str) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0
