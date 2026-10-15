#!/usr/bin/env python3
"""Regression tests for catalog-derived per-namespace ResourceQuota.

The generator sizes each environment's quota from its components, one surge pod of
the largest web component, and the database if declared — not a hard-coded 3Gi.
Every generation case runs the repository's real catalog-sync.py in a synthetic
temporary instance. Nothing is written to the checkout and no network is used.

The testdata catalog (scripts/testdata/catalog-quota) is the oracle: its rendered
namespaces.yaml is committed, and a full-document diff must only ever move the
ResourceQuota docs. Adding a component and dropping the database are exercised
against the same generator, not against the function that produces the numbers.
"""
import importlib.util
import pathlib
import shutil
import subprocess
import sys
import tempfile
import unittest

import yaml


ROOT = pathlib.Path(__file__).resolve().parent.parent
GENERATOR = ROOT / "scripts" / "catalog-sync.py"
CHART = ROOT / "charts" / "service"
TESTDATA = ROOT / "scripts" / "testdata" / "catalog-quota"
EXPECTED_NAMESPACES = TESTDATA / "expected" / "namespaces.yaml"
ORBITD_NAMESPACE = (
    ROOT / "template" / "provisioning" / "clusters" / "platform"
    / "config" / "orbitd" / "namespace.yaml")


def _stage_orbitd_namespace(root: pathlib.Path) -> None:
    dest = (root / "template" / "provisioning" / "clusters" / "platform"
            / "config" / "orbitd" / "namespace.yaml")
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(ORBITD_NAMESPACE, dest)

# Hand-computed from the testdata catalog and the chart defaults this test pins
# below. widgets: web 1×256Mi/250m + importer 1×512Mi/20m + surge(web) 256Mi/250m
# + database 256Mi/100m + the chart's pg_dump Job 128Mi/20m (no bucket in the fixture,
# so the dump runs beside the database).
#   memory: 256+512+256+256+128 = 1408Mi
#   cpu:    250+20+250+100+20   = 640m → next 0.5 core = 1000m
#   pods:   1+1 + 1 surge + 1 db + 2 jobs/probes = 6
WIDGETS_QUOTA = {
    "requests.cpu": "1000m",
    "requests.memory": "1408Mi",
    "limits.memory": "1408Mi",
    "pods": "6",
    "persistentvolumeclaims": "4",
}

# bare: one web at chart defaults (256Mi/250m) + surge, no database.
#   memory: 256+256 = 512Mi
#   cpu:    250+250 = 500m (already a half-core)
#   pods:   1 + 1 surge + 0 db + 2 = 4 → floor 6
BARE_QUOTA = {
    "requests.cpu": "500m",
    "requests.memory": "512Mi",
    "limits.memory": "512Mi",
    "pods": "6",
    "persistentvolumeclaims": "4",
}

# The database term is the chart Cluster default (256Mi), because testdata names
# memory: 256Mi explicitly and the template default is the same number.
DATABASE_MEMORY_MI = 256
DUMP_MEMORY_MI = 128   # the chart's pg_dump Job, rendered for a database with no bucket


class Sandbox:
    def __init__(self, catalog_src=None):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.tmp.name)
        (self.root / "scripts").mkdir()
        shutil.copy2(GENERATOR, self.root / "scripts" / "catalog-sync.py")
        (self.root / "charts").mkdir()
        shutil.copytree(CHART, self.root / "charts" / "service")
        src = catalog_src or TESTDATA
        shutil.copytree(src / "catalog", self.root / "catalog")
        shutil.copytree(src / "instance", self.root / "instance")
        (self.root / "clusters" / "apps").mkdir(parents=True)
        _stage_orbitd_namespace(self.root)

    def close(self):
        self.tmp.cleanup()

    def generate(self):
        return subprocess.run(
            [sys.executable, "scripts/catalog-sync.py"], cwd=self.root,
            capture_output=True, text=True)

    def docs(self, name):
        path = self.root / "clusters" / "apps" / name
        return [doc for doc in yaml.safe_load_all(path.read_text()) if doc]

    def quota(self, namespace):
        matches = [doc for doc in self.docs("namespaces.yaml")
                   if doc.get("kind") == "ResourceQuota"
                   and doc["metadata"]["namespace"] == namespace]
        if len(matches) != 1:
            raise AssertionError(
                f"expected one ResourceQuota for {namespace}, found {len(matches)}")
        return matches[0]["spec"]["hard"]

    def namespaces_text(self):
        return (self.root / "clusters" / "apps" / "namespaces.yaml").read_text()

    def snapshot(self):
        return {
            path.name: path.read_bytes()
            for path in sorted((self.root / "clusters" / "apps").glob("*.yaml"))
        }


