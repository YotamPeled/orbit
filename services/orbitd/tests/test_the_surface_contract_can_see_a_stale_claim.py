"""Whether the contract beside this one can see the claims it exists to catch.

WHY THIS FILE EXISTS. Three rounds on this node were defeated by a contract blind to its own
subject, and the front's rule is that a shape which fails twice is not repaired a third time
by the same means. The replacement contract does drive the surface and does compare what a
caller is handed against what the documents promise, and restoring the pre-branch documents
turns it red -- so the shape is right. What a review then found is narrower and still fatal:
the two patterns that lift a claim out of a paragraph cannot represent three ordinary ways of
writing one, and one of those ways is already in the shipped reference document, where it
keeps a false sentence readable by spelling the number in the plural.

A false claim in each of those three shapes was inserted into a shipped skill and the
contract stayed green; a singular status and an unspoken token each turned it red. So the
contract can be walked past by anyone writing normally, which is everyone.

A fourth round found four more spellings that walked past it -- the same code in bold, in a
fenced block, in an indented sample, and a status written `HTTP-503` -- and two sentences
that lie without ever saying the word "refused", which the paragraph gate therefore never
read. The contract was rebuilt to search, literally, for the words the driven surface never
says, so every row below is now held by a search with no notion of backticks, paragraphs or
keywords. One row went the other way and is recorded as a gap rather than a row: an invented
English outcome word (a document promising a reader the word "denied") is not something a
literal search can object to without objecting to true prose as well -- a document says "the
retry is rejected as a protocol error" truthfully -- so this file no longer claims that a
document inventing an English outcome word is held by anything.

WHAT THIS FILE HOLDS. Not the documents: the contract. Each row plants one false claim and
requires the contract to object to it. The controls are here too, so a repair that simply
objects to everything is caught by the same rows.

Run under pytest from services/orbitd.
"""

from __future__ import annotations

import pathlib
import shutil

import pytest

import test_skills_describe_this_surface as contract
from test_skills_describe_this_surface import measured  # noqa: F401  (the fixture)

pytestmark = pytest.mark.anyio

