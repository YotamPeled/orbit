#!/usr/bin/env python3
"""
Placing a sealed file in a second namespace, against REAL sops.

WHY THIS FILE REPLACES THE FIRST CONTRACT. The first contract pinned the wrong property.
It required that sops never be invoked, on the premise that the sealing tool signs only the
encrypted blocks, so rewriting `metadata.namespace` by hand would leave the file openable.
That premise is false. sops computes its message authentication code over EVERY value in the
document, encrypted or not, unless the file was sealed with `mac_only_encrypted` — and this
platform's sealing path does not pass that flag. So the one line the verb exists to rewrite
is exactly the line that breaks the signature, and the copy does not open: sops refuses it
with a MAC mismatch.

The first contract could not see this because it drove a double: its sops stand-in wrote a
constant signature and never read one back, so the decrypt assertion passed for any document
carrying a `sops:` key. Seven hundred green tests over a file real sops refuses.

So this contract drives the real tool. It generates a throwaway key in a temporary directory,
seals a fixture through the platform's own sealing path, and then requires the real binary to
open what the verb produced. Nothing here is a stand-in, and the test that would have caught
the old implementation is written down as a guard: a hand-edited namespace MUST fail to open.
If that guard ever passes, this whole file has gone vacuous again and says so.

WHAT THE VERB MUST NOW DO. Rewrite the namespace THROUGH sops (`sops set`), which recomputes
the signature over the edited document. Measured on sops 3.13.3: the file still opens, and the
ciphertext of every encrypted value is byte-identical to the source — so the value is still
never written out, never logged, never in argv. The change from the old design is only that
the tool is in the loop, holding the key it already holds for every other sealing operation.

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

# A value that is recognisable in any file it leaks into, and is not a real credential.
PLAINTEXT = "canary-value-must-never-reach-disk"


def copy_secret(*args, **kwargs):
    """Resolved at call time: a missing verb must FAIL this contract, not skip it."""
    fn = getattr(secrets_mod, "copy_secret", None)
    assert fn is not None, "orbitd.secrets has no copy_secret; the verb is the subject here"
    return fn(*args, **kwargs)


class RealSopsCase(unittest.TestCase):
    """Every test here runs the real binary. A machine without it fails this contract rather
    than skipping it: a contract that skips itself green is the thing that let the first one
    through."""

    def setUp(self):
        self.assertIsNotNone(SOPS, "sops is not installed; this contract cannot be faked")
        self.assertIsNotNone(AGE_KEYGEN, "age-keygen is not installed")
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        keyfile = self.tmp / "key.txt"
        subprocess.run([AGE_KEYGEN, "-o", str(keyfile)], capture_output=True, text=True, check=True)
        self.recipient = self._public_key(keyfile.read_text())
        # The verb runs sops as a child of THIS process, and the module finds the key
        # the way the desk does: SOPS_AGE_KEY_FILE in the environment. Setting it only
        # for the fixture's own sops calls would leave the verb unable to open anything,
        # which is a property of the fixture and not of the verb.
        previous = os.environ.get("SOPS_AGE_KEY_FILE")
        os.environ["SOPS_AGE_KEY_FILE"] = str(keyfile)
        self.addCleanup(self._restore_key, previous)
        self.env = dict(os.environ)
        self.instance = self.tmp / "instance"
        (self.instance / "clusters" / "platform" / "secrets").mkdir(parents=True)

    @staticmethod
    def _restore_key(previous):
        if previous is None:
            os.environ.pop("SOPS_AGE_KEY_FILE", None)
        else:
            os.environ["SOPS_AGE_KEY_FILE"] = previous

    @staticmethod
    def _public_key(text: str) -> str:
        for line in text.splitlines():
            m = re.search(r"(age1[a-z0-9]+)", line)
            if m:
                return m.group(1)
        raise AssertionError("age-keygen produced no public key")

    def sops(self, *args, stdin: str = None):
        return subprocess.run([SOPS, "--config", os.devnull, *args],
                              input=stdin, capture_output=True, text=True, env=self.env)

    def seal(self, namespace: str, name: str = "orbit-s3") -> Path:
        """A sealed file produced the way the platform produces them: the same flags the
        module's own sealing path builds."""
        doc = yaml.safe_dump({
            "apiVersion": secrets_mod.SOPS_API,
            "kind": secrets_mod.SOPS_KIND,
            "metadata": {"name": name, "namespace": namespace},
            "spec": {"secretTemplates": [{"name": name, "data": {"TOKEN": PLAINTEXT}}]},
        }, sort_keys=False)
        proc = self.sops("encrypt", "--age", self.recipient,
                         "--encrypted-regex", secrets_mod.ENCRYPTED_REGEX,
                         "--input-type", "yaml", "--output-type", "yaml",
                         "/dev/stdin", stdin=doc)
        self.assertEqual(proc.returncode, 0, proc.stderr[:400])
        path = self.instance / "clusters" / "platform" / "secrets" / namespace / f"{name}.yaml"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(proc.stdout)
        return path

    def opens(self, path: Path) -> bool:
        return self.sops("decrypt", str(path)).returncode == 0

    def sealed_values(self, path: Path) -> dict:
        doc = yaml.safe_load(path.read_text())
        return doc["spec"]["secretTemplates"][0]["data"]

    def target_of(self, namespace: str, name: str = "orbit-s3") -> Path:
        return self.instance / "clusters" / "platform" / "secrets" / namespace / f"{name}.yaml"


