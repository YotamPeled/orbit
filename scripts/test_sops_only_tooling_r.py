#!/usr/bin/env python3
"""
The repair contract for the vault-tooling removal — run as
`python3 scripts/test_sops_only_tooling_r.py`. No cluster, no credentials, no network.

WHY THIS FILE EXISTS. The removal of the vault plane's tooling took two things with it that
were never the vault's:

  1. stage5-platform lost the LIVE SINGLE-SIGN-ON rows for the two consumers (the dashboards
     and the deploy tool) along with the gate that armed them. Only the gate named the retired
     plane; the rows themselves assert the OIDC chain. With them gone the identity plane is
     half-checked: the IdP can be up and every consumer silently off SSO with the stage green.
  2. stage4's Secret-existence row asks the cluster for the SEALED FILE'S OWN NAME instead of
     the names the operator actually renders (one Secret per entry in spec.secretTemplates).
     A file whose template is named differently, or which carries a second template, passes
     green while the real Secret is absent.

And three more the same commit left behind: a failed discovery call read as absence, no row
that goes red when a sealed file fails to decrypt, and a live runbook that still instructs the
operator to run two scripts that no longer exist.

LATER: the identity provider retired, and the sign-on rows from point 1 retired with it —
a row that can never run is deleted rather than keyed off for ever, and that deletion is
what SingleSignOnRowsTest now holds. The dashboards' own door and datasource rows stay.

HOW IT IS TESTED. The stage4 rows are asserted BEHAVIOURALLY against the same fixture and the
same strict fake kubectl the stage4 harness uses: build a fresh sops org, break exactly one
thing, and assert a NAMED FAIL line appears — a mutation that goes red for an unrelated reason
proves nothing about the row it was written for. The stage5 rows are asserted on the file,
because the platform stage's live rows need a real cluster to exercise.

THE BOARD RULE HOLDS THROUGHOUT: a row may ask whether a Secret EXISTS and may read a
SopsSecret's .status (the operator's decryption message, which is not secret data). No row may
select .data, even to compare it — so the restored SSO rows do NOT bring back the deploy tool's
break-glass login, which read the initial admin Secret's password.
"""
import os
import pathlib
import re
import shutil
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
_BYTECODE, sys.dont_write_bytecode = sys.dont_write_bytecode, True
from test_verify_secrets import (  # noqa: E402
    CIPHERTEXT, ROOT, build_repo, sealed_doc)
sys.dont_write_bytecode = _BYTECODE

STAGE4 = ROOT / "scripts" / "verify" / "stage4.sh"
STAGE5 = ROOT / "scripts" / "verify" / "stage5-platform.sh"
STAGE3 = ROOT / "scripts" / "verify" / "stage3.sh"


def _flat(text):
    """One line, comment markers and wrapping dropped — a claim that wraps across two lines
    is the same claim."""
    return " ".join(l.lstrip("#").strip() for l in text.splitlines())

