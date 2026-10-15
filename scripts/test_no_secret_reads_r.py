#!/usr/bin/env python3
"""
The secret-read oracle, round 1 repairs — run as
`python3 scripts/test_no_secret_reads_r.py`. No cluster, no credentials, no network.

WHY. The node that stopped two verify stages reading Secrets passed its own oracle and was
then reviewed. The oracle's central claim — that it is "about the SHAPE of the command
rather than a list of known offenders" — turned out to be false, and a third stage walked
straight through it.

  * A THIRD STAGE STILL DECODES A SECRET, and the oracle is blind to it because the Secret
    is named through a shell variable. One line resolves the generated Alertmanager config
    Secret with `get secret -o name`, which is permitted and reveals nothing; the next runs
    `kubectl get "$AMSECRET" -o json`, reads its `data`, base64-decodes it, gunzips it and
    writes the whole running Alertmanager configuration to the operator's disk. The word
    "secret" never appears in the offending command, so a regular expression looking for it
    sees nothing. An oracle that can be stepped around by naming a variable is a list of
    known offenders wearing a rule's clothes.

  * THE DELETED ROW WAS NOT REPLACED BY WHAT REPLACED IT. Removing the Secret read also
    removed a row that grepped the operator's copy of the bot token against the whole
    decoded configuration — so it caught the credential WHEREVER it appeared. The
    structural row offered in its place inspects one field of one receiver type. A
    credential reaching the same chat through a webhook receiver's URL was caught before
    and is not caught now, and that row was live on this workstation rather than
    hypothetical.

  * THE CERTIFICATE NAMES NOW COME FROM A LABEL BESIDE THE CERTIFICATE rather than from
    the certificate. The annotation is metadata on a Secret any operator can edit, and the
    row stays green against a certificate replaced without touching it — which is the
    substitution the row exists to catch. The stage opens a real TLS handshake to the edge
    eleven lines further down; the names it serves are the names that matter, and reading
    them there needs no Secret at all.
"""
import pathlib
import re
import subprocess
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
_BYTECODE, sys.dont_write_bytecode = sys.dont_write_bytecode, True
from test_no_secret_reads import (RENDERS_DATA, ROOT, offences,  # noqa: E402
                                  scanned_files)
sys.dont_write_bytecode = _BYTECODE

CHECKER = ROOT / "scripts" / "verify" / "am-inline-credentials.py"
STAGE2 = ROOT / "scripts" / "verify" / "stage2.sh"
STAGE3 = ROOT / "scripts" / "verify" / "stage3.sh"
STAGE5 = ROOT / "scripts" / "verify" / "stage5-platform.sh"

# A read whose target is a shell variable that was assigned from a Secret query. This is
# the shape the oracle missed; the repaired oracle must recognise it wherever it appears.
TAINTED = """\
AMSECRET=$(kubectl -n monitoring get secret -o name | grep -m1 generated)
kubectl -n monitoring get "$AMSECRET" -o json | python3 -c 'import json,sys; json.load(sys.stdin)["data"]'
"""

PERMITTED = """\
AMSECRET=$(kubectl -n monitoring get secret -o name | grep -m1 generated)
[ -n "$AMSECRET" ] && echo "the generated config Secret exists"
"""


def oracle_offences(text: str, tmp_path: pathlib.Path) -> list:
    """Run the repaired oracle over a scratch script."""
    script = tmp_path / "scratch.sh"
    script.write_text(text)
    return offences(script)


class TaintTest(unittest.TestCase):
    """The oracle follows a Secret named through a variable."""

    def setUp(self):
        import tempfile
        self.tmp = pathlib.Path(tempfile.mkdtemp(prefix="orbit-j28r-"))
        self.addCleanup(__import__("shutil").rmtree, self.tmp, ignore_errors=True)

    def test_a_read_through_a_variable_is_an_offence(self):
        found = oracle_offences(TAINTED, self.tmp)
        self.assertTrue(found,
                        "the oracle does not see a Secret read whose target is a shell "
                        "variable, so any stage can step around it by naming one")

    def test_resolving_a_secret_by_name_alone_is_still_permitted(self):
        self.assertEqual(oracle_offences(PERMITTED, self.tmp), [],
                         "checking that a Secret EXISTS was made an offence; the rule "
                         "permits existence and forbids contents")

    def test_the_braced_spelling_is_caught_too(self):
        found = oracle_offences(TAINTED.replace('"$AMSECRET"', '"${AMSECRET}"'), self.tmp)
        self.assertTrue(found, "${VAR} walks past the oracle while $VAR does not")

    def test_a_variable_that_never_held_a_secret_is_not_an_offence(self):
        clean = ('APP=$(kubectl -n argocd get app -o name | head -1)\n'
                 'kubectl -n argocd get "$APP" -o json > /dev/null\n')
        self.assertEqual(oracle_offences(clean, self.tmp), [],
                         "rendering an Application was called a Secret read; the oracle "
                         "must taint from the Secret query, not from any query")


class WholeTreeTest(unittest.TestCase):
    """Nothing shipped survives the repaired oracle."""

    def test_nothing_shipped_reads_a_secret_value(self):
        from test_no_secret_reads import EXEMPT
        bad = []
        for path in scanned_files():
            rel = str(path.relative_to(ROOT))
            if rel in EXEMPT:
                continue
            bad += [f"{rel}:{n}: {cmd}" for n, cmd in offences(path)]
        self.assertEqual(bad, [],
                         "a shipped command renders a Secret's contents:\n" + "\n".join(bad))

    def test_the_running_alertmanager_config_is_not_decoded_from_its_secret(self):
        text = STAGE3.read_text()
        self.assertNotRegex(
            text, r"gzip\.decompress|b64decode",
            "the stage still decodes the generated Alertmanager config Secret and writes "
            "the running configuration to the operator's disk")

    def test_the_live_routing_rows_survive(self):
        """Removing the read must not remove the check. The live rows are the ones that
        answer where an alert would go TONIGHT, as opposed to what git says."""
        text = STAGE3.read_text()
        self.assertIn("alertmanager(live)", text,
                      "the live routing rows were deleted along with the Secret read; "
                      "git being right says nothing about what Alertmanager loaded")


