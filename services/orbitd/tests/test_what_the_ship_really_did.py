"""What a ship, a restart and the documents each say happened, against what happened.

WHY THIS FILE EXISTS. The node beside this one repaired a ship that answered `applied` when
its apply step had been skipped and no object had been written. That repair is right and it
is well held. A review then found three things it got wrong, and two of them are the same
mistake pointing in new directions.

The first is the repair's mirror. A restart writes the pod-template annotation through the
kube API BEFORE the apply step, and that patch IS the restart: changing the pod template is
what makes Kubernetes replace the pods. It needs no applier. The repair treated a skipped
apply as a refusal there too, so with the switch off the workload really is being replaced
while the operator is told the job was refused and nothing was applied.

The second is my own contract. The rule that no shipped string promises a retired plane
will act matched one proper noun within sixty characters of a verb, and read documents one
line at a time. The review put the false promise back twice with everything green: once
wrapped the way markdown wraps, so the name and the verb never met; once naming no plane at
all -- "the platform's sync controller applies it on its next pass and the deploy goes live
without you". The front's rule is that a contract blind to its own subject three rounds
running is not repaired a fourth time, and that the shape which holds drives the surface and
compares what a caller is handed against what the document promised.

That replacement was wrong too, and a fourth review found it three ways at once. Its walk
pointed one directory too shallow, so it read no documents at all and every case in it --
including the case that existed to stop exactly that -- passed on an empty sequence.
Repaired, it objected to two honest shipped paragraphs and let nine of nine new false
promises through, because `applied` is both one of the words it anchors to and one of the
verbs a false promise is written with. And it never read the tool descriptions, which the
rule it replaced did read: a false promise planted in one left the whole suite green.

Four rounds is the answer. The question "does this sentence falsely promise that something
else applies the change" is a question about what English means, and no rule over prose has
answered it here: the shapes that defeat every version name nothing at all ("the platform
picks the change up on its next pass, so your deploy takes effect with nothing further from
you"), and there is no registry to subtract them from. So this file stops asking it.

What it asks instead is exact and has no notion of meaning: no string this release puts in
front of a caller names a plane this release does not run. One literal name, searched over
exactly the routes the enumeration below returns, with a dated allow-list for the one
historical sentence that legitimately names it. That catches the naming, which is what
actually shipped wrong; it does not catch a sentence that credits nobody, and the planted
row below marked as not caught is the whole of what this rule gives up. The routes this
rule reads are exactly the ones its own enumeration returns: print them with
`python -m pytest tests/test_what_the_ship_really_did.py -k every_route -s` run from
services/orbitd.

Run under pytest from services/orbitd.
"""

from __future__ import annotations

import json
import inspect
import pathlib
import re
import sys

import pytest
from conftest import mint, sdk_client

from orbitd.roles import Role

from test_apply_pairs import set_pairs
from orbitd import deploy

from test_reconcile import (COMMIT as SHIPPED, ENV, OLD, PROJECT, FakeSource, rendered,
                            restart, set_mode, use_render)
from test_tools_operator import TAG, WORKER, install, seed_world, wait_result

pytestmark = pytest.mark.anyio


# The plane this release retired. It is one literal string and it is searched for as one:
# a name does not straddle a line break, appear in bold, or hide in a fenced block the way a
# subject and its verb do, so there is no spelling for it to walk past.
A_RETIRED_PLANE = "argocd"


def _normalise(text: str) -> str:
    """The single reduction every shipped line goes through before the search: collapse
    whitespace and hyphens to nothing and lowercase it. After this reduction the
    vendor's own official spelling with a space, the hyphenated spelling, the
    upper-case spelling and a double space are all the one literal name, and the search
    that follows stays literal with nothing to walk past. The same join can manufacture
    the name out of adjacent words that merely end and begin the right way (`Cargo CD`
    reduces to text containing it), so a legitimate line caught that way goes in the dated
    allow-list below: the rule errs toward objecting."""
    return re.sub(r"[\s\-]+", "", text).lower()

# Sentences a person decided, on a date, legitimately name it. Each row is (where, the exact
# text that may appear on the line, why, when it was decided). A row that matches nothing is
# a defect of its own: the sentence changed and nobody re-decided it.
ALLOWED: tuple[tuple[str, str, str, str], ...] = ()

