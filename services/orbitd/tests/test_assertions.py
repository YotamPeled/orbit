"""Four post-ship assertions over FakeKube/FakeBackends. No cluster, no network."""

from __future__ import annotations

from orbitd.assertions import assert_deployed
from orbitd.backends import BackendError, BackendUnconfigured

from test_tools_viewer_kube import labels, seed_catalog

PROJECT = "demo"
ENV_PROD = "prod"
ENV_DEV = "dev"
WEB = "web"
WORKER = "worker"
TAG = "ship-1"
IMAGE = f"mirror.invalid/demo-web:{TAG}"
WORKER_IMAGE = f"mirror.invalid/demo-worker:{TAG}"
OLD_IMAGE = "mirror.invalid/demo-web:old"
SINCE = 1_710_000_000
POD = "demo-web-6b8f9c7d5-xk2lm"
WORKER_POD = "demo-worker-6b8f9c7d5-xk2lm"
CREATED = "2026-01-01T00:00:00Z"
PROD_HOST = "demo.example.test"

CATALOG = {
    "id": "demo",
    "environments": {
        "prod": {"hostnames": [PROD_HOST]},
        "dev": {"hostnames": ["demo.dev.example.test"]},
    },
    "components": [
        {
            "name": "web", "kind": "web", "image": "mirror.invalid/demo-web",
            "port": 8080, "probePath": "/healthz", "readinessPath": "/readyz",
        },
        {"name": "worker", "kind": "worker", "image": "mirror.invalid/demo-worker"},
    ],
}


class FakeClock:
    def __init__(self, t: float = float(SINCE)) -> None:
        self.t = t
        self.sleeps: list[float] = []

    def now(self) -> float:
        return self.t

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.t += seconds


def _loki(pod: str, ts: str = "1710000000000000000", line: str = "listening") -> dict:
    return {
        "status": "success",
        "data": {
            "resultType": "streams",
            "result": [{"stream": {"pod": pod, "namespace": "demo-prod"}, "values": [[ts, line]]}],
        },
    }


def _loki_n(pod: str, n: int, start_ts: int = 1_710_000_000_000_000_000) -> dict:
    values = [[str(start_ts + i), f"line-{i}"] for i in range(n)]
    return {
        "status": "success",
        "data": {
            "resultType": "streams",
            "result": [{"stream": {"pod": pod}, "values": values}],
        },
    }


def _http_ok():
    calls: list[str] = []

    def get(url: str) -> int:
        calls.append(url)
        return 200

    get.calls = calls  # type: ignore[attr-defined]
    return get


def _pod(
    kube, *, name=POD, image=IMAGE, component=WEB, env=ENV_PROD,
    ready=True, restarts=0, waiting=None, oom=False, deleting=False,
    created=CREATED, period=5, extra_containers=None,
):
    namespace = f"{PROJECT}-{env}"
    pod = kube.add_pod(
        namespace=namespace, name=name, component=component, image=image,
        restarts=restarts, labels=labels(PROJECT, component),
        last_terminated="OOMKilled" if oom else None,
        phase="Pending" if waiting else "Running",
    )
    pod["metadata"]["creationTimestamp"] = created
    if deleting:
        pod["metadata"]["deletionTimestamp"] = "2026-01-01T00:01:00Z"
    container = pod["spec"]["containers"][0]
    if period is not None:
        container["readinessProbe"] = {"periodSeconds": period}
    if extra_containers:
        pod["spec"]["containers"].extend(extra_containers)
    cs = pod["status"]["containerStatuses"][0]
    cs["ready"] = ready
    if waiting:
        cs["state"] = {"waiting": {"reason": waiting}}
    pod["status"]["conditions"] = [{"type": "Ready", "status": "True" if ready else "False"}]
    return pod


def _deploy(kube, *, component=WEB, env=ENV_PROD, generation=1, observed=1,
            updated=1, ready=1, replicas=1, available=1):
    namespace = f"{PROJECT}-{env}"
    dep = kube.add_deployment(
        namespace=namespace, name=f"{PROJECT}-{component}",
        replicas=replicas, ready=ready, image=IMAGE if component == WEB else WORKER_IMAGE,
    )
    dep["metadata"]["generation"] = generation
    dep["status"].update({
        "observedGeneration": observed,
        "updatedReplicas": updated,
        "readyReplicas": ready,
        "replicas": replicas,
        "availableReplicas": available,
    })
    return dep


