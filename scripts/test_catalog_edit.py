#!/usr/bin/env python3
"""scripts/catalog-edit.py: one project's entry is replaced or removed, and every other
line of the catalog stays exactly as a person wrote it."""
import importlib.util
import json
import os
import pathlib
import subprocess
import sys
import tempfile
import unittest

import yaml

ROOT = pathlib.Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "catalog-edit.py"
spec = importlib.util.spec_from_file_location("catalog_edit", SCRIPT)
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)

CATALOG = """# The catalog. Hand-written: comments are part of it.
version: 3

projects:
  # the first project, with a note about why it has two replicas
  - id: alpha
    name: Alpha            # shown in the UI
    hosted: self
    environments:
      prod: {}
      dev:
        enabled: true
    components:
      - name: web
        kind: web
        image: example/alpha-web
        port: 8080
        replicas: 2        # after the incident of March

  # beta is the one under test
  - id: beta
    name: Beta
    hosted: self
    database:
      size: 2Gi
    environments:
      prod: {}
    components:
      - name: api
        kind: web
        image: example/beta-api
        port: 8080
        path: /

  # gamma comes last and has a trailing comment below
  - id: gamma
    name: Gamma
    hosted: self
    environments:
      prod: {}
    components:
      - name: web
        kind: web
        image: example/gamma-web
        port: 8080
# the end
"""
TAGS = """tags:
  alpha/prod/web: v1
  beta/prod/api: v7
  beta/dev/api: v8
  gamma/prod/web: v2
"""


def entry(text: str, project: str) -> dict:
    return next(p for p in yaml.safe_load(text)["projects"] if p["id"] == project)


def lines_outside(text: str, project: str) -> list[str]:
    """Every line that is not part of the project's entry."""
    first, last, _ = mod._entry_lines(text, project)
    lines = text.split("\n")
    return lines[:first] + lines[last + 1:]


class ReplacingAnEntry(unittest.TestCase):
    def test_only_that_entrys_lines_change(self):
        for project in ("alpha", "beta", "gamma"):
            with self.subTest(project=project):
                new = dict(entry(CATALOG, project))
                new["components"] = [dict(new["components"][0], replicas=3, memory="384Mi")]
                out = mod.edit(CATALOG, project, new)
                self.assertEqual(entry(out, project), new)
                self.assertEqual(lines_outside(out, project), lines_outside(CATALOG, project),
                                 "a line outside the entry moved")
                for other in {"alpha", "beta", "gamma"} - {project}:
                    self.assertEqual(entry(out, other), entry(CATALOG, other))

    def test_the_comments_of_the_other_entries_and_between_entries_stay(self):
        out = mod.edit(CATALOG, "beta", dict(entry(CATALOG, "beta"), name="Beta Two"))
        for kept in ("# The catalog. Hand-written", "# the first project", "# shown in the UI", "# after the incident of March",
                     "# beta is the one under test", "# gamma comes last", "# the end"):
            self.assertIn(kept, out)
        self.assertIn("name: Beta Two", out)

    def test_an_unchanged_entry_moves_no_byte(self):
        self.assertEqual(mod.edit(CATALOG, "beta", entry(CATALOG, "beta")), CATALOG)

    def test_a_new_entry_is_written_the_way_the_catalog_is(self):
        out = mod.edit(CATALOG, "beta", dict(entry(CATALOG, "beta"), name="Beta Two"))
        first, last, _ = mod._entry_lines(out, "beta")
        block = out.split("\n")[first:last + 1]
        self.assertEqual(block[0], "  - id: beta")
        self.assertIn("    components:", block)
        self.assertIn("      - name: api", block, "a list sits indented under its key")

    def test_editing_twice_is_stable(self):
        new = dict(entry(CATALOG, "alpha"), name="A")
        once = mod.edit(CATALOG, "alpha", new)
        self.assertEqual(mod.edit(once, "alpha", new), once)

    def test_the_id_cannot_change_and_the_project_must_exist(self):
        with self.assertRaisesRegex(mod.Refused, "id cannot change"):
            mod.edit(CATALOG, "beta", dict(entry(CATALOG, "beta"), id="delta"))
        with self.assertRaisesRegex(mod.Refused, "is not in the catalog"):
            mod.edit(CATALOG, "delta", {"id": "delta"})
        with self.assertRaisesRegex(mod.Refused, "not a project id"):
            mod.edit(CATALOG, "Beta; rm", {"id": "Beta; rm"})
        with self.assertRaisesRegex(mod.Refused, "not a mapping"):
            mod.edit(CATALOG, "beta", ["id", "beta"])

    def test_a_value_yaml_would_read_differently_is_written_so_it_reads_back(self):
        """A string that looks like a number, a boolean or a null must come back a string."""
        new = dict(entry(CATALOG, "beta"), name="yes", note="0123", tag="null", colon="a: b # c")
        out = mod.edit(CATALOG, "beta", new)
        self.assertEqual(entry(out, "beta"), new)

    def test_a_catalog_it_cannot_splice_is_refused_untouched(self):
        flow = CATALOG.replace("""  - id: gamma
    name: Gamma
    hosted: self
    environments:
      prod: {}
    components:
      - name: web
        kind: web
        image: example/gamma-web
        port: 8080
""", "  - {id: gamma, name: Gamma, hosted: self, environments: {prod: {}}, components: []}\n")
        with self.assertRaisesRegex(mod.Refused, "written on one line"):
            mod.edit(flow, "gamma", {"id": "gamma"})
        twice = CATALOG.replace("- id: gamma", "- id: beta")
        with self.assertRaisesRegex(mod.Refused, "more than one project beta"):
            mod.edit(twice, "beta", {"id": "beta"})
        with self.assertRaisesRegex(mod.Refused, "not YAML"):
            mod.edit("projects: [unclosed", "beta", {"id": "beta"})


