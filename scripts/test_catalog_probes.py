#!/usr/bin/env python3
"""Regression tests for catalog-generated tenant blackbox Probe resources.

Every generation case runs the repository's real catalog-sync.py in a synthetic
temporary instance. Nothing is written to the checkout and no network is used.
"""
import importlib.util
import contextlib
import io
import pathlib
import re
import shutil
import subprocess
import sys
import tempfile
import unittest

import yaml


ROOT = pathlib.Path(__file__).resolve().parent.parent
GENERATOR = ROOT / "scripts" / "catalog-sync.py"
CHART = ROOT / "charts" / "service"
BLACKBOX = ROOT / "template" / "provisioning" / "clusters" / "platform" / "apps" / "blackbox-exporter.yaml"
PLATFORM_PROBES = (ROOT / "template" / "provisioning" / "clusters" / "platform"
                   / "monitoring" / "probes.yaml")
HELM = shutil.which("helm")

INSTANCE = """\
instance: {name: fixture}
git:
  repoUrl: git@github.com:example/test.git
  branch: main
  github_org: example
domains:
  primary: example.org
  dev: dev.example.org
  dnsZones: [example.org]
registry: ghcr.io/example
"""

WEB = {
    "name": "web", "kind": "web", "image": "example/web", "port": 8080,
    "path": "/",
}


def project(pid, environments, components=None, **fields):
    result = {
        "id": pid, "name": pid.title(), "hosted": "self",
        "environments": environments,
        "components": components if components is not None else [dict(WEB)],
    }
    result.update(fields)
    return result