def _seed(state, *, env=ENV_PROD, component=WEB, pod=None, deploy=True, loki=None):
    seed_catalog(state, [CATALOG])
    kube = state.backends.kube
    if pod is None:
        _pod(kube, component=component, env=env,
             name=POD if component == WEB else WORKER_POD,
             image=IMAGE if component == WEB else WORKER_IMAGE)
    if deploy:
        _deploy(kube, component=component, env=env)
    if loki is None:
        loki = _loki(POD if component == WEB else WORKER_POD)
    state.backends.loki.responses["/loki/api/v1/query_range"] = loki


def _run(state, *, env=ENV_PROD, component=WEB, tag=TAG, since=SINCE,
         timeout_s=60.0, clock=None, sleeper=None, http_get=None):
    clock = clock or FakeClock()
    return assert_deployed(
        state, PROJECT, env, component, tag, since,
        timeout_s=timeout_s,
        clock=clock.now if hasattr(clock, "now") else clock,
        sleeper=sleeper or (clock.sleep if hasattr(clock, "sleep") else None),
        http_get=http_get or _http_ok(),
    ), clock


def _by_name(report):
    return {s.name: s for s in report.steps}


# ------------------------------------------------------------------ A pod set


def test_a_pod_set_ignores_terminating_includes_active_old_replicaset(fake_backends):
    """A. Terminating pods are ignored; an active old-ReplicaSet pod is in the set."""
    state = fake_backends
    seed_catalog(state, [CATALOG])
    kube = state.backends.kube
    _pod(kube, name="demo-web-old-live", image=OLD_IMAGE)
    _pod(kube, name=POD, image=IMAGE)
    _pod(kube, name="demo-web-old-term", image=OLD_IMAGE, deleting=True)
    _deploy(kube)
    report, _ = _run(state)
    assert report.passed is False
    assert report.verdict == "LANDED NOT DEPLOYED image"
    image = _by_name(report)["image"]
    assert image.status == "failed"
    assert "demo-web-old-live" in (image.detail or "")
    assert "demo-web-old-term" not in (image.detail or "")
    names = [p["name"] for p in report.evidence["pods"]]
    assert "demo-web-old-live" in names
    assert POD in names
    assert "demo-web-old-term" not in names


# ------------------------------------------------------------------ B image


def test_b_image_every_named_container_carries_the_tag(fake_backends):
    """B. The component container carries the tag; sidecars are ignored; empty set fails."""
    state = fake_backends
    _seed(state)
    kube = state.backends.kube
    kube.pods[0]["spec"]["containers"].append({"name": "sidecar", "image": "sidecar:old"})
    report, _ = _run(state)
    assert report.passed is True, report
    assert report.verdict == "DEPLOYED"
    assert _by_name(report)["image"].status == "ok"

    kube.pods.clear()
    kube.deployments.clear()
    report, _ = _run(state)
    assert report.passed is False
    assert report.verdict == "LANDED NOT DEPLOYED image"
    assert "empty pod set" in (_by_name(report)["image"].detail or "")


def test_b_image_wrong_repository_same_tag_fails(fake_backends):
    """B. Same tag on a different repository is not the shipped reference."""
    state = fake_backends
    seed_catalog(state, [CATALOG])
    kube = state.backends.kube
    wrong = f"wrong/repo:{TAG}"
    _pod(kube, image=wrong)
    _deploy(kube)
    report, _ = _run(state)
    assert report.passed is False
    assert report.verdict == "LANDED NOT DEPLOYED image"
    detail = _by_name(report)["image"].detail or ""
    assert POD in detail
    assert wrong in detail


def test_b_image_digest_form_matching_repo_passes(fake_backends):
    """B. Digest form of the catalog repo is accepted when imageID names that repo."""
    state = fake_backends
    seed_catalog(state, [CATALOG])
    kube = state.backends.kube
    digest = "sha256:" + ("a" * 64)
    img = f"mirror.invalid/demo-web@{digest}"
    image_id = f"docker-pullable://mirror.invalid/demo-web@{digest}"
    pod = _pod(kube, image=img)
    pod["status"]["containerStatuses"][0]["imageID"] = image_id
    _deploy(kube)
    state.backends.loki.responses["/loki/api/v1/query_range"] = _loki(POD)
    report, _ = _run(state)
    assert report.passed is True, report
    assert report.verdict == "DEPLOYED"
    assert _by_name(report)["image"].status == "ok"


