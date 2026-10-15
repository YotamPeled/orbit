#!/usr/bin/env python3
"""
Static and behavioural guards for scripts/verify/stage4.sh — run as
`python3 scripts/test_verify_secrets.py`. No cluster, no credentials, no network.

WHY THIS FILE EXISTS NOW. stage4 is the secret plane's oracle and it was the one vendored
stage with no harness: scripts/test_verify_platform.py has run stage5-platform against
real scaffolds since 2026-09-07, and the same argument applies here word for word — every
org runs the copy it pinned, so a regression ships to every orbit with a green repo, and
the failures it guards against are invisible until a real org hits them.

WHAT IT GUARDS. The sops plane: sealed SopsSecret files under
clusters/platform/secrets/<namespace>/<name>.yaml, decrypted in place by the sops
operator. The git rows prove every file is age-sealed with no plaintext and namespaced
as its directory; the live rows prove the operator runs, every sealed file exists live
with a healthy sync, every named Secret exists (existence only — never a value), and
the retired plane's APIs are gone.

It is modelled on test_verify_platform.py deliberately, down to the mutation method:
build a fresh sops org the way ONBOARDING does, run the stage PRE-BOOT to establish
that it is green, then break exactly ONE thing and assert a NAMED FAIL line appears.
Asserting the named line rather than a non-zero exit is the whole point — a mutation
that goes red for an unrelated reason proves nothing about the row it was written for.

Live rows run against a strict fake kubectl on PATH (no cluster): it answers the six
call shapes the stage issues, refuses anything else, and logs every argv so the
existence-only property is asserted on what the stage actually asked, not on its text.

THE TREE IS A GIT REPOSITORY HERE, and that is not incidental: stage4's git rows call
`git ls-files`, which in a plain directory answers with silence — so a fixture that was
not a repository would make "no sealed files" pass for the wrong reason, forever.
"""
import os
import pathlib
import re
import shutil
import subprocess
import sys
import tempfile
import unittest

import yaml

# THE FIXTURE IS SHARED ON PURPOSE. `build_scaffold` is the one place that knows how a fresh
# org is born — copy template/, fill the example config, vendor what core-manifest.txt lists,
# run `make init` — and two copies of that would drift, which is exactly how a harness starts
# testing a tree no org ever has. `dont_write_bytecode` around the import so running the tests
# does not leave a scripts/__pycache__ in the repository.
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
_BYTECODE, sys.dont_write_bytecode = sys.dont_write_bytecode, True
from test_verify_platform import (  # noqa: E402
    INSTANCE_STRINGS, ROOT, build_scaffold)
sys.dont_write_bytecode = _BYTECODE

STAGE = ROOT / "scripts" / "verify" / "stage4.sh"
REL = "scripts/verify/stage4.sh"


def sealed_doc(name, namespace, data_lines, sops=True, kind="SopsSecret"):
    """A hand-derived sealed file. Values are obvious fakes in ENC[...] wrappers —
    ciphertext-shaped, never real, and the stage must never print them."""
    lines = ["apiVersion: isindir.github.com/v1alpha3",
             "kind: %s" % kind,
             "metadata:",
             "  name: %s" % name,
             "  namespace: %s" % namespace,
             "spec:",
             "  secretTemplates:",
             "    - name: %s" % name,
             "      data:"]
    lines.extend("        %s" % l for l in data_lines)
    if sops:
        lines.extend(["sops:",
                      "  age:",
                      "    - recipient: age1fixturepublickey000000000000000000000000000000000",
                      "      enc: |",
                      "        -----BEGIN AGE ENCRYPTED FILE-----",
                      "        YWdlLWVuY3J5cHRpb24ub3JnL3YxCi0+IFgyNTUxOSBmaXh0dXJlCmZpeHR1cmU=",
                      "        -----END AGE ENCRYPTED FILE-----"])
    return "\n".join(lines) + "\n"


CIPHERTEXT = ("API_TOKEN: "
              "ENC[AES256_GCM,data:fixture-ciphertext-1,iv:fixture-iv,tag:fixture-tag,type:str]")


class StageFileTest(unittest.TestCase):
    def setUp(self):
        self.assertTrue(STAGE.is_file(), f"{REL} is missing")
        self.text = STAGE.read_text()

    def test_it_parses_and_is_executable(self):
        proc = subprocess.run(["bash", "-n", str(STAGE)], capture_output=True, text=True)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertTrue(STAGE.stat().st_mode & 0o111, f"{REL} is not executable")

    def test_it_carries_no_instance_string(self):
        """The same guard test_verify_platform.py applies to stage5: the vendored
        stage runs in every org, so one org's hostname would fail all the others."""
        hits = []
        for needle in INSTANCE_STRINGS:
            rx = re.compile(re.escape(needle), re.I)
            for n, line in enumerate(self.text.splitlines(), 1):
                if rx.search(line):
                    hits.append(f"{REL}:{n}: {needle!r} in {line.strip()[:100]}")
        self.assertEqual(hits, [], "the vendored stage carries instance strings:\n"
                                   + "\n".join(hits))

    def test_it_takes_its_identity_from_lib_instance(self):
        """Positively: KUBECONFIG derives from lib-instance.sh, not from a literal
        and not from a second parser of instance/config.yaml."""
        self.assertIn('. "$(dirname "$0")/../lib-instance.sh"', self.text)
        self.assertRegex(self.text, r"\$\{?ORBIT_INSTANCE_NAME\b",
                         "$ORBIT_INSTANCE_NAME is never used — the stage keys its "
                         "pre-boot probe off a kubeconfig path it invented")

    def test_every_live_row_can_key_off_before_bootstrap(self):
        """The stage3 convention, asserted structurally, exactly as stage5-platform is:
        pre-boot is decided by the kubeconfig FILE and never by a reachability probe, and
        the two reasons a live row cannot run are worded apart."""
        self.assertRegex(self.text, r'PREBOOT=0\s*\n(#[^\n]*\n)*kubeconfig_present\(\)\{[^}]*\}\s*\nkubeconfig_present "\$KUBECONFIG" \|\| PREBOOT=1')
        self.assertIn("LIVEOFF=1", self.text)
        helper = self.text.split("keyoff_live(){", 1)[-1].split("\n}", 1)[0]
        self.assertIn("PRE-BOOT:", helper)
        self.assertIn("UNREACHABLE:", helper)

    def test_it_is_vendored_and_in_the_extraction_scope(self):
        manifest = (ROOT / "core-manifest.txt").read_text().splitlines()
        self.assertIn(REL, [l.strip() for l in manifest])
        scope = subprocess.run(
            [sys.executable, str(ROOT / "scripts" / "verify" / "no-instance-strings.py"),
             "--print-scope"], cwd=ROOT, capture_output=True, text=True)
        self.assertEqual(scope.returncode, 0, scope.stdout + scope.stderr)
        self.assertIn(REL, scope.stdout.split())