# The one exception ever granted -- the reference document's sentence about what this
# surface replaced -- was rewritten on 2026-09-12 to say who applies now, so nothing on the
# shipped surface names the plane and the list above is empty. The mechanism the rows
# below exercise (a sentence granted in one document, and only there, with nothing
# appended) is kept honest with this synthetic grant, which the shipped documents never
# match and which the test plants for those rows alone.
A_SYNTHETIC_GRANT = (
    ("ORBITD.md",
     "Before orbitd replaces ArgoCD as the applier",
     "a synthetic exception that exercises the allow-list's shape; not a real grant",
     "2026-09-12"),
)

# was never in any noun list because it does not exist. The third is true and must pass.
PLANTED = (
    # The two shapes that defeated the rules before this one. Both are caught now for the
    # same reason: the search is for a name, and a name has no grammar to hide behind.
    ("the retired plane, wrapped over two lines",
     "Stopping the job stops only this one. Nothing is lost: the change is in the\n"
     "repository and ArgoCD applies it on its next sync, so the deploy will be live.",
     True),
    ("the retired plane in lower case inside a command",
     "    kubectl -n argocd get applications  # it will reconcile your change for you",
     True),
    ("the retired plane in a fenced block",
     '```json\n{"applier": "ArgoCD", "note": "picks up the bump on its next sync"}\n```',
     True),
    ("the retired plane in bold",
     "After the bump, **ArgoCD** takes it from there and the deploy goes live.",
     True),
    # The name is a registry of spellings, not a string: the one normalisation above
    # covers each of these, and each is proven caught here.
    ("the retired plane with a space (the vendor's own spelling)",
     "Ask Argo CD to reconcile the bump and the deploy goes live.",
     True),
    ("the retired plane hyphenated",
     "Ask Argo-CD to reconcile the bump and the deploy goes live.",
     True),
    ("the retired plane in upper case",
     "Ask ARGOCD to reconcile the bump and the deploy goes live.",
     True),
    ("the retired plane with a double space",
     "Ask Argo  CD to reconcile the bump and the deploy goes live.",
     True),
    # The shape this rule does NOT catch, planted here on purpose so the gap is a row a
    # reader can see rather than a sentence in a docstring. If a later change makes this
    # catchable, this row is the first thing to flip.
    ("an applier that is not named at all (the known gap)",
     "You do not need to do anything else after the bump: the platform picks the change\n"
     "up on its next pass and your deploy takes effect with nothing further from you.",
     False),
    # Controls. A rule repaired into objecting to everything fails in these rows.
    ("a true sentence from the shipped skill (control)",
     "A `skipped` `apply` step instead refuses the job with nothing applied: its `detail`\n"
     "names the switch and the pair list.",
     False),
    ("a paragraph about shipping that claims no apply at all (control)",
     "Pick the component you want to ship and read its current tag first, so you can tell\n"
     "afterwards whether anything moved.",
     False),
    ("a sentence naming the plane that a person allowed (control)",
     "Before orbitd replaces ArgoCD as the applier, it rendered the objects itself.",
     False),
    # The allow-list keys on a sentence, not a line: the granted sentence in a document
    # it was not granted for must object, and so must the granted sentence with a real
    # promise appended to it.
    ("the granted sentence in a document it was not granted for",
     "Before orbitd replaces ArgoCD as the applier, it rendered the objects itself.",
     True),
    ("the allowed sentence with a false promise appended",
     "Before orbitd replaces ArgoCD as the applier, and ArgoCD still applies every change you ship.",
     True),
)


def _repository_root() -> pathlib.Path:
    """The tree the release ships from. An earlier version of this walk pointed one level
    too shallow, read no documents at all, and passed every case on an empty sequence --
    including the case that existed to stop exactly that. The assertions in the walks below
    keep that shape: the walk must reach real documents before any rule runs."""
    root = pathlib.Path(deploy.__file__).parents[3]
    assert (root / "docs" / "ORBITD.md").is_file(), (
        f"the walk does not reach the shipped tree from {root}, so every case in this "
        "file would pass on nothing")
    return root


