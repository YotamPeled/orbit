"""Area 3c of the whole-app sweep: the job and loop layer. Each test names the break it
holds shut: a repair reverted turns it red."""

from __future__ import annotations

import threading
import time

import pytest
import yaml

from orbitd import deploy, reconcile, shadow
from orbitd.deploy import (
    GitLedger, RunResult, execute_ship, scrub, start_job, tag_ok, image_ok,
)
from orbitd.jobs import MAX_JOBS, JOB_TTL_SECONDS, Jobs
from orbitd.onboard import catalog_pairs as onboard_pairs, dashboard_sign_in
from orbitd.reconcile import ReconcileRunner, ReconcileTick
from orbitd.roles import Mutation, Step
from orbitd.testing import FakeDigest, FakeRunner
from orbitd.tools_viewer.common import jobs_of

from test_reconcile import FakeSource, OLD, rendered, set_mode, use_render
from test_tools_operator import CATALOG, COMMIT, ENV, PROJECT, TAG, WORKER, install, seed_world
from test_tools_viewer_kube import seed_catalog

MARK = "PLANTED-VALUE-7f3a"


def _blob(mutation: Mutation) -> str:
    return mutation.model_dump_json()


# ------------------------------------------------------------ the loop outlives a tick


def test_the_reconcile_loop_outlives_an_unreadable_catalog(fake_backends, monkeypatch):
    calls = {"n": 0}

    def pairs(state, **kw):
        calls["n"] += 1
        if calls["n"] == 1:
            raise yaml.YAMLError("mapping values are not allowed here")
        return []

    monkeypatch.setattr(reconcile, "catalog_pairs", pairs)
    runner = ReconcileRunner(fake_backends, dry_run=True, period_s=0.01)
    monkeypatch.setattr(runner, "tick_monitors", lambda: None)
    runner.start()
    deadline = time.time() + 5
    while calls["n"] < 3 and time.time() < deadline:
        time.sleep(0.01)
    alive = runner.thread.is_alive()
    runner.stop()
    assert calls["n"] >= 3, calls
    assert alive, "the loop thread died on the first bad catalog"


def test_a_failing_pair_is_recorded_and_the_next_pair_still_ticks(fake_backends, monkeypatch):
    monkeypatch.setattr(reconcile, "catalog_pairs",
                        lambda state, **kw: [(PROJECT, "prod"), (PROJECT, "dev")])
    runner = ReconcileRunner(fake_backends, dry_run=True)
    monkeypatch.setattr(runner, "tick_monitors", lambda: None)

    def tick(project, env):
        if env == "prod":
            raise RuntimeError(f"plan blew up {MARK}")
        t = ReconcileTick(last_run=reconcile._now(), applied=1, mode="dry-run")
        runner._record(project, env, t)
        return t

    monkeypatch.setattr(runner, "tick", tick)
    ticks = runner.run_once()
    assert [t.failed for t in ticks] == [1, 0]
    assert runner.latest(PROJECT, "prod").failed == 1
    assert runner.latest(PROJECT, "dev").applied == 1


def test_the_loop_ticks_only_the_pairs_the_generator_renders(fake_backends, monkeypatch):
    seed_catalog(fake_backends, [
        {"id": "demo", "environments": {"prod": {}, "dev": {"enabled": False},
                                         "stage": {"enabled": True}}},
        {"id": "elsewhere", "hosted": "external", "environments": {"prod": {}}},
        {"id": "managed", "managed_elsewhere": True, "environments": {"prod": {}}},
        {"id": "a=b", "environments": {"prod": {}}},
    ])
    assert shadow.catalog_pairs(fake_backends, rendered=True) == [("demo", "prod"), ("demo", "stage")]
    assert onboard_pairs(fake_backends.backends.instance.projects()["projects"]) == [("demo", "prod"), ("demo", "stage")]
    runner = ReconcileRunner(fake_backends, dry_run=True)
    seen: list[tuple[str, str]] = []
    monkeypatch.setattr(runner, "tick", lambda p, e: seen.append((p, e)))
    monkeypatch.setattr(runner, "tick_monitors", lambda: None)
    runner.run_once()
    assert seen == [("demo", "prod"), ("demo", "stage")]


# ------------------------------------------------------------ the lock outlives the budget


