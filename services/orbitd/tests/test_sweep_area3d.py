"""Area 3d sweep (assertions/platform/monitors/render/server/cli), round-1 repairs.

Each test pins one repair: the plan JSON carries Secret fingerprints, never bodies;
Application fields are shape-checked before they are helm argv or a path; the
alert webhook keeps only monitor-name/RFC 3339 shaped rows; helm is bounded in
time and output; a missing or empty apps tree is a refusal, not an empty plan; a
failing live read is one scrubbed line and EXIT_RENDER; thresholds are finite and
free-text fields bounded; bootstrap is one atomic write; the deploy runtime is one
per process.
"""

from __future__ import annotations

import base64
import io
import json
import os
import shutil
import stat
import sys
import threading
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from orbitd import alert_log, helm, platform
from orbitd.alert_log import AlertLog
from orbitd.auth import BootstrapDone, Store
from orbitd.backends import BackendError
from orbitd.deploy import runtime_of
from orbitd.monitors import validate
from orbitd.platform import (
    EXIT_DRIFT, EXIT_EMPTY, EXIT_RENDER, RenderError, RenderReadError, cmd_plan, helm_argv,
    load_applications, render_application, secrets_fingerprinted,
)
from orbitd.testing import FakeKube

from test_monitors import CATALOG, metric

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "platform"
MARKER = "PLANTED-MARKER-AREA3D-Q7"
MARKER_B64 = base64.b64encode(MARKER.encode()).decode()


def _plan_args(instance_dir, *, app=None, kube=None, live_from=None):
    return SimpleNamespace(instance_dir=str(instance_dir), app=app, summary=False, kube=kube,
                           kubeconfig=None, render_only=None, live_from=live_from)


def _run_plan(args) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        rc = cmd_plan(args)
    return rc, out.getvalue(), err.getvalue()


# ------------------------------------------------------------ R1 Secret bodies

def _classes_live() -> list[dict]:
    app = next(a for a in load_applications(FIXTURES / "classes") if a["metadata"]["name"] == "classes")
    live = json.loads(json.dumps(render_application(FIXTURES / "classes", app)))
    minted = next(o for o in live if o["kind"] == "Secret" and o["metadata"]["name"] == "minted")
    minted["data"]["password"] = MARKER_B64
    return live


def test_plan_json_carries_secret_fingerprints_never_values():
    kube = FakeKube()
    for obj in _classes_live():
        kube.add_object(obj)
    rc, out, err = _run_plan(_plan_args(FIXTURES / "classes", app="classes", kube=kube))
    assert MARKER_B64 not in out and MARKER not in out and MARKER_B64 not in err
    body = json.loads(out)
    secrets = [o for o in body["apps"][0]["live"] if o.get("kind") == "Secret"]
    assert len(secrets) >= 2, "the live Secrets are still listed (by fingerprint)"
    assert all(v.startswith("sha256:") for s in secrets for v in s["data"].values())
    # the compare itself still ran on the values: the minted key is reported as generated
    gen = {(g["kind"], g["name"]): g["keys"] for g in body["apps"][0]["generated"]}
    assert gen[("Secret", "minted")] == ["password"]


def test_saved_plan_replays_with_fingerprints_and_still_names_secret_drift(tmp_path):
    saved = tmp_path / "plan.json"
    saved.write_text(json.dumps({"apps": [{"name": "classes", "live": secrets_fingerprinted(_classes_live())}]}))
    assert MARKER_B64 not in saved.read_text()
    rc, out, err = _run_plan(_plan_args(FIXTURES / "classes", app="classes", live_from=str(saved)))
    assert rc == EXIT_EMPTY, err  # a generated Secret's drift is listed, not counted
    row = json.loads(out)["apps"][0]
    gen = {(g["kind"], g["name"]): g["keys"] for g in row["generated"]}
    assert gen[("Secret", "minted")] == ["password"]
    # an unchanged Secret compares equal through its fingerprint: no false drift
    changed = {(c["kind"], c["name"]) for c in row["changed"]}
    assert ("Secret", "webhook-tls") not in changed


