"""The rule that decides what a machine identity may be called, against every place that
restates it.

WHY THIS FILE EXISTS. A review found two things wrong with that rule and they are one thing.
Its length -- sixty-three characters -- is held by nothing: narrowing it to thirty left the
whole suite green, so the number in it is a number nobody checks. And the admin skill states
the pattern verbatim, which makes the document a fourth copy of a rule that already exists in
three places in the code, none of them derived from the others.

Four copies of a rule with nothing holding any of them is a rule that will drift, and drift
here is not cosmetic: a document that states a narrower rule than the surface enforces turns
away names the surface would take, and a wider one promises names it refuses. A reader of the
skill builds a name from what the skill says.

WHAT THIS FILE HOLDS. Not the pattern -- the behaviour. It drives the surface with a name of
every shape at and around the rule's edges, writes down which the surface really creates and
which it really refuses, and then requires every restatement -- each literal pattern in the
shipped code, and the one the admin skill puts in front of a reader -- to agree with the
driven answer on every one of them. Nothing here knows the right pattern in advance, which is
the point: change the rule and the copies must follow, in code and in the document alike.

WHAT A REVIEW CHANGED, 2026-09-12. Three things, and the first is the reason for the other
two. The walk read one of the two shipped skill trees and one document under `docs/`, so
"every place that restates it" meant a fifth of the places. It now reads every markdown
document this repository ships, from the one inventory module every document contract shares,
plus the surface's own prose -- a tool description is read by a caller before any call, so a
rule restated there is a rule somebody will build a name from.

Widening it showed that anchored patterns are not rare, and that most of them state some
other rule: which field of a Secret may be read, what a registry allows as a tag, how long a
silence may last. So a pattern is a restatement of THIS rule unless somebody has said which
other rule it is, in the declared list below, and that list is guarded from both sides -- a
row that matches nothing is stale, and a row may not declare a pattern the shipped code
carries as this rule, which is how a drifted copy would otherwise be hidden.

And the restatements are counted per route rather than in total. A total let a document copy
go dark: with a floor of four and five restatements present, the one a reader follows could
be rewritten in a spelling nothing matches and the number stayed above the floor.

Run under pytest from services/orbitd.
"""

from __future__ import annotations

import pathlib
import re

import pytest
from conftest import mint, rpc, sdk_client

from orbitd.roles import Role

pytestmark = pytest.mark.anyio


# A name of every shape at and around the rule's edges. The lengths matter most: the number
# in the pattern is the half of it nothing was holding, so a name exactly at the length, one
# under and one over are all here and all drive the surface.
NAMES = (
    ("a single character", "a"),
    ("a single digit", "7"),
    ("an ordinary name", "ops-team"),
    ("one under the rule's length", "a" * 62),
    ("exactly the rule's length", "a" * 63),
    ("one over the rule's length", "a" * 64),
    ("far over the rule's length", "a" * 300),
    ("the empty name", ""),
    ("a leading hyphen", "-ops"),
    ("a trailing hyphen", "ops-"),
    ("capitals", "Ops-Team"),
    ("an underscore", "ops_team"),
    ("a dot", "ops.team"),
    ("a space", "ops team"),
    ("a slash", "ops/team"),
    ("a name that is not Latin", "運用"),
)

# The three places in the shipped code that carry the rule, named by the symbol that holds
# it rather than found by searching for its text. By the symbol on purpose: a copy that has
# drifted is no longer textually the same as the others, so a search for the text stops
# seeing exactly the copy that went wrong.
#
# A fourth copy added somewhere new is not seen by this list, and that is the gap. It is a
# short, named list because the rule belongs in one place and three is already two too many;
# if a fifth appears, the answer is to delete it, not to lengthen this.
IN_THE_CODE = (
    ("orbitd.tools.PRINCIPAL_NAME_PATTERN", "orbitd.tools", "PRINCIPAL_NAME_PATTERN"),
    ("orbitd.cli.BOOTSTRAP_NAME_RE", "orbitd.cli", "BOOTSTRAP_NAME_RE"),
)
# A third constant used to be here: the identifier pattern the viewer tools use for a
# project, an environment and a component. It carries the same characters and it is not a
# copy of this rule -- it answers a different question about different things, nothing
# derives either from the other, and holding it to the name a principal may have sends the
# next maintainer to change the wrong one. Removed by the review that noticed, 2026-09-12.