class SlowRunner:
    """A ship.sh that takes longer than the whole job budget."""

    def __init__(self, hold_s: float) -> None:
        self.hold_s = hold_s
        self.calls: list[list[str]] = []

    def run(self, argv, extra_env):
        self.calls.append(list(argv))
        time.sleep(self.hold_s)
        return RunResult(0, stdout=f"COMMIT {COMMIT}\n", stderr="")


def test_the_env_lock_is_held_until_the_worker_exits_and_no_write_follows_the_verdict(
        fake_backends, monkeypatch):
    runner = SlowRunner(hold_s=0.6)
    rt, _ = install(fake_backends, runner=runner)
    rt.runner_timeout_s = 0.05
    rt.rollout_timeout_s = 0.05  # budget 0.1 s, the bump takes 0.6 s
    seed_world(fake_backends)
    set_mode(fake_backends, "on")
    use_render(monkeypatch, rendered())
    monkeypatch.setattr("orbitd.deploy.read_source", FakeSource(OLD, COMMIT, stale=0))
    applies: list[str] = []
    monkeypatch.setattr("orbitd.deploy.apply_step",
                        lambda *a, **k: applies.append("apply") or Step(name="apply", status="ok"))
    opening = [Step(name="confirm", status="ok")]

    def run():
        return execute_ship(fake_backends, project=PROJECT, env=ENV,
                            components=[CATALOG["components"][1]], tag=TAG, action="ship",
                            actor="desk-op", expect=None, opening=opening)

    started = start_job(fake_backends, project=PROJECT, env=ENV, fn=run, actor="desk-op",
                        tool="orbit_deploy_ship", component=WORKER, tag=TAG, opening=opening)
    job_id = started.evidence["job_id"]
    time.sleep(0.3)  # past the budget, inside the bump
    job = jobs_of(fake_backends).get(job_id)
    assert job.status == "failed", job.status
    assert job.result["evidence"]["verdict"] == "LANDED NOT DEPLOYED rollout"
    assert rt.locks.try_acquire(PROJECT, ENV, "second") == job_id, "the lock was free while ship.sh ran"
    deadline = time.time() + 5
    while rt.locks.try_acquire(PROJECT, ENV, "second") is not None and time.time() < deadline:
        time.sleep(0.02)
    rt.locks.release(PROJECT, ENV, "second")
    assert len(runner.calls) == 1
    assert applies == [], "the worker applied after the verdict"


# ------------------------------------------------------------ the job table


def test_the_cap_evicts_terminal_records_and_spares_a_running_job():
    jobs = Jobs()
    running = jobs.create(status="running")
    for _ in range(MAX_JOBS + 20):
        jobs.create(status="cancelled")
    assert jobs.get(running.id) is not None
    assert len(jobs._jobs) == MAX_JOBS
    assert jobs.complete(running.id, status="completed", result={"ok": True}) is not None
    assert jobs.get(running.id).result == {"ok": True}


def test_a_record_past_the_ttl_is_gone(monkeypatch):
    jobs = Jobs()
    job = jobs.create(status="completed")
    job.created_at -= JOB_TTL_SECONDS + 1
    assert jobs.get(job.id) is None


def test_a_busy_refusal_leaves_no_job_record(fake_backends):
    rt, _ = install(fake_backends)
    seed_world(fake_backends)
    assert rt.locks.try_acquire(PROJECT, ENV, "holder") is None
    before = len(jobs_of(fake_backends)._jobs)
    refused = start_job(fake_backends, project=PROJECT, env=ENV, fn=lambda: None,
                        actor="desk-op", tool="orbit_deploy_ship", component=WORKER, tag=TAG,
                        opening=[])
    assert not isinstance(refused, Mutation)
    assert refused.structured_content["code"] == "busy"
    assert len(jobs_of(fake_backends)._jobs) == before
    rt.locks.release(PROJECT, ENV, "holder")


# ------------------------------------------------------------ what may reach argv


BAD_TAG = f"--output=/tmp/x;`id`\n{MARK}"


