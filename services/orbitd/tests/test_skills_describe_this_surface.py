"""What the skills tell a reader a refusal looks like, against what the surface answers.

Three reviews in a row found the contract for this blind, each time differently, and each
time because it compared the skills against the shape of the tree: a helper's name, then a
constant's spelling. Every claim that actually went stale was invisible to those patterns --
a status number, a header, two lowercase words -- and restoring the whole of the stale text
left the suite green.

So this one does not read the tree. It drives the surface into each refusal, writes down what
a caller is really handed, and holds the skills to that. A word a skill puts in a reader's
hands has to be a word the surface produces; a status it names has to be one the surface
answers with. Nothing here knows what the right answers are in advance, which is the point:
when the surface changes, the measurement changes with it and the skills are held to the new
one.

A fourth review found the comparison blind again, and for the last reason left: lifting a
claim out of prose. Whatever pattern did the lifting was always one spelling short -- a code
in bold rather than backticks, a status inside a fenced block, a true sentence that never
said the word "refused". So nothing is lifted any more. The direction is inverted: from the
measurement we derive the words this surface never says -- the statuses it does not answer
and the code-shaped words it does not produce -- and search every line of every document for
those, literally, with no notion of backticks, paragraphs or keywords. A literal search has
no spelling to walk past. The legitimate exception, a sentence that truthfully names
something this surface does not produce, goes in a dated allow-list below, which is a
decision a person makes once and a later reader can audit.

A fifth review found the vocabulary vouching for the documents. Two thirds of the measured
words came from what the surface says about itself -- every tool description, title and
schema sentence the listing hands a caller -- so a document vouched for itself: an error
name planted in a tool's field description and in a shipped skill left the suite green, and
removing only the tool-description half turned it red. The line that matters is not between
the surface and the documents. It is between what the surface answers when it is driven --
a status, an error code, a header value, a field of a body -- and what a caller reads
rather than receives, which is a document like any other and is searched by the same rule
below. The same review found the word collector dropping every number, so a JSON-RPC code
-- which arrives as a number -- never reached the vocabulary except where a helper added
one by hand; numbers are words now, and every code the surface sends arrives by the same
road as every other word.

A sixth review found the walk itself blind. This file read the skills under one tree and one
document under `docs/`; a second tracked tree holds six more skills, `docs/` holds eighteen
documents, and a sibling contract was making the same mistake in its own copy of the same
walk. The inventory is no longer here: it lives in one module beside this file, is derived
from what git tracks, and every contract that judges documents takes its list from there and
says which kinds it judges. This one judges every shipped skill in every tree, plus every
document under `docs/` that names a tool of this surface -- a document that never names one
is a design note, which promises a caller nothing.

Widening the walk split one question into two. A word is a word in any document, so the word
half and the protocol-code half run over the whole inventory. A three-digit number is only a
status where the document is talking about a call to THIS surface: in a document about a
tenant's own application it is a base image's user id, a rolling update answering 502, an
address, a pull request number. Measured over the full inventory that half produced
thirty-one lines and every one was wrong, so the status half and the invented-header half
ask only the documents whose subject is this surface's protocol. That scope is a decision, it
is recorded once in the inventory module with its guards, and the note there says what was
measured before it was accepted.
"""

from __future__ import annotations

import pathlib
import re
from http import HTTPStatus
from typing import NamedTuple

import pytest
from conftest import PROTOCOL_REVISION, mint, rpc, sdk_client

from orbitd import auth
from orbitd.roles import Role

from test_tools_viewer_kube import DEMO, seed_catalog

pytestmark = pytest.mark.anyio


# Each way a call can be refused, and how to provoke it. The vocabulary these produce is the
# whole of what a skill is allowed to promise about refusals.
# A value carried in more than one place is only measured if the drive sends the agreeing shape as well as each disagreement: the transport drive below sends the method, name and version mirrors disagreeing in both directions, both version carriers agreeing on an unknown version, and the capabilities key missing.
REFUSALS = (
    ("a name no tool answers to", Role.viewer, "orbit_not_a_tool_at_all", {}),
    ("a name of no shape at all", Role.viewer, "definitely_not_orbit", {}),
    ("a call above the caller's role", Role.viewer, "orbit_auth_revoke",
     {"jti": "Q1w2E3r4T5y6U7i8"}),
    ("a required argument left out", Role.viewer, "orbit_deploy_status", {}),
)

# Every status the HTTP registry defines. The ones this surface really answers are measured
# by driving it; everything else in here is a status a document could promise a reader and
# that the reader would then wait for for ever.
EVERY_STATUS = {status.value for status in HTTPStatus}

# The error names a reader of an OAuth-protected tool surface would plausibly be handed:
# RFC 6749 section 5.2, RFC 6750 section 3.1, and the two a JSON-RPC caller reaches for.
# This is not a list of what we produce. Whichever of these the surface really says is
# subtracted by the measurement below; what survives the subtraction is the set no
# document may put in a reader's hands.
PLAUSIBLE_CODES = (
    "invalid_request", "invalid_client", "invalid_grant", "unauthorized_client",
    "unsupported_grant_type", "invalid_scope", "insufficient_scope", "invalid_token",
    "access_denied", "unsupported_response_type", "server_error",
    "temporarily_unavailable", "method_not_found", "invalid_params",
)