class InlineCredentialTest(unittest.TestCase):
    """The property the deleted row proved, proved for every receiver rather than one.

    A checker with its own fixtures, because the thing that went wrong last time was a
    check narrow enough to look right: a credential reaching the same chat through a
    webhook URL passed a row that only ever read `telegram_configs[].bot_token`.
    """

    def run_checker(self, config: str):
        # Guarded: without this every "must be red" test below passes vacuously on the
        # non-zero exit of a missing file, which is a contract that grades itself.
        self.assertTrue(CHECKER.is_file(),
                        f"{CHECKER.relative_to(ROOT)} does not exist")
        import tempfile
        tmp = pathlib.Path(tempfile.mkdtemp(prefix="orbit-j28r-cfg-"))
        self.addCleanup(__import__("shutil").rmtree, tmp, ignore_errors=True)
        path = tmp / "config.yaml"
        path.write_text(config)
        return subprocess.run([sys.executable, str(CHECKER), str(path)],
                              capture_output=True, text=True)

    def test_the_checker_exists(self):
        self.assertTrue(CHECKER.is_file(),
                        f"{CHECKER.relative_to(ROOT)} does not exist; the property has "
                        "nowhere to live that a test can reach")

    def test_a_config_reading_every_credential_from_a_file_is_green(self):
        proc = self.run_checker("""
receivers:
  - name: telegram
    telegram_configs:
      - bot_token_file: /etc/alertmanager/secrets/telegram/bot-token
        chat_id: 111
  - name: intake
    webhook_configs:
      - url: http://orbit-intake.orbit:8080/alertmanager
        http_config:
          authorization:
            credentials_file: /etc/alertmanager/secrets/intake/token
""")
        self.assertEqual(proc.returncode, 0,
                         (proc.stdout + proc.stderr)[-1200:])

    def test_an_inlined_bot_token_is_red(self):
        proc = self.run_checker("""
receivers:
  - name: telegram
    telegram_configs:
      - bot_token: <secret>
        chat_id: 111
""")
        self.assertNotEqual(proc.returncode, 0,
                            "an inlined bot token passed:\n" + proc.stdout[-800:])
        self.assertIn("bot_token", proc.stdout + proc.stderr,
                      "the failure does not name the field it found")

    def test_a_credential_in_a_webhook_url_is_red(self):
        """THE case the narrow row missed: the same chat, reached a different way."""
        proc = self.run_checker("""
receivers:
  - name: telegram-by-hand
    webhook_configs:
      - url: https://api.telegram.org/bot123456:AAquiteLongLookingSecretValue/sendMessage
""")
        self.assertNotEqual(proc.returncode, 0,
                            "a credential embedded in a webhook URL passed; this is "
                            "exactly what the deleted workstation row used to catch:\n"
                            + proc.stdout[-800:])

    def test_an_inlined_credential_under_any_receiver_type_is_red(self):
        for block, field in (("email_configs", "auth_password"),
                             ("webhook_configs", "http_config"),
                             ("opsgenie_configs", "api_key")):
            proc = self.run_checker(f"""
receivers:
  - name: r
    {block}:
      - {field}: <secret>
""")
            self.assertNotEqual(proc.returncode, 0,
                                f"{block}.{field} passed; the check is narrow again:\n"
                                + proc.stdout[-600:])

    def test_it_never_prints_what_it_found(self):
        """It runs in CI, where stdout is a log kept for ever. Naming the FIELD is the
        report; printing the value would make the check the leak."""
        value = "AAquiteLongLookingSecretValue"
        proc = self.run_checker(f"""
receivers:
  - name: r
    telegram_configs:
      - bot_token: {value}
""")
        self.assertNotIn(value, proc.stdout + proc.stderr,
                         "the checker printed the credential it was complaining about")

    def test_the_stage_runs_it(self):
        self.assertIn("am-inline-credentials", STAGE5.read_text(),
                      "the checker exists and no stage runs it")


class CertificateNamesTest(unittest.TestCase):
    """The names that matter are the ones the edge actually serves."""

    def test_the_names_come_from_the_served_certificate(self):
        text = STAGE2.read_text()
        hits = [f"{n}: {line.strip()[:110]}"
                for n, line in enumerate(text.splitlines(), 1) if "alt-names" in line]
        self.assertEqual(hits, [],
                         "the row trusts an annotation beside the certificate; that "
                         "annotation is metadata any operator can edit, and the row "
                         "stays green against a certificate swapped without it:\n"
                         + "\n".join(hits))
        self.assertRegex(
            text, r"s_client[^\n]*\n?[^\n]*(subjectAltName|-ext)",
            "nothing reads the subject alternative names from the handshake the stage "
            "already opens to the edge")

    def test_each_missing_name_is_still_named(self):
        text = STAGE2.read_text()
        self.assertRegex(text, r'SAN \$?\{?d\}? (present|MISSING)|SAN .*MISSING',
                         "the per-domain SAN rows were collapsed into one opaque row, so "
                         "a certificate missing one name no longer says which")

    def test_the_stage_still_reads_no_secret(self):
        self.assertEqual(offences(STAGE2), [],
                         "the stage reads a Secret again:\n"
                         + "\n".join(f"{n}: {c}" for n, c in offences(STAGE2)))


if __name__ == "__main__":
    unittest.main(verbosity=2)
