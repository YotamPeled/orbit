#!/usr/bin/env python3
"""The generator follows the instance's features (provisioning/features.yaml).

What catalog-sync writes for a project depends on four optional features: traces (the
tenant `Instrumentation`), dashboards (the project's dashboard), uptime (the project's
public probe and the dashboard's probe panel), databases (a `database:` needs the
operator) and backups (the bucket archive of a prod database). Every case runs the
repository's real catalog-sync.py in a synthetic temporary instance.
"""
import pathlib
import shutil
import subprocess
import sys
import tempfile
import unittest

import yaml

ROOT = pathlib.Path(__file__).resolve().parent.parent
TESTDATA = ROOT / "scripts" / "testdata" / "catalog-quota"
REGISTRY = ROOT / "template" / "provisioning" / "features.yaml"
ORBITD_NAMESPACE = pathlib.Path("template/provisioning/clusters/platform/config/orbitd/namespace.yaml")
OPTIONAL = [name for name, spec in yaml.safe_load(REGISTRY.read_text(encoding="utf-8"))["features"].items()
            if not (spec or {}).get("always")]
PROBE_PANEL, WAL_PANEL, DATABASE_PANEL = "probe_success", "cnpg_pg_stat_archiver_seconds_since_last_archival", "cnpg_backends_total"
BUCKET = {"provider": "hetzner", "endpoint": "https://fsn1.example.com", "buckets": {"platform": "fixture-platform"}}


class Instance:
    def __init__(self, features: dict | None):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.tmp.name)
        (self.root / "scripts").mkdir()
        shutil.copy2(ROOT / "scripts" / "catalog-sync.py", self.root / "scripts" / "catalog-sync.py")
        shutil.copytree(ROOT / "scripts" / "verify", self.root / "scripts" / "verify")
        shutil.copytree(ROOT / "charts" / "service", self.root / "charts" / "service")
        shutil.copytree(TESTDATA / "catalog", self.root / "catalog")
        shutil.copytree(TESTDATA / "instance", self.root / "instance")
        (self.root / "clusters" / "apps").mkdir(parents=True)
        (self.root / ORBITD_NAMESPACE).parent.mkdir(parents=True)
        shutil.copy2(ROOT / ORBITD_NAMESPACE, self.root / ORBITD_NAMESPACE)
        (self.root / "provisioning").mkdir()
        shutil.copy2(REGISTRY, self.root / "provisioning" / "features.yaml")
        config = yaml.safe_load((self.root / "instance" / "config.yaml").read_text())
        config["objectStorage"] = BUCKET
        if features is not None:
            config["features"] = features
        (self.root / "instance" / "config.yaml").write_text(yaml.safe_dump(config, sort_keys=False))

    def generate(self):
        return subprocess.run([sys.executable, "scripts/catalog-sync.py"], cwd=self.root,
                              capture_output=True, text=True)

    def docs(self):
        out = []
        for path in sorted((self.root / "clusters" / "apps").glob("*.yaml")):
            out += [(path.name, doc) for doc in yaml.safe_load_all(path.read_text()) if doc]
        return out

    def kinds(self, kind: str) -> list:
        return [doc for _, doc in self.docs() if doc.get("kind") == kind]

    def text(self) -> str:
        return "\n".join(path.read_text() for path in sorted((self.root / "clusters" / "apps").glob("*.yaml")))


def everything(**off) -> dict:
    return {name: name not in off for name in OPTIONAL}


