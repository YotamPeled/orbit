"""orbit_monitor_status — Prometheus /api/v1/rules matched by alert name."""

from __future__ import annotations

from typing import Annotated, Any, Literal

from pydantic import BaseModel, Field

from ..alert_log import Transition, alert_log_of
from ..backends import BackendError, BackendUnconfigured
from ..roles import Listing, Registry, Role, failure
from ..state import AppState
from .util import run

MonitorState = Literal["firing", "pending", "inactive"]
_RANK = {"inactive": 0, "pending": 1, "firing": 2}
LOKI_RULES_PATH = "/prometheus/api/v1/rules"


class MonitorTransition(BaseModel):
    state: Literal["firing", "resolved"]
    at: str = ""


class MonitorStatus(BaseModel):
    name: str
    state: MonitorState
    project: str = ""
    env: str = ""
    kind: str = ""
    severity: str = ""
    summary: str = ""
    last_transition: MonitorTransition | None = None
    definition: dict[str, Any] = Field(
        default_factory=dict,
        description="the monitor's file as it is in the repository: query, condition {comparison, "
                    "threshold, for}, target and module (a probe), notify, runbook. What "
                    "orbit_monitor_upsert takes back, changed")
    silences: list[dict[str, Any]] | None = Field(
        default=None,
        description="the silences in force for this monitor: {id, ends_at, created_by, comment}; [] when "
                    "none; null when Alertmanager could not be asked. Lift them with orbit_monitor_unsilence")


def register(registry: Registry, state: AppState) -> None:

    @registry.tool(role=Role.viewer, read_only=True, idempotent=True)
    def orbit_monitor_status(
        name: Annotated[str | None, Field(description="Monitor / alert name. Omit to return every defined monitor.")] = None,
    ) -> Listing:
        """Report firing/pending/inactive state for declared monitors.

        Definitions come from the instance repo; live state is Prometheus
        /api/v1/rules matched by alert name, and for a log monitor the Loki ruler's
        rules (the same shape, from Loki). An unknown name is not_found. Each item
        also carries `definition`, the monitor's whole file (query, condition, target,
        notify: change it and pass it to orbit_monitor_upsert), and `silences`, the
        silences in force for it from Alertmanager ({id, ends_at, created_by, comment};
        null when Alertmanager did not answer): a silenced monitor still shows `firing`
        here while nobody is messaged.
        Example: orbit_monitor_status(name="queue-backlog") returns
        {items: [{name, state, project, env, kind, severity, summary, definition, silences}]}."""

        def _go() -> Listing:
            state.backends.instance.require()
            defined = [body for body in state.backends.instance.monitors()
                       if isinstance(body, dict) and body.get("name")]
            by_name = {str(body["name"]): body for body in defined}
            if name is not None and name not in by_name:
                return failure(
                    "not_found",
                    f"monitor {name} is not defined",
                    next_action="call orbit_monitor_list to see defined monitors",
                    name=name,
                )  # type: ignore[return-value]
            state.backends.prom.require()
            rules = state.backends.prom.get("/api/v1/rules")
            live = _states(rules)
            wanted_now = [by_name[name]] if name is not None else defined
            if any(body.get("kind") == "log" for body in wanted_now):
                # A log monitor's rule is not in Prometheus: the Loki ruler evaluates it,
                # and answers in Prometheus's own shape. Without this a log monitor
                # read `inactive` while it was firing (v1.0.1-rc1 on the test box).
                # A Loki that does not answer leaves those monitors at what Prometheus
                # says, which is inactive; the alert itself is still in orbit_alerts_active.
                try:
                    state.backends.loki.require()
                    for alert, value in _states(state.backends.loki.get(LOKI_RULES_PATH)).items():
                        if _RANK[value] > _RANK[live.get(alert, "inactive")]:
                            live[alert] = value
                except (BackendError, BackendUnconfigured):
                    pass
            log = alert_log_of(state)
            wanted = [by_name[name]] if name is not None else defined
            silenced = active_silences(state)
            items = [_status(body, live.get(str(body["name"]), "inactive"),
                             log.latest(str(body["name"])),
                             None if silenced is None else silenced.get(str(body["name"]), []))
                     for body in wanted]
            return Listing.of([i.model_dump() for i in items])

        return run(_go)  # type: ignore[return-value]


def active_silences(state: AppState) -> dict[str, list[dict[str, Any]]] | None:
    """Monitor name -> its silences in force, from Alertmanager; None when it cannot be
    asked. A silence is a monitor's when it has the matcher `orbit_monitor=<name>`, the
    one orbit_monitor_silence writes."""
    try:
        state.backends.alertmanager.require()
        listed = state.backends.alertmanager.get("/api/v2/silences")
    except (BackendError, BackendUnconfigured):
        return None
    out: dict[str, list[dict[str, Any]]] = {}
    for row in listed if isinstance(listed, list) else []:
        if not isinstance(row, dict) or str((row.get("status") or {}).get("state") or "") != "active":
            continue
        for matcher in row.get("matchers") or []:
            if (isinstance(matcher, dict) and matcher.get("name") == "orbit_monitor"
                    and not matcher.get("isRegex") and matcher.get("isEqual", True) and matcher.get("value")):
                out.setdefault(str(matcher["value"]), []).append({
                    "id": str(row.get("id") or ""), "ends_at": str(row.get("endsAt") or ""),
                    "created_by": str(row.get("createdBy") or ""), "comment": str(row.get("comment") or "")})
    return out


def _status(body: dict[str, Any], state: MonitorState,
            last: Transition | None = None,
            silences: list[dict[str, Any]] | None = None) -> MonitorStatus:
    return MonitorStatus(
        definition={str(k): v for k, v in body.items()},
        silences=silences,
        name=str(body.get("name") or ""),
        state=state,
        project=str(body.get("project") or ""),
        env=str(body.get("env") or ""),
        kind=str(body.get("kind") or ""),
        severity=str(body.get("severity") or ""),
        summary=str(body.get("summary") or ""),
        last_transition=(MonitorTransition(state=last.state, at=last.at)
                         if last is not None else None),
    )


def _states(data: object) -> dict[str, MonitorState]:
    payload = data if isinstance(data, dict) else {}
    groups = ((payload.get("data") or {}).get("groups") or []) if isinstance(payload.get("data"), dict) else []
    out: dict[str, MonitorState] = {}
    for group in groups:
        if not isinstance(group, dict):
            continue
        for rule in group.get("rules") or []:
            if not isinstance(rule, dict):
                continue
            if rule.get("type") not in (None, "alerting", "alert"):
                continue
            alert = str(rule.get("name") or rule.get("alert") or "")
            if not alert:
                continue
            state = _coerce(rule.get("state"))
            if _RANK[state] > _RANK[out.get(alert, "inactive")]:
                out[alert] = state
    return out


def _coerce(value: object) -> MonitorState:
    text = str(value or "inactive").lower()
    if text == "firing":
        return "firing"
    if text == "pending":
        return "pending"
    return "inactive"