# WHAT THIS CONTRACT DOES NOT HOLD, in full, because a review found the list above was being
# read as though it held identifiers in general. It holds exactly the names in it, minus what
# the surface says. Five classes of false claim walk past, and each is here with its reason:
#
#  1. An invented code outside the list -- `orbit_forbidden`. There is no registry of names
#     nobody has thought of, so there is nothing to subtract from. This is the shape of the
#     limit, not an oversight: subtraction can only hold what can be enumerated.
#  2. An invented code that reads like a real one -- `token_expired`. Same reason as (1), and
#     worse, because it is the spelling a reader would most readily believe.
#  3. An ordinary English outcome word -- a document promising a reader "denied". A literal
#     search cannot tell that promise from the true sentence "the retry is rejected as a
#     protocol error", so holding it would mean objecting to true prose, which is how three
#     earlier rounds of this contract were lost.
#  4. The right status named for the wrong call -- "a tool name no tool answers to is refused
#     with a 401". The surface answers that one inside the protocol, but 401 IS a status this
#     surface answers, so subtraction cannot see it. This is the honest limit of the inverted
#     design: subtraction throws away WHICH call produced which status, and keeping that would
#     mean reading the prose to find out which call a sentence is about.
#  5. A header invented without the `X-` prefix -- `Orbit-Scope`. It reads exactly like a
#     standard header to a literal search, and telling them apart means understanding which
#     side of the call the sentence is about.
#
#  6. A status the surface never answers, named IN DIGITS inside the surface's OWN prose --
#     a tool description, the instructions string, a resource description. Those documents
#     are searched for every code-shaped word and for the protocol codes, but not for bare
#     three-digit runs, because three digits are ordinary there in a way they are not in a
#     skill: a page size capped at 100, a list capped at 500, an example naming a status as
#     a search tag. When the surface's prose first joined the searched documents this half
#     objected to six true sentences at once, and the only way past it was to spell the
#     numbers out in words, which makes a description worse for the agent reading it. A rule
#     that bends true prose is the failure this file was rewritten twice to escape, so the
#     status half stops at the shipped documents, where a status IS claimed as an answer a
#     reader should wait for.
#
# Release-checklist step 3 names what a person reads instead. A sixth class was in this list
# until a review found it: an invented JSON-RPC numeric code. That one turned out to have a
# registry after all -- the reserved range -- so it is held now rather than named here, which
# is the right outcome whenever a gap turns out to be enumerable.

# A status, read the way the inversion reads everything else. Not a pattern with lookarounds
# around it -- that was the half of this contract that never got inverted, and a review found it
# blind four ways at once: the lookahead that stopped `v1.503` reading as a status also stopped
# `503.` at the end of a sentence, because a full stop is the same character.
#
# So the document is read as runs of digits, and a run is a status claim when the WHOLE run is
# one. `3339` is a run of four digits whose value is not a status; `256` in `SHA-256` is a run
# whose value is not a status; `503` ending a sentence, glued to a word in `HTTP503`, or
# standing alone, is a run whose value is one. Nothing is looked around, so there is no
# neighbouring character for a claim to hide behind.
A_RUN_OF_DIGITS = re.compile(r"[0-9]+")

# The other registry of numbers a reader can be handed, and one a review found nothing looking
# for: JSON-RPC reserves -32768 to -32000 for protocol errors, so a number in that range is a
# code a caller would match against what arrives. Subtract what the surface really produces and
# the rest is a set no document may promise. Read with its sign, because that is what makes it
# a protocol code rather than a quantity.
A_SIGNED_NUMBER = re.compile(r"-[0-9]+")
RESERVED_PROTOCOL_CODES = frozenset(range(-32768, -31999))

# A code, likewise. A reader looks for a word, and the same word is written with an underscore,
# a hyphen, a space, or across a line wrap; the contract this one replaced held the hyphenated
# spelling and losing it was a regression. So every way of writing a separator is flattened to
# one before the search, and each line is asked both alone and joined to the line after it,
# which is where a wrap puts the second half.
A_SEPARATOR = re.compile(r"[-_\s]+")

# A header name a document invents. Only the `X-` form is held, and that is the whole claim:
# by convention an `X-` name is never a standard header, so one named in a document is one
# this surface is said to send, and the measurement knows every header it really sends. A
# One segment or many -- `X-Scope` is as much an invention as `X-Orbit-Scope`, and requiring
# two segments was a hole a review found. A
# header invented without the prefix (`Orbit-Scope`) reads exactly like a standard one to a
# literal search, and telling them apart means understanding which side of the call the
# sentence is about, which is the prose-reading this contract exists to stop doing.
AN_INVENTED_HEADER = re.compile(r"\bX-[A-Za-z]+(?:-[A-Za-z]+)*\b")

# Sentences a person decided, on a date, are true although they name something this surface
# does not produce -- a historical note about a status we once answered, say. Each row is
# (document, the exact text that may appear on a line, why, when it was decided). A row
# that no longer matches anything is a defect of its own: the sentence changed and nobody
# re-decided it. Empty today, on purpose: the shipped documents need no exception.
# This is the one hand-operated lever in an otherwise driven contract, so it carries its own
# guards, and a review found it needed all three. A row names one document, never all of them.
# A row must be long enough to be a sentence rather than a character, because a one-character
# row would silence every line containing that character. And a row that excuses nothing is
# refused, because a row matching no line is either a sentence that changed or a lever somebody
# reached for and did not aim.
ALLOW_ROW_MIN = 24

ALLOWED: tuple[tuple[str, str, str, str], ...] = ()