def test_a_tag_that_is_not_a_tag_shape_never_reaches_argv(fake_backends):
    runner = FakeRunner(commit=COMMIT)
    digest = FakeDigest()
    install(fake_backends, runner=runner, digest=digest)
    seed_world(fake_backends)
    result = execute_ship(fake_backends, project=PROJECT, env=ENV,
                          components=[CATALOG["components"][1]], tag=BAD_TAG, action="ship",
                          actor="desk-op", expect=None, opening=[])
    assert result.status == "failed"
    assert result.steps[-1].name == "catalog"
    assert runner.calls == [] and digest.calls == []
    assert MARK not in _blob(result)


def test_a_catalog_image_that_is_not_an_image_reference_never_reaches_argv(fake_backends):
    runner = FakeRunner(commit=COMMIT)
    digest = FakeDigest()
    install(fake_backends, runner=runner, digest=digest)
    seed_world(fake_backends)
    comp = dict(CATALOG["components"][1], image=f"$(id) --force {MARK}")
    result = execute_ship(fake_backends, project=PROJECT, env=ENV, components=[comp], tag=TAG,
                          action="ship", actor="desk-op", expect=None, opening=[])
    assert result.status == "failed"
    assert result.steps[-1].name == "catalog"
    assert runner.calls == [] and digest.calls == []
    assert MARK not in _blob(result)


class _Instance:
    """A tags ledger whose history carries one value that is not a tag."""

    def __init__(self, values: list[str]) -> None:
        self.values = values

    def tags(self):
        return {"tags": {f"{PROJECT}/{ENV}/{WORKER}": self.values[0]}}

    def tags_history(self, *, limit, skip):
        return [] if skip else [{"commit": f"{i:040x}"} for i in range(len(self.values))]

    def show(self, spec, missing_ok=False):
        index = int(spec.split(":", 1)[0], 16)
        return yaml.safe_dump({"tags": {f"{PROJECT}/{ENV}/{WORKER}": self.values[index]}})


def test_the_git_ledger_fails_closed_on_a_value_that_is_not_a_tag():
    ledger = GitLedger(_Instance([TAG, BAD_TAG, "ship-0"]))
    with pytest.raises(ValueError) as caught:
        ledger.previous_tag(PROJECT, ENV, WORKER)
    assert MARK not in str(caught.value)
    assert GitLedger(_Instance([BAD_TAG])).current_tag(PROJECT, ENV, WORKER) is None
    assert GitLedger(_Instance([TAG, "ship-0"])).previous_tag(PROJECT, ENV, WORKER) == "ship-0"


def test_the_tag_and_image_shapes():
    assert tag_ok("v1.2.3-rc.1") and tag_ok("ship-1") and tag_ok("A" * 128)
    for bad in ("", "-x", "a b", "a\n", "A" * 129, "a/b", "a=b"):
        assert not tag_ok(bad), bad
    assert image_ok("mirror.invalid/demo-worker") and image_ok("ghcr.io/org/app__x") \
        and image_ok("registry:5000/a/b") and image_ok("nginx")
    for bad in ("", "-x", "$(id)", "a b", "Nginx", "a/b:tag", "a@sha256:00", "a\n"):
        assert not image_ok(bad), bad


# ------------------------------------------------------------ scrub literals


def test_scrub_ignores_blank_and_short_credential_files(tmp_path, monkeypatch):
    cfg = tmp_path / "github-app"
    cfg.mkdir()
    (cfg / "github-app-id").write_text("\n")
    (cfg / "github-app-installation-id").write_text("7\n")
    (cfg / "github-app-key.pem").write_text("A-REAL-LOOKING-KEY-VALUE\n")
    monkeypatch.setenv("ORBIT_CONFIG_DIR", str(cfg))
    monkeypatch.delenv("ORBIT_REGISTRY_TOKEN_FILE", raising=False)
    assert scrub("bump FAIL rc=7\npush rejected") == "bump FAIL rc=7\npush rejected"
    assert scrub("key A-REAL-LOOKING-KEY-VALUE here") == "key <token> here"


# ------------------------------------------------------------ onboard's sign-in origin


def test_the_dashboard_sign_in_refuses_a_domain_that_is_not_a_hostname(tmp_path, monkeypatch):
    pw = tmp_path / "pw"
    pw.write_text(f"{MARK}\n")
    monkeypatch.setattr("urllib.request.build_opener",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("opened")))
    answer = dashboard_sign_in(f"corp.example@collector.test", pw)
    assert answer == "unanswered (domains.primary is not a hostname shape)"
    assert MARK not in answer
    for bad in ("", "-x.example", "EXAMPLE.org", "a b.example", "x", "example.org/x"):
        assert dashboard_sign_in(bad, pw).startswith("unanswered (domains.primary"), bad