def _opaque_instance(tmp_path: Path, value: str) -> Path:
    """An instance tree with one directory-source app: a ConfigMap and an Opaque Secret."""
    root = tmp_path / "instance"
    apps = root / "clusters" / "platform" / "apps"
    apps.mkdir(parents=True)
    (root / "manifests").mkdir()
    (apps / "plain.yaml").write_text(yaml.safe_dump({
        "apiVersion": "argoproj.io/v1alpha1", "kind": "Application", "metadata": {"name": "plain"},
        "spec": {"source": {"path": "manifests"}, "destination": {"namespace": "plain-ns"}}}))
    (root / "manifests" / "all.yaml").write_text(yaml.safe_dump_all([
        {"apiVersion": "v1", "kind": "ConfigMap", "metadata": {"name": "cm"}, "data": {"k": "v"}},
        {"apiVersion": "v1", "kind": "Secret", "type": "Opaque", "metadata": {"name": "plain-secret"},
         "data": {"token": value}},
    ]))
    return root


def test_replay_of_an_unchanged_opaque_secret_is_not_drift_and_a_changed_one_is(tmp_path):
    root = _opaque_instance(tmp_path, MARKER_B64)
    app = load_applications(root)[0]
    live = render_application(root, app)
    saved = tmp_path / "plan.json"
    # the saved plan carries fingerprints: an identical Secret compares equal through them
    saved.write_text(json.dumps({"apps": [{"name": "plain", "live": secrets_fingerprinted(live)}]}))
    rc, out, err = _run_plan(_plan_args(root, live_from=str(saved)))
    assert rc == EXIT_EMPTY, err
    assert json.loads(out)["apps"][0]["changed"] == []
    # a plan saved before fingerprinting (bodies in the file) replays the same way, and the
    # body never reaches stdout
    saved.write_text(json.dumps({"apps": [{"name": "plain", "live": live}]}))
    rc, out, err = _run_plan(_plan_args(root, live_from=str(saved)))
    assert rc == EXIT_EMPTY and MARKER_B64 not in out and MARKER not in out, err
    # a different live value is named per key, by fingerprint
    other = json.loads(json.dumps(live))
    next(o for o in other if o["kind"] == "Secret")["data"]["token"] = base64.b64encode(b"other").decode()
    saved.write_text(json.dumps({"apps": [{"name": "plain", "live": secrets_fingerprinted(other)}]}))
    rc, out, err = _run_plan(_plan_args(root, live_from=str(saved)))
    assert rc == EXIT_DRIFT
    changed = {(c["kind"], c["name"]): c["paths"] for c in json.loads(out)["apps"][0]["changed"]}
    assert changed[("Secret", "plain-secret")] == ["/data/token"]


def test_a_live_secret_is_reduced_to_identity_and_fingerprints():
    # a kubectl-applied Secret carries its whole body in the last-applied annotation
    body = {"apiVersion": "v1", "kind": "Secret", "type": "Opaque",
            "metadata": {"name": "s", "namespace": "ns", "labels": {"app": "x"}, "uid": "u",
                         "managedFields": [{"manager": "kubectl"}],
                         "annotations": {"kubectl.kubernetes.io/last-applied-configuration":
                                         json.dumps({"data": {"password": MARKER_B64}})}},
            "data": {"password": MARKER_B64},
            "immutable": True, "status": {"note": MARKER}, "ownerReferences": [{"name": "o"}]}
    (reduced,) = secrets_fingerprinted([body])
    assert MARKER not in json.dumps(reduced)
    assert MARKER_B64 not in json.dumps(reduced)
    assert set(reduced) == {"apiVersion", "kind", "type", "metadata", "data"}
    assert set(reduced["metadata"]) == {"name", "namespace", "labels"}
    kube = FakeKube()
    for obj in _classes_live():
        if obj["kind"] == "Secret":
            obj.setdefault("metadata", {}).setdefault("annotations", {})[
                "kubectl.kubernetes.io/last-applied-configuration"] = json.dumps(obj)
        kube.add_object(obj)
    rc, out, err = _run_plan(_plan_args(FIXTURES / "classes", app="classes", kube=kube))
    assert MARKER_B64 not in out and "last-applied" not in out