def _the_protocol_documents(surface):
    """The documents whose subject is this surface's protocol, from the same inventory. See
    the scope's own note there for why the subject of a document is a decision and not a
    derivation, and what was measured before that was accepted."""
    import shipped_documents

    return shipped_documents.documents_whose_subject_is_the_protocol(_tool_names(surface))


def _tool_names(surface) -> list:
    """The names of the tools the surface listed, taken off the driven measurement rather
    than off a list here."""
    return [label[len("tool "):] for label, _ in surface if label.startswith("tool ")]


def _documents(surface):
    """What this contract judges: every shipped skill, in every tree, plus every document
    under `docs/` that names a tool of this surface.

    The inventory is not walked here. It comes from the one module that answers what this
    repository ships as a document, because two contracts each walking their own path is
    how both of them came to read one skill tree of the two that exist. What this file
    chooses is the KIND, and the choice is this: a document that never names a tool is a
    design note or a plan, which promises a caller nothing and is nobody's instruction; a
    document that does name one is telling a reader what to call and what comes back, which
    is exactly the claim this contract holds. Whether a document is judged therefore follows
    what the surface offers, and a design note starts being judged the day it starts telling
    a reader to call something.

    No paragraph is skipped and no claim is lifted out of the prose: the search is literal,
    so there is nothing for a spelling to walk past and nothing for a gate to leave behind.
    The surface's own prose is not here; it is driven, so it rides with the measurement."""
    import shipped_documents

    return (shipped_documents.skill_documents()
            + shipped_documents.documents_that_name_a_tool(_tool_names(surface)))


# What a caller reads rather than receives. A description, a title and the instructions
# string are read; a name, a key, a pattern, an enum member, a bound or a default is
# received -- a caller sends it back or matches against it. The first half joins the
# searched documents; the second half stays vocabulary.
# What a caller reads rather than receives, and the prose helpers that collect it, live in
# the inventory module beside this file: the sibling contract needs the same three, and two
# copies of a walk is the defect this move exists to close.
from shipped_documents import PROSE_KEYS, tool_prose as _tool_prose  # noqa: E402,F401
from shipped_documents import surface_documents as _surface_documents_of  # noqa: E402,F401



def _words_of_listing(payload, words: set) -> None:
    """The listing half of the vocabulary: everything a listing answer carries except
    what a caller reads. Description, title and instructions strings are left out --
    they join the searched documents above instead -- while names, keys, patterns,
    enums, bounds, defaults, mime types and numbers stay, because those are what the
    surface answers with."""
    if isinstance(payload, bool):
        return
    if isinstance(payload, dict):
        for key, value in payload.items():
            words.add(str(key))
            if key in PROSE_KEYS and isinstance(value, str):
                continue
            _words_of_listing(value, words)
    elif isinstance(payload, (list, tuple)):
        for value in payload:
            _words_of_listing(value, words)
    else:
        _words_of(payload, words)


def _the_words_this_surface_never_says(statuses: set, words: set):
    """The whole of what a document may not promise, derived by subtraction from what the
    surface was driven into actually producing."""
    lowered = {word.lower() for word in words}
    return (EVERY_STATUS - statuses,
            {code for code in PLAUSIBLE_CODES if code not in lowered})


def _the_protocol_codes_it_never_sends(words: set) -> frozenset:
    """The reserved JSON-RPC numbers this surface does not answer with. Whatever it really
    sends appears in the measured vocabulary as a number written out, so subtracting those
    leaves the codes a document would be inventing."""
    said = set()
    for word in words:
        try:
            said.add(int(str(word)))
        except (TypeError, ValueError):
            continue
    return frozenset(RESERVED_PROTOCOL_CODES - said)


def _offences(label: str, text: str, unanswered: set, unsaid: set,
              known_headers: frozenset | None = None,
              unsent_protocol_codes: frozenset = frozenset()):
    """Every line of one document that hands a reader something the surface never gives back.

    A line, not a paragraph: a fenced block, a bold span, an indented sample and a bare
    sentence are all just lines. But a line and the one after it as well, because a line wrap
    is where a code gets broken in half, and the rule this replaced could not put it back
    together."""
    exemptions = [row[1] for row in ALLOWED
                  if row[0] == label and len(row[1]) >= ALLOW_ROW_MIN]
    lines = text.splitlines()
    found = []
    for number, line in enumerate(lines, 1):
        if any(exempt in line for exempt in exemptions):
            continue
        for run in A_RUN_OF_DIGITS.findall(line):
            # The whole run, never part of one: 3339 is a date format, 503 is a status.
            if len(run) == 3 and int(run) in unanswered:
                found.append(f"{label}:{number} names status {run}: {line.strip()[:90]}")
        for signed in A_SIGNED_NUMBER.findall(line):
            if int(signed) in unsent_protocol_codes:
                found.append(f"{label}:{number} names the protocol code {signed}: "
                             f"{line.strip()[:90]}")
        # The line, and the line joined to the one after it, with every way of writing a
        # separator flattened to the one the surface uses.
        wrapped = line if number >= len(lines) else line + " " + lines[number]
        flattened = A_SEPARATOR.sub("_", wrapped.lower())
        for code in sorted(unsaid):
            if code in flattened:
                found.append(f"{label}:{number} hands a reader {code!r}: {line.strip()[:90]}")
        # The header half is asked where the caller passes the headers this surface really
        # sends -- which is where the subject is this surface. Passing nothing leaves it
        # unasked, rather than asking it against an empty set and calling every header in
        # the document invented.
        if known_headers is not None:
            for header in AN_INVENTED_HEADER.findall(line):
                if header.lower() not in known_headers:
                    found.append(f"{label}:{number} names the header {header}: "
                                 f"{line.strip()[:90]}")
    return found


