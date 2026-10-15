#!/usr/bin/env python3
"""
The desk verb that rotates one key of a sealed file, against REAL sops.

WHY THIS IS SEPARATE FROM THE ROTATION ITSELF. `test_secrets_set.py` holds the rotation
function to what it does to the file. This holds the way in: the command a person actually
types, what it is forbidden to put on a command line, and the one row in the operator's
capability table that tells a reader where this lives. A function nobody can reach rotates
nothing, and a router that says nothing rotates a credential is worse than no router at all
once something does.

THE NEW CHANNEL. The desk adds exactly one direction the function never had: a command
line. A value on a command line is in the process listing, the shell history and every log
that records a command. So the command declares no option that could carry one, and the
value the rotation generates is hunted for afterwards in the tree, in what the person is
told, and in every argument list handed to a child process.

WHERE THE VALUE COMES FROM, AND WHY NOBODY TYPES IT. The verb generates the new credential
itself. That is not a convenience: a credential nobody has typed cannot have been pasted,
shell-expanded or scrolled back to, and the sealed file is the only place it exists. The
service reads it from the Secret the sealed file renders, so no person ever needs to know
it.

Run under pytest from services/orbitd.
"""
import argparse
import base64
import io
import re
import sys
from pathlib import Path

from orbitd import secrets as secrets_mod
from test_secrets_set import CANARY, KEPT, RealSopsCase

REPO = Path(__file__).resolve().parents[3]
OPERATOR_SKILL = REPO / "skills" / "orbit-operator" / "SKILL.md"


def encodings_of(value: str) -> dict[str, str]:
    raw = value.encode()
    return {"as written": value,
            "base64": base64.b64encode(raw).decode(),
            "base64 without padding": base64.b64encode(raw).decode().rstrip("=")}


def set_subparser() -> argparse.ArgumentParser:
    """The `set` subcommand, resolved off the real parser. A missing verb FAILS."""
    parser = secrets_mod.build_parser()
    holders = [a for a in parser._actions
               if isinstance(getattr(a, "choices", None), dict)]
    assert holders, "orbit secrets declares no subcommands"
    choices = holders[0].choices
    assert "set" in choices, (
        "orbit secrets has no `set`; the rotation is a function nobody can reach, "
        f"and the desk offers only {sorted(choices)}")
    return choices["set"]


class DeskCase(RealSopsCase):
    """Drives the desk as a person does: one argument list, nothing else.

    Everything the command says and every argument list it hands to a child is recorded,
    because most of what follows is about what must NOT appear in them.
    """

    def setUp(self):
        super().setUp()
        self.argvs: list[list[str]] = []
        self.out = ""
        self.err = ""
        real = secrets_mod.run_sops

        def recording(argv, **kwargs):
            self.argvs.append([str(a) for a in argv])
            return real(argv, **kwargs)

        secrets_mod.run_sops = recording
        self.addCleanup(setattr, secrets_mod, "run_sops", real)

    def run_set(self, *, key: str = "admin-password", namespace: str = "widgets-prod",
                name: str = "dashboard-signin", extra: list[str] | None = None) -> int:
        argv = ["set", str(self.instance), "--namespace", namespace,
                "--name", name, "--key", key, *(extra or [])]
        out, err = io.StringIO(), io.StringIO()
        saved = (sys.stdout, sys.stderr)
        sys.stdout, sys.stderr = out, err
        try:
            code = secrets_mod.main(argv)
        except SystemExit as exc:  # a parser that dies is a refusal too
            code = 2 if exc.code is None else int(exc.code)
        finally:
            sys.stdout, sys.stderr = saved
        self.out, self.err = out.getvalue(), err.getvalue()
        return code

    def rotate_one(self) -> tuple[Path, str]:
        """One successful rotation, returning the file and the value it now holds. Every
        leak hunt below runs against this value, so a rotation that did not happen fails
        here rather than passing an empty search."""
        path = self.seal()
        code = self.run_set()
        self.assertEqual(code, secrets_mod.EXIT_OK,
                         f"the desk refused a rotation it should have done: {self.err[:400]}")
        after = self.opened(path)["admin-password"]
        self.assertNotEqual(after, CANARY, "the desk reported success and rotated nothing")
        self.assertTrue(after, "the desk rotated the credential to nothing")
        return path, after


