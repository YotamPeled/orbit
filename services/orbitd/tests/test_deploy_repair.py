"""Repair tests: each defect from r15-verdict, reproduced then fixed."""

from __future__ import annotations

import os
import sys
import time

import pytest
import yaml
from conftest import mint, sdk_client

from orbitd.deploy import (
    GitLedger, RunnerTimeout, RunResult, SubprocessRunner,
    bump_fail_detail, execute_ship, start_job, scrub,
)
from orbitd.roles import Role, Step
from orbitd.testing import FakeDigest

from test_reconcile import FakeSource, OLD, rendered, set_mode, use_render
from test_tools_operator import (
    CATALOG, COMMIT, DIGEST, ENV, IMAGE, PROJECT, TAG, WORKER,
    install, seed_world, wait_result,
)


def _real_apply(monkeypatch, state) -> None:
    """The switch on plus the render and checkout fakes, so a ship reaches the
    rollout and assertions these repair tests are here to break."""
    set_mode(state, "on")
    use_render(monkeypatch, rendered())
    monkeypatch.setattr("orbitd.deploy.read_source", FakeSource(OLD, COMMIT, stale=0))


def _dump(obj) -> str:
    if hasattr(obj, "model_dump"):
        return yaml.safe_dump(obj.model_dump(), sort_keys=False)
    return yaml.safe_dump(obj, sort_keys=False)


def _ship(state, tag=TAG, expect=None, opening=None):
    return execute_ship(
        state, project=PROJECT, env=ENV,
        components=[CATALOG["components"][1]],
        tag=tag, action="ship", actor="desk-op", expect=expect,
        opening=opening or [Step(name="confirm", status="ok")],
    )


# ------------------------------------------------------------------ 1. credential exposure


class EchoRunner:
    """Echoes argv, process env and extra env, then fails. Same shape as the review probe."""

    def __init__(self, body: str, returncode: int = 1) -> None:
        self.body = body
        self.returncode = returncode
        self.calls: list[dict] = []

    def run(self, argv, extra_env):
        self.calls.append({"argv": list(argv), "env": dict(extra_env)})
        lines = ["ARGV " + " ".join(argv), self.body]
        for key, value in os.environ.items():
            lines.append(f"{key}={value}")
        for key, value in extra_env.items():
            lines.append(f"{key}={value}")
        text = "\n".join(lines)
        return RunResult(self.returncode, stdout=text, stderr=text)


@pytest.mark.anyio
async def test_secret_echo_is_scrubbed_from_step_detail(fake_backends, tmp_path, monkeypatch):
    token_file = tmp_path / "registry.token"
    token_file.write_text("registry-secret-value-xyz\n")
    cfg = tmp_path / "github-app"
    cfg.mkdir()
    (cfg / "github-app-id").write_text("app-id-fixture-12345")
    (cfg / "github-app-installation-id").write_text("install-id-fixture-67890")
    pem = "-----BEGIN PRIVATE KEY-----\nMIIFAKEKEYMATERIAL\n-----END PRIVATE KEY-----\n"
    (cfg / "github-app-key.pem").write_text(pem)
    monkeypatch.setenv("ORBIT_REGISTRY_TOKEN_FILE", str(token_file))
    monkeypatch.setenv("ORBIT_CONFIG_DIR", str(cfg))
    leaked = "\n".join([
        "Authorization: Bearer fake.review.jwt",
        "ghs_FAKESECRET",
        "ghp_FAKESECRET",
        "x-access-token:ghs_FAKESECRET",
        "registry-secret-value-xyz",
        "app-id-fixture-12345",
        pem.strip(),
        "eyJhbGciOiJSUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiIxIn0.sigbytes",
    ])
    runner = EchoRunner(leaked)
    install(fake_backends, runner=runner)
    seed_world(fake_backends)
    result = _ship(fake_backends)
    blob = _dump(result)
    assert result.status == "failed"
    by_name = {s.name: s for s in result.steps}
    assert by_name["bump"].status == "failed"
    assert "<token>" in (by_name["bump"].detail or "")
    for secret in (
        "Bearer fake.review.jwt", "ghs_FAKESECRET", "ghp_FAKESECRET",
        "x-access-token:ghs_FAKESECRET", "registry-secret-value-xyz",
        "app-id-fixture-12345", "MIIFAKEKEYMATERIAL",
        "eyJhbGciOiJSUzI1NiIsInR5cCI6IkpXVCJ9",
    ):
        assert secret not in blob, secret
        assert secret not in (by_name["bump"].detail or ""), secret