# The sealed tree every fixture below carries: one sealed file per namespace, each
# sealing keys nothing else seals, so a per-file live miss names exactly one file.
SEALED_FILES = {
    "clusters/platform/secrets/team-a/api.yaml": ("api", "team-a", [CIPHERTEXT]),
    "clusters/platform/secrets/team-b/web.yaml":
        ("web", "team-b",
         ["TOKEN_A: ENC[AES256_GCM,data:fixture-ciphertext-2,iv:fixture-iv,tag:fixture-tag,type:str]",
          "TOKEN_B: ENC[AES256_GCM,data:fixture-ciphertext-3,iv:fixture-iv,tag:fixture-tag,type:str]"]),
}


def build_repo(dest: pathlib.Path) -> None:
    """A sops scaffold that is also a git repository, because stage4's git rows ask git."""
    build_scaffold(dest, provider="sops")
    for rel, (name, namespace, data_lines) in SEALED_FILES.items():
        f = dest / rel
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(sealed_doc(name, namespace, data_lines))
    env = dict(os.environ, GIT_AUTHOR_NAME="fixture", GIT_AUTHOR_EMAIL="f@fixture.org",
               GIT_COMMITTER_NAME="fixture", GIT_COMMITTER_EMAIL="f@fixture.org")
    # gc.auto 0: the commit of a whole scaffold passes git's loose-object threshold and
    # starts a DETACHED `gc --auto`, which repacks .git/objects while the tests copytree
    # the scaffold ("No such file or directory" on an object, CI 2026-10-04).
    for cmd in (["git", "init", "-q", "-b", "main", "."], ["git", "config", "gc.auto", "0"],
                ["git", "add", "-A"], ["git", "commit", "-qm", "scaffold"]):
        proc = subprocess.run(cmd, cwd=dest, env=env, capture_output=True, text=True)
        if proc.returncode != 0:
            raise AssertionError(" ".join(cmd) + ":\n" + proc.stdout + proc.stderr)


STUB = r"""#!/usr/bin/env bash
# Strict fake kubectl: answers the six call shapes stage4.sh issues, refuses anything
# else (exit 2), and logs every argv for the existence-only assertion.
echo "$@" >> "$STUB_LOG"
if [ "${STUB_UNREACHABLE:-0}" = 1 ]; then exit 1; fi
case "$1" in --request-timeout=*) shift;; esac
case "$1" in
  -n)
    ns="$2"; shift 2
    # kubectl -n orbit get configmap orbit-platform-inventory-secrets: the desk's record
    # that it applied the sealed tree (existence only; a test that wants it absent sets
    # STUB_SECRETS_UNAPPLIED)
    if [ "$1" = get ] && [ "$2" = configmap ] && [ "$3" = orbit-platform-inventory-secrets ]; then
      [ "${STUB_SECRETS_UNAPPLIED:-0}" = 1 ] && exit 1
      echo "configmap/orbit-platform-inventory-secrets"; exit 0
    fi
    echo "STUB-REFUSED: -n $ns $*" >&2; exit 2;;
  get)
    shift
    case "$1" in
      --raw) exit 0;;  # /version: the cluster answers
      deploy)
        # get deploy -n sops ...: the operator's availability
        if [ "${STUB_OPERATOR_UP:-1}" = 1 ]; then echo "1"; else echo ""; fi
        exit 0;;
      sopssecret)
        name="$2"; ns=""; prev=""; jsonpath=""
        for a in "$@"; do
          case "$prev" in -n) ns="$a";; -o) jsonpath="$a";; esac
          prev="$a"
        done
        case " ${STUB_MISSING_SOPSSECRETS:-} " in
          *" $ns/$name "*) exit 1;;
        esac
        # The decryption-status row asks for .status.message; the operator's healthy string
        # is "Healthy" and a test that wants an unhealthy file sets STUB_SOPS_STATUS.
        case "$jsonpath" in
          *jsonpath*) printf '%s' "${STUB_SOPS_STATUS-Healthy}"; exit 0;;
        esac
        echo "sopssecret/$name"; exit 0;;
      secret)
        name="$2"; ns=""; prev=""
        for a in "$@"; do
          if [ "$prev" = "-n" ]; then ns="$a"; fi
          prev="$a"
        done
        case " ${STUB_MISSING_SECRETS:-} " in
          *" $ns/$name "*) exit 1;;
          *) echo "secret/$name"; exit 0;;
        esac;;
    esac
    echo "STUB-REFUSED: get $*" >&2; exit 2;;
  api-resources)
    # api-resources --api-group=<retired group> -o name
    if [ "${STUB_OLD_APIS:-0}" = 1 ]; then echo "externalsecrets.retired.example"; fi
    exit 0;;
esac
echo "STUB-REFUSED: $*" >&2; exit 2
"""


