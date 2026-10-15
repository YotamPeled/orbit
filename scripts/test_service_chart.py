#!/usr/bin/env python3
"""Guard tests for the service chart's readiness/scratch rendering.

Run as `python3 scripts/test_service_chart.py`. Needs `helm` on PATH and skips
without it (the local test image has it; see scripts/local-tests.sh).

Each test writes a values file into a tmpdir and runs the real `helm template`
over `charts/service` — the same render Argo applies — then asserts on the
parsed Deployment. Rendered manifests go through PyYAML (in the local test image; see
scripts/local-tests.sh), never through a hand-rolled line scan, so
the assertions see the same structure the API server will.
"""
import json
import pathlib
import re
import shutil
import subprocess
import sys
import tempfile
import unittest

import yaml

ROOT = pathlib.Path(__file__).resolve().parent.parent
CHART = ROOT / "charts" / "service"
HELM = shutil.which("helm")

GOLDEN = ROOT / "scripts" / "testdata" / "service-chart" / "absent-keys.golden.yaml"
GOLDEN_HEADER = """\
# Golden render for test_absent_keys_render_byte_identical_to_base: the minimal
# VALUES_DEFAULTS component rendered against the pre-#43 chart (f25d15a
# _helpers.tpl + values.schema.json — the change that added the readinessPath
# and scratch keys, which change nothing for a component that sets neither).
# Regenerate: python3 scripts/test_service_chart.py --regenerate
"""

PROBE_PATH = "/livez"
READINESS_PATH = "/readyz"
SCRATCH_MOUNT = "/tmp/scratch"
SCRATCH_SIZE = "512Mi"
SCRATCH_LIMIT = "1Gi"
WORKER_DRAIN = 45
WEB_DRAIN = 40

VALUES_FULL = """\
project: charttest
env: prod
components:
  - name: worker
    kind: worker
    image: ghcr.io/example-org/charttest-worker
    port: 8080
    probePath: %s
    readinessPath: %s
    readOnlyRootFilesystem: true
    memoryLimit: %s
    scratch:
      mountPath: %s
      sizeLimit: %s
""" % (PROBE_PATH, READINESS_PATH, SCRATCH_LIMIT, SCRATCH_MOUNT, SCRATCH_SIZE)

VALUES_WORKER_DRAIN = """\
project: charttest
env: prod
components:
  - name: worker
    kind: worker
    image: ghcr.io/example-org/charttest-worker
    drain: %d
""" % WORKER_DRAIN

VALUES_WEB_DRAIN = """\
project: charttest
env: prod
components:
  - name: web
    kind: web
    image: ghcr.io/example-org/charttest-web
    port: 8080
    probePath: /healthz
    drain: %d
""" % WEB_DRAIN

VALUES_WEB_DRAIN_TOO_SHORT = """\
project: charttest
env: prod
components:
  - name: web
    kind: web
    image: ghcr.io/example-org/charttest-web
    port: 8080
    probePath: /healthz
    drain: 5
"""

VALUES_SCRATCH_TOO_LARGE = """\
project: charttest
env: prod
components:
  - name: worker
    kind: worker
    image: ghcr.io/example-org/charttest-worker
    memoryLimit: 512Mi
    scratch:
      mountPath: /tmp/scratch
      sizeLimit: 512Mi
"""

VALUES_SCRATCH_NO_MEMORY_LIMIT = """\
project: charttest
env: prod
components:
  - name: worker
    kind: worker
    image: ghcr.io/example-org/charttest-worker
    scratch:
      mountPath: /tmp/scratch
      sizeLimit: 512Mi
"""

VALUES_DEFAULTS = """\
project: charttest
env: prod
components:
  - name: web
    kind: web
    image: ghcr.io/example-org/charttest-web
    tag: main-0000001
    port: 8080
    probePath: /healthz
"""

VALUES_NO_SIZELIMIT = """\
project: charttest
env: prod
components:
  - name: worker
    kind: worker
    image: ghcr.io/example-org/charttest-worker
    scratch:
      mountPath: /tmp/scratch
"""

VALUES_READINESS_WITHOUT_PROBE = """\
project: charttest
env: prod
components:
  - name: web
    kind: web
    image: ghcr.io/example-org/charttest-web
    port: 8080
    readinessPath: /readyz
"""

VALUES_DB = """\
project: charttest
env: prod
components:
  - name: web
    kind: web
    image: ghcr.io/example-org/charttest-web
    port: 8080
    probePath: /healthz
database:
  size: 2Gi
  memory: 256Mi
"""

