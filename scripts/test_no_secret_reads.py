#!/usr/bin/env python3
"""
Nothing this platform ships reads a Secret's contents — run as
`python3 scripts/test_no_secret_reads.py`. No cluster, no credentials, no network.

WHY. The operating rule for everyone who touches this platform is that a Secret's
EXISTENCE may be checked and its CONTENTS may not: any command selecting a Secret's
`.data`, even masked, counts as having printed the value and forces a rotation. The rule
exists because a value that has been rendered once — into a terminal, a log, a CI
artifact, a shell variable another process can read — cannot be un-rendered, and the only
honest response is to replace it.

Our own tooling broke that rule in two places, and both are VENDORED artifacts: every
organisation runs the copy it pinned, so the read happens on every org's workstation,
against that org's live credentials, every time somebody runs the verify suite.

  * The platform stage decodes the Alertmanager configuration Secret in order to assert
    that the Telegram bot token is not inlined in it — reading the credential to prove
    the credential is not readable. Alertmanager already serves its own configuration
    with every secret field redacted, which answers the same question without the read:
    the KEY `bot_token` is visible in that view even though its value is not, so an
    inlined token is still detectable.
  * The edge stage decodes the wildcard TLS Secret to read the certificate's subject
    alternative names. cert-manager writes those names onto the same Secret as an
    ANNOTATION, which is metadata and not data, and the stage already opens a live TLS
    connection to the edge a few lines later.

WHAT THIS PINS. An oracle over everything the platform ships, plus the two replacements.
The oracle is deliberately about the SHAPE of the command rather than about a list of
known offenders: a new row written next year gets the same answer.

CLOSED EXEMPTION, kept as history: `scripts/pitr-drill.sh` used to read the
database name out of the application Secret and copy the bucket credential into the
drill namespace by round-tripping the whole object. It now takes the database name
from the Cluster's own spec and places the bucket key by copying the sealed file,
whose ciphertext is never rendered. No operational file remains on the exemption
list: the contracts below stay because their fixtures ARE the forbidden shapes,
quoted so the repaired oracle can be proven against them.
"""
import pathlib
import re
import unittest

ROOT = pathlib.Path(__file__).resolve().parent.parent

# Files allowed to contain the forbidden shape: the contracts, whose fixtures ARE
# the forbidden shapes, quoted so the repaired oracle can be proven against them
# (see the docstring). Nothing operational may remain here: an operational file
# that needs an exemption is a hole with a note attached, not a finding.
EXEMPT = {
    "scripts/test_no_secret_reads.py",
    "scripts/test_no_secret_reads_r.py",
    "scripts/test_no_secret_reads_r2.py",
}

# Where the platform's own commands live. Documentation is included: a runbook is a
# command somebody will paste.
SCANNED = ("scripts", ".claude/skills", "docs", "template", "Makefile")
SUFFIXES = {".sh", ".py", ".md", ".yaml", ".yml", ""}

# A kubectl invocation, up to the end of the line or the end of the pipeline stage.
KUBECTL = re.compile(r"kubectl\b[^\n|;]*")

# Selecting a Secret at all.
SECRET = re.compile(r"\bget\s+(?:-[a-zA-Z-]+\s+\S+\s+)*(secret|secrets)\b")

# Output forms that render the object's `data` block.
RENDERS_DATA = (
    re.compile(r"-o\s*(?:=\s*)?(?:yaml|json)\b"),
    re.compile(r"--output[= ]\s*(?:yaml|json)\b"),
    re.compile(r"jsonpath[^'\"]*[\"'][^\"']*\.data"),
    re.compile(r"(?:go-template|--template)[^\n|;]*\.[Dd]ata"),
    re.compile(r"custom-columns[=:\s][^\n|;]*\.data"),
)

# A shell assignment whose right-hand side may hold a command: `VAR=...`,
# `export VAR=...`, `local VAR=...`.
ASSIGN = re.compile(r"^\s*(?:export\s+|local\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)$")

# A read of a shell variable, `$VAR` and `${VAR}` both.
VAR_USE = re.compile(r"\$(?:([A-Za-z_][A-Za-z0-9_]*)|\{([A-Za-z_][A-Za-z0-9_]*)\})")


def taints(cmd):
    """Does this kubectl invocation select a Secret's object?

    An `auth can-i ... get secrets` question is about the caller's RBAC, not
    about any object: its answer names nothing, so it taints nothing.
    """
    return bool(SECRET.search(cmd)) and "can-i" not in cmd


def reads_tainted(command, tainted):
    """Does this command read a tainted variable, `$VAR` or `${VAR}`?"""
    for m in VAR_USE.finditer(command):
        if (m.group(1) or m.group(2)) in tainted:
            return True
    return False


def scanned_files():
    for rel in SCANNED:
        path = ROOT / rel
        if path.is_file():
            yield path
            continue
        for found in sorted(path.rglob("*")):
            if found.is_file() and found.suffix in SUFFIXES:
                if str(found.relative_to(ROOT)).startswith(".git"):
                    continue
                yield found