def test_a_symlinked_manifest_out_of_the_checkout_is_refused(tmp_path):
    root = _opaque_instance(tmp_path, MARKER_B64)
    outside = tmp_path / "outside.yaml"
    outside.write_text(f"apiVersion: v1\nkind: ConfigMap\nmetadata:\n  name: exfil\ndata:\n  m: {MARKER}\n")
    (root / "manifests" / "leak.yaml").symlink_to(outside)
    app = load_applications(root)[0]
    with pytest.raises(RenderError) as exc:
        render_application(root, app)
    assert "outside the instance checkout" in str(exc.value) and MARKER not in str(exc.value)
    rc, out, err = _run_plan(_plan_args(root, kube=FakeKube()))
    assert rc == EXIT_RENDER and MARKER not in out and MARKER not in err


def test_a_value_file_outside_the_checkout_is_refused(tmp_path):
    root = tmp_path / "instance"
    (root / "c").mkdir(parents=True)
    (root / "c" / "Chart.yaml").write_text("apiVersion: v2\nname: c\nversion: 0.1.0\n")
    outside = tmp_path / "values-outside.yaml"
    outside.write_text("a: 1\n")
    with pytest.raises(RenderError, match="outside the instance checkout"):
        platform._value_file_paths(root, root / "c", {"valueFiles": [str(outside)]}, "app")
    (root / "c" / "inside.yaml").write_text("a: 1\n")
    assert platform._value_file_paths(root, root / "c", {"valueFiles": ["inside.yaml"]}, "app") == [
        (root / "c" / "inside.yaml").resolve()]


def test_run_helm_works_past_the_select_descriptor_limit(tmp_path):
    env = _fake_helm(tmp_path, "echo 'kind: ConfigMap'\n")
    held = [os.open(os.devnull, os.O_RDONLY) for _ in range(1100)]
    try:
        assert "kind: ConfigMap" in helm.run_helm(["helm", "template", "x"], cwd=tmp_path, env=env)
    finally:
        for fd in held:
            os.close(fd)


def test_fingerprinting_is_idempotent_and_folds_string_data():
    secret = {"apiVersion": "v1", "kind": "Secret", "metadata": {"name": "s"},
              "stringData": {"k": MARKER}}
    once = secrets_fingerprinted([secret])
    twice = secrets_fingerprinted(once)
    assert once == twice
    assert "stringData" not in once[0] and once[0]["data"]["k"].startswith("sha256:")
    assert MARKER not in json.dumps(once)


# ------------------------------------------------------------ R2 argv and paths

@pytest.mark.parametrize("release, source, namespace", [
    ("--kube-version=1.99.0", {"chart": "c", "repoURL": "https://charts.example.test"}, "ns"),
    ("rel", {"chart": "--set", "repoURL": "https://charts.example.test"}, "ns"),
    ("rel", {"chart": "c", "repoURL": "https://x.test --kube-version 1.0"}, "ns"),
    ("rel", {"chart": "c", "repoURL": "https://x.test", "targetRevision": "--debug"}, "ns"),
    ("rel", {"chart": "c", "repoURL": "ftp://evil.test"}, "ns"),
    ("rel", {"chart": "c", "repoURL": "https://user:pw@x.test/charts"}, "ns"),
    ("rel", {"chart": "c", "repoURL": "https://x.test"}, "--namespace=kube-system"),
    ("rel", {"chart": "../outside", "repoURL": ""}, "ns"),
])
def test_helm_argv_refuses_fields_that_are_not_of_their_shape(tmp_path, release, source, namespace):
    values = tmp_path / "values.yaml"
    values.write_text("{}\n")
    with pytest.raises(ValueError) as exc:
        helm_argv(release, source, root=tmp_path, values_path=values, namespace=namespace)
    for word in (release, source["chart"], source["repoURL"], namespace):
        if word.startswith("-") or "@" in word or " " in word:
            assert word not in str(exc.value)


def test_directory_source_outside_the_checkout_is_refused(tmp_path):
    root = tmp_path / "instance"
    (root / "clusters" / "platform" / "apps").mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "cm.yaml").write_text(
        f"apiVersion: v1\nkind: ConfigMap\nmetadata:\n  name: x\ndata:\n  m: {MARKER}\n")
    app = {"apiVersion": "argoproj.io/v1alpha1", "kind": "Application",
           "metadata": {"name": "esc"},
           "spec": {"source": {"path": "../outside"}, "destination": {"namespace": "ns"}}}
    with pytest.raises(RenderError) as exc:
        render_application(root, app)
    assert "outside the instance checkout" in str(exc.value)
    assert "../outside" not in str(exc.value)


