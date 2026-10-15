#!/usr/bin/env python3
"""
Phase 6 decision 6, the tooling half: with Infisical gone, the vault's bootstrap tooling
goes too, and the verify stages check the plane that exists.

- ToolingGoneTest: the root ceremony, the vault provisioner, the vault session client,
  their tests and vectors, and the vault's OpenTofu module are deleted, and nothing in
  core still calls them.
- VerifyStagesTest: no live line of a verify stage, the shared instance reader, a skill
  or a workflow names Infisical or ESO; stage4 checks the sops plane; every stage parses.
- NoSecretDataTest: stage4 checks that Secrets exist and never reads their data (board
  rule: any read of a Secret's .data, even masked, counts as printing it).

Run as `python3 scripts/test_sops_only_tooling.py` or under pytest.
"""
import pathlib
import re
import subprocess
import unittest

HERE = pathlib.Path(__file__).resolve().parent
ROOT = HERE.parent
VERIFY = HERE / "verify"
STAGE4 = VERIFY / "stage4.sh"

RETIRED = re.compile(r"infisical|external-?secrets?|clustersecretstore", re.I)
TOOLS = re.compile(r"root-ceremony|provision-vault|vault-session|tofu/vault|srp-vectors")

RETIRED_PATHS = (
    "scripts/root-ceremony.sh",
    "scripts/provision-vault.sh",
    "scripts/vault-session.py",
    "scripts/test_root_ceremony.py",
    "scripts/test_provision_vault.py",
    "scripts/test_vault_session.py",
    "scripts/srp-vectors.json",
    "tofu/vault",
)

# The files this test does not scan for tool names: history (docs/), this file, and the
# desk's handover code, which still reads ExternalSecrets to adopt them on other instances.
UNSCANNED = ("docs/", "scripts/test_sops_only_tooling.py", "scripts/test_sops_only_tooling_r.py",
             "scripts/test_sops_only_plane.py", "scripts/test_sops_only_plane_r.py", "services/orbitd/")


def _live_lines(text: str):
    for n, line in enumerate(text.splitlines(), 1):
        s = line.strip()
        if s and not s.startswith("#"):
            yield n, line


def _tracked():
    out = subprocess.run(["git", "ls-files"], cwd=ROOT, capture_output=True, text=True,
                         check=True).stdout.split()
    return [ROOT / p for p in out if not p.startswith(UNSCANNED)]


def _scan(files, pattern):
    hits = {}
    for f in files:
        if not f.is_file():
            continue
        try:
            text = f.read_text()
        except UnicodeDecodeError:
            continue
        found = [(n, l.strip()[:120]) for n, l in _live_lines(text) if pattern.search(l)]
        if found:
            hits[str(f.relative_to(ROOT))] = found[:3]
    return hits


class ToolingGoneTest(unittest.TestCase):
    def test_the_vault_tooling_is_deleted(self):
        present = [p for p in RETIRED_PATHS if (ROOT / p).exists()]
        self.assertEqual(present, [], "vault bootstrap tooling still in core")

    def test_nothing_in_core_still_calls_it(self):
        self.assertEqual(_scan(_tracked(), TOOLS), {}, "a live line still names the vault tooling")

    def test_the_vendoring_manifest_names_none_of_it(self):
        text = (ROOT / "core-manifest.txt").read_text()
        self.assertEqual([l for _, l in _live_lines(text) if TOOLS.search(l)], [])


class VerifyStagesTest(unittest.TestCase):
    def test_no_stage_reader_skill_or_workflow_names_the_old_plane(self):
        files = sorted(VERIFY.glob("*.sh")) + [HERE / "lib-instance.sh"]
        files += [p for p in (ROOT / ".claude" / "skills").rglob("*") if p.is_file()]
        files += [ROOT / "services" / "agent-runtime" / "app.py",
                  ROOT / "services" / "intake" / "app.py"]
        self.assertEqual(_scan(files, RETIRED), {}, "live lines still name Infisical or ESO")

    def test_stage4_checks_the_sops_plane(self):
        text = STAGE4.read_text()
        live = "\n".join(l for _, l in _live_lines(text))
        self.assertIn("SopsSecret", live.replace("sopssecret", "SopsSecret"))
        self.assertIn("clusters/platform/secrets", live)

    def test_every_stage_parses(self):
        for f in sorted(VERIFY.glob("*.sh")) + [HERE / "lib-instance.sh"]:
            with self.subTest(stage=f.name):
                proc = subprocess.run(["bash", "-n", str(f)], capture_output=True, text=True)
                self.assertEqual(proc.returncode, 0, proc.stderr)


class NoSecretDataTest(unittest.TestCase):
    GET_SECRET = re.compile(r"\bget\s+(-n\s+\S+\s+|--namespace[= ]\S+\s+)?secrets?\b", re.I)
    DATA = re.compile(r"-o\s*(json|yaml)\b|-o(json|yaml)\b|\{\.data|\.data\[|\.data\.|go-template|-o\s*jsonpath=\S*data")

    def test_stage4_never_reads_a_secret_value(self):
        bad = []
        for n, line in _live_lines(STAGE4.read_text()):
            if self.GET_SECRET.search(line) and self.DATA.search(line):
                bad.append((n, line.strip()[:120]))
        self.assertEqual(bad, [], "stage4 reads a Secret's data; check existence only")

    def test_no_verify_stage_decrypts_a_sops_file_to_the_terminal(self):
        bad = {}
        for f in sorted(VERIFY.glob("*.sh")):
            found = [(n, l.strip()[:120]) for n, l in _live_lines(f.read_text())
                     if re.search(r"\bsops\s+(-d|--decrypt|decrypt)\b", l)
                     and not re.search(r">\s*/dev/null", l)]
            if found:
                bad[f.name] = found
        self.assertEqual(bad, {}, "a verify stage decrypts sops output where it can be seen")


if __name__ == "__main__":
    unittest.main()
