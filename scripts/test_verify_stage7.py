"""verify stage7 (scenario regressions, docs/V1.md §1.3): pre-boot keys off, the
guardrail and isolation rows key off with no deployed component, every public door's
redirect is judged, and a missing desk CLI is red, never a pass. Stubs stand in for
kubectl, curl and orbit; the live rows were proven on the test box."""
from __future__ import annotations

import json
import os
import pathlib
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
from test_verify_runner import CONFIG_YAML  # noqa: E402

INGRESS = {"items": [{"spec": {"rules": [{"host": "app.fixture.org"}, {"host": "BAD HOST"}]}}]}


class Stage7Test(unittest.TestCase):
    def setUp(self):
        self.root = pathlib.Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        (self.root / "scripts" / "verify").mkdir(parents=True)
        for rel in ("scripts/verify/stage7-scenarios.sh", "scripts/verify/guardrails.py", "scripts/verify/shown.py",
                    "scripts/lib-instance.sh"):
            shutil.copy2(ROOT / rel, self.root / rel)
        (self.root / "instance").mkdir()
        (self.root / "instance" / "config.yaml").write_text(CONFIG_YAML)
        (self.root / "catalog").mkdir()
        (self.root / "catalog" / "tags.yaml").write_text("tags: {}\n")
        self.bin = self.root / "bin"; self.bin.mkdir()
        self.kubeconfig = self.root / "kubeconfig"

    def stub(self, name, body):
        p = self.bin / name
        p.write_text("#!/usr/bin/env bash\n" + body, newline="\n")
        p.chmod(0o755)

    def run_stage(self, with_orbit=True, env=None):
        if with_orbit:
            self.stub("orbit", "exit 1\n")
        path = f"{self.bin}{os.pathsep}{os.environ['PATH']}"
        if not with_orbit:
            # a PATH with no orbit at all (the desk image itself carries one in /usr/local/bin)
            py = self.bin / "python3"
            if not py.exists():
                py.symlink_to(sys.executable)
            path = f"{self.bin}{os.pathsep}/usr/bin{os.pathsep}/bin"
        env = dict(os.environ, PATH=path, KUBECONFIG=str(self.kubeconfig), **{"ORBIT_VERIFY_RETRY_S": "0", **(env or {})})
        return subprocess.run(["bash", str(self.root / "scripts" / "verify" / "stage7-scenarios.sh")],
                              cwd=self.root, env=env, capture_output=True, text=True, timeout=120)

    def test_pre_boot_keys_the_stage_off(self):
        proc = self.run_stage()
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("stage7-scenarios: 0 passed, 0 failed (pre-boot", proc.stdout)

    def test_with_nothing_shipped_the_doors_are_still_judged(self):
        self.kubeconfig.write_text("apiVersion: v1\n")
        ingress = json.dumps(INGRESS)
        self.stub("kubectl", f"""case "$*" in
  *"get --raw /version"*) echo '{{}}';;
  *"get ingress -A"*) echo '{ingress}';;
  *"get ingressroute -A"*) echo '{{"items": []}}';;
  *) exit 1;;
esac
""")
        self.stub("curl", 'case "$*" in *http://app.fixture.org/*) printf "301 https://app.fixture.org/";; *) printf "000 ";; esac\n')
        proc = self.run_stage()
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("no deployed component in catalog/tags.yaml yet", proc.stdout)
        self.assertIn("no tenant workload yet", proc.stdout)
        self.assertIn("ok    http://app.fixture.org/ redirects to https (301 https://app.fixture.org/)", proc.stdout)
        self.assertNotIn("BAD HOST", proc.stdout)
        self.assertIn("stage7-scenarios: 3 passed, 0 failed", proc.stdout)

    def test_a_door_that_serves_plain_http_is_red(self):
        self.kubeconfig.write_text("apiVersion: v1\n")
        self.stub("kubectl", f"""case "$*" in
  *"get --raw /version"*) echo '{{}}';;
  *"get ingress -A"*) echo '{json.dumps(INGRESS)}';;
  *) echo '{{"items": []}}';;
esac
""")
        self.stub("curl", 'printf "200 "\n')
        proc = self.run_stage()
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("FAIL  http://app.fixture.org/ does not redirect to https (200 )", proc.stdout)

    def test_a_door_that_gives_no_answer_once_is_asked_again(self):
        """One dropped connection on the operator machine's link is not a finding (docs/V1.md
        §8): 000 is asked again; the answer that comes is the one judged."""
        self.kubeconfig.write_text("apiVersion: v1\n")
        self.stub("kubectl", f"""case "$*" in
  *"get --raw /version"*) echo '{{}}';;
  *"get ingress -A"*) echo '{json.dumps(INGRESS)}';;
  *) echo '{{"items": []}}';;
esac
""")
        count = self.root / "curl.count"
        self.stub("curl", f'echo x >> "{count.as_posix()}"; if [ "$(wc -l < "{count.as_posix()}")" -lt 2 ]; then printf "000 "; else printf "301 https://app.fixture.org/"; fi\n')
        proc = self.run_stage(env={"ORBIT_VERIFY_RETRY_S": "0"})
        self.assertIn("ok    http://app.fixture.org/ redirects to https (301 https://app.fixture.org/)", proc.stdout)
        self.assertEqual(len(count.read_text().splitlines()), 2)

    def test_a_door_that_never_answers_is_red_after_three_asks_and_a_wrong_answer_is_asked_once(self):
        self.kubeconfig.write_text("apiVersion: v1\n")
        self.stub("kubectl", f"""case "$*" in
  *"get --raw /version"*) echo '{{}}';;
  *"get ingress -A"*) echo '{json.dumps(INGRESS)}';;
  *) echo '{{"items": []}}';;
esac
""")
        count = self.root / "curl.count"
        self.stub("curl", f'echo x >> "{count.as_posix()}"; printf "000 "\n')
        proc = self.run_stage(env={"ORBIT_VERIFY_RETRY_S": "0"})
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("FAIL  http://app.fixture.org/ does not redirect to https (000 )", proc.stdout)
        self.assertEqual(len(count.read_text().splitlines()), 3)
        count.unlink()
        self.stub("curl", f'echo x >> "{count.as_posix()}"; printf "200 "\n')
        proc = self.run_stage(env={"ORBIT_VERIFY_RETRY_S": "0"})
        self.assertIn("FAIL  http://app.fixture.org/ does not redirect to https (200 )", proc.stdout)
        self.assertEqual(len(count.read_text().splitlines()), 1)

    def test_a_deployed_component_without_the_desk_cli_is_red(self):
        self.kubeconfig.write_text("apiVersion: v1\n")
        (self.root / "catalog" / "tags.yaml").write_text("tags:\n  shop/dev/web: main-abc1234\n")
        self.stub("kubectl", """case "$*" in
  *"get --raw /version"*) echo '{}';;
  *"get deploy shop-web"*) exit 0;;
  *) echo '{"items": []}';;
esac
""")
        self.stub("curl", 'printf "000 "\n')
        proc = self.run_stage(with_orbit=False)
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("the `orbit` desk CLI is not on PATH — the guardrails CANNOT be probed", proc.stdout)


if __name__ == "__main__":
    unittest.main(verbosity=2)