# ------------------------------------------------------------ kube timeouts that hold


def _is_client_timeout(value) -> bool:
    """The only shapes kubernetes.client.rest honours: an int, or a (connect, read) pair."""
    if isinstance(value, bool):
        return False
    if isinstance(value, int):
        return True
    return (isinstance(value, tuple) and len(value) == 2
            and all(isinstance(v, (int, float)) and v > 0 for v in value))


def test_every_kube_timeout_this_layer_builds_is_a_shape_the_client_honours(fake_backends):
    from orbitd import assertions, backends
    rt, _ = install(fake_backends)
    assert _is_client_timeout(deploy.kube_request_timeout(rt))
    assert _is_client_timeout(deploy.kube_request_timeout(rt, rt.clock() + 1))
    assert _is_client_timeout(assertions._kube_timeout(fake_backends, None, rt.clock))
    assert _is_client_timeout(assertions._kube_timeout(fake_backends, rt.clock() + 1, rt.clock))
    assert _is_client_timeout(backends.KUBE_TIMEOUT)
    # a float is what the client silently ignores
    assert not _is_client_timeout(15.0)


def test_a_held_kube_connection_times_out_inside_the_deadline(fake_backends):
    import socket
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    from kubernetes import client

    from orbitd.backends import BackendError
    from orbitd.tools_viewer._util import kube_call

    class Hold(BaseHTTPRequestHandler):
        def do_GET(self):
            self.server.gate.wait()

        def log_message(self, *a):
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Hold)
    server.gate = threading.Event()
    threading.Thread(target=server.serve_forever, daemon=True).start()
    watchdog = threading.Timer(5.0, server.gate.set)  # a regressed bound fails, never hangs
    watchdog.start()
    try:
        cfg = client.Configuration()
        cfg.host = f"http://127.0.0.1:{server.server_address[1]}"
        from urllib3.util.retry import Retry
        cfg.retries = Retry(total=0, connect=False, read=False, redirect=0, status=0)
        api = client.AppsV1Api(client.ApiClient(cfg))
        rt, _ = install(fake_backends)
        rt.kube_timeout_s = 0.2
        started = time.monotonic()
        with pytest.raises(BackendError):
            kube_call(api.read_namespaced_deployment, "x", "y",
                      _request_timeout=deploy.kube_request_timeout(rt))
        assert time.monotonic() - started < 2.0, "the client's timeout was not honoured"
    finally:
        watchdog.cancel()
        server.gate.set()
        server.shutdown()
        server.server_close()


def test_the_appliers_pass_a_timeout_shape_the_client_honours(fake_backends, monkeypatch):
    from orbitd import applier
    install(fake_backends)
    seed_world(fake_backends)
    set_mode(fake_backends, "on")
    use_render(monkeypatch, rendered())
    plan = applier.plan(fake_backends, PROJECT, ENV)
    applier.apply(fake_backends, plan, dry_run=False)
    kube = fake_backends.backends.kube
    recorded = list(kube.dynamic_calls) + list(kube.apply_calls) + list(kube.delete_calls) + list(kube.mutations)
    calls = [c for c in recorded if isinstance(c, dict) and "_request_timeout" in c]
    assert calls, "no kube call recorded a timeout"
    assert any(c in kube.apply_calls for c in calls), "the write path recorded no timeout"
    bad = [c["_request_timeout"] for c in calls if not _is_client_timeout(c["_request_timeout"])]
    assert bad == [], bad