class GuardTest(RealSopsCase):
    """If these stop holding, the fixtures have drifted and every other test here is noise."""

    def test_a_freshly_sealed_file_opens(self):
        self.assertTrue(self.opens(self.seal("widgets-prod")), "the fixture itself does not open")

    def test_a_hand_edited_namespace_does_not_open(self):
        """The defect the first contract could not see, written down so it cannot return.
        This is what the old implementation produced."""
        src = self.seal("widgets-prod")
        edited = self.tmp / "edited.yaml"
        edited.write_text(src.read_text().replace("namespace: widgets-prod", "namespace: drill"))
        self.assertFalse(self.opens(edited),
                         "rewriting the namespace by hand produced a file that still opens — "
                         "either sops changed its signing rules or this fixture is not sealed "
                         "the way the platform seals; either way every test below is vacuous")


class CopyTest(RealSopsCase):
    def test_the_copy_opens(self):
        src = self.seal("widgets-prod")
        rc = copy_secret(self.instance, source=src, namespace="drill")
        self.assertEqual(rc, 0)
        out = self.target_of("drill")
        self.assertTrue(out.is_file(), "the verb reported success and wrote no file")
        self.assertTrue(self.opens(out),
                        "the copy does not open: this is the whole point of the verb")

    def test_the_copy_carries_the_new_namespace(self):
        src = self.seal("widgets-prod")
        self.assertEqual(copy_secret(self.instance, source=src, namespace="drill"), 0)
        doc = yaml.safe_load(self.target_of("drill").read_text())
        self.assertEqual(doc["metadata"]["namespace"], "drill")
        self.assertEqual(doc["metadata"]["name"], "orbit-s3", "the name must not move")

    def test_the_encrypted_values_are_byte_identical(self):
        """Re-signing is allowed. Re-encrypting is not: a changed ciphertext means the value
        was opened and sealed again, which is a different key-usage story and a different
        audit trail."""
        src = self.seal("widgets-prod")
        before = self.sealed_values(src)
        self.assertEqual(copy_secret(self.instance, source=src, namespace="drill"), 0)
        self.assertEqual(self.sealed_values(self.target_of("drill")), before,
                         "the copy re-encrypted the value instead of carrying it")

    def test_the_source_is_untouched(self):
        src = self.seal("widgets-prod")
        before = src.read_text()
        self.assertEqual(copy_secret(self.instance, source=src, namespace="drill"), 0)
        self.assertEqual(src.read_text(), before, "the verb edited the file it was reading")
        self.assertTrue(self.opens(src), "the source no longer opens after being copied")

    def test_no_plaintext_reaches_any_file(self):
        """The value may pass through the tool's memory. It may not land anywhere."""
        src = self.seal("widgets-prod")
        self.assertEqual(copy_secret(self.instance, source=src, namespace="drill"), 0)
        offenders = [str(p.relative_to(self.tmp)) for p in self.tmp.rglob("*")
                     if p.is_file() and PLAINTEXT in p.read_text(errors="ignore")]
        self.assertEqual(offenders, [], "the value was written in clear to: %s"
                                        % ", ".join(offenders))

    def test_nothing_is_left_behind(self):
        """A temporary file holding an opened document is the same leak with a shorter life."""
        src = self.seal("widgets-prod")
        before = {p for p in self.tmp.rglob("*") if p.is_file()}
        self.assertEqual(copy_secret(self.instance, source=src, namespace="drill"), 0)
        after = {p for p in self.tmp.rglob("*") if p.is_file()}
        new = sorted(str(p.relative_to(self.tmp)) for p in after - before)
        self.assertEqual(new, ["instance/clusters/platform/secrets/drill/orbit-s3.yaml"],
                         "the verb left files behind: %s" % ", ".join(new))