@pytest.mark.anyio
async def test_digest_error_is_scrubbed(fake_backends):
    install(fake_backends, digest=FakeDigest(error="Bearer fake.review.jwt ghs_FAKESECRET"))
    seed_world(fake_backends)
    result = _ship(fake_backends)
    blob = _dump(result)
    assert result.status == "failed"
    detail = next(s.detail or "" for s in result.steps if s.name == "registry")
    assert "Bearer fake.review.jwt" not in detail
    assert "ghs_FAKESECRET" not in blob
    assert "<token>" in detail


@pytest.mark.anyio
async def test_a_registry_refusal_names_the_image_it_asked_for(fake_backends):
    """A ship of three components answered "manifest not found" and no more. The agent had
    mistyped one component's image in the catalog; with nothing naming it, it looked for
    the fault in orbitd's registry access (the test box, 2026-10-12)."""
    digest = FakeDigest(error="manifest not found")
    install(fake_backends, digest=digest)
    seed_world(fake_backends)
    result = _ship(fake_backends)
    assert result.status == "failed"
    detail = next(s.detail or "" for s in result.steps if s.name == "registry")
    image, tag = digest.calls[-1]
    assert f"{image}:{tag}" in detail and "manifest not found" in detail, detail
    assert "component " in detail, detail


def test_scrub_shapes_without_files():
    text = scrub(
        "Authorization: Bearer abc.def.ghi "
        "Basic YTpi "
        "ghs_ABCDEF "
        "ghp_ABCDEF "
        "x-access-token:sekrit "
        "eyJhbGciOiJSUzI1NiJ9.eyJpc3MiOiIxIn0.sig"
    )
    assert "Bearer abc.def.ghi" not in text
    assert "Basic YTpi" not in text
    assert "ghs_ABCDEF" not in text
    assert "ghp_ABCDEF" not in text
    assert "x-access-token:sekrit" not in text
    assert "eyJhbGciOiJSUzI1NiJ9" not in text
    assert text.count("<token>") >= 4


# ------------------------------------------------------------------ 2. rollout polling pre-ship


@pytest.mark.anyio
async def test_rollout_waits_for_new_generation_and_tag(fake_backends, monkeypatch):
    """Pre-bump Deployment (old tag, gen matching) must not conclude the rollout."""
    _real_apply(monkeypatch, fake_backends)
    rt, clock = install(fake_backends)
    kube = seed_world(fake_backends, image="mirror.invalid/demo-worker:old")
    polls = {"n": 0}

    def sleeper(seconds: float):
        polls["n"] += 1
        clock.t += seconds
        if polls["n"] == 1:
            dep = kube.deployments[0]
            dep["spec"]["template"]["spec"]["containers"][0]["image"] = IMAGE
            dep["metadata"]["generation"] = 2
            dep["status"].update({
                "observedGeneration": 2, "updatedReplicas": 1, "readyReplicas": 1,
                "replicas": 1, "availableReplicas": 1,
            })
            kube.pods[0]["spec"]["containers"][0]["image"] = IMAGE

    rt.sleeper = sleeper
    result = _ship(fake_backends)
    assert result.status == "applied", result
    assert polls["n"] >= 1
    assert result.commit == COMMIT
    assert result.evidence["verdict"] == "DEPLOYED"


# ------------------------------------------------------------------ 4. runner deadline