def _split_docs(text):
    """(kind, namespace, doc) for every YAML document in a generated stream."""
    out = []
    for doc in yaml.safe_load_all(text):
        if not doc:
            continue
        meta = doc.get("metadata") or {}
        out.append((doc.get("kind"), meta.get("namespace") or meta.get("name"), doc))
    return out


class CatalogQuotaTest(unittest.TestCase):
    def setUp(self):
        self.box = Sandbox()

    def tearDown(self):
        self.box.close()

    def assert_generated(self):
        proc = self.box.generate()
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        return proc

    def test_chart_defaults_match_the_hand_computation(self):
        """The hand-computed sums below are only an oracle while these hold."""
        values = yaml.safe_load((CHART / "values.yaml").read_text())
        self.assertEqual(values["defaultMemory"], "256Mi")
        self.assertEqual(values["defaultClass"], "interactive")
        self.assertEqual(values["resourceClasses"]["interactive"]["cpu"], "250m")
        self.assertEqual(values["resourceClasses"]["batch"]["cpu"], "20m")
        self.assertEqual(values["resourceClasses"]["background"]["cpu"], "10m")
        db = (CHART / "templates" / "database.yaml").read_text()
        self.assertIn('.cpu | default "100m"', db)
        self.assertIn('.memory | default "256Mi"', db)
        self.assertRegex(db, r"(?m)^\s*instances:\s*1\s*$")

    def test_testdata_catalog_matches_hand_computed_quota(self):
        self.assert_generated()
        self.assertEqual(self.box.quota("widgets-prod"), WIDGETS_QUOTA)
        self.assertEqual(self.box.quota("widgets-dev"), WIDGETS_QUOTA,
                         "dev and prod share the catalog's components and database")
        self.assertEqual(self.box.quota("bare-prod"), BARE_QUOTA)

    def test_rendered_namespaces_match_committed_expected_output(self):
        """Full-document diff: only ResourceQuota hard numbers may move.

        The committed expected file is the testdata render. A change that touches
        Namespace / Instrumentation / NetworkPolicy fails here
        even if the quota numbers happen to stay green.
        """
        self.assert_generated()
        self.assertTrue(EXPECTED_NAMESPACES.is_file(),
                        "scripts/testdata/catalog-quota/expected/namespaces.yaml "
                        "is missing — generate it from the testdata catalog")
        got = self.box.namespaces_text()
        expected = EXPECTED_NAMESPACES.read_text()
        if got == expected:
            return
        got_docs = _split_docs(got)
        exp_docs = _split_docs(expected)
        self.assertEqual([(k, n) for k, n, _ in got_docs],
                         [(k, n) for k, n, _ in exp_docs],
                         "generated namespaces.yaml gained or lost documents")
        drifts = []
        for (gk, gn, gdoc), (_, _, edoc) in zip(got_docs, exp_docs):
            if gdoc == edoc:
                continue
            if gk != "ResourceQuota":
                drifts.append(f"{gk} {gn} changed")
                continue
            # ResourceQuota may differ only in spec.hard; identity stays.
            gmeta, emeta = dict(gdoc), dict(edoc)
            ghard = gmeta.get("spec", {}).pop("hard", None)
            ehard = emeta.get("spec", {}).pop("hard", None)
            if gmeta != emeta:
                drifts.append(f"ResourceQuota {gn} changed outside spec.hard")
            elif ghard != ehard:
                # Allowed, and the hand-computed test is what judges the numbers.
                continue
        self.assertEqual(drifts, [],
                         "rendered-manifest diff moved something other than "
                         "ResourceQuota spec.hard:\n  " + "\n  ".join(drifts)
                         + "\nupdate the expected file only for ResourceQuota docs")
        # If we got here the only drift is quota hard numbers, but the committed
        # expected file is stale — fail so the expected output is extended.
        self.assertEqual(got, expected,
                         "ResourceQuota changed; extend "
                         "scripts/testdata/catalog-quota/expected/namespaces.yaml")

    def test_every_tenant_namespace_gets_the_orbitd_configmaps_role_and_binding(self):
        """orbitd's ConfigMap authority is namespaced: one Role + RoleBinding named
        orbit-applier-configmaps per namespace, configmaps create/patch/delete only,
        bound to the orbitd ServiceAccount the platform template declares (orbit/orbitd);
        never a Cluster* kind."""
        self.assert_generated()
        docs = self.box.docs("namespaces.yaml")
        namespaces = {d["metadata"]["name"] for d in docs if d["kind"] == "Namespace"}
        self.assertTrue(namespaces)
        roles = {d["metadata"]["namespace"]: d for d in docs if d["kind"] == "Role"}
        bindings = {d["metadata"]["namespace"]: d for d in docs if d["kind"] == "RoleBinding"}
        self.assertEqual(set(roles), namespaces)
        self.assertEqual(set(bindings), namespaces)
        self.assertFalse({d["kind"] for d in docs} & {"ClusterRole", "ClusterRoleBinding"})
        for ns in namespaces:
            role, rb = roles[ns], bindings[ns]
            self.assertEqual(role["metadata"]["name"], "orbit-applier-configmaps")
            self.assertEqual(role["apiVersion"], "rbac.authorization.k8s.io/v1")
            self.assertEqual(role["rules"], [{"apiGroups": [""], "resources": ["configmaps"],
                                              "verbs": ["create", "patch", "delete"]}])
            self.assertEqual(rb["metadata"]["name"], "orbit-applier-configmaps")
            self.assertEqual(rb["roleRef"], {"apiGroup": "rbac.authorization.k8s.io",
                                             "kind": "Role", "name": "orbit-applier-configmaps"})
            self.assertEqual(rb["subjects"], [{"kind": "ServiceAccount", "name": "orbitd",
                                               "namespace": "orbit"}])

    def test_env_memory_override_raises_only_that_env_quota(self):
        """An overlay that raises one component's memory must raise that env's
        quota and leave the other env's quota on the catalog default.

        widgets web is 256Mi; overlaying dev to 512Mi adds 256Mi on the replica
        and 256Mi on the surge pod of that same web component.
        """
        self.assert_generated()
        self.assertEqual(self.box.quota("widgets-prod"), WIDGETS_QUOTA)
        self.assertEqual(self.box.quota("widgets-dev"), WIDGETS_QUOTA)
        catalog = yaml.safe_load(
            (self.box.root / "catalog" / "projects.yaml").read_text())
        widgets = next(p for p in catalog["projects"] if p["id"] == "widgets")
        widgets["environments"]["dev"]["components"] = {"web": {"memory": "512Mi"}}
        (self.box.root / "catalog" / "projects.yaml").write_text(
            yaml.safe_dump(catalog, sort_keys=False))
        self.assert_generated()
        self.assertEqual(self.box.quota("widgets-prod"), WIDGETS_QUOTA,
                         "prod must keep the catalog default when only dev is overlaid")
        raised = self.box.quota("widgets-dev")
        self.assertGreater(
            _mi(raised["requests.memory"]), _mi(WIDGETS_QUOTA["requests.memory"]))
        self.assertGreater(
            _mi(raised["limits.memory"]), _mi(WIDGETS_QUOTA["limits.memory"]))
        # web 512 + importer 512 + surge(web) 512 + database 256 + dump 128 = 1920
        self.assertEqual(raised["requests.memory"], "1920Mi")
        self.assertEqual(raised["limits.memory"], "1920Mi")
        self.assertEqual(raised["pods"], WIDGETS_QUOTA["pods"])
        self.assertEqual(self.box.quota("bare-prod"), BARE_QUOTA)

    def test_adding_a_component_raises_the_quota(self):
        self.assert_generated()
        before = dict(self.box.quota("widgets-prod"))
        catalog = yaml.safe_load(
            (self.box.root / "catalog" / "projects.yaml").read_text())
        widgets = next(p for p in catalog["projects"] if p["id"] == "widgets")
        widgets["components"].append({
            "name": "reporter",
            "kind": "worker",
            "image": "example/reporter",
            "class": "background",
            "memory": "1Gi",
        })
        (self.box.root / "catalog" / "projects.yaml").write_text(
            yaml.safe_dump(catalog, sort_keys=False))
        self.assert_generated()
        after = self.box.quota("widgets-prod")
        self.assertGreater(
            _mi(after["requests.memory"]), _mi(before["requests.memory"]),
            "adding a 1Gi worker must raise requests.memory")
        self.assertGreater(
            _mi(after["limits.memory"]), _mi(before["limits.memory"]),
            "adding a 1Gi worker must raise limits.memory")
        self.assertGreaterEqual(int(after["pods"]), int(before["pods"]))
        # The extra replica is one more pod; widgets was already at the floor of 6.
        self.assertEqual(int(after["pods"]), int(before["pods"]) + 1)
        # The other project is untouched.
        self.assertEqual(self.box.quota("bare-prod"), BARE_QUOTA)
        # 1Gi extra on top of the hand-computed widgets sum.
        self.assertEqual(after["requests.memory"], "2432Mi")  # 1408 + 1024
        self.assertEqual(after["limits.memory"], "2432Mi")

    def test_fixture_without_a_database_omits_the_database_term(self):
        self.assert_generated()
        with_db = self.box.quota("widgets-prod")
        without = self.box.quota("bare-prod")
        self.assertEqual(
            _mi(with_db["requests.memory"]) - _mi(without["requests.memory"]),
            # widgets extras over bare: importer 512Mi. The database term (256Mi,
            # plus the 128Mi pg_dump Job a database with no bucket brings) is
            # under test; subtract importer to isolate it.
            # widgets = web 256 + importer 512 + surge 256 + db 256 + dump 128 = 1408
            # bare    = web 256 + surge 256                                    = 512
            # difference = 896 = importer 512 + database 256 + dump 128
            512 + DATABASE_MEMORY_MI + DUMP_MEMORY_MI)

        catalog = yaml.safe_load(
            (self.box.root / "catalog" / "projects.yaml").read_text())
        widgets = next(p for p in catalog["projects"] if p["id"] == "widgets")
        widgets.pop("database")
        (self.box.root / "catalog" / "projects.yaml").write_text(
            yaml.safe_dump(catalog, sort_keys=False))
        self.assert_generated()
        dropped = self.box.quota("widgets-prod")
        self.assertEqual(
            _mi(with_db["requests.memory"]) - _mi(dropped["requests.memory"]),
            DATABASE_MEMORY_MI + DUMP_MEMORY_MI,
            "dropping the database must remove exactly its memory request and its dump Job's")
        self.assertEqual(
            _mi(with_db["limits.memory"]) - _mi(dropped["limits.memory"]),
            DATABASE_MEMORY_MI + DUMP_MEMORY_MI)
        # CPU: 620m with db → 1000m; 520m without → 1000m. The half-core round
        # swallows 100m, so the cpu string may stay; memory is the term.
        self.assertEqual(dropped["persistentvolumeclaims"], "4")

    def test_chart_values_fall_back_to_the_generator_source_tree(self):
        """ROOT without a vendored chart still reads the chart this file shipped with.

        A sandbox that copies the generator cannot exercise this: __file__ moves
        with the copy. Importing the original and rebinding ROOT does.
        """
        spec = importlib.util.spec_from_file_location(
            "catalog_sync_quota_fallback", GENERATOR)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        empty = pathlib.Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, empty, ignore_errors=True)
        module.ROOT = empty
        values = module._chart_values()
        self.assertEqual(values["defaultMemory"], "256Mi")
        self.assertEqual(values["defaultClass"], "interactive")
        quota = module.derived_quota(
            [{"name": "web", "kind": "web", "image": "example/web"}], None)
        self.assertEqual(quota, BARE_QUOTA)

    def test_missing_chart_values_keeps_the_failure_message(self):
        """Neither the run root nor the copied generator's tree has the chart."""
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = pathlib.Path(tmp.name)
        (root / "scripts").mkdir()
        shutil.copy2(GENERATOR, root / "scripts" / "catalog-sync.py")
        shutil.copytree(TESTDATA / "catalog", root / "catalog")
        shutil.copytree(TESTDATA / "instance", root / "instance")
        (root / "clusters" / "apps").mkdir(parents=True)
        proc = subprocess.run(
            [sys.executable, "scripts/catalog-sync.py"], cwd=root,
            capture_output=True, text=True)
        self.assertNotEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        err = proc.stdout + proc.stderr
        self.assertIn("service chart values not found at", err)
        self.assertIn(str(root / "charts" / "service" / "values.yaml"), err)

    def test_instrumented_64mi_web_is_charged_init_memory(self):
        """A 64Mi web with language: is charged 128Mi — the init container.

        Kubernetes bills max(app container, init container). Replica and surge
        both pick up the floor.
        """
        mod = _load_generator()
        quota = mod.derived_quota([{
            "name": "web", "kind": "web", "image": "example/web",
            "memory": "64Mi", "language": "nodejs",
        }], None)
        self.assertEqual(quota["requests.memory"], "256Mi")  # 128 + surge 128
        self.assertEqual(quota["limits.memory"], "256Mi")

    def test_a_wal_archiving_database_charges_its_sidecar(self):
        """docs/V1.md §8: a prod database archiving WAL runs the barman-cloud sidecar
        (192Mi, 20m in the chart). Left out, the quota had no room for the surge pod and
        checkers-lab-prod's first ship timed out at 'exceeded quota'."""
        mod = _load_generator()
        web = [{"name": "web", "kind": "web", "image": "example/web", "memory": "256Mi"}]
        db = {"size": "1Gi", "memory": "256Mi"}
        plain = mod.derived_quota(web, db)
        archiving = mod.derived_quota(web, db, wal=True)
        self.assertEqual(plain["requests.memory"], "896Mi")      # web 256 + surge 256 + db 256 + dump 128
        self.assertEqual(archiving["requests.memory"], "960Mi")  # + sidecar 192
        self.assertEqual(archiving["limits.memory"], "960Mi")
        db_text = (CHART / "templates" / "database.yaml").read_text()
        self.assertRegex(db_text, r"instanceSidecarConfiguration:\s*\n\s*resources:\s*\n\s*requests:\s*\n"
                                  r"\s*cpu:\s*20m\s*\n\s*memory:\s*192Mi")

    def test_a_database_with_no_bucket_charges_its_pg_dump_job(self):
        """docs/V1.md §8: without a bucket the chart's pg_dump Job runs beside the database
        (once per deploy, waiting for the server, and nightly). Left out, s17-scratch-dev's
        per-deploy dump held the room the initdb pod needed and the database never
        bootstrapped. The archiving database renders no dump and is not charged for one."""
        mod = _load_generator()
        web = [{"name": "web", "kind": "worker", "image": "example/web", "memory": "64Mi"}]
        db = {"size": "1Gi", "memory": "256Mi"}
        self.assertEqual(mod.derived_quota(web, db)["requests.memory"], "448Mi")   # 64 + db 256 + dump 128
        self.assertEqual(mod.derived_quota(web, db)["limits.memory"], "448Mi")
        self.assertEqual(mod.derived_quota(web, dict(db, backup={"memory": "192Mi"}))["requests.memory"], "512Mi")
        self.assertEqual(mod.derived_quota(web, db, wal=True)["requests.memory"], "512Mi")  # + sidecar 192, no dump
        db_text = (CHART / "templates" / "database.yaml").read_text()
        self.assertIn('"memory" ($backup.memory | default "128Mi")', db_text)

    def test_instrumented_512mi_component_is_unchanged(self):
        """A 512Mi component already exceeds the init container; charge stays 512Mi."""
        mod = _load_generator()
        quota = mod.derived_quota([{
            "name": "web", "kind": "web", "image": "example/web",
            "memory": "512Mi", "language": "python",
        }], None)
        self.assertEqual(quota["requests.memory"], "1024Mi")  # 512 + surge 512
        self.assertEqual(quota["limits.memory"], "1024Mi")

    def test_uninstrumented_64mi_web_is_not_floored(self):
        """Without language: the chart emits no inject annotation; no INIT floor."""
        mod = _load_generator()
        quota = mod.derived_quota([{
            "name": "web", "kind": "web", "image": "example/web",
            "memory": "64Mi",
        }], None)
        self.assertEqual(quota["requests.memory"], "128Mi")  # 64 + surge 64
        self.assertEqual(quota["limits.memory"], "128Mi")

    def test_generator_charges_instrumented_64mi_web_at_init_floor(self):
        """Same 64Mi floor through the real generator, not just derived_quota."""
        self.assert_generated()
        catalog = yaml.safe_load(
            (self.box.root / "catalog" / "projects.yaml").read_text())
        widgets = next(p for p in catalog["projects"] if p["id"] == "widgets")
        web = next(c for c in widgets["components"] if c["name"] == "web")
        web["memory"] = "64Mi"
        web["language"] = "nodejs"
        (self.box.root / "catalog" / "projects.yaml").write_text(
            yaml.safe_dump(catalog, sort_keys=False))
        self.assert_generated()
        # web 128 (floored) + importer 512 + surge 128 + database 256 + dump 128 = 1152
        self.assertEqual(self.box.quota("widgets-prod")["requests.memory"], "1152Mi")
        self.assertEqual(self.box.quota("widgets-prod")["limits.memory"], "1152Mi")
        self.assertEqual(self.box.quota("bare-prod"), BARE_QUOTA)

    def test_generator_leaves_instrumented_512mi_component_unchanged(self):
        """Importer is already 512Mi; adding language: must not raise the quota."""
        self.assert_generated()
        self.assertEqual(self.box.quota("widgets-prod"), WIDGETS_QUOTA)
        catalog = yaml.safe_load(
            (self.box.root / "catalog" / "projects.yaml").read_text())
        widgets = next(p for p in catalog["projects"] if p["id"] == "widgets")
        importer = next(c for c in widgets["components"] if c["name"] == "importer")
        importer["language"] = "python"
        (self.box.root / "catalog" / "projects.yaml").write_text(
            yaml.safe_dump(catalog, sort_keys=False))
        self.assert_generated()
        self.assertEqual(self.box.quota("widgets-prod"), WIDGETS_QUOTA)

    def test_instrumentation_carries_init_resources(self):
        """Every language block pins the init-container resources; no operator default.

        OpenTelemetry Operator 0.158.0: java's field is `resources`; the other
        languages use `resourceRequirements`.
        """
        self.assert_generated()
        mod = _load_generator()
        expected = mod._instrumentation_resources()
        self.assertEqual(mod.INSTRUMENTATION_RESOURCE_FIELD["java"], "resources")
        for lang in ("nodejs", "python", "dotnet", "go", "apacheHttpd", "nginx"):
            self.assertEqual(
                mod.INSTRUMENTATION_RESOURCE_FIELD[lang], "resourceRequirements", lang)
        docs = [d for d in self.box.docs("namespaces.yaml")
                if d.get("kind") == "Instrumentation"]
        self.assertTrue(docs)
        for doc in docs:
            for lang in ("python", "dotnet", "nodejs", "java"):
                field = mod.INSTRUMENTATION_RESOURCE_FIELD[lang]
                other = ("resources" if field == "resourceRequirements"
                         else "resourceRequirements")
                self.assertEqual(
                    doc["spec"][lang][field], expected,
                    f"{doc['metadata']['namespace']} {lang}")
                self.assertNotIn(other, doc["spec"][lang])