# The strict fake kubectl of the stage4 harness, with the three knobs this contract needs:
# a discovery call that FAILS (rather than answering "nothing"), a SopsSecret .status the
# operator would write, and an argv log every assertion reads. Anything else is refused, so a
# row that starts reading something new fails loudly here rather than passing silently.
STUB = r"""#!/usr/bin/env bash
echo "$@" >> "$STUB_LOG"
if [ "${STUB_UNREACHABLE:-0}" = 1 ]; then exit 1; fi
case "$1" in --request-timeout=*) shift;; esac
case "$1" in
  -n)
    ns="$2"; shift 2
    # the desk's record that it applied the sealed tree (existence only)
    if [ "$1" = get ] && [ "$2" = configmap ] && [ "$3" = orbit-platform-inventory-secrets ]; then
      echo "configmap/orbit-platform-inventory-secrets"; exit 0
    fi
    echo "STUB-REFUSED: -n $ns $*" >&2; exit 2;;
  get)
    shift
    case "$1" in
      --raw) exit 0;;
      deploy)
        if [ "${STUB_OPERATOR_UP:-1}" = 1 ]; then echo "1"; else echo ""; fi
        exit 0;;
      sopssecret)
        name="$2"; ns=""; prev=""; jsonpath=""
        for a in "$@"; do
          case "$prev" in -n) ns="$a";; -o) jsonpath="$a";; esac
          prev="$a"
        done
        case " ${STUB_MISSING_SOPSSECRETS:-} " in *" $ns/$name "*) exit 1;; esac
        case "$jsonpath" in
          *.data*) echo "STUB-REFUSED: a row selected Secret data: $*" >&2; exit 2;;
          jsonpath*|*jsonpath*) printf '%s' "${STUB_SOPS_STATUS-Healthy}"; exit 0;;
        esac
        echo "sopssecret/$name"; exit 0;;
      secret)
        name="$2"; ns=""; prev=""
        for a in "$@"; do
          if [ "$prev" = "-n" ]; then ns="$a"; fi
          prev="$a"
        done
        case " $* " in *".data"*) echo "STUB-REFUSED: a row selected Secret data: $*" >&2; exit 2;; esac
        case " ${STUB_MISSING_SECRETS:-} " in
          *" $ns/$name "*) exit 1;;
          *) echo "secret/$name"; exit 0;;
        esac;;
    esac
    echo "STUB-REFUSED: get $*" >&2; exit 2;;
  api-resources)
    if [ "${STUB_DISCOVERY_RC:-0}" != 0 ]; then exit "${STUB_DISCOVERY_RC}"; fi
    if [ "${STUB_OLD_APIS:-0}" = 1 ]; then echo "externalsecrets.retired.example"; fi
    exit 0;;
esac
echo "STUB-REFUSED: $*" >&2; exit 2
"""


def run_stage4(tree, stub_env=None):
    """stage4 in that tree against the fake kubectl, with a kubeconfig FILE so the live
    rows arm. Returns (completed process, the argv the stage issued)."""
    env = dict(os.environ)
    bindir = tempfile.mkdtemp(prefix="orbit-j24br-kubectl-")
    stub = pathlib.Path(bindir) / "kubectl"
    stub.write_text(STUB)
    stub.chmod(0o755)
    env["PATH"] = bindir + os.pathsep + env.get("PATH", "")
    env["STUB_LOG"] = str(pathlib.Path(bindir) / "argv.log")
    pathlib.Path(env["STUB_LOG"]).write_text("")
    env.update(stub_env or {})
    kubeconfig = tree / "fake-kubeconfig"
    kubeconfig.write_text("apiVersion: v1\nkind: Config\n")
    env["KUBECONFIG"] = str(kubeconfig)
    try:
        proc = subprocess.run(["bash", "scripts/verify/stage4.sh"], cwd=tree,
                              capture_output=True, text=True, env=env)
        log = pathlib.Path(env["STUB_LOG"]).read_text()
        return proc, log
    finally:
        shutil.rmtree(bindir, ignore_errors=True)


def sealed_with_templates(name, namespace, templates):
    """A sealed file whose rendered Secret names are NOT its own name. `templates` is a list
    of (secret name, data lines) — the shape sops-secrets-operator actually renders by."""
    lines = ["apiVersion: isindir.github.com/v1alpha3",
             "kind: SopsSecret",
             "metadata:",
             "  name: %s" % name,
             "  namespace: %s" % namespace,
             "spec:",
             "  secretTemplates:"]
    for tname, data_lines in templates:
        lines.append("    - name: %s" % tname)
        lines.append("      data:")
        lines.extend("        %s" % l for l in data_lines)
    lines.extend(["sops:",
                  "  age:",
                  "    - recipient: age1fixturepublickey000000000000000000000000000000000",
                  "      enc: |",
                  "        -----BEGIN AGE ENCRYPTED FILE-----",
                  "        YWdlLWVuY3J5cHRpb24ub3JnL3YxCi0+IFgyNTUxOSBmaXh0dXJlCmZpeHR1cmU=",
                  "        -----END AGE ENCRYPTED FILE-----"])
    return "\n".join(lines) + "\n"