# ------------------------------------------------------------------ C rollout


def test_c_rollout_generation_replicas_waiting_oom(fake_backends):
    """C. observedGeneration, replica counters, waiting reasons, OOMKilled."""
    state = fake_backends
    _seed(state)
    report, _ = _run(state)
    assert report.passed is True
    assert _by_name(report)["rollout"].status == "ok"
    counters = report.evidence["deployment"]
    assert counters["observedGeneration"] == counters["generation"] == 1
    assert counters["updatedReplicas"] == counters["readyReplicas"] == counters["replicas"] == counters["availableReplicas"]

    state.backends.kube.deployments[0]["status"]["observedGeneration"] = 0
    report, _ = _run(state)
    assert report.verdict == "LANDED NOT DEPLOYED rollout"
    assert "observedGeneration" in (_by_name(report)["rollout"].detail or "")


def test_c_rollout_fails_crashloop(fake_backends):
    state = fake_backends
    seed_catalog(state, [CATALOG])
    kube = state.backends.kube
    _pod(kube, waiting="CrashLoopBackOff", ready=False)
    _deploy(kube)
    report, _ = _run(state)
    assert report.verdict == "LANDED NOT DEPLOYED rollout"
    assert "CrashLoopBackOff" in (_by_name(report)["rollout"].detail or "")


# ------------------------------------------------------------------ D ready


def test_d_ready_two_snapshots_same_pods(fake_backends):
    """D. Two snapshots one probe period apart; same names, ready, restartCount unchanged."""
    state = fake_backends
    _seed(state)
    clock = FakeClock()
    report, clock = _run(state, clock=clock)
    assert report.passed is True
    assert _by_name(report)["ready"].status == "ok"
    assert 5 in clock.sleeps
    snap = report.evidence["ready"]
    assert set(snap["snapshot1"]) == set(snap["snapshot2"]) == {POD}
    assert snap["snapshot1"][POD]["ready"] is True
    assert snap["snapshot1"][POD]["restarts"] == snap["snapshot2"][POD]["restarts"]
    assert snap["period_s"] == 5


def test_d_ready_requires_explicit_ready_condition(fake_backends):
    """D. A pod with ready containers but no Ready condition is not ready."""
    state = fake_backends
    seed_catalog(state, [CATALOG])
    kube = state.backends.kube
    pod = _pod(kube, ready=True)
    pod["status"]["conditions"] = []
    pod["status"]["containerStatuses"][0]["ready"] = True
    _deploy(kube)
    state.backends.loki.responses["/loki/api/v1/query_range"] = _loki(POD)
    report, _ = _run(state)
    assert report.passed is False
    assert report.verdict == "LANDED NOT DEPLOYED ready"
    assert POD in (_by_name(report)["ready"].detail or "")


def test_d_ready_fails_when_pod_replaced_or_restarts(fake_backends):
    state = fake_backends
    _seed(state)
    kube = state.backends.kube

    def sleeper_replace(seconds: float):
        clock.t += seconds
        kube.pods.clear()
        _pod(kube, name="demo-web-6b8f9c7d5-other")

    clock = FakeClock()
    report, _ = _run(state, clock=clock, sleeper=sleeper_replace)
    assert report.verdict == "LANDED NOT DEPLOYED ready"
    detail = _by_name(report)["ready"].detail or ""
    assert POD in detail or "demo-web-6b8f9c7d5-other" in detail

    kube.pods.clear()
    kube.deployments.clear()
    _pod(kube, restarts=0)
    _deploy(kube)

    def sleeper_restart(seconds: float):
        clock2.t += seconds
        kube.pods[0]["status"]["containerStatuses"][0]["restartCount"] = 2

    clock2 = FakeClock()
    report, _ = _run(state, clock=clock2, sleeper=sleeper_restart)
    assert report.verdict == "LANDED NOT DEPLOYED ready"
    assert "restartCount" in (_by_name(report)["ready"].detail or "")
    assert POD in (_by_name(report)["ready"].detail or "")