def test_a_request_that_names_no_timeout_is_still_bounded(tmp_path, monkeypatch):
    """Discovery and the untimed list calls: bound at the request layer of the real client."""
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    from orbitd import backends
    from orbitd.backends import BackendError, Kube

    class Hold(BaseHTTPRequestHandler):
        def do_GET(self):
            self.server.gate.wait()

        def log_message(self, *a):
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Hold)
    server.gate = threading.Event()
    threading.Thread(target=server.serve_forever, daemon=True).start()
    kubeconfig = tmp_path / "kubeconfig"
    kubeconfig.write_text(yaml.safe_dump({
        "apiVersion": "v1", "kind": "Config", "current-context": "t",
        "clusters": [{"name": "t", "cluster": {"server": f"http://127.0.0.1:{server.server_address[1]}"}}],
        "contexts": [{"name": "t", "context": {"cluster": "t", "user": "t"}}],
        "users": [{"name": "t", "user": {"token": "fixture-token-not-a-secret"}}],
    }))
    monkeypatch.setattr(backends, "KUBE_TIMEOUT", (0.2, 0.2))
    watchdog = threading.Timer(5.0, server.gate.set)  # a regressed bound fails, never hangs
    watchdog.start()
    try:
        kube = Kube(str(kubeconfig))
        started = time.monotonic()
        with pytest.raises(Exception):
            kube.core.list_namespace()
        with pytest.raises(Exception):
            kube.dynamic.resources.get(api_version="v1", kind="Namespace")
        assert time.monotonic() - started < 3.0, "an untimed request outlived the bound"
    finally:
        watchdog.cancel()
        server.gate.set()
        server.shutdown()
        server.server_close()


# ------------------------------------------------------------ each guard on its own


def test_run_once_itself_survives_an_unreadable_catalog(fake_backends, monkeypatch):
    def pairs(state, **kw):
        raise yaml.YAMLError("mapping values are not allowed here")

    monkeypatch.setattr(reconcile, "catalog_pairs", pairs)
    runner = ReconcileRunner(fake_backends, dry_run=True)
    monkeypatch.setattr(runner, "tick_monitors", lambda: None)
    assert runner.run_once() == []


def test_the_loop_survives_a_tick_that_raises_past_run_once(fake_backends, monkeypatch):
    calls = {"n": 0}

    def run_once():
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("the tick machinery itself blew up")
        return []

    runner = ReconcileRunner(fake_backends, dry_run=True, period_s=0.01)
    monkeypatch.setattr(runner, "run_once", run_once)
    runner.start()
    deadline = time.time() + 5
    while calls["n"] < 3 and time.time() < deadline:
        time.sleep(0.01)
    alive = runner.thread.is_alive()
    runner.stop()
    assert calls["n"] >= 3 and alive


def test_the_rollout_poll_stops_once_the_budget_is_spent(fake_backends):
    rt, _ = install(fake_backends)
    rt.rollout_timeout_s = 5
    seed_world(fake_backends)
    assert rt.locks.try_acquire(PROJECT, ENV, "job") is None
    rt.locks.expire(PROJECT, ENV, "job")
    started = time.monotonic()
    step = deploy.wait_rollout(fake_backends, PROJECT, ENV, WORKER, tag="never-such-tag")
    rt.locks.release(PROJECT, ENV, "job")
    assert step.status == "failed" and step.detail.startswith("job budget spent")
    assert time.monotonic() - started < 1.0


# ------------------------------------------------------------ nothing follows the verdict


def _expire(rt, project=PROJECT, env=ENV, job="job"):
    assert rt.locks.try_acquire(project, env, job) is None
    rt.locks.expire(project, env, job)
    assert rt.locks.expired(project, env)


def test_a_bump_is_refused_at_the_loop_head_once_the_budget_is_spent(fake_backends, monkeypatch):
    runner = FakeRunner(commit=COMMIT)
    rt, _ = install(fake_backends, runner=runner)
    seed_world(fake_backends)
    monkeypatch.setattr("orbitd.deploy.read_source", FakeSource(OLD, COMMIT, stale=0))
    _expire(rt)
    result = execute_ship(fake_backends, project=PROJECT, env=ENV,
                          components=[CATALOG["components"][1]], tag=TAG, action="ship",
                          actor="desk-op", expect=None, opening=[])
    rt.locks.release(PROJECT, ENV, "job")
    assert result.status == "failed"
    assert runner.calls == []
    bump = [s for s in result.steps if s.name == "bump"]
    assert bump and bump[0].status == "failed" and "budget spent" in (bump[0].detail or "")