def _words_of(value, into: set) -> None:
    """Every key and every string and number value the surface produced, however deep.

    Numbers are words too: a JSON-RPC error code arrives as a number, and the reserved
    range below is subtracted from what the surface really sent, so a code that never
    reaches this set reads as never sent. Every code the surface sends arrives by this
    road, the same as every other word -- no helper adds one by hand. Booleans are not
    words: nobody looks for True in what the surface answers."""
    if isinstance(value, bool):
        return
    if isinstance(value, dict):
        for key, inner in value.items():
            into.add(str(key))
            _words_of(inner, into)
    elif isinstance(value, (list, tuple)):
        for inner in value:
            _words_of(inner, into)
    elif isinstance(value, (int, float)):
        into.add(str(value))
    elif isinstance(value, str):
        into.add(value)
        for part in re.findall(r"[A-Za-z][A-Za-z0-9_]*", value):
            into.add(part)


async def _a_backend_that_cannot_be_reached(state, http, words):
    """The fifth way a call fails, and the one no refusal produces: the surface reached for
    a backend and could not. Its vocabulary is part of what a skill may promise."""
    seed_catalog(state, [DEMO])

    class MaxRetryError(Exception):
        pass

    def boom(*args, **kwargs):
        raise MaxRetryError("connection refused")

    state.backends.kube.core.list_namespaced_pod = boom
    async with http(mint(state, Role.viewer)) as h:
        r = await rpc(h, "tools/call", {"name": "orbit_workload_list",
                                        "arguments": {"project": "demo", "env": "prod"}})
    body = r.json()
    assert body.get("result", {}).get("isError"), (
        "the unreachable backend did not fail, so its words are not being measured: " + str(body))
    _words_of(body, words)


def _watch_the_stages_as_they_are_set(monkeypatch, words: set) -> None:
    """Some of this surface's vocabulary is never in a finished record.

    A component that has rolled out is marked so, and the assertion step immediately marks
    it again -- asserted, or failed. So `rolled_out` is a stage this surface really sets and
    a reader really sees while watching a ship, and no completed answer carries it. Reading
    it out of a settled payload is impossible; polling a running job for it would make every
    case in this file depend on a race.

    So the stages are recorded as they are set, which is the surface producing them rather
    than a list of words kept here. Anything the drives below never set is still absent."""
    from orbitd import deploy

    real = deploy._set_stage

    def watched(evidence, name, stage, *args, **kwargs):
        words.add(str(stage).lower())
        return real(evidence, name, stage, *args, **kwargs)

    monkeypatch.setattr(deploy, "_set_stage", watched)


async def _a_mutation_that_runs_to_the_end(state, http, monkeypatch, words: set) -> None:
    """A ship, driven to its last step. Its steps have statuses of their own -- a step not
    reached, a rollout observed, an assertion made -- and a skill describing the walk names
    them. A measurement that only ever drove refusals called every one of those a lie.

    Both routes are driven: one where the apply is skipped and one where it really applies,
    because the steps that exist differ between them."""
    from test_apply_pairs import set_pairs
    from test_reconcile import (COMMIT as SHIPPED, ENV, OLD, PROJECT, FakeSource, rendered,
                                set_mode, use_render)
    from test_tools_operator import TAG, WORKER, install, seed_world
    from orbitd import deploy

    def applies_for_real():
        set_mode(state, "on")
        set_pairs(state, f"{PROJECT}/{ENV}")
        use_render(monkeypatch, rendered())
        monkeypatch.setattr(deploy, "read_source", FakeSource(OLD, SHIPPED, stale=0))

    for arrange in (lambda: set_mode(state, "off"), applies_for_real):
        install(state)
        seed_world(state)
        arrange()
        # Through a client that answers the confirmation, because a raw call stops at
        # the question and no job is ever created -- which is why an earlier draft of this
        # drive measured nothing at all.
        async with sdk_client(http, mint(state, Role.operator), confirm=True) as client:
            started = await client.call_tool("orbit_deploy_ship", {
                "project": PROJECT, "env": ENV, "components": [WORKER], "tag": TAG})
            _words_of(started.structured_content, words)
            job_id = _job_id_of(started.structured_content)
            if job_id is None:
                continue
            done = await client.call_tool("orbit_job_wait",
                                          {"job_id": job_id, "timeout_s": 5})
        _words_of(done.structured_content, words)


def _job_id_of(payload):
    """The job id, wherever the answer carries it."""
    found = []

    def walk(value):
        if isinstance(value, dict):
            if isinstance(value.get("job_id"), str):
                found.append(value["job_id"])
            for inner in value.values():
                walk(inner)
        elif isinstance(value, list):
            for inner in value:
                walk(inner)

    walk(payload)
    return found[0] if found else None