def _lines(label: str, text: str):
    for number, line in enumerate(text.splitlines(), 1):
        yield label, number, line


def _disk_routes() -> list[tuple[str, list[tuple[str, int, str]]]]:
    """Every document this release ships, read off disk: each skill file anywhere under
    the root, found by walking the tree, and the reference document.

    The walk names no directory. An earlier version globbed one skills directory and
    missed the second tree entirely while the suite stayed green; a third tree appearing
    is covered the day it appears, without anyone editing this rule. Each skill file
    carries its top-level tree as its route, so losing a whole tree goes red in the
    per-route count instead of hiding inside a total.

    Line by line, not by paragraph. The rule below is a literal search for a name, and a
    name does not straddle a line break the way a subject and its verb do."""
    root = _repository_root()
    found = sorted(root.rglob("SKILL.md"))
    assert found, (
        f"the walk found no skill file at all under {root}, so the rule below would "
        "pass on nothing")
    by_tree: dict[str, list[pathlib.Path]] = {}
    for path in found:
        by_tree.setdefault(path.relative_to(root).parts[0], []).append(path)
    routes = []
    for tree in sorted(by_tree):
        lines = [item for path in by_tree[tree]
                 for item in _lines(str(path.relative_to(root)), path.read_text())]
        routes.append((f"skills:{tree}", lines))
    routes.append(("reference", list(_lines("ORBITD.md", (root / "docs" / "ORBITD.md").read_text()))))
    return routes


def _disk_strings():
    for route, lines in _disk_routes():
        for label, number, line in lines:
            yield route, label, number, line


async def _surface_routes(state, http) -> list[tuple[str, list[tuple[str, int, str]]]]:
    """Every string the running surface hands a caller: the instructions string from the
    handshake, which is the first string every caller reads before any tool; each
    resource description from the resource listing; and each tool description from the
    tool listing, driven at the highest role.

    The tool listing is role-gated: a lesser token is shown fewer tools, and the ones
    that vanish are the ones whose prose is most about who applies a change. A rule
    driven at a lesser role is measuring the role gate, not the documents, so this
    drives at admin, and the per-route count holds that its reconcile and platform-diff
    verbs are present by name. Like the disk walk, every route is returned even when it
    yields nothing, so emptying any one of them goes red instead of passing quietly."""
    routes: list[tuple[str, list[tuple[str, int, str]]]] = []
    async with sdk_client(http, mint(state, Role.admin)) as client:
        routes.append(("instructions", list(_lines("instructions", client.instructions or ""))))
        listed = await client.list_resources()
        routes.append(("resources",
                       [item for one in listed.resources
                        for item in _lines(f"resource {one.uri}", one.description or "")]))
        templates = await client.list_resource_templates()
        routes.append(("resource-templates",
                       [item for one in templates.resource_templates
                        for item in _lines(f"resource-template {one.uri_template}",
                                           one.description or "")]))
        found = await client.list_tools()
        routes.append(("tools",
                       [item for one in getattr(found, "tools", found)
                        for item in _lines(f"tool {one.name}", one.description or "")]))
    return routes


async def _surface_strings(state, http):
    for route, lines in await _surface_routes(state, http):
        for label, number, line in lines:
            yield route, label, number, line


def _offences(where: str, number: int, line: str) -> str | None:
    """One line of one shipped string, against the one registry spelling. The line is
    normalised first, so every spelling of the name reduces to the one literal string
    the search looks for; what falls out is case-insensitivity, because a reader looks
    for the name and not its capitalisation.

    The allow-list subtracts the granted sentence and searches the remainder, so a real
    promise appended to a granted sentence is still caught: a row that silenced its
    whole line would make that promise invisible."""
    remainder = line
    for place, quoted, _why, _when in ALLOWED:
        if place == where and quoted in line:
            remainder = remainder.replace(quoted, "")
    if A_RETIRED_PLANE in _normalise(remainder):
        return f"{where}:{number}: {line.strip()[:120]}"
    return None


async def _ship(state, http):
    """The result a caller is handed, and the id of the job record kept beside it."""
    async with sdk_client(http, mint(state, Role.operator), confirm=True) as client:
        running = await client.call_tool("orbit_deploy_ship", {
            "project": PROJECT, "env": ENV, "components": [WORKER], "tag": TAG,
        })
        return await wait_result(client, running)