def run_stage(tree: pathlib.Path, live: bool = False, stub_env: dict | None = None):
    """The stage in that tree. Pre-boot by default (a KUBECONFIG path that does not
    exist); live with the fake kubectl when asked, with a real kubeconfig FILE so the
    pre-boot probe arms the live rows."""
    env = dict(os.environ)
    bindir = None
    if live:
        bindir = tempfile.mkdtemp(prefix="orbit-stage4-kubectl-")
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
    else:
        env["KUBECONFIG"] = str(tree / "no-such-kubeconfig")
    try:
        return subprocess.run(["bash", "scripts/verify/stage4.sh"],
                              cwd=tree, capture_output=True, text=True, env=env)
    finally:
        if bindir is not None:
            log = pathlib.Path(bindir) / "argv.log"
            run_stage.last_log = log.read_text() if log.is_file() else ""
            shutil.rmtree(bindir, ignore_errors=True)


run_stage.last_log = ""


class _Sealed(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if cls is _Sealed:
            raise unittest.SkipTest("base class")
        cls._tmp = tempfile.mkdtemp(prefix="orbit-stage4-")
        cls.scaffold = pathlib.Path(cls._tmp) / "scaffold"
        build_repo(cls.scaffold)
        cls.baseline = run_stage(cls.scaffold)

    @classmethod
    def tearDownClass(cls):
        if cls is not _Sealed:
            shutil.rmtree(cls._tmp, ignore_errors=True)

    def mutate(self, apply):
        tree = pathlib.Path(tempfile.mkdtemp(dir=self._tmp)) / "t"
        shutil.copytree(self.scaffold, tree)
        apply(tree)
        env = dict(os.environ, GIT_AUTHOR_NAME="f", GIT_AUTHOR_EMAIL="f@f",
                   GIT_COMMITTER_NAME="f", GIT_COMMITTER_EMAIL="f@f")
        subprocess.run(["git", "add", "-A"], cwd=tree, env=env, capture_output=True)
        subprocess.run(["git", "commit", "-qm", "mutation"], cwd=tree, env=env,
                       capture_output=True)
        return run_stage(tree)

    def assertRedWith(self, proc, needle, mutation):
        self.assertNotEqual(proc.returncode, 0,
                            f"{mutation} left the stage GREEN:\n{proc.stdout[-3000:]}")
        fails = [l for l in proc.stdout.splitlines() if l.lstrip().startswith("FAIL")]
        self.assertTrue(any(needle in l for l in fails),
                        f"{mutation} went red, but no FAIL line mentions {needle!r} — the "
                        f"mutation may be caught by an unrelated row.\nFAIL lines were:\n"
                        + "\n".join(fails))


class Round10SealedTest(_Sealed):
    """Area-1 sweep round 10 (breaker B8, B9)."""

    def test_a_tracked_file_named_like_a_credential_is_counted_not_shown(self):
        """Round 26 (auditor A1): a tracked sealed file (or a placeholder's directory) whose
        name passes the path grammar but is of a credential's shape was repeated."""
        def apply(tree):
            (tree / "clusters/platform/secrets/team-a/ghp_PLANTED0123456789abcdefghijklmnop.yaml").write_text("not: [yaml\n")
            (tree / "clusters/platform/secrets/PLANTED0123456789abcdefgh").mkdir()
            (tree / "clusters/platform/secrets/PLANTED0123456789abcdefgh/.gitkeep").write_text("")
        proc = self.mutate(apply)
        self.assertRedWith(proc, "under clusters/platform/secrets has a name not of a path shape, or of a credential's shape (not repeated) — rename it", "a token-named tracked file")
        self.assertRedWith(proc, "under clusters/platform/secrets has a path not of a path shape, or of a credential's shape (not repeated) — rename its directory", "a token-named placeholder directory")
        self.assertNotIn("PLANTED", proc.stdout + proc.stderr)

    def test_an_untracked_file_named_like_a_credential_is_counted_not_shown(self):
        """Round 26 (breaker B2): an untracked file whose name passes the path grammar but
        is of a credential's shape (ghp_…) was repeated in the red row."""
        tree = pathlib.Path(tempfile.mkdtemp(dir=self._tmp)) / "t"
        shutil.copytree(self.scaffold, tree)
        (tree / "clusters/platform/secrets/team-a/ghp_PLANTED0123456789abcdefghijklmnop.yaml").write_text("apiVersion: v1\nkind: Secret\nstringData: {token: PLANTED_VALUE_123}\n")
        proc = run_stage(tree)
        self.assertRedWith(proc, "under clusters/platform/secrets (a name not of a path shape, or of a credential's shape; not repeated) is UNTRACKED under the sealed tree", "a token-named untracked file")
        self.assertNotIn("PLANTED", proc.stdout + proc.stderr)

    def test_an_untracked_file_under_the_sealed_tree_is_red(self):
        """B8: the rows walked `git ls-files`, so a file on disk that git did not track
        (which the desk still applies) was never checked."""
        tree = pathlib.Path(tempfile.mkdtemp(dir=self._tmp)) / "t"
        shutil.copytree(self.scaffold, tree)
        (tree / "clusters/platform/secrets/team-a/PLANTED-untracked.yaml").write_text("apiVersion: v1\nkind: Secret\nstringData: {token: PLANTED_VALUE_123}\n")
        proc = run_stage(tree)
        self.assertRedWith(proc, "clusters/platform/secrets/team-a/PLANTED-untracked.yaml is UNTRACKED under the sealed tree", "an untracked file")
        self.assertNotIn("PLANTED_VALUE", proc.stdout + proc.stderr)

    def test_an_ignored_file_under_the_sealed_tree_is_red_too(self):
        """B1 (round 11): --exclude-standard hid a file that .git/info/exclude (or a
        .gitignore) hides from git but not from the renderer."""
        tree = pathlib.Path(tempfile.mkdtemp(dir=self._tmp)) / "t"
        shutil.copytree(self.scaffold, tree)
        (tree / "clusters/platform/secrets/team-a/ignored.yaml").write_text("apiVersion: v1\nkind: Secret\nstringData: {token: PLANTED_VALUE_123}\n")
        (tree / ".git/info/exclude").write_text("clusters/platform/secrets/team-a/ignored.yaml\n")
        self.assertEqual(subprocess.run(["git", "check-ignore", "-q", "clusters/platform/secrets/team-a/ignored.yaml"], cwd=tree).returncode, 0)
        proc = run_stage(tree)
        self.assertRedWith(proc, "clusters/platform/secrets/team-a/ignored.yaml is UNTRACKED under the sealed tree (ignored files count)", "an ignored file")
        self.assertNotIn("PLANTED_VALUE", proc.stdout + proc.stderr)

    def test_an_untracked_file_name_is_shown_only_when_path_shaped(self):
        """B2 (round 11): the untracked row repeated the file name verbatim."""
        tree = pathlib.Path(tempfile.mkdtemp(dir=self._tmp)) / "t"
        shutil.copytree(self.scaffold, tree)
        (tree / "clusters/platform/secrets/team-a/Bearer PLANTED_FILENAME_CREDENTIAL.yaml").write_text("x: y\n")
        proc = run_stage(tree)
        self.assertRedWith(proc, "untracked file #0 under clusters/platform/secrets (a name not of a path shape, or of a credential's shape; not repeated) is UNTRACKED", "an unshaped untracked name")
        self.assertNotIn("PLANTED", proc.stdout + proc.stderr)

    def test_a_dotted_namespace_is_red(self):
        """A7 (round 11): the namespace check used the object-name (subdomain) shape, so
        `team.prod` passed and reached the live rows; the API server refuses it."""
        def apply(tree):
            d = tree / "clusters/platform/secrets/team.prod"; d.mkdir()
            (d / "x.yaml").write_text(sealed_doc("x", "team.prod", [CIPHERTEXT]))
        proc = self.mutate(apply)
        self.assertRedWith(proc, "clusters/platform/secrets/team.prod/x.yaml seals a namespace that is not a Kubernetes namespace name (a DNS label", "a dotted namespace")

    def test_a_tracked_file_name_is_shown_only_when_path_shaped(self):
        """B4 (round 12): tracked sealed-tree and recovery/ file names were repeated
        verbatim in FAIL rows."""
        def apply(tree):
            (tree / "clusters/platform/secrets/team-a/password=PLANTED_FILENAME_CREDENTIAL.yaml").write_text("not: [yaml\n")
            (tree / "recovery").mkdir(); (tree / "recovery/password=PLANTED_RECOVERY_CREDENTIAL").write_text("x\n")
            (tree / "recovery/plain.txt").write_text("x\n")
        proc = self.mutate(apply)
        self.assertRedWith(proc, "tracked file #1 under clusters/platform/secrets has a name not of a path shape, or of a credential's shape (not repeated)", "an unshaped tracked name")
        self.assertRedWith(proc, "recovery/ holds 2 unsanctioned file(s) — delete them, no recovery backup is sanctioned: recovery/plain.txt (+1 name(s) not of a path shape, or of a credential's shape)", "an unshaped recovery name")
        self.assertNotIn("PLANTED", proc.stdout + proc.stderr)

    def test_a_placeholder_path_is_shown_only_when_path_shaped(self):
        """B4 (round 13): a non-empty .gitkeep under an unshaped directory name had its
        whole path repeated in the row."""
        def apply(tree):
            d = tree / "clusters/platform/secrets/Bearer PLANTED_PATH_CREDENTIAL"; d.mkdir()
            (d / ".gitkeep").write_text("x\n")
        proc = self.mutate(apply)
        self.assertRedWith(proc, "under clusters/platform/secrets has a path not of a path shape, or of a credential's shape (not repeated)", "an unshaped placeholder path")
        self.assertNotIn("PLANTED", proc.stdout + proc.stderr)

    def test_a_sealed_file_the_walker_cannot_read_is_red_by_class(self):
        """B9: a metadata that is a list (or any shape the walker did not model) raised
        out of the loop and the stage died with a traceback instead of a row."""
        def apply(tree):
            f = tree / "clusters/platform/secrets/team-a/api.yaml"
            f.write_text("apiVersion: isindir.github.com/v1alpha3\nkind: SopsSecret\nmetadata: [PLANTED-LIST]\nspec: {}\n")
        proc = self.mutate(apply)
        self.assertRedWith(proc, "clusters/platform/secrets/team-a/api.yaml cannot be checked (AttributeError)", "a list metadata")
        self.assertNotIn("Traceback", proc.stdout + proc.stderr); self.assertNotIn("PLANTED", proc.stdout + proc.stderr)


class Round21SealedTest(_Sealed):
    def test_a_sops_secret_of_another_api_version_is_red(self):
        """Round 21 (breaker B4): a document of kind SopsSecret under an apiVersion the
        operator does not watch renders nothing, and the stage accepted it as a live
        target (its kind was the only thing checked)."""
        def apply(tree):
            f = tree / "clusters/platform/secrets/team-a/api.yaml"
            self.assertIn("apiVersion: isindir.github.com/v1alpha3", f.read_text())
            f.write_text(f.read_text().replace("apiVersion: isindir.github.com/v1alpha3", "apiVersion: example.com/v1", 1))
        proc = self.mutate(apply)
        self.assertRedWith(proc, "is not a SopsSecret of the operator's API version (isindir.github.com/v1alpha3", "another apiVersion")


class Round19SealedTest(_Sealed):
    def test_a_duplicate_template_name_is_validated_before_it_is_repeated(self):
        """Round 19 (breaker B1): two templates both named after a token were reported as
        a duplicate with the name repeated — the shape check came second."""
        def apply(tree):
            f = tree / "clusters/platform/secrets/team-a/api.yaml"; text = f.read_text()
            head, _, templates = text.partition("  secretTemplates:\n")
            block = templates[:templates.index("sops:")].replace("name: api\n", "name: Bearer PLANTED_CREDENTIAL_19\n", 1)
            f.write_text(head + "  secretTemplates:\n" + block + block + templates[templates.index("sops:"):])
        proc = self.mutate(apply)
        self.assertRedWith(proc, "secretTemplate 1 has a name that is not a Kubernetes object name", "two templates with one token-shaped name")
        self.assertNotIn("PLANTED", proc.stdout + proc.stderr)


class Round18SealedTest(_Sealed):
    def test_two_producers_of_one_rendered_secret_are_red(self):
        """Round 18 (auditor A3 = breaker B4): two SopsSecrets (or one, twice) rendering
        the same Secret both passed, and the live rows certified a union nobody renders."""
        def across(tree):
            f = tree / "clusters/platform/secrets/team-a/api.yaml"
            (tree / "clusters/platform/secrets/team-a/other.yaml").write_text(f.read_text().replace("name: api\n", "name: other\n", 1))
        proc = self.mutate(across)
        self.assertRedWith(proc, "renders Secret team-a/api, which clusters/platform/secrets/team-a/", "a second SopsSecret rendering the same Secret")
        self.assertIn("already renders — one producer per rendered Secret", proc.stdout)
        def within(tree):
            f = tree / "clusters/platform/secrets/team-a/api.yaml"; text = f.read_text()
            head, _, templates = text.partition("  secretTemplates:\n")
            block = templates[:templates.index("sops:")]
            f.write_text(head + "  secretTemplates:\n" + block + block + templates[templates.index("sops:"):])
        proc = self.mutate(within)
        self.assertRedWith(proc, "renders Secret team-a/api twice — one producer per rendered Secret", "one SopsSecret rendering a Secret twice")


class Round17SealedTest(_Sealed):
    def test_two_files_sealing_one_identity_are_red(self):
        """Round 17 (breaker B4): two tracked files sealing SopsSecret team-a/api both
        passed, and the live rows certified whichever entry they found."""
        def apply(tree):
            f = tree / "clusters/platform/secrets/team-a/api.yaml"
            (tree / "clusters/platform/secrets/team-a/api-again.yaml").write_text(f.read_text())
        proc = self.mutate(apply)
        self.assertRedWith(proc, "seals team-a/api, which clusters/platform/secrets/team-a/api", "a second file sealing the same identity")
        self.assertIn("already seals — one sealed file per Secret", proc.stdout)


class Round14SealedTest(_Sealed):
    def test_a_symlinked_sealed_file_is_red(self):
        """Round 14 (breaker B7): a symlink in place of a sealed file was read through, so
        a link to a plaintext file elsewhere passed as sealed."""
        def apply(tree):
            f = tree / "clusters/platform/secrets/team-a/api.yaml"
            body = f.read_text(); f.unlink()
            (tree / "elsewhere.yaml").write_text(body)
            f.symlink_to("../../../../elsewhere.yaml")
        proc = self.mutate(apply)
        self.assertRedWith(proc, "is not a regular file (a symlink, or a special file)", "a symlinked sealed file")


class SealedTreeTest(_Sealed):
    """The git rows: every mutation breaks exactly one sealed-file property."""

    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.mkdtemp(prefix="orbit-stage4-sealed-")
        cls.scaffold = pathlib.Path(cls._tmp) / "scaffold"
        build_repo(cls.scaffold)
        cls.baseline = run_stage(cls.scaffold)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls._tmp, ignore_errors=True)

    def test_a_fresh_sops_tree_is_green_before_bootstrap(self):
        self.assertEqual(self.baseline.returncode, 0,
                         "a fresh sealed tree is NOT green pre-boot:\n"
                         + self.baseline.stdout[-4000:])
        self.assertIn("2 sealed SopsSecret file(s)", self.baseline.stdout)

    def test_a_name_shaped_like_an_option_never_reaches_kubectl(self):
        """Sweep area 1 (2026-09-13): the names reach kubectl as arguments, so only a
        Kubernetes object name (DNS-1123 subdomain) does; the FAIL names the file and the
        template's position, never the value."""
        def apply(tree):
            f = tree / "clusters/platform/secrets/team-a/api.yaml"
            f.write_text(f.read_text().replace("    - name: api\n", "    - name: --kubeconfig=PLANTED-OTHER\n"))
        proc = self.mutate(apply)
        self.assertRedWith(proc, "secretTemplate 1 has a name that is not a Kubernetes object name", "an option-shaped template name")
        self.assertNotIn("PLANTED", proc.stdout + proc.stderr)
        def apply2(tree):
            f = tree / "clusters/platform/secrets/team-a/api.yaml"
            f.write_text(f.read_text().replace("  name: api\n", "  name: -PLANTED\n", 1))
        proc = self.mutate(apply2)
        self.assertRedWith(proc, "Secret whose name is not a Kubernetes object name", "an option-shaped Secret name")
        self.assertNotIn("PLANTED", proc.stdout + proc.stderr)

    def test_a_plaintext_key_of_no_secret_key_shape_is_named_by_position(self):
        """Round 4 (breaker): the plaintext row repeated a data: key verbatim, and a key
        may hold any text. Only a key of a Secret key's shape is shown; the rest by index."""
        def apply(tree):
            f = tree / "clusters/platform/secrets/team-a/api.yaml"
            docs = list(yaml.safe_load_all(f.read_text()))
            for d in docs:
                for t in ((d.get("spec") or {}).get("secretTemplates") or []):
                    for key in ("data", "stringData"):
                        if isinstance(t.get(key), dict):
                            t[key]["PLANTED token=KEY_CREDENTIAL_123456789"] = "plain"
            f.write_text(yaml.safe_dump_all(docs))
        proc = self.mutate(apply)
        self.assertRedWith(proc, "key #", "an oddly named plaintext key")
        self.assertIn("(its name is not of a Secret key's shape)", proc.stdout)
        self.assertNotIn("PLANTED", proc.stdout + proc.stderr)

    def test_a_plaintext_key_of_a_credentials_shape_is_named_by_position(self):
        """Round 24 (breaker B1): a key that passes the Secret-key grammar but reads like a
        token (`ghp_…`, a hex body) was repeated as the key name. Shown by index instead."""
        def apply(tree):
            f = tree / "clusters/platform/secrets/team-a/api.yaml"
            docs = list(yaml.safe_load_all(f.read_text()))
            for d in docs:
                for t in ((d.get("spec") or {}).get("secretTemplates") or []):
                    for key in ("data", "stringData"):
                        if isinstance(t.get(key), dict):
                            t[key]["ghp_PLANTED0123456789abcdefghijklmnop"] = "plain"
            f.write_text(yaml.safe_dump_all(docs))
        proc = self.mutate(apply)
        self.assertRedWith(proc, "key #", "a token-shaped plaintext key")
        self.assertNotIn("PLANTED", proc.stdout + proc.stderr)

    def test_a_value_that_only_starts_like_ciphertext_is_red(self):
        """Sweep area 1 (2026-09-13, breaker): `ENC[` alone was accepted; the whole sops
        envelope (cipher, data, iv, tag, type) is required now."""
        def apply(tree):
            f = tree / "clusters/platform/secrets/team-a/api.yaml"
            f.write_text(re.sub(r"ENC\[AES256_GCM,[^\]]*\]", "ENC[PLANTED-PLAINTEXT", f.read_text(), count=1))
        proc = self.mutate(apply)
        self.assertRedWith(proc, "holds plaintext", "a bare ENC[ prefix")
        self.assertNotIn("PLANTED", proc.stdout + proc.stderr)

    @unittest.skipUnless(shutil.which("sops") and shutil.which("age-keygen"), "sops and age-keygen are needed")
    def test_a_file_the_real_sealer_writes_is_green(self):
        """Round 3 (2026-09-13, breaker + auditor): the round-2 metadata check demanded an
        ENC[AGE1 file key and failed every file sops actually writes (the age file key is
        an armored age file). A SopsSecret encrypted by the installed sops with a
        throwaway age key passes the stage; the key never leaves its temporary directory."""
        def apply(tree):
            keydir = pathlib.Path(tempfile.mkdtemp(dir=self._tmp))
            gen = subprocess.run(["age-keygen", "-o", str(keydir / "key.txt")], capture_output=True, text=True, timeout=60)
            self.assertEqual(gen.returncode, 0, gen.stderr)
            pub = next(l.split(":", 1)[1].strip() for l in (keydir / "key.txt").read_text().splitlines() if l.startswith("# public key:"))
            f = tree / "clusters/platform/secrets/team-a/api.yaml"
            f.write_text(sealed_doc("api", "team-a", ["DB_URL: postgres://x:fixture-not-a-secret@db/x"], sops=False))
            enc = subprocess.run(["sops", "--encrypt", "--age", pub, "--encrypted-regex", "^(data|stringData)$", "--in-place", str(f)],
                                 capture_output=True, text=True, timeout=120)
            self.assertEqual(enc.returncode, 0, enc.stderr)
            self.assertIn("-----BEGIN AGE ENCRYPTED FILE-----", f.read_text())
            self.assertNotIn("fixture-not-a-secret", f.read_text())
        proc = self.mutate(apply)
        self.assertEqual(proc.returncode, 0, "a file the real sealer wrote went red:\n" + proc.stdout[-3000:])

    def test_a_scalar_data_block_is_red(self):
        """Round 2 (breaker): `data:` holding a string instead of a map walked past the
        per-key envelope check. A data: that is not a map is plaintext, whatever it holds."""
        def apply(tree):
            f = tree / "clusters/platform/secrets/team-a/api.yaml"
            f.write_text(re.sub(r"\n(\s+)data:\n(?:\1\s+\S.*\n)+", r"\n\1data: PLANTED-PLAINTEXT\n", f.read_text(), count=1))
        proc = self.mutate(apply)
        self.assertRedWith(proc, "holds plaintext", "a scalar data: block")
        self.assertNotIn("PLANTED", proc.stdout + proc.stderr)

    def test_a_hand_written_sops_block_is_red(self):
        """Round 2 (breaker): `sops: {age: yes}` passed as metadata. The block must have
        the shape sops writes: a list of recipient + enc."""
        armored = "-----BEGIN AGE ENCRYPTED FILE-----\nUExBTlRFRA==\n-----END AGE ENCRYPTED FILE-----"
        for shape in ("PLANTED-handwritten", ["PLANTED-handwritten"], [{"PLANTED": 1}],
                      [{"recipient": "age1PLANTED", "enc": "PLANTED"}], [{"recipient": "PLANTED", "enc": armored}],
                      # round 3 (breaker + auditor): the ENC[...] envelope wraps VALUES; the file key is armored
                      [{"recipient": "age1fixturepublickey000000000000000000000000000000000", "enc": "ENC[AGE1,PLANTED]"}]):
            with self.subTest(shape=shape):
                def apply(tree, shape=shape):
                    f = tree / "clusters/platform/secrets/team-a/api.yaml"
                    doc = yaml.safe_load(f.read_text())
                    doc["sops"]["age"] = shape
                    f.write_text(yaml.safe_dump(doc))
                proc = self.mutate(apply)
                self.assertRedWith(proc, "sops age metadata", "a sops block that is not sops' own shape")
                self.assertNotIn("PLANTED", proc.stdout + proc.stderr)

    def test_a_placeholder_with_content_is_red(self):
        """B8 (round 9): any file named .gitkeep was exempt; only an empty regular file is."""
        def apply(tree):
            (tree / "clusters/platform/secrets/team-a/.gitkeep").write_text("PLANTED-PLAINTEXT-PASSWORD\n")
        proc = self.mutate(apply)
        self.assertRedWith(proc, "is a placeholder that is not an empty regular file", "a .gitkeep with content")
        self.assertNotIn("PLANTED", proc.stdout + proc.stderr)

    def test_a_sealed_file_repeating_a_key_is_red(self):
        """B1 (round 8): YAML keeps the LAST value of a repeated key, so a plaintext
        first occurrence sat in git behind a sealed second one and the plane was green."""
        def apply(tree):
            f = tree / "clusters/platform/secrets/team-a/api.yaml"
            lines = f.read_text().splitlines(keepends=True)
            i = next(n for n, l in enumerate(lines) if re.match(r"\s+\S+: ENC\[", l))
            key = lines[i].split(":")[0]
            lines.insert(i, key + ": PLANTED-PLAINTEXT-VALUE-1234567890\n")
            f.write_text("".join(lines))
        proc = self.mutate(apply)
        self.assertRedWith(proc, "repeats a key", "a duplicated data key")
        self.assertNotIn("PLANTED", proc.stdout + proc.stderr)

    def test_a_plaintext_value_is_red(self):
        """The property the whole plane reduces to: a value in git that is not
        ciphertext must fail, naming the key but never the value."""
        def apply(tree):
            f = tree / "clusters/platform/secrets/team-a/api.yaml"
            f.write_text(f.read_text().replace("ENC[AES256_GCM", "plaintext-value"))
        proc = self.mutate(apply)
        self.assertRedWith(proc, "holds plaintext", "unsealing one value")
        self.assertIn("API_TOKEN", proc.stdout)
        self.assertNotIn("plaintext-value", proc.stdout)

    def test_a_file_with_no_sops_block_is_red(self):
        """A hand-written file shaped like a sealed one but never encrypted."""
        def apply(tree):
            f = tree / "clusters/platform/secrets/team-a/api.yaml"
            text = f.read_text()
            text = text[:text.index("sops:")]
            f.write_text(text)
        self.assertRedWith(self.mutate(apply), "no sops age metadata block",
                           "dropping the sops metadata block")

    def test_a_file_sealing_another_namespace_is_red(self):
        """The sealed namespace is the directory: a file moved across namespaces
        without re-sealing would decrypt into the wrong one."""
        def apply(tree):
            src = tree / "clusters/platform/secrets/team-a/api.yaml"
            dst = tree / "clusters/platform/secrets/team-b/api.yaml"
            dst.write_text(src.read_text())
            src.unlink()
        self.assertRedWith(self.mutate(apply), "lives under",
                           "moving a sealed file to another namespace's directory")

    def test_two_documents_in_one_file_are_red(self):
        """One SopsSecret per file: a second document would never be decrypted."""
        def apply(tree):
            f = tree / "clusters/platform/secrets/team-a/api.yaml"
            f.write_text(f.read_text() + "---\nkind: ConfigMap\n")
        self.assertRedWith(self.mutate(apply), "one SopsSecret per file",
                           "appending a second document to a sealed file")

    def test_a_non_sopssecret_kind_is_red(self):
        def apply(tree):
            f = tree / "clusters/platform/secrets/team-a/api.yaml"
            f.write_text(f.read_text().replace("kind: SopsSecret", "kind: ConfigMap"))
        self.assertRedWith(self.mutate(apply), "not a SopsSecret",
                           "replacing the sealed kind with a plain kind")

    def test_no_sealed_tree_at_all_is_red(self):
        """Fail closed: an org with no sealed files has no secret plane, and a
        missing directory reads the same as an empty one."""
        def apply(tree):
            shutil.rmtree(tree / "clusters/platform/secrets")
        self.assertRedWith(self.mutate(apply), "no SopsSecret files",
                           "deleting the whole sealed tree")

    def test_a_committed_dotenv_is_red(self):
        """The failure mode sealed Secrets replace, findable only through git."""
        self.assertRedWith(
            self.mutate(lambda tree: (tree / ".env.production").write_text("A=b\n")),
            "a .env file is committed", "committing a .env file")