class TheDeskOffersTheVerbTest(DeskCase):
    def test_the_standalone_parser_offers_it(self):
        self.assertIsNotNone(set_subparser())

    def test_the_orbit_command_offers_it_too(self):
        """The skills name `orbit secrets set`; every other test here drives the
        standalone parser. If the two ever diverge, the skills are wrong."""
        root = argparse.ArgumentParser()
        sub = root.add_subparsers(dest="command")
        secrets_mod.add_parser(sub)
        args = root.parse_args(["secrets", "set", str(self.instance),
                                "--namespace", "widgets-prod",
                                "--name", "dashboard-signin", "--key", "admin-password"])
        self.assertEqual(getattr(args, "secrets_command", None), "set")

    def test_it_rotates_the_key_it_was_asked_about_and_no_other(self):
        path, _ = self.rotate_one()
        self.assertEqual(self.opened(path)["admin-user"], KEPT,
                         "the rotation took the key it was not asked about with it")

    def test_the_help_says_what_it_rotates(self):
        text = set_subparser().format_help().lower()
        self.assertIn("rotate", text)


class NoOptionCarriesAValueTest(DeskCase):
    """A credential on a command line is in the process listing, the shell history and
    every log that records a command. The function never had this channel; the desk does."""

    FORBIDDEN = re.compile(r"^--(value|secret|password|credential|new-value|plaintext|to)$")

    def test_no_option_is_shaped_like_a_credential(self):
        offenders = [s for a in set_subparser()._actions for s in a.option_strings
                     if self.FORBIDDEN.match(s)]
        self.assertEqual(offenders, [],
                         f"these options would carry a credential on a command line: {offenders}")


class TheGeneratedValueDoesNotTravelTest(DeskCase):
    def test_it_is_not_in_the_tree_in_clear(self):
        _, value = self.rotate_one()
        for how, form in encodings_of(value).items():
            self.assertEqual(self.leaks_under(self.instance, form), [],
                             f"the new credential is on disk {how}")

    def test_it_is_not_in_what_the_person_is_told(self):
        _, value = self.rotate_one()
        for how, form in encodings_of(value).items():
            self.assertNotIn(form, self.out + self.err,
                             f"the desk printed the new credential {how}")

    def test_it_is_not_in_any_argument_list_of_any_child(self):
        _, value = self.rotate_one()
        self.assertTrue(self.argvs, "no child process ran; this check would be empty")
        for argv in self.argvs:
            joined = " ".join(argv)
            for how, form in encodings_of(value).items():
                self.assertNotIn(form, joined,
                                 f"the new credential was passed to a child {how}: {argv[:2]}")

    def test_the_check_would_notice_a_value_left_in_the_open(self):
        """Anti-vacuity: the hunt above must be able to find something."""
        _, value = self.rotate_one()
        (self.instance / "leak.txt").write_text(value)
        self.assertEqual(self.leaks_under(self.instance, value), ["leak.txt"])


class TheDeskSaysWhatItDidTest(DeskCase):
    def test_it_names_the_file_and_the_key(self):
        self.rotate_one()
        said = self.out + self.err
        self.assertIn("widgets-prod", said)
        self.assertIn("dashboard-signin", said)
        self.assertIn("admin-password", said)


class TheRouterSaysWhereItLivesTest(DeskCase):
    """The capability table tells a reader with no tool for something where to go instead.
    Its row for rotating a credential has said 'no tool behind it yet' since before the
    rotation existed, and a row that is wrong about the desk is worse than no row."""

    def test_the_row_names_the_desk_command(self):
        rows = [line for line in OPERATOR_SKILL.read_text().splitlines()
                if line.startswith("|") and "rotate a secret" in line]
        self.assertEqual(len(rows), 1, f"expected one row about rotating a secret, got {rows}")
        self.assertIn("`orbit secrets set`", rows[0],
                      "the row does not name the desk command that now does this")
        self.assertNotIn("no tool behind it yet", rows[0],
                         "the row still says nothing rotates a credential")
