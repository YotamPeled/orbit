"""Shadow render-and-diff: helm render on the fixture instance, fake live, streak, runner."""

from __future__ import annotations

import json
import shutil
import threading
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
import yaml
from conftest import mint, sdk_client

from orbitd.render import render
from orbitd.roles import Role
from orbitd.shadow import (
    FIRST_DELAY_SECONDS, INTERVAL_SECONDS, DailyRunner, ShadowReport,
    diff, state_dir_of, streak_days,
)
from orbitd.testing import FakeClock

REPO = Path(__file__).resolve().parents[3]
CHART = REPO / "charts" / "service"
HELM = shutil.which("helm")

DEMO = {
    "id": "demo",
    "name": "Demo",
    "environments": {"prod": {"branch": "main"}, "dev": {"branch": "dev", "enabled": True}},
    "components": [{"name": "web", "kind": "web", "image": "example/web"}],
}

VALUES = {
    "project": "demo",
    "env": "prod",
    "hostnames": ["demo.example.org"],
    "components": [{
        "name": "web",
        "kind": "web",
        "image": "example/web",
        "port": 8080,
        "path": "/",
        "probePath": "/healthz",
        "replicas": 1,
    }],
}

EXTRA_CM = {
    "apiVersion": "v1",
    "kind": "ConfigMap",
    "metadata": {"name": "extra-cm", "namespace": "demo-prod"},
    "data": {"k": "v"},
}


def _application(project: str, env: str, values: dict) -> dict:
    ns = f"{project}-{env}"
    return {
        "apiVersion": "argoproj.io/v1alpha1",
        "kind": "Application",
        "metadata": {"name": ns, "namespace": "argocd"},
        "spec": {
            "source": {"path": "charts/service", "helm": {"valuesObject": values}},
            "destination": {"server": "https://kubernetes.default.svc", "namespace": ns},
        },
    }


def seed_instance(state, *, project: str = "demo", env: str = "prod") -> Path:
    root = state.backends.instance_root
    (root / "catalog" / "projects.yaml").write_text(
        yaml.safe_dump({"version": 3, "projects": [DEMO]}, sort_keys=False))
    dest_chart = root / "charts" / "service"
    if dest_chart.exists():
        shutil.rmtree(dest_chart)
    shutil.copytree(CHART, dest_chart)
    apps = root / "clusters" / "apps"
    apps.mkdir(parents=True, exist_ok=True)
    docs = [_application(project, env, VALUES), EXTRA_CM]
    (apps / f"{project}-{env}.yaml").write_text(yaml.safe_dump_all(docs, sort_keys=False))
    return root


def kinds_of(objects: list[dict]) -> list[str]:
    return [o.get("kind") for o in objects]


@pytest.fixture
def world(fake_backends):
    seed_instance(fake_backends)
    return fake_backends


@pytest.mark.skipif(not HELM, reason="helm not on PATH")
def test_render_on_fixture_produces_expected_kinds_and_is_deterministic(world):
    objects, sha = render(world, "demo", "prod")
    assert kinds_of(objects) == ["ConfigMap", "Deployment", "Ingress", "Service", "ServiceAccount"]
    names = {(o["kind"], (o.get("metadata") or {}).get("name")) for o in objects}
    assert ("Deployment", "demo-web") in names
    assert ("Ingress", "demo") in names
    assert ("Service", "demo-web") in names
    assert ("ServiceAccount", "demo") in names
    assert ("ConfigMap", "extra-cm") in names
    assert all((o.get("metadata") or {}).get("namespace") == "demo-prod"
               or o.get("kind") == "Namespace" for o in objects)
    again, sha2 = render(world, "demo", "prod")
    assert sha == sha2 and len(sha) == 64
    assert kinds_of(again) == kinds_of(objects)


@pytest.mark.skipif(not HELM, reason="helm not on PATH")
def test_diff_identical_live_is_empty(world):
    objects, _ = render(world, "demo", "prod")
    kube = world.backends.kube
    for obj in objects:
        live = deepcopy(obj)
        live.setdefault("metadata", {})["uid"] = "live-uid"
        live["metadata"]["resourceVersion"] = "9"
        live["metadata"]["generation"] = 3
        live["metadata"]["creationTimestamp"] = "2026-01-01T00:00:00Z"
        live["status"] = {"observedGeneration": 3}
        ann = live["metadata"].setdefault("annotations", {})
        ann["argocd.argoproj.io/tracking-id"] = "x"
        ann["kubectl.kubernetes.io/last-applied-configuration"] = "{}"
        kube.add_object(live)
    report = diff(world, "demo", "prod")
    assert report.empty is True
    assert report.missing == [] and report.extra == [] and report.changed == []
    assert report.render_sha
    assert report.streak_days == 1
    assert any(c.get("_request_timeout") is not None for c in kube.dynamic_calls)
    stored = Path(report.path)
    assert stored.is_file()
    body = json.loads(stored.read_text())
    assert body["empty"] is True