def test_subprocess_runner_timeout_kills_process_group(tmp_path):
    pids_path = tmp_path / "pids"
    script = tmp_path / "hang.py"
    script.write_text(
        "import os, sys, time\n"
        f"pids_path = {str(pids_path)!r}\n"
        "child = os.fork()\n"
        "if child == 0:\n"
        "    time.sleep(30)\n"
        "    raise SystemExit(0)\n"
        "with open(pids_path, 'w') as fh:\n"
        "    fh.write('%s %s' % (os.getpid(), child))\n"
        "time.sleep(30)\n"
    )
    runner = SubprocessRunner(timeout_s=0.4)
    t0 = time.monotonic()
    with pytest.raises(RunnerTimeout) as raised:
        runner.run([sys.executable, str(script)], {})
    elapsed = time.monotonic() - t0
    assert elapsed < 5, elapsed
    assert "timeout" in str(raised.value)
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline and not pids_path.exists():
        time.sleep(0.05)
    if pids_path.exists():
        parent, child = (int(x) for x in pids_path.read_text().split())
        for pid in (parent, child):
            gone = False
            for _ in range(20):
                try:
                    os.kill(pid, 0)
                except OSError:
                    gone = True
                    break
                time.sleep(0.05)
            assert gone, f"pid {pid} still running after timeout"


class TimeoutRunner:
    def __init__(self) -> None:
        self.calls = []

    def run(self, argv, extra_env):
        self.calls.append({"argv": list(argv), "env": dict(extra_env)})
        raise RunnerTimeout("timeout: /app/scripts/ship.sh exceeded 600s")


@pytest.mark.anyio
async def test_runner_timeout_fails_and_releases_lock(fake_backends, http):
    rt, _ = install(fake_backends, runner=TimeoutRunner())
    seed_world(fake_backends)
    async with sdk_client(http, mint(fake_backends, Role.operator), confirm=True) as client:
        running = await client.call_tool("orbit_deploy_ship", {
            "project": PROJECT, "env": ENV, "components": [WORKER], "tag": TAG,
        })
        result, _ = await wait_result(client, running)
    assert result["status"] == "failed"
    bump = next(s for s in result["steps"] if s["name"] == "bump")
    assert bump["status"] == "failed"
    assert "timeout" in (bump["detail"] or "")
    held = rt.locks.try_acquire(PROJECT, ENV, "after-timeout")
    assert held is None
    rt.locks.release(PROJECT, ENV, "after-timeout")


# ------------------------------------------------------------------ 5. rollout timeout verdict


@pytest.mark.anyio
async def test_rollout_timeout_keeps_commit_and_landed_verdict(fake_backends, monkeypatch):
    """A rollout that never observes the tag fails with the commit and the landed
    verdict. The switch is on so the ship reaches the rollout it is here to time out."""
    _real_apply(monkeypatch, fake_backends)
    rt, _ = install(fake_backends)
    rt.rollout_timeout_s = 0
    kube = seed_world(fake_backends)
    dep = kube.deployments[0]
    dep["metadata"]["generation"] = 1
    dep["status"]["observedGeneration"] = 0
    result = _ship(fake_backends)
    assert result.status == "failed"
    assert result.commit == COMMIT
    assert result.evidence.get("digest") == DIGEST
    assert result.evidence.get("verdict") == "LANDED NOT DEPLOYED rollout"
    rolled = next(s for s in result.steps if s.name == "rollout")
    assert rolled.status == "failed"
    assert "timeout" in (rolled.detail or "")


# ------------------------------------------------------------------ 7. rollback history cap


class PagingHistory:
    """Honours limit/skip the way InstanceRepo.tags_history does."""

    def __init__(self, rows: list[tuple[str, dict]]) -> None:
        self.rows = rows
        self.calls: list[tuple[int, int]] = []

    def tags_history(self, limit: int = 50, skip: int = 0):
        self.calls.append((int(limit), int(skip)))
        page = self.rows[skip:skip + limit]
        return [{"commit": sha, "authored": "2026-01-01T00:00:00Z", "subject": sha}
                for sha, _ in page]

    def show(self, spec: str, missing_ok: bool = True) -> str:
        sha = spec.split(":", 1)[0]
        for commit, tags in self.rows:
            if commit == sha:
                return yaml.safe_dump({"tags": tags})
        return ""


def test_previous_tag_walks_past_one_hundred_commits():
    key = f"{PROJECT}/{ENV}/{WORKER}"
    rows = [(f"{i:040d}", {key: TAG}) for i in range(1, 102)]
    rows.append((f"{102:040d}", {key: "ship-0"}))
    history = PagingHistory(rows)
    ledger = GitLedger(history)  # type: ignore[arg-type]
    assert ledger.previous_tag(PROJECT, ENV, WORKER) == "ship-0"
    assert history.calls, "must page rather than take a single capped list"
    assert any(skip > 0 for _limit, skip in history.calls)
    assert max(skip + limit for limit, skip in history.calls) > 100