class RemovingAnEntry(unittest.TestCase):
    def test_the_entry_goes_and_nothing_else(self):
        for project in ("alpha", "beta", "gamma"):
            with self.subTest(project=project):
                out = mod.edit(CATALOG, project, None)
                ids = [p["id"] for p in yaml.safe_load(out)["projects"]]
                self.assertEqual(ids, [p for p in ("alpha", "beta", "gamma") if p != project])
                self.assertEqual(out.split("\n"), lines_outside(CATALOG, project))
                self.assertIn("# the end", out)

    def test_removing_the_only_project_leaves_an_empty_list_not_a_null(self):
        one = "version: 3\n\nprojects:\n  # the only one\n  - id: only\n    name: Only\n\n# notes below stay\n"
        out = mod.edit(one, "only", None)
        self.assertEqual(yaml.safe_load(out), {"version": 3, "projects": []})
        self.assertIn("# notes below stay", out)
        self.assertIn("# the only one", out)

    def test_its_tags_go_with_it(self):
        out = mod.drop_tags(TAGS, "beta")
        self.assertEqual(yaml.safe_load(out), {"tags": {"alpha/prod/web": "v1", "gamma/prod/web": "v2"}})
        self.assertEqual(mod.drop_tags(TAGS, "delta"), TAGS)
        # a project whose id is the start of another's keeps the other's tags
        both = TAGS + "  beta-two/prod/api: v9\n"
        self.assertIn("beta-two/prod/api", mod.drop_tags(both, "beta"))


class WhatChanged(unittest.TestCase):
    def test_paths_name_components_by_name(self):
        old = entry(CATALOG, "alpha")
        new = json.loads(json.dumps(old))
        new["components"][0]["replicas"] = 3
        new["environments"]["dev"]["memory"] = {"web": "384Mi"}
        new["hostnames"] = ["a.example.org"]
        del new["hosted"]
        self.assertEqual(sorted(mod.changed(old, new)),
                         ["components[web].replicas", "environments.dev.memory (added)", "hosted (removed)", "hostnames (added)"])
        self.assertEqual(mod.changed(old, old), [])


