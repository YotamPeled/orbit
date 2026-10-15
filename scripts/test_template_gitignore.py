#!/usr/bin/env python3
"""The template's .gitignore: what a fresh instance checkout must never commit.

M4's fourth live run (docs/V1.md §8) stopped at `infrastructure`: the template a new
organization copies had no .gitignore, the run commits whatever changed in the checkout,
and what had changed included tofu's providers (250 MB: GitHub refused the push), the
state, the variables written from the form, and the run's own log. The test makes a
checkout the way the onboarding skill does (`git init`, copy the template), puts each of
those files where the run leaves them, and asks git what `git add -A` would take.
"""
import pathlib
import shutil
import subprocess
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parent.parent

NEVER = ("tofu/.terraform/providers/registry.opentofu.org/x/y/1.0.0/linux_amd64/terraform-provider-y_v1.0.0",
         "tofu/.terraform.lock.hcl", "tofu/terraform.tfstate", "tofu/terraform.tfstate.backup",
         "tofu/instance.auto.tfvars", "onboard-run.log", "onboard-run2.log", "verify.log",
         "scripts/verify/__pycache__/shown.cpython-313.pyc",
         "age.key", "env", ".env", "orbit-recovery-kit-acme.txt")
ALWAYS = ("tofu/adopt.tf", "tofu/import.tf", "instance/config.yaml", ".sops.yaml",
          "clusters/platform/secrets/monitoring/grafana-admin.yaml", "scripts/desk.sh", "catalog/projects.yaml")


class TemplateGitignoreTest(unittest.TestCase):
    def test_a_fresh_checkout_commits_the_instance_and_never_the_machines_own_files(self):
        with tempfile.TemporaryDirectory() as td:
            repo = pathlib.Path(td) / "acme-platform"
            shutil.copytree(ROOT / "template", repo)
            self.assertTrue((repo / ".gitignore").is_file(), "the template ships no .gitignore")
            subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True)
            for rel in NEVER + ALWAYS:
                p = repo / rel
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_text("x\n")
            subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True)
            staged = set(subprocess.run(["git", "-C", str(repo), "diff", "--cached", "--name-only"],
                                        check=True, capture_output=True, text=True).stdout.split())
            for rel in NEVER:
                self.assertNotIn(rel, staged, f"{rel} would be committed")
            for rel in ALWAYS:
                self.assertIn(rel, staged, f"{rel} is the instance's and would be left out")


if __name__ == "__main__":
    unittest.main(verbosity=2)