def _outcome_words(result, into: set) -> None:
    """The words the ship uses to say HOW A THING ENDED -- its statuses, and nothing else.

    Two earlier drafts of this measurement were wrong in instructive ways. Taking every word
    the payload carried swept in `is`, `it`, `on` and `not`, so every planted lie anchored to
    one of them. Adding the verdict swept in `deployed`, which is one of the very verbs a
    false promise is written with, so the rule anchored lies to their own wording. A step's
    NAME is no better: a paragraph may mention the bump step truthfully and still lie about
    what happens after it.

    What is left is the only thing that answers "how did it end": the job's status, each
    step's status, and the stage recorded against each component."""
    if not isinstance(result, dict):
        return
    for key in ("status", "stage"):
        if isinstance(result.get(key), str):
            into.add(result[key].lower())
    evidence = result.get("evidence")
    if isinstance(evidence, dict):
        for component in evidence.get("components") or []:
            _outcome_words(component, into)
    for step in result.get("steps") or []:
        if isinstance(step, dict) and isinstance(step.get("status"), str):
            into.add(step["status"].lower())


def _anchored(claim: str, anchors: set) -> bool:
    """Whether a paragraph ties its claim to an outcome the ship really produces. Whole
    words only: the draft that matched substrings read the `not` inside `Nothing`."""
    said = set(re.findall(r"[a-z_][a-z0-9_]*", claim.lower()))
    return bool(said & anchors)


@pytest.fixture
async def anchors(fake_backends, http, monkeypatch):
    """Every word a caller is actually handed by a ship, over all three routes: the switch
    off, the pair not enabled, and an apply that really happens. This is the measurement the
    document rule compares against, so it is taken from the running surface and never
    written down here."""
    words: set[str] = set()

    install(fake_backends)
    seed_world(fake_backends)
    set_mode(fake_backends, "off")
    _outcome_words((await _ship(fake_backends, http))[0], words)

    install(fake_backends)
    seed_world(fake_backends)
    set_mode(fake_backends, "on")
    set_pairs(fake_backends, "demo/dev")
    _outcome_words((await _ship(fake_backends, http))[0], words)

    install(fake_backends)
    seed_world(fake_backends)
    set_mode(fake_backends, "on")
    set_pairs(fake_backends, f"{PROJECT}/{ENV}")   # the drive above narrowed it
    use_render(monkeypatch, rendered())
    monkeypatch.setattr(deploy, "read_source", FakeSource(OLD, SHIPPED, stale=0))
    _outcome_words((await _ship(fake_backends, http))[0], words)
    return words


class TestTheMeasurementIsWorthComparingAgainst:
    async def test_the_ship_really_answers_in_words(self, anchors):
        assert len(anchors) >= 4, (
            "the drive collected almost nothing, so the rule below would object to every "
            f"document ever written: {sorted(anchors)}")

    async def test_it_carries_the_words_the_walk_turns_on(self, anchors):
        """Anti-vacuity from the other side: a measurement missing these would let a
        document say anything about them."""
        for word in ("applied", "refused", "skipped"):
            assert word in anchors, (
                f"a ship never once handed back {word!r}, so either the drives above stopped "
                f"reaching the surface or the walk changed: {sorted(anchors)[:40]}")