class TheCommand(unittest.TestCase):
    def run_in(self, argv, stdin=""):
        tmp = pathlib.Path(tempfile.mkdtemp())
        (tmp / "catalog").mkdir()
        (tmp / "catalog" / "projects.yaml").write_text(CATALOG, encoding="utf-8")
        (tmp / "catalog" / "tags.yaml").write_text(TAGS, encoding="utf-8")
        proc = subprocess.run([sys.executable, str(SCRIPT), *argv], cwd=tmp, input=stdin, capture_output=True, text=True,
                              env={"PATH": os.environ["PATH"]})
        return tmp, proc

    def test_set_reads_the_entry_from_stdin(self):
        new = dict(entry(CATALOG, "beta"), name="From stdin")
        tmp, proc = self.run_in(["set", "beta"], json.dumps(new))
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertEqual(entry((tmp / "catalog" / "projects.yaml").read_text(), "beta"), new)
        self.assertEqual((tmp / "catalog" / "tags.yaml").read_text(), TAGS)

    def test_remove_takes_the_tags_too(self):
        tmp, proc = self.run_in(["remove", "beta"])
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertNotIn("beta", (tmp / "catalog" / "projects.yaml").read_text().replace("# beta is the one under test", ""))
        self.assertNotIn("beta/", (tmp / "catalog" / "tags.yaml").read_text())

    def test_remove_writes_down_the_archive_a_database_leaves_in_the_bucket(self):
        """The removal deletes no backup, so the bucket keeps the project's archive, and a
        project made under the name again must not be pointed at it (docs/V1.md §8,
        2026-10-09). beta has a database; alpha has none and leaves nothing."""
        tmp, proc = self.run_in(["remove", "beta"])
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        retired = tmp / "catalog" / "retired.yaml"
        self.assertEqual(yaml.safe_load(retired.read_text()), {"retired": {"beta": 1}})
        self.assertTrue(retired.read_text().startswith("# Written by `catalog-edit.py remove`"))
        tmp, proc = self.run_in(["remove", "alpha"])
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertFalse((tmp / "catalog" / "retired.yaml").exists())

    def test_each_removal_of_the_name_moves_the_generation_on(self):
        beta = entry(CATALOG, "beta")
        self.assertEqual(yaml.safe_load(mod.retire(None, "beta", beta)), {"retired": {"beta": 1}})
        # made again (the generator gave it 2) and removed again; the other names stay
        again = mod.retire("retired:\n  beta: 1\n  zeta: 4\n", "beta", beta)
        self.assertEqual(yaml.safe_load(again), {"retired": {"beta": 2, "zeta": 4}})
        # one that named a later generation itself
        later = dict(beta, database=dict(beta["database"], archiveGeneration=6))
        self.assertEqual(yaml.safe_load(mod.retire("retired:\n  beta: 1\n", "beta", later)), {"retired": {"beta": 6}})
        self.assertIsNone(mod.retire("retired:\n  beta: 1\n", "alpha", entry(CATALOG, "alpha")))
        with self.assertRaises(mod.Refused):
            mod.retire("- beta\n", "beta", beta)

    def test_a_removal_by_hand_that_wrote_nothing_down_is_refused_before_anything_is_deleted(self):
        """remove-project.sh is the step both ways of removing share. The check sits after
        the dry run's exit and before the first backup or delete."""
        text = (ROOT / "scripts" / "remove-project.sh").read_text(encoding="utf-8")
        refused = text.index("catalog/retired.yaml does not name $P")
        self.assertLess(text.index("dry run: nothing changed"), refused)
        self.assertLess(refused, text.index("db-backup.sh\" "))
        self.assertLess(refused, text.index("kubectl delete ns"))
        self.assertIn("Nothing was deleted.", text)

    def test_a_refusal_writes_nothing_and_says_one_sentence(self):
        tmp, proc = self.run_in(["set", "beta"], json.dumps({"id": "other"}))
        self.assertEqual(proc.returncode, 1)
        self.assertEqual(proc.stdout.strip(), "the new entry's id is not beta: an entry's id cannot change")
        self.assertEqual((tmp / "catalog" / "projects.yaml").read_text(), CATALOG)
        tmp, proc = self.run_in(["set", "beta"], "{not: [json")
        self.assertEqual(proc.returncode, 1)
        self.assertEqual((tmp / "catalog" / "projects.yaml").read_text(), CATALOG)


if __name__ == "__main__":
    unittest.main()