# ------------------------------------------------------------------ E log


def test_e_log_loki_line_from_new_pod_in_set(fake_backends):
    """E. A Loki line counts only from a pod in the set created at or after since."""
    state = fake_backends
    _seed(state, loki=_loki(POD))
    report, clock = _run(state)
    assert report.passed is True, report
    assert _by_name(report)["log"].status == "ok"
    assert report.evidence["log"]["pod"] == POD
    params = state.backends.loki.calls[-1]["params"]
    assert params["query"] == '{namespace="demo-prod", pod=~"demo-web-.*"}'
    assert params["direction"] == "forward"
    assert params["limit"] == 1000
    assert params["start"] == str(SINCE * 1_000_000_000)

    state.backends.loki.responses["/loki/api/v1/query_range"] = _loki("demo-web-someone-else")
    report, _ = _run(state, timeout_s=0)
    assert report.verdict == "LANDED NOT DEPLOYED log"


def test_e_log_ignores_pod_created_before_since_and_pages(fake_backends):
    """An old pod's line never vouches for new pods: with a new pod in the set, only a
    line from a pod born since the ship counts. (A set made ONLY of old pods is the
    reused-pod case below, docs/V1.md §8.)"""
    state = fake_backends
    seed_catalog(state, [CATALOG])
    kube = state.backends.kube
    _pod(kube, name="demo-web-old-aaaa", created="2020-01-01T00:00:00Z")
    _pod(kube)
    _deploy(kube, replicas=2, ready=2, updated=2, available=2)
    state.backends.loki.responses["/loki/api/v1/query_range"] = _loki("demo-web-old-aaaa")
    report, _ = _run(state, timeout_s=0)
    assert report.verdict == "LANDED NOT DEPLOYED log"

    kube.pods.clear()
    kube.deployments.clear()
    state.backends.loki.calls.clear()
    _pod(kube)
    _deploy(kube)
    n = {"i": 0}

    def paged(call):
        n["i"] += 1
        if n["i"] == 1:
            return _loki_n("demo-web-old-aaaa", 1000)
        return _loki(POD)

    state.backends.loki.responses["/loki/api/v1/query_range"] = paged
    report, clock = _run(state)
    assert report.passed is True, report
    assert n["i"] == 2
    second = [c for c in state.backends.loki.calls if c["path"] == "/loki/api/v1/query_range"][1]
    assert int(second["params"]["start"]) == 1_710_000_000_000_000_000 + 999 + 1
    assert clock.sleeps == [5]


# ------------------------------------------------------------------ web rule


def test_web_non_prod_hits_in_cluster_service(fake_backends):
    state = fake_backends
    _seed(state, env=ENV_DEV)
    # Loki stream is still selected by namespace demo-dev.
    state.backends.loki.responses["/loki/api/v1/query_range"] = _loki(POD)
    http_get = _http_ok()
    report, _ = _run(state, env=ENV_DEV, http_get=http_get)
    assert report.passed is True, report
    assert http_get.calls == ["http://demo-web.demo-dev.svc:8080/readyz"]
    assert report.evidence["http"]["url"] == "http://demo-web.demo-dev.svc:8080/readyz"
    assert report.evidence["http"]["status"] == 200
    assert "user" not in report.evidence["http"]["url"]


def test_web_prod_hits_public_https(fake_backends):
    state = fake_backends
    _seed(state, env=ENV_PROD)
    http_get = _http_ok()
    report, _ = _run(state, env=ENV_PROD, http_get=http_get)
    assert report.passed is True, report
    assert http_get.calls == [f"https://{PROD_HOST}/"]
    assert report.evidence["http"]["url"] == f"https://{PROD_HOST}/"
    assert report.evidence["http"]["status"] == 200