def offences(path: pathlib.Path):
    out = []
    try:
        text = path.read_text()
    except UnicodeDecodeError:
        return out
    # A command is judged whole, however it is laid out: a trailing backslash
    # continues the command on the next line, so continuations are joined first
    # and every question below is asked of the joined command, never of a raw
    # line. Wrapping a command changes neither its meaning nor its verdict.
    text = re.sub(r"\\\n", "", text)
    # Shell variables that currently hold a Secret's name: assigned from a
    # command that selects a Secret (`AMSECRET=$(kubectl get secret -o name
    # ...)` reveals nothing itself), in command order, so a use before the
    # assignment is not tainted by it. A variable assigned from any other
    # query — an Application, a pod — never enters this set.
    tainted = set()
    for n, line in enumerate(text.splitlines(), 1):
        m = ASSIGN.match(line)
        if m and any(taints(cmd) for cmd in KUBECTL.findall(m.group(2))):
            tainted.add(m.group(1))
        for command in KUBECTL.findall(line):
            if not any(p.search(command) for p in RENDERS_DATA):
                continue
            if SECRET.search(command) or reads_tainted(command, tainted):
                out.append((n, command.strip()[:130]))
    return out


class OracleTest(unittest.TestCase):
    """No command this platform ships renders a Secret's data."""

    def test_nothing_shipped_reads_a_secret_value(self):
        bad = []
        for path in scanned_files():
            rel = str(path.relative_to(ROOT))
            if rel in EXEMPT:
                continue
            bad += [f"{rel}:{n}: {cmd}" for n, cmd in offences(path)]
        self.assertEqual(bad, [],
                         "a shipped command renders a Secret's contents; reading it once "
                         "forces a rotation of the value:\n" + "\n".join(bad))

    def test_the_oracle_catches_every_form_that_renders_data(self):
        """The guard is about the shape of the command, so a row written next year in a
        different spelling gets the same answer. If this ever goes red the oracle has a
        hole, not the tree."""
        for spelling in (
            "kubectl -n ns get secret x -o yaml",
            "kubectl get secret x -o json",
            "kubectl get secrets -n ns --output=yaml",
            "kubectl -n ns get secret x -o jsonpath='{.data.token}'",
            'kubectl get secret x -o jsonpath="{.data.tls\\.crt}"',
            "kubectl get secret x -o go-template='{{.data.password}}'",
        ):
            self.assertTrue(
                SECRET.search(spelling) and any(p.search(spelling) for p in RENDERS_DATA),
                f"the oracle does not recognise {spelling!r} as a read")

    def test_the_oracle_permits_checking_that_a_secret_exists(self):
        for spelling in (
            "kubectl get secret x -o name",
            "kubectl -n ns get secret x >/dev/null 2>&1",
            "kubectl -n ns get secret -o name | grep -m1 generated",
            "kubectl describe secret x",
            "kubectl auth can-i get secrets -n ns",
            "kubectl get secret x -o jsonpath='{.metadata.annotations}'",
        ):
            self.assertFalse(
                any(p.search(spelling) for p in RENDERS_DATA),
                f"the oracle refuses {spelling!r}, which reveals nothing")

    def test_the_exemption_list_is_finite_and_real(self):
        """An exemption for a file that no longer exists is a hole nobody notices."""
        missing = [rel for rel in EXEMPT if not (ROOT / rel).exists()]
        self.assertEqual(missing, [], "an exemption names a file that is gone: "
                                      + ", ".join(missing))


class AlertPathTest(unittest.TestCase):
    """The platform stage answers the same question without the read."""

    STAGE = ROOT / "scripts" / "verify" / "stage5-platform.sh"

    def text(self):
        return self.STAGE.read_text()

    def test_the_stage_does_not_decode_the_alertmanager_config_secret(self):
        self.assertEqual(offences(self.STAGE), [],
                         "the stage still decodes a Secret:\n"
                         + "\n".join(f"{n}: {c}" for n, c in offences(self.STAGE)))

    def test_an_inlined_token_is_still_detected(self):
        """The replacement must keep the property, not drop the row. Alertmanager's own
        status view redacts secret VALUES and keeps the KEYS, so a config that inlines
        the token still shows `bot_token` where a correct one shows `bot_token_file`."""
        text = self.text()
        self.assertIn("bot_token_file", text,
                      "the row that requires the token to be read from a file is gone")
        self.assertRegex(
            text, r"bot_token['\"]?\s*[:=]|'bot_token'|\"bot_token\"",
            "nothing asserts the ABSENCE of an inlined bot_token; requiring "
            "bot_token_file alone passes a config that carries both")

    def test_the_receiver_and_watchdog_rows_survive(self):
        text = self.text()
        for needle in ("telegram_configs", "Watchdog"):
            self.assertIn(needle, text,
                          f"the {needle} row was deleted with the Secret read rather "
                          "than moved to the source that does not need it")


class CertificateNamesTest(unittest.TestCase):
    """The edge stage reads the issued names from metadata or from the live handshake."""

    STAGE = ROOT / "scripts" / "verify" / "stage2.sh"

    def test_the_stage_does_not_decode_the_tls_secret(self):
        self.assertEqual(offences(self.STAGE), [],
                         "the stage still decodes the certificate Secret:\n"
                         + "\n".join(f"{n}: {c}" for n, c in offences(self.STAGE)))

    def test_the_subject_alternative_names_are_still_checked(self):
        text = self.STAGE.read_text()
        self.assertRegex(
            text, r"alt-names|subjectAltName|subject_alt",
            "the row that proves the wildcard certificate covers every domain is gone; "
            "without it a certificate missing one name ships unnoticed")
        self.assertIn("SAN", text, "the named SAN rows were dropped")


if __name__ == "__main__":
    unittest.main(verbosity=2)