class LiveTest(_Sealed):
    """The live rows against a strict fake kubectl: the cluster boundary is the
    only thing faked, and the double refuses anything but the stage's own calls."""

    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.mkdtemp(prefix="orbit-stage4-live-")
        cls.scaffold = pathlib.Path(cls._tmp) / "scaffold"
        build_repo(cls.scaffold)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls._tmp, ignore_errors=True)

    def live(self, **env):
        return run_stage(self.scaffold, live=True, stub_env=env)

    def test_a_traced_run_never_shows_the_operator_status_message(self):
        """Round 5 (breaker): the SopsSecret status message (an operator's decryption
        error quotes what it read) sat in a shell variable, so `bash -x` printed it."""
        env = dict(os.environ, STUB_SOPS_STATUS="decryption failed: PLANTED-STATUS-CREDENTIAL-123456789")
        bindir = tempfile.mkdtemp(prefix="orbit-stage4-trace-"); self.addCleanup(shutil.rmtree, bindir, ignore_errors=True)
        stub = pathlib.Path(bindir) / "kubectl"; stub.write_text(STUB); stub.chmod(0o755)
        env["PATH"] = bindir + os.pathsep + env["PATH"]; env["STUB_LOG"] = str(pathlib.Path(bindir) / "argv.log")
        kubeconfig = self.scaffold / "fake-kubeconfig"; kubeconfig.write_text("apiVersion: v1\nkind: Config\n")
        env["KUBECONFIG"] = str(kubeconfig)
        proc = subprocess.run(["bash", "-x", "scripts/verify/stage4.sh"], cwd=self.scaffold, capture_output=True, text=True, env=env)
        self.assertNotIn("PLANTED", proc.stdout + proc.stderr)
        self.assertIn("did not decrypt", proc.stdout) if "did not decrypt" in proc.stdout else self.assertNotEqual(proc.returncode, 0)

    def test_a_booted_cluster_with_an_empty_sealed_tree_is_red(self):
        """Round 5 (breaker): "0 sealed file(s), every one sealed" was green live; before
        bootstrap it stays an [UNVERIFIED] note (the pre-boot contract)."""
        for f in (self.scaffold / "clusters" / "platform" / "secrets").rglob("*.yaml"):
            f.unlink()
        self.addCleanup(lambda: (shutil.rmtree(self.scaffold, ignore_errors=True), build_repo(self.scaffold)))
        (self.scaffold / "clusters" / "platform" / "secrets" / ".gitkeep").write_text("")
        subprocess.run(["git", "add", "-A"], cwd=self.scaffold, capture_output=True)
        subprocess.run(["git", "-c", "user.name=f", "-c", "user.email=f@f", "commit", "-qm", "empty"], cwd=self.scaffold, capture_output=True)
        proc = self.live()
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("FAIL  sealed tree: the sealed plane holds no SopsSecret file at all", proc.stdout)
        proc = run_stage(self.scaffold)
        self.assertIn("[UNVERIFIED] PRE-BOOT: the sealed plane holds no SopsSecret file at all", proc.stdout)

    def test_a_healthy_cluster_is_green_and_names_every_row(self):
        proc = self.live()
        self.assertEqual(proc.returncode, 0,
                         "a healthy cluster is NOT green:\n" + proc.stdout[-4000:])
        for needle in ("the sops operator is available",
                       "all 2 sealed file(s) in git exist live as SopsSecrets",
                       "the secrets app carries a stored inventory",
                       "all 2 Secret(s) the sealed files name exist",
                       "the retired plane's API group is absent"):
            self.assertIn(needle, proc.stdout, f"no row says {needle!r}:\n"
                                               + proc.stdout[-3000:])

    def test_no_live_invocation_reads_a_secret_value(self):
        """The board rule, asserted on what the stage actually asked: no kubectl
        argv may ask for json/yaml output or touch .data, even masked."""
        proc = self.live()
        self.assertEqual(proc.returncode, 0, proc.stdout[-4000:])
        for line in run_stage.last_log.splitlines():
            self.assertNotRegex(line, r"-o\s*(json|yaml)\b",
                                f"a live call reads a whole object: {line!r}")
            self.assertNotIn(".data", line, f"a live call touches .data: {line!r}")

    def test_a_missing_operator_is_red(self):
        self.assertRedWith(self.live(STUB_OPERATOR_UP="0"),
                           "no ready Deployment in ns sops",
                           "stopping the sops operator")

    def test_a_sealed_file_missing_live_is_red(self):
        """One sealed file deleted live: the row names exactly that file."""
        proc = self.live(STUB_MISSING_SOPSSECRETS="team-b/web")
        self.assertRedWith(proc, "team-b/web", "deleting one live SopsSecret")
        fails = [l for l in proc.stdout.splitlines() if l.lstrip().startswith("FAIL")]
        self.assertFalse(any("team-a/api" in l for l in fails),
                         "the healthy file was named too:\n" + "\n".join(fails))

    def test_a_secret_missing_live_is_red(self):
        proc = self.live(STUB_MISSING_SECRETS="team-a/api")
        self.assertRedWith(proc, "team-a/api", "deleting one rendered Secret")

    def test_a_sealed_tree_the_desk_never_applied_is_red(self):
        """Existence is not enough: SopsSecrets that exist with no stored inventory for
        the secrets app are objects nothing keeps in step with git."""
        proc = self.live(STUB_SECRETS_UNAPPLIED="1")
        self.assertRedWith(proc, "no stored inventory", "removing the secrets app's inventory")

    def test_a_served_retired_api_is_red(self):
        proc = self.live(STUB_OLD_APIS="1")
        self.assertRedWith(proc, "still served", "re-installing the old operator")

    def test_an_unreachable_cluster_keys_off_loudly(self):
        """One reachability FAIL, every live row keyed off as UNREACHABLE — never
        a silent green and never a cascade of broken-component rows."""
        proc = self.live(STUB_UNREACHABLE="1")
        self.assertNotEqual(proc.returncode, 0, proc.stdout[-3000:])
        self.assertIn("the cluster did not answer", proc.stdout)
        keyed = [l for l in proc.stdout.splitlines() if "UNREACHABLE:" in l]
        self.assertGreaterEqual(len(keyed), 4,
                                "live rows did not all key off:\n" + proc.stdout[-3000:])


if __name__ == "__main__":
    unittest.main()