class TestNoDiskStringNamesAPlaneThisReleaseDoesNotRun:
    """One literal name over every document the release ships on disk. The ways the rules
    before this one were walked past -- a markdown line wrap, an applier that does not
    exist, a walk that read nothing, and a walk that read one skills tree while a second
    sat beside it -- are all answered by the same properties: the search knows no grammar,
    the walk names no directory, and every route is counted before the rule runs."""

    def test_no_disk_document_names_it(self):
        offenders = [found for _route, label, number, line in _disk_strings()
                     for found in [_offences(label, number, line)] if found]
        assert not offenders, (
            "a shipped document names the plane this release retired, so a reader is told "
            f"something else will act on their change: {offenders}")

    def test_every_route_on_disk_yielded_something(self):
        """Anti-vacuity per route, not in total: asserting one total over hundreds of
        strings hides the loss of a route completely, which is how the second skills tree
        was missed. Each route below prints its line count, which is also how a reader of
        this file prints the routes the rule reads."""
        routes = _disk_routes()
        print("disk routes and their line counts: "
              + ", ".join(f"{route}: {len(lines)}" for route, lines in routes))
        for route, lines in routes:
            assert lines, (
                f"the {route} route yielded nothing, so the rule is measuring the walk "
                "instead of the documents")
        trees = {label.split("/", 1)[0] for _route, lines in routes for label, _, _ in lines
                 if label.endswith("SKILL.md") and "/" in label}
        assert len(trees) >= 2, (
            f"the walk spans only {sorted(trees)}, so a second tree of the same documents "
            "sitting beside it would go unread")
        total = sum(len(lines) for _, lines in routes)
        assert total > 500, (
            f"the walk found {total} lines in total, which is too few to be the shipped "
            "surface")

    def test_every_exception_a_person_granted_still_matches(self):
        """An allow-list row matching nothing means the sentence it excused has changed and
        nobody re-decided it."""
        lines = {(label, line) for _route, label, _, line in _disk_strings()}
        stale = [f"{place}: {quoted!r} ({why}, {when})"
                 for place, quoted, why, when in ALLOWED
                 if not any(place == label and quoted in line for label, line in lines)]
        assert not stale, (
            "a sentence a person decided was a legitimate exception is no longer in the "
            f"document, so the exception is unexamined: {stale}")


class TestNoSurfaceStringNamesAPlaneThisReleaseDoesNotRun:
    """One literal name over every string the running surface hands a caller. An earlier
    rule read none of these, and a false promise planted in a tool description left the
    whole suite green; the per-route count below keeps that from recurring."""

    async def test_no_surface_string_names_it(self, fake_backends, http):
        offenders = [found async for _route, label, number, line
                     in _surface_strings(fake_backends, http)
                     for found in [_offences(label, number, line)] if found]
        assert not offenders, (
            "a string the running surface hands a caller names the plane this release "
            f"retired: {offenders}")

    async def test_every_route_on_the_surface_yielded_something(self, fake_backends, http):
        """Anti-vacuity per route, not in total: every route is returned even when it
        yields nothing, so emptying any one of them goes red here. Each route prints its
        line count, which is also how a reader of this file prints the routes the rule
        reads. The listing is role-gated, so this also holds that the tool route was
        driven at the role that hides nothing."""
        routes = await _surface_routes(fake_backends, http)
        print("surface routes and their line counts: "
              + ", ".join(f"{route}: {len(lines)}" for route, lines in routes))
        for route, lines in routes:
            assert lines, (
                f"the {route} route yielded nothing, so the rule is measuring the walk "
                "instead of the surface")
        labels = [label for _route, lines in routes for label, _, _ in lines]
        for verb in ("tool orbit_deploy_reconcile", "tool orbit_platform_diff"):
            assert verb in labels, (
                f"{verb} is not in the tool listing, so the listing was driven at a role "
                "that hides the verbs most about who applies a change")


class TestARestartThatReplacedThePodsDoesNotReportOtherwise:
    """The repair's mirror. This route writes the pod template before the apply step is
    reached, and that write is the restart."""

    def test_the_pods_really_were_replaced(self, fake_backends):
        """Anti-vacuity: if this route wrote nothing, the case below would be asking about
        a restart that did not happen."""
        install(fake_backends)
        kube = seed_world(fake_backends)
        set_mode(fake_backends, "off")
        restart(fake_backends)
        assert kube.patches, (
            "the restart wrote no pod-template patch at all, so this file is testing the "
            "wrong route")

    def test_it_is_not_reported_as_having_done_nothing(self, fake_backends):
        install(fake_backends)
        kube = seed_world(fake_backends)
        set_mode(fake_backends, "off")
        result = restart(fake_backends)
        assert kube.patches, "the restart wrote no patch, so this case measures nothing"
        assert result.status != "refused", (
            "the restart replaced the workload's pods and the operator is told the job was "
            "refused with nothing applied, which is the defect this node exists to repair "
            f"pointing the other way: {result.status} / {getattr(result, 'evidence', None)}")

    def test_a_reader_can_still_see_the_apply_was_skipped(self, fake_backends):
        """The other half: reporting the restart honestly must not hide that the apply step
        did not run."""
        install(fake_backends)
        seed_world(fake_backends)
        set_mode(fake_backends, "off")
        result = restart(fake_backends)
        steps = {s.name: s.status for s in result.steps}
        assert steps.get("apply") == "skipped", (
            "a reader cannot tell from the result that the apply step did not run: "
            + repr(steps))
        assert steps.get("restart") == "ok", (
            "a reader cannot tell from the result that the pods were replaced: "
            + repr(steps))


