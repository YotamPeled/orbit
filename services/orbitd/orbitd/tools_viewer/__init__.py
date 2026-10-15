"""Viewer tools over the live cluster, observability backends and orbit:// resources.

`register` is the one hook `server.py` calls. Each sibling module's
register() is invoked exactly once.
"""

from __future__ import annotations

from orbitd.roles import Registry
from orbitd.state import AppState

from .common import jobs_of
from . import alerts_active
from . import catalog_get
from . import catalog_history
from . import catalog_schema
from . import dashboards_check
from . import db_status
from . import deploy_history
from . import deploy_status
from . import deploy_wait
from . import job_status
from . import job_wait
from . import ledger_get
from . import logs_query
from . import metrics_query
from . import monitor_list
from . import monitor_status
from . import platform_budget
from . import platform_diff
from . import platform_health
from . import features_list
from . import project_list
from . import render_get
from . import resources
from . import size_recommend
from . import traces_find
from . import traces_get
from . import workload_describe
from . import workload_list


def register(registry: Registry, state: AppState, server=None) -> None:
    jobs_of(state)
    catalog_get.register(registry, state)
    catalog_history.register(registry, state)
    dashboards_check.register(registry, state)
    db_status.register(registry, state)
    deploy_history.register(registry, state)
    deploy_status.register(registry, state)
    deploy_wait.register(registry, state)
    job_status.register(registry, state)
    job_wait.register(registry, state)
    project_list.register(registry, state)
    workload_describe.register(registry, state)
    workload_list.register(registry, state)
    logs_query.register(registry, state)
    metrics_query.register(registry, state)
    traces_find.register(registry, state)
    traces_get.register(registry, state)
    alerts_active.register(registry, state)
    monitor_list.register(registry, state)
    monitor_status.register(registry, state)
    platform_budget.register(registry, state)
    size_recommend.register(registry, state)
    platform_health.register(registry, state)
    platform_diff.register(registry, state)
    catalog_schema.register(registry, state)
    render_get.register(registry, state)
    ledger_get.register(registry, state)
    features_list.register(registry, state)
    resources.bind(registry)
    if server is not None:
        resources.mount(server, state)