VALUES_DB_PARAMETERS = """\
project: charttest
env: prod
components:
  - name: web
    kind: web
    image: ghcr.io/example-org/charttest-web
    port: 8080
    probePath: /healthz
database:
  size: 2Gi
  memory: 256Mi
  parameters:
    max_connections: "60"
    shared_buffers: "64MB"
"""

VALUES_DB_EMPTY_PARAMETERS = """\
project: charttest
env: prod
components:
  - name: web
    kind: web
    image: ghcr.io/example-org/charttest-web
    port: 8080
    probePath: /healthz
database:
  size: 2Gi
  memory: 256Mi
  parameters: {}
"""

VALUES_DB_BAD_PARAMETER = """\
project: charttest
env: prod
components:
  - name: web
    kind: web
    image: ghcr.io/example-org/charttest-web
    port: 8080
    probePath: /healthz
database:
  size: 2Gi
  parameters:
    max-connections: "60"
"""

# Same shape as the generator fixture in scripts/verify/stage3.sh: the values
# are the fixture's own, and the component carries both new keys so the test
# fails if either stops passing through.
SANDBOX_CONFIG = """\
instance:
  name: example
git:
  repoUrl: git@github.com:example-org/example-instance.git
  branch: main
  github_org: example-org
domains:
  primary: example.org
  dev: dev.example.org
  dnsZones: [example.org]
registry: ghcr.io/example-org
"""

SANDBOX_CATALOG = """\
projects:
  - id: scratchtest
    name: Scratch Test
    hosted: self
    environments:
      prod: {}
    components:
      - name: api
        kind: web
        image: ghcr.io/example-org/scratchtest-api
        port: 8080
        path: /
        probePath: /livez
        readinessPath: /readyz
        readOnlyRootFilesystem: true
        drain: 40
        memoryLimit: 1Gi
        scratch: {mountPath: /tmp/scratch, sizeLimit: 512Mi}
"""


def render(values_text: str, chart: pathlib.Path = CHART) -> subprocess.CompletedProcess:
    """`helm template` over VALUES_TEXT against CHART. The tmpdir dies with the context."""
    tmp = tempfile.TemporaryDirectory()
    try:
        vals = pathlib.Path(tmp.name) / "values.yaml"
        vals.write_text(values_text)
        return subprocess.run(
            [HELM, "template", "charttest", str(chart), "-f", str(vals)],
            capture_output=True, text=True, cwd=ROOT)
    finally:
        tmp.cleanup()


def deployment(output: str) -> dict:
    """The rendered Deployment, parsed. There is exactly one per component."""
    for doc in yaml.safe_load_all(output):
        if isinstance(doc, dict) and doc.get("kind") == "Deployment":
            return doc
    raise AssertionError("rendered output contains no Deployment")


def cluster(output: str) -> dict:
    """The rendered CloudNativePG Cluster, parsed."""
    for doc in yaml.safe_load_all(output):
        if isinstance(doc, dict) and doc.get("kind") == "Cluster":
            return doc
    raise AssertionError("rendered output contains no Cluster")


def container(dep: dict) -> dict:
    """The component's only container."""
    containers = dep["spec"]["template"]["spec"]["containers"]
    assert len(containers) == 1, f"expected one container, got {len(containers)}"
    return containers[0]


def regenerate_golden() -> None:
    """Re-render GOLDEN from the pre-#43 chart. One-off maintenance only; the
    test itself never touches git — it compares against the committed file."""
    if not HELM:
        raise SystemExit("helm is not installed here")
    tmp = tempfile.TemporaryDirectory()
    try:
        shutil.copytree(CHART, pathlib.Path(tmp.name) / "service")
        for name in ("templates/_helpers.tpl", "values.schema.json"):
            shown = subprocess.run(
                ["git", "show", f"f25d15a:charts/service/{name}"],
                capture_output=True, text=True, cwd=ROOT, check=True)
            (pathlib.Path(tmp.name) / "service" / name).write_text(shown.stdout)
        proc = render(VALUES_DEFAULTS,
                      chart=pathlib.Path(tmp.name) / "service")
        if proc.returncode != 0:
            raise SystemExit(f"golden render failed: {proc.stderr}")
        GOLDEN.parent.mkdir(parents=True, exist_ok=True)
        GOLDEN.write_text(GOLDEN_HEADER + proc.stdout)
    finally:
        tmp.cleanup()