# Each row: what shape of claim it is, the paragraph, and whether the contract must object.
# The false ones are false about THIS surface: it answers 401, never 403 or 503, and it never
# produces the words in them.
#
# The controls matter as much as the rows. A first draft of this file planted
# `WWW-Authenticate` as a false claim on a review's say-so, and the surface really does send
# that header on its 401s -- so the row demanded the contract object to something true, and
# a build that obeyed it would have made the contract useless. Every row below was checked
# against a driven surface before it was written down. The pair `denied` / `decline` is the
# point of the whole file: no rule about spelling separates them, and the measurement does,
# because one of them is a reply this surface really accepts and the other is a word nobody
# here has ever said.
CLAIMS = (
    ("a hyphenated header name in backticks",
     "The refusal also carries an `X-Orbit-Scope` header naming the scope to present.",
     True),
    ("a status written in the plural",
     "Calls above your role are refused with 403s by the gate in front of the protocol.",
     True),
    ("a singular status the surface never answers",
     "A call above your role is refused with a 503 by the gate in front of the protocol.",
     True),
    ("a backticked code the surface never says",
     "A call above your role is refused with `insufficient_scope` as its error.",
     True),
    # The four spellings the fourth review found: each writes the same lie in a way the
    # pattern that used to lift claims out of prose could not represent.
    ("the same code in bold rather than backticks",
     "A call above your role is refused with **insufficient_scope** as its error.",
     True),
    ("the same code inside a fenced block",
     'A call above your role is refused:\n\n```json\n{"error": "insufficient_scope"}\n```',
     True),
    ("the same code in an indented sample",
     'A call above your role is refused, like this:\n\n    {"error": "insufficient_scope"}',
     True),
    ("a status written with a hyphen before it",
     "A call above your role is refused and comes back HTTP-503 from the gate.",
     True),
    # The two that lie without ever saying they are about a refusal. The gate that used to
    # decide which paragraphs to read never read either of them.
    ("a status in a paragraph that never says it is about a refusal",
     "Calls above your role are answered with 503 by the gate in front of the protocol.",
     True),
    ("a code in a paragraph that never says it is about a refusal",
     "The gate in front of the protocol hands back `insufficient_scope` on such calls.",
     True),
    # Controls, and they matter as much as the rows: a repair that objects to everything
    # fails here. A first draft of this file planted `WWW-Authenticate` as a false claim on
    # a review's say-so, and the surface really does send that header, so the row demanded
    # the contract object to something true. Every row here was checked against a driven
    # surface before it was written down.
    ("a hyphenated header the surface really does send (control)",
     "A refusal with no token carries a `WWW-Authenticate` header naming where to sign in.",
     False),
    ("the status this surface really does answer with (control)",
     "A call carrying no token at all is answered with a 401 by the gate.",
     False),
    ("a code this surface really does produce (control)",
     "An expired token comes back as `invalid_token` in the challenge header.",
     False),
    ("a number that is not a status at all (control)",
     "A refusal carries its time as unix seconds or RFC 3339, never as a bare offset.",
     False),
    ("a memory figure in a table, whose digits are not a status (control)",
     "A pod of this shape asks for | 128 Mi | and is limited to | 256 Mi |.",
     False),
    ("a hash name whose digits are not a status (control)",
     "A presented secret is kept only as its SHA-256 digest, never in clear.",
     False),
    ("a true claim (control)",
     "A call above your role is refused, and the refusal names the role the call needs.",
     False),
    # The four spellings a fifth review found, all of them past the half of this contract
    # that had never been inverted: a pattern with lookarounds around it, where the same
    # character that stops a version number also stops an ordinary full stop.
    ("a status that ends a sentence",
     "A call above your role is refused with 503.",
     True),
    ("a status glued to the word before it",
     "A call above your role comes back HTTP503 from the gate in front of the protocol.",
     True),
    ("a code written with a hyphen instead of an underscore",
     "A call above your role is refused with `insufficient-scope` as its error.",
     True),
    ("a code written with a space instead of an underscore",
     "A call above your role is refused with an insufficient scope error.",
     True),
    ("a code broken across a line wrap",
     "A call above your role is refused and the error it carries is insufficient_\nscope, "
     "which names the scope you would need.",
     True),
    ("a header invented with one segment rather than two",
     "The refusal also carries an `X-Scope` header naming the scope to present.",
     True),
    # More controls for the shapes above: each is the true sentence the planted one imitates.
    # Not a control: the price of the inversion, written down as a row so it is visible.
    # The lookahead that used to let a version number through is the same character that let
    # a status ending a sentence through, and a status hiding behind a full stop is the worse
    # of the two. So a version number whose last part happens to be a valid status is
    # objected to. No shipped document contains one; if one ever does, the allow-list is
    # where a person decides it, once, with a date.
    ("a version number whose last part is a status (the price, not a control)",
     "This document describes the surface from v1.503 onwards, and no earlier.",
     True),
    ("a four-digit number ending a sentence (control)",
     "Give start and end as unix seconds or as RFC 3339.",
     False),
)


@pytest.fixture
def planted(monkeypatch):
    """The shipped documents as the contract really reads them, with one paragraph added to
    the first of them. The contract reads its documents through two functions, and both are
    wrapped here, so it is the real contract running against a document it has never seen.

    Both, because the contract asks two questions of two scopes: a word or a code is asked of
    every document it judges, and a status is asked only of the documents whose subject is
    this surface's protocol. A plant that landed in one scope would leave the other half
    measuring nothing."""
    added: list = []
    real_documents = contract._documents
    real_protocol = contract._the_protocol_documents

    def with_the_plant(real):
        def wrapped(surface):
            documents = list(real(surface))
            if added and documents:
                label, text = documents[0]
                documents[0] = (label, text.rstrip("\n") + "\n\n"
                                + "\n\n".join(added) + "\n")
            return documents
        return wrapped

    monkeypatch.setattr(contract, "_documents", with_the_plant(real_documents))
    monkeypatch.setattr(contract, "_the_protocol_documents", with_the_plant(real_protocol))
    return added.append