def _a_mutation_that_stops_partway(state, monkeypatch, words: set) -> None:
    """A ship of two components where the first fails. The component the ship never got to
    has a stage of its own, and the walk's own document names it. Driven through the deploy
    path rather than the protocol, because what is wanted is the record, not the answer."""
    from orbitd.deploy import Step, execute_ship
    from test_reconcile import (COMMIT, ENV, NS, OLD, PROJECT, CATALOG, FakeSource,
                                rendered, set_mode, use_render)
    from test_tools_operator import TAG, install, seed_world
    from orbitd import deploy

    set_mode(state, "on")
    install(state)
    kube = seed_world(state)
    use_render(monkeypatch, rendered())
    monkeypatch.setattr(deploy, "read_source", FakeSource(OLD, COMMIT, stale=0))
    kube.apply_errors[("ConfigMap", NS, "app-config")] = RuntimeError("the apply refused")
    stopped = execute_ship(
        state, project=PROJECT, env=ENV,
        components=[CATALOG["components"][1], CATALOG["components"][0]],
        tag=TAG, action="ship", actor="a-measurement", expect=None,
        opening=[Step(name="confirm", status="ok")])
    _words_of({"status": stopped.status, "evidence": stopped.evidence,
               "steps": [{"name": s.name, "status": s.status} for s in stopped.steps]},
              words)


def _the_names_of_the_documents_beside_this_one(words: set) -> None:
    """A skill pointing a reader at a sibling skill is naming something this release really
    ships, not promising a code the surface will send. The names come from the tree."""
    import shipped_documents

    for path in shipped_documents.skill_document_paths():
        words.add(path.parent.name)


async def _the_confirmation_round(state, http, words: set) -> None:
    """A mutating tool asks before it writes, and the exchange has a vocabulary of its own:
    the kind of answer that comes back, the fields it carries, the fields a caller sends to
    answer it, and the replies it may send. A skill explaining a confirmation names these,
    and a measurement that never asks for one calls every true sentence about them a lie.

    The field names are read off the types this surface answers and accepts, by their names
    on the wire, so widening the request later cannot leave this behind."""
    from mcp_types import CallToolRequestParams, ElicitResult, InputRequiredResult

    async with http(mint(state, Role.admin)) as h:
        asked = await rpc(h, "tools/call", {
            "name": "orbit_auth_issue",
            "arguments": {"name": "a-new-reader", "role": "viewer", "ttl": 3600}})
    _words_of(asked.json(), words)
    # The protocol's own names for this exchange, taken from the types this surface answers
    # with rather than written down here.
    for kind in (InputRequiredResult, ElicitResult, CallToolRequestParams):
        words.add(kind.__name__)
        fields = getattr(kind, "model_fields", None) or {}
        for field in fields:
            words.add(field)
            alias = getattr(fields[field], "alias", None)
            if alias:
                words.add(alias)
    for reply in ("accept", "decline", "cancel"):
        words.add(reply)


def _the_words_a_token_carries(state, words: set) -> None:
    """What a caller holds. The reference document walks a reader through the claims in the
    token it was issued, so those claim names are this surface's vocabulary too."""
    import jwt

    token = mint(state, Role.viewer)
    claims = jwt.decode(token, options={"verify_signature": False})
    _words_of(claims, words)
    for name in vars(state):
        words.add(name)


async def _the_transport_mirror_shapes(state, http, words: set, statuses: set) -> None:
    """The transport answers before the protocol wherever a value is carried in more than
    one place. A header that disagrees with the envelope is a mismatch whichever carrier
    is wrong; version carriers that agree on a version the surface does not know are an
    unsupported version instead; a missing capabilities key is invalid params. The
    disagreements alone can never produce the agreeing answer, so every shape is driven
    and whatever code each answer carries is recorded as the surface's own."""
    from mcp_types import CLIENT_CAPABILITIES_META_KEY, PROTOCOL_VERSION_META_KEY

    unknown = "1999-01-01"
    accept = {"Accept": "application/json, text/event-stream"}

    def headers(version=PROTOCOL_REVISION, method="tools/call", name="orbit_platform_health"):
        return {"MCP-Protocol-Version": version, "Mcp-Method": method,
                "Mcp-Name": name, **accept}

    def body(name="orbit_platform_health", version=PROTOCOL_REVISION,
             capabilities=None, drop_caps=False):
        meta = {PROTOCOL_VERSION_META_KEY: version}
        if not drop_caps:
            meta[CLIENT_CAPABILITIES_META_KEY] = {} if capabilities is None else capabilities
        return {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                "params": {"name": name, "arguments": {}, "_meta": meta}}

    shapes = (
        ("the method mirror disagrees",
         headers(method="tools/list"), body()),
        ("the name mirror disagrees",
         headers(), body(name="orbit_project_list")),
        ("the version carriers disagree, header unknown",
         headers(version=unknown), body()),
        ("the version carriers disagree, envelope unknown",
         headers(), body(version=unknown)),
        ("both version carriers agree on an unknown one",
         headers(version=unknown), body(version=unknown)),
        ("no clientCapabilities key at all",
         headers(), body(drop_caps=True)),
    )
    for _what, request_headers, request_body in shapes:
        async with http(mint(state, Role.viewer)) as h:
            r = await h.post("/mcp", json=request_body, headers=request_headers)
        statuses.add(r.status_code)
        payload = r.json()
        _words_of(payload, words)


class Measured(NamedTuple):
    """Everything driving the surface handed back: the statuses it answers refusals
    with, every word it puts in an answer, and the prose it says about itself.

    The contract beside this one calls the two checks below with only the fixture, so
    the prose rides along as the third element rather than through new fixtures the
    sibling would have to pass."""

    statuses: set
    words: set
    surface: list


