#!/usr/bin/env python3
"""
The copy verb, round 3: crash-safe, honest about the key, and reachable from the command line.

Round 2's review confirmed the redesign. Rewriting the namespace through the sealing tool
does produce a copy that opens; the ciphertext of every value is byte-identical; the only
field that changes in the seal is the signature; and no value reaches argv, the environment,
a log line, an error message or any file. What it found instead were three defects around
the edges of that, and one hole my own contract opened.

CRASH SAFETY. The verb writes the source document to the FINAL target path and only then
asks the tool to rewrite the namespace in place. Between those two steps the file on disk is
not half-written — it is a complete, correctly signed, openable document sitting in the
target namespace's directory while still naming the SOURCE namespace. An interrupt there
leaves the tree in a state the verb itself then refuses forever, because it refuses an
existing target: the recovery drill wedges permanently, and an operator retrying by hand
applies over the live Secret in the source namespace. The fix is the ordinary one: build the
finished file somewhere else and move it into place in one step.

HONESTY ABOUT THE KEY. The verb needs the operator's key, which it did not before. Its two
sibling verbs check for it and say so. This one folds a missing key, a wrong key and an
unreadable key into one message that says the SOURCE file cannot be opened — so on the drill
it claims the production bucket credential is corrupt when the operator simply has no key
loaded, and it hides exactly the condition that makes a contract like this one look green
while proving nothing.

THE HOLE I OPENED. Round 2 deleted the first contract, correctly, because it pinned a false
premise. Five of its tests pinned properties that are still true and are now unpinned — most
importantly that the verb exists on the command line at all, which is the only way the
recovery drill reaches it. Those are restored here, against the command line rather than
against the Python function, so the drill's actual interface is what is covered.

Run under pytest from services/orbitd.
"""
import os
import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

import yaml

from orbitd import secrets as secrets_mod

SOPS = shutil.which("sops")
AGE_KEYGEN = shutil.which("age-keygen")
PLAINTEXT = "canary-value-must-never-reach-disk"