def test_local_chart_outside_the_checkout_is_not_a_local_chart(tmp_path):
    root = tmp_path / "instance"
    root.mkdir()
    outside = tmp_path / "outside" / "c"
    (outside / "templates").mkdir(parents=True)
    (outside / "Chart.yaml").write_text("apiVersion: v2\nname: c\nversion: 0.1.0\n")
    assert platform._local_chart(root, str(tmp_path / "outside"), "c") is None
    (root / "c" / "templates").mkdir(parents=True)
    (root / "c" / "Chart.yaml").write_text("apiVersion: v2\nname: c\nversion: 0.1.0\n")
    assert platform._local_chart(root, "", "c") == (root / "c").resolve()


def test_application_name_and_namespace_must_be_dns_labels():
    bad = {"apiVersion": "argoproj.io/v1alpha1", "kind": "Application",
           "metadata": {"name": f"a=b,{MARKER}"}, "spec": {"source": {"path": "x"}}}
    with pytest.raises(RenderError) as exc:
        render_application(FIXTURES, bad)
    assert MARKER not in str(exc.value)
    bad_ns = {"apiVersion": "argoproj.io/v1alpha1", "kind": "Application",
              "metadata": {"name": "ok"},
              "spec": {"source": {"path": "x"}, "destination": {"namespace": "Not/A Label"}}}
    with pytest.raises(RenderError, match="destination.namespace"):
        render_application(FIXTURES, bad_ns)


def test_render_refuses_a_chart_path_outside_the_checkout(fake_backends, tmp_path):
    from orbitd.render import _helm_template
    root = fake_backends.backends.instance_root
    outside = tmp_path / "outside-chart"
    (outside / "templates").mkdir(parents=True)
    (outside / "Chart.yaml").write_text("apiVersion: v2\nname: outside\nversion: 0.1.0\n")
    rel = os.path.relpath(outside, root)
    app = {"metadata": {"name": "demo-prod"},
           "spec": {"source": {"path": rel, "helm": {"valuesObject": {}}},
                    "destination": {"namespace": "demo-prod"}}}
    with pytest.raises(BackendError) as exc:
        _helm_template(fake_backends, app)
    assert "inside the checkout" in exc.value.body
    app["spec"]["source"]["path"] = "charts/service"
    app["spec"]["source"]["helm"]["releaseName"] = "--kube-version=1.99.0"
    with pytest.raises(BackendError) as exc:
        _helm_template(fake_backends, app)
    assert "DNS label" in exc.value.body and "1.99.0" not in exc.value.body


# ------------------------------------------------------------ R3 webhook fields

def test_alert_log_keeps_only_monitor_shaped_names_and_rfc3339_stamps():
    log = AlertLog()
    assert log.record("x" * 100_000, "firing", "2026-09-10T10:00:00Z") is False
    assert log.record("queue-backlog\n\x1b[2J", "firing", "2026-09-10T10:00:00Z") is False
    assert log.names() == []
    assert log.record("queue-backlog", "firing", MARKER + "\r\n" * 1000) is True
    assert log.latest("queue-backlog").at == ""
    assert log.record("queue-backlog", "resolved", "2026-09-10T10:07:00.123456789+02:00") is True
    assert log.latest("queue-backlog").at == "2026-09-10T10:07:00.123456789+02:00"


# ------------------------------------------------------------ R4 helm bounded

def _fake_helm(tmp_path: Path, body: str) -> dict[str, str]:
    bin_dir = tmp_path / "fakebin"
    bin_dir.mkdir(exist_ok=True)
    script = bin_dir / "helm"
    script.write_text("#!/bin/sh\n" + body)
    script.chmod(script.stat().st_mode | stat.S_IXUSR)
    env = dict(os.environ)
    env["PATH"] = f"{bin_dir}:{env.get('PATH', '')}"
    return env


def test_a_helm_that_never_answers_is_a_render_failure_within_the_budget(tmp_path):
    env = _fake_helm(tmp_path, "sleep 30\n")
    with pytest.raises(helm.HelmFailure, match="within 1s"):
        helm.run_helm(["helm", "template", "x"], cwd=tmp_path, env=env, timeout=1.0)


