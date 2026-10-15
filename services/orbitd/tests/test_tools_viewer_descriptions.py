"""Tool-description defects from the phase-3 throwaway acceptance.

Each test fails before the matching fix and passes after.
"""

from __future__ import annotations

import typing
from datetime import datetime, timezone

import pytest

from pydantic import TypeAdapter

from orbitd.roles import Listing, Registry
from orbitd.tools_viewer import register
from orbitd.tools_viewer.common import WorkloadPod
from orbitd.tools_viewer.deploy_status import ComponentDeploy
from orbitd.tools_viewer.util import Duration
from orbitd.tools_viewer.workload_describe import WorkloadDescribe

from test_tools_viewer_kube import DEMO, call, labels, seed_catalog
from test_tools_viewer_observability import invoke

SECONDS = "seconds since the pod was created"
SINCE = "one number and one unit (`15m`, `3h`, `2d`), no compounds"


def _registry(state) -> Registry:
    registry = Registry()
    register(registry, state)
    return registry


def _description(extra: object) -> str:
    return str(getattr(extra, "description", "") or "")


def _since_field(fn) -> object:
    hint = typing.get_type_hints(fn, include_extras=True)["since"]
    extras = typing.get_args(hint)
    for extra in extras:
        if _description(extra):
            return extra
        nested = typing.get_args(extra)
        for inner in nested:
            if _description(inner):
                return inner
    raise AssertionError(f"since has no Field description: {hint!r}")


def assert_rfc3339_now(value: object) -> None:
    assert isinstance(value, str) and value, value
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    assert parsed.tzinfo is not None, value
    delta = abs((datetime.now(timezone.utc) - parsed).total_seconds())
    assert delta < 10, value


def test_usage_vs_limit_lists_only_the_component_pods(fake_backends):
    """Foreign namespace pods used to appear with memory_limit null."""
    state = fake_backends
    seed_catalog(state, [DEMO])
    kube = state.backends.kube
    kube.add_pod(namespace="demo-prod", name="web-0", component="web",
                 labels=labels("demo", "web"))
    kube.add_pod(namespace="demo-prod", name="worker-0", component="worker",
                 labels=labels("demo", "worker"))
    kube.add_deployment(namespace="demo-prod", name="demo-web")
    kube.pod_metrics = [
        {"metadata": {"name": "web-0", "namespace": "demo-prod"},
         "containers": [{"name": "web", "usage": {"cpu": "10m", "memory": "45Mi"}}]},
        {"metadata": {"name": "worker-0", "namespace": "demo-prod"},
         "containers": [{"name": "worker", "usage": {"cpu": "5m", "memory": "20Mi"}}]},
    ]
    result = invoke(state, "orbit_workload_describe",
                    project="demo", env="prod", component="web")
    assert not getattr(result, "is_error", False), result
    rows = result.usage_vs_limit
    assert [row.pod for row in rows] == ["web-0"]
    assert all(row.memory_limit is not None for row in rows)
    assert rows[0].memory_limit == "256Mi"


def test_age_and_pod_age_descriptions_say_seconds(fake_backends):
    registry = _registry(fake_backends)
    for name in ("orbit_deploy_status", "orbit_workload_describe", "orbit_workload_list"):
        assert SECONDS in registry.get(name).description, name
    assert ComponentDeploy.model_fields["pod_age"].description == SECONDS
    assert WorkloadPod.model_fields["age"].description == SECONDS


def test_deploy_status_docs_name_spec_images_and_image_id(fake_backends):
    text = _registry(fake_backends).get("orbit_deploy_status").description
    assert "spec.containers" in text
    assert "imageID" in text
    assert "running_digests" in text
    assert "running_images" in text


def test_workload_describe_says_events_expire(fake_backends):
    spec = _registry(fake_backends).get("orbit_workload_describe")
    text = spec.description
    assert "expire" in text
    assert "nothing happened" in text
    assert "events_window_note" not in WorkloadDescribe.model_fields


def test_logs_query_since_description_forbids_compounds(fake_backends):
    fn = _registry(fake_backends).get("orbit_logs_query").fn
    field = _since_field(fn)
    assert field.description == SINCE
    schema = TypeAdapter(Duration).json_schema()
    assert schema["description"] == SINCE
    assert schema["pattern"] == r"^[0-9]+(ms|s|m|h|d|w)$"


@pytest.mark.anyio
@pytest.mark.parametrize("name, args", [
    ("orbit_deploy_status", {"project": "demo", "env": "prod"}),
    ("orbit_workload_describe", {"project": "demo", "env": "prod", "component": "web"}),
    ("orbit_workload_list", {"project": "demo", "env": "prod"}),
    ("orbit_alerts_active", {}),
])
async def test_now_is_rfc3339_server_clock(fake_backends, http, name, args):
    seed_catalog(fake_backends, [DEMO])
    kube = fake_backends.backends.kube
    kube.add_pod(namespace="demo-prod", name="web-0", component="web",
                 labels=labels("demo", "web"))
    kube.add_deployment(namespace="demo-prod", name="demo-web")
    r = await call(http, fake_backends, name, args)
    assert not r.is_error, (name, r.content)
    assert_rfc3339_now(r.structured_content["now"])


def test_alerts_active_now_on_direct_invoke(fake_backends):
    result = invoke(fake_backends, "orbit_alerts_active")
    assert isinstance(result, Listing)
    assert_rfc3339_now(result.now)
