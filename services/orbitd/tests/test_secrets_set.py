#!/usr/bin/env python3
"""
Rotating ONE key of a sealed file, against REAL sops.

WHY THE VERB IS NEEDED. The sealed plane is live and every credential the platform holds
is a sealed file. Nothing can change one of them. `mint --rotate` rebuilds a file from
scratch around a single key, so pointing it at a two-key file destroys the key it was not
asked about; the sign-in credential for the dashboards is exactly that shape, and it is
the credential the phase's acceptance rotates. `copy` re-signs a file into a second
namespace and changes no value at all. So there is no path from "this credential should
change" to a sealed file that holds the new value, and the phase cannot be accepted.

WHAT THE VERB MUST DO. Open the existing sealed file with the key the desk already holds,
replace one key's value with a freshly generated one, and re-seal the whole document. Every
other key keeps the value it had, the Secret keeps its type, and the file still opens.

WHERE THE VALUE MAY GO. Memory and sops's standard input. That is the whole list. Not a
command line, not an environment variable, not standard output, not an error message, not
any file under the instance tree except as ciphertext. The tests below hold the value to
that list by planting a canary and hunting for it — in the tree, in the output, and in
every argument list the module hands to a child process.

HOW IT MUST DO IT, AND WHY THAT CHANGED. Three rounds took the document apart and put it
back, and each one lost something it had not thought to carry: first the other keys, then
the fields around them, then the sealing envelope itself — the recipients the file is
sealed for and the pattern saying which fields are sealed. The loss kept moving because
the design was wrong, not the patch. So the rule is now that the verb does not reassemble
the document at all. The sealing tool can set one value inside a sealed file in place, and
its `set` form reads the value from standard input, which was the only objection to using
it. Everything the file carries and this code has never heard of survives because nothing
here ever rebuilds it. The checks in `EnvelopeTest` below are written against that
outcome, not that mechanism: any implementation that leaves the envelope, the sealed-field
pattern and the untouched text exactly as it found them passes.

This contract drives the real binary. It generates a throwaway key, seals fixtures through
the platform's own sealing path, and requires the real tool to open what the verb produced.
A machine without the tool FAILS this contract rather than skipping it: an earlier contract
in this family drove a stand-in and went seven hundred tests green over a file the real
binary refuses.

Run under pytest from services/orbitd.
"""
import base64
import unittest.mock
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

# Planted where a value would be, recognisable in any file or stream it leaks into, and
# not a real credential.
CANARY = "canary-value-must-never-reach-disk"
KEPT = "second-key-must-survive-the-rotation"


def set_secret(*args, **kwargs):
    """Resolved at call time: a missing verb must FAIL this contract, not skip it."""
    fn = getattr(secrets_mod, "set_secret", None)
    assert fn is not None, "orbitd.secrets has no set_secret; the verb is the subject here"
    return fn(*args, **kwargs)


