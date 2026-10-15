"""Documents that still describe the plane this release retired.

WHY THIS FILE EXISTS. The retirement decision sorted the shipped documents into two piles:
rewrite the ones that tell a reader what to do, and mark the ones that are records of how it
used to work. Four documents were marked. The pile was never finished, and the work stopped
at the documents whose defect somebody had pointed at -- the same scoping fault the front has
now ruled on twice: a decision carried out where the last instance was found is a decision
waiting for the next instance.

So nine documents still describe the retired plane as the thing that applies a change. One
tells the reader that a merge makes it deploy. Another says it reverts a direct mutation, so
an operator need not worry. Neither carries any mark, and a reader who opens either has no
way to know the machine it names was deleted.

WHAT THIS FILE ASKS, AND WHAT IT DELIBERATELY DOES NOT. The neighbouring contract over the
surface's own strings learned four rounds' worth of the same lesson: no rule over prose
decides whether a sentence promises that something else will act. This file does not try. It
asks a question with no notion of meaning: does the document name the plane at all, and if it
does, is the reader told. Being told is one of two things -- the document opens with a dated
banner saying it was superseded, or the sentence that names it is on a dated list somebody
decided, one row per sentence, with the reason.

That is stricter than the decision was, on purpose. A document that names a machine which no
longer exists owes its reader a word either way, and a rule that needs no judgement is a rule
no wrapping, bolding or fencing walks past.

Run under pytest from services/orbitd.
"""

from __future__ import annotations

import pathlib
import re

import pytest

from shipped_documents import every_tracked_markdown, repo_root

#: The plane this release retired, in every spelling a document uses for it. The long name is
#: matched after the one reduction below, so the vendor's spelling with a space, the
#: hyphenated one and the shouted one are all the same literal. The short name is matched as a
#: word on the unreduced text, because that is how the documents that describe it as live
#: actually write it -- "merge, and it applies" with the family name alone.
A_RETIRED_PLANE = "argocd"
ITS_SHORT_NAME = re.compile(r"\bargo\b", re.I)

#: Other products of the same family, which this tree discusses legitimately and which the
#: short name would otherwise swallow. Each is a two-word phrase, subtracted before the short
#: name is looked for, so a document may weigh one of them without being told to mark itself.
#: A phrase that no document uses is not an error: the family has more members than we name.
THE_SAME_FAMILY_BUT_NOT_THE_PLANE = ("argo events", "argo workflows", "argo rollouts",
                                     "argo cd install")

#: A banner: the first line of the document, quoted, carrying a date. Its wording is the
#: author's; the date is what makes it a decision somebody took on a day rather than a note.
A_DATED_BANNER = re.compile(r"^>.*\b20\d\d-\d\d-\d\d\b")

#: Sentences a person decided, on a date, legitimately name the plane with no banner over the
#: document. One row per sentence: (document, the exact text on the line, why, when). A row
#: that matches nothing in the tree is a defect of its own -- the sentence changed and nobody
#: re-decided it -- so the rows are guarded below.
ALLOWED: tuple[tuple[str, str, str, str], ...] = (
    ('docs/ONBOARDING.md', '**Argo CD.** Retired: the template no longer ships it, and there is no console to', 'it names the plane only to say it is retired and that no console of it remains; the paragraph then gives the desk and the ship flow as the replacements', '2026-09-12'),
    ('docs/ONBOARDING.md', 'open. An existing instance still carrying `clusters/platform/config/argocd/oidc.yaml`', "it names the deleted config path only to tell an old instance it loses that file on the next make init; the sentence's verdict is removal", '2026-09-12'),
    ('docs/CHANGELOG.md', '— ArgoCD removed from the template', 'release history: the v0.28.0 line records the removal itself', '2026-10-04'),
    ('docs/CHANGELOG.md', '— plugins: write the enabled default out — omitted, Argo is permanently OutOfSync', 'release history: the v0.3.10 line, written when the plane still synced the cluster', '2026-10-04'),
    ('docs/README.md', 'secret plane, ArgoCD, Zitadel, agentgateway, the console, the context lake) or plans that', 'the index lists it among the superseded designs whose records were removed', '2026-10-04'),
    ('docs/README.md', '| DESIGN-v2.md | the ArgoCD-to-desk handover design', 'the index names a removed record by what it recorded: the handover away from the plane', '2026-10-04'),
    ('docs/V1.md', 'agentgateway|argocd|zitadel|infisical|external-secrets|dexidp|ebpf-instrument|example', 'evidence: the grep that proved no retired component image is left on the node', '2026-10-04'),
)


def _without_the_rest_of_the_family(text: str) -> str:
    for phrase in THE_SAME_FAMILY_BUT_NOT_THE_PLANE:
        text = re.sub(re.escape(phrase).replace(r"\ ", r"[\s\-]+"), " ", text, flags=re.I)
    return text


def _reduced(text: str) -> str:
    """Collapse whitespace and hyphens and lowercase, so every spelling of the long name is
    the one literal. A join can manufacture the name out of two words that merely end and
    begin the right way, which makes the rule err toward objecting; such a sentence goes in
    the dated list above."""
    return re.sub(r"[\s\-]+", "", text).lower()