class _Stage4(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.mkdtemp(prefix="orbit-j24br-")
        cls.scaffold = pathlib.Path(cls._tmp) / "scaffold"
        build_repo(cls.scaffold)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls._tmp, ignore_errors=True)

    def tree(self, apply=None):
        tree = pathlib.Path(tempfile.mkdtemp(dir=self._tmp)) / "t"
        shutil.copytree(self.scaffold, tree)
        if apply is not None:
            apply(tree)
            env = dict(os.environ, GIT_AUTHOR_NAME="f", GIT_AUTHOR_EMAIL="f@f",
                       GIT_COMMITTER_NAME="f", GIT_COMMITTER_EMAIL="f@f")
            subprocess.run(["git", "add", "-A"], cwd=tree, env=env, capture_output=True)
            subprocess.run(["git", "commit", "-qm", "mutation"], cwd=tree, env=env,
                           capture_output=True)
        return tree

    def assertRedWith(self, proc, needle, mutation):
        self.assertNotEqual(proc.returncode, 0,
                            f"{mutation} left the stage GREEN:\n{proc.stdout[-3000:]}")
        fails = [l for l in proc.stdout.splitlines() if l.lstrip().startswith("FAIL")]
        self.assertTrue(any(needle in l for l in fails),
                        f"{mutation} went red, but no FAIL line mentions {needle!r} — the "
                        f"mutation may be caught by an unrelated row.\nFAIL lines were:\n"
                        + "\n".join(fails))


class RenderedSecretNamesTest(_Stage4):
    """The Secret-existence rows must ask for the names the OPERATOR renders."""

    def test_a_template_named_apart_from_its_file_is_the_name_checked(self):
        def apply(tree):
            (tree / "clusters/platform/secrets/team-a/api.yaml").write_text(
                sealed_with_templates("api", "team-a", [("api-real", [CIPHERTEXT])]))
        tree = self.tree(apply)
        proc, log = run_stage4(tree, {"STUB_MISSING_SECRETS": "team-a/api-real"})
        self.assertIn("get secret api-real", log,
                      "the stage never asked for the Secret the file renders — it asked:\n" + log)
        self.assertRedWith(proc, "api-real", "the rendered Secret is absent")

    def test_the_file_name_alone_is_not_taken_as_proof(self):
        """The failure the row exists for: a Secret named after the FILE exists, the one the
        file actually renders does not, and the stage must not call that green."""
        def apply(tree):
            (tree / "clusters/platform/secrets/team-a/api.yaml").write_text(
                sealed_with_templates("api", "team-a", [("api-real", [CIPHERTEXT])]))
        tree = self.tree(apply)
        proc, _ = run_stage4(tree, {"STUB_MISSING_SECRETS": "team-a/api-real"})
        self.assertNotEqual(proc.returncode, 0,
                            "a file whose rendered Secret is missing passed green:\n"
                            + proc.stdout[-3000:])

    def test_every_template_in_a_file_is_checked(self):
        def apply(tree):
            (tree / "clusters/platform/secrets/team-a/api.yaml").write_text(
                sealed_with_templates("api", "team-a",
                                      [("api", [CIPHERTEXT]),
                                       ("api-two", [CIPHERTEXT])]))
        tree = self.tree(apply)
        proc, log = run_stage4(tree, {"STUB_MISSING_SECRETS": "team-a/api-two"})
        self.assertIn("get secret api-two", log,
                      "the second template's Secret was never asked for — the stage asked:\n" + log)
        self.assertRedWith(proc, "api-two", "the second template's Secret is absent")

    def test_the_sealed_file_itself_is_still_asked_for_by_its_own_name(self):
        """The SopsSecret existence row keys off metadata.name — that one was right."""
        tree = self.tree()
        proc, log = run_stage4(tree, {"STUB_MISSING_SOPSSECRETS": "team-a/api"})
        self.assertIn("get sopssecret api", log)
        self.assertRedWith(proc, "team-a/api", "the sealed file is not live")

    def test_no_row_reads_a_value(self):
        """Existence only, asserted on what the stage ASKED, not on its text: the fake
        kubectl refuses any argv selecting .data, so a green run is the proof."""
        tree = self.tree()
        proc, log = run_stage4(tree)
        self.assertEqual(proc.returncode, 0,
                         "a healthy sops tree is not green:\n" + proc.stdout[-4000:])
        self.assertNotIn("STUB-REFUSED", proc.stdout + proc.stderr)
        self.assertNotIn(".data", log)