class RealSopsCase(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(SOPS, "sops is not installed; this contract cannot be faked")
        self.assertIsNotNone(AGE_KEYGEN, "age-keygen is not installed")
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        keyfile = self.tmp / "key.txt"
        subprocess.run([AGE_KEYGEN, "-o", str(keyfile)], capture_output=True, text=True, check=True)
        self.recipient = self._public_key(keyfile.read_text())
        # The verb runs sops as a child of THIS process and finds the key the way the desk
        # does. Setting it only for the fixture's own calls would leave the verb unable to
        # open anything, which is a property of the fixture and not of the verb.
        previous = os.environ.get("SOPS_AGE_KEY_FILE")
        os.environ["SOPS_AGE_KEY_FILE"] = str(keyfile)
        self.addCleanup(self._restore_key, previous)
        self.env = dict(os.environ)
        self.instance = self.tmp / "instance"
        (self.instance / "clusters" / "platform" / "secrets").mkdir(parents=True)
        secrets_mod.write_sops_yaml(self.instance / ".sops.yaml", self.recipient)

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

    def seal(self, namespace: str = "widgets-prod", name: str = "dashboard-signin",
             data: dict | None = None, secret_type: str | None = None) -> Path:
        """A sealed file produced the way the platform produces them. The default shape is
        the one the acceptance rotates: two keys, only one of which changes."""
        values = data if data is not None else {"admin-password": CANARY, "admin-user": KEPT}
        template = {"name": name, "data": {k: base64.b64encode(v.encode()).decode()
                                           for k, v in values.items()}}
        if secret_type is not None:
            template["type"] = secret_type
        doc = yaml.safe_dump({
            "apiVersion": secrets_mod.SOPS_API,
            "kind": secrets_mod.SOPS_KIND,
            "metadata": {"name": name, "namespace": namespace},
            "spec": {"secretTemplates": [template]},
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

    def opened(self, path: Path) -> dict:
        """The file's keys, decoded. The one place this contract holds plaintext, and it
        holds it in memory exactly as the verb is allowed to."""
        proc = self.sops("decrypt", "--output-type", "yaml", str(path))
        self.assertEqual(proc.returncode, 0, proc.stderr[:400])
        data = yaml.safe_load(proc.stdout)["spec"]["secretTemplates"][0]["data"]
        return {k: base64.b64decode(v).decode() for k, v in data.items()}

    def template_of(self, path: Path) -> dict:
        proc = self.sops("decrypt", "--output-type", "yaml", str(path))
        self.assertEqual(proc.returncode, 0, proc.stderr[:400])
        return yaml.safe_load(proc.stdout)["spec"]["secretTemplates"][0]

    def leaks_under(self, root: Path, needle: str) -> list[str]:
        found = []
        for path in sorted(root.rglob("*")):
            if not path.is_file():
                continue
            try:
                if needle in path.read_text():
                    found.append(str(path.relative_to(root)))
            except (UnicodeDecodeError, OSError):
                continue
        return found


class GuardTest(RealSopsCase):
    """If these stop holding, the fixtures have drifted and every other test here is noise."""

    def test_a_freshly_sealed_file_opens_and_holds_both_keys(self):
        self.assertEqual(self.opened(self.seal()),
                         {"admin-password": CANARY, "admin-user": KEPT})

    def test_the_planted_value_is_not_on_disk_in_clear_before_the_verb_runs(self):
        self.seal()
        self.assertEqual(self.leaks_under(self.instance, CANARY), [],
                         "the fixture wrote the value in clear; the leak hunt below would "
                         "pass or fail for reasons that have nothing to do with the verb")

    def test_a_hand_edited_value_does_not_open(self):
        """The sealing tool signs the whole document. A verb that edits a value without
        re-sealing produces a file nothing can open, and this is what that looks like."""
        src = self.seal()
        edited = self.tmp / "edited.yaml"
        text = src.read_text()
        doc = yaml.safe_load(text)
        old = doc["spec"]["secretTemplates"][0]["data"]["admin-password"]
        edited.write_text(text.replace(old, old[:-4] + "AAAA"))
        self.assertFalse(self.opens(edited),
                         "a hand-edited value still opens — either the signing rules "
                         "changed or this fixture is not sealed the way the platform "
                         "seals; either way every test below is vacuous")


class RotationTest(RealSopsCase):
    def test_the_rotated_file_still_opens(self):
        path = self.seal()
        set_secret(self.instance, namespace="widgets-prod", name="dashboard-signin",
                   key="admin-password")
        self.assertTrue(self.opens(path), "the verb produced a file the tool refuses")

    def test_the_named_key_holds_a_different_value_afterwards(self):
        path = self.seal()
        set_secret(self.instance, namespace="widgets-prod", name="dashboard-signin",
                   key="admin-password")
        self.assertNotEqual(self.opened(path)["admin-password"], CANARY)

    def test_every_other_key_keeps_the_value_it_had(self):
        """The defect that makes this verb necessary: the minting path rebuilds the file
        around one key and loses the rest."""
        path = self.seal()
        set_secret(self.instance, namespace="widgets-prod", name="dashboard-signin",
                   key="admin-password")
        self.assertEqual(self.opened(path)["admin-user"], KEPT)

    def test_the_secret_type_is_kept(self):
        path = self.seal(data={"token": CANARY}, secret_type="kubernetes.io/dockerconfigjson")
        set_secret(self.instance, namespace="widgets-prod", name="dashboard-signin", key="token")
        self.assertEqual(self.template_of(path).get("type"), "kubernetes.io/dockerconfigjson")

    def test_the_new_value_is_not_the_one_next_door(self):
        """A rotation that copies another key of the same file would pass every test above."""
        path = self.seal()
        set_secret(self.instance, namespace="widgets-prod", name="dashboard-signin",
                   key="admin-password")
        after = self.opened(path)
        self.assertNotEqual(after["admin-password"], after["admin-user"])

    def test_two_rotations_produce_two_different_values(self):
        path = self.seal()
        set_secret(self.instance, namespace="widgets-prod", name="dashboard-signin",
                   key="admin-password")
        first = self.opened(path)["admin-password"]
        set_secret(self.instance, namespace="widgets-prod", name="dashboard-signin",
                   key="admin-password")
        self.assertNotEqual(self.opened(path)["admin-password"], first)

    def test_the_value_can_be_supplied_for_a_test_and_still_lands_sealed(self):
        """The minting path takes a factory so a test can pin the value without the verb
        ever accepting one from a command line. This verb takes the same seam."""
        path = self.seal()
        set_secret(self.instance, namespace="widgets-prod", name="dashboard-signin",
                   key="admin-password", value_factory=lambda: "a-pinned-value")
        self.assertEqual(self.opened(path)["admin-password"], "a-pinned-value")


class LeakTest(RealSopsCase):
    def test_the_replaced_value_never_reaches_the_tree_in_clear(self):
        self.seal()
        set_secret(self.instance, namespace="widgets-prod", name="dashboard-signin",
                   key="admin-password", value_factory=lambda: CANARY + "-new")
        self.assertEqual(self.leaks_under(self.tmp, CANARY + "-new"), [],
                         "the new value was written somewhere in clear")

    def test_the_surviving_value_never_reaches_the_tree_in_clear(self):
        """The verb has to open the file to keep the other keys, so the moment it is
        careless with a temporary file it spills a credential it was not even asked
        about."""
        self.seal()
        set_secret(self.instance, namespace="widgets-prod", name="dashboard-signin",
                   key="admin-password")
        self.assertEqual(self.leaks_under(self.tmp, KEPT), [])

    def test_no_value_is_passed_on_a_command_line(self):
        """The board's rule, held mechanically. The sealing tool's own `set` form takes the
        value as an argument, so this test is what stops the obvious implementation."""
        seen = []
        original = secrets_mod.run_sops

        def recording(argv, *args, **kwargs):
            seen.append(list(argv))
            return original(argv, *args, **kwargs)

        self.seal()
        with unittest.mock.patch.object(secrets_mod, "run_sops", recording):
            set_secret(self.instance, namespace="widgets-prod", name="dashboard-signin",
                       key="admin-password", value_factory=lambda: CANARY + "-new",
                       sops_run=recording)
        self.assertTrue(seen, "the verb never invoked the sealing tool; it cannot have re-sealed")
        for argv in seen:
            for arg in argv:
                self.assertNotIn(CANARY, str(arg), f"a value appeared in an argument list: {argv}")

    def test_nothing_it_prints_carries_a_value(self):
        self.seal()
        import contextlib
        import io
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            set_secret(self.instance, namespace="widgets-prod", name="dashboard-signin",
                       key="admin-password", value_factory=lambda: CANARY + "-new")
        printed = out.getvalue() + err.getvalue()
        self.assertNotIn(CANARY, printed)
        self.assertNotIn(KEPT, printed)

    def test_what_it_prints_names_the_secret_and_the_key(self):
        """The operator has to be able to tell what was rotated from the output alone,
        since the value is the one thing that may not be in it."""
        self.seal()
        import contextlib
        import io
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            set_secret(self.instance, namespace="widgets-prod", name="dashboard-signin",
                       key="admin-password")
        printed = out.getvalue()
        self.assertIn("widgets-prod", printed)
        self.assertIn("dashboard-signin", printed)
        self.assertIn("admin-password", printed)


class RefusalTest(RealSopsCase):
    def test_a_file_that_is_not_there_is_refused(self):
        rc = set_secret(self.instance, namespace="widgets-prod", name="dashboard-signin",
                        key="admin-password")
        self.assertNotEqual(rc, 0)

    def test_a_key_the_file_does_not_hold_is_refused(self):
        """Rotation, not creation. A typo in the key name must not quietly add a key
        nothing reads while the real credential goes unrotated."""
        path = self.seal()
        before = path.read_text()
        rc = set_secret(self.instance, namespace="widgets-prod", name="dashboard-signin",
                        key="admin-pasword")
        self.assertNotEqual(rc, 0)
        self.assertEqual(path.read_text(), before, "the file changed on a refused rotation")

    def test_a_refusal_names_the_key_it_refused(self):
        import contextlib
        import io
        self.seal()
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            set_secret(self.instance, namespace="widgets-prod", name="dashboard-signin",
                       key="admin-pasword")
        self.assertIn("admin-pasword", err.getvalue())

    def test_a_file_the_key_cannot_open_is_refused_and_left_alone(self):
        path = self.seal()
        os.environ["SOPS_AGE_KEY_FILE"] = str(self.tmp / "not-a-key.txt")
        before = path.read_text()
        rc = set_secret(self.instance, namespace="widgets-prod", name="dashboard-signin",
                        key="admin-password")
        self.assertNotEqual(rc, 0)
        self.assertEqual(path.read_text(), before)

    def test_the_original_survives_a_crash_mid_rotation(self):
        """Killed while it holds the new value, the original must be untouched.

        This check has been wrong twice before by patching a call the code makes BEFORE it
        holds anything, so it asserts it reached the window first. The window moves when the
        design does: round five never calls the sealing helper, it runs the tool against a
        copy and then moves it, so the window is the move."""
        path = self.seal()
        before = path.read_text()
        reached = []
        real_replace = os.replace

        def dies(src, dst):
            reached.append((src, dst))
            raise KeyboardInterrupt

        with unittest.mock.patch.object(os, "replace", dies):
            with self.assertRaises(KeyboardInterrupt):
                set_secret(self.instance, namespace="widgets-prod", name="dashboard-signin",
                           key="admin-password", value_factory=lambda: CANARY + "-new")
        self.assertTrue(reached, "the rotation never reached the point where it replaces the "
                                 "file; this check is standing over a window it never opened")
        self.assertEqual(path.read_text(), before, "the original was changed by a crash")
        self.assertEqual(self.opened(path)["admin-password"], CANARY)
        self.assertEqual(real_replace.__name__, "replace")
        self.assertEqual([], self.leaks_under(self.instance, CANARY + "-new"))

    def test_a_crash_leaves_no_half_written_file_beside_the_original(self):
        """The interrupt has to land INSIDE the window this is about — after the temporary
        file exists and before it is moved into place. An earlier version of this test
        interrupted a call that runs before the temporary file is created, so it stood over
        a window it never opened and would have passed however the cleanup was written."""
        path = self.seal()
        real_replace = os.replace
        opened = []

        def dies(src, dst, *args, **kwargs):
            opened.append(Path(src).name)
            raise KeyboardInterrupt

        with unittest.mock.patch.object(os, "replace", dies):
            with self.assertRaises(KeyboardInterrupt):
                set_secret(self.instance, namespace="widgets-prod", name="dashboard-signin",
                           key="admin-password")
        self.assertTrue(opened, "the interrupt never reached the window this test is about")
        leftovers = [p.name for p in path.parent.iterdir() if p.name != path.name]
        self.assertEqual(leftovers, [], f"the rotation left {leftovers} behind")
        self.assertEqual(self.opened(path)["admin-password"], CANARY)


class PreservationTest(RealSopsCase):
    """The rotation re-seals the document it opened, with one value replaced. It does not
    rebuild one.

    The first design reconstructed a document from the namespace, the name, the type and
    the keys, so every other field was dropped. A guard was added to refuse shapes the
    reconstruction could not reproduce; a reviewer then found five more fields the guard
    accepted and the rebuild still dropped, two of which invert rather than vanish — a
    suspended resource becomes active, an ownership opt-out becomes the default opt-in.
    That is the same defect twice, so the shape changes rather than the guard growing: what
    goes back in is the document that came out, edited in memory. Then nothing can be
    dropped and no guard has to enumerate the operator's fields."""

    EXTRA_TEMPLATE = {"labels": {"app.kubernetes.io/part-of": "widgets"},
                      "annotations": {"orbit.local/owner": "widgets-team"}}
    EXTRA_SPEC = {"suspend": True}
    EXTRA_META = {"annotations": {"orbit.local/adopt": "false"}}

    def seal_rich(self, namespace: str = "widgets-prod", name: str = "dashboard-signin") -> Path:
        """A sealed file carrying every field the reviewer found the rebuild dropping."""
        template = {"name": name, "type": "Opaque",
                    "data": {"admin-password": base64.b64encode(CANARY.encode()).decode(),
                             "admin-user": base64.b64encode(KEPT.encode()).decode()},
                    **self.EXTRA_TEMPLATE}
        doc = yaml.safe_dump({
            "apiVersion": secrets_mod.SOPS_API,
            "kind": secrets_mod.SOPS_KIND,
            "metadata": {"name": name, "namespace": namespace, **self.EXTRA_META},
            "spec": {"secretTemplates": [template], **self.EXTRA_SPEC},
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

    def whole(self, path: Path) -> dict:
        proc = self.sops("decrypt", "--output-type", "yaml", str(path))
        self.assertEqual(proc.returncode, 0, proc.stderr[:400])
        doc = yaml.safe_load(proc.stdout)
        doc.pop("sops", None)
        return doc

    def test_the_fixture_carries_fields_a_rebuild_would_drop(self):
        """Anti-vacuity: if the fixture held nothing beyond what a reconstruction produces,
        every test below would pass on the old design too."""
        doc = self.whole(self.seal_rich())
        self.assertEqual(doc["spec"].get("suspend"), True)
        self.assertIn("orbit.local/adopt", doc["metadata"].get("annotations", {}))
        self.assertIn("app.kubernetes.io/part-of",
                      doc["spec"]["secretTemplates"][0].get("labels", {}))

    def test_nothing_but_the_rotated_value_changes(self):
        """THE check. Everything the file said before it still says afterwards, except the
        one value that was rotated."""
        path = self.seal_rich()
        before = self.whole(path)
        set_secret(self.instance, namespace="widgets-prod", name="dashboard-signin",
                   key="admin-password")
        after = self.whole(path)
        rotated = after["spec"]["secretTemplates"][0]["data"].pop("admin-password")
        expected = before["spec"]["secretTemplates"][0]["data"].pop("admin-password")
        self.assertNotEqual(rotated, expected, "the value was not rotated at all")
        self.assertEqual(after, before,
                         "the rotation changed something other than the value it was asked "
                         "to change")

    def test_a_flag_that_inverts_when_dropped_survives(self):
        """The two fields the reviewer singled out, because losing them does not read as a
        loss: a suspended resource becomes active and an ownership opt-out becomes the
        default opt-in. A rotation that silently un-suspends a resource is worse than one
        that fails."""
        path = self.seal_rich()
        set_secret(self.instance, namespace="widgets-prod", name="dashboard-signin",
                   key="admin-password")
        doc = self.whole(path)
        self.assertEqual(doc["spec"].get("suspend"), True)
        self.assertEqual(doc["metadata"]["annotations"].get("orbit.local/adopt"), "false")

    def test_a_key_the_file_does_not_hold_is_still_refused(self):
        """What the shape guard shrinks to: the only question left is whether there is a
        key here to replace."""
        path = self.seal_rich()
        before = path.read_text()
        self.assertNotEqual(
            set_secret(self.instance, namespace="widgets-prod", name="dashboard-signin",
                       key="admin-pasword"), 0)
        self.assertEqual(path.read_text(), before)


# ReadBackTest lived here. It required the rotation to REFUSE a file whose instance
# declares a recipient the desk cannot open, because round three re-sealed from that
# declared recipient and would otherwise have written a file nobody could open over the
# only copy of a fresh credential. Round five never uses the declared recipient -- the
# sealing tool edits the file in place and keeps the file's own recipients -- so the hazard
# is gone and the refusal became a scar that costs an operator a live credential. The
# property that mattered, that what the verb wrote can be reopened, is held by
# RotationTest.test_the_rotated_file_still_opens and by DriftedRecipientTest below, which
# requires the same scenario to SUCCEED and the result to open.


class LeftoverTest(RealSopsCase):
    """What a hard kill leaves in a directory something else applies."""

    def test_the_half_written_file_is_not_named_like_a_manifest(self):
        """A kill that runs no cleanup leaves the temporary file behind. The directory it
        sits in is applied whole, so a leftover that ends in the manifest suffix is a
        second, complete, validly sealed resource declaring the same namespace and name as
        the real one. The module's own crash-safe writer avoids this with a suffix the
        applier's glob ignores; this verb took the other verb's suffix without the reason
        that verb had for it.

        The look has to happen while the temporary file exists, which is the moment before
        it is moved into place — an earlier draft of this test looked during the sealing
        calls, which all finish before the temporary file is created, and so passed without
        ever seeing one."""
        seen = []
        real_replace = os.replace

        def watch(src, dst, *args, **kwargs):
            seen.extend(p.name for p in Path(dst).parent.iterdir())
            return real_replace(src, dst, *args, **kwargs)

        path = self.seal()
        with unittest.mock.patch.object(os, "replace", watch):
            set_secret(self.instance, namespace="widgets-prod", name="dashboard-signin",
                       key="admin-password")
        leftovers = [n for n in seen if n != path.name]
        self.assertTrue(leftovers, "no temporary file existed at the moment of the move; "
                                   "this test is standing over a window it never opened")
        self.assertTrue(all(not n.endswith(".yaml") for n in leftovers),
                        f"a temporary file is named like a manifest: {sorted(set(leftovers))}")

    def test_the_rotated_file_keeps_the_permissions_its_siblings_have(self):
        """The temporary file's mode rides along through the move, so a rotated file comes
        out readable only by its owner where every other sealed file in the tree is not.
        Nothing breaks; the tree simply stops being uniform, and the next person to notice
        has to work out which files were rotated and why they differ."""
        path = self.seal()
        sibling = path.parent / "other.yaml"
        sibling.write_text("placeholder\n")
        os.chmod(sibling, 0o644)
        os.chmod(path, 0o644)
        set_secret(self.instance, namespace="widgets-prod", name="dashboard-signin",
                   key="admin-password")
        self.assertEqual(path.stat().st_mode & 0o777, sibling.stat().st_mode & 0o777)


class CommandLineTest(RealSopsCase):
    """The interface the acceptance actually drives."""

    def run_cli(self, *args) -> int:
        here = os.getcwd()
        os.chdir(self.instance)
        try:
            return secrets_mod.main(["set", ".", *args])
        finally:
            os.chdir(here)

    def test_the_verb_is_reachable_from_the_command_line(self):
        path = self.seal()
        rc = self.run_cli("--namespace", "widgets-prod", "--name", "dashboard-signin",
                          "--key", "admin-password")
        self.assertEqual(rc, 0)
        self.assertNotEqual(self.opened(path)["admin-password"], CANARY)
        self.assertEqual(self.opened(path)["admin-user"], KEPT)

    def test_the_command_line_offers_no_way_to_hand_it_a_value(self):
        """If a value can be typed, it is in a shell history and in the process table, and
        the board's rule is broken by the interface rather than by the implementation."""
        parser = secrets_mod.build_parser()
        for action in parser._actions:
            if getattr(action, "choices", None) and "set" in action.choices:
                flags = {o for a in action.choices["set"]._actions for o in a.option_strings}
                self.assertNotIn("--value", flags)
                self.assertNotIn("--from-literal", flags)
                self.assertNotIn("--password", flags)
                return
        self.fail("the parser advertises no `set` subcommand")


if __name__ == "__main__":
    unittest.main(verbosity=2)


# ----------------------------------------------------------------- round four
# A fresh review broke round three by running it. Both blockers were losses of things the
# file carried and the code never read: the recipients it is sealed for, and the pattern
# saying which of its fields are sealed. Two more defects were real and are not fixed by
# preserving the envelope, so they are held here too.

WIDER_REGEX = "^(data|stringData|recoveryPhrase)$"
EXTRA_SEALED = "second-sealed-field-must-stay-sealed"


class EnvelopeTest(RealSopsCase):
    """Everything the file carries that this code has never heard of."""

    def second_key(self):
        """A second recipient, standing for the break-glass key a real file is sealed for
        alongside the desk's."""
        keyfile = self.tmp / "break-glass.txt"
        subprocess.run([AGE_KEYGEN, "-o", str(keyfile)], capture_output=True, text=True, check=True)
        return keyfile, self._public_key(keyfile.read_text())

    def seal_for(self, recipients: list[str], *, regex: str = None, extra: dict = None,
                 namespace: str = "widgets-prod", name: str = "dashboard-signin") -> Path:
        values = {"admin-password": CANARY, "admin-user": KEPT}
        template = {"name": name, "data": {k: base64.b64encode(v.encode()).decode()
                                           for k, v in values.items()}}
        doc = {"apiVersion": secrets_mod.SOPS_API, "kind": secrets_mod.SOPS_KIND,
               "metadata": {"name": name, "namespace": namespace},
               "spec": {"secretTemplates": [template]}}
        if extra:
            doc.update(extra)
        proc = self.sops("encrypt", "--age", ",".join(recipients),
                         "--encrypted-regex", regex or secrets_mod.ENCRYPTED_REGEX,
                         "--input-type", "yaml", "--output-type", "yaml",
                         "/dev/stdin", stdin=yaml.safe_dump(doc, sort_keys=False))
        self.assertEqual(proc.returncode, 0, proc.stderr[:400])
        path = self.instance / "clusters" / "platform" / "secrets" / namespace / f"{name}.yaml"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(proc.stdout)
        return path

    def recipients_of(self, path: Path) -> list[str]:
        envelope = yaml.safe_load(path.read_text()).get("sops", {})
        return sorted(entry["recipient"] for entry in envelope.get("age", []))

    def opens_with(self, path: Path, keyfile: Path) -> bool:
        env = dict(self.env, SOPS_AGE_KEY_FILE=str(keyfile))
        return subprocess.run([SOPS, "--config", os.devnull, "decrypt", str(path)],
                              capture_output=True, text=True, env=env).returncode == 0

    # -- the file is sealed for more than one key -----------------------------------

    def test_the_fixture_really_does_seal_for_two_keys(self):
        """Anti-vacuity: if the second key never opened it, the check below proves nothing."""
        keyfile, public = self.second_key()
        path = self.seal_for([self.recipient, public])
        self.assertEqual(len(self.recipients_of(path)), 2)
        self.assertTrue(self.opens_with(path, keyfile), "the break-glass key never opened it")

    def test_every_key_the_file_was_sealed_for_still_opens_it(self):
        """Round three read the recipients from the instance configuration instead of from
        the file, so rotating a two-key file quietly revoked the break-glass key — and the
        read-back guard, which only asked whether the desk's own key still worked, passed."""
        keyfile, public = self.second_key()
        path = self.seal_for([self.recipient, public])
        before = self.recipients_of(path)
        set_secret(self.instance, namespace="widgets-prod", name="dashboard-signin",
                   key="admin-password")
        self.assertEqual(self.recipients_of(path), before,
                         "the rotation changed which keys the file is sealed for")
        self.assertTrue(self.opens_with(path, keyfile),
                        "a key that opened the file before the rotation no longer opens it")

    # -- the file says which of its fields are sealed --------------------------------

    def test_the_fixture_really_does_seal_the_extra_field(self):
        """Anti-vacuity: the wider pattern must actually hide the field before the rotation."""
        path = self.seal_for([self.recipient], regex=WIDER_REGEX,
                             extra={"recoveryPhrase": EXTRA_SEALED})
        self.assertNotIn(EXTRA_SEALED, path.read_text(),
                         "the fixture never sealed the extra field")

    def test_a_field_the_file_sealed_is_still_sealed_afterwards(self):
        """Round three hard-coded which fields it seals, so a document sealing anything
        wider had the extra field written back in clear, permanently."""
        path = self.seal_for([self.recipient], regex=WIDER_REGEX,
                             extra={"recoveryPhrase": EXTRA_SEALED})
        set_secret(self.instance, namespace="widgets-prod", name="dashboard-signin",
                   key="admin-password")
        self.assertNotIn(EXTRA_SEALED, path.read_text(),
                         "a field the file had sealed came back in cleartext on disk")

    def test_the_pattern_saying_which_fields_are_sealed_is_kept(self):
        path = self.seal_for([self.recipient], regex=WIDER_REGEX,
                             extra={"recoveryPhrase": EXTRA_SEALED})
        set_secret(self.instance, namespace="widgets-prod", name="dashboard-signin",
                   key="admin-password")
        envelope = yaml.safe_load(path.read_text()).get("sops", {})
        self.assertEqual(envelope.get("encrypted_regex"), WIDER_REGEX)


class UntouchedTextTest(RealSopsCase):
    """A rotation reprints the document only where the value changed."""

    def seal_text(self, text: str, namespace="widgets-prod", name="dashboard-signin") -> Path:
        proc = self.sops("encrypt", "--age", self.recipient,
                         "--encrypted-regex", secrets_mod.ENCRYPTED_REGEX,
                         "--input-type", "yaml", "--output-type", "yaml",
                         "/dev/stdin", stdin=text)
        self.assertEqual(proc.returncode, 0, proc.stderr[:400])
        path = self.instance / "clusters" / "platform" / "secrets" / namespace / f"{name}.yaml"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(proc.stdout)
        return path

    def document(self):
        data = {k: base64.b64encode(v.encode()).decode()
                for k, v in {"admin-password": CANARY, "admin-user": KEPT}.items()}
        return (
            f"apiVersion: {secrets_mod.SOPS_API}\n"
            f"kind: {secrets_mod.SOPS_KIND}\n"
            "metadata:\n"
            "  name: dashboard-signin\n"
            "  namespace: widgets-prod\n"
            "sealedOn: '2026-09-11T00:00:00Z'\n"
            "spec:\n"
            "  secretTemplates:\n"
            "    - name: dashboard-signin\n"
            "      data:\n"
            + "".join(f"        {k}: {v}\n" for k, v in data.items())
        )

    def test_a_scalar_the_rotation_did_not_touch_is_not_reprinted(self):
        """Round three round-tripped the whole document through a YAML library, which
        rewrote a quoted timestamp into a native one. Nothing read it back, so nothing
        noticed; a consumer that parses that field as a string would have."""
        path = self.seal_text(self.document())
        before = yaml.safe_load(path.read_text())["sealedOn"]
        self.assertIsInstance(before, str, "the fixture's own timestamp is not a string")
        set_secret(self.instance, namespace="widgets-prod", name="dashboard-signin",
                   key="admin-password")
        after = yaml.safe_load(path.read_text())["sealedOn"]
        self.assertEqual(after, before, "a field the rotation did not touch was reprinted")


class EveryTemplateTest(RealSopsCase):
    """A file may render more than one Secret, and the same key may sit in several."""

    def seal_two_templates(self, namespace="widgets-prod", name="alert-router") -> Path:
        def template(secret_name):
            return {"name": secret_name,
                    "data": {"webhook-token": base64.b64encode(CANARY.encode()).decode()}}
        doc = yaml.safe_dump({
            "apiVersion": secrets_mod.SOPS_API, "kind": secrets_mod.SOPS_KIND,
            "metadata": {"name": name, "namespace": namespace},
            "spec": {"secretTemplates": [template("alert-router"), template("alert-router-mirror")]},
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

    def values(self, path: Path) -> list[str]:
        proc = self.sops("decrypt", "--output-type", "yaml", str(path))
        self.assertEqual(proc.returncode, 0, proc.stderr[:400])
        return [base64.b64decode(t["data"]["webhook-token"]).decode()
                for t in yaml.safe_load(proc.stdout)["spec"]["secretTemplates"]]

    def test_the_fixture_really_does_carry_the_key_twice(self):
        self.assertEqual(self.values(self.seal_two_templates()), [CANARY, CANARY])

    def test_a_key_that_sits_in_two_templates_is_rotated_in_both_or_not_at_all(self):
        """Round three rotated the first and reported success, leaving the second Secret
        serving the old credential — the worst outcome, because the operator believes the
        old value is dead."""
        path = self.seal_two_templates()
        code = set_secret(self.instance, namespace="widgets-prod", name="alert-router",
                          key="webhook-token")
        after = self.values(path)
        if code == 0:
            self.assertNotIn(CANARY, after,
                             "the verb reported success with the old value still in the file")
            self.assertEqual(len(set(after)), 1,
                             "the two Secrets rendered from one file now hold different values")
        else:
            self.assertEqual(after, [CANARY, CANARY],
                             "the verb refused and changed something anyway")


class ConcurrencyTest(RealSopsCase):
    """Two rotations at once must not lose a value that exists nowhere else."""

    def test_two_rotations_at_once_do_not_both_report_success(self):
        """Round three had no lock. Both calls read the same file, both wrote, both
        returned zero, and the value the loser generated was gone for good — it had never
        been printed anywhere, by design."""
        import threading

        path = self.seal()
        results, values = {}, {}
        started = threading.Barrier(2)

        def rotate(tag, pinned):
            started.wait()
            values[tag] = pinned
            results[tag] = set_secret(self.instance, namespace="widgets-prod",
                                      name="dashboard-signin", key="admin-password",
                                      value_factory=lambda: pinned)

        threads = [threading.Thread(target=rotate, args=(t, f"pinned-{t}")) for t in ("a", "b")]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        landed = self.opened(path)["admin-password"]
        winners = [tag for tag, code in results.items() if code == 0]
        self.assertEqual(len(winners), 1,
                         f"both rotations reported {results}; the value that did not land is lost")
        self.assertEqual(landed, values[winners[0]],
                         "the rotation that reported success is not the one whose value is in the file")
        self.assertEqual(self.opened(path)["admin-user"], KEPT)


# ---------------------------------------------------------------- round five
# A fourth review, a fourth loss of the same class. Round four stopped rebuilding the
# document, which removed three instances. It did not remove the cause: the code still
# decides for itself WHERE the sealed values live. A sops file carries its own pattern
# saying which of its fields are sealed; this code assumes two field names. Give it a file
# whose pattern is NARROWER than that assumption and the freshly minted credential is
# written in the clear, the read-back opens it happily, and the verb reports success.
#
# The earlier envelope checks missed it because they only ever WIDEN the pattern. Widening
# is the safe direction. So the contract stops enumerating seal shapes and asks the only
# question that matters about the result, in a form that needs no knowledge of the sealing
# tool's pattern language: after a rotation, the new value must not be findable in the file
# it was written to, under any encoding. That is a property of the outcome, and a fifth
# shape nobody has thought of is covered by it too.

PINNED = "the-new-credential-must-not-be-findable"


def encodings_of(value: str) -> dict[str, str]:
    """Every form the value could take in a file that claims to have sealed it."""
    raw = value.encode()
    return {
        "as written": value,
        "base64": base64.b64encode(raw).decode(),
        "base64 without padding": base64.b64encode(raw).decode().rstrip("="),
    }


class TheValueIsNotInTheFileTest(RealSopsCase):
    """The check that ends the class. One question, asked of every shape."""

    def seal_with(self, *, regex: str = None, suffix: str = None, field: str = "data",
                  namespace="widgets-prod", name="dashboard-signin") -> Path:
        """A sealed file whose OWN pattern decides what is sealed, which may be narrower
        than anything this code knows about."""
        values = {"admin-password": CANARY, "admin-user": KEPT}
        if field == "data":
            body = {k: base64.b64encode(v.encode()).decode() for k, v in values.items()}
        else:
            body = dict(values)
        doc = yaml.safe_dump({
            "apiVersion": secrets_mod.SOPS_API, "kind": secrets_mod.SOPS_KIND,
            "metadata": {"name": name, "namespace": namespace},
            "spec": {"secretTemplates": [{"name": name, field: body}]},
        }, sort_keys=False)
        how = ["--encrypted-regex", regex] if regex else ["--encrypted-suffix", suffix]
        proc = self.sops("encrypt", "--age", self.recipient, *how,
                         "--input-type", "yaml", "--output-type", "yaml",
                         "/dev/stdin", stdin=doc)
        self.assertEqual(proc.returncode, 0, proc.stderr[:400])
        path = self.instance / "clusters" / "platform" / "secrets" / namespace / f"{name}.yaml"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(proc.stdout)
        return path

    def rotate(self, key="admin-password", name="dashboard-signin"):
        return set_secret(self.instance, namespace="widgets-prod", name=name, key=key,
                          value_factory=lambda: PINNED)

    def assert_not_findable(self, path: Path, where: str):
        text = path.read_text()
        for how, form in encodings_of(PINNED).items():
            self.assertNotIn(form, text,
                             f"{where}: the new credential is in the file {how}; "
                             "the verb wrote a secret the file does not seal")

    # -- the shapes the review proved, and the one that was always safe ---------------

    def test_the_shape_the_contract_already_covered_still_holds(self):
        """The wide direction, which four rounds have all handled."""
        path = self.seal_with(regex="^(data|stringData|recoveryPhrase)$")
        self.assertEqual(self.rotate(), 0)
        self.assert_not_findable(path, "a file sealing more than the usual fields")

    def test_a_file_whose_pattern_is_narrower_than_this_code_assumes(self):
        """The sops manual's own Kubernetes example: only `data` is sealed, and the key
        being rotated lives under `stringData`."""
        path = self.seal_with(regex="^(data)$", field="stringData")
        code = self.rotate()
        self.assert_not_findable(path, "a file sealing only one of the two fields")
        self.assertNotEqual(code, 0, "the verb reported success for a value it left in clear")

    def test_a_file_sealed_by_suffix_rather_than_by_pattern(self):
        """The other way the tool is told what to seal. Base64 is not encryption."""
        path = self.seal_with(suffix="Secret")
        code = self.rotate()
        self.assert_not_findable(path, "a file sealed by suffix")
        self.assertNotEqual(code, 0, "the verb reported success for a value it left in clear")

    # -- the guards ------------------------------------------------------------------

    def test_the_fixtures_really_do_seal_differently_from_one_another(self):
        """Anti-vacuity: each fixture must produce a file the tool opens, and the narrow
        one must really leave its other field unsealed before any rotation happens."""
        wide = self.seal_with(regex="^(data|stringData|recoveryPhrase)$")
        self.assertTrue(self.opens(wide))
        narrow = self.seal_with(regex="^(data)$", field="stringData")
        self.assertTrue(self.opens(narrow))
        self.assertIn(CANARY, narrow.read_text(),
                      "the narrow fixture sealed the field after all; it proves nothing")

    def test_the_check_would_notice_a_value_left_in_the_open(self):
        """Anti-vacuity for the check itself: plant the value and require it to be found."""
        path = self.seal_with(regex="^(data)$", field="stringData")
        path.write_text(path.read_text() + f"\nplanted: {PINNED}\n")
        with self.assertRaises(AssertionError):
            self.assert_not_findable(path, "a planted value")


class ReadUnderTheLockTest(RealSopsCase):
    """A rotation that reported success must not be undone by one that started earlier."""

    def test_a_rotation_that_straddles_another_cannot_put_the_old_value_back(self):
        """The file is read before the lock is taken, and that stale snapshot is what gets
        written back. One rotation reports success and another silently reverts it. The
        lock alone does not close this: it only decides who writes, not who read first."""
        import threading

        path = self.seal()
        opened = threading.Event()
        released = threading.Event()
        real_load = secrets_mod.yaml.safe_load
        first = {"seen": False}

        def slow_load(text):
            doc = real_load(text)
            if not first["seen"]:
                first["seen"] = True
                opened.set()
                released.wait(timeout=15)
            return doc

        results = {}

        def rotate(tag, key, pinned):
            results[tag] = set_secret(self.instance, namespace="widgets-prod",
                                      name="dashboard-signin", key=key,
                                      value_factory=lambda: pinned)

        with unittest.mock.patch.object(secrets_mod.yaml, "safe_load", slow_load):
            straddler = threading.Thread(target=rotate, args=("straddler", "admin-user", "u-new"))
            straddler.start()
            self.assertTrue(opened.wait(timeout=15),
                            "the straddling rotation never read the file")
            results["inner"] = set_secret(self.instance, namespace="widgets-prod",
                                          name="dashboard-signin", key="admin-password",
                                          value_factory=lambda: "p-new")
            released.set()
            straddler.join(timeout=30)

        self.assertTrue(first["seen"], "the straddling rotation never read the file at all")
        self.assertEqual(results.get("inner"), 0,
                         "the inner rotation did not complete while the other was mid-flight; "
                         f"this check proves nothing as written: {results}")
        after = self.opened(path)
        self.assertEqual(after["admin-password"], "p-new",
                         "a rotation that reported success was silently put back")
        if results.get("straddler") == 0:
            self.assertEqual(after["admin-user"], "u-new",
                             "the straddling rotation reported success and its value is not there")


class DriftedRecipientTest(RealSopsCase):
    """The refusal the third design needed and this one does not."""

    def test_a_file_sealed_for_a_key_the_instance_no_longer_declares_can_still_rotate(self):
        """Round three had to refuse this, because it re-sealed from the instance's
        declared recipient and would have locked the file's real holders out. This design
        never uses that recipient, so the refusal costs an operator a live credential and
        buys nothing. If the verb still refuses, it is carrying the old design's scar."""
        keyfile = self.tmp / "other.txt"
        subprocess.run([AGE_KEYGEN, "-o", str(keyfile)], capture_output=True, text=True, check=True)
        other = self._public_key(keyfile.read_text())
        path = self.seal()
        # the instance now declares a recipient the file was not sealed for
        secrets_mod.write_sops_yaml(self.instance / ".sops.yaml", other)
        code = set_secret(self.instance, namespace="widgets-prod", name="dashboard-signin",
                          key="admin-password")
        self.assertEqual(code, 0, "the rotation refused a file its own key still opens")
        self.assertTrue(self.opens(path), "the rotation produced a file the desk cannot open")
        self.assertEqual(self.opened(path)["admin-user"], KEPT)


# ---------------------------------------------------------------------------------------
# Round seven. The check that was supposed to end the class was itself an instance of it.
# ---------------------------------------------------------------------------------------


class TheValueHasOneSerialisationTest(TheValueIsNotInTheFileTest):
    """The sixth instance, and why constraining the value ends the question.

    Round five stopped rebuilding the document and asked an outcome question instead: is
    the new value in the file's bytes? It asked that of three forms — as written, base64,
    base64 without padding. A review produced a fourth: the document decides how a value is
    serialised, and a value carrying a newline is written as an indented block scalar, so
    none of the three appears and the check passes over a credential left in the open. The
    code was deciding what the file already states — the class, now in the check rather
    than in the rotation.

    The answer is not a fifth form to search for. Searching for forms is the defect: the
    list is written by the file, not by this code, and it will always be one short. The
    answer is that a credential is a single printable line, checked before anything is
    written. A value of that shape has one serialisation in every file shape this verb
    accepts, so the outcome check's three forms are then the whole list rather than a
    guess at it.

    Probed before this was written: with the shape rule in place there is no value the verb
    accepts and the file writes in a form the check misses. A file stored as JSON is
    refused as not a sealed file; a value carrying a quote is base64-encoded into `data`
    before the document ever sees it; and the narrow-pattern cases below already refuse. So
    this contract requires the shape rule and keeps the outcome check, and does not pretend
    to force a document-parsing check it cannot make fail.
    """

    def rotate_value(self, value: str, key="admin-password"):
        return set_secret(self.instance, namespace="widgets-prod", name="dashboard-signin",
                          key=key, value_factory=lambda: value)

    def assert_refused_and_unchanged(self, path: Path, code: int, value: str, why: str):
        self.assertNotEqual(code, 0, why)
        self.assertEqual(self.opened(path)["admin-password"], CANARY,
                         "a refused rotation changed the credential")
        self.assertEqual(self.opened(path)["admin-user"], KEPT)
        for how, form in encodings_of(value).items():
            self.assertEqual(self.leaks_under(self.instance, form), [],
                             f"a refused rotation left the value on disk {how}")

    # -- the shape of a credential -------------------------------------------------------

    def test_a_value_carrying_a_newline_never_reaches_the_file(self):
        """The proven sixth instance. The file writes this as a block scalar."""
        path = self.seal()
        value = "first-line\nsecond-line-must-never-reach-disk"
        code = self.rotate_value(value)
        self.assert_refused_and_unchanged(
            path, code, value,
            "a value carrying a newline was sealed; the file writes it in a form the "
            "check does not search for")

    def test_a_value_carrying_a_carriage_return_never_reaches_the_file(self):
        path = self.seal()
        value = "first\rsecond-must-never-reach-disk"
        self.assert_refused_and_unchanged(path, self.rotate_value(value), value,
                                          "a value carrying a carriage return was sealed")

    def test_a_value_that_is_not_printable_never_reaches_the_file(self):
        path = self.seal()
        value = "before\x00after-must-never-reach-disk"
        self.assert_refused_and_unchanged(path, self.rotate_value(value), value,
                                          "a value carrying a control byte was sealed")

    def test_an_empty_value_never_reaches_the_file(self):
        path = self.seal()
        self.assertNotEqual(self.rotate_value(""), 0,
                            "the credential was rotated to nothing")
        self.assertEqual(self.opened(path)["admin-password"], CANARY)

    def test_the_refusal_does_not_repeat_the_value(self):
        import io
        import sys

        self.seal()
        value = "first-line\nsecond-line-must-never-reach-disk"
        err = io.StringIO()
        saved = sys.stderr
        sys.stderr = err
        try:
            self.rotate_value(value)
        finally:
            sys.stderr = saved
        for how, form in encodings_of(value).items():
            self.assertNotIn(form, err.getvalue(),
                             f"the refusal printed the value it refused {how}")

    def test_the_value_the_verb_generates_is_of_that_shape(self):
        """The constraint must not refuse the verb's own credential."""
        path = self.seal()
        self.assertEqual(set_secret(self.instance, namespace="widgets-prod",
                                    name="dashboard-signin", key="admin-password"), 0)
        after = self.opened(path)["admin-password"]
        self.assertNotEqual(after, CANARY)
        self.assertEqual(after, after.strip())
        self.assertTrue(after.isprintable(), "the verb generated a value it would refuse")
        self.assertNotIn("\n", after)

    # -- the check itself ---------------------------------------------------------------

    def test_a_narrow_pattern_still_refuses_a_value_carrying_a_quote(self):
        """Single-line and printable, so the shape rule admits it. The file's own pattern
        seals only one field, so the rotated field would be left in the open: the verb must
        notice and refuse rather than move the file into place. Held here because the shape
        rule must not become a reason to trust the outcome check less."""
        path = self.seal_with(regex="^(admin-user)$")
        before = path.read_bytes()
        value = 'quote"and\\backslash-must-never-reach-disk'
        code = self.rotate_value(value)
        self.assertNotEqual(code, 0, (
            "the verb sealed into a file whose own pattern leaves that field in the open"))
        self.assertEqual(path.read_bytes(), before, "a refused rotation changed the file")
        for form in encodings_of(value).values():
            self.assertEqual(self.leaks_under(self.instance, form), [])

    def test_a_narrow_pattern_refuses_an_ordinary_value_too(self):
        """Anti-vacuity for the case above: the same file and the same narrow pattern with
        an ordinary value must ALSO refuse, so that case is passing on the outcome check
        rather than on anything about the value."""
        path = self.seal_with(regex="^(admin-user)$")
        before = path.read_bytes()
        self.assertNotEqual(self.rotate_value("ordinary-value-must-never-reach-disk"), 0)
        self.assertEqual(path.read_bytes(), before)

    def test_a_file_that_seals_the_field_still_rotates(self):
        """And the guard against neutralising all of this by refusing everything."""
        path = self.seal()
        self.assertEqual(self.rotate_value("a-perfectly-ordinary-new-credential"), 0,
                         "the verb now refuses a rotation it should do")
        self.assertEqual(self.opened(path)["admin-password"],
                         "a-perfectly-ordinary-new-credential")
        self.assertEqual(self.opened(path)["admin-user"], KEPT)


# ---------------------------------------------------------------------------------------
# Round eight. Stop predicting what the emitter will write; write only where there is
# nothing to predict.
# ---------------------------------------------------------------------------------------


class TheVerbWritesOnlyWhereThereIsOneFormTest(TheValueIsNotInTheFileTest):
    """The seventh instance, and the last question this class can ask.

    Round seven required the value to be a single printable line, on the premise that such
    a value has one serialisation in every file shape the verb accepts. A review broke the
    premise on the field that stores values in clear. The document's emitter re-serialises,
    and two families of admitted value land in the open while none of the three searched
    forms appears in the file:

      * a value carrying an apostrophe together with something that forces quoting, which
        the emitter writes with the apostrophe doubled;
      * a value carrying a character outside the basic plane, which the emitter writes as
        an escape.

    Both were proven against the real tool, decrypt-confirmed, under two different sealing
    patterns. A fifth searched form is not the answer: the list of escapes is written by
    the emitter, and this code will always be one behind it. That has now been the shape of
    every round of this node.

    So the verb stops writing anywhere the emitter has a choice. The field that stores
    values base64-encoded has exactly one serialisation for every value, which the same
    review confirmed across a twenty-six value sweep. A key in a field that stores values
    in clear is refused, naming the reason, and the question of what the emitter would have
    done never arises.

    Every sealed file in the live tree stores its values in the encoded field; the
    plaintext field appears in them only inside the pattern saying which fields are sealed.
    So this refuses a shape the platform does not have, rather than one it depends on.
    """

    def rotate_value(self, value: str, name="dashboard-signin"):
        return set_secret(self.instance, namespace="widgets-prod", name=name,
                          key="admin-password", value_factory=lambda: value)

    def assert_refused_and_clean(self, path: Path, code: int, value: str, why: str):
        self.assertNotEqual(code, 0, why)
        self.assertEqual(path.read_bytes(), self.before, "a refused rotation changed the file")
        for how, form in encodings_of(value).items():
            self.assertEqual(self.leaks_under(self.instance, form), [],
                             f"a refused rotation left the value on disk {how}")

    # -- the two families the review proved, on the field that stores values in clear ----

    def seal_plaintext(self, regex="^(admin-user)$") -> Path:
        path = self.seal_with(regex=regex, field="stringData")
        self.before = path.read_bytes()
        return path

    def test_a_value_whose_apostrophe_the_emitter_doubles(self):
        path = self.seal_plaintext()
        value = "it's: must-never-reach-disk"
        self.assert_refused_and_clean(
            path, self.rotate_value(value), value,
            "the value was written into a field stored in clear, where the emitter doubled "
            "its apostrophe, so none of the searched forms appears and the check passed "
            "over a credential left in the open")

    def test_a_value_whose_trailing_apostrophe_forces_quoting(self):
        path = self.seal_plaintext()
        value = "trail-quote': must-never-reach-disk"
        self.assert_refused_and_clean(path, self.rotate_value(value), value,
                                      "the emitter quoted the value and the check missed it")

    def test_a_value_outside_the_basic_plane(self):
        path = self.seal_plaintext()
        value = "hello-\U0001F511-must-never-reach-disk"
        self.assert_refused_and_clean(
            path, self.rotate_value(value), value,
            "the emitter wrote the value as an escape sequence and the check missed it")

    def test_the_same_shapes_under_a_suffix_sealing_pattern(self):
        path = self.seal_with(suffix="Sealed", field="stringData")
        self.before = path.read_bytes()
        value = "it's: must-never-reach-disk-too"
        self.assert_refused_and_clean(path, self.rotate_value(value), value,
                                      "a file sealed by suffix rather than by pattern")

    # -- the refusal is about the field, not about the value ----------------------------

    def test_an_ordinary_value_in_a_plaintext_field_is_refused_too(self):
        """The refusal must not be a fifth list of dangerous values. It is about where the
        value would be written, so an entirely ordinary value is refused in the same place
        for the same reason. If this passes while the three above fail, the repair guessed
        at values again."""
        path = self.seal_plaintext(regex="^(data|stringData)$")
        self.assertNotEqual(self.rotate_value("an-entirely-ordinary-credential"), 0,
                            "the verb still writes into a field whose form the emitter "
                            "chooses; the repair was written about values, not about place")
        self.assertEqual(path.read_bytes(), self.before)

    def test_the_refusal_says_what_is_wrong(self):
        import io
        import sys

        self.seal_plaintext()
        err = io.StringIO()
        saved = sys.stderr
        sys.stderr = err
        try:
            self.rotate_value("an-entirely-ordinary-credential")
        finally:
            sys.stderr = saved
        self.assertTrue(err.getvalue().strip(), "the refusal said nothing")
        self.assertNotIn("an-entirely-ordinary-credential", err.getvalue())

    # -- and the shape the platform actually has still rotates ---------------------------

    def test_the_encoded_field_still_rotates(self):
        """The guard that matters most. Every sealed file the platform holds stores its
        values encoded, and this is the file the secret plane's acceptance rotates. A
        repair that refused everything would pass every check above."""
        path = self.seal()
        self.assertEqual(self.rotate_value("a-perfectly-ordinary-new-credential"), 0,
                         "the verb now refuses the only file shape the platform has")
        self.assertEqual(self.opened(path)["admin-password"],
                         "a-perfectly-ordinary-new-credential")
        self.assertEqual(self.opened(path)["admin-user"], KEPT)

    def test_the_encoded_field_holds_for_the_shapes_that_broke_the_other_one(self):
        """The encoded field has one serialisation for every value, so the families above
        are not dangerous there — they are simply stored, encoded, and sealed."""
        path = self.seal()
        value = "it's: an-awkward-but-legitimate-credential"
        self.assertEqual(self.rotate_value(value), 0)
        self.assertEqual(self.opened(path)["admin-password"], value)