# An anchored regex is a machine token and not prose: it begins and ends the way one does, so
# a search for that shape has nothing to walk past. Every one a shipped document states is
# held against the driven answers below.
#
# Today there is exactly one in the whole shipped surface and it is this rule. If a second
# appears that states some other rule, this file turns red and whoever added it has to say
# which rule it is -- which is the decision being made once and visibly, rather than a
# document quietly acquiring a pattern nobody checks. That is the point, not a defect.
A_PATTERN = re.compile(r"\^[^\s`\"']*\$")


# An anchored pattern a document states that is some OTHER rule, not the rule for a
# principal's name. Each row is (route, the pattern exactly as the route writes it, which
# rule it states, when that was decided). This is the one hand-operated lever in the file and
# it carries its own guards: a row that matches nothing is stale, and a row may not declare a
# pattern that the shipped code carries as this rule -- which is how a drifted copy would
# otherwise be silenced by calling it something else.
OTHER_RULES = (
    ("markdown docs/ONBOARDING.md", "^(data|stringData)$",
     "which field of a Secret a projection may read", "2026-09-12"),
    ("markdown skills/orbit-onboard/SKILL.md", "^(data|stringData)$",
     "which field of a Secret a projection may read (the sealing command, restated for the agent)", "2026-09-12"),
    ("markdown docs/PRODUCT.md", "^[a-z_]+$",
     "what a metric label may be called", "2026-09-12"),
    ("tool orbit_monitor_silence", "^[1-9][0-9]*[mhd]$",
     "how long a silence may last, written as a duration", "2026-09-12"),
)


def _in_the_code():
    import importlib

    found = []
    for label, module, symbol in IN_THE_CODE:
        held = getattr(importlib.import_module(module), symbol)
        found.append((label, 0, getattr(held, "pattern", held)))
    return found


def _restatements(surface=()):
    """Every place the creation rule is written down: the constants the shipped code carries,
    every anchored pattern any document this repository ships puts in front of a reader, and
    every one the surface's own prose hands a caller off the listing.

    The document inventory is not walked here. It comes from the one module that answers what
    this repository ships, because two contracts each walking their own path is how both came
    to read one skill tree of the two that exist. This one reads the widest kind -- every
    tracked markdown -- because the question is whether this rule is restated ANYWHERE, and a
    copy in a design note is still a copy a person will follow.

    A pattern a route states that is some other rule is declared in `OTHER_RULES` and is not
    a restatement of this one. Everything else is."""
    import shipped_documents

    found = _in_the_code()
    declared = {(route, said) for route, said, _, _ in OTHER_RULES}
    documents = list(shipped_documents.every_tracked_markdown()) + list(surface)
    for label, text in documents:
        for number, line in enumerate(text.splitlines(), 1):
            for said in A_PATTERN.findall(line):
                if (label, said) in declared:
                    continue
                found.append((label, number, said))
    return found


@pytest.fixture
async def surface_prose(state, http):
    """What the surface itself hands a caller off the listing: every tool description and
    title, every schema sentence, the instructions string, every resource description.

    A caller reads this before making any call, so a rule restated here is a rule somebody
    will build a name from, exactly like one in a document. It is collected by the same
    helper the sibling contract uses, from the inventory module, rather than by a second
    copy of the same walk."""
    import shipped_documents

    async with http(mint(state, Role.admin)) as h:
        listed = await rpc(h, "tools/list")
        discovered = await rpc(h, "server/discover")
        resources = await rpc(h, "resources/list")
        templates = await rpc(h, "resources/templates/list")
    return shipped_documents.surface_documents(
        listed.json(), discovered.json(), resources.json(), templates.json())


@pytest.fixture
async def created(state, http):
    """What the surface really does with each name: created, or refused. Driven, so the
    answer follows the surface rather than any copy of the rule."""
    answers = {}
    async with sdk_client(http, mint(state, Role.admin)) as client:
        for what, name in NAMES:
            result = await client.call_tool("orbit_auth_issue",
                                            {"name": name, "role": "viewer", "ttl": 3600})
            answers[what] = not result.is_error
    return answers