def sync_sandbox() -> tempfile.TemporaryDirectory:
    """A minimal tree scripts/catalog-sync.py runs in: the script, a catalog
    with one readinessPath+scratch component, and the fixture instance config
    (same shape as scripts/verify/stage3.sh writes for its sandboxes).

    The generator is copied into the sandbox, so Path(__file__) is the copy and
    the chart-values fallback to the generator's source tree cannot see the
    real charts/service. The instance tree always vendors that chart; this
    fixture does too.
    """
    tmp = tempfile.TemporaryDirectory()
    root = pathlib.Path(tmp.name)
    shutil.copytree(ROOT / "scripts", root / "scripts")
    (root / "charts").mkdir()
    shutil.copytree(CHART, root / "charts" / "service")
    (root / "catalog").mkdir()
    (root / "clusters" / "apps").mkdir(parents=True)
    (root / "instance").mkdir()
    (root / "catalog" / "projects.yaml").write_text(SANDBOX_CATALOG)
    (root / "catalog" / "tags.yaml").write_text("tags: {}\n")
    (root / "instance" / "config.yaml").write_text(SANDBOX_CONFIG)
    return tmp


@unittest.skipUnless(HELM, "helm is not installed here")
class UntaggedComponentTest(unittest.TestCase):
    """No tag yet: nothing runs until the first ship (docs/V1.md §8). An untagged image
    would render as :latest, which an Orbit registry never carries: pods sat in
    ImagePullBackOff and a CronJob's first run hung and blocked every later one."""

    VALUES = "\n".join([
        "project: charttest", "env: dev", "components:",
        "  - name: web", "    kind: web", "    image: ghcr.io/example-org/charttest-web",
        "    port: 8080", "    probePath: /healthz",
        "  - name: job", "    kind: cron", "    image: ghcr.io/example-org/charttest-job",
        '    schedule: "*/10 * * * *"', ""])

    def test_an_untagged_deployment_renders_zero_replicas(self):
        proc = render(self.VALUES)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(deployment(proc.stdout)["spec"]["replicas"], 0)

    def test_an_untagged_cronjob_renders_suspended(self):
        proc = render(self.VALUES)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual([c["spec"].get("suspend") for c in docs(proc.stdout, "CronJob")], [True])

    def test_a_tag_lifts_both(self):
        tagged = self.VALUES.replace("    probePath: /healthz", "    probePath: /healthz\n    tag: main-0000001")
        tagged = tagged.replace('    schedule: "*/10 * * * *"', '    schedule: "*/10 * * * *"\n    tag: main-0000001')
        proc = render(tagged)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(deployment(proc.stdout)["spec"]["replicas"], 1)
        self.assertNotIn("suspend", docs(proc.stdout, "CronJob")[0]["spec"])


@unittest.skipUnless(HELM, "helm is not installed here")
class DefaultSizeTest(unittest.TestCase):
    """A component that names no size gets the documented default (docs/V1.md M4b,
    docs/PRODUCT.md "Sizing a service"): 256Mi of memory as request AND limit, its class's
    CPU request, and no CPU limit. There is no per-service floor or ceiling beyond that."""

    BASE = "\n".join([
        "project: charttest", "env: prod", "components:",
        "  - name: web", "    kind: web", "    image: ghcr.io/example-org/charttest-web",
        "    tag: main-0000001", "    port: 8080", "    probePath: /healthz", ""])

    def resources(self, values: str) -> dict:
        proc = render(values)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        return deployment(proc.stdout)["spec"]["template"]["spec"]["containers"][0]["resources"]

    def test_no_size_named_is_256Mi_request_and_limit_and_the_class_cpu(self):
        got = self.resources(self.BASE)
        self.assertEqual(got["requests"], {"cpu": "250m", "memory": "256Mi"})
        self.assertEqual(got["limits"], {"memory": "256Mi"})

    def test_another_class_changes_the_cpu_request_only(self):
        got = self.resources(self.BASE.replace("    kind: web", "    kind: web\n    class: batch"))
        self.assertEqual(got["requests"], {"cpu": "20m", "memory": "256Mi"})
        self.assertEqual(got["limits"], {"memory": "256Mi"})

    def test_a_named_size_is_taken_as_named(self):
        got = self.resources(self.BASE.replace(
            "    port: 8080", "    port: 8080\n    memory: 128Mi\n    memoryLimit: 192Mi\n    cpu: 100m"))
        self.assertEqual(got["requests"], {"cpu": "100m", "memory": "128Mi"})
        self.assertEqual(got["limits"], {"memory": "192Mi"})


