#!/usr/bin/env python3
"""
Behavioural tests for scripts/assert-deployed.sh.

No cluster, no credentials, no network: kubectl, curl, sleep and date are
stub executables on PATH (a temp dir). The instance catalog and config are
synthetic fixtures. Every expectation is a hand-derived literal.

Required shape (the job spec):
  one test per assertion passing,
  one per assertion failing with the final line LANDED NOT DEPLOYED <name>,
  one for the web-only HTTP check being skipped for a worker,
  plus one test per exact-semantics rule that would fail before the repair.
"""
import json
import os
import pathlib
import shutil
import subprocess
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "assert-deployed.sh"

PROJECT = "portal"
ENV = "prod"
COMPONENT = "web"
WORKER = "importer"
TAG = "main-deadbee"
NS = "%s-%s" % (PROJECT, ENV)
IMAGE = "ghcr.io/fixture-org/%s-%s:%s" % (PROJECT, COMPONENT, TAG)
WORKER_IMAGE = "ghcr.io/fixture-org/%s-%s:%s" % (PROJECT, WORKER, TAG)
# Chart service.name = <project>-<component>; pods are <name>-<rs-hash>-<pod>.
RS = "portal-web-6b8f9c7d5"
POD = "portal-web-6b8f9c7d5-xk2lm"
WORKER_RS = "portal-importer-6b8f9c7d5"
WORKER_POD = "portal-importer-6b8f9c7d5-xk2lm"
HOST = "portal.fixture.org"
RS_CREATED = "2026-01-01T00:00:00Z"
POD_CREATED = "2026-01-01T00:00:00Z"
DEFAULT_SINCE = "1710000000"

CONFIG_YAML = """\
instance:
  name: fixtureinst
git:
  repoUrl: git@github.com:fixture-org/fixture-instance.git
  branch: main
  github_org: fixture-org
domains:
  primary: fixture.org
  dev: dev.fixture.org
  dnsZones: [fixture.org]
registry: ghcr.io/fixture-org
nodes:
  - name: fixture-node-1
    ip: 198.51.100.10
    role: control-plane
secrets:
  provider: infisical-cloud
  projectSlug: fixture-slug
contacts:
  email: ops@fixture.org
"""

WEB_CATALOG = """\
version: 3
projects:
  - id: portal
    environments:
      prod: {}
    components:
      - name: web
        kind: web
        image: ghcr.io/fixture-org/portal-web
        port: 8080
"""

WORKER_CATALOG = """\
version: 3
projects:
  - id: portal
    environments:
      prod: {}
    components:
      - name: importer
        kind: worker
        image: ghcr.io/fixture-org/portal-importer
"""

KUBECTL_STUB = r"""#!/usr/bin/env bash
echo "$*" >> "$STUB_DIR/kubectl.log"
if [ -f "$STUB_DIR/kubectl.fail" ]; then
  echo "connection refused" >&2
  exit 1
fi
case " $* " in
  *" get pods "*|*" get pod "*)
    n=0
    [ -f "$STUB_DIR/pods.n" ] && n=$(cat "$STUB_DIR/pods.n")
    n=$((n + 1))
    echo "$n" > "$STUB_DIR/pods.n"
    if [ "$n" -ge 2 ] && [ -f "$STUB_DIR/pods2.json" ]; then
      cat "$STUB_DIR/pods2.json"
    else
      cat "$STUB_DIR/pods.json"
    fi
    exit 0
    ;;
  *" get deploy "*|*" get deployment "*)
    cat "$STUB_DIR/deploy.json"
    exit 0
    ;;
  *" get rs "*|*" get replicaset "*|*" get replicasets "*)
    cat "$STUB_DIR/rs.json"
    exit 0
    ;;
esac
echo "unexpected kubectl: $*" >&2
exit 1
"""