@pytest.mark.skipif(not HELM, reason="helm not on PATH")
def test_diff_changed_replica_count_names_the_path(world):
    objects, _ = render(world, "demo", "prod")
    kube = world.backends.kube
    for obj in objects:
        live = deepcopy(obj)
        if live.get("kind") == "Deployment":
            live.setdefault("spec", {})["replicas"] = 3
        kube.add_object(live)
    report = diff(world, "demo", "prod")
    assert report.empty is False
    assert report.missing == [] and report.extra == []
    assert len(report.changed) == 1
    change = report.changed[0]
    assert change.kind == "Deployment" and change.name == "demo-web"
    assert "/spec/replicas" in change.paths
    replica = next(d for d in change.diffs if d.path == "/spec/replicas")
    # the rendered side is whatever the chart rendered (an untagged fixture renders 0
    # replicas since the chart idles untagged components), never a hard-coded number
    rendered_replicas = next(o for o in objects if o.get("kind") == "Deployment")["spec"]["replicas"]
    assert replica.rendered == str(rendered_replicas)
    assert replica.live == "3" and replica.rendered != replica.live


@pytest.mark.skipif(not HELM, reason="helm not on PATH")
def test_diff_extra_live_service_is_extra(world):
    objects, _ = render(world, "demo", "prod")
    kube = world.backends.kube
    for obj in objects:
        kube.add_object(deepcopy(obj))
    kube.add_object({
        "apiVersion": "v1",
        "kind": "Service",
        "metadata": {
            "name": "stray",
            "namespace": "demo-prod",
            "labels": {"app.kubernetes.io/instance": "demo"},
        },
        "spec": {"ports": [{"port": 80}]},
    })
    report = diff(world, "demo", "prod")
    assert report.empty is False
    assert [e.name for e in report.extra] == ["stray"]
    assert report.extra[0].kind == "Service"


@pytest.mark.skipif(not HELM, reason="helm not on PATH")
def test_diff_missing_ingress_is_missing(world):
    objects, _ = render(world, "demo", "prod")
    kube = world.backends.kube
    for obj in objects:
        if obj.get("kind") == "Ingress":
            continue
        kube.add_object(deepcopy(obj))
    report = diff(world, "demo", "prod")
    assert report.empty is False
    assert [m.kind for m in report.missing] == ["Ingress"]
    assert report.missing[0].name == "demo"


def test_streak_over_stored_reports(fake_backends):
    clock = FakeClock()
    today = datetime.fromtimestamp(clock.now(), tz=timezone.utc).date()
    directory = state_dir_of(fake_backends) / "shadow" / "demo-prod"
    directory.mkdir(parents=True)
    for i in range(7):
        day = today - timedelta(days=i)
        (directory / f"{day.isoformat()}.json").write_text(json.dumps({"empty": True}))
    assert streak_days(fake_backends, "demo", "prod", clock=clock) == 7
    (directory / f"{(today - timedelta(days=2)).isoformat()}.json").write_text(
        json.dumps({"empty": False}))
    assert streak_days(fake_backends, "demo", "prod", clock=clock) == 2
    (directory / f"{today.isoformat()}.json").unlink()
    assert streak_days(fake_backends, "demo", "prod", clock=clock) == 0


def test_daily_runner_uses_injectable_clock(fake_backends):
    seed_instance(fake_backends)
    clock = FakeClock()
    stop = threading.Event()
    calls: list[tuple[str, str]] = []

    def fake_diff(state, project, env, clock=None):
        calls.append((project, env))
        return ShadowReport(project=project, env=env, empty=True)

    def sleep(seconds):
        clock.sleeps.append(seconds)
        clock.t += seconds
        if len(clock.sleeps) >= 2:
            stop.set()

    clock.sleep = sleep  # type: ignore[method-assign]
    runner = DailyRunner(
        fake_backends, clock=clock, stop_event=stop, differ=fake_diff,
        first_delay=FIRST_DELAY_SECONDS, interval=INTERVAL_SECONDS,
    )
    runner.loop()
    assert clock.sleeps == [FIRST_DELAY_SECONDS, INTERVAL_SECONDS]
    assert ("demo", "prod") in calls
    assert ("demo", "dev") in calls
    assert len(runner.runs) == 1


@pytest.mark.anyio
@pytest.mark.skipif(not HELM, reason="helm not on PATH")
async def test_orbit_platform_diff_is_admin_only_and_returns_report(world, http):
    objects, _ = render(world, "demo", "prod")
    for obj in objects:
        world.backends.kube.add_object(deepcopy(obj))
    async with sdk_client(http, mint(world, Role.viewer)) as client:
        listed = await client.list_tools()
    assert "orbit_platform_diff" not in {t.name for t in listed.tools}
    async with sdk_client(http, mint(world, Role.admin)) as client:
        result = await client.call_tool(
            "orbit_platform_diff", {"project": "demo", "env": "prod"},
        )
    assert not result.is_error, result.content
    body = result.structured_content
    assert body["returned"] == 1
    item = body["items"][0]
    assert item["empty"] is True
    assert item["render_sha"]
    assert item["streak_days"] >= 1
    assert item["path"]
    assert item["report_path"] == item["path"]
    assert item["date"]
    assert "shadow/demo-prod/" in item["path"].replace("\\", "/")
    assert item["date"] in item["path"].replace("\\", "/")