def _names_it(line: str) -> bool:
    without = _without_the_rest_of_the_family(line)
    return A_RETIRED_PLANE in _reduced(without) or bool(ITS_SHORT_NAME.search(without))


def _allowed_for(path: str) -> tuple[str, ...]:
    return tuple(text for where, text, _why, _when in ALLOWED if path.endswith(where))


def _lines_that_name_it(label: str, text: str) -> list[tuple[int, str]]:
    allowed = _allowed_for(label)
    found = []
    for number, line in enumerate(text.splitlines(), start=1):
        if not _names_it(line):
            continue
        if any(text in line for text in allowed):
            continue
        found.append((number, line.strip()))
    return found


def _has_a_banner(text: str) -> bool:
    """A banner is the document's first line. Front matter is not prose a reader reads as a
    statement about the document, so a document that opens with it is asked for its banner on
    the first line after it."""
    body = text.lstrip()
    if body.startswith("---"):
        _, _, rest = body[3:].partition("\n---")
        body = rest.lstrip()
    first = body.splitlines()[0] if body else ""
    return bool(A_DATED_BANNER.match(first))


DOCUMENTS = every_tracked_markdown()


class TestTheRuleIsReadingSomething:
    """Every way this file could pass by measuring nothing."""

    def test_it_reads_the_whole_shipped_tree(self):
        assert len(DOCUMENTS) > 20, (
            f"the inventory returned {len(DOCUMENTS)} documents, which is not this tree")

    def test_some_document_names_the_plane(self):
        naming = [label for label, text in DOCUMENTS if _names_it(text)]
        assert naming, ("no document names the retired plane, so every row below passes on "
                        "nothing; if that is really true, delete this file")

    def test_some_document_carries_a_banner(self):
        # Since 2026-10-04 the superseded documents are deleted rather than bannered, so the
        # tree may legitimately hold no banner at all. The rule must still be able to tell a
        # marked document from an unmarked one, so when the tree offers no banner the
        # detector is proven on a minimal pair instead of being skipped.
        if any(_has_a_banner(text) for _label, text in DOCUMENTS):
            return
        marked = "\n".join(["> SUPERSEDED 2026-10-04 — replaced.", "", "# A record", ""])
        unmarked = "\n".join(["# A record", "", "> 2026-10-04 a quote further down", ""])
        assert _has_a_banner(marked), (
            "the banner detector does not recognise a dated banner")
        assert not _has_a_banner(unmarked), (
            "the banner detector accepts a document whose first line is not a banner")

    @pytest.mark.parametrize("where,text,why,when", ALLOWED,
                             ids=[row[0] for row in ALLOWED] or None)
    def test_every_allowed_sentence_is_still_there(self, where, text, why, when):
        path = repo_root() / where
        assert path.exists(), f"{where} was decided about on {when} and is gone"
        assert text in path.read_text(errors="replace"), (
            f"the sentence allowed in {where} on {when} ({why}) is not there any more, so "
            "nobody has decided about whatever replaced it")


class TestTheRuleCannotBeWalkedPast:
    """The shapes that defeated three earlier rules over this same subject, planted here so a
    fourth version cannot quietly stop seeing them."""

    PLANTED = (
        ("wrapped the way markdown wraps",
         "the change is in the repository and ArgoCD\napplies it on the next sync"),
        ("in bold", "After the bump, **ArgoCD** takes it from there."),
        ("in a fenced block", '```json\n{"applier": "ArgoCD"}\n```'),
        ("in lower case inside a command", "    kubectl -n argocd get applications"),
        ("with the vendor's own space", "Ask Argo CD to reconcile the bump."),
        ("by the family name alone", "merge, and Argo applies it for you"),
    )

    @pytest.mark.parametrize("how,text", PLANTED, ids=[row[0] for row in PLANTED])
    def test_a_planted_sentence_is_seen(self, how, text):
        assert any(_names_it(line) for line in text.splitlines()), (
            f"a sentence naming the retired plane {how} is invisible to this rule")

    def test_subtracting_the_family_does_not_subtract_the_plane(self):
        assert _names_it("Argo Events is capable of all three legs, and Argo applies it."), (
            "a line that weighs another product of the same family and then credits the "
            "retired plane is read as though it only weighed the other product")

    def test_another_product_of_the_family_is_not_the_plane(self):
        assert not _names_it("Argo Events is capable of all three legs."), (
            "weighing another product of the same family is read as naming the retired plane, "
            "so a document is told to mark itself over a machine it never mentioned")


class TestADocumentThatNamesItTellsItsReader:
    @pytest.mark.parametrize("label,text", DOCUMENTS, ids=[row[0] for row in DOCUMENTS])
    def test_it_carries_a_banner_or_the_sentence_was_decided(self, label, text):
        if _has_a_banner(text):
            return
        found = _lines_that_name_it(label, text)
        assert not found, (
            f"{label} names the plane this release retired on "
            f"{', '.join(str(number) for number, _line in found[:6])}"
            f"{' and elsewhere' if len(found) > 6 else ''}, carries no dated banner, and no "
            "row decided about it, so a reader is left describing a machine that was deleted. "
            f"First one: {found[0][1][:160]!r}")