@unittest.skipUnless(HELM, "helm is not installed here")
class FinishedCronRunsAreNotKeptTest(unittest.TestCase):
    """A finished run's Job goes after 20 minutes (docs/V1.md §8, S16): kept by the history
    limits alone, one run cut by a node reboot fired KubeJobFailed for hours while every
    later run succeeded."""

    VALUES = UntaggedComponentTest.VALUES.replace(
        '    schedule: "*/10 * * * *"', '    schedule: "*/10 * * * *"\n    tag: main-0000001')

    def test_a_cron_run_is_deleted_twenty_minutes_after_it_ends(self):
        proc = render(self.VALUES)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        (job,) = docs(proc.stdout, "CronJob")
        self.assertEqual(job["spec"]["jobTemplate"]["spec"]["ttlSecondsAfterFinished"], 1200)
        # the history limits stay what they were: the TTL is what lets a failed run go
        self.assertEqual(job["spec"]["failedJobsHistoryLimit"], 3)

    def test_a_component_may_keep_its_runs_longer(self):
        proc = render(self.VALUES.replace("    tag: main-0000001", "    tag: main-0000001\n    ttlSecondsAfterFinished: 86400"))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        (job,) = docs(proc.stdout, "CronJob")
        self.assertEqual(job["spec"]["jobTemplate"]["spec"]["ttlSecondsAfterFinished"], 86400)