class RefusalTest(RealSopsCase):
    def test_an_unsealed_source_is_refused(self):
        """r30 finding 3: the refusal checked the kind and never the seal, so a SopsSecret
        carrying cleartext copied happily and its plaintext was written to the new path and
        announced as copied."""
        path = self.instance / "clusters" / "platform" / "secrets" / "widgets-prod" / "bare.yaml"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(yaml.safe_dump({
            "apiVersion": secrets_mod.SOPS_API,
            "kind": secrets_mod.SOPS_KIND,
            "metadata": {"name": "bare", "namespace": "widgets-prod"},
            "spec": {"secretTemplates": [{"name": "bare", "data": {"TOKEN": PLAINTEXT}}]},
        }, sort_keys=False))
        rc = copy_secret(self.instance, source=path, namespace="drill")
        self.assertNotEqual(rc, 0, "an unsealed file was accepted as a sealed one")
        self.assertFalse(self.target_of("drill", "bare").exists(),
                         "the refusal still wrote the cleartext to the new path")

    def test_a_file_sops_cannot_open_is_refused(self):
        """A corrupted seal must be a refusal, not a copy that nobody can open later."""
        src = self.seal("widgets-prod")
        broken = src.read_text().replace("mac: ENC[", "mac: ENC[x")
        src.write_text(broken)
        rc = copy_secret(self.instance, source=src, namespace="drill")
        self.assertNotEqual(rc, 0, "a file that does not open was copied anyway")

    def test_the_refusals_name_no_value(self):
        path = self.instance / "clusters" / "platform" / "secrets" / "widgets-prod" / "bare.yaml"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(yaml.safe_dump({
            "apiVersion": secrets_mod.SOPS_API, "kind": secrets_mod.SOPS_KIND,
            "metadata": {"name": "bare", "namespace": "widgets-prod"},
            "spec": {"secretTemplates": [{"name": "bare", "data": {"TOKEN": PLAINTEXT}}]},
        }, sort_keys=False))
        import contextlib
        import io
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            copy_secret(self.instance, source=path, namespace="drill")
        both = out.getvalue() + err.getvalue()
        self.assertNotIn(PLAINTEXT, both, "a refusal printed the value it refused")
        self.assertNotIn("Traceback", both, "a refusal must be a message, not a crash")


class DrillTest(unittest.TestCase):
    ROOT = Path(__file__).resolve().parents[3]

    def test_the_drill_removes_the_copy_the_verb_actually_wrote(self):
        """r30 finding 5: the verb names the target from the document's own name, and the
        drill hard-coded the filename. When they disagree the real copy stays in the instance
        repo and the cleanup deletes nothing."""
        drill = (self.ROOT / "scripts" / "pitr-drill.sh").read_text()
        self.assertIn("secrets copy", drill, "the drill no longer uses the verb")
        placed = [l.strip() for l in drill.splitlines() if "SEALED_COPY" in l]
        self.assertTrue(placed, "the drill tracks no copy to clean up")
        # An empty initializer is fine. An assignment that spells the path out is not:
        # the path must come from what the verb reported.
        hardcoded = [l for l in placed
                     if re.match(r'SEALED_COPY="[^"]+"', l)
                     and "$(" not in l]
        self.assertEqual(hardcoded, [], "the drill guesses the copy's path instead of taking "
                                        "it from the verb: %s" % "; ".join(hardcoded))


if __name__ == "__main__":
    unittest.main()