def test_helm_output_past_the_cap_is_a_render_failure_not_a_memory_hog(tmp_path):
    env = _fake_helm(tmp_path, "yes 'kind: ConfigMap' | head -c 3000000\n")
    with pytest.raises(helm.HelmFailure, match="exceeded"):
        helm.run_helm(["helm", "template", "x"], cwd=tmp_path, env=env, max_output=1_000_000)


def test_helm_stderr_reaches_the_failure_and_stdout_reaches_the_caller(tmp_path):
    env = _fake_helm(tmp_path, "echo 'kind: ConfigMap'; echo 'chart trouble' >&2; exit 3\n")
    with pytest.raises(helm.HelmFailure, match="chart trouble"):
        helm.run_helm(["helm", "template", "x"], cwd=tmp_path, env=env)
    env = _fake_helm(tmp_path, "printf 'kind: ConfigMap\\n---\\nkind: Secret\\n'\n")
    assert helm.run_helm(["helm", "template", "x"], cwd=tmp_path, env=env).count("kind:") == 2


def test_a_render_of_too_many_documents_is_refused(monkeypatch, tmp_path):
    monkeypatch.setattr(helm, "RENDER_DOCS_MAX", 3)
    with pytest.raises(helm.HelmFailure, match="at most 3"):
        helm.documents_bounded(4)
    helm.documents_bounded(3)


# ------------------------------------------------------------ R5 empty tree

def test_a_missing_or_empty_apps_tree_is_a_read_failure_not_an_empty_plan(tmp_path):
    with pytest.raises(RenderReadError):
        load_applications(tmp_path)
    (tmp_path / "clusters" / "platform" / "apps").mkdir(parents=True)
    with pytest.raises(RenderReadError, match="no Application"):
        load_applications(tmp_path)
    rc, out, err = _run_plan(_plan_args(tmp_path, kube=FakeKube()))
    assert rc == EXIT_RENDER and out == "" and "no Application" in err


# ------------------------------------------------------------ R6 scrubbed failure

class _Forbidden(Exception):
    status = 403
    reason = "Forbidden"

    def __str__(self) -> str:
        return f'secrets is forbidden: {MARKER}'


def _denying_kube() -> FakeKube:
    class Res:
        def get(self, *a, **k):
            raise _Forbidden()

    class Resources:
        def get(self, *a, **k):
            return Res()

    kube = FakeKube()
    kube.dynamic = SimpleNamespace(resources=Resources())
    return kube


def test_a_failing_live_read_is_one_scrubbed_line_and_exit_render():
    rc, out, err = _run_plan(_plan_args(FIXTURES, app="dir-a", kube=_denying_kube()))
    assert rc == EXIT_RENDER
    assert out == ""
    assert err.count("\n") == 1 and "Traceback" not in err
    assert "HTTP 403" in err and MARKER not in err


def test_a_recursion_error_while_planning_is_the_same_clean_exit(monkeypatch):
    def deep(*_a, **_k):
        raise RecursionError("maximum recursion depth exceeded")
    monkeypatch.setattr(platform, "compare_objects", deep)
    rc, out, err = _run_plan(_plan_args(FIXTURES, app="dir-a", kube=FakeKube()))
    assert rc == EXIT_RENDER and out == "" and "RecursionError" in err and "Traceback" not in err


# ------------------------------------------------------------ R16–R19 terminal-round amendments

def test_a_symlink_inside_a_chart_out_of_the_checkout_is_refused(fake_backends, tmp_path):
    from orbitd.render import _helm_template
    root = fake_backends.backends.instance_root
    outside = tmp_path / "stolen.yaml"
    outside.write_text(f"apiVersion: v1\nkind: ConfigMap\nmetadata:\n  name: stolen\ndata:\n  m: {MARKER}\n")
    chart = root / "charts" / "c"
    (chart / "templates").mkdir(parents=True)
    (chart / "Chart.yaml").write_text("apiVersion: v2\nname: c\nversion: 0.1.0\n")
    (chart / "templates" / "stolen.yaml").symlink_to(outside)
    app = {"metadata": {"name": "demo-prod"},
           "spec": {"source": {"path": "charts/c", "helm": {"valuesObject": {}}},
                    "destination": {"namespace": "demo-prod"}}}
    with pytest.raises(BackendError) as exc:
        _helm_template(fake_backends, app)
    assert "outside the checkout" in exc.value.body and MARKER not in exc.value.body
    # the platform renderer refuses the same chart as a local chart source
    inst = tmp_path / "platform-instance"
    (inst / "clusters" / "platform" / "apps").mkdir(parents=True)
    (inst / "charts").mkdir()
    shutil.copytree(chart, inst / "charts" / "c", symlinks=True)
    papp = {"apiVersion": "argoproj.io/v1alpha1", "kind": "Application", "metadata": {"name": "c"},
            "spec": {"source": {"chart": "c", "repoURL": ""}, "destination": {"namespace": "ns"}}}
    with pytest.raises(RenderError, match="outside the instance checkout"):
        render_application(inst, papp)