@unittest.skipUnless(HELM, "helm is not installed here")
class ServiceChartTest(unittest.TestCase):
    def test_readiness_path_splits_from_liveness(self):
        proc = render(VALUES_FULL)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        cont = container(deployment(proc.stdout))
        self.assertEqual(cont["readinessProbe"]["httpGet"]["path"], READINESS_PATH)
        self.assertEqual(cont["livenessProbe"]["httpGet"]["path"], PROBE_PATH)
        self.assertTrue(cont["securityContext"]["readOnlyRootFilesystem"])

    def test_scratch_volume_and_mount(self):
        proc = render(VALUES_FULL)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        dep = deployment(proc.stdout)
        pod = dep["spec"]["template"]["spec"]
        self.assertIn({"name": "scratch",
                       "emptyDir": {"medium": "Memory", "sizeLimit": SCRATCH_SIZE}},
                      pod["volumes"])
        cont = container(dep)
        mounts = [m for m in cont.get("volumeMounts", []) if m["name"] == "scratch"]
        self.assertEqual(len(mounts), 1, f"expected one scratch mount: {cont.get('volumeMounts')}")
        self.assertEqual(mounts[0]["mountPath"], SCRATCH_MOUNT)
        self.assertNotIn("readOnly", mounts[0],
                         "the scratch mount must stay writable under a read-only root")

    def test_absent_keys_render_byte_identical_to_base(self):
        """A minimal component renders exactly as before the change: the
        current chart renders VALUES_DEFAULTS byte-identical to the committed
        golden (rendered once from the pre-#43 chart). No git involved."""
        proc = render(VALUES_DEFAULTS)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        golden = GOLDEN.read_text()
        self.assertTrue(golden.startswith(GOLDEN_HEADER),
                        f"{GOLDEN} header changed; regenerate it instead of editing")
        self.assertEqual(proc.stdout, golden[len(GOLDEN_HEADER):],
                         "head render differs from the committed golden render")

    def test_scratch_without_size_limit_fails_schema(self):
        proc = render(VALUES_NO_SIZELIMIT)
        self.assertNotEqual(proc.returncode, 0,
                            "scratch without sizeLimit rendered clean")
        self.assertIn("sizeLimit", proc.stderr)

    def test_worker_drain_sets_grace_without_prestop(self):
        proc = render(VALUES_WORKER_DRAIN)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        pod = deployment(proc.stdout)["spec"]["template"]["spec"]
        self.assertEqual(pod["terminationGracePeriodSeconds"], WORKER_DRAIN)
        self.assertNotIn("lifecycle", container(deployment(proc.stdout)))

    def test_web_drain_keeps_prestop_and_uses_component_grace(self):
        proc = render(VALUES_WEB_DRAIN)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        dep = deployment(proc.stdout)
        pod = dep["spec"]["template"]["spec"]
        self.assertEqual(pod["terminationGracePeriodSeconds"], WEB_DRAIN)
        self.assertEqual(container(dep)["lifecycle"]["preStop"]["sleep"]["seconds"], 5)

    def test_web_drain_not_greater_than_prestop_fails(self):
        proc = render(VALUES_WEB_DRAIN_TOO_SHORT)
        self.assertNotEqual(proc.returncode, 0,
                            "web drain <= preStopSeconds rendered clean")
        self.assertIn('component "web"', proc.stderr)
        self.assertIn("must be greater than drain.preStopSeconds", proc.stderr)

    def test_scratch_at_or_above_memory_limit_fails(self):
        proc = render(VALUES_SCRATCH_TOO_LARGE)
        self.assertNotEqual(proc.returncode, 0,
                            "scratch.sizeLimit == memoryLimit rendered clean")
        self.assertIn(
            'component "worker": scratch.sizeLimit (512Mi) must be smaller than '
            "memoryLimit (512Mi)",
            proc.stderr)

    def test_scratch_without_memory_limit_fails(self):
        proc = render(VALUES_SCRATCH_NO_MEMORY_LIMIT)
        self.assertNotEqual(proc.returncode, 0,
                            "scratch without memoryLimit rendered clean")
        self.assertIn(
            'component "worker": scratch requires memoryLimit',
            proc.stderr)

    def test_readiness_path_without_probe_path_fails_schema(self):
        """readinessPath alone renders zero probes (both are guarded by
        probePath), so the schema refuses it instead of rendering green."""
        proc = render(VALUES_READINESS_WITHOUT_PROBE)
        self.assertNotEqual(proc.returncode, 0,
                            "readinessPath without probePath rendered clean")
        self.assertIn("probePath", proc.stderr)

    def test_catalog_sync_passes_readiness_and_scratch_through(self):
        """scripts/catalog-sync.py on a small catalog: both keys survive the
        component pass-through into the generated valuesObject."""
        box = sync_sandbox()
        try:
            proc = subprocess.run(
                [sys.executable, "scripts/catalog-sync.py"],
                capture_output=True, text=True, cwd=box.name)
            self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
            docs = [d for d in yaml.safe_load_all(
                (pathlib.Path(box.name) / "clusters" / "apps"
                 / "scratchtest-prod.yaml").read_text()) if d]
            apps = [d for d in docs if d.get("kind") == "Application"]
            self.assertEqual(len(apps), 1, f"expected one Application: {box.name}")
            comps = apps[0]["spec"]["source"]["helm"]["valuesObject"]["components"]
            self.assertEqual(len(comps), 1)
            self.assertEqual(comps[0].get("readinessPath"), "/readyz")
            self.assertEqual(comps[0].get("drain"), 40)
            self.assertEqual(comps[0].get("memoryLimit"), "1Gi")
            self.assertEqual(comps[0].get("scratch"),
                             {"mountPath": "/tmp/scratch", "sizeLimit": "512Mi"})
        finally:
            box.cleanup()

    def _sync_catalog(self, catalog: str) -> subprocess.CompletedProcess:
        box = sync_sandbox()
        try:
            (pathlib.Path(box.name) / "catalog" / "projects.yaml").write_text(catalog)
            return subprocess.run(
                [sys.executable, "scripts/catalog-sync.py"],
                capture_output=True, text=True, cwd=box.name)
        finally:
            box.cleanup()

    def test_catalog_sync_refuses_scratch_at_or_above_memory_limit(self):
        proc = self._sync_catalog(
            SANDBOX_CATALOG.replace("memoryLimit: 1Gi", "memoryLimit: 512Mi"))
        self.assertNotEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn(
            'component "api": scratch.sizeLimit (512Mi) must be smaller than '
            "memoryLimit (512Mi)",
            proc.stderr)

    def test_catalog_sync_refuses_scratch_without_memory_limit(self):
        proc = self._sync_catalog(
            SANDBOX_CATALOG.replace("        memoryLimit: 1Gi\n", ""))
        self.assertNotEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn(
            'component "api": scratch requires memoryLimit',
            proc.stderr)

    def test_database_parameters_render_into_cluster(self):
        proc = render(VALUES_DB_PARAMETERS)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(cluster(proc.stdout)["spec"]["postgresql"]["parameters"], {
            "max_connections": "60",
            "shared_buffers": "64MB",
        })

    def test_absent_database_parameters_render_identical(self):
        """No parameters, and an empty parameters map, render the same Cluster —
        no postgresql block, byte-identical to each other."""
        absent = render(VALUES_DB)
        empty = render(VALUES_DB_EMPTY_PARAMETERS)
        self.assertEqual(absent.returncode, 0, absent.stderr)
        self.assertEqual(empty.returncode, 0, empty.stderr)
        self.assertEqual(absent.stdout, empty.stdout)
        self.assertNotIn("postgresql", cluster(absent.stdout)["spec"])

    def test_invalid_database_parameter_key_fails_schema(self):
        proc = render(VALUES_DB_BAD_PARAMETER)
        self.assertNotEqual(proc.returncode, 0, "invalid GUC name rendered clean")
        self.assertIn("additional properties 'max-connections' not allowed", proc.stderr)