class DecryptionHealthTest(_Stage4):
    """A sealed file that fails to decrypt leaves the previous Secret in place and the sync
    Healthy — the existence rows cannot see it, so a row must read the operator's own status."""

    def test_an_unhealthy_sealed_file_goes_red(self):
        tree = self.tree()
        proc, log = run_stage4(tree, {"STUB_SOPS_STATUS": "Failed to decrypt: no key"})
        self.assertRegex(log, r"get sopssecret \S+ .*(-o jsonpath|status)",
                         "no row reads a sealed file's status — a decryption failure after a "
                         "rotation is invisible. The stage asked:\n" + log)
        self.assertRedWith(proc, "team-a/api", "the operator reports a decryption failure")

    def test_only_the_statuses_the_operator_calls_healthy_are_green(self):
        """The row must know what healthy LOOKS like, not list the ways it can fail: a
        denylist passes every state the operator invents next."""
        for status in ("Failed to decrypt: no key", "Error", "Unknown",
                       "sops metadata not found", ""):
            tree = self.tree()
            proc, _ = run_stage4(tree, {"STUB_SOPS_STATUS": status})
            self.assertNotEqual(proc.returncode, 0,
                                f"the operator status {status!r} passed as healthy:\n"
                                + proc.stdout[-2000:])

    def test_a_sealed_file_with_no_templates_is_red(self):
        """Counting a file as checked and then checking nothing in it is the shape of a
        row that certifies an empty set."""
        def apply(tree):
            (tree / "clusters/platform/secrets/team-a/api.yaml").write_text(
                sealed_with_templates("api", "team-a", []))
        tree = self.tree(apply)
        proc, _ = run_stage4(tree)
        self.assertNotEqual(proc.returncode, 0,
                            "a sealed file that renders no Secret passed:\n" + proc.stdout[-2000:])

    def test_a_healthy_status_is_green(self):
        tree = self.tree()
        proc, _ = run_stage4(tree, {"STUB_SOPS_STATUS": "Healthy"})
        self.assertEqual(proc.returncode, 0,
                         "a healthy tree went red on the status row:\n" + proc.stdout[-3000:])


class DiscoveryTest(_Stage4):
    """A check's silence is not a pass."""

    def test_a_failed_discovery_call_is_not_read_as_absence(self):
        tree = self.tree()
        proc, _ = run_stage4(tree, {"STUB_DISCOVERY_RC": "1"})
        self.assertNotEqual(proc.returncode, 0,
                            "a discovery call that FAILED was reported as 'nothing serves the "
                            "retired plane':\n" + proc.stdout[-3000:])
        self.assertNotIn("the retired plane's API group is absent", proc.stdout)

    def test_a_served_retired_group_is_still_red(self):
        tree = self.tree()
        proc, _ = run_stage4(tree, {"STUB_OLD_APIS": "1"})
        self.assertRedWith(proc, "retired", "the retired plane is still served")