class TheGeneratorFollowsTheFeatures(unittest.TestCase):
    def made(self, features):
        box = Instance(features)
        self.addCleanup(box.tmp.cleanup)
        proc = box.generate()
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        return box

    def test_with_everything_on_each_feature_leaves_its_mark(self):
        """The oracle for every case below: what an off feature must take away is there."""
        box = self.made(everything())
        self.assertTrue(box.kinds("Probe"), "no tenant Probe with uptime on")
        self.assertTrue(box.kinds("Instrumentation"), "no Instrumentation with traces on")
        # panels are named by their series: the dumped dashboard wraps a title across lines
        self.assertIn(PROBE_PANEL, box.text())
        self.assertIn(WAL_PANEL, box.text())
        self.assertIn("fixture-platform", box.text(), "a prod database does not archive to the bucket")
        self.assertTrue([name for name, _ in box.docs() if name.startswith("dashboard-")])

    def test_no_features_block_reads_as_everything_on(self):
        """An instance from before features changes nothing by upgrading."""
        self.assertEqual(self.made(None).text(), self.made(everything()).text())

    def test_uptime_off_writes_no_probe_and_no_probe_panel(self):
        box = self.made(everything(uptime=1))
        self.assertEqual(box.kinds("Probe"), [])
        self.assertNotIn(PROBE_PANEL, box.text())
        self.assertTrue(box.kinds("Instrumentation"), "turning uptime off took something else with it")

    def test_traces_off_writes_no_instrumentation(self):
        box = self.made(everything(traces=1))
        self.assertEqual(box.kinds("Instrumentation"), [])
        self.assertTrue(box.kinds("Probe"))

    def test_dashboards_off_writes_no_dashboard(self):
        box = self.made(everything(dashboards=1))
        self.assertEqual([name for name, _ in box.docs() if name.startswith("dashboard-")], [])
        self.assertTrue(box.kinds("Probe"))

    def test_backups_off_keeps_the_database_and_drops_the_bucket_archive(self):
        box = self.made(everything(backups=1))
        self.assertNotIn("fixture-platform", box.text())
        self.assertNotIn(WAL_PANEL, box.text())
        self.assertIn(DATABASE_PANEL, box.text(), "the database itself must stay")

    def with_a_database(self, box) -> str:
        catalog = yaml.safe_load((box.root / "catalog" / "projects.yaml").read_text())
        return next(p["id"] for p in catalog["projects"] if p.get("database") is not None)

    def test_a_project_made_under_a_removed_name_archives_under_the_next_generation(self):
        """A removed project's archive stays in the bucket and barman refuses a path that
        holds another database's files: S01's project, made again under its name, archived
        nothing for as long as it lived (docs/V1.md §8, 2026-10-09). The removal writes the
        generation down and the generator gives the next project of the name the one after."""
        box = Instance(everything())
        self.addCleanup(box.tmp.cleanup)
        self.assertEqual(box.generate().returncode, 0)
        self.assertNotIn("archiveGeneration", box.text(), "a name never removed archives at the plain path")
        pid = self.with_a_database(box)
        (box.root / "catalog" / "retired.yaml").write_text(f"retired:\n  {pid}: 2\n  someone-else: 5\n")
        proc = box.generate()
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("archiveGeneration: 3", box.text())
        self.assertNotIn("archiveGeneration: 6", box.text())

    def test_a_generation_a_removed_project_used_is_refused_and_a_later_one_is_kept(self):
        box = Instance(everything())
        self.addCleanup(box.tmp.cleanup)
        pid = self.with_a_database(box)
        (box.root / "catalog" / "retired.yaml").write_text(f"retired:\n  {pid}: 2\n")
        path = box.root / "catalog" / "projects.yaml"
        catalog = yaml.safe_load(path.read_text())

        def named(generation):
            for project in catalog["projects"]:
                if project["id"] == pid:
                    project["database"] = dict(project["database"] or {}, archiveGeneration=generation)
            path.write_text(yaml.safe_dump(catalog, sort_keys=False))
            return box.generate()

        proc = named(2)
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("a removed project of this name archived under generation 2", proc.stdout + proc.stderr)
        self.assertIn("it becomes 3", proc.stdout + proc.stderr)
        proc = named(7)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("archiveGeneration: 7", box.text())

    def test_a_retired_file_of_another_shape_is_refused(self):
        box = Instance(everything())
        self.addCleanup(box.tmp.cleanup)
        for bad in ("retired: [a]\n", "retired:\n  a: zero\n", "retired:\n  a: 0\n", "- a\n"):
            (box.root / "catalog" / "retired.yaml").write_text(bad)
            proc = box.generate()
            self.assertNotEqual(proc.returncode, 0, bad)
            self.assertIn("catalog/retired.yaml must be `retired:`", proc.stdout + proc.stderr)

    def test_databases_off_refuses_a_project_that_declares_one(self):
        """Refused by name, with the way out, and nothing half-written."""
        box = Instance(everything(databases=1, backups=1))
        self.addCleanup(box.tmp.cleanup)
        proc = box.generate()
        self.assertNotEqual(proc.returncode, 0)
        said = proc.stdout + proc.stderr
        self.assertIn("declares a database and the databases feature is off", said)
        self.assertIn("orbit features enable databases", said)
        self.assertEqual(box.docs(), [], "a refused catalog still wrote files")

    def test_databases_off_is_fine_for_a_catalog_without_one(self):
        box = Instance(everything(databases=1, backups=1))
        self.addCleanup(box.tmp.cleanup)
        catalog = yaml.safe_load((box.root / "catalog" / "projects.yaml").read_text())
        for project in catalog["projects"]:
            project.pop("database", None)
        (box.root / "catalog" / "projects.yaml").write_text(yaml.safe_dump(catalog, sort_keys=False))
        proc = box.generate()
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertNotIn(DATABASE_PANEL, box.text())


