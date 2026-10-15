"""The shared display rule (scripts/verify/shown.py): one predicate decides whether a
name read from outside is repeated in a diagnostic row, and every stage, helper and the
instance initializer use it instead of a private copy."""
import os
import pathlib
import re
import subprocess
import sys
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts" / "verify"))
import shown as display  # noqa: E402

TOKEN = "ghp_" + "Ab3" * 8            # 24 letters and digits after the prefix
HEX = "0123456789abcdef0123456789abcdef"


class RuleTest(unittest.TestCase):
    def test_tokenish_is_a_16_run_holding_a_letter_and_a_digit(self):
        for s in (TOKEN, HEX, "svc-" + HEX + ".ns", "a" * 15 + "1"):
            self.assertTrue(display.tokenish(s), s)
        for s in ("orbit-agents", "a" * 40, "1" * 40, "abc123", "a1" * 7 + "-" + "b2" * 7, None, 7, ""):
            self.assertFalse(display.tokenish(s), repr(s))

    def test_fits_needs_a_string_within_the_limit_matching_in_full_and_not_a_token(self):
        self.assertTrue(display.fits("orbitd", "object-name"))
        self.assertTrue(display.fits("a." * 100 + "a", "object-name"))
        for v in (None, 3, b"orbitd", "Orbitd", "orbit d", "-x", "a." * 130 + "a", HEX, "x" + HEX):
            self.assertFalse(display.fits(v, "object-name"), repr(v))
        self.assertTrue(display.fits("dev", "namespace")); self.assertFalse(display.fits("dev.x", "namespace"))
        self.assertTrue(display.fits(".env", "file-name")); self.assertFalse(display.fits("a/b", "file-name"))
        self.assertTrue(display.fits("INTAKE_TOKEN", "key")); self.assertFalse(display.fits("k v", "key"))
        self.assertTrue(display.fits("KubeDown", "alert-name")); self.assertFalse(display.fits("Kube Down", "alert-name"))
        self.assertTrue(display.fits("a.example", "hostname")); self.assertFalse(display.fits("-a.example", "hostname"))
        self.assertTrue(display.fits("ghcr.io/fixture-org", "owner")); self.assertFalse(display.fits("ghcr.io/" + HEX, "owner"))
        self.assertTrue(display.fits("inFlightReq", "word")); self.assertFalse(display.fits("in-flight", "word"))
        with self.assertRaises(KeyError):
            display.fits("x", "no-such-shape")

    def test_shown_repeats_only_what_fits_and_describes_the_rest(self):
        self.assertEqual(display.shown("orbitd", "object-name"), "orbitd")
        self.assertEqual(display.shown(HEX, "object-name"), "(a name not of an object-name shape, or of a credential's shape)")
        self.assertEqual(display.shown(None, "label", "environment key"), "(an environment key not of a label shape, or of a credential's shape)")
        self.assertEqual(display.described("alert-name", "rule name"), "(a rule name not of an alert-name shape, or of a credential's shape)")

    def test_shown_parts_keeps_the_fitting_components_and_the_root(self):
        self.assertEqual(display.shown_parts("clusters/platform/" + HEX + ".yaml"),
                         "clusters/platform/(a name not of a file-name shape, or of a credential's shape)")
        self.assertEqual(display.shown_parts("/a/b"), "/a/b")
        self.assertEqual(display.shown_parts("svc." + HEX + ".svc", "host-label", "label", "."),
                         "svc.(a label not of a host-label shape, or of a credential's shape).svc")
        self.assertNotIn(HEX, display.shown_parts(pathlib.PurePath("x") / HEX))

    def test_shown_list_counts_what_it_does_not_repeat(self):
        good, note = display.shown_list(["KubeDown", HEX, "Kube Down", None], "alert-name", "rule name")
        self.assertEqual(good, ["KubeDown"])
        self.assertEqual(note, "(+3 rule name(s) not of an alert-name shape, or of a credential's shape)")
        self.assertEqual(display.shown_list(["a"], "name"), (["a"], ""))