class SingleSignOnRowsTest(unittest.TestCase):
    """What remains of the sign-on rows after the identity provider retired.

    The removal repair restored the dashboards' and the deploy tool's SSO rows; the
    retirement deleted them again — a row that can never run is deleted, not keyed off
    for ever. What stays is the shape of the repair: no row gates on the retired plane's
    kind, and no restored row reads a Secret's data. Asserted on the stage file: the
    platform stage's live rows need a real cluster."""

    @classmethod
    def setUpClass(cls):
        cls.text = STAGE5.read_text()

    def test_no_row_checks_a_retired_sign_on_chain(self):
        """A row that can never run is deleted. The dashboards' own door and datasource
        rows stay; the redirect/PKCE/issuer chain against the retired provider goes."""
        for needle in ("login/generic_oauth", "code_challenge_method=S256",
                       "auth/login?return_url", "grafana-oidc"):
            self.assertNotIn(needle, self.text,
                             "the stage still checks a sign-on chain nothing serves")

    def test_the_rows_arm_off_the_sealed_plane(self):
        self.assertNotRegex(self.text, r"(?i)externalsecret",
                            "the stage still gates a row on the retired plane's kind")

    def test_the_break_glass_password_row_does_not_come_back(self):
        """The restored rows stop at the OIDC chain. The row that base64-decoded the deploy
        tool's initial admin password stays deleted: the board rule refuses any read of a
        Secret's data, and this contract must not be satisfied by restoring it."""
        offenders = [f"{n}: {l.strip()[:120]}"
                     for n, l in enumerate(self.text.splitlines(), 1)
                     if "argocd-initial-admin-secret" in l or ".data.password" in l]
        self.assertEqual(offenders, [],
                         "a restored row reads the deploy tool's admin password:\n"
                         + "\n".join(offenders))


class RunbookTest(unittest.TestCase):
    """The operator-facing text must not instruct anyone to run what was deleted."""

    GONE = re.compile(r"root-ceremony\.sh|provision-vault\.sh")

    def live_docs(self):
        paths = [ROOT / "docs" / "ONBOARDING.md", ROOT / "docs" / "DEVELOPMENT.md"]
        paths.extend(sorted((ROOT / ".claude" / "skills").rglob("SKILL.md")))
        return [p for p in paths if p.is_file()]

    def test_the_runbook_does_not_call_the_deleted_scripts(self):
        hits = []
        for p in self.live_docs():
            for n, line in enumerate(p.read_text().splitlines(), 1):
                if self.GONE.search(line):
                    hits.append(f"{p.relative_to(ROOT)}:{n}: {line.strip()[:100]}")
        self.assertEqual(hits, [], "live operator text still runs deleted scripts:\n"
                                   + "\n".join(hits))

    def test_the_runbook_still_tells_an_operator_how_to_declare_a_secret(self):
        """Deleting the instructions is not the fix — the sections they were in must say how
        it is done now, on the sealed plane."""
        text = (ROOT / "docs" / "ONBOARDING.md").read_text()
        self.assertRegex(text, r"orbit secrets (mint|adopt|export|verify)",
                         "the runbook no longer tells an operator how to put a value in — the "
                         "vault instructions were removed without a replacement")

    def test_the_retired_design_doc_says_it_is_retired(self):
        p = ROOT / "docs" / "SECRET-PROVISIONING.md"
        if not p.is_file():
            return
        head = "\n".join(p.read_text().splitlines()[:15])
        self.assertRegex(head, r"(?i)superseded|retired|historical",
                         "the vault-plane design doc reads as current while its mechanism is "
                         "deleted — mark it, or delete it")


class LeftoversTest(unittest.TestCase):
    """What the removal left behind: text that describes deleted checks."""

    def test_stage3_drops_the_comment_for_the_check_it_deleted(self):
        self.assertNotIn("Generated per namespace now", _flat(STAGE3.read_text()),
                         "stage3 keeps a comment explaining a row that is gone")

    def test_stage4_has_no_dead_variable(self):
        self.assertNotIn("SOPSRC", STAGE4.read_text(),
                         "stage4 assigns a status variable nothing reads")

    def test_stage4_does_not_claim_what_it_does_not_say(self):
        self.assertNotIn("its kinds are named in this comment", _flat(STAGE4.read_text()),
                         "the comment says the retired kinds are named here; they are not")

    def test_the_ship_skill_does_not_overclaim_the_stage(self):
        p = ROOT / ".claude" / "skills" / "orbit-ship" / "SKILL.md"
        self.assertNotIn("no plaintext value is committed anywhere in the repo", _flat(p.read_text()),
                         "the skill claims the stage scans the whole repo; it scans the sealed "
                         "directory and .env filenames")


if __name__ == "__main__":
    unittest.main(verbosity=2)
