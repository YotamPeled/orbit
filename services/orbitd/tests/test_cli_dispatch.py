#!/usr/bin/env python3
"""
Every subcommand the desk advertises can actually be run.

WHAT THIS CAUGHT. `orbit platform retire` was built, tested, reviewed and landed. Its
parser was wired, its help text shipped, its behaviour was covered. It was never added to
the dispatch table, so every invocation returned "unknown command" — and nothing failed,
because the tests called the command's function directly and the parser test only asked
whether the parser knew the name. The defect surfaced when an operator's dry run of the
live cutover could not print what the verb would delete.

THE SHAPE OF THE MISTAKE. A command has two halves: the parser that accepts it and the
dispatcher that runs it. Tests that drive the halves separately can both pass while the
command is unreachable. So this contract walks the parser itself — whatever it advertises
today, including anything added next year — and requires the dispatcher to reach each one.

HOW IT DECIDES. It does not run the commands; most need a cluster. It asks the narrower
question the defect was about: does the dispatcher recognise the name? A refusal that says
"unknown command" is the failure. Anything else — a usage error about missing arguments, a
connection failure, a refusal about a missing instance directory — means the dispatcher
reached the command and the command took over, which is all this is about.

Run under pytest from services/orbitd.
"""
import argparse
import contextlib
import io
import unittest

from orbitd import client as client_mod

# What the dispatcher prints when it does not know a name. Matching the message is the
# point: it is the only observable difference between "not wired" and "wired, then
# refused for its own reasons".
UNKNOWN = "unknown command"


def advertised(group: str) -> list[str]:
    """Every subcommand name the desk's own parser accepts under `group`, read from the
    parser rather than from a list kept beside it — a list beside it is the thing that
    drifts."""
    parser = client_mod._parser()
    for action in parser._subparsers._group_actions:
        for name, sub in action.choices.items():
            if name != group:
                continue
            for inner in sub._subparsers._group_actions:
                return sorted(inner.choices)
    raise AssertionError(f"the desk parser advertises no {group!r} group")


def _minimal_argv(group: str, name: str) -> list[str]:
    """The shortest command line the parser will accept for this subcommand: its required
    positionals filled with placeholders, asked of the parser rather than hard-coded, so a
    command that grows an argument next year is still reached."""
    parser = client_mod._parser()
    sub = None
    for action in parser._subparsers._group_actions:
        if group in action.choices:
            for inner in action.choices[group]._subparsers._group_actions:
                sub = inner.choices[name]
    assert sub is not None, f"{group} {name} is not in the parser"
    argv = [group, name]
    required_options = []
    for action in sub._actions:
        if action.dest == "help":
            continue
        if action.option_strings:
            # A required flag stops the parser before the dispatcher is ever reached,
            # which would make this whole contract blind.
            if getattr(action, "required", False):
                required_options.append(action.option_strings[-1])
                if action.nargs != 0:
                    required_options.append("placeholder")
            continue
        if action.nargs in ("?", "*"):
            continue
        argv.append("placeholder")
    return argv + required_options


def dispatch_says_unknown(group: str, name: str) -> bool:
    """Run the desk far enough to learn whether the dispatcher recognised the name. The
    command is expected to fail afterwards — it has a placeholder for a directory and no
    cluster — and that failure is not this contract's business. Only the dispatcher's own
    refusal counts."""
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        try:
            client_mod.main(_minimal_argv(group, name))
        except BaseException:
            return False
    return UNKNOWN in (out.getvalue() + err.getvalue())


class PlatformDispatchTest(unittest.TestCase):
    def test_the_parser_advertises_the_commands_we_expect(self):
        """A guard: if this list ever shrinks, the test below passes by having nothing to
        check."""
        names = advertised("platform")
        for expected in ("plan", "apply", "release-owners", "retire"):
            self.assertIn(expected, names, f"the desk no longer advertises {expected!r}")

    def test_every_advertised_platform_command_is_dispatched(self):
        unreachable = []
        for name in advertised("platform"):
            if dispatch_says_unknown("platform", name):
                unreachable.append(name)
        self.assertEqual(unreachable, [], "the desk advertises these commands and the "
                                          "dispatcher does not know them, so every "
                                          "invocation returns 'unknown command': %s"
                                          % ", ".join(unreachable))

    def test_a_name_nobody_advertises_is_still_refused(self):
        """The other direction: the check above must not pass by the dispatcher accepting
        everything."""
        parser = client_mod._parser()
        with self.assertRaises(SystemExit):
            with contextlib.redirect_stderr(io.StringIO()):
                parser.parse_args(["platform", "definitely-not-a-command"])


class SecretsDispatchTest(unittest.TestCase):
    def test_every_advertised_secrets_command_is_dispatched(self):
        unreachable = [name for name in advertised("secrets")
                       if dispatch_says_unknown("secrets", name)]
        self.assertEqual(unreachable, [], "advertised and not dispatched: %s"
                                          % ", ".join(unreachable))


class CheckDispatchTest(unittest.TestCase):
    def test_check_is_a_desk_command_not_a_tool_call(self):
        """`orbit check all .` must reach the checks, not be rewritten to `orbit call check`."""
        self.assertEqual(client_mod._rewrite(["check", "--list"]), ["check", "--list"])
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = client_mod.main(["check", "--list"])
        self.assertEqual(code, 0, err.getvalue())
        from orbitd import checks
        for name in checks.names():
            self.assertIn(name, out.getvalue())


class FeaturesDispatchTest(unittest.TestCase):
    def test_the_parser_advertises_the_commands_we_expect(self):
        names = advertised("features")
        for expected in ("status", "set", "converge"):
            self.assertIn(expected, names, f"the desk no longer advertises {expected!r}")

    def test_every_advertised_features_command_is_dispatched(self):
        unreachable = [name for name in advertised("features")
                       if dispatch_says_unknown("features", name)]
        self.assertEqual(unreachable, [], "advertised and not dispatched: %s"
                                          % ", ".join(unreachable))


if __name__ == "__main__":
    unittest.main()