def test_a_restart_is_refused_before_the_patch_once_the_budget_is_spent(fake_backends, monkeypatch):
    rt, _ = install(fake_backends)
    seed_world(fake_backends)
    monkeypatch.setattr("orbitd.deploy.read_source", FakeSource(OLD, COMMIT, stale=0))
    _expire(rt)
    result = deploy.execute_restart(fake_backends, project=PROJECT, env=ENV, component=WORKER,
                                    tag=TAG, opening=[])
    rt.locks.release(PROJECT, ENV, "job")
    assert result.status == "failed"
    assert fake_backends.backends.kube.patches == []
    assert result.steps[-1].name == "restart" and "budget spent" in (result.steps[-1].detail or "")


def test_a_restarts_apply_is_refused_when_the_budget_goes_during_the_patch(fake_backends, monkeypatch):
    rt, _ = install(fake_backends)
    seed_world(fake_backends)
    set_mode(fake_backends, "on")
    use_render(monkeypatch, rendered())
    monkeypatch.setattr("orbitd.deploy.read_source", FakeSource(OLD, COMMIT, stale=0))
    assert rt.locks.try_acquire(PROJECT, ENV, "job") is None
    kube = fake_backends.backends.kube
    real_patch = kube.apps.patch_namespaced_deployment

    def patch_then_budget(*a, **k):
        out = real_patch(*a, **k)
        rt.locks.expire(PROJECT, ENV, "job")
        return out

    monkeypatch.setattr(kube.apps, "patch_namespaced_deployment", patch_then_budget)
    applies: list[str] = []
    monkeypatch.setattr("orbitd.applier.apply", lambda *a, **k: applies.append("apply"))
    result = deploy.execute_restart(fake_backends, project=PROJECT, env=ENV, component=WORKER,
                                    tag=TAG, opening=[])
    rt.locks.release(PROJECT, ENV, "job")
    assert len(kube.patches) == 1
    assert applies == [], "the restart applied after the verdict"
    assert result.status == "failed" and result.evidence.get("verdict") == "LANDED NOT DEPLOYED apply"
    apply = [s for s in result.steps if s.name == "apply"]
    assert apply and "budget spent" in (apply[0].detail or "")


def test_the_applier_stops_between_objects_once_the_budget_is_spent(fake_backends, monkeypatch):
    from orbitd import applier
    rt, _ = install(fake_backends)
    seed_world(fake_backends)
    set_mode(fake_backends, "on")
    use_render(monkeypatch, rendered())  # two objects
    assert rt.locks.try_acquire(PROJECT, ENV, "job") is None
    real = applier._server_side_apply
    seen: list[str] = []

    def apply_then_budget(dyn, obj, ref, *, dry):
        seen.append(ref.name)
        rt.locks.expire(PROJECT, ENV, "job")
        return real(dyn, obj, ref, dry=dry)

    monkeypatch.setattr(applier, "_server_side_apply", apply_then_budget)
    plan = applier.plan(fake_backends, PROJECT, ENV)
    result = applier.apply(fake_backends, plan, dry_run=False)
    rt.locks.release(PROJECT, ENV, "job")
    assert len(plan.objects) == 2 and seen == [plan.refs[0].name]
    assert not result.ok and result.results[-1].result == "failed"
    assert "budget spent" in (result.results[-1].reason or "")
    assert result.inventory is None, "an inventory was written for an apply that stopped"


def test_a_prune_stops_before_its_first_delete_once_the_budget_is_spent(fake_backends, monkeypatch):
    from orbitd import applier
    from orbitd.applier import ObjectRef
    rt, _ = install(fake_backends)
    seed_world(fake_backends)
    set_mode(fake_backends, "on")
    use_render(monkeypatch, rendered())
    plan = applier.plan(fake_backends, PROJECT, ENV)
    doomed = ObjectRef(api_version="v1", kind="ConfigMap", namespace=f"{PROJECT}-{ENV}", name="old-config")
    from dataclasses import replace
    plan = replace(plan, prune=[doomed])
    _expire(rt)
    result = applier.prune(fake_backends, plan, dry_run=False)
    rt.locks.release(PROJECT, ENV, "job")
    assert not result.ok and result.results == [] or result.results[-1].result == "failed"
    assert fake_backends.backends.kube.delete_calls == []