def test_web_prod_under_a_path_hits_its_own_door(fake_backends):
    """docs/V1.md §8 (S02): a component served under /api has no router at `/`; the root
    read Traefik's 404 and halted a good prod ship. The check requests path + probePath."""
    import copy
    state = fake_backends
    _seed(state, env=ENV_PROD)
    catalog = copy.deepcopy(CATALOG)
    catalog["components"][0]["path"] = "/api"
    seed_catalog(state, [catalog])
    http_get = _http_ok()
    report, _ = _run(state, env=ENV_PROD, http_get=http_get)
    assert report.passed is True, report
    assert http_get.calls == [f"https://{PROD_HOST}/api/healthz"]
    assert report.evidence["http"]["url"] == f"https://{PROD_HOST}/api/healthz"


def _generated_app(state, hosts) -> None:
    import yaml
    apps = state.backends.instance_root / "clusters" / "apps"
    apps.mkdir(parents=True, exist_ok=True)
    (apps / "demo-prod.yaml").write_text(yaml.safe_dump({
        "apiVersion": "argoproj.io/v1alpha1", "kind": "Application", "metadata": {"name": "demo-prod"},
        "spec": {"destination": {"namespace": "demo-prod"},
                 "source": {"helm": {"valuesObject": {"project": "demo", "env": "prod", "hostnames": hosts}}}}}))


def test_web_prod_with_the_default_hostname_hits_the_generated_door(fake_backends):
    """QA of v1.0.0, F1: an environment that names no hostname gets `<id>.<domain>` from
    the generator and serves there; the first prod ship of such a project was failed
    with "no public hostname in the catalog" and its other components were skipped."""
    import copy
    state = fake_backends
    _seed(state, env=ENV_PROD)
    catalog = copy.deepcopy(CATALOG)
    catalog["environments"]["prod"] = {}
    seed_catalog(state, [catalog])
    _generated_app(state, ["demo.example.test"])
    http_get = _http_ok()
    report, _ = _run(state, env=ENV_PROD, http_get=http_get)
    assert report.passed is True, report
    assert http_get.calls == ["https://demo.example.test/"]


def test_web_prod_that_declares_no_door_is_asked_inside_the_cluster(fake_backends):
    """`hostnames: []` and `expose: false` declare no public door: no hostname is invented
    for the check, and the ship is not failed for lacking one; the Service is asked."""
    import copy
    state = fake_backends
    for envcfg in ({"hostnames": []}, {"expose": False}):
        _seed(state, env=ENV_PROD)
        catalog = copy.deepcopy(CATALOG)
        catalog["environments"]["prod"] = envcfg
        seed_catalog(state, [catalog])
        _generated_app(state, ["stale.example.test"])
        http_get = _http_ok()
        report, _ = _run(state, env=ENV_PROD, http_get=http_get)
        assert report.passed is True, report
        assert http_get.calls == ["http://demo-web.demo-prod.svc:8080/readyz"]


def test_worker_skips_http(fake_backends):
    state = fake_backends
    _seed(state, component=WORKER)
    http_get = _http_ok()
    report, _ = _run(state, component=WORKER, http_get=http_get)
    assert report.passed is True, report
    assert http_get.calls == []
    assert "http" not in report.evidence
    assert "skipped" in (_by_name(report)["log"].detail or "")


# ------------------------------------------------------------------ verdict


def test_verdict_landed_not_deployed_names_first_failing_assertion(fake_backends):
    state = fake_backends
    seed_catalog(state, [CATALOG])
    kube = state.backends.kube
    _pod(kube, image=OLD_IMAGE)
    _deploy(kube, observed=0, generation=3)
    report, _ = _run(state)
    assert report.passed is False
    assert report.verdict == "LANDED NOT DEPLOYED image"
    by = _by_name(report)
    assert by["image"].status == "failed"
    assert by["rollout"].status == "skipped"
    assert by["ready"].status == "skipped"
    assert by["log"].status == "skipped"
    assert [s.name for s in report.steps] == ["image", "rollout", "ready", "log"]


def test_backend_error_becomes_failed_step_with_what_failed(fake_backends):
    state = fake_backends
    _seed(state)
    state.backends.loki.responses["/loki/api/v1/query_range"] = BackendError(
        status=503, body="unavailable",
        what_failed="ORBITD_LOKI_URL returned 503",
        env_var="ORBITD_LOKI_URL",
    )
    report, _ = _run(state)
    assert report.passed is False
    assert report.verdict == "LANDED NOT DEPLOYED log"
    assert (_by_name(report)["log"].detail or "") == "ORBITD_LOKI_URL returned 503"