class RealSopsCase(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(SOPS, "sops is not installed; this contract cannot be faked")
        self.assertIsNotNone(AGE_KEYGEN, "age-keygen is not installed")
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.keyfile = self.tmp / "key.txt"
        subprocess.run([AGE_KEYGEN, "-o", str(self.keyfile)],
                       capture_output=True, text=True, check=True)
        self.recipient = self._public_key(self.keyfile.read_text())
        self.set_key(str(self.keyfile))
        self.instance = self.tmp / "instance"
        (self.instance / "clusters" / "platform" / "secrets").mkdir(parents=True)

    def set_key(self, value):
        previous = os.environ.get("SOPS_AGE_KEY_FILE")
        if value is None:
            os.environ.pop("SOPS_AGE_KEY_FILE", None)
        else:
            os.environ["SOPS_AGE_KEY_FILE"] = value
        self.addCleanup(self._restore, previous)

    @staticmethod
    def _restore(previous):
        if previous is None:
            os.environ.pop("SOPS_AGE_KEY_FILE", None)
        else:
            os.environ["SOPS_AGE_KEY_FILE"] = previous

    @staticmethod
    def _public_key(text):
        m = re.search(r"(age1[a-z0-9]+)", text)
        assert m, "age-keygen produced no public key"
        return m.group(1)

    def sops(self, *args):
        return subprocess.run([SOPS, "--config", os.devnull, *args],
                              capture_output=True, text=True)

    def seal(self, namespace, name="orbit-s3"):
        doc = yaml.safe_dump({
            "apiVersion": secrets_mod.SOPS_API,
            "kind": secrets_mod.SOPS_KIND,
            "metadata": {"name": name, "namespace": namespace},
            "spec": {"secretTemplates": [{"name": name, "data": {"TOKEN": PLAINTEXT}}]},
        }, sort_keys=False)
        proc = subprocess.run(
            [SOPS, "--config", os.devnull, "encrypt", "--age", self.recipient,
             "--encrypted-regex", secrets_mod.ENCRYPTED_REGEX,
             "--input-type", "yaml", "--output-type", "yaml", "/dev/stdin"],
            input=doc, capture_output=True, text=True)
        self.assertEqual(proc.returncode, 0, proc.stderr[:400])
        path = self.instance / "clusters" / "platform" / "secrets" / namespace / f"{name}.yaml"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(proc.stdout)
        return path

    def target_of(self, namespace, name="orbit-s3"):
        return self.instance / "clusters" / "platform" / "secrets" / namespace / f"{name}.yaml"

    def cli(self, *args):
        """The drill's actual interface, invoked the way the drill invokes it: from the
        instance repository's root, with the instance directory given as `.` and the
        sealed file named relative to it."""
        here = os.getcwd()
        os.chdir(self.instance)
        try:
            return secrets_mod.main(["copy", ".", *args])
        finally:
            os.chdir(here)


class CrashSafetyTest(RealSopsCase):
    def test_the_target_never_exists_naming_the_source_namespace(self):
        """The interrupt window, driven directly: make the re-signing step fail after the
        verb has decided where the file goes, and require that nothing openable is left
        behind naming the wrong namespace."""
        src = self.seal("widgets-prod")
        real_run = secrets_mod.run_sops
        seen = {}

        def die_after_write(argv, **kwargs):
            seen["target_before"] = self.target_of("drill").exists()
            raise KeyboardInterrupt("interrupted while re-signing")

        secrets_mod.run_sops = die_after_write
        self.addCleanup(setattr, secrets_mod, "run_sops", real_run)
        try:
            secrets_mod.copy_secret(self.instance, source=src, namespace="drill")
        except KeyboardInterrupt:
            pass
        self.assertFalse(
            self.target_of("drill").exists(),
            "an interrupt during the re-signing left a file at the target path; if it "
            "opens it names the wrong namespace, and either way the verb refuses that "
            "path from then on and the drill is wedged")

    def test_no_leftovers_anywhere_after_an_interrupted_copy(self):
        src = self.seal("widgets-prod")
        before = {p for p in self.tmp.rglob("*") if p.is_file()}
        real_run = secrets_mod.run_sops
        secrets_mod.run_sops = lambda *a, **k: (_ for _ in ()).throw(KeyboardInterrupt())
        self.addCleanup(setattr, secrets_mod, "run_sops", real_run)
        try:
            secrets_mod.copy_secret(self.instance, source=src, namespace="drill")
        except KeyboardInterrupt:
            pass
        new = sorted(str(p.relative_to(self.tmp)) for p in
                     {p for p in self.tmp.rglob("*") if p.is_file()} - before)
        self.assertEqual(new, [], "an interrupted copy left files behind: %s" % ", ".join(new))

    def test_a_second_copy_into_a_taken_target_refuses_without_touching_it(self):
        src = self.seal("widgets-prod")
        self.assertEqual(secrets_mod.copy_secret(self.instance, source=src,
                                                 namespace="drill"), 0)
        target = self.target_of("drill")
        before = target.read_text()
        rc = secrets_mod.copy_secret(self.instance, source=src, namespace="drill")
        self.assertNotEqual(rc, 0, "an existing target was overwritten")
        self.assertEqual(target.read_text(), before, "the refused copy changed the target")


class KeyTest(RealSopsCase):
    def missing_key_output(self, setter):
        src = self.seal("widgets-prod")
        setter()
        import contextlib
        import io
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = secrets_mod.copy_secret(self.instance, source=src, namespace="drill")
        return rc, (out.getvalue() + err.getvalue())

    def test_no_key_is_reported_as_a_missing_key_not_a_bad_source(self):
        rc, text = self.missing_key_output(lambda: self.set_key(None))
        self.assertNotEqual(rc, 0)
        self.assertNotIn("cannot open", text.lower(),
                         "a missing key was reported as if the sealed file were corrupt, "
                         "which on the drill accuses the production credential: " + text[:300])
        self.assertIn("SOPS_AGE_KEY_FILE", text,
                      "the refusal does not name what the operator must set: " + text[:300])

    def test_a_key_path_that_is_not_a_file_is_reported_the_same_way(self):
        rc, text = self.missing_key_output(
            lambda: self.set_key(str(self.tmp / "not-here.txt")))
        self.assertNotEqual(rc, 0)
        self.assertIn("SOPS_AGE_KEY_FILE", text, text[:300])

    def test_a_wrong_key_is_not_reported_as_a_missing_one(self):
        """A key that exists and does not open this file is a different problem, and the
        message must not send the operator to set a variable that is already set."""
        other = self.tmp / "other.txt"
        subprocess.run([AGE_KEYGEN, "-o", str(other)], capture_output=True, check=True)
        rc, text = self.missing_key_output(lambda: self.set_key(str(other)))
        self.assertNotEqual(rc, 0)
        self.assertNotIn("is not set", text,
                         "a wrong key was reported as an unset one: " + text[:300])

    def test_no_refusal_quotes_the_value(self):
        for setter in (lambda: self.set_key(None),
                       lambda: self.set_key(str(self.tmp / "not-here.txt"))):
            with self.subTest():
                _, text = self.missing_key_output(setter)
                self.assertNotIn(PLAINTEXT, text)
                self.assertNotIn("Traceback", text)


class CommandLineTest(RealSopsCase):
    """The drill reaches the verb only through the command line. Round 2's diff deleted the
    only tests that touched it."""

    def test_the_verb_exists_on_the_command_line_and_copies(self):
        src = self.seal("widgets-prod")
        rc = self.cli("--file", str(src.relative_to(self.instance)), "--namespace", "drill")
        self.assertEqual(rc, 0, "the copy subcommand is not reachable or did not copy")
        self.assertTrue(self.target_of("drill").is_file())

    def test_the_command_line_names_the_file_it_wrote(self):
        """The drill parses this to know what to clean up."""
        import contextlib
        import io
        src = self.seal("widgets-prod")
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = self.cli("--file", str(src.relative_to(self.instance)),
                          "--namespace", "drill")
        self.assertEqual(rc, 0)
        self.assertIn("drill/orbit-s3.yaml", out.getvalue(),
                      "the verb does not report the path it wrote, so the caller can only "
                      "guess it: " + out.getvalue()[:300])

    def test_a_namespace_that_is_not_a_namespace_name_is_refused(self):
        """The only thing keeping a quote out of the argument handed to the sealing tool
        and a traversal segment out of the target path."""
        src = self.seal("widgets-prod")
        for bad in ('dr"ill', "../escape", "UPPER", "-leading", "x" * 64, ""):
            with self.subTest(namespace=bad):
                rc = self.cli("--file", str(src.relative_to(self.instance)),
                              "--namespace", bad)
                self.assertNotEqual(rc, 0, f"{bad!r} was accepted as a namespace")

    def test_copying_into_its_own_namespace_is_refused(self):
        src = self.seal("widgets-prod")
        rc = self.cli("--file", str(src.relative_to(self.instance)),
                      "--namespace", "widgets-prod")
        self.assertNotEqual(rc, 0, "a copy onto itself was accepted")

    def test_a_missing_source_is_refused(self):
        rc = self.cli("--file", "clusters/platform/secrets/widgets-prod/absent.yaml",
                      "--namespace", "drill")
        self.assertNotEqual(rc, 0, "a source that does not exist was accepted")


if __name__ == "__main__":
    unittest.main()
