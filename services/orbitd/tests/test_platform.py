"""orbit platform plan: render platform Applications and diff against live."""

from __future__ import annotations

import json
import shutil
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from orbitd.platform import (
    EXIT_DRIFT, EXIT_EMPTY, EXIT_RENDER, RenderError, cmd_plan, helm_argv,
    load_applications, load_live_from, load_yaml_documents, main, plan,
    render_application, resolve_kubeconfig,
)
from orbitd.shadow import compare_objects
from orbitd.testing import FakeKube

FIXTURES = Path(__file__).resolve().parent / "fixtures"
PLATFORM = FIXTURES / "platform"
HELM = shutil.which("helm")

CHART_A_KEYS = {
    ("ConfigMap", "proj-a", "chart-a"),
    ("Namespace", "", "proj-a"),
}
DIR_A_KEYS = {
    ("ClusterRole", "", "chart-a-role"),
    ("ConfigMap", "proj-a", "keep-cm"),
    ("CustomResourceDefinition", "", "widgets.example.io"),
    ("Namespace", "", "proj-a"),
    ("StatefulSet", "proj-a", "sts-a"),
    ("ValidatingWebhookConfiguration", "", "chart-a-webhook"),
}
SKIP_A_KEYS = {
    ("ConfigMap", "proj-a", "skip-a"),
    ("Namespace", "", "proj-a"),
}


def _keys(objects: list[dict]) -> set[tuple[str, str, str]]:
    out = set()
    for obj in objects:
        meta = obj.get("metadata") or {}
        out.add((
            str(obj.get("kind") or ""),
            str(meta.get("namespace") or ""),
            str(meta.get("name") or ""),
        ))
    return out


def _load_live(name: str) -> list[dict]:
    return json.loads((FIXTURES / f"{name}-live.json").read_text())


def _kube_with_live(*names: str) -> FakeKube:
    kube = FakeKube()
    for name in names:
        for obj in _load_live(name):
            kube.add_object(deepcopy(obj))
    return kube


def _plan_args(instance_dir, *, app=None, summary=False, kube=None, kubeconfig=None,
               render_only=None, live_from=None):
    return SimpleNamespace(
        instance_dir=str(instance_dir),
        app=app,
        summary=summary,
        kube=kube,
        kubeconfig=kubeconfig,
        render_only=render_only,
        live_from=live_from,
    )


@pytest.mark.skipif(not HELM, reason="helm not on PATH")
def test_render_chart_and_directory_counts_and_namespaces():
    apps = load_applications(PLATFORM)
    names = [a["metadata"]["name"] for a in apps]
    assert names == ["chart-a", "dir-a", "skip-a"]
    by_name = {n: next(a for a in apps if a["metadata"]["name"] == n) for n in names}
    chart_objs = render_application(PLATFORM, by_name["chart-a"])
    dir_objs = render_application(PLATFORM, by_name["dir-a"])
    skip_objs = render_application(PLATFORM, by_name["skip-a"])
    assert _keys(chart_objs) == CHART_A_KEYS
    assert _keys(dir_objs) == DIR_A_KEYS
    assert _keys(skip_objs) == SKIP_A_KEYS
    assert ("CustomResourceDefinition", "", "skipwidgets.example.io") not in _keys(skip_objs)
    for obj in chart_objs + dir_objs:
        kind = obj.get("kind")
        ns = (obj.get("metadata") or {}).get("namespace") or ""
        if kind in {"Namespace", "ClusterRole", "CustomResourceDefinition",
                    "ValidatingWebhookConfiguration"}:
            assert ns == ""
        else:
            assert ns == "proj-a"


@pytest.mark.skipif(not HELM, reason="helm not on PATH")
def test_directory_exclude_honoured():
    app = next(a for a in load_applications(PLATFORM) if a["metadata"]["name"] == "dir-a")
    names = {(o.get("metadata") or {}).get("name") for o in render_application(PLATFORM, app)}
    assert "hidden-cm" not in names
    assert "nested-app" not in names
    assert "keep-cm" in names