async def _objections(measured) -> list[str]:  # noqa: F811
    """What the real contract says about the documents as they stand right now."""
    subject = contract.TestASkillPromisesOnlyWhatTheSurfaceAnswers()
    said = []
    for check in (subject.test_no_document_names_a_status_the_surface_does_not_answer,
                  subject.test_no_document_hands_a_reader_a_word_the_surface_never_says):
        try:
            await check(measured)
        except AssertionError as objection:
            said.append(str(objection))
    return said


class TestTheOneHandOperatedLeverCannotBeUsedToHideARepair:
    """The allow-list is the only thing in this contract a person operates by hand, so it is
    the only place a rule repaired into objecting to everything could be hidden. A review
    asked exactly that and found three ways. Each is a row here."""

    LIE = "Calls above your role are answered with 503 by the gate in front of the protocol."

    @staticmethod
    def _judged(rows, label="a-document"):
        import unittest.mock
        with unittest.mock.patch.object(contract, "ALLOWED", rows):
            return contract._offences(
                label, TestTheOneHandOperatedLeverCannotBeUsedToHideARepair.LIE,
                {503}, {"insufficient_scope"})

    def test_without_a_row_the_planted_line_is_caught(self):
        assert self._judged(()), "the planted line is not caught, so the rows below prove nothing"

    def test_a_row_that_excuses_nothing_is_not_a_row(self):
        """A row matching no line is either a sentence that changed or a lever somebody
        reached for and did not aim. Either way a reader should be told."""
        granted = (("a-document", "a sentence this document has never contained at all",
                    "for this test", "2026-09-12"),)
        assert self._judged(granted), (
            "a row that excuses nothing silenced the line anyway")

    def test_a_row_of_one_character_does_not_silence_everything(self):
        """The shape that would hide a repair-into-objecting-to-everything: one character
        that nearly every line contains."""
        granted = (("a-document", "e", "for this test", "2026-09-12"),)
        assert self._judged(granted), (
            "a one-character row silenced a line, so the lever can be used to silence every "
            "document at once")

    def test_a_row_granted_for_one_document_does_not_silence_another(self):
        granted = (("a-document", self.LIE, "for this test", "2026-09-12"),)
        assert not self._judged(granted, "a-document"), "the aimed row did not excuse its line"
        assert self._judged(granted, "another-document"), (
            "a row granted for one document silenced the same line in another")


class TestTheContractObjectsToAClaimHoweverItIsWritten:
    @pytest.mark.parametrize("how,paragraph,must_object", CLAIMS,
                             ids=[c[0] for c in CLAIMS])
    async def test_it_sees_this_shape(self, planted, measured, how, paragraph,  # noqa: F811
                                      must_object):
        planted(paragraph)
        said = await _objections(measured)
        if must_object:
            assert said, (
                f"a false claim written as {how} is invisible to the contract, so anyone "
                "writing that way can tell a reader something this surface never does: "
                + paragraph)
        else:
            assert not said, (
                f"the contract objects to a claim that is true ({how}), so it would refuse "
                "every correct document too: " + "; ".join(said)[:400])

    async def test_the_shipped_documents_pass_as_they_stand(self, planted,
                                                            measured):  # noqa: F811
        """Anti-vacuity for every row above: the copied tree with nothing planted must be
        quiet, or the rows are measuring the copy rather than the claim."""
        said = await _objections(measured)
        assert not said, (
            "the shipped documents do not satisfy their own contract: "
            + "; ".join(said)[:400])