# ------------------------------------------------------------------ 8. multi-component results


class SeqRunner:
    """Returns one canned RunResult per call. Extra calls are a test failure."""

    def __init__(self, results: list[RunResult]) -> None:
        self.results = list(results)
        self.calls: list[dict] = []

    def run(self, argv, extra_env):
        self.calls.append({"argv": list(argv), "env": dict(extra_env)})
        if not self.results:
            raise AssertionError(f"later component attempted: {argv}")
        return self.results.pop(0)


@pytest.mark.anyio
async def test_multi_component_failure_keeps_landed_commits(fake_backends, monkeypatch):
    """A later bump failure must keep every landed SHA and skip the rest. The
    switch is on so the first component truly applies before the second bump fails."""
    _real_apply(monkeypatch, fake_backends)
    runner = SeqRunner([
        RunResult(0, stdout=f"COMMIT {COMMIT}\n"),
        RunResult(1, stderr="second component push refused"),
    ])
    install(fake_backends, runner=runner)
    seed_world(fake_backends)
    third = {"name": "batch", "kind": "worker", "image": "mirror.invalid/demo-batch"}
    result = execute_ship(
        fake_backends, project=PROJECT, env=ENV,
        components=[CATALOG["components"][1], CATALOG["components"][0], third],
        tag=TAG, action="ship", actor="desk-op", expect=None,
        opening=[Step(name="confirm", status="ok")],
    )
    assert result.status == "failed"
    assert result.commit == COMMIT
    comps = {row["name"]: row for row in (result.evidence.get("components") or [])}
    assert comps[WORKER]["commit"] == COMMIT
    assert result.evidence.get("verdict") != "DEPLOYED"
    failed_bump = next(s for s in result.steps if s.name == "bump" and s.status == "failed")
    assert failed_bump.status == "failed"
    skipped = result.evidence.get("skipped") or []
    assert "batch" in skipped or (comps.get("batch") or {}).get("stage") == "not_reached"
    assert len(runner.calls) == 2
    assert [c["argv"][3] for c in runner.calls] == [WORKER, "web"]


def test_bump_fail_detail_prefers_rc_line():
    blob = "STEP bump FAIL\nbump FAIL rc=1: could not push after 5 attempts\n"
    assert bump_fail_detail(blob) == "bump FAIL rc=1: could not push after 5 attempts"
    assert bump_fail_detail("second component push refused") == "second component push refused"
    assert bump_fail_detail("  \n") == "ship.sh failed"


@pytest.mark.anyio
async def test_bump_fail_detail_carries_rc_and_last_line(fake_backends):
    runner = SeqRunner([
        RunResult(
            1,
            stdout="STEP bump FAIL\nbump FAIL rc=1: could not push after 5 attempts\n",
            stderr="could not push after 5 attempts\n",
        ),
    ])
    install(fake_backends, runner=runner)
    seed_world(fake_backends)
    result = _ship(fake_backends)
    assert result.status == "failed"
    assert result.commit is None
    bump = next(s for s in result.steps if s.name == "bump")
    assert bump.status == "failed"
    assert bump.detail == "bump FAIL rc=1: could not push after 5 attempts"


@pytest.mark.anyio
async def test_bump_fail_keeps_landed_commit_from_output(fake_backends):
    """A COMMIT line in ship.sh output is recorded even when the bump then fails."""
    runner = SeqRunner([
        RunResult(
            1,
            stdout=f"COMMIT {COMMIT}\nSTEP bump FAIL\nbump FAIL rc=1: later helper died\n",
        ),
    ])
    install(fake_backends, runner=runner)
    seed_world(fake_backends)
    result = _ship(fake_backends)
    assert result.status == "failed"
    assert result.commit == COMMIT
    bump = next(s for s in result.steps if s.name == "bump")
    assert bump.status == "failed"
    assert bump.detail == "bump FAIL rc=1: later helper died"
    comps = {row["name"]: row for row in (result.evidence.get("components") or [])}
    assert comps[WORKER]["commit"] == COMMIT