class WhatTheGeneratorRefusesByName(unittest.TestCase):
    """Found by the commit gate's live test (docs/V1.md §8, 2026-10-06): the generator
    accepted a component the chart then refused at render time, and a project key nothing
    reads. Both are refused at generation, naming the project and the key."""

    def refused(self, mutate):
        box = Instance(everything())
        self.addCleanup(box.tmp.cleanup)
        path = box.root / "catalog" / "projects.yaml"
        catalog = yaml.safe_load(path.read_text())
        mutate(catalog)
        path.write_text(yaml.safe_dump(catalog, sort_keys=False))
        proc = box.generate()
        self.assertNotEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertEqual(box.docs(), [], "a refused catalog still wrote files")
        return proc.stdout + proc.stderr

    def project(self, catalog, pid):
        return next(p for p in catalog["projects"] if p["id"] == pid)

    def test_a_component_named_like_the_database_backup_job_beside_a_database(self):
        said = self.refused(lambda c: self.project(c, "widgets")["components"].append(
            {"name": "db-backup", "kind": "worker", "image": "example/x", "memory": "64Mi"}))
        self.assertIn("the component name `db-backup` is taken by the project's own database backup job (widgets-db-backup)", said)

    def test_the_same_name_is_fine_where_there_is_no_database(self):
        box = Instance(everything())
        self.addCleanup(box.tmp.cleanup)
        path = box.root / "catalog" / "projects.yaml"
        catalog = yaml.safe_load(path.read_text())
        self.project(catalog, "bare")["components"].append({"name": "db-backup", "kind": "worker", "image": "example/x", "memory": "64Mi"})
        path.write_text(yaml.safe_dump(catalog, sort_keys=False))
        proc = box.generate()
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)

    def test_a_project_key_nothing_reads(self):
        for key in ("replicas", "enviroments", "bogusField"):
            with self.subTest(key=key):
                said = self.refused(lambda c, k=key: self.project(c, "widgets").__setitem__(k, 2))
                self.assertIn("widgets: unknown key", said)
                self.assertIn(key, said)
                self.assertIn("A project may carry:", said)

    def test_every_key_the_template_documents_and_the_fixtures_use_is_allowed(self):
        """The closed set must not refuse what core itself teaches or tests with."""
        import re
        source = (ROOT / "scripts" / "catalog-sync.py").read_text(encoding="utf-8")
        keys = set(re.findall(r'"([a-z_]+)"', source[source.index("PROJECT_KEYS = frozenset("):source.index("DB_BACKUP_COMPONENT =")]))
        template = (ROOT / "template" / "catalog" / "projects.yaml").read_text(encoding="utf-8")
        worked = template[template.index("# projects:\n#   - id: "):]
        worked = worked[:worked.index("\n#     components:") + len("\n#     components:")]
        documented = set(re.findall(r"^#   (?:- |  )([a-z_]+):", worked, re.M))
        self.assertTrue({"name", "repo", "database", "environments", "components"} <= documented)
        self.assertEqual(sorted(documented - keys), [])
        for path in sorted((ROOT / "scripts" / "testdata").rglob("projects.yaml")):
            doc = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
            for project in doc.get("projects") or []:
                self.assertEqual(sorted(set(project) - keys), [], f"{path}: {project.get('id')}")


if __name__ == "__main__":
    unittest.main()