async def _drive(state, http, monkeypatch) -> Measured:
    """Drive every refusal and record what a caller is handed: the statuses the surface
    answers a refusal with, and every word it puts in the answer -- including the
    discovery answers, whose field names the reference document promises a reader
    looking for where to authenticate. The surface's own prose is driven too, and
    comes back as searched documents rather than vocabulary."""
    statuses, words = set(), set()
    for _what, role, name, args in REFUSALS:
        token = mint(state, role)
        async with http(token) as h:
            r = await rpc(h, "tools/call", {"name": name, "arguments": args})
        statuses.add(r.status_code)
        _words_of(r.json(), words)
        # What a header *says* is handed to the caller as surely as the body is: the
        # challenge on a 401 is where a reader is told the error code to look for.
        for header, value in r.headers.items():
            words.add(header)
            _words_of(value, words)
    async with http("not-a-token-at-all") as h:
        r = await rpc(h, "tools/list")
    statuses.add(r.status_code)
    # What a header *says* is handed to the caller as surely as the body is: the
    # challenge on a 401 is where a reader is told the error code to look for.
    for header, value in r.headers.items():
        words.add(header)
        _words_of(value, words)
    # A caller in good standing: its vocabulary is the surface's too, and a skill may name a
    # field it sees on a successful call while explaining a refusal. The listing answers
    # are driven at the role that hides nothing -- an admin token returns every tool,
    # where an operator sees fewer and a viewer fewer still -- so no tool's answers are
    # missing from the vocabulary.
    async with http(mint(state, Role.admin)) as h:
        ok = await rpc(h, "tools/call",
                       {"name": "orbit_auth_list", "arguments": {}})
        listed = await rpc(h, "tools/list")
        discovered = await rpc(h, "server/discover")
        resources = await rpc(h, "resources/list")
        templates = await rpc(h, "resources/templates/list")
    _words_of(ok.json(), words)
    # The listing answers enter the vocabulary with their prose left out: a description,
    # a title, the instructions string and a resource description are what a caller
    # reads, not what it receives, so they join the searched documents instead. Folding
    # them in let a document vouch for itself.
    listed_body, discovered_body = listed.json(), discovered.json()
    resources_body, templates_body = resources.json(), templates.json()
    for payload in (listed_body, discovered_body, resources_body, templates_body):
        _words_of_listing(payload, words)
    surface = _surface_documents_of(listed_body, discovered_body,
                                    resources_body, templates_body)
    # The discovery documents need no token and name the fields (`token_endpoint`,
    # `jwks_uri`, the client-secret methods) the reference document walks a reader
    # through. They are produced by this surface, so they are measured like the rest.
    doc_paths = [auth.AS_METADATA_PATH, auth.JWKS_PATH, *auth.rs_metadata_paths(state.cfg)]
    async with http() as h:
        for doc_path in doc_paths:
            fetched = await h.get(doc_path)
            assert fetched.status_code == 200, (
                f"{doc_path} did not answer, so the discovery vocabulary is not being "
                f"measured: {fetched.status_code} {fetched.text[:200]}")
            _words_of(fetched.json(), words)
    _watch_the_stages_as_they_are_set(monkeypatch, words)
    await _the_transport_mirror_shapes(state, http, words, statuses)
    await _the_confirmation_round(state, http, words)
    await _a_mutation_that_runs_to_the_end(state, http, monkeypatch, words)
    _a_mutation_that_stops_partway(state, monkeypatch, words)
    _the_names_of_the_documents_beside_this_one(words)
    _the_words_a_token_carries(state, words)
    # Last, because it breaks the backend it reaches for.
    await _a_backend_that_cannot_be_reached(state, http, words)
    return Measured(statuses, words, surface)


@pytest.fixture
async def measured(fake_backends, http, monkeypatch):
    """The one driven measurement every check below compares against."""
    return await _drive(fake_backends, http, monkeypatch)