# ------------------------------------------------------------------ 9. kubernetes deadlines


def test_kube_deadline_fails_job_and_releases_lock(fake_backends, monkeypatch):
    """Withheld kube responses must not leave the job running or the env lock held.
    The switch is on so the ship reaches the kube calls it is here to hang on."""
    import socket
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    from kubernetes import client

    from orbitd.tools_viewer.common import jobs_of

    class HoldHandler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.server.gate.wait()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b"{}")

        do_PUT = do_GET
        do_POST = do_GET
        do_PATCH = do_GET
        do_HEAD = do_GET

        def log_message(self, format, *args):
            return

    _real_apply(monkeypatch, fake_backends)
    rt, _ = install(fake_backends)
    rt.runner_timeout_s = 0.05
    rt.rollout_timeout_s = 0.05
    rt.kube_timeout_s = 0.05
    seed_world(fake_backends)

    server = ThreadingHTTPServer(("127.0.0.1", 0), HoldHandler)
    server.gate = threading.Event()
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    port = server.server_address[1]
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.05):
                break
        except OSError:
            time.sleep(0.01)
    try:
        cfg = client.Configuration()
        cfg.host = f"http://127.0.0.1:{port}"
        cfg.verify_ssl = False
        try:
            from urllib3.util.retry import Retry
            cfg.retries = Retry(total=0, connect=False, read=False, redirect=0, status=0)
        except Exception:
            if hasattr(cfg, "retries"):
                cfg.retries = False
        fake_backends.backends.kube.apps = client.AppsV1Api(client.ApiClient(cfg))
        opening = [Step(name="confirm", status="ok")]

        def run():
            return execute_ship(
                fake_backends, project=PROJECT, env=ENV,
                components=[CATALOG["components"][1]],
                tag=TAG, action="ship", actor="desk-op", expect=None,
                opening=opening,
            )

        running = start_job(
            fake_backends, project=PROJECT, env=ENV, fn=run,
            actor="desk-op", tool="orbit_deploy_ship",
            component=WORKER, tag=TAG, opening=opening,
        )
        assert running.status == "running"
        job_id = running.evidence["job_id"]
        time.sleep(0.3)
        job = jobs_of(fake_backends).get(job_id)
        assert job is not None
        assert job.status != "running", job.status
        assert job.status == "failed"
        result = job.result or {}
        evidence = result.get("evidence") or {}
        assert evidence.get("verdict") == "LANDED NOT DEPLOYED rollout"
        # The verdict is given at the budget; the env lock stays with the job until its
        # worker thread exits (a second job must never race a ship.sh still running).
        held = rt.locks.try_acquire(PROJECT, ENV, "after-deadline")
        release_by = time.monotonic() + 10
        while held is not None and time.monotonic() < release_by:
            time.sleep(0.02)
            held = rt.locks.try_acquire(PROJECT, ENV, "after-deadline")
        assert held is None, "the worker exited without releasing the env lock"
        rt.locks.release(PROJECT, ENV, "after-deadline")
    finally:
        server.gate.set()
        server.shutdown()
        server.server_close()


def test_a_failed_job_says_in_the_log_at_which_step_it_stopped():
    """The audit line of a job carries the one word `failed`; the step and its reason lived
    only in the in-memory job record (docs/V1.md §8: a ship back that failed four seconds
    in, and nothing left to read). `failed_at` is what the log line is made from."""
    from orbitd import deploy
    from orbitd.roles import Mutation, Step
    m = Mutation(status="failed", steps=[Step(name="confirm", status="ok"), Step(name="registry", status="failed", detail="GET manifest answered 503"),
                                         Step(name="bump", status="skipped")])
    assert deploy.failed_at(m) == ("registry", "GET manifest answered 503")
    assert deploy.failed_at(Mutation(status="refused", steps=[Step(name="confirm", status="ok")])) == ("confirm", "")
    assert deploy.failed_at(Mutation(status="failed")) == ("-", "")
    long = Mutation(status="failed", steps=[Step(name="apply", status="failed", detail="x" * 900)])
    assert len(deploy.failed_at(long)[1]) == 300