@pytest.mark.skipif(not HELM, reason="helm not on PATH")
def test_compare_live_fixtures_empty():
    apps = {a["metadata"]["name"]: a for a in load_applications(PLATFORM)}
    seen = []
    for path in sorted(FIXTURES.glob("*-live.json")):
        name = path.name.removesuffix("-live.json")
        if name not in apps:
            continue
        seen.append(name)
        rendered = render_application(PLATFORM, apps[name])
        live = json.loads(path.read_text())
        report = compare_objects(rendered, live, project=name)
        assert report.empty is True, (
            f"{name} changed={[(c.kind, c.name, c.paths) for c in report.changed]} "
            f"missing={[(m.kind, m.name) for m in report.missing]} "
            f"extra={[(e.kind, e.name) for e in report.extra]}"
        )
        assert not report.changed and not report.missing and not report.extra
        if name == "dir-a":
            assert [(o.kind, o.name) for o in report.owned] == [
                ("PersistentVolumeClaim", "data-sts-a-0"),
            ]
            assert [(h.kind, h.name) for h in report.hooks] == [("Job", "hook-job")]
    assert seen == ["chart-a", "dir-a", "skip-a"]


@pytest.mark.skipif(not HELM, reason="helm not on PATH")
def test_plan_exit_0_when_empty_and_does_not_mutate():
    kube = _kube_with_live("chart-a", "dir-a", "skip-a")
    code, body = plan(PLATFORM, kube)
    assert code == EXIT_EMPTY
    assert body["empty"] is True
    assert [row["name"] for row in body["apps"]] == ["chart-a", "dir-a", "skip-a"]
    for row in body["apps"]:
        assert "generated" in row
        assert row["generated"] == []
        assert "hooks" in row
        assert "owned" in row
    assert kube.mutations == []
    assert kube.patches == []
    verbs = {c.get("verb") for c in kube.mutations}
    assert not verbs
    for call in kube.dynamic_calls:
        assert call.get("verb") not in {
            "create", "replace", "patch", "delete", "apply", "server_side_apply",
        }


@pytest.mark.skipif(not HELM, reason="helm not on PATH")
def test_plan_exit_1_when_changed_or_extra():
    kube = _kube_with_live("chart-a", "dir-a", "skip-a")
    for obj in kube.objects:
        if obj.get("kind") == "ConfigMap" and (obj.get("metadata") or {}).get("name") == "chart-a":
            obj.setdefault("data", {})["image"] = "example/a:9"
    code, body = plan(PLATFORM, kube)
    assert code == EXIT_DRIFT
    assert body["empty"] is False
    chart = next(row for row in body["apps"] if row["name"] == "chart-a")
    assert chart["empty"] is False
    assert chart["changed"]
    assert any("/data/image" in c["paths"] for c in chart["changed"])
    assert kube.mutations == []


@pytest.mark.skipif(not HELM, reason="helm not on PATH")
def test_plan_reports_labelled_extra():
    kube = _kube_with_live("chart-a", "dir-a")
    kube.add_object({
        "apiVersion": "v1",
        "kind": "Service",
        "metadata": {
            "name": "stray",
            "namespace": "proj-a",
            "labels": {"app.kubernetes.io/instance": "chart-a"},
        },
        "spec": {"ports": [{"port": 80}]},
    })
    kube.add_object({
        "apiVersion": "v1",
        "kind": "Service",
        "metadata": {
            "name": "stray-argocd",
            "namespace": "proj-a",
            "labels": {"argocd.argoproj.io/instance": "chart-a"},
        },
        "spec": {"ports": [{"port": 80}]},
    })
    code, body = plan(PLATFORM, kube, app="chart-a")
    assert code == EXIT_DRIFT
    extras = {(e["kind"], e["name"]) for e in body["apps"][0]["extra"]}
    assert ("Service", "stray") in extras
    assert ("Service", "stray-argocd") in extras
    assert kube.mutations == []