class WiringTest(unittest.TestCase):
    """No private copy of the rule survives, and every consumer can reach the module."""
    CONSUMERS = ("scripts/verify/stage3.sh", "scripts/verify/stage4.sh", "scripts/verify/stage5-platform.sh",
                 "scripts/verify/am-routes.py", "scripts/verify/am-inline-credentials.py",
                 "scripts/verify/no-instance-strings.py", "scripts/instance-init.py", "scripts/no-credentials.py")

    def test_no_consumer_carries_its_own_token_test_or_name_fallback(self):
        for rel in self.CONSUMERS:
            text = (ROOT / rel).read_text()
            self.assertNotIn("def tokenish", text, rel)
            self.assertNotIn("len(r) >= 16", text, rel)
            self.assertIn("import shown as display", text, rel)
            for old in ("(not an object name)", "(a name not of a file-name shape)\"", "not of an owner's shape",
                        "(a label of a credential's shape)", "(a value not of a label shape)\""):
                self.assertNotIn(old, text, (rel, old))

    def test_every_stage_exports_the_module_directory_before_its_first_python_block(self):
        for name in ("stage2.sh", "stage3.sh", "stage4.sh", "stage5-platform.sh"):
            text = (ROOT / "scripts" / "verify" / name).read_text()
            i = text.index("export PYTHONPATH=")
            self.assertLess(i, text.index("python3"), name)
            self.assertIn("${PYTHONPATH:+:$PYTHONPATH}", text[i:i + 120], name)

    def test_the_module_is_vendored_beside_the_stages(self):
        manifest = (ROOT / "core-manifest.txt").read_text().splitlines()
        self.assertIn("scripts/verify/shown.py", manifest)

    def test_a_stage_block_reaches_the_module_from_an_instance_root(self):
        """The stage4 recovery listing, run as the stage runs it (from an instance root that
        vendored scripts/verify), with the module reachable only through the stage's own
        PYTHONPATH export — not the suite's."""
        text = (ROOT / "scripts" / "verify" / "stage4.sh").read_text()
        block = text[text.index('REC_ROW=$(python3 - "$RECF" <<\'PY\''):text.index("PY\n)", text.index('REC_ROW=$(python3 - "$RECF"')) + 4]
        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td); (root / "scripts" / "verify").mkdir(parents=True)
            (root / "scripts" / "verify" / "shown.py").write_bytes((ROOT / "scripts" / "verify" / "shown.py").read_bytes())
            recf = root / "rec"; recf.write_bytes(b"recovery/plain.txt\0recovery/" + HEX.encode() + b".age\0")
            env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
            script = 'export PYTHONPATH="$PWD/scripts/verify${PYTHONPATH:+:$PYTHONPATH}"\nRECF=%s\n%s\necho "$REC_ROW"' % (recf, block)
            proc = subprocess.run(["bash", "-c", script], cwd=root, env=env, capture_output=True, text=True, timeout=60)
        self.assertEqual(proc.stdout.strip(), "2 recovery/plain.txt (+1 name(s) not of a path shape, or of a credential's shape)", proc.stdout + proc.stderr)
        self.assertNotIn(HEX, proc.stdout + proc.stderr)

    def test_a_door_row_describes_a_token_that_passes_the_name_grammar(self):
        """The stage3 intake-door rows: a host and a backend name of a credential's shape
        pass the old grammar; the shared rule describes them (a gap no round had covered)."""
        text = (ROOT / "scripts" / "verify" / "stage3.sh").read_text()
        start = text.index("        for p in ((rule.get(\"http\") or {}).get(\"paths\") or []):")
        end = text.index("if not items and wired:")
        helpers = text[text.index("NAME = re.compile(r\"[a-z0-9]([a-z0-9.-]{0,251}[a-z0-9])?\")\nHOST = "):text.index("for ing in items:\n    name = shaped(")]
        pre = ("import re\nimport shown as display\n" + helpers + "bad = []\nINTAKE_SVC = 'orbit-agents-intake'\nname = 'door'\n"
               "host = shaped('%s.example', HOST, 'host')\n"
               "rule = {'http': {'paths': [{'path': '/x', 'pathType': 'Prefix', 'backend': {'service': {'name': '%s'}}}]}}\n"
               "if True:\n    if True:\n" % (HEX, HEX))
        proc = subprocess.run([sys.executable, "-c", pre + text[start:end] + "\nprint('\\n'.join(bad))"], capture_output=True, text=True, timeout=60)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("door publishes a path rule on (a host not of a host shape, or of a credential's shape) -> Service (a name not of a name shape, or of a credential's shape)", proc.stdout)
        self.assertNotIn(HEX, proc.stdout + proc.stderr)

    def test_the_credential_scanner_describes_a_token_shaped_key(self):
        """A key named like a token (letters and digits, 20 long) passes the scanner's key
        grammar; the shared rule describes it instead of repeating it."""
        key = "password_" + "k0" * 10
        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td); (root / "scripts" / "verify").mkdir(parents=True); (root / "clusters" / "apps").mkdir(parents=True)
            for rel in ("scripts/no-credentials.py", "scripts/verify/shown.py"):
                (root / rel).write_bytes((ROOT / rel).read_bytes())
            (root / "clusters" / "apps" / "bait.yaml").write_text("kind: ConfigMap\ndata: {%s: x}\n" % key)
            env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
            proc = subprocess.run([sys.executable, "scripts/no-credentials.py"], cwd=root, env=env, capture_output=True, text=True, timeout=60)
        self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)
        self.assertIn("bait.yaml.data.#0(a key not of a key shape, or of a credential's shape): a literal value under a credential-shaped name", proc.stdout)
        self.assertNotIn("k0k0", proc.stdout + proc.stderr)


if __name__ == "__main__":
    unittest.main()