class TestTheDrivenAnswerIsWorthComparingAgainst:
    """Anti-vacuity. A drive that refused everything, or accepted everything, would agree
    with any pattern at all."""

    async def test_the_surface_creates_some_and_refuses_others(self, created):
        taken = [what for what, yes in created.items() if yes]
        turned = [what for what, yes in created.items() if not yes]
        assert len(taken) >= 4, f"the surface accepted only {taken}"
        assert len(turned) >= 4, f"the surface refused only {turned}"

    async def test_the_length_in_the_rule_is_really_the_edge(self, created):
        """The half of the rule nothing was holding. If these three agree, the number in the
        pattern is a number the surface actually enforces."""
        assert created["one under the rule's length"], (
            "a name one short of the rule's length was refused")
        assert created["exactly the rule's length"], (
            "a name of exactly the rule's length was refused, so the number in every copy of "
            "the pattern is not the number the surface enforces")
        assert not created["one over the rule's length"], (
            "a name one past the rule's length was created, so the length bounds nothing")

    def test_the_rule_is_restated_in_several_places(self):
        """Counted per route, never in total. A total is what let a document copy go dark:
        add a fifth restatement anywhere and the one a reader follows could be rewritten in
        a spelling nothing matches while the number stayed above its floor."""
        found = _restatements()
        in_code = {label for label, _, _ in found if label.startswith("orbitd.")}
        in_documents = {label for label, _, _ in found if not label.startswith("orbitd.")}
        assert in_code == {label for label, _, _ in _in_the_code()}, (
            "a constant the shipped code carries as this rule is no longer being read: "
            f"{sorted(in_code)}")
        assert in_documents, (
            "no shipped document states the rule, so the copy a reader builds a name from "
            "is not being held -- either the document lost it or it is now written in a "
            "spelling an anchored pattern does not match")
        assert len({said for _, _, said in found}) == 1, (
            "the copies of this rule are not all the same rule any more, which is the drift "
            f"this file exists to catch: {found}")

    async def test_every_pattern_called_another_rule_is_still_there(self, surface_prose):
        """The lever's own guard. A row that matches nothing means the route changed and
        nobody re-decided what the pattern in it states. Asked of the surface's own prose as
        well, because a rule the listing states is declared here the same way."""
        import shipped_documents

        texts = dict(shipped_documents.every_tracked_markdown())
        texts.update(dict(surface_prose))
        stale = [f"{route}: {said!r} ({which}, {when})"
                 for route, said, which, when in OTHER_RULES
                 if said not in texts.get(route, "")]
        assert not stale, (
            "a pattern a person once decided states some other rule is no longer there, so "
            f"the decision is unexamined: {stale}")

    def test_nothing_called_another_rule_is_this_one(self):
        """And the lever cannot be used to silence a drifted copy: a pattern the shipped code
        carries as this rule is this rule, whatever a row says it is."""
        ours = {said for _, _, said in _in_the_code()}
        disguised = [f"{route}: {said!r} is declared as {which}"
                     for route, said, which, _ in OTHER_RULES if said in ours]
        assert not disguised, (
            "a pattern the shipped code carries as the name rule is declared as some other "
            f"rule, which is how a copy that drifted would be hidden: {disguised}")


class TestEveryRestatementAgreesWithTheSurface:
    @pytest.mark.parametrize("what,name", NAMES, ids=[row[0] for row in NAMES])
    async def test_no_copy_of_the_rule_disagrees_about_this_name(self, created, surface_prose,
                                                                 what, name):
        """The whole file. A copy that would turn away a name the surface takes, or take one
        the surface turns away, is a copy that has drifted -- and a reader following the
        document is the one who pays for it."""
        really = created[what]
        offenders = []
        for label, number, said in _restatements(surface_prose):
            try:
                matched = re.match(said, name) is not None
            except re.error as bad:
                offenders.append(f"{label}:{number} is not a usable pattern: {said!r} ({bad})")
                continue
            if matched != really:
                offenders.append(
                    f"{label}:{number} {said!r} "
                    f"{'accepts' if matched else 'refuses'} {name[:40]!r} "
                    f"and the surface {'creates' if really else 'refuses'} it")
        assert not offenders, (
            f"a restatement of the name rule disagrees with the surface about {what}, so "
            f"whoever follows that copy is told the wrong thing: {offenders}")