def test_plan_exit_2_on_render_failure_names_app(tmp_path, capsys):
    apps = tmp_path / "clusters" / "platform" / "apps"
    apps.mkdir(parents=True)
    (apps / "broken.yaml").write_text(yaml.safe_dump({
        "apiVersion": "argoproj.io/v1alpha1",
        "kind": "Application",
        "metadata": {"name": "chart-a"},
        "spec": {
            "source": {
                "chart": "no-such-chart",
                "targetRevision": "0.0.0",
            },
            "destination": {"namespace": "proj-a"},
        },
    }))
    kube = FakeKube()
    with pytest.raises(RenderError) as caught:
        plan(tmp_path, kube)
    assert caught.value.app == "chart-a"
    rc = cmd_plan(_plan_args(tmp_path, kube=kube))
    err = capsys.readouterr().err
    assert rc == EXIT_RENDER
    assert "chart-a" in err
    assert kube.mutations == []


def test_platform_plan_lists_statefulset_daemonset_pdb():
    kube = _kube_with_live("dir-a")
    plan(PLATFORM, kube, app="dir-a")
    listed = {c["kind"] for c in kube.dynamic_calls if c.get("name") is None}
    assert "StatefulSet" in listed
    assert "DaemonSet" in listed
    assert "PodDisruptionBudget" in listed


def test_plan_unknown_app_exits_2(capsys):
    kube = FakeKube()
    rc = cmd_plan(_plan_args(PLATFORM, app="missing-app", kube=kube))
    err = capsys.readouterr().err
    assert rc == EXIT_RENDER
    assert "missing-app" in err


@pytest.mark.skipif(not HELM, reason="helm not on PATH")
def test_plan_app_filter_and_summary(capsys):
    kube = _kube_with_live("chart-a")
    rc = cmd_plan(_plan_args(PLATFORM, app="chart-a", summary=True, kube=kube))
    out, err = capsys.readouterr()
    assert rc == EXIT_EMPTY
    body = json.loads(out)
    assert [row["name"] for row in body["apps"]] == ["chart-a"]
    assert "chart-a empty=true" in err
    assert "dir-a" not in err
    assert kube.mutations == []


@pytest.mark.skipif(not HELM, reason="helm not on PATH")
def test_cli_platform_plan_json(capsys, monkeypatch):
    kube = _kube_with_live("chart-a", "dir-a", "skip-a")
    monkeypatch.setattr("orbitd.platform.Kube", lambda kubeconfig=None: kube)
    rc = main(["plan", str(PLATFORM)])
    out = capsys.readouterr().out
    assert rc == EXIT_EMPTY
    body = json.loads(out)
    assert body["empty"] is True
    assert kube.mutations == []


def test_helm_argv_https_uses_repo_flag(tmp_path):
    values = tmp_path / "values.yaml"
    values.write_text("{}\n")
    argv = helm_argv(
        "alloy",
        {
            "chart": "alloy",
            "repoURL": "https://grafana.github.io/helm-charts",
            "targetRevision": "1.12.0",
        },
        root=tmp_path,
        values_path=values,
        namespace="alloy",
    )
    assert argv[:4] == ["helm", "template", "alloy", "alloy"]
    assert "--repo" in argv
    assert argv[argv.index("--repo") + 1] == "https://grafana.github.io/helm-charts"
    assert "--include-crds" in argv
    assert "--no-hooks" in argv
    assert "--version" in argv and "1.12.0" in argv
    assert "--namespace" in argv and "alloy" in argv


def test_helm_argv_oci_passthrough(tmp_path):
    values = tmp_path / "values.yaml"
    values.write_text("{}\n")
    argv = helm_argv(
        "chart-a",
        {
            "chart": "chart-a",
            "repoURL": "oci://ghcr.io/example/charts",
            "targetRevision": "0.1.0",
        },
        root=tmp_path,
        values_path=values,
        namespace="proj-a",
    )
    assert argv[3] == "oci://ghcr.io/example/charts/chart-a"
    assert "--repo" not in argv
    assert "--include-crds" in argv
    assert "--no-hooks" in argv