class Sandbox:
    def __init__(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.tmp.name)
        (self.root / "scripts").mkdir()
        shutil.copy2(GENERATOR, self.root / "scripts" / "catalog-sync.py")
        # Quota derivation reads component/database defaults from the service chart
        # rather than duplicating the numbers; the generator must see the same tree
        # an instance has after core-sync.
        (self.root / "charts").mkdir()
        shutil.copytree(CHART, self.root / "charts" / "service")
        dest = (self.root / "template" / "provisioning" / "clusters" / "platform"
                / "config" / "orbitd" / "namespace.yaml")
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(
            ROOT / "template" / "provisioning" / "clusters" / "platform"
            / "config" / "orbitd" / "namespace.yaml", dest)
        (self.root / "catalog").mkdir()
        (self.root / "catalog" / "tags.yaml").write_text("tags: {}\n")
        (self.root / "instance").mkdir()
        (self.root / "instance" / "config.yaml").write_text(INSTANCE)
        (self.root / "clusters" / "apps").mkdir(parents=True)
        # The generator must leave existing platform output byte-for-byte alone.
        platform = self.root / "clusters" / "platform" / "monitoring" / "probes.yaml"
        platform.parent.mkdir(parents=True)
        shutil.copy2(PLATFORM_PROBES, platform)
        self.platform_probe = platform
        self.platform_before = platform.read_bytes()

    def close(self):
        self.tmp.cleanup()

    def generate(self, projects):
        (self.root / "catalog" / "projects.yaml").write_text(
            yaml.safe_dump({"version": 3, "projects": projects}, sort_keys=False))
        return subprocess.run(
            [sys.executable, "scripts/catalog-sync.py"], cwd=self.root,
            capture_output=True, text=True)

    def docs(self, name):
        path = self.root / "clusters" / "apps" / name
        return [doc for doc in yaml.safe_load_all(path.read_text()) if doc]

    def probe(self, pid, env):
        matches = [(path, doc)
                   for path in sorted((self.root / "clusters" / "apps").glob("*.yaml"))
                   for doc in self.docs(path.name)
                   if doc.get("kind") == "Probe"
                   and doc["metadata"]["labels"]["orbit.local/project"] == pid
                   and doc["metadata"]["labels"]["orbit.local/env"] == env]
        if len(matches) != 1:
            raise AssertionError(f"expected one Probe for {pid}/{env}, found {len(matches)}")
        return matches[0][1]

    def probe_path(self, pid, env):
        for path in sorted((self.root / "clusters" / "apps").glob("*.yaml")):
            if any(doc.get("kind") == "Probe"
                   and doc["metadata"]["labels"]["orbit.local/project"] == pid
                   and doc["metadata"]["labels"]["orbit.local/env"] == env
                   for doc in self.docs(path.name)):
                return path
        raise AssertionError(f"expected a Probe path for {pid}/{env}")

    def generate_with_digest(self, projects, digest):
        """Run the production render entry point with its injectable digest seam."""
        (self.root / "catalog" / "projects.yaml").write_text(
            yaml.safe_dump({"version": 3, "projects": projects}, sort_keys=False))
        spec = importlib.util.spec_from_file_location(
            f"catalog_sync_fixture_{id(self)}", self.root / "scripts" / "catalog-sync.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        module.ROOT = self.root
        module.CATALOG = self.root / "catalog" / "projects.yaml"
        module.TAGS = self.root / "catalog" / "tags.yaml"
        module.APPS_DIR = self.root / "clusters" / "apps"
        module.ORBIT_DIR = self.root / "clusters" / "orbit"
        module.INSTANCE = self.root / "instance" / "config.yaml"
        module._INST_CACHE = None
        stdout, stderr = io.StringIO(), io.StringIO()
        try:
            with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                result = module.main(probe_digest=digest)
        except SystemExit as exc:
            result = exc.code if isinstance(exc.code, int) else 1
            if not isinstance(exc.code, int):
                stderr.write(str(exc.code) + "\n")
        return subprocess.CompletedProcess([], result, stdout.getvalue(), stderr.getvalue())

    def app_values(self, pid, env):
        docs = self.docs(f"{pid}-{env}.yaml")
        app = next(doc for doc in docs if doc.get("kind") == "Application")
        return app["spec"]["source"]["helm"]["valuesObject"]

    def app_snapshot(self):
        return {
            path.name: path.read_bytes()
            for path in sorted((self.root / "clusters" / "apps").glob("*.yaml"))
        }

    def probe_docs(self):
        return [
            (path, doc)
            for path in sorted((self.root / "clusters" / "apps").glob("*.yaml"))
            for doc in self.docs(path.name)
            if doc.get("kind") == "Probe"
        ]

    def helm_ingress_rules(self, pid, env):
        if not HELM:
            raise unittest.SkipTest("helm is not installed")
        values_file = self.root / f"values-{pid}-{env}.yaml"
        values_file.write_text(yaml.safe_dump(self.app_values(pid, env)))
        rendered = subprocess.run(
            [HELM, "template", "fixture", str(CHART), "-f", str(values_file),
             "--namespace", f"{pid}-{env}"],
            capture_output=True, text=True, cwd=ROOT)
        if rendered.returncode:
            raise AssertionError(rendered.stderr)
        ingress = next(doc for doc in yaml.safe_load_all(rendered.stdout)
                       if doc and doc.get("kind") == "Ingress")
        return {
            rule["host"]: ((rule.get("http") or {}).get("paths") or [])
            for rule in ingress["spec"]["rules"]
        }

    def helm_docs(self, pid, env):
        if not HELM:
            raise unittest.SkipTest("helm is not installed")
        values_file = self.root / f"values-{pid}-{env}.yaml"
        values_file.write_text(yaml.safe_dump(self.app_values(pid, env)))
        rendered = subprocess.run(
            [HELM, "template", "fixture", str(CHART), "-f", str(values_file),
             "--namespace", f"{pid}-{env}"],
            capture_output=True, text=True, cwd=ROOT)
        if rendered.returncode:
            raise AssertionError(rendered.stderr)
        return [doc for doc in yaml.safe_load_all(rendered.stdout) if doc]

    def helm_kind(self, pid, env, kind):
        matches = [doc for doc in self.helm_docs(pid, env) if doc.get("kind") == kind]
        if len(matches) != 1:
            raise AssertionError(f"expected one {kind} for {pid}/{env}, found {len(matches)}")
        return matches[0]


class CatalogProbeTest(unittest.TestCase):
    def setUp(self):
        self.box = Sandbox()

    def tearDown(self):
        self.box.close()

    def assert_generated(self, projects):
        proc = self.box.generate(projects)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertEqual(self.box.platform_probe.read_bytes(), self.box.platform_before,
                         "catalog sync changed the existing platform Probe")
        return proc

    def assert_no_generated_probes(self):
        probes = self.box.probe_docs()
        self.assertEqual(probes, [], "unexpected generated Probe manifests: " + repr([
            (path.name, doc.get("metadata", {}).get("name")) for path, doc in probes
        ]))

    def test_dashboard_configmap_carries_project_and_env_labels(self):
        self.assert_generated([project("pid", {"prod": {}, "dev": {"enabled": True}})])
        for env in ("prod", "dev"):
            docs = self.box.docs(f"dashboard-pid-{env}.yaml")
            self.assertEqual(len(docs), 1, env)
            doc = docs[0]
            self.assertEqual(doc["kind"], "ConfigMap")
            self.assertEqual(doc["metadata"]["namespace"], "monitoring")
            self.assertEqual(doc["metadata"]["labels"], {
                "grafana_dashboard": "1",
                "orbit.local/project": "pid",
                "orbit.local/env": env,
            })

    def test_a_project_with_no_root_route_is_probed_where_it_answers(self):
        """docs/V1.md §8 (checkers-lab): a project whose only web route is /api has no
        route at `/`, so a probe of the bare host met Traefik's 404 on both doors (the dev
        door too: no router matches `/`, so no basic-auth middleware runs). The probe
        requests the first web component's path plus its probePath instead."""
        api = {"name": "api", "kind": "web", "image": "example/api", "port": 8080,
               "path": "/api", "probePath": "/healthz"}
        self.assert_generated([project("pid", {"prod": {}, "dev": {"enabled": True}},
                                       components=[api])])
        prod = self.box.probe("pid", "prod")
        dev = self.box.probe("pid", "dev")
        self.assertEqual(prod["spec"]["targets"]["staticConfig"]["static"],
                         ["https://pid.example.org/api/healthz"])
        self.assertEqual(dev["spec"]["module"], "http_401")
        self.assertTrue(all(t.endswith("/api/healthz")
                            for t in dev["spec"]["targets"]["staticConfig"]["static"]))

    def test_a_root_route_keeps_the_bare_host(self):
        api = {"name": "api", "kind": "web", "image": "example/api", "port": 8080,
               "path": "/api", "probePath": "/healthz"}
        self.assert_generated([project("pid", {"prod": {}}, components=[dict(WEB), api])])
        self.assertEqual(self.box.probe("pid", "prod")["spec"]["targets"]["staticConfig"]["static"],
                         ["https://pid.example.org"])

    def test_default_prod_and_normalized_dev_aliases_match_ingress_values(self):
        projects = [project("pid", {
            "prod": {},
            "dev": {
                "enabled": True,
                "hostnames": ["DEV-Alias.Example.ORG.", "dev-alias.example.org"],
                "customDomains": [
                    {"host": "Other.Example.NET."},
                    {"host": "other.example.net"},
                ],
            },
        })]
        self.assert_generated(projects)

        prod = self.box.probe("pid", "prod")
        dev = self.box.probe("pid", "dev")
        self.assertEqual(prod["spec"]["module"], "http_2xx")
        self.assertEqual(prod["spec"]["targets"]["staticConfig"]["static"],
                         ["https://pid.example.org"])
        self.assertEqual(dev["spec"]["module"], "http_401")
        self.assertEqual(dev["spec"]["targets"]["staticConfig"]["static"], [
            "https://dev-alias.example.org", "https://other.example.net",
        ])

        for env, probe_doc in (("prod", prod), ("dev", dev)):
            values = self.box.app_values("pid", env)
            targets = [url.removeprefix("https://") for url in
                       probe_doc["spec"]["targets"]["staticConfig"]["static"]]
            self.assertEqual(targets, values["hostnames"])
            if HELM:
                values_file = self.box.root / f"values-{env}.yaml"
                values_file.write_text(yaml.safe_dump(values))
                rendered = subprocess.run(
                    [HELM, "template", "fixture", str(CHART), "-f", str(values_file),
                     "--namespace", f"pid-{env}"],
                    capture_output=True, text=True, cwd=ROOT)
                self.assertEqual(rendered.returncode, 0, rendered.stderr)
                ingresses = [doc for doc in yaml.safe_load_all(rendered.stdout)
                             if doc and doc.get("kind") == "Ingress"]
                self.assertEqual(len(ingresses), 1)
                ingress_hosts = [rule["host"] for rule in ingresses[0]["spec"]["rules"]]
                self.assertEqual(targets, ingress_hosts)

        for probe_doc, env, check in (
                (prod, "prod", "public-surface"),
                (dev, "dev", "protected-door")):
            self.assertEqual(probe_doc["apiVersion"], "monitoring.coreos.com/v1")
            self.assertEqual(probe_doc["kind"], "Probe")
            self.assertEqual(probe_doc["metadata"]["namespace"], "monitoring")
            name = probe_doc["metadata"]["name"]
            self.assertRegex(name, r"^tenant-probe-[0-9a-f]{50}$")
            self.assertEqual(len(name), 63)
            self.assertEqual(self.box.probe_path("pid", env).stem, name,
                             "one identity call must name both resource and file")
            self.assertEqual(probe_doc["metadata"]["labels"], {
                "orbit.local/project": "pid", "orbit.local/env": env,
                "orbit.local/check": check,
            })
            self.assertEqual(probe_doc["spec"]["jobName"], name)
            self.assertEqual(probe_doc["spec"]["prober"]["url"],
                             "blackbox-exporter.monitoring.svc:9115")

    def test_unexposed_disabled_external_and_worker_only_environments_have_no_probe(self):
        projects = [
            project("hidden", {"prod": {"expose": False}, "dev": {"enabled": False}}),
            project("worker", {"prod": {}}, components=[
                {"name": "queue", "kind": "worker", "image": "example/worker"},
            ]),
            {
                "id": "external", "name": "External", "hosted": "external",
                "url": "https://outside.example.net",
                "environments": {"prod": {"hostnames": ["outside.example.net"]}},
            },
        ]
        self.assert_generated(projects)
        names = set(self.box.app_snapshot())
        self.assert_no_generated_probes()
        self.assertNotIn("hidden-dev.yaml", names)
        self.assertEqual(self.box.app_values("worker", "prod")["hostnames"],
                         ["worker.example.org"],
                         "worker fixture must prove hostname values alone do not create a Probe")

    def test_no_probe_assertion_rejects_unconditional_public_hosts_mutation(self):
        generator = self.box.root / "scripts" / "catalog-sync.py"
        source = generator.read_text()
        old = '            public_hosts = public_ingress_hosts(values)\n'
        self.assertIn(old, source, "mutation seam no longer matches the production renderer")
        generator.write_text(source.replace(old, '            public_hosts = values["hostnames"]\n',
                                            1))
        proc = self.box.generate([project("worker", {"prod": {}}, components=[
            {"name": "queue", "kind": "worker", "image": "example/worker"},
        ])])
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertTrue(self.box.probe_docs(), "bad mutation did not emit its worker Probe")
        with self.assertRaisesRegex(AssertionError, "unexpected generated Probe manifests"):
            self.assert_no_generated_probes()

    def test_probe_targets_only_hosts_with_rendered_ingress_backends(self):
        projects = [project("routes", {
            "prod": {
                "hostnames": ["a.example.org", "b.example.org", "c.example.org"],
                "customDomains": [{"host": "custom.example.net"}],
            },
            "dev": {},
        }, components=[
            {**WEB, "name": "a", "hosts": ["a.example.org"]},
            {**WEB, "name": "b", "path": "/api", "hosts": ["b.example.org"]},
            {**WEB, "name": "dev-only", "hosts": ["c.example.org"],
             "environments": ["dev"]},
            {"name": "queue", "kind": "worker", "image": "example/worker",
             "port": 9000},
        ])]
        self.assert_generated(projects)
        probe = self.box.probe("routes", "prod")
        targets = probe["spec"]["targets"]["staticConfig"]["static"]
        self.assertEqual(targets, ["https://a.example.org", "https://b.example.org"])

        rules = self.box.helm_ingress_rules("routes", "prod")
        self.assertTrue(rules["a.example.org"])
        self.assertTrue(rules["b.example.org"])
        self.assertEqual(rules["c.example.org"], [])
        self.assertEqual(rules["custom.example.net"], [])
        self.assertEqual(
            targets,
            [f"https://{host}" for host, paths in rules.items() if paths],
            "Probe targets must be exactly the chart's hosts with usable backends")

    def test_unrestricted_component_covers_default_and_custom_hosts(self):
        projects = [project("custom", {"prod": {
            "customDomains": [{"host": "www.customer.example"}],
        }})]
        self.assert_generated(projects)
        probe = self.box.probe("custom", "prod")
        targets = probe["spec"]["targets"]["staticConfig"]["static"]
        rules = self.box.helm_ingress_rules("custom", "prod")
        self.assertEqual(targets, ["https://custom.example.org",
                                   "https://www.customer.example"])
        self.assertEqual(targets, [f"https://{host}" for host, paths in rules.items()
                                   if paths])

    def test_all_hosts_unserved_by_restricted_web_components_have_no_probe(self):
        projects = [project("unserved", {"prod": {
            "hostnames": ["a.example.org", "b.example.org"],
        }}, components=[
            {**WEB, "hosts": ["elsewhere.example.org"]},
        ])]
        self.assert_generated(projects)
        rules = self.box.helm_ingress_rules("unserved", "prod")
        self.assertEqual(rules, {"a.example.org": [], "b.example.org": []})
        self.assert_no_generated_probes()

    def test_host_withdrawal_and_tenant_removal_prune_only_the_named_old_targets(self):
        initial = [project("alpha", {"prod": {}, "dev": {"enabled": True}}),
                   project("beta", {"prod": {}})]
        legacy = self.box.root / "clusters" / "apps" / "probe-alpha-prod.yaml"
        legacy.write_text("# legacy pre-digest Probe identity\n")
        first = self.assert_generated(initial)
        self.assertFalse(legacy.exists(), "the naming migration left an old Probe orphan")
        self.assertIn("pruned clusters/apps/probe-alpha-prod.yaml", first.stdout)
        old_probe_paths = {
            self.box.probe_path(pid, env).name
            for pid, env in (("alpha", "prod"), ("alpha", "dev"), ("beta", "prod"))
        }
        for name in old_probe_paths:
            self.assertIn(f"wrote clusters/apps/{name}", first.stdout)

        stable_before = self.box.app_snapshot()
        self.assert_generated(initial)
        self.assertEqual(self.box.app_snapshot(), stable_before,
                         "an identical sync changed generated raw bytes")

        changed = [project("alpha", {"prod": {"expose": False}})]
        proc = self.assert_generated(changed)
        remaining = set(self.box.app_snapshot())
        self.assertTrue(old_probe_paths.isdisjoint(remaining), remaining)
        for name in old_probe_paths:
            self.assertIn(f"pruned clusters/apps/{name}", proc.stdout)
        self.assertNotIn("https://alpha.example.org", "".join(
            data.decode() for data in self.box.app_snapshot().values()))
        self.assertNotIn("https://beta.example.org", "".join(
            data.decode() for data in self.box.app_snapshot().values()))

    def test_a_platform_hostname_cannot_be_taken_by_a_project(self):
        """QA of v1.0.0, F24: `grafana.<domain>` under a project's hostnames was accepted
        and applied, an Ingress rule for a platform door in a tenant namespace."""
        for label in ("grafana", "orbit", "mcp", "intake"):
            for key in ("hostnames", "customDomains"):
                value = [f"{label}.example.org"] if key == "hostnames" else [{"host": f"{label}.Example.org."}]
                proc = self.box.generate([project("one", {"prod": {key: value}})])
                self.assertNotEqual(proc.returncode, 0, f"{label} under {key} was accepted")
                self.assertIn("is one of the platform's own", proc.stdout + proc.stderr)
        self.assert_generated([project("grafana2", {"prod": {}})])

    def test_a_hostname_outside_the_instance_domains_is_a_custom_domain(self):
        """QA of v1.0.0, F23: a foreign host under `hostnames` was accepted and its
        certificate order then failed forever. Under `customDomains` it is accepted."""
        proc = self.box.generate([project("one", {"prod": {"hostnames": ["one.example.com"]}})])
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("is outside this instance's domains", proc.stdout + proc.stderr)
        self.assertIn("customDomains", proc.stdout + proc.stderr)
        self.assert_generated([project("one", {"prod": {"customDomains": [{"host": "one.example.com"}]}})])
        self.assert_generated([project("one", {"prod": {"hostnames": ["shop.example.org"]},
                                               "dev": {"enabled": True, "hostnames": ["shop.dev.example.org"]}})])

    def test_a_schedule_that_is_not_one_is_refused_showing_what_was_written(self):
        """QA of v1.0.0, F25: "`schedule` (a value not of a name shape, or of a credential's
        shape) does not match" hid the mistake it was reporting. A schedule is shown as
        written; a credential pasted where one goes is still never repeated."""
        cron = {"name": "janitor", "kind": "cron", "image": "ghcr.io/example/janitor", "schedule": "every tuesday"}
        proc = self.box.generate([project("one", {"prod": {}}, components=[dict(WEB), cron])])
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("`schedule` 'every tuesday' does not match", proc.stdout + proc.stderr)
        secret = "ghp_" + "a1B2c3D4e5F6g7H8i9J0"
        proc = self.box.generate([project("one", {"prod": {}}, components=[dict(WEB), dict(cron, schedule=secret)])])
        self.assertNotEqual(proc.returncode, 0)
        self.assertNotIn(secret, proc.stdout + proc.stderr)

    def test_duplicate_host_is_refused_and_bare_project_cannot_overwrite_probe(self):
        dup = [project("one", {"prod": {"hostnames": ["Same.Example.ORG."]}}),
               project("two", {"prod": {"hostnames": ["same.example.org"]}})]
        proc = self.box.generate(dup)
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("claimed by both", proc.stdout + proc.stderr)
        # the refusal names both owners (QA of v1.0.0, F25: `one/prod` is not a name's
        # shape, so the other project's name was replaced by a description of a value)
        self.assertIn("claimed by both 'one'/'prod' and 'two'/'prod'", proc.stdout + proc.stderr)

        # Round one's concrete overwrite: probe/foo-bar's established Application path
        # was probe-foo-bar.yaml, exactly foo/bar's old derived Probe path. The revised
        # identity must allow the project while keeping both Applications and both Probes.
        colliders = [project("probe", {"foo-bar": {"enabled": True}}),
                     project("foo", {"bar": {"enabled": True}})]
        for ordered in (colliders, list(reversed(colliders))):
            self.assert_generated(ordered)
            names = set(self.box.app_snapshot())
            self.assertIn("probe-foo-bar.yaml", names)
            self.assertIn("foo-bar.yaml", names)
            probes = [self.box.probe("probe", "foo-bar"),
                      self.box.probe("foo", "bar")]
            self.assertEqual(len({doc["metadata"]["name"] for doc in probes}), 2)
            self.assertTrue(all(self.box.probe_path(pid, env).stem ==
                                self.box.probe(pid, env)["metadata"]["name"]
                                for pid, env in (("probe", "foo-bar"), ("foo", "bar"))))

    def test_catalog_cannot_supply_a_probe_manifest(self):
        injected = project("safe", {"prod": {}})
        injected["_extra"] = {"prod": [{
            "apiVersion": "monitoring.coreos.com/v1", "kind": "Probe",
            "metadata": {"name": "supplied", "namespace": "monitoring"},
        }]}
        proc = self.box.generate([injected])
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("generated manifests are not authored in the catalog",
                      proc.stdout + proc.stderr)

        spec = importlib.util.spec_from_file_location("catalog_sync_fixture", GENERATOR)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        module.guard_writable([{"kind": "Probe", "metadata": {"name": "built"}}], "test")
        with self.assertRaises(SystemExit):
            module.guard_namespaces([{
                "kind": "Probe", "metadata": {"name": "built", "namespace": "tenant"},
            }], {"monitoring"}, "test")

    def test_long_probe_identities_stay_bounded_and_distinct(self):
        # These two valid names collided under the former 8-hex (32-bit) suffix. Keep
        # the concrete pair as an oracle instead of duplicating the digest calculation.
        pid = "p" * 40
        envs = ("environmentded3", "environment14a70")
        self.assert_generated([project(pid, {
            envs[0]: {"enabled": True, "hostnames": ["one.example.org"]},
            envs[1]: {"enabled": True, "hostnames": ["two.example.org"]},
        })])
        probes = [self.box.probe(pid, env) for env in envs]
        names = [doc["metadata"]["name"] for doc in probes]
        jobs = [doc["spec"]["jobName"] for doc in probes]
        self.assertTrue(all(len(name) <= 63 for name in names), names)
        self.assertEqual(len(set(names)), 2, names)
        self.assertEqual(len(set(jobs)), 2, jobs)
        self.assertEqual([self.box.probe_path(pid, env).stem for env in envs], names)
        self.assertEqual([doc["metadata"]["labels"]["orbit.local/env"] for doc in probes],
                         list(envs))

    def test_probe_identities_differing_only_past_old_truncation_stay_distinct(self):
        pid = "p" * 20
        envs = ("e" * 38 + "a", "e" * 38 + "b")
        self.assert_generated([project(pid, {
            envs[0]: {"enabled": True, "hostnames": ["one.example.org"]},
            envs[1]: {"enabled": True, "hostnames": ["two.example.org"]},
        })])
        names = [self.box.probe(pid, env)["metadata"]["name"] for env in envs]
        self.assertEqual(len(set(names)), 2, names)
        self.assertTrue(all(re.fullmatch(r"tenant-probe-[0-9a-f]{50}", name)
                            for name in names), names)
        self.assertEqual([self.box.probe_path(pid, env).stem for env in envs], names)

    def test_forced_digest_collision_fails_render_and_names_both_source_tuples(self):
        projects = [project("one", {"prod": {}}), project("two", {"prod": {}})]
        proc = self.box.generate_with_digest(projects, lambda payload: "0" * 64)
        self.assertNotEqual(proc.returncode, 0)
        message = proc.stdout + proc.stderr
        self.assertIn("generated identity collision", message)
        for value in ("'one'", "'two'", "'prod'", "'tenant-http'"):
            self.assertIn(value, message)
        self.assertFalse(any(path.name.startswith("tenant-probe-")
                             for path in (self.box.root / "clusters" / "apps").glob("*.yaml")),
                         "collision must fail before either colliding path is written")

    def test_blackbox_modules_keep_strict_2xx_and_add_strict_401(self):
        app = yaml.safe_load(BLACKBOX.read_text())
        values = yaml.safe_load(app["spec"]["source"]["helm"]["values"])
        modules = values["config"]["modules"]
        expected_http = {
            "preferred_ip_protocol": "ip4",
            "ip_protocol_fallback": False,
            "follow_redirects": True,
            "tls_config": {"insecure_skip_verify": False},
            "headers": {"User-Agent": "orbit-uptime-probe"},
        }
        for name, status in (("http_2xx", 200), ("http_401", 401)):
            self.assertIn(name, modules)
            self.assertEqual(modules[name]["prober"], "http")
            self.assertEqual(modules[name]["timeout"], "10s")
            http = dict(modules[name]["http"])
            self.assertEqual(http.pop("valid_status_codes"), [status])
            self.assertEqual(http, expected_http)
        self.assertEqual(set(modules), {"http_2xx", "http_401"},
                         "the module delta must be exactly the narrow 401 module")

    def test_environment_component_override_changes_only_that_env(self):
        """An overlay on dev must not touch prod's generated values or Deployment."""
        components = [{
            "name": "web", "kind": "web", "image": "example/web", "port": 8080,
            "path": "/", "memory": "256Mi", "cpu": "100m", "replicas": 2,
            # a shipped component: an untagged one renders idle (zero replicas) until its
            # first ship, which is not what this test is about
            "tag": "main-0000001",
        }]
        base = [project("svc", {
            "prod": {},
            "dev": {"enabled": True},
        }, components=components)]
        overridden = [project("svc", {
            "prod": {},
            "dev": {"enabled": True, "components": {
                "web": {"memory": "128Mi", "replicas": 1, "cpu": "50m",
                        "drain": 45},
            }},
        }, components=[dict(components[0])])]

        other = Sandbox()
        try:
            self.assert_generated(base)
            proc = other.generate(overridden)
            self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
            self.assertEqual(self.box.app_values("svc", "prod"),
                             other.app_values("svc", "prod"),
                             "prod values must stay byte-identical when only dev is overlaid")
            dev = other.app_values("svc", "dev")["components"][0]
            prod = other.app_values("svc", "prod")["components"][0]
            self.assertEqual(dev["memory"], "128Mi")
            self.assertEqual(dev["replicas"], 1)
            self.assertEqual(dev["cpu"], "50m")
            self.assertEqual(dev["drain"], 45)
            self.assertEqual(prod["memory"], "256Mi")
            self.assertEqual(prod["replicas"], 2)
            self.assertEqual(prod["cpu"], "100m")
            self.assertNotIn("drain", prod)

            if HELM:
                prod_dep = other.helm_kind("svc", "prod", "Deployment")
                dev_dep = other.helm_kind("svc", "dev", "Deployment")
                base_prod = self.box.helm_kind("svc", "prod", "Deployment")
                self.assertEqual(prod_dep, base_prod)
                self.assertEqual(dev_dep["spec"]["replicas"], 1)
                self.assertEqual(prod_dep["spec"]["replicas"], 2)
                dev_res = dev_dep["spec"]["template"]["spec"]["containers"][0]["resources"]
                prod_res = prod_dep["spec"]["template"]["spec"]["containers"][0]["resources"]
                self.assertEqual(dev_res["requests"]["memory"], "128Mi")
                self.assertEqual(prod_res["requests"]["memory"], "256Mi")
                self.assertEqual(dev_res["requests"]["cpu"], "50m")
                self.assertEqual(prod_res["requests"]["cpu"], "100m")
                self.assertEqual(
                    dev_dep["spec"]["template"]["spec"]["terminationGracePeriodSeconds"], 45)
                self.assertEqual(
                    prod_dep["spec"]["template"]["spec"]["terminationGracePeriodSeconds"], 30)
        finally:
            other.close()

    def test_unknown_override_key_is_refused(self):
        projects = [project("svc", {
            "prod": {},
            "dev": {"enabled": True, "components": {"web": {"image": "evil.example/x"}}},
        })]
        proc = self.box.generate(projects)
        self.assertNotEqual(proc.returncode, 0, "unknown overlay key rendered clean")
        message = proc.stdout + proc.stderr
        self.assertIn("image", message)
        self.assertIn("unknown key", message)

    def test_unknown_override_component_is_refused(self):
        projects = [project("svc", {
            "prod": {},
            "dev": {"enabled": True, "components": {"ghost": {"replicas": 1}}},
        })]
        proc = self.box.generate(projects)
        self.assertNotEqual(proc.returncode, 0, "unknown overlay component rendered clean")
        message = proc.stdout + proc.stderr
        self.assertIn("ghost", message)
        self.assertIn("is not a component", message)

    def test_catalog_sync_passes_database_parameters_through(self):
        projects = [project("svc", {"prod": {}}, database={
            "size": "2Gi", "memory": "256Mi",
            "parameters": {"max_connections": 60, "shared_buffers": "64MB"},
        })]
        self.assert_generated(projects)
        db = self.box.app_values("svc", "prod")["database"]
        self.assertEqual(db["parameters"], {
            "max_connections": "60",
            "shared_buffers": "64MB",
        })
        if HELM:
            cluster = self.box.helm_kind("svc", "prod", "Cluster")
            self.assertEqual(cluster["spec"]["postgresql"]["parameters"], {
                "max_connections": "60",
                "shared_buffers": "64MB",
            })

    def test_invalid_database_parameter_key_is_refused(self):
        projects = [project("svc", {"prod": {}}, database={
            "size": "2Gi",
            "parameters": {"max-connections": "60"},
        })]
        proc = self.box.generate(projects)
        self.assertNotEqual(proc.returncode, 0, "invalid GUC name rendered clean")
        message = proc.stdout + proc.stderr
        self.assertIn("additional properties 'max-connections' not allowed", message)


if __name__ == "__main__":
    unittest.main()