class TestTheMeasurementIsWorthComparingAgainst:
    """Anti-vacuity for everything below. A measurement that came back empty, or that
    swallowed the whole dictionary, would pass every skill no matter what it said."""

    async def test_the_refusals_really_refused(self, fake_backends, http):
        state = fake_backends
        for what, role, name, args in REFUSALS:
            token = mint(state, role)
            async with http(token) as h:
                r = await rpc(h, "tools/call", {"name": name, "arguments": args})
            body = r.json()
            refused = r.status_code != 200 or body.get("result", {}).get("isError")
            assert refused, f"{what} was not refused at all, so it measures nothing: {body}"

    async def test_the_vocabulary_is_neither_empty_nor_everything(self, measured):
        statuses, words, _surface = measured
        assert statuses, "no status was measured"
        assert len(words) > 20, f"the surface's vocabulary came back as {len(words)} words"
        for never in ("insufficient_scope", "a_word_the_surface_never_says"):
            assert never not in words, (
                f"{never!r} is in the measured vocabulary, so this comparison would accept "
                "a skill that promised it")

    async def test_the_documents_are_really_being_read(self, measured):
        _statuses, _words, surface = measured
        documents = _documents(surface)
        assert len(documents) >= 10, (
            f"only {len(documents)} documents were opened, so this file is reading almost "
            "nothing")
        assert all(len(text) > 400 for _, text in documents), (
            f"a document came back nearly empty, so the search below runs over nothing: "
            f"{[label for label, text in documents if len(text) <= 400]}")
        held = [label for label, _ in documents]
        assert any(label.endswith("ORBITD.md") for label in held), (
            f"the reference document is not among {held}, and it is the one that used to "
            "drift by hand with nothing holding it")
        trees = {label.split("/")[0] for label in held if label.startswith("skill ")}
        assert len(trees) >= 2, (
            f"every skill read came from one tree ({trees}); this contract once read one "
            "tree of the two that exist and claimed to read them all")

    async def test_the_surface_prose_is_really_being_read(self, measured):
        """The driven half of the documents, counted per route rather than in total, so
        losing a whole route goes red instead of shrinking a number that is still in the
        hundreds. Thirty tools is the floor because the listing is driven at the role
        that hides nothing: an admin token returns thirty-four, where an operator sees
        twenty-eight and a viewer twenty-two, so a gate regression reads here as a
        missing route rather than a quieter rule."""
        _, _, surface = measured
        labels = [label for label, _ in surface]
        tools = [label for label in labels if label.startswith("tool ")]
        assert len(tools) >= 30, (
            f"only {len(tools)} tool documents were driven, so this file is holding the "
            "skills to a fraction of the surface's own prose")
        assert "server instructions" in labels, (
            "the instructions string is not among the driven documents, so the sentence "
            "every caller reads before any tool is judged by nothing")
        resources = [label for label in labels if label.startswith("resource ")]
        assert len(resources) >= 5, (
            f"only {len(resources)} resource documents were driven, so a resource "
            "description could promise anything")
        templates = [label for label in labels if label.startswith("resource template ")]
        assert len(templates) >= 2, (
            f"only {len(templates)} template documents were driven, so a template "
            "description could promise anything")
        empty = [label for label, text in surface if not text.strip()]
        assert not empty, (
            f"a driven document came back empty, so the search below runs over nothing: "
            f"{empty}")

    async def test_there_is_a_real_vocabulary_to_search_for(self, measured):
        """If the subtraction ate everything, the search below would look for nothing and
        pass whatever the documents said."""
        statuses, words, _surface = measured
        unanswered, unsaid = _the_words_this_surface_never_says(statuses, words)
        assert len(unanswered) > 40, (
            f"only {len(unanswered)} statuses are left to look for, so this surface is "
            "claimed to answer nearly the whole registry")
        assert "insufficient_scope" in unsaid, (
            "the word three skills once promised a reader, and that this surface does not "
            "produce, is not among the words being searched for")
        assert unsaid >= {"access_denied", "server_error"}, (
            f"the codes left to look for are only {sorted(unsaid)}")

    async def test_every_exception_a_person_granted_still_matches_its_document(self, measured):
        """An allow-list row that matches nothing means the sentence it excused has changed
        and nobody re-decided it."""
        _statuses, _words, surface = measured
        texts = dict(_documents(surface))
        stale = [f"{label}: {quoted!r} ({why}, {when})"
                 for label, quoted, why, when in ALLOWED
                 if quoted not in texts.get(label, "")]
        assert not stale, (
            "a sentence a person once decided was a legitimate exception is no longer in "
            f"the document, so the exception is unexamined: {stale}")


class TestASkillPromisesOnlyWhatTheSurfaceAnswers:
    async def test_no_document_names_a_status_the_surface_does_not_answer(self, measured):
        """The claim that went stale and that no pattern could see: three skills told a
        reader a refusal arrives as one status while the surface answered with another.

        The surface's own prose is held to the protocol codes but NOT to the bare
        three-digit statuses, and the difference is the shape of the two registries
        rather than any difference in standard.

        A protocol code is written `-32020`: a signed number of that size is a claim
        and can be nothing else, so it is searched everywhere. A status is three
        digits, and three digits are ordinary in a tool description -- a page size
        capped at 100, a list capped at 500, an example naming a status code as a
        search tag. Searching those here objected to six true sentences and the only
        way past it was to spell the numbers out in words, which makes a description
        worse for the agent reading it. That is the rule bending prose rather than
        catching a lie.

        The shipped documents keep the status half, because that is where a status is
        claimed as an answer a reader should wait for. What this gives up is a tool
        description that names a status the surface never sends, in digits. That is
        a gap, and it is written down in the block at the top of this file.

        The other gap this half has: a document whose subject is a tenant's own application
        states that application's statuses, and nothing tells those apart from a claim about
        this surface except the subject of the document. So this half asks the protocol
        scope, and a status claim invented in a working skill about a tenant's app is not an
        offence here. Measured: asking every document produced thirty-one lines, all wrong."""
        statuses, words, surface = measured
        unanswered, _ = _the_words_this_surface_never_says(statuses, words)
        unsent = _the_protocol_codes_it_never_sends(words)
        offenders = []
        for label, text in _the_protocol_documents(surface):
            offenders += _offences(label, text, unanswered, set(),
                                   unsent_protocol_codes=unsent)
        for label, text in _documents(surface):
            offenders += _offences(label, text, set(), set(),
                                   unsent_protocol_codes=unsent)
        for label, text in surface:
            offenders += _offences(label, text, set(), set(),
                                   unsent_protocol_codes=unsent)
        assert not offenders, (
            "a shipped document tells a reader something arrives as a status this surface "
            f"never answers with, so the reader waits for what never comes: {offenders}")

    async def test_no_document_hands_a_reader_a_word_the_surface_never_says(self, measured):
        """The other half: a code-shaped word a document puts in front of a reader is a
        word an agent will look for in what it receives.

        The surface's own prose is held to the same standard, and separately from the
        vocabulary: a word a tool description says does not make it a word the surface
        produces, so a skill repeating it is still an offence."""
        statuses, words, surface = measured
        _, unsaid = _the_words_this_surface_never_says(statuses, words)
        lowered = frozenset(word.lower() for word in words)
        offenders = []
        for label, text in _documents(surface) + surface:
            offenders += _offences(label, text, set(), unsaid)
        # The invented-header half asks whether a document shows a reader a header THIS
        # surface sends, so it asks only where the subject is this surface. A document
        # about an event arriving from another vendor names that vendor's headers, and
        # they are true.
        for label, text in _the_protocol_documents(surface) + surface:
            offenders += _offences(label, text, set(), unsaid, lowered)
        assert not offenders, (
            "a shipped document hands a reader a word to look for that this surface never "
            f"produces: {offenders}")