CURL_STUB = r"""#!/usr/bin/env bash
echo "$*" >> "$STUB_DIR/curl.log"
cat > "$STUB_DIR/curl.stdin"
case "$*" in
  *query_range*|*loki/api*)
    if [ -f "$STUB_DIR/curl.fail" ]; then
      echo "curl: failed to connect" >&2
      exit 1
    fi
    n=0
    [ -f "$STUB_DIR/loki.n" ] && n=$(cat "$STUB_DIR/loki.n")
    n=$((n + 1))
    echo "$n" > "$STUB_DIR/loki.n"
    if [ -f "$STUB_DIR/loki.empty_until" ]; then
      until_n=$(cat "$STUB_DIR/loki.empty_until")
      if [ "$n" -le "$until_n" ]; then
        printf '%s' '{"status":"success","data":{"resultType":"streams","result":[]}}'
        exit 0
      fi
    fi
    if [ -f "$STUB_DIR/loki.$n.json" ]; then
      cat "$STUB_DIR/loki.$n.json"
    elif [ -f "$STUB_DIR/loki.json" ]; then
      cat "$STUB_DIR/loki.json"
    else
      printf '%s' '{"status":"success","data":{"resultType":"streams","result":[]}}'
    fi
    exit 0
    ;;
esac
url=""
for a in "$@"; do
  case "$a" in https://*) url="$a";; esac
done
if [ -n "$url" ]; then
  echo "$url" >> "$STUB_DIR/http.log"
  if [ -f "$STUB_DIR/http_code" ]; then
    tr -d '\n' < "$STUB_DIR/http_code"
  else
    printf '200'
  fi
  exit 0
fi
echo "unexpected curl: $*" >&2
exit 1
"""

SLEEP_STUB = r"""#!/usr/bin/env bash
echo "$*" >> "$STUB_DIR/sleep.log"
exit 0
"""

DATE_STUB = r"""#!/usr/bin/env bash
if [ "$1" = "+%s" ] && [ -f "$STUB_DIR/now.s" ]; then
  tr -d ' \n' < "$STUB_DIR/now.s"
  echo
  echo $(( $(tr -d ' \n' < "$STUB_DIR/now.s") + 5 )) > "$STUB_DIR/now.s"
  exit 0
fi
exec /usr/bin/date "$@"
"""


def _write(path: pathlib.Path, text: str, executable: bool = False) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text if text.endswith("\n") else text + "\n")
    if executable:
        path.chmod(0o755)


def _pod(name, image, ready=True, oom=False, period=5, phase="Running", rs=RS,
         component=COMPONENT, restart=0, waiting=None, deleting=False,
         created=POD_CREATED, image_id=None):
    last_state = {}
    if oom:
        last_state = {"terminated": {"reason": "OOMKilled"}}
    if waiting:
        state = {"waiting": {"reason": waiting}}
    elif oom:
        state = {"terminated": {"reason": "OOMKilled"}}
    else:
        state = {"running": {"startedAt": "2026-01-01T00:00:00Z"}}
    spec_container = {"name": component, "image": image}
    if period is not None:
        spec_container["readinessProbe"] = {"periodSeconds": period}
    pth = rs.split("-")[-1]
    meta = {
        "name": name,
        "namespace": NS,
        "creationTimestamp": created,
        "labels": {
            "app.kubernetes.io/name": component,
            "app.kubernetes.io/instance": PROJECT,
            "pod-template-hash": pth,
        },
        "ownerReferences": [{"kind": "ReplicaSet", "name": rs, "controller": True}],
    }
    if deleting:
        meta["deletionTimestamp"] = "2026-01-01T00:01:00Z"
    return {
        "metadata": meta,
        "spec": {"containers": [spec_container]},
        "status": {
            "phase": phase,
            "conditions": [{"type": "Ready", "status": "True" if ready else "False"}],
            "containerStatuses": [{
                "name": component,
                "ready": ready,
                "restartCount": restart,
                "lastState": last_state,
                "state": state,
                **({"imageID": image_id} if image_id is not None else {}),
            }],
        },
    }


def _deploy(updated=1, ready=1, replicas=1, name=None, generation=1,
            observed=1, available=None):
    if available is None:
        available = ready
    return {
        "metadata": {
            "name": name or "%s-%s" % (PROJECT, COMPONENT),
            "generation": generation,
        },
        "spec": {"replicas": replicas},
        "status": {
            "replicas": replicas,
            "updatedReplicas": updated,
            "readyReplicas": ready,
            "availableReplicas": available,
            "observedGeneration": observed,
        },
    }