def test_helm_argv_oci_scheme_added(tmp_path):
    values = tmp_path / "values.yaml"
    values.write_text("{}\n")
    argv = helm_argv(
        "chart-a",
        {"chart": "chart-a", "repoURL": "ghcr.io/example/charts", "targetRevision": "0.1.0"},
        root=tmp_path,
        values_path=values,
        namespace="proj-a",
    )
    assert argv[3] == "oci://ghcr.io/example/charts/chart-a"
    assert "--repo" not in argv


def test_helm_argv_local_chart_absolute(tmp_path):
    values = tmp_path / "values.yaml"
    values.write_text("{}\n")
    argv = helm_argv(
        "chart-a",
        {"chart": "chart-a", "repoURL": "charts", "targetRevision": "0.1.0"},
        root=PLATFORM,
        values_path=values,
        namespace="proj-a",
    )
    chart = Path(argv[3])
    assert chart.is_absolute()
    assert (chart / "Chart.yaml").is_file()
    assert "--repo" not in argv
    assert "--include-crds" in argv
    assert "--no-hooks" in argv


def test_helm_argv_skip_crds_omits_include_crds(tmp_path):
    values = tmp_path / "values.yaml"
    values.write_text("{}\n")
    argv = helm_argv(
        "traefik",
        {
            "chart": "traefik",
            "repoURL": "https://traefik.github.io/charts",
            "targetRevision": "37.1.1",
            "helm": {"skipCrds": True},
        },
        root=tmp_path,
        values_path=values,
        namespace="traefik",
    )
    assert "--include-crds" not in argv
    assert "--no-hooks" in argv
    argv_on = helm_argv(
        "traefik",
        {"chart": "traefik", "repoURL": "https://traefik.github.io/charts",
         "helm": {"skipCrds": False}},
        root=tmp_path,
        values_path=values,
        namespace="traefik",
    )
    assert "--include-crds" in argv_on


@pytest.mark.skipif(not HELM, reason="helm not on PATH")
def test_value_files_relative_to_chart_are_applied():
    apps = {a["metadata"]["name"]: a for a in load_applications(PLATFORM)}
    app = deepcopy(apps["chart-a"])
    app["spec"]["source"]["helm"]["valueFiles"] = ["values-extra.yaml"]
    objects = render_application(PLATFORM, app)
    cms = [o for o in objects if o.get("kind") == "ConfigMap"]
    assert cms
    assert cms[0]["data"]["image"] == "example/a:from-file"


def test_crd_and_webhook_server_stamps_are_ignored():
    rendered = {
        "apiVersion": "apiextensions.k8s.io/v1",
        "kind": "CustomResourceDefinition",
        "metadata": {"name": "widgets.example.io"},
        "spec": {"group": "example.io"},
    }
    live = deepcopy(rendered)
    live["metadata"]["annotations"] = {
        "controller-gen.kubebuilder.io/version": "v0.16.0",
    }
    live["spec"]["conversion"] = {"strategy": "None"}
    live["status"] = {"storedVersions": ["v1"]}
    report = compare_objects([rendered], [live])
    assert report.empty is True

    hook = {
        "apiVersion": "admissionregistration.k8s.io/v1",
        "kind": "ValidatingWebhookConfiguration",
        "metadata": {
            "name": "hook",
            "annotations": {"cert-manager.io/inject-ca-from": "ns/cert"},
        },
        "webhooks": [{"name": "h.example.io", "clientConfig": {"service": {"name": "s"}}}],
    }
    live_hook = deepcopy(hook)
    live_hook["webhooks"][0]["clientConfig"]["caBundle"] = "Y2VydA=="
    live_hook["webhooks"][0]["clientConfig"]["service"]["port"] = 443
    live_hook["webhooks"][0]["matchPolicy"] = "Equivalent"
    live_hook["webhooks"][0]["timeoutSeconds"] = 10
    report = compare_objects([hook], [live_hook])
    assert report.empty is True

    mutating = {
        "apiVersion": "admissionregistration.k8s.io/v1",
        "kind": "MutatingWebhookConfiguration",
        "metadata": {
            "name": "mutate",
            "annotations": {"cert-manager.io/inject-ca-from": "ns/cert"},
        },
        "webhooks": [{"name": "m.example.io", "clientConfig": {"service": {"name": "s"}},
                      "rules": [{"apiGroups": ["example.io"]}]}],
    }
    live_mut = deepcopy(mutating)
    live_mut["webhooks"][0]["clientConfig"]["caBundle"] = "Y2VydA=="
    live_mut["webhooks"][0]["matchPolicy"] = "Equivalent"
    live_mut["webhooks"][0]["reinvocationPolicy"] = "Never"
    live_mut["webhooks"][0]["timeoutSeconds"] = 10
    live_mut["webhooks"][0]["rules"][0]["scope"] = "*"
    live_mut["webhooks"][0]["clientConfig"]["service"]["port"] = 443
    report = compare_objects([mutating], [live_mut])
    assert report.empty is True