VALUES_METRICS_WORKER = """\
project: charttest
env: prod
components:
  - name: worker
    kind: worker
    image: ghcr.io/example-org/charttest-worker
    port: 8080
    probePath: /healthz
    metrics: {port: 8080}
"""

VALUES_METRICS_WEB = """\
project: charttest
env: prod
components:
  - name: web
    kind: web
    image: ghcr.io/example-org/charttest-web
    port: 8080
    probePath: /healthz
    metrics: {port: 9090, path: %s}
    persistence: {size: 1Gi, mountPath: %s}
    configMap: {name: %s, mountPath: %s}
"""

INJECT = '"/x\\n      injected: true"'


def docs(output: str, kind: str) -> list:
    return [d for d in yaml.safe_load_all(output) if isinstance(d, dict) and d.get("kind") == kind]


class SweepArea5Test(unittest.TestCase):
    """Whole-app sweep, area 5 (charts/service): the schema is the last line between a
    catalog commit and the rendered objects, so every free-text path is anchored and
    every render site quoted."""

    def test_a_newline_in_metrics_path_is_refused_not_rendered(self):
        proc = render(VALUES_METRICS_WEB % (INJECT, "/data", "cm", "/srv"))
        self.assertNotEqual(proc.returncode, 0, "a metrics path carrying a newline rendered")
        self.assertNotIn("injected", proc.stdout)

    def test_a_newline_in_a_mount_path_or_configmap_name_is_refused(self):
        for values in (
            VALUES_METRICS_WEB % ("/metrics", INJECT, "cm", "/srv"),
            VALUES_METRICS_WEB % ("/metrics", "/data", '"cm\\n      injected: true"', "/srv"),
            VALUES_METRICS_WEB % ("/metrics", "/data", "cm", INJECT),
            VALUES_FULL.replace(SCRATCH_MOUNT, INJECT),
        ):
            proc = render(values)
            self.assertNotEqual(proc.returncode, 0, values)
            self.assertNotIn("injected", proc.stdout)

    def test_paths_and_names_render_quoted(self):
        proc = render(VALUES_METRICS_WEB % ("/metrics", "/data", "cm", "/srv"))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn('path: "/metrics"', proc.stdout)
        self.assertIn('mountPath: "/data"', proc.stdout)
        self.assertIn('mountPath: "/srv"', proc.stdout)
        self.assertIn('name: "cm"', proc.stdout)
        proc = render(VALUES_FULL)
        self.assertIn('mountPath: "%s"' % SCRATCH_MOUNT, proc.stdout)

    def test_a_worker_with_metrics_on_its_own_port_publishes_that_port(self):
        proc = render(VALUES_METRICS_WORKER)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        (svc,) = docs(proc.stdout, "Service")
        ports = {p["name"]: p for p in svc["spec"]["ports"] or []}
        (mon,) = docs(proc.stdout, "ServiceMonitor")
        named = mon["spec"]["endpoints"][0]["port"]
        self.assertIn(named, ports, f"the ServiceMonitor names {named!r}; the Service publishes {sorted(ports)}")
        self.assertEqual(ports[named]["targetPort"], 8080)

    def test_backup_timezone_is_a_value_with_the_component_charset(self):
        proc = render(VALUES_DB)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        (job,) = docs(proc.stdout, "CronJob")
        self.assertEqual(job["spec"]["timeZone"], "Asia/Jerusalem")
        proc = render(VALUES_DB + "  backup: {timeZone: Etc/UTC}\n")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        (job,) = docs(proc.stdout, "CronJob")
        self.assertEqual(job["spec"]["timeZone"], "Etc/UTC")
        proc = render(VALUES_DB + "  backup: {timeZone: %s}\n" % INJECT)
        self.assertNotEqual(proc.returncode, 0)

    def test_a_scraped_component_carries_the_catalog_names_under_a_sample_limit(self):
        """docs/V1.md §6.1 and §6.6: tenant series carried namespace/pod/service but not the
        catalog's project, env and component; and nothing bounded a label explosion."""
        proc = render(VALUES_METRICS_WORKER)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        (sm,) = docs(proc.stdout, "ServiceMonitor")
        self.assertEqual(sm["spec"]["sampleLimit"], 5000)
        relabel = {r["targetLabel"]: r["sourceLabels"] for r in sm["spec"]["endpoints"][0]["relabelings"]}
        self.assertEqual(relabel, {
            "project": ["__meta_kubernetes_pod_label_app_kubernetes_io_instance"],
            "env": ["__meta_kubernetes_pod_label_orbit_local_env"],
            "component": ["__meta_kubernetes_pod_label_app_kubernetes_io_name"],
        })

    def test_every_database_is_scraped_with_or_without_a_bucket(self):
        """docs/V1.md §8: the PodMonitor was enabled only with WAL archiving, so a dev
        database was never scraped — its dashboard panel had no data and OrbitDatabaseDown
        (cnpg_collector_up) could not fire for it."""
        plain = render(VALUES_DB)
        archiving = render(VALUES_DB + "s3: {bucket: shop-platform, endpoint: https://fsn1.example.invalid}\n")
        for proc in (plain, archiving):
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertEqual(cluster(proc.stdout)["spec"]["monitoring"], {"enablePodMonitor": True})

    def test_s3_bucket_and_endpoint_are_anchored_and_quoted(self):
        base = VALUES_DB + "s3: {bucket: %s, endpoint: %s}\n"
        proc = render(base % ("shop-platform", "https://fsn1.example.invalid"))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn('destinationPath: "s3://shop-platform/cnpg/charttest-prod"', proc.stdout)
        self.assertIn('endpointURL: "https://fsn1.example.invalid"', proc.stdout)
        for bucket, endpoint in ((INJECT.replace("/x", "b"), "https://e.invalid"),
                                 ("shop-platform", '"https://e.invalid\\n      injected: true"')):
            proc = render(base % (bucket, endpoint))
            self.assertNotEqual(proc.returncode, 0, (bucket, endpoint))
            self.assertNotIn("injected", proc.stdout)

    def test_s3_endpoint_admits_a_trailing_slash_and_a_mixed_case_host(self):
        base = VALUES_DB + "s3: {bucket: shop-platform, endpoint: %s}\n"
        for endpoint in ("https://objects.example.invalid/", "https://OBJECTS.example.invalid",
                         "https://objects.example.invalid:9000/"):
            proc = render(base % endpoint)
            self.assertEqual(proc.returncode, 0, (endpoint, proc.stderr))
        proc = render(base % "https://objects.example.invalid/prefix")
        self.assertNotEqual(proc.returncode, 0, "an endpoint with a path rendered")

    def test_catalog_sync_refuses_an_s3_value_the_chart_would_refuse(self):
        """The generator mirrors the chart's schema for s3 (bucket, endpoint from
        objectStorage), so a bad value is refused before an Application is written,
        naming the field and never repeating the value."""
        marker = "PLANTED-S3-MARKER-A5"
        for field, value in (("endpoint", f"https://o.example.invalid/{marker}"),
                             ("buckets", {"platform": f"Bad_Bucket-{marker}-" + "x" * 80})):
            obj = {"provider": "hetzner", "endpoint": "https://o.example.invalid",
                   "buckets": {"platform": "example-platform"}}
            obj[field] = value
            config = SANDBOX_CONFIG + "objectStorage:\n" + "".join(
                f"  {k}: {json.dumps(v)}\n" for k, v in obj.items())
            catalog = SANDBOX_CATALOG + "    database:\n      size: 2Gi\n"
            proc = self._sync(catalog, config)
            self.assertNotEqual(proc.returncode, 0, f"{field}: the generator wrote the Application")
            self.assertIn("s3", proc.stdout + proc.stderr)
            self.assertNotIn(marker, proc.stdout + proc.stderr)
        config = SANDBOX_CONFIG + "objectStorage:\n  provider: hetzner\n  endpoint: https://o.example.invalid/\n  buckets: {platform: example-platform}\n"
        proc = self._sync(SANDBOX_CATALOG + "    database:\n      size: 2Gi\n", config)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)

    def _sync(self, catalog: str, config: str) -> subprocess.CompletedProcess:
        box = sync_sandbox()
        try:
            root = pathlib.Path(box.name)
            (root / "catalog" / "projects.yaml").write_text(catalog)
            (root / "instance" / "config.yaml").write_text(config)
            return subprocess.run([sys.executable, "scripts/catalog-sync.py"],
                                  capture_output=True, text=True, cwd=box.name)
        finally:
            box.cleanup()

    def test_an_undeclared_component_key_is_refused_by_the_schema(self):
        proc = render(VALUES_DEFAULTS + "    serviceAccountName: charttest-intake\n")
        self.assertNotEqual(proc.returncode, 0, "serviceAccountName rendered")
        self.assertIn("serviceAccountName", proc.stderr)
        proc = render(VALUES_DEFAULTS + "    command: [/bin/app, --serve]\n    args: [--port, '8080']\n")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        cont = container(deployment(proc.stdout))
        self.assertEqual(cont["command"], ["/bin/app", "--serve"])
        self.assertEqual(cont["args"], ["--port", "8080"])
        proc = render(VALUES_DEFAULTS + "    command: sh -c 'echo x'\n")
        self.assertNotEqual(proc.returncode, 0, "a string command rendered")

    def test_a_component_named_db_backup_is_refused_on_the_pg_dump_path(self):
        values = VALUES_DB.replace("components:\n", (
            "components:\n  - name: db-backup\n    kind: cron\n    schedule: \"0 4 * * *\"\n"
            "    image: ghcr.io/example-org/charttest-cron\n"))
        proc = render(values)
        self.assertNotEqual(proc.returncode, 0, "a component named db-backup rendered beside the chart's own")
        self.assertIn("db-backup", proc.stderr)

    def test_every_stage3_chart_row_still_holds(self):
        """scripts/verify/stage3.sh keeps a table of chart guards (`chart <label> refuse|accept
        <fragment> [pattern]`); a schema change can flip a row from green to red with no test
        noticing (area-5 round 2: closing the component item refused a row that expected a
        render). Replay each literal row here through the same values shape."""
        text = (ROOT / "scripts" / "verify" / "stage3.sh").read_text()
        digest = re.search(r"^DIGEST=([0-9a-f]{64})$", text, flags=re.M).group(1)
        arg = r"""(?:'([^']*)'|"((?:[^"\\]|\\.)*)")"""  # a single- or double-quoted shell word
        rows = re.findall(r"^chart \"([^\"]+)\" (refuse|accept) \\\n  " + arg + r"(?: \\?\n?\s*" + arg + ")?",
                          text, flags=re.M)
        self.assertEqual(len(rows), len(re.findall(r'^chart "', text, flags=re.M)),
                         "a stage-3 chart row is not replayed here")

        def word(single: str, double: str) -> str:
            return single or double.replace('\\"', '"').replace("$DIGEST", digest)

        for label, expect, frag1, frag2, pat1, pat2 in rows:
            fragment, pattern = word(frag1, frag2), word(pat1, pat2)
            values = ("project: c\nenv: prod\nhostnames: [c.example.org]\ncomponents:\n"
                      "  - {name: api, kind: web, image: img, port: 80, path: /, %s}\n" % fragment)
            proc = render(values)
            if expect == "refuse":
                self.assertNotEqual(proc.returncode, 0, f"stage3 row {label!r} expects a refusal")
            else:
                self.assertEqual(proc.returncode, 0, f"stage3 row {label!r} expects a render: {proc.stderr}")
                if pattern:
                    self.assertRegex(proc.stdout, pattern, f"stage3 row {label!r}")

    def test_backup_keep_renders_as_a_number_in_the_prune_command(self):
        proc = render(VALUES_DB + "  backup: {keep: 14}\n")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        (job,) = docs(proc.stdout, "CronJob")
        args = json.dumps(job["spec"]["jobTemplate"]["spec"]["template"]["spec"]["containers"])
        self.assertIn("-mtime +14 ", args)
        self.assertNotIn("float64", args)

    def test_the_rbac_selector_keys_are_refused_on_a_component_one_at_a_time(self):
        """rbac.jobRunner is a root value: a component carrying either key, of any type a
        future schema might give it, is refused (the stage-3 row probes both at once)."""
        for fragment in ('    jobRunner: "api"\n', "    jobRunner: true\n", "    rbac: true\n",
                         "    rbac: {jobRunner: web}\n"):
            proc = render(VALUES_DEFAULTS + fragment)
            self.assertNotEqual(proc.returncode, 0, f"a component carrying {fragment.strip()} rendered")

    def test_values_yaml_documents_only_component_keys_the_schema_admits(self):
        schema = json.loads((CHART / "values.schema.json").read_text())
        allowed = set(schema["properties"]["components"]["items"]["properties"])
        documented = set()
        block = (CHART / "values.yaml").read_text().split("# Workloads ---", 1)[1]
        for line in block.splitlines():
            if line.startswith("# ") and line.rstrip().endswith("---"):
                break  # the next documented section
            m = re.match(r"#    ([a-zA-Z]+):", line)
            if m:
                documented.add(m.group(1))
        self.assertTrue(documented, "no documented component keys found")
        self.assertEqual(sorted(documented - allowed), [],
                         "values.yaml documents keys the schema (and so the generator) refuses")


if __name__ == "__main__":
    if "--regenerate" in sys.argv:
        sys.argv.remove("--regenerate")
        regenerate_golden()
    else:
        unittest.main()