def test_a_manifest_symlinked_to_another_file_of_the_checkout_renders(tmp_path):
    root = _opaque_instance(tmp_path, MARKER_B64)
    (root / "common").mkdir()
    (root / "common" / "shared.yaml").write_text(
        "apiVersion: v1\nkind: ConfigMap\nmetadata:\n  name: shared\ndata:\n  k: v\n")
    (root / "manifests" / "shared.yaml").symlink_to(Path("..") / "common" / "shared.yaml")
    app = load_applications(root)[0]
    names = sorted(o["metadata"]["name"] for o in render_application(root, app))
    assert "shared" in names


def test_bootstrap_names_an_unreadable_store_not_a_name_collision(tmp_path, monkeypatch, capsys):
    from orbitd import cli
    store = tmp_path / "principals.json"
    store.write_text("{not json")
    monkeypatch.setenv("ORBITD_MCP_URL", "https://mcp.fixture.invalid/mcp")
    monkeypatch.setenv("ORBITD_SIGNING_KEY", str(tmp_path / "signing.pem"))
    monkeypatch.setenv("ORBITD_PRINCIPALS", str(store))
    monkeypatch.setenv("ORBITD_REVOCATIONS", str(tmp_path / "revoked.txt"))
    assert cli.bootstrap(SimpleNamespace(name="alpha")) == 2
    out, err = capsys.readouterr()
    assert out == "" and "cannot read" in err and "already exists" not in err
    assert store.read_text() == "{not json"


def test_a_replay_keeps_owner_references_so_an_owned_secret_is_not_extra():
    body = {"apiVersion": "v1", "kind": "Secret", "type": "Opaque",
            "metadata": {"name": "web-token", "namespace": "ns",
                         "ownerReferences": [{"apiVersion": "apps/v1", "kind": "Deployment",
                                              "name": "web", "uid": "u1"}],
                         "annotations": {"kubectl.kubernetes.io/last-applied-configuration": MARKER}},
            "data": {"token": MARKER_B64}}
    (reduced,) = secrets_fingerprinted([body])
    assert reduced["metadata"]["ownerReferences"] == body["metadata"]["ownerReferences"]
    assert MARKER not in json.dumps(reduced) and MARKER_B64 not in json.dumps(reduced)


# ------------------------------------------------------------ R15 deadline names its step

def test_a_deadline_that_elapses_during_ready_names_ready_not_rollout(fake_backends):
    from test_assertions import FakeClock, _by_name, _seed
    _seed(fake_backends)
    clock = FakeClock()
    from orbitd.assertions import assert_deployed
    # the readiness sleep is cut to the remaining budget; a real sleep overshoots it
    report = assert_deployed(
        fake_backends, "demo", "prod", "web", "ship-1", 1_710_000_000, timeout_s=60.0,
        clock=clock.now, sleeper=lambda seconds: clock.sleep(seconds + 0.5),
        http_get=lambda *a, **k: 200, deadline=clock.t + 1.0,
    )
    rows = _by_name(report)
    assert report.passed is False
    assert rows["image"].status == "ok" and rows["rollout"].status == "ok"
    assert rows["ready"].status == "failed" and "timeout" in (rows["ready"].detail or "")
    assert report.verdict == "LANDED NOT DEPLOYED ready"
    assert [s.name for s in report.steps].count("rollout") == 1
    # and a deadline already past names the first step, never a later one
    early = assert_deployed(
        fake_backends, "demo", "prod", "web", "ship-1", 1_710_000_000, timeout_s=60.0,
        clock=clock.now, sleeper=clock.sleep, http_get=lambda *a, **k: 200,
        deadline=clock.t - 1.0,
    )
    assert early.verdict == "LANDED NOT DEPLOYED image"