class TestAJobThatSucceededIsRecordedAsSucceeded:
    """The pin that was flipped rather than replaced. Recording every deploy job as failed
    left the whole suite green, so nothing held the ordinary case at all."""

    async def test_a_ship_that_applied_is_recorded_completed(self, fake_backends, http,
                                                             monkeypatch):
        install(fake_backends)
        seed_world(fake_backends)
        set_mode(fake_backends, "on")
        use_render(monkeypatch, rendered())
        monkeypatch.setattr(deploy, "read_source", FakeSource(OLD, SHIPPED, stale=0))
        result, job_id = await _ship(fake_backends, http)
        assert result["status"] == "applied", (
            "this case needs a ship that really applied: " + repr(result)[:200])
        job = fake_backends.jobs.get(job_id)
        assert job is not None, "no job record was kept for a ship that ran to the end"
        assert job.status == "completed", (
            "a deploy job that applied the change is recorded as something other than "
            "completed, so the job log disagrees with the answer the caller was handed: "
            + repr(job.status))


class TestTheRuleCatchesWhatTheLastOneLetThrough:
    """Each row is a claim planted into a line of a document, with whether the rule must
    object. The controls are in the same rows, so a rule repaired into objecting to
    everything fails here too -- including the allow-listed sentence, which must still be
    let through by the exception a person granted and by nothing else."""

    @staticmethod
    def _judge(where: str, claim: str) -> bool:
        return any(_offences(where, number, line)
                   for number, line in enumerate(claim.splitlines(), 1))

    @pytest.mark.parametrize("how,claim,must_object", PLANTED,
                             ids=[row[0] for row in PLANTED])
    def test_the_rule_sees_this_shape(self, how, claim, must_object, monkeypatch):
        if "allowed" in how or "granted" in how:
            monkeypatch.setattr(sys.modules[__name__], "ALLOWED", ALLOWED + A_SYNTHETIC_GRANT)
        where = "ORBITD.md" if "allowed" in how else "orbit-ship"
        objected = self._judge(where, claim)
        if must_object:
            assert objected, (
                f"a false promise written as {how} is invisible to this rule, so anyone "
                "writing that way can tell a reader something else will act on their "
                "change: " + claim)
        else:
            assert not objected, (
                f"the rule objects to a line it must not ({how}), so it would refuse "
                "correct documents too: " + claim)

    async def test_the_shipped_surface_passes_it(self, fake_backends, http):
        """Anti-vacuity for every row above: the shipped strings as they stand must be
        quiet, or the rows are measuring something other than the claim."""
        offenders = [found for _route, label, number, line in _disk_strings()
                     for found in [_offences(label, number, line)] if found]
        offenders += [found async for _route, label, number, line
                      in _surface_strings(fake_backends, http)
                      for found in [_offences(label, number, line)] if found]
        assert not offenders, (
            "the shipped strings do not satisfy their own rule: " + repr(offenders))

    def test_the_gap_is_a_gap_and_not_a_secret(self):
        """The one row above marked as not caught is the whole of what this rule gives up.
        It is asserted here so that a reader of this file cannot miss it, and so that a
        later change that closes it fails here and has to say so."""
        unnamed = [claim for how, claim, must in PLANTED if "known gap" in how]
        assert unnamed, "the gap row was removed without anything replacing it"
        assert not self._judge("orbit-ship", unnamed[0]), (
            "the rule now catches a promise that names nobody, which is more than it claims "
            "to do -- say so in the docstring, name the new route in the enumeration only, "
            "and move this row to the caught side")