def _netpol(docs, namespace):
    matches = [d for d in docs if d.get("kind") == "NetworkPolicy"
               and (d.get("metadata") or {}).get("name") == "default-deny-ingress"
               and (d.get("metadata") or {}).get("namespace") == namespace]
    if len(matches) != 1:
        raise AssertionError(
            f"expected one default-deny-ingress for {namespace}, found {len(matches)}")
    return matches[0]


def _orbit_ingress_rule(policy, orbit_ns):
    """The ingress rule whose from-list is exactly the orbitd namespace."""
    for rule in (policy.get("spec") or {}).get("ingress") or []:
        froms = rule.get("from") or []
        names = [
            ((src.get("namespaceSelector") or {}).get("matchLabels") or {})
            .get("kubernetes.io/metadata.name")
            for src in froms if isinstance(src, dict)
        ]
        if names == [orbit_ns]:
            return rule
    return None


def _rule_ports(rule):
    return [(p.get("protocol"), p.get("port")) for p in (rule.get("ports") or [])]


class CatalogOrbitWebCheckNetpolTest(unittest.TestCase):
    """Non-prod default-deny admits orbitd on web ports; prod is untouched."""

    def setUp(self):
        self.box = Sandbox()
        self.orbit_ns = yaml.safe_load(ORBITD_NAMESPACE.read_text())["metadata"]["name"]

    def tearDown(self):
        self.box.close()

    def assert_generated(self):
        proc = self.box.generate()
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        return proc

    def test_orbitd_namespace_is_read_from_the_manifest(self):
        """The admit-list name is the Namespace document's metadata.name."""
        mod = _load_generator()
        self.assertEqual(mod._orbitd_namespace(), self.orbit_ns)

    def test_admit_list_tracks_the_namespace_document_name(self):
        """A second copy of the name would stay 'orbit' when the document moves."""
        dest = (self.box.root / "template" / "provisioning" / "clusters" / "platform"
                / "config" / "orbitd" / "namespace.yaml")
        doc = yaml.safe_load(dest.read_text())
        doc["metadata"]["name"] = "orbit-control"
        dest.write_text(yaml.safe_dump(doc))
        self.assert_generated()
        policy = _netpol(self.box.docs("namespaces.yaml"), "widgets-dev")
        self.assertIsNotNone(_orbit_ingress_rule(policy, "orbit-control"))
        self.assertIsNone(_orbit_ingress_rule(policy, self.orbit_ns))

    def test_non_prod_policy_admits_orbit_on_exactly_the_web_ports(self):
        self.assert_generated()
        policy = _netpol(self.box.docs("namespaces.yaml"), "widgets-dev")
        rule = _orbit_ingress_rule(policy, self.orbit_ns)
        self.assertIsNotNone(rule, "widgets-dev has no orbitd ingress rule")
        self.assertEqual(_rule_ports(rule), [("TCP", 8080)])
        # The existing admit-list is a separate rule and must not gain orbit
        # or lose its unrestricted ports.
        other = [r for r in policy["spec"]["ingress"] if r is not rule]
        self.assertEqual(len(other), 1)
        self.assertNotIn("ports", other[0])
        from_names = [
            ((src.get("namespaceSelector") or {}).get("matchLabels") or {})
            .get("kubernetes.io/metadata.name")
            for src in other[0].get("from") or []
        ]
        self.assertNotIn(self.orbit_ns, from_names)

    def test_prod_policy_has_no_orbit_rule(self):
        self.assert_generated()
        docs = self.box.docs("namespaces.yaml")
        for ns in ("widgets-prod", "bare-prod"):
            policy = _netpol(docs, ns)
            self.assertIsNone(
                _orbit_ingress_rule(policy, self.orbit_ns),
                f"{ns} must not admit orbitd (prod check is the public origin)")
            self.assertEqual(len(policy["spec"]["ingress"]), 1)

    def test_two_web_ports_are_both_admitted_and_worker_ports_are_not(self):
        self.assert_generated()
        catalog = yaml.safe_load(
            (self.box.root / "catalog" / "projects.yaml").read_text())
        widgets = next(p for p in catalog["projects"] if p["id"] == "widgets")
        widgets["components"].append({
            "name": "admin",
            "kind": "web",
            "image": "example/admin",
            "port": 9090,
        })
        widgets["components"].append({
            "name": "metrics-worker",
            "kind": "worker",
            "image": "example/metrics-worker",
            "port": 9100,
            "metrics": {"port": 9100},
        })
        (self.box.root / "catalog" / "projects.yaml").write_text(
            yaml.safe_dump(catalog, sort_keys=False))
        self.assert_generated()
        docs = self.box.docs("namespaces.yaml")
        dev = _orbit_ingress_rule(_netpol(docs, "widgets-dev"), self.orbit_ns)
        self.assertIsNotNone(dev)
        self.assertEqual(_rule_ports(dev), [("TCP", 8080), ("TCP", 9090)])
        prod = _orbit_ingress_rule(_netpol(docs, "widgets-prod"), self.orbit_ns)
        self.assertIsNone(prod)

    def test_non_prod_without_web_ports_gains_no_orbit_rule(self):
        catalog = yaml.safe_load(
            (self.box.root / "catalog" / "projects.yaml").read_text())
        widgets = next(p for p in catalog["projects"] if p["id"] == "widgets")
        widgets["components"] = [
            c for c in widgets["components"] if c.get("kind") != "web"]
        widgets["components"].append({
            "name": "batch",
            "kind": "worker",
            "image": "example/batch",
            "class": "batch",
        })
        (self.box.root / "catalog" / "projects.yaml").write_text(
            yaml.safe_dump(catalog, sort_keys=False))
        self.assert_generated()
        policy = _netpol(self.box.docs("namespaces.yaml"), "widgets-dev")
        self.assertIsNone(_orbit_ingress_rule(policy, self.orbit_ns))
        self.assertEqual(len(policy["spec"]["ingress"]), 1)


def _load_generator():
    spec = importlib.util.spec_from_file_location("catalog_sync_quota", GENERATOR)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _mi(quantity: str) -> int:
    if not quantity.endswith("Mi"):
        raise AssertionError(f"quota memory {quantity!r} is not an Mi string")
    return int(quantity[:-2])


if __name__ == "__main__":
    unittest.main()