def test_loki_unconfigured_uses_envelope_what_failed(fake_backends):
    state = fake_backends
    _seed(state)

    class DeadLoki:
        env_var = "ORBITD_LOKI_URL"

        def require(self):
            raise BackendUnconfigured("ORBITD_LOKI_URL")

    state.backends.loki = DeadLoki()
    report, _ = _run(state)
    assert report.verdict == "LANDED NOT DEPLOYED log"
    assert (_by_name(report)["log"].detail or "") == "ORBITD_LOKI_URL is not set"


def test_catalog_read_error_fails_log(fake_backends):
    state = fake_backends
    _seed(state)
    root = state.backends.instance_root
    (root / "catalog" / "projects.yaml").write_text("not: [valid")
    # yaml may still parse; force a read error instead
    (root / "catalog" / "projects.yaml").unlink()
    report, _ = _run(state)
    assert report.verdict == "LANDED NOT DEPLOYED log"
    assert "catalog" in (_by_name(report)["log"].detail or "").lower() or "reading" in (_by_name(report)["log"].detail or "")


def test_assert_deployed_does_not_raise(fake_backends):
    state = fake_backends
    seed_catalog(state, [CATALOG])

    class Boom:
        def list_namespaced_pod(self, *a, **k):
            raise RuntimeError("kube exploded")

    state.backends.kube.core = Boom()
    report, _ = _run(state)
    assert report.passed is False
    assert "LANDED NOT DEPLOYED" in report.verdict


def test_e_log_a_set_of_only_reused_pods_is_asked_for_a_recent_line_and_may_be_quiet(fake_backends):
    """docs/V1.md §8 (S04): a rollback to a ReplicaSet whose pod never stopped serving
    creates no pod, so no pod is born since the ship. A recent line (within
    REUSED_LOG_WINDOW_S before the ship) from those very pods is looked for and named,
    and a line from some other pod is never counted as theirs. When they wrote nothing
    they are quiet, not broken: on the test box (v1.0.1-rc2) two rollbacks of a dev
    service nobody had called for an hour ended `LANDED NOT DEPLOYED log` although the
    rollback had landed and the pod was Ready."""
    from orbitd.assertions import REUSED_LOG_WINDOW_S
    state = fake_backends
    seed_catalog(state, [CATALOG])
    kube = state.backends.kube
    _pod(kube, created="2020-01-01T00:00:00Z")
    _deploy(kube)
    state.backends.loki.responses["/loki/api/v1/query_range"] = _loki(POD)
    report, _ = _run(state)
    assert _by_name(report)["log"].status == "ok", report
    assert report.evidence["log"]["reused"] is True
    params = [c for c in state.backends.loki.calls if c["path"] == "/loki/api/v1/query_range"][-1]["params"]
    assert params["start"] == str((SINCE - REUSED_LOG_WINDOW_S) * 1_000_000_000)

    state.backends.loki.responses["/loki/api/v1/query_range"] = _loki("demo-web-someone-else")
    http_get = _http_ok()
    report, clock = _run(state, env=ENV_PROD, http_get=http_get)
    assert report.passed is True and report.verdict == "DEPLOYED", report
    step = _by_name(report)["log"]
    assert step.status == "ok" and "not restarted and wrote nothing in the last 15 min" in step.detail
    assert report.evidence["log"] == {"reused": True, "quiet": True}, "another pod's line was counted as theirs"
    assert len(http_get.calls) == 1, "the door is still asked"
    assert sum(clock.sleeps) <= 10, "it waited for lines that can only be in the past"


def test_e_log_a_set_with_a_new_pod_still_owes_a_line_from_it(fake_backends):
    """Quiet is for pods that were not restarted. A pod born since the ship that writes
    nothing is the failure the step exists for."""
    state = fake_backends
    seed_catalog(state, [CATALOG])
    kube = state.backends.kube
    _pod(kube)
    _deploy(kube)
    state.backends.loki.responses["/loki/api/v1/query_range"] = _loki("demo-web-someone-else")
    report, _ = _run(state, timeout_s=0)
    assert report.verdict == "LANDED NOT DEPLOYED log"
    assert "new pods" in _by_name(report)["log"].detail
