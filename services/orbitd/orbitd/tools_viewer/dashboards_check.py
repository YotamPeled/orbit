"""orbit_dashboards_check — every generated panel's query, ok / no_data / broken."""

from __future__ import annotations

import json
from typing import Annotated, Any

from pydantic import Field

from ..backends import BackendError
from ..roles import Listing, Registry, Role
from ..state import AppState
from . import _util as u
from .common import relayed
from .util import Ident, run

MONITORING_NAMESPACE = "monitoring"
LABEL_PROJECT = "orbit.local/project"
LABEL_ENV = "orbit.local/env"
LABEL_GENERATED = "grafana_dashboard"


def register(registry: Registry, state: AppState) -> None:

    @registry.tool(role=Role.viewer, read_only=True, idempotent=True)
    def orbit_dashboards_check(
        project: Annotated[Ident | None, Field(description="Project id; omitted means every project.")] = None,
        env: Annotated[Ident | None, Field(description="Environment id; omitted means every environment.")] = None,
    ) -> Listing:
        """Check every generated Grafana panel's query against Prometheus.

        Reads the generated dashboards (the ConfigMaps the Grafana sidecar
        consumes in the monitoring namespace) and runs every panel target's
        query as an instant query. One row per panel: `ok` when the query
        returned at least one series, `no_data` when the query is valid but
        the component emits nothing yet, `broken` when Prometheus refused the
        expression or the dashboard JSON could not be read. A `no_data` row
        names the project, environment and panel so a human knows who to
        chase; a `broken` row carries the reason. Panels with no query
        targets (structural rows, text) are skipped, as are dashboards drawn
        by hand. Returns statuses only, never series data: a caller that
        wants values has orbit_metrics_query. Example:
        orbit_dashboards_check(project="demo", env="prod") returns
        {items: [{dashboard: "orbit-red-demo-prod", project: "demo",
        env: "prod", panel: "Requests/s", status: "ok"}, ...]}."""

        def _go() -> Listing:
            state.backends.prom.require()
            rows: list[dict[str, Any]] = []
            for cm in _generated_dashboards(state, project, env):
                rows.extend(_check_dashboard(state, cm))
            return Listing.of(rows)

        return run(_go)  # type: ignore[return-value]


def _generated_dashboards(state: AppState, project: str | None, env: str | None) -> list[dict[str, Any]]:
    """Generated dashboard ConfigMaps, narrowest first. A dashboard someone drew by
    hand carries no project/env labels and is not the tool's business."""
    resource = state.backends.kube.dynamic.resources.get(api_version="v1", kind="ConfigMap")
    body = u.kube_call(resource.get, namespace=MONITORING_NAMESPACE,
                       label_selector=f"{LABEL_GENERATED}=1")
    out: list[dict[str, Any]] = []
    for cm in u.items_of(body):
        # The dynamic client hands back its own field objects, not dicts; read them as
        # the plain document they carry, or every generated dashboard is skipped.
        cm = _as_document(cm)
        if not isinstance(cm, dict):
            continue
        labels = (cm.get("metadata") or {}).get("labels") or {}
        pid, penv = labels.get(LABEL_PROJECT), labels.get(LABEL_ENV)
        if not pid or not penv:
            continue
        if project is not None and str(pid) != project:
            continue
        if env is not None and str(penv) != env:
            continue
        out.append(cm)
    out.sort(key=lambda cm: str((cm.get("metadata") or {}).get("name") or ""))
    return out


def _as_document(obj: Any) -> Any:
    """The plain dict behind whatever the kubernetes client returned for one object."""
    if isinstance(obj, dict):
        return obj
    to_dict = getattr(obj, "to_dict", None)
    return to_dict() if callable(to_dict) else obj


def _row(dashboard: str, project: str, env: str, panel: str,
         status: str, reason: str | None = None) -> dict[str, Any]:
    row: dict[str, Any] = {
        "dashboard": dashboard, "project": project, "env": env,
        "panel": panel, "status": status,
    }
    if reason:
        row["reason"] = reason
    return row


def _check_dashboard(state: AppState, cm: dict[str, Any]) -> list[dict[str, Any]]:
    meta = cm.get("metadata") or {}
    labels = meta.get("labels") or {}
    pid = str(labels.get(LABEL_PROJECT) or "")
    penv = str(labels.get(LABEL_ENV) or "")
    name = str(meta.get("name") or "")
    rows: list[dict[str, Any]] = []
    data = cm.get("data") or {}
    for key in sorted(data):
        try:
            body = json.loads(data[key])
            if not isinstance(body, dict):
                raise ValueError("dashboard JSON is not an object")
        except (ValueError, TypeError) as e:
            rows.append(_row(name, pid, penv, relayed(key, limit=256), "broken",
                             reason=_short(f"dashboard JSON {key!r} could not be read: {e}")))
            continue
        dashboard = relayed(body.get("uid") or name, limit=256)
        for panel in body.get("panels") or []:
            if not isinstance(panel, dict):
                continue
            title = relayed(panel.get("title") or f"panel-{panel.get('id', '?')}", limit=256)
            exprs = [str(t.get("expr")) for t in (panel.get("targets") or [])
                     if isinstance(t, dict) and t.get("expr")]
            if not exprs:
                continue
            status, reason = _check_panel(state, exprs)
            rows.append(_row(dashboard, pid, penv, title, status, reason=reason))
    return rows


def _check_panel(state: AppState, exprs: list[str]) -> tuple[str, str | None]:
    """One verdict for a panel's targets: a refused query is a generator defect
    even beside healthy targets, otherwise any series means the panel is alive."""
    broken_reason: str | None = None
    saw_ok = False
    for expr in exprs:
        status, detail = _ask(state, expr)
        if status == "broken" and broken_reason is None:
            broken_reason = detail
        elif status == "ok":
            saw_ok = True
    if broken_reason is not None:
        return "broken", broken_reason
    if saw_ok:
        return "ok", None
    return "no_data", None


def _ask(state: AppState, expr: str) -> tuple[str, str | None]:
    try:
        payload = state.backends.prom.get("/api/v1/query", params={"query": expr})
    except BackendError as e:
        if e.status is not None and 400 <= e.status <= 499:
            # How Prometheus answers a query it refuses over HTTP: a parse error
            # is a 400, an execution error (a bad regexp, too many samples) a 422.
            # A 5xx says the backend is down, nothing about the query, so it stays
            # a tool failure rather than thirty invented generator defects.
            return "broken", _short(e.body or e.what_failed)
        raise
    if not isinstance(payload, dict):
        return "broken", f"unexpected prometheus response for {expr[:80]}"
    if payload.get("status") == "error":
        detail = payload.get("error") or payload.get("errorType") or "query rejected"
        return "broken", _short(str(detail))
    data = payload.get("data")
    result = (data.get("result") or []) if isinstance(data, dict) else []
    if isinstance(result, list) and len(result) > 0:
        return "ok", None
    return "no_data", None


def _short(text: str, limit: int = 200) -> str:
    from .common import relayed
    return relayed(text, limit=limit)