def test_clusterrole_no_unexpected_defaults():
    rendered = {
        "apiVersion": "rbac.authorization.k8s.io/v1",
        "kind": "ClusterRole",
        "metadata": {"name": "chart-a-role"},
        "rules": [{"apiGroups": [""], "resources": ["configmaps"], "verbs": ["get"]}],
    }
    live = deepcopy(rendered)
    live["metadata"]["uid"] = "u"
    live["metadata"]["resourceVersion"] = "1"
    live["status"] = {}
    report = compare_objects([rendered], [live])
    assert report.empty is True


def test_value_tag_loads_as_string():
    docs = load_yaml_documents("items:\n- =\nkey: =\n")
    assert docs == [{"items": ["="], "key": "="}]


def test_resolve_kubeconfig_flag_then_orbitd_then_kubeconfig(monkeypatch):
    monkeypatch.delenv("ORBITD_KUBECONFIG", raising=False)
    monkeypatch.delenv("KUBECONFIG", raising=False)
    assert resolve_kubeconfig(None) is None
    assert resolve_kubeconfig("  ") is None
    monkeypatch.setenv("KUBECONFIG", "/from-kube")
    monkeypatch.setenv("ORBITD_KUBECONFIG", "/from-orbitd")
    assert resolve_kubeconfig("/flag") == "/flag"
    assert resolve_kubeconfig(None) == "/from-orbitd"
    monkeypatch.delenv("ORBITD_KUBECONFIG")
    assert resolve_kubeconfig(None) == "/from-kube"
    monkeypatch.delenv("KUBECONFIG")
    assert resolve_kubeconfig(None) is None


def test_render_only_writes_json_and_does_not_construct_kube(tmp_path, monkeypatch, capsys):
    def boom(*_a, **_k):
        raise AssertionError("Kube must not be constructed for --render-only")
    monkeypatch.setattr("orbitd.platform.Kube", boom)
    out = tmp_path / "out.json"
    rc = cmd_plan(_plan_args(PLATFORM, app="dir-a", render_only=str(out), summary=True))
    err = capsys.readouterr().err
    assert rc == EXIT_EMPTY
    objects = json.loads(out.read_text())
    assert isinstance(objects, list)
    assert _keys(objects) == DIR_A_KEYS
    assert "dir-a rendered=" in err
    assert "chart-a" not in err
    out2 = tmp_path / "out2.json"
    rc = main(["plan", str(PLATFORM), "--render-only", str(out2), "--app", "dir-a"])
    assert rc == EXIT_EMPTY
    assert _keys(json.loads(out2.read_text())) == DIR_A_KEYS