def _rs_obj(name=RS, revision="2", created=RS_CREATED, component=COMPONENT):
    pth = name.split("-")[-1]
    return {
        "metadata": {
            "name": name,
            "creationTimestamp": created,
            "labels": {
                "app.kubernetes.io/name": component,
                "app.kubernetes.io/instance": PROJECT,
                "pod-template-hash": pth,
            },
            "annotations": {"deployment.kubernetes.io/revision": str(revision)},
        },
        "status": {"replicas": 1, "readyReplicas": 1},
    }


def _rs(name=RS, revision="2", created=RS_CREATED, component=COMPONENT):
    return {"items": [_rs_obj(name, revision, created, component)]}


def _loki(pod, line="listening"):
    return {
        "status": "success",
        "data": {
            "resultType": "streams",
            "result": [{
                "stream": {"namespace": NS, "pod": pod},
                "values": [["1710000000000000000", line]],
            }],
        },
    }


def _loki_n(pod, n, start_ts=1710000000000000000):
    values = [[str(start_ts + i), "line-%d" % i] for i in range(n)]
    return {
        "status": "success",
        "data": {
            "resultType": "streams",
            "result": [{
                "stream": {"namespace": NS, "pod": pod},
                "values": values,
            }],
        },
    }


class AssertDeployedTest(unittest.TestCase):

    def setUp(self):
        self.tmp = pathlib.Path(tempfile.mkdtemp(prefix="assert-deployed-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.stubs = self.tmp / "stubs"
        _write(self.stubs / "kubectl", KUBECTL_STUB, executable=True)
        _write(self.stubs / "curl", CURL_STUB, executable=True)
        _write(self.stubs / "sleep", SLEEP_STUB, executable=True)
        _write(self.stubs / "date", DATE_STUB, executable=True)
        _write(self.tmp / "instance" / "config.yaml", CONFIG_YAML)
        _write(self.tmp / "kubeconfig", "dummy")
        _write(self.tmp / "now.s", DEFAULT_SINCE)
        self._catalog(WEB_CATALOG)
        self._cluster()

    def _catalog(self, text):
        _write(self.tmp / "catalog" / "projects.yaml", text)

    def _cluster(self, pod=None, deploy=None, rs=None, loki=None,
                 pods2=None, http_code=None):
        if isinstance(pod, list):
            items = pod
        else:
            items = [pod or _pod(POD, IMAGE)]
        deploy = deploy if deploy is not None else _deploy()
        rs = rs if rs is not None else _rs()
        loki = loki if loki is not None else _loki(POD)
        _write(self.tmp / "pods.json", json.dumps({"items": items}))
        if pods2 is not None:
            if isinstance(pods2, list):
                _write(self.tmp / "pods2.json", json.dumps({"items": pods2}))
            else:
                _write(self.tmp / "pods2.json", json.dumps({"items": [pods2]}))
        _write(self.tmp / "deploy.json", json.dumps(deploy))
        _write(self.tmp / "rs.json", json.dumps(rs))
        _write(self.tmp / "loki.json", json.dumps(loki))
        if http_code is not None:
            _write(self.tmp / "http_code", str(http_code))

    def _run(self, component=COMPONENT, tag=TAG, extra=None, env_extra=None):
        env = dict(os.environ)
        env["PATH"] = str(self.stubs) + os.pathsep + env.get("PATH", "")
        env["KUBECONFIG"] = str(self.tmp / "kubeconfig")
        env["ORBIT_INSTANCE_CONFIG"] = str(self.tmp / "instance" / "config.yaml")
        env["ORBIT_CATALOG"] = str(self.tmp / "catalog" / "projects.yaml")
        env["STUB_DIR"] = str(self.tmp)
        env["ORBIT_LOKI_URL"] = "http://loki.loki.svc:3100"
        if env_extra:
            env.update(env_extra)
        cmd = ["bash", str(SCRIPT), PROJECT, ENV, component, tag]
        extra = list(extra) if extra is not None else ["--since", DEFAULT_SINCE]
        cmd.extend(extra)
        return subprocess.run(
            cmd, cwd=str(ROOT), capture_output=True, text=True, env=env)

    def _lines(self, proc):
        return [ln for ln in (proc.stdout + proc.stderr).splitlines() if ln.strip()]

    def _step(self, proc, name):
        for ln in self._lines(proc):
            if ln.startswith("STEP %s " % name):
                return ln
        self.fail("no STEP %s line in:\n%s" % (name, proc.stdout + proc.stderr))

    def _landed(self, proc):
        landed = [ln for ln in self._lines(proc) if ln.startswith("LANDED NOT DEPLOYED ")]
        self.assertTrue(landed, "missing LANDED NOT DEPLOYED in:\n%s" % (proc.stdout + proc.stderr))
        return landed[-1]

    def _assert_landed_last(self, proc, name):
        self.assertEqual(self._landed(proc), "LANDED NOT DEPLOYED %s" % name)
        stdout_lines = [ln for ln in proc.stdout.splitlines() if ln.strip()]
        self.assertTrue(stdout_lines, proc.stdout + proc.stderr)
        self.assertEqual(stdout_lines[-1], "LANDED NOT DEPLOYED %s" % name)

    def test_image_passes(self):
        proc = self._run()
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        image_step = self._step(proc, "image")
        self.assertRegex(image_step, r"^STEP image OK ")
        self.assertIn(IMAGE, image_step)

    def test_image_fails_different_repo_same_tag(self):
        """Same tag on a different repo is not the shipped reference."""
        wrong = "wrong/repo:%s" % TAG
        self._cluster(pod=_pod(POD, wrong))
        proc = self._run()
        self.assertNotEqual(proc.returncode, 0, proc.stdout)
        self.assertEqual(self._step(proc, "image"), "STEP image FAIL %s=%s" % (POD, wrong))
        self._assert_landed_last(proc, "image")
        steps = [ln for ln in self._lines(proc) if ln.startswith("STEP ")]
        self.assertEqual(len(steps), 1, "must stop at the first failure:\n%s" % proc.stdout)

    def test_image_digest_form_matching_repo_passes(self):
        """Digest form of the expected repo is accepted when imageID names that repo."""
        digest = "sha256:" + ("a" * 64)
        img = "ghcr.io/fixture-org/%s-%s@%s" % (PROJECT, COMPONENT, digest)
        image_id = "docker-pullable://ghcr.io/fixture-org/%s-%s@%s" % (
            PROJECT, COMPONENT, digest)
        self._cluster(pod=_pod(POD, img, image_id=image_id))
        proc = self._run()
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        image_step = self._step(proc, "image")
        self.assertRegex(image_step, r"^STEP image OK ")
        self.assertIn(IMAGE, image_step)

    def test_rollout_passes(self):
        proc = self._run()
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertRegex(self._step(proc, "rollout"), r"^STEP rollout OK ")

    def test_ready_passes(self):
        proc = self._run()
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertRegex(self._step(proc, "ready"), r"^STEP ready OK ")
        slept = (self.tmp / "sleep.log").read_text().split()
        self.assertEqual(slept, ["5"],
                         "ready must sleep readinessProbe.periodSeconds, got %r" % slept)

    def test_log_passes(self):
        proc = self._run()
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertRegex(self._step(proc, "log"), r"^STEP log OK ")
        http = (self.tmp / "http.log").read_text().split()
        self.assertEqual(http, ["https://%s/" % HOST], proc.stdout)
        curl = (self.tmp / "curl.log").read_text()
        self.assertIn("query_range", curl)
        self.assertIn('pod=~"%s-.*"' % ("%s-%s" % (PROJECT, COMPONENT)), curl)
        self.assertIn("direction=forward", curl)
        self.assertIn("limit=1000", curl)
        self.assertNotIn("direction=backward", curl)
        kubectl = (self.tmp / "kubectl.log").read_text()
        self.assertIn(
            "app.kubernetes.io/name=%s,app.kubernetes.io/instance=%s" % (COMPONENT, PROJECT),
            kubectl)

    def test_image_fails_wrong_tag(self):
        wrong = IMAGE.replace(TAG, "main-oldold")
        self._cluster(pod=_pod(POD, wrong))
        proc = self._run()
        self.assertNotEqual(proc.returncode, 0, proc.stdout)
        self.assertEqual(self._step(proc, "image"), "STEP image FAIL %s=%s" % (POD, wrong))
        self._assert_landed_last(proc, "image")
        steps = [ln for ln in self._lines(proc) if ln.startswith("STEP ")]
        self.assertEqual(len(steps), 1, "must stop at the first failure:\n%s" % proc.stdout)

    def test_rollout_fails_replica_mismatch(self):
        self._cluster(deploy=_deploy(updated=1, ready=0, replicas=1))
        proc = self._run()
        self.assertNotEqual(proc.returncode, 0, proc.stdout)
        self.assertRegex(self._step(proc, "rollout"), r"^STEP rollout FAIL ")
        self._assert_landed_last(proc, "rollout")

    def test_ready_fails_after_probe_period(self):
        self._cluster(pods2=_pod(POD, IMAGE, ready=False))
        proc = self._run()
        self.assertNotEqual(proc.returncode, 0, proc.stdout)
        self.assertRegex(self._step(proc, "ready"), r"^STEP ready FAIL ")
        self.assertIn(POD, self._step(proc, "ready"))
        self._assert_landed_last(proc, "ready")
        self.assertTrue((self.tmp / "sleep.log").exists(),
                        "ready must sleep the probe period before re-checking")

    def test_log_fails_without_loki_line(self):
        self._cluster(loki={"status": "success", "data": {"resultType": "streams", "result": []}})
        proc = self._run()
        self.assertNotEqual(proc.returncode, 0, proc.stdout)
        self.assertRegex(self._step(proc, "log"), r"^STEP log FAIL ")
        self._assert_landed_last(proc, "log")

    def test_worker_skips_http(self):
        self._catalog(WORKER_CATALOG)
        self._cluster(
            pod=_pod(WORKER_POD, WORKER_IMAGE, rs=WORKER_RS, component=WORKER),
            deploy=_deploy(name="%s-%s" % (PROJECT, WORKER)),
            rs=_rs(name=WORKER_RS, component=WORKER),
            loki=_loki(WORKER_POD),
        )
        proc = self._run(component=WORKER)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertRegex(self._step(proc, "log"), r"^STEP log OK ")
        self.assertIn("skipped", self._step(proc, "log"))
        self.assertFalse((self.tmp / "http.log").exists(),
                         "kind worker must not probe a public host:\n%s" %
                         ((self.tmp / "curl.log").read_text() if (self.tmp / "curl.log").exists() else proc.stdout))
        curl = (self.tmp / "curl.log").read_text()
        self.assertIn("query_range", curl)
        self.assertIn('pod=~"%s-.*"' % ("%s-%s" % (PROJECT, WORKER)), curl)
        self.assertNotIn("https://", curl)

    # ── exact semantics (review r3) ──────────────────────────────────────────

    def test_image_fails_active_old_replicaset_pod(self):
        """A. Active old-ReplicaSet pods are in the set; a wrong image fails."""
        old_rs = "portal-web-aaaaaaa"
        old_image = IMAGE.replace(TAG, "main-oldold")
        leftover = _pod("portal-web-aaaaaaa-live", old_image, rs=old_rs, deleting=False)
        current = _pod(POD, IMAGE)
        self._cluster(
            pod=[leftover, current],
            rs={"items": [_rs_obj(old_rs, "1"), _rs_obj(RS, "2")]},
        )
        proc = self._run()
        self.assertNotEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertEqual(
            self._step(proc, "image"),
            "STEP image FAIL %s=%s" % ("portal-web-aaaaaaa-live", old_image))
        self._assert_landed_last(proc, "image")

    def test_image_ignores_terminating_old_replicaset_pod(self):
        """A. Terminating pods are ignored; nothing else is."""
        old_rs = "portal-web-aaaaaaa"
        old_image = IMAGE.replace(TAG, "main-oldold")
        terminating = _pod("portal-web-aaaaaaa-term", old_image, rs=old_rs, deleting=True)
        current = _pod(POD, IMAGE)
        self._cluster(
            pod=[terminating, current],
            rs={"items": [_rs_obj(old_rs, "1"), _rs_obj(RS, "2")]},
        )
        proc = self._run()
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertRegex(self._step(proc, "image"), r"^STEP image OK ")

    def test_image_fails_empty_pod_set(self):
        """B. Empty pod set fails image."""
        self._cluster(pod=[])
        proc = self._run()
        self.assertNotEqual(proc.returncode, 0, proc.stdout)
        self.assertRegex(self._step(proc, "image"), r"^STEP image FAIL ")
        self._assert_landed_last(proc, "image")

    def test_image_ignores_other_named_containers(self):
        """B. Only the container named after the component is checked."""
        p = _pod(POD, IMAGE)
        p["spec"]["containers"].append({"name": "sidecar", "image": "sidecar:old"})
        self._cluster(pod=p)
        proc = self._run()
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertRegex(self._step(proc, "image"), r"^STEP image OK ")

    def test_rollout_fails_observed_generation(self):
        """C. observedGeneration != generation fails rollout."""
        self._cluster(deploy=_deploy(generation=3, observed=2))
        proc = self._run()
        self.assertNotEqual(proc.returncode, 0, proc.stdout)
        self.assertRegex(self._step(proc, "rollout"), r"^STEP rollout FAIL ")
        self.assertIn("observedGeneration", self._step(proc, "rollout"))
        self._assert_landed_last(proc, "rollout")

    def test_rollout_fails_available_replicas(self):
        """C. availableReplicas must equal the other replica fields."""
        self._cluster(deploy=_deploy(updated=1, ready=1, replicas=1, available=0))
        proc = self._run()
        self.assertNotEqual(proc.returncode, 0, proc.stdout)
        self.assertRegex(self._step(proc, "rollout"), r"^STEP rollout FAIL ")
        self._assert_landed_last(proc, "rollout")

    def test_rollout_fails_crashloop(self):
        """C. CrashLoopBackOff fails rollout even when replica counts match."""
        self._cluster(pod=_pod(POD, IMAGE, waiting="CrashLoopBackOff"))
        proc = self._run()
        self.assertNotEqual(proc.returncode, 0, proc.stdout)
        self.assertRegex(self._step(proc, "rollout"), r"^STEP rollout FAIL ")
        self.assertEqual(self._landed(proc), "LANDED NOT DEPLOYED rollout")
        self.assertIn("CrashLoopBackOff", self._step(proc, "rollout"))

    def test_rollout_fails_imagepull(self):
        """C. ImagePullBackOff fails rollout."""
        running = _pod(POD, IMAGE)
        pulling = _pod("portal-web-6b8f9c7d5-pull", IMAGE,
                       waiting="ImagePullBackOff", phase="Pending", ready=False)
        self._cluster(pod=[running, pulling])
        proc = self._run()
        self.assertNotEqual(proc.returncode, 0, proc.stdout)
        self.assertRegex(self._step(proc, "rollout"), r"^STEP rollout FAIL ")
        self.assertEqual(self._landed(proc), "LANDED NOT DEPLOYED rollout")
        self.assertIn("ImagePullBackOff", self._step(proc, "rollout"))

    def test_rollout_fails_errimagepull(self):
        """C. ErrImagePull fails rollout."""
        running = _pod(POD, IMAGE)
        pulling = _pod("portal-web-6b8f9c7d5-err", IMAGE,
                       waiting="ErrImagePull", phase="Pending", ready=False)
        self._cluster(pod=[running, pulling])
        proc = self._run()
        self.assertNotEqual(proc.returncode, 0, proc.stdout)
        self.assertRegex(self._step(proc, "rollout"), r"^STEP rollout FAIL ")
        self.assertIn("ErrImagePull", self._step(proc, "rollout"))
        self._assert_landed_last(proc, "rollout")

    def test_ready_fails_when_pod_replaced(self):
        """D. Snapshot 2 must keep the same pod names."""
        other = "portal-web-6b8f9c7d5-other"
        self._cluster(pod=_pod(POD, IMAGE), pods2=_pod(other, IMAGE))
        proc = self._run()
        self.assertNotEqual(proc.returncode, 0, proc.stdout)
        self.assertRegex(self._step(proc, "ready"), r"^STEP ready FAIL ")
        self.assertIn(POD, self._step(proc, "ready"))
        self._assert_landed_last(proc, "ready")

    def test_ready_fails_when_pod_added(self):
        """D. A pod joining the set during the wait fails ready, naming it."""
        extra = "portal-web-6b8f9c7d5-extra"
        self._cluster(pod=_pod(POD, IMAGE),
                      pods2=[_pod(POD, IMAGE), _pod(extra, IMAGE)])
        proc = self._run()
        self.assertNotEqual(proc.returncode, 0, proc.stdout)
        self.assertRegex(self._step(proc, "ready"), r"^STEP ready FAIL ")
        self.assertIn(extra, self._step(proc, "ready"))
        self._assert_landed_last(proc, "ready")

    def test_ready_fails_restartcount_increase(self):
        """D. restartCount unchanged across the probe period (ready, not rollout)."""
        self._cluster(pod=_pod(POD, IMAGE, restart=0),
                      pods2=_pod(POD, IMAGE, restart=2))
        proc = self._run()
        self.assertNotEqual(proc.returncode, 0, proc.stdout)
        self.assertRegex(self._step(proc, "ready"), r"^STEP ready FAIL ")
        self.assertIn(POD, self._step(proc, "ready"))
        self.assertIn("restartCount", self._step(proc, "ready"))
        self._assert_landed_last(proc, "ready")
        self.assertRegex(self._step(proc, "rollout"), r"^STEP rollout OK ")

    def test_ready_without_probe_waits_10s(self):
        """D. no readinessProbe waits 10s and says so in the STEP detail."""
        self._cluster(pod=_pod(POD, IMAGE, period=None))
        proc = self._run()
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        ready = self._step(proc, "ready")
        self.assertRegex(ready, r"^STEP ready OK ")
        self.assertIn("no readinessProbe", ready)
        self.assertIn("10", ready)
        slept = (self.tmp / "sleep.log").read_text().split()
        self.assertEqual(slept, ["10"], "no-probe ready wait must be 10s, got %r" % slept)

    def test_loki_selector_matches_chart_pod_name(self):
        """E. Loki stream selector is project-component-.*, not component-.*."""
        proc = self._run()
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        curl = (self.tmp / "curl.log").read_text()
        self.assertIn('pod=~"portal-web-.*"', curl)
        self.assertNotIn('pod=~"web-.*"', curl)
        self.assertNotIn('pod=~"web-.+"', curl)

    def test_log_since_unix_is_query_start(self):
        """E. --since unix is the Loki query start (nanoseconds)."""
        proc = self._run(extra=["--since", "1710000000"])
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        curl = (self.tmp / "curl.log").read_text()
        self.assertIn("start=1710000000000000000", curl)
        self.assertEqual(curl.count("query_range"), 1)

    def test_log_since_rfc3339(self):
        """E. --since RFC3339 is accepted."""
        proc = self._run(extra=["--since", "2024-03-09T16:00:00Z"])
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        curl = (self.tmp / "curl.log").read_text()
        self.assertIn("query_range", curl)
        self.assertIn("start=", curl)

    def test_log_polls_until_line_arrives(self):
        """E. empty Loki is retried every 5s until since+60s."""
        _write(self.tmp / "now.s", "1000000000")
        _write(self.tmp / "loki.empty_until", "2")
        proc = self._run(extra=["--since", "1000000000"])
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertRegex(self._step(proc, "log"), r"^STEP log OK ")
        curl = (self.tmp / "curl.log").read_text()
        self.assertEqual(curl.count("query_range"), 3, curl)
        slept = (self.tmp / "sleep.log").read_text().split()
        self.assertEqual(slept, ["5", "5", "5"],
                         "ready period plus two Loki poll sleeps, got %r" % slept)

    def test_log_requires_since(self):
        """E/F. --since is required; missing it is LANDED NOT DEPLOYED args, no STEP."""
        proc = self._run(extra=[])
        self.assertNotEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self._assert_landed_last(proc, "args")
        steps = [ln for ln in self._lines(proc) if ln.startswith("STEP ")]
        self.assertEqual(steps, [], "usage errors must print no STEP line:\n%s" % proc.stdout)

    def test_log_invalid_since_is_args(self):
        """F. Invalid --since is LANDED NOT DEPLOYED args before any STEP line."""
        proc = self._run(extra=["--since", "not-a-timestamp"])
        self.assertNotEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self._assert_landed_last(proc, "args")
        steps = [ln for ln in self._lines(proc) if ln.startswith("STEP ")]
        self.assertEqual(steps, [], "invalid --since must print no STEP line:\n%s" % proc.stdout)

    def test_log_ignores_line_from_pod_created_before_since(self):
        """E. A Loki line counts only if that pod's creationTimestamp >= since."""
        old = _pod(POD, IMAGE, created="2020-01-01T00:00:00Z")
        self._cluster(pod=old, loki=_loki(POD))
        proc = self._run()
        self.assertNotEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertRegex(self._step(proc, "log"), r"^STEP log FAIL ")
        self._assert_landed_last(proc, "log")

    def test_log_ignores_line_from_pod_not_in_set(self):
        """E. A Loki line whose pod is not in the set does not count."""
        self._cluster(loki=_loki("portal-web-someone-else"))
        proc = self._run()
        self.assertNotEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertRegex(self._step(proc, "log"), r"^STEP log FAIL ")
        self._assert_landed_last(proc, "log")

    def test_log_pages_past_full_unrelated_result(self):
        """E. Paging with limit=1000 reaches a later matching line without sleeping."""
        _write(self.tmp / "loki.1.json", json.dumps(_loki_n("portal-web-old-aaaa", 1000)))
        _write(self.tmp / "loki.2.json", json.dumps(_loki(POD)))
        proc = self._run()
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertRegex(self._step(proc, "log"), r"^STEP log OK ")
        curl = (self.tmp / "curl.log").read_text()
        self.assertEqual(curl.count("query_range"), 2, curl)
        slept = (self.tmp / "sleep.log").read_text().split()
        self.assertEqual(slept, ["5"],
                         "paging must not sleep between pages, got %r" % slept)

    def test_catalog_read_error_fails_log(self):
        """E. malformed catalog is LANDED NOT DEPLOYED log, not a worker skip."""
        self._catalog("[")
        proc = self._run()
        self.assertNotEqual(proc.returncode, 0, proc.stdout)
        self.assertRegex(self._step(proc, "log"), r"^STEP log FAIL ")
        self.assertEqual(self._landed(proc), "LANDED NOT DEPLOYED log")
        self.assertIn("catalog", self._step(proc, "log").lower())
        self.assertNotIn("skipped", proc.stdout)

    def test_kubectl_failure_prints_landed_on_stdout(self):
        """kubectl failure must not swallow STEP/LANDED into a substitution."""
        _write(self.tmp / "kubectl.fail", "1")
        proc = self._run()
        self.assertNotEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("STEP image FAIL", proc.stdout)
        self.assertIn("LANDED NOT DEPLOYED image", proc.stdout)

    def test_loki_curl_failure_prints_landed_on_stdout(self):
        """curl failure on Loki also prints the marker on stdout."""
        _write(self.tmp / "curl.fail", "1")
        proc = self._run()
        self.assertNotEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("STEP log FAIL", proc.stdout)
        self.assertIn("LANDED NOT DEPLOYED log", proc.stdout)

    def test_web_auth_file_sends_credential_on_stdin_not_argv(self):
        cred = "authuser:s3cret-orbit-web"
        auth = self.tmp / "web.auth"
        _write(auth, cred)
        proc = self._run(env_extra={"ORBIT_WEB_AUTH_FILE": str(auth)})
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertRegex(self._step(proc, "log"), r"^STEP log OK ")
        argv = (self.tmp / "curl.log").read_text()
        self.assertNotIn(cred, argv)
        self.assertNotRegex(argv, r"(^|\s)-u(\s|=)")
        self.assertIn("-K", argv)
        stdin = (self.tmp / "curl.stdin").read_text()
        self.assertIn('user = "%s"' % cred, stdin)

    def test_web_401_without_auth_file_names_orbit_web_auth_file(self):
        self._cluster(http_code=401)
        proc = self._run()
        self.assertNotEqual(proc.returncode, 0, proc.stdout)
        self.assertEqual(
            self._step(proc, "log"),
            "STEP log FAIL https://%s/ returned 401 (basic auth; set ORBIT_WEB_AUTH_FILE)" % HOST)
        self._assert_landed_last(proc, "log")

    def test_web_auth_credential_absent_from_output_on_failure(self):
        cred = "authuser:s3cret-orbit-web"
        auth = self.tmp / "web.auth"
        _write(auth, cred)
        self._cluster(http_code=401)
        proc = self._run(env_extra={"ORBIT_WEB_AUTH_FILE": str(auth)})
        self.assertNotEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        combined = proc.stdout + proc.stderr
        self.assertNotIn(cred, combined)
        self.assertNotIn(cred, (self.tmp / "curl.log").read_text())


if __name__ == "__main__":
    unittest.main()