class TestTheSurfaceCannotVouchForItself:
    """Anti-vacuity for the division above, planted so a later reader does not have to
    rediscover either half. The first row plants the fourth review's mutation exactly: the
    same invented word in a served tool description and in a shipped skill. The second
    row plants the number half: a JSON-RPC code arrives as a number, so a code the
    surface sends must be vocabulary and one it never sends must be an offence."""

    PLANTED = "access_denied"

    async def test_a_word_in_a_tool_description_and_a_skill_is_still_an_offence(
            self, fake_backends, tmp_path, monkeypatch):
        """The defect this whole change exists for. The planted word is served in one
        tool's description by a real app built here, and written into a copied skill; the
        rule must object to both documents, because the description saying the word is
        not the surface producing it."""
        import shutil

        import httpx2

        from conftest import ISSUER
        from orbitd.roles import Registry
        from orbitd.server import build_app

        state = fake_backends
        sentence = f"A caller without scope sees {self.PLANTED} here."
        real_tool = Registry.tool

        def tool(self, **kwargs):
            decorate = real_tool(self, **kwargs)

            def planting(fn):
                if fn.__name__ == "orbit_platform_health":
                    fn.__doc__ = (fn.__doc__ or "") + "\n" + sentence
                return decorate(fn)

            return planting

        monkeypatch.setattr(Registry, "tool", tool)
        application = build_app(state)
        async with application.router.lifespan_context(application):
            def make(token=None, **headers):
                found = dict(headers)
                if token:
                    found["Authorization"] = f"Bearer {token}"
                return httpx2.AsyncClient(
                    transport=httpx2.ASGITransport(app=application),
                    base_url=ISSUER, headers=found)

            red = await _drive(state, make, monkeypatch)
        statuses, words, surface = red
        _, unsaid = _the_words_this_surface_never_says(statuses, words)
        assert self.PLANTED in unsaid, (
            f"{self.PLANTED!r} left the unsaid set, so planting it proves nothing: either "
            "the surface really produces it now, or the vocabulary is trusting prose again")
        tool_docs = [(label, text) for label, text in surface if self.PLANTED in text]
        assert len(tool_docs) == 1, (
            f"the planted sentence is served in {len(tool_docs)} tool documents, so this "
            "case is not planting what it claims to plant")
        import shipped_documents

        root = tmp_path / "skills"
        source = shipped_documents.skill_document_paths()[0].parent.parent
        shutil.copytree(source, root)
        target = sorted(root.rglob("SKILL.md"))[0]
        target.write_text(target.read_text().rstrip("\n")
                          + f"\n\nA call above your role is refused with `{self.PLANTED}`.\n")
        lowered = frozenset(word.lower() for word in words)
        unsent = _the_protocol_codes_it_never_sends(words)
        offenders = []
        for label, text in tool_docs + [(target.parent.name, target.read_text())]:
            offenders += _offences(label, text, set(), unsaid, lowered,
                                   unsent_protocol_codes=unsent)
        assert any(offence.startswith(f"{tool_docs[0][0]}:") for offence in offenders), (
            "the served tool description naming an invented word is not an offence, so a "
            "tool description can promise anything again")
        assert any(offence.startswith(f"{target.parent.name}:") for offence in offenders), (
            "the skill repeating a word only a tool description says is not an offence, "
            "so the document is vouching for itself again")

    async def test_a_sent_protocol_code_is_vocabulary_and_an_unsent_one_is_an_offence(
            self, measured):
        """The number half. Whatever reserved code the drive saw on the wire must not be
        reported when a document names it, and the smallest code the surface never sent
        must be reported when one does. The codes are read off the drive rather than
        written down here, so a transport that legitimately changes its codes changes
        what this holds rather than failing on a stale literal."""
        _, words, _surface = measured
        unsent = _the_protocol_codes_it_never_sends(words)
        sent = sorted(RESERVED_PROTOCOL_CODES - unsent)
        assert sent, (
            "the surface sent no reserved protocol code, so this case measures nothing")
        answered = sent[0]
        quiet = _offences("a-document",
                          f"A version mismatch is answered with {answered}.",
                          set(), set(), unsent_protocol_codes=unsent)
        assert not quiet, (
            f"a code the surface really sends ({answered}) is reported as invented, so "
            f"numbers are not reaching the vocabulary: {quiet}")
        missing = sorted(unsent)[0]
        loud = _offences("a-document",
                         f"A version mismatch is answered with {missing}.",
                         set(), set(), unsent_protocol_codes=unsent)
        assert loud, (
            f"a code the surface never sends ({missing}) is not reported, so the "
            "reserved range is measuring nothing")