def test_render_only_exit_2_names_app_without_kube(tmp_path, monkeypatch, capsys):
    def boom(*_a, **_k):
        raise AssertionError("Kube must not be constructed for --render-only")
    monkeypatch.setattr("orbitd.platform.Kube", boom)
    apps = tmp_path / "clusters" / "platform" / "apps"
    apps.mkdir(parents=True)
    (apps / "broken.yaml").write_text(yaml.safe_dump({
        "apiVersion": "argoproj.io/v1alpha1",
        "kind": "Application",
        "metadata": {"name": "chart-a"},
        "spec": {
            "source": {"chart": "no-such-chart", "targetRevision": "0.0.0"},
            "destination": {"namespace": "proj-a"},
        },
    }))
    out = tmp_path / "out.json"
    rc = cmd_plan(_plan_args(tmp_path, render_only=str(out)))
    err = capsys.readouterr().err
    assert rc == EXIT_RENDER
    assert "chart-a" in err
    assert not out.exists()


def test_plan_honours_kubeconfig_env_when_flag_absent(monkeypatch):
    seen: dict[str, str | None] = {}
    kube = FakeKube()

    def fake_kube(kubeconfig=None):
        seen["kubeconfig"] = kubeconfig
        return kube

    monkeypatch.setattr("orbitd.platform.Kube", fake_kube)
    monkeypatch.setenv("ORBITD_KUBECONFIG", "/from-orbitd")
    monkeypatch.setenv("KUBECONFIG", "/from-kube")
    rc = main(["plan", str(PLATFORM), "--app", "dir-a"])
    assert rc in {EXIT_EMPTY, EXIT_DRIFT}
    assert seen["kubeconfig"] == "/from-orbitd"
    monkeypatch.delenv("ORBITD_KUBECONFIG")
    rc = main(["plan", str(PLATFORM), "--app", "dir-a"])
    assert seen["kubeconfig"] == "/from-kube"
    rc = main(["plan", str(PLATFORM), "--app", "dir-a", "--kubeconfig", "/from-flag"])
    assert seen["kubeconfig"] == "/from-flag"


@pytest.mark.anyio
async def test_orbit_cli_help_includes_platform(capsys):
    from orbitd.client import async_main
    assert await async_main(["platform", "--help"]) == 0
    assert await async_main(["platform", "plan", "--help"]) == 0
    help_text = capsys.readouterr().out
    assert "--render-only" in help_text
    assert "--live-from" in help_text
    assert "--kubeconfig" in help_text


@pytest.mark.skipif(not HELM, reason="helm not on PATH")
def test_live_from_replays_without_kube(tmp_path, monkeypatch, capsys):
    def boom(*_a, **_k):
        raise AssertionError("Kube must not be constructed for --live-from")
    monkeypatch.setattr("orbitd.platform.Kube", boom)
    saved = {
        "apps": [
            {"name": "dir-a", "live": _load_live("dir-a")},
        ],
    }
    plan_path = tmp_path / "plan.json"
    plan_path.write_text(json.dumps(saved))
    rc = cmd_plan(_plan_args(
        PLATFORM, app="dir-a", live_from=str(plan_path), summary=True,
    ))
    err = capsys.readouterr().err
    assert rc == EXIT_EMPTY
    assert "dir-a empty=true" in err
    loaded = load_live_from(plan_path)
    assert "dir-a" in loaded
    assert loaded["dir-a"]


def test_live_from_missing_live_objects_exits_2(tmp_path, monkeypatch, capsys):
    def boom(*_a, **_k):
        raise AssertionError("Kube must not be constructed for --live-from")
    monkeypatch.setattr("orbitd.platform.Kube", boom)
    plan_path = tmp_path / "plan.json"
    plan_path.write_text(json.dumps({
        "apps": [{"name": "dir-a", "rendered": [{"kind": "ConfigMap", "name": "x"}]}],
    }))
    rc = cmd_plan(_plan_args(PLATFORM, app="dir-a", live_from=str(plan_path)))
    err = capsys.readouterr().err
    assert rc == EXIT_RENDER
    assert "live objects" in err


# ------------------------------------------------------------- the apps are planned side by side