# ------------------------------------------------------------ R7/R8 monitor fields

@pytest.mark.parametrize("threshold", [float("nan"), float("inf"), float("-inf")])
def test_a_non_finite_threshold_is_refused(threshold):
    problems = validate(metric(condition={"comparison": ">", "threshold": threshold, "for": "5m"}),
                        catalog=CATALOG)
    assert any("finite" in p for p in problems)


def test_free_text_monitor_fields_are_bounded():
    assert validate(metric(), catalog=CATALOG) == []
    long = validate(metric(summary="s" * 1025, runbook="https://r.invalid/" + "x" * 2048,
                           query="max(q)" + " " * 4096), catalog=CATALOG)
    assert sum(1 for p in long if "at most" in p) == 3


# ------------------------------------------------------------ R9 bootstrap once

def test_bootstrap_is_one_atomic_write_so_a_race_leaves_one_admin(tmp_path):
    store = Store(str(tmp_path / "principals.json"))
    outcomes: list[str] = []
    start = threading.Barrier(8)

    def go(name: str) -> None:
        start.wait()
        try:
            store.add_first_admin(name, 3600)
            outcomes.append("admin")
        except BootstrapDone:
            outcomes.append("done")

    threads = [threading.Thread(target=go, args=(f"admin-{i}",)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert outcomes.count("admin") == 1 and outcomes.count("done") == 7
    assert len(store.admins()) == 1


def test_cli_bootstrap_refuses_a_second_admin_and_prints_the_secret_once(tmp_path, monkeypatch, capsys):
    from orbitd import cli
    monkeypatch.setenv("ORBITD_MCP_URL", "https://mcp.fixture.invalid/mcp")
    monkeypatch.setenv("ORBITD_SIGNING_KEY", str(tmp_path / "signing.pem"))
    monkeypatch.setenv("ORBITD_PRINCIPALS", str(tmp_path / "principals.json"))
    monkeypatch.setenv("ORBITD_REVOCATIONS", str(tmp_path / "revoked.txt"))
    assert cli.bootstrap(SimpleNamespace(name="alpha")) == 0
    first = json.loads(capsys.readouterr().out)
    assert first["client_id"] == "alpha" and first["client_secret"]
    assert cli.bootstrap(SimpleNamespace(name="bravo")) == 2
    out, err = capsys.readouterr()
    assert out == "" and "bootstrap already done" in err and "['alpha']" in err


# ------------------------------------------------------------ R10 one runtime

def test_concurrent_first_callers_share_one_deploy_runtime(fake_backends):
    sys.setswitchinterval(1e-6)
    try:
        for _ in range(50):
            fake_backends.deploy = None
            got: list[object] = []
            start = threading.Barrier(8)

            def go() -> None:
                start.wait()
                got.append(runtime_of(fake_backends).locks)

            threads = [threading.Thread(target=go) for _ in range(8)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()
            assert len({id(x) for x in got}) == 1
    finally:
        sys.setswitchinterval(0.005)


def test_serve_builds_the_runtime_before_any_runner(monkeypatch, cfg):
    from orbitd import server
    order: list[str] = []

    def runner(state):
        order.append("runner:" + ("has-runtime" if getattr(state, "deploy", None) else "no-runtime"))
        return SimpleNamespace(stop=lambda: None)

    monkeypatch.setattr("orbitd.shadow.start_daily_runner", runner)
    monkeypatch.setattr("orbitd.reconcile.start_reconcile_runner", runner)
    monkeypatch.setattr(server, "stop_runners", lambda runners: None)
    fake_uvicorn = SimpleNamespace(run=lambda *a, **k: order.append("uvicorn"))
    monkeypatch.setitem(sys.modules, "uvicorn", fake_uvicorn)
    monkeypatch.setattr("orbitd.state.Backends.from_config", lambda cfg: __import__("orbitd.testing", fromlist=["FakeBackends"]).FakeBackends.build(Path(cfg.principals_path).parent))
    server.serve(cfg)
    assert order == ["runner:has-runtime", "runner:has-runtime", "uvicorn"]
