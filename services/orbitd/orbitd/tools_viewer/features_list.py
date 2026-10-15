"""orbit_features_list — the platform's optional features and the state of each."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from .. import features_status
from ..roles import Registry, Role
from ..state import AppState
from .common import guarded


class Requirement(BaseModel):
    name: str
    met: bool
    detail: str


class FeatureRow(BaseModel):
    feature: str
    summary: str
    always: bool
    declared: bool = Field(description="on in the instance repo (instance/config.yaml `features:`)")
    applied: bool | None = Field(default=None, description="its Applications are applied on the cluster; null when that cannot be read")
    ready: bool | None = Field(default=None, description="declared on, applied, and every workload ready; null for a feature that is off")
    not_ready: list[str] = Field(default_factory=list)
    requires: list[Requirement] = Field(default_factory=list)
    tools: list[str] = Field(default_factory=list, description="MCP tools that exist only while it is on")
    apps: dict[str, bool] = Field(default_factory=dict)
    pending: str | None = Field(default=None, description="what a converge would do now: enable, disable, or null")
    memory_mib: float | None = Field(
        default=None,
        description="resident memory of its own workloads right now (the pods of the applications it owns); "
                    "null for a feature that owns no application (it lives inside another feature's, as "
                    "dashboards lives in the metrics stack) and when Prometheus cannot be asked")
    says: str = ""


class FeaturesListing(BaseModel):
    items: list[FeatureRow]
    total: int = 0
    returned: int = 0
    truncated: bool = False
    next_cursor: str | None = None
    explicit: bool = Field(default=False, description="the instance declares a `features:` block (a feature left out of it is off)")
    converge_job: str | None = Field(default=None, description="the converge Job running now, if any")
    problem: str | None = None


def listing(state: AppState) -> FeaturesListing:
    data = features_status.collect(state)
    rows = [FeatureRow(**row, says=features_status.sentence(row)) for row in data["features"]]
    return FeaturesListing(items=rows, total=len(rows), returned=len(rows), explicit=data["explicit"],
                           converge_job=data["converge_job"], problem=data["problem"])


def register(registry: Registry, state: AppState) -> None:

    @registry.tool(role=Role.viewer, read_only=True, idempotent=True)
    def orbit_features_list() -> FeaturesListing:
        """List the platform's features and the state of each. `core` always runs; every
        other feature is optional and switched by an admin with orbit_features_set. Per
        feature: `declared` (on in the instance repo), `applied` (its Applications are on
        the cluster), `ready` and `not_ready` (its workloads), `requires` (each thing it
        needs — a bucket, another feature, a sealed secret — with `met` and a sentence),
        `tools` (the MCP tools that exist only while it is on), `pending` (what a
        converge would do now: a feature declared on and not applied reads `enable`, one
        declared off and still applied reads `disable`), `memory_mib` (resident memory of
        its own workloads right now; null when it owns no application) and `says`, the
        row as one sentence. `explicit` is
        whether the instance declares a `features:` block; `converge_job` names the
        converge Job running now; `problem` says why the checkout or the cluster could
        not be read. Example: orbit_features_list() -> {items: [{feature: "traces",
        declared: false, applied: false, tools: ["orbit_traces_find", "orbit_traces_get"],
        says: "traces: off"}, ...]}."""
        return guarded(lambda: listing(state))  # type: ignore[return-value]


__all__: list[Any] = ["FeaturesListing", "FeatureRow", "listing", "register"]