@pytest.mark.skipif(not HELM, reason="helm not on PATH")
def test_a_plan_side_by_side_is_the_plan_one_after_another(monkeypatch):
    """Same report, same order, whatever the number of workers."""
    from orbitd import platform as platform_module

    monkeypatch.setattr(platform_module, "PLAN_WORKERS", 1)
    one_code, one_body = plan(PLATFORM, _kube_with_live("chart-a", "dir-a"))
    monkeypatch.setattr(platform_module, "PLAN_WORKERS", 6)
    many_code, many_body = plan(PLATFORM, _kube_with_live("chart-a", "dir-a"))
    assert (many_code, many_body) == (one_code, one_body)
    assert [row["name"] for row in many_body["apps"]] == ["chart-a", "dir-a", "skip-a"]


def test_apps_really_are_planned_at_the_same_time_and_reported_in_declared_order(monkeypatch):
    """Three apps whose render each waits for the other two to have started: one after
    another this would never finish; the report is still in declared order."""
    import threading

    from orbitd import platform as platform_module

    names = ["first", "second", "third"]
    started = threading.Barrier(len(names), timeout=20)
    monkeypatch.setattr(platform_module, "PLAN_WORKERS", 6)
    monkeypatch.setattr(platform_module, "load_applications",
                        lambda _dir, _app=None: [{"metadata": {"name": n}} for n in names])

    def render(_dir, application):
        started.wait()
        return []

    monkeypatch.setattr(platform_module, "render_application", render)
    monkeypatch.setattr(platform_module, "read_live", lambda *a, **k: [])
    code, body = plan(PLATFORM, object())
    assert [row["name"] for row in body["apps"]] == names
    assert code == EXIT_EMPTY


def test_the_failure_reported_is_the_first_in_declared_order(monkeypatch):
    """A later app that fails sooner does not take the first failing app's place."""
    import time

    from orbitd import platform as platform_module
    from orbitd.platform import RenderError

    monkeypatch.setattr(platform_module, "PLAN_WORKERS", 6)
    monkeypatch.setattr(platform_module, "load_applications",
                        lambda _dir, _app=None: [{"metadata": {"name": n}} for n in ("ok", "slow-bad", "fast-bad")])

    def render(_dir, application):
        name = application["metadata"]["name"]
        if name == "slow-bad":
            time.sleep(0.5)
            raise RenderError(name, "does not render")
        if name == "fast-bad":
            raise RenderError(name, "does not render either")
        return []

    monkeypatch.setattr(platform_module, "render_application", render)
    monkeypatch.setattr(platform_module, "read_live", lambda *a, **k: [])
    with pytest.raises(RenderError) as caught:
        plan(PLATFORM, object())
    assert caught.value.app == "slow-bad"


def test_each_worker_plans_with_a_cluster_client_of_its_own(monkeypatch):
    """One client shared by the workers broke the plan on the minimal profile: the
    library's discovery cache is not safe under concurrent lookups (docs/V1.md §8)."""
    import threading

    from orbitd import platform as platform_module
    from orbitd.backends import Kube

    names = ["first", "second", "third"]
    started = threading.Barrier(len(names), timeout=20)
    seen: dict[str, int] = {}
    monkeypatch.setattr(platform_module, "PLAN_WORKERS", 6)
    monkeypatch.setattr(platform_module, "load_applications",
                        lambda _dir, _app=None: [{"metadata": {"name": n}} for n in names])
    monkeypatch.setattr(platform_module, "render_application", lambda _dir, _app: (started.wait(), [])[1])

    def read_live(state, objects, name, *a, **k):
        seen[name] = id(state.backends.kube)
        return []

    monkeypatch.setattr(platform_module, "read_live", read_live)
    shared = Kube("/nonexistent/kubeconfig")          # never loaded: read_live is a stand-in
    code, body = plan(PLATFORM, shared)
    assert [row["name"] for row in body["apps"]] == names
    assert len(set(seen.values())) == 3 and id(shared) not in seen.values()