def test_a_monitor_commit_is_refused_once_the_units_budget_is_spent(fake_backends, monkeypatch):
    from orbitd.monitors_apply import UNIT
    from orbitd.tools_operator import monitors as monitor_tools
    runner = FakeRunner(commit=COMMIT)
    rt, _ = install(fake_backends, runner=runner)
    monkeypatch.setattr("orbitd.deploy.read_source", FakeSource(OLD, COMMIT, stale=0))
    _expire(rt, UNIT, "")
    up = monitor_tools._execute_upsert(fake_backends, monitor={"name": "m"}, name="m", refs=[], actor="desk-op")
    down = monitor_tools._execute_delete(fake_backends, name="m", refs=[], actor="desk-op")
    rt.locks.release(UNIT, "", "job")
    assert runner.calls == [], "monitor-commit ran after the verdict"
    for result in (up, down):
        assert result.status == "failed"
        assert result.steps[-1].name == "commit" and "budget spent" in (result.steps[-1].detail or "")


def test_the_monitors_applier_stops_between_objects_once_the_budget_is_spent(fake_backends, monkeypatch):
    from dataclasses import replace
    from orbitd import monitors_apply
    from orbitd.applier import ObjectRef, Plan
    from orbitd.monitors_apply import UNIT
    from orbitd.roles import CheckoutSource
    rt, _ = install(fake_backends)
    set_mode(fake_backends, "on")
    objects = [{"apiVersion": "v1", "kind": "ConfigMap", "metadata": {"name": f"m{i}", "namespace": "monitoring"}}
               for i in range(2)]
    refs = [ObjectRef(api_version="v1", kind="ConfigMap", namespace="monitoring", name=f"m{i}") for i in range(2)]
    plan = Plan(project=UNIT, env="", namespace="", objects=objects, refs=refs, prune=[], withheld=[],
                previous=[], inventory_found=False, render_sha="0" * 40,
                source=CheckoutSource(commit=COMMIT, age_s=0, lag_note="test"))
    _expire(rt, UNIT, "")
    applied = monitors_apply.apply_monitors(fake_backends, plan, dry_run=False)
    pruned = monitors_apply.prune_monitors(fake_backends, replace(plan, prune=refs), dry_run=False)
    rt.locks.release(UNIT, "", "job")
    assert not applied.ok and applied.results[0].result == "failed" and "budget spent" in applied.results[0].reason
    assert not pruned.ok and pruned.results[0].result == "failed"
    assert fake_backends.backends.kube.apply_calls == [] and fake_backends.backends.kube.delete_calls == []


# ------------------------------------------------------------ the lock's expiry bookkeeping


def test_release_clears_the_expiry_for_the_next_holder():
    locks = deploy.EnvLocks()
    assert locks.try_acquire(PROJECT, ENV, "j1") is None
    locks.expire(PROJECT, ENV, "j1")
    assert locks.expired(PROJECT, ENV)
    locks.release(PROJECT, ENV, "j1")
    assert not locks.expired(PROJECT, ENV)
    assert locks.try_acquire(PROJECT, ENV, "j2") is None
    assert not locks.expired(PROJECT, ENV)


def test_only_the_holder_can_expire_its_lock():
    locks = deploy.EnvLocks()
    assert locks.try_acquire(PROJECT, ENV, "j1") is None
    locks.expire(PROJECT, ENV, "stale-timer-of-another-job")
    assert not locks.expired(PROJECT, ENV)
    locks.expire(PROJECT, "other-env", "j1")
    assert not locks.expired(PROJECT, "other-env")


def test_a_dry_run_preview_is_not_refused_by_another_jobs_spent_budget(fake_backends, monkeypatch):
    """orbit_deploy_reconcile's default preview holds no lock and writes nothing; the
    verdict of the job that does hold the lock must not fail it."""
    from orbitd import applier
    rt, _ = install(fake_backends)
    seed_world(fake_backends)
    set_mode(fake_backends, "on")
    use_render(monkeypatch, rendered())
    _expire(rt)
    plan = applier.plan(fake_backends, PROJECT, ENV)
    result = applier.apply(fake_backends, plan, dry_run=True)
    rt.locks.release(PROJECT, ENV, "job")
    assert result.ok and result.dry_run
    assert [r.result for r in result.results] == ["applied", "applied"]
    assert all(c.get("dry_run") or "dryRun" in str(c) for c in fake_backends.backends.kube.apply_calls), \
        fake_backends.backends.kube.apply_calls[:1]
