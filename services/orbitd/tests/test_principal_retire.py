"""Taking a machine identity out of service.

WHY IT IS NEEDED. A principal registered on this surface is permanent. Its secret's
fingerprint lives in the store for ever, and nothing removes it. The only destructive verb
here revokes one access TOKEN by its id, which does nothing about the credential that
minted it: the holder simply exchanges the secret again and gets another. So a leaked
credential cannot be taken out of service at all, and an identity created by mistake can
never be tidied away — the front created three such identities during an acceptance run and
could not remove any of them, so every reader of the identity list since has seen them.

WHAT RETIRING MUST DO. Three things, and the first two are the whole point:

  - the secret stops minting tokens;
  - the tokens it already minted stop working, because a retirement that leaves a live
    token behind has not taken the identity out of service, it has only made the next
    exchange fail;
  - the name stops appearing in the identity list, so what a reader sees is what is real.

AND ONE IT MUST REFUSE. The surface must not be able to lock itself out. Retiring the last
identity that can administer it leaves nobody who can issue another, and no path back in.
That refusal changes nothing.

The confirmation, the shape of the answer and the idempotence follow the destructive verb
that already exists here, so a reader who knows one knows the other.

Run under pytest from services/orbitd.
"""

from __future__ import annotations

import json
import jwt
import pytest
from conftest import MCP_URL, mint, sdk_client
from mcp.client.streamable_http import streamable_http_client
from mcp import Client
from mcp_types import ElicitResult
from mcp.types import TextContent  # noqa: F401  (parity with the sibling suites)

from orbitd.roles import Role
from test_auth import rpc

pytestmark = pytest.mark.anyio

RETIRE = "orbit_auth_retire"


async def exchange(http, name: str, secret: str):
    async with http() as h:
        return await h.post("/token", data={"grant_type": "client_credentials"},
                            auth=(name, secret))


async def call_retire(http, token, name, confirm=True):
    async with sdk_client(http, token, confirm=confirm) as client:
        return await client.call_tool(RETIRE, {"name": name})


async def listed_principals(http, token) -> set[str]:
    async with sdk_client(http, token) as client:
        result = await client.call_tool("orbit_auth_list", {})
    items = result.structured_content["principals"]["items"]
    return {p["name"] for p in items}


# ------------------------------------------------------------------ the verb exists

async def test_the_surface_offers_a_way_to_retire_an_identity(state, http):
    """Today it does not, and that is the defect: revoking a token is not retiring an
    identity, because the secret mints another one."""
    async with sdk_client(http, mint(state, Role.admin)) as client:
        names = {t.name for t in (await client.list_tools()).tools}
    assert RETIRE in names, (
        "an admin cannot take a machine identity out of service; a leaked credential "
        "can only be chased one token at a time, for ever")


# --------------------------------------------------------------- what it must achieve

async def test_the_secret_stops_minting_tokens(state, http):
    secret = state.store.add_principal("leaked", Role.viewer, 3600)
    assert (await exchange(http, "leaked", secret)).status_code == 200, (
        "the fixture's credential never worked; nothing below would mean anything")
    result = await call_retire(http, mint(state, Role.admin), "leaked")
    assert not result.is_error, result.content
    assert (await exchange(http, "leaked", secret)).status_code != 200, (
        "the retired credential still mints tokens")


async def test_the_tokens_it_already_minted_stop_working(state, http):
    """The half that makes retirement mean something. Without it, a leaked credential's
    live token keeps working until it expires, which may be a year."""
    secret = state.store.add_principal("leaked", Role.viewer, 3600)
    issued = (await exchange(http, "leaked", secret)).json()["access_token"]
    async with sdk_client(http, issued) as client:
        assert not (await client.call_tool("orbit_platform_health", {})).is_error, (
            "the fixture's token never worked; nothing below would mean anything")
    await call_retire(http, mint(state, Role.admin), "leaked")
    async with http(issued) as h:
        r = await h.get("/mcp")
    assert r.status_code == 401, (
        "a token minted before the retirement is still accepted: " + r.text[:200])


async def test_the_name_stops_appearing_in_the_identity_list(state, http):
    state.store.add_principal("mistake", Role.viewer, 3600)
    admin = mint(state, Role.admin)
    assert "mistake" in await listed_principals(http, admin)
    await call_retire(http, mint(state, Role.admin), "mistake")
    assert "mistake" not in await listed_principals(http, admin)


# ------------------------------------------------------------------ what it must refuse

async def test_the_last_administrator_cannot_be_retired(state, http):
    """Otherwise the surface locks itself out: nobody left who can issue another."""
    state.store.add_principal("only-admin", Role.admin, 3600)
    before = state.store.admins()
    assert before == ["only-admin"], f"the fixture is not a single-admin surface: {before}"
    result = await call_retire(http, mint(state, Role.admin), "only-admin")
    assert result.is_error, "the surface retired the only identity that can administer it"
    assert state.store.admins() == before, "a refusal changed the store"


async def test_a_second_administrator_can_be_retired(state, http):
    """The guard that stops the refusal above being a blanket ban on admins."""
    state.store.add_principal("admin-one", Role.admin, 3600)
    state.store.add_principal("admin-two", Role.admin, 3600)
    result = await call_retire(http, mint(state, Role.admin), "admin-two")
    assert not result.is_error, result.content
    assert state.store.admins() == ["admin-one"]


async def test_declining_the_confirmation_changes_nothing(state, http):
    secret = state.store.add_principal("kept", Role.viewer, 3600)
    result = await call_retire(http, mint(state, Role.admin), "kept", confirm=False)
    assert result.is_error or result.structured_content.get("status") != "applied"
    assert (await exchange(http, "kept", secret)).status_code == 200, (
        "a declined retirement took the identity out of service anyway")


async def test_retiring_a_name_that_is_not_there_changes_nothing(state, http):
    state.store.add_principal("present", Role.viewer, 3600)
    before = state.store.load()
    await call_retire(http, mint(state, Role.admin), "absent")
    assert state.store.load()["principals"].keys() == before["principals"].keys()


async def test_retiring_twice_is_the_same_as_retiring_once(state, http):
    state.store.add_principal("twice", Role.viewer, 3600)
    admin = mint(state, Role.admin)
    first = await call_retire(http, admin, "twice")
    assert not first.is_error, first.content
    second = await call_retire(http, mint(state, Role.admin), "twice")
    assert not second.is_error, second.content


# ---------------------------------------------------------------------- who may do it

@pytest.mark.parametrize("role", [Role.viewer, Role.operator])
async def test_only_an_administrator_may_retire(state, http, role):
    secret = state.store.add_principal("target", Role.viewer, 3600)
    async with sdk_client(http, mint(state, role)) as client:
        names = {t.name for t in (await client.list_tools()).tools}
    assert RETIRE not in names, f"a {role.value} is offered the retirement verb"
    assert (await exchange(http, "target", secret)).status_code == 200, (
        "the identity was retired by someone who should not have been able to")


# ------------------------------------------------------------------- and no leakage

async def test_no_secret_appears_in_what_the_verb_says(state, http):
    secret = state.store.add_principal("quiet", Role.viewer, 3600)
    result = await call_retire(http, mint(state, Role.admin), "quiet")
    blob = str(result.content) + str(result.structured_content)
    assert secret not in blob
    assert "secret_sha256" not in blob


# --------------------------------------------- a name that predates the naming rule

# The rule that says what a principal may be called was added after this surface had
# already created principals. A reviewer proved what that leaves behind: an identity
# whose name does not match the rule cannot be retired at all, because the rule refuses
# the name before the handler that would retire it ever runs. Its tokens stay live and
# its name stays in the list, for ever, which is the exact condition retiring exists to
# end.
#
# The split these checks require: the rule constrains what may be CREATED, not what may
# be NAMED. A reader can always name something that exists. Creating is still held to
# the rule, and the checks for that stay where they are.

LEGACY = "Ops_Team.2024"


async def test_an_identity_whose_name_predates_the_rule_can_still_be_retired(state, http):
    secret = state.store.add_principal(LEGACY, Role.viewer, 3600)
    assert (await exchange(http, LEGACY, secret)).status_code == 200, (
        "the fixture's credential never worked; nothing below would mean anything")
    result = await call_retire(http, mint(state, Role.admin), LEGACY)
    assert not result.is_error, (
        "an identity created before the naming rule cannot be retired, so its credential "
        f"stays live for ever: {result.content}")
    assert (await exchange(http, LEGACY, secret)).status_code != 200, (
        "the retired credential still mints tokens")


async def test_its_live_tokens_stop_working_too(state, http):
    secret = state.store.add_principal(LEGACY, Role.viewer, 3600)
    issued = (await exchange(http, LEGACY, secret)).json()["access_token"]
    async with sdk_client(http, issued) as client:
        assert not (await client.call_tool("orbit_project_list", {})).is_error, (
            "the fixture's token never worked")
    await call_retire(http, mint(state, Role.admin), LEGACY)
    # Asked the way its strict twin asks it. A retired token is refused at the transport,
    # so no session is ever established and there is no call to read an error from.
    # Asked with a request that always answers. A GET on this surface opens a stream, so
    # if the retirement did not happen the case would hang rather than fail, and a suite
    # that hangs on a broken implementation says nothing about which case is stuck.
    async with http(issued) as h:
        r = await rpc(h, "tools/list")
    assert r.status_code == 401, (
        "a token minted by an identity whose name predates the rule outlived its "
        "retirement: " + r.text[:200])


async def test_its_name_leaves_the_list(state, http):
    state.store.add_principal(LEGACY, Role.viewer, 3600)
    admin = mint(state, Role.admin)
    assert LEGACY in await listed_principals(http, admin)
    await call_retire(http, admin, LEGACY)
    assert LEGACY not in await listed_principals(http, admin)


async def test_naming_something_that_does_not_exist_still_goes_nowhere(state, http):
    """Widening what may be named must not widen what may be made. A name nothing holds
    is reported as holding nothing, and no principal appears."""
    admin = mint(state, Role.admin)
    before = await listed_principals(http, admin)
    result = await call_retire(http, admin, "No_Such.Identity")
    assert not result.is_error, result.content
    assert await listed_principals(http, admin) == before, (
        "retiring a name nothing holds created something")


async def test_creating_is_still_held_to_the_rule(state, http):
    """The other half of the split, and the reason this is not simply a widening: the
    rule still decides what may be created, so no new name like this can appear."""
    async with sdk_client(http, mint(state, Role.admin)) as client:
        result = await client.call_tool("orbit_auth_issue",
                                        {"name": LEGACY, "role": "viewer", "ttl": 3600})
    assert result.is_error, (
        "an identity can still be created with a name the rule forbids, so the rule now "
        "constrains nothing at all")
    assert state.store.principal(LEGACY) is None


# This case used to assert that a name of five thousand characters is refused. That was the
# old shape: the verb carried a length bound, so an absurd name was refused by it. The bound
# is gone, because loading applies no rule to what the store file holds and any bound leaves
# some identity listed, minting tokens, and un-retirable.
#
# The concern the case was written for is still real and is kept here: an unbounded string
# must not become a way to hurt the surface. What changed is the answer. An absurd name is
# now a name that simply is not there, which is a case the verb already had an answer for --
# it is idempotent, it changes nothing, and everything it hands back or writes down stays
# bounded.


async def test_a_name_nobody_could_hold_changes_nothing(state, http):
    """A name that does not exist is retired idempotently, whatever its length."""
    admin = mint(state, Role.admin)
    state.store.add_principal("a-real-one", Role.viewer, 3600)
    before = await listed_principals(http, admin)
    async with sdk_client(http, admin) as client:
        result = await client.call_tool(RETIRE, {"name": "x" * 5000})
    assert not result.is_error, (
        "a name nobody holds is neither there to remove nor a reason to fail: "
        + str(result.content)[:300])
    assert await listed_principals(http, admin) == before, (
        "retiring a name nobody holds changed the identity list")


# The case that stood here reached one of the five places this verb echoes the caller's
# name. It left the confirmation callback at its default, which declines, so the call landed
# on the refusal and never on the question the operator is asked, the applied answer, the
# last-admin refusal, or the evidence. A review removed the cut from the other four together
# and the whole suite stayed green, while the confirmation question for a two-megabyte name
# measured two million characters and the last-admin refusal four million.
#
# That is the third time this node has been failed for a contract that could not see its
# subject, so the rows are the sites now, not one call. Every outcome the verb has is driven
# and everything it hands the caller is captured -- including the question, which is the one
# an operator actually reads and the one nothing here had ever looked at.
#
# Two properties on each, because a review also showed that cutting to nothing passes a
# bound: what comes back is short, and it still names the identity. Replacing the whole cut
# with an empty string left the suite green and every operator being asked to retire ''.


class Asked:
    """Captures what the caller is handed at each of the verb's outcomes, the question
    included."""

    def __init__(self, confirm):
        self.confirm = confirm
        self.questions: list[str] = []

    def callback(self):
        async def ask(_context, params) -> ElicitResult:
            self.questions.append(json.dumps(params.model_dump(mode="json"), default=str)
                                  if hasattr(params, "model_dump") else str(params))
            if self.confirm is None:
                return ElicitResult(action="decline")
            return ElicitResult(action="accept", content={"confirm": self.confirm})

        return ask

    def everything(self, result):
        return self.questions + [json.dumps(result.content, default=str),
                                 json.dumps(getattr(result, "structuredContent", None),
                                            default=str)]


async def _drive(http, token, name, confirm):
    asked = Asked(confirm)
    async with http(token) as h:
        transport = streamable_http_client(MCP_URL, http_client=h)
        async with Client(transport, elicitation_callback=asked.callback()) as client:
            result = await client.call_tool(RETIRE, {"name": name})
    return asked.everything(result)


HUGE = "x" * 2_000_000


# Every outcome the verb has. The last-admin refusal needs the admin to be retiring itself;
# the others are reached with a name nobody holds or one planted for the purpose.
OUTCOMES = ("declined", "applied", "not there", "the last admin")


async def _run(state, http, outcome):
    admin_name = "an-admin"
    secret = state.store.add_principal(admin_name, Role.admin, 3600)
    token = mint(state, Role.admin)
    if outcome == "the last admin":
        data = state.store.load()
        data["principals"][HUGE] = data["principals"].pop(admin_name)
        state.store.save(data)
        return await _drive(http, token, HUGE, True)
    if outcome == "applied":
        data = state.store.load()
        data["principals"][HUGE] = {"role": "viewer", "secret_sha256": "x" * 64,
                                    "ttl": 3600, "created": "2026-01-01T00:00:00+00:00"}
        state.store.save(data)
        return await _drive(http, token, HUGE, True)
    return await _drive(http, token, HUGE, None if outcome == "declined" else True)


@pytest.mark.parametrize("outcome", OUTCOMES)
class TestEverythingTheVerbHandsBack:
    async def test_it_is_bounded(self, state, http, outcome):
        handed = await _run(state, http, outcome)
        for piece in handed:
            assert len(piece) < 65_536, (
                f"on the {outcome} path the caller is handed {len(piece)} characters for "
                "one name, so an unbounded argument is an unbounded answer")

    async def test_it_still_names_the_identity(self, state, http, outcome):
        """A cut that cuts to nothing satisfies every bound and asks an operator to destroy
        something it will not name."""
        handed = " ".join(await _run(state, http, outcome))
        assert "xxxxxxxxxx" in handed, (
            f"on the {outcome} path nothing the caller is handed names the identity, so a "
            "destructive verb stopped saying what it is about to destroy: " + handed[:400])


async def test_the_question_is_asked_at_all(state, http):
    """Anti-vacuity for the rows above: if no question reached the caller, the case that
    reads it is measuring an empty list."""
    state.store.add_principal("an-admin", Role.admin, 3600)
    asked = Asked(True)
    async with http(mint(state, Role.admin)) as h:
        transport = streamable_http_client(MCP_URL, http_client=h)
        async with Client(transport, elicitation_callback=asked.callback()) as client:
            await client.call_tool(RETIRE, {"name": "a-name-nobody-holds"})
    assert asked.questions, "the destructive verb asked nothing before acting"



# ------------------------------------------------------------------ round two, after review
# The review said land, and found six things. Three of them are here.
#
# The first is the shape of the change itself. Splitting the rule in two -- creation held to
# it, retirement not -- removed the only thing that made a WIDENING visible. A reviewer
# replaced the creation pattern with one that admits capitals and underscores, so an
# administrator could create the very name the bootstrap contract exists to forbid, and the
# whole suite stayed green. The case that was meant to catch this used the same legacy name
# as everything else, which carries an underscore and a dot, so it could only see a pattern
# narrowed, never one widened.
#
# The second is the change's own defect at a different boundary. The store applies no length
# rule at all -- it accepted names of 63, 256, 257 and 4096 characters -- while the
# retirement verb refuses anything past 256. So an identity long enough is exactly as
# un-retirable as the legacy name was: the verb refuses, the name stays, and the credential
# still mints tokens. The verb's own description tells the reader the opposite.
#
# The third is that two rules of this front are unheld here: a bound nothing tests, and a
# sentence to the operator that no check reads.


WIDENED = ("Ops", "Ops_Team", "ops.team", "OPS-TEAM", "Ops_Team.2024")


class TestCreatingStaysHeldToTheRule:
    """Written against a widening, not a narrowing: each of these is a name the rule must
    keep refusing, and none of them is the legacy name, so passing them cannot be an
    accident of that one string's shape."""

    @pytest.mark.parametrize("name", WIDENED)
    async def test_a_name_outside_the_rule_cannot_be_created(self, state, http, name):
        admin = mint(state, Role.admin)
        before = await listed_principals(http, admin)
        async with sdk_client(http, admin, confirm=True) as client:
            result = await client.call_tool("orbit_auth_issue",
                                            {"name": name, "role": "viewer", "ttl": 3600})
        assert result.is_error, (
            f"the creation rule was widened far enough to admit {name!r}, which is the "
            "name the bootstrap contract exists to forbid")
        assert await listed_principals(http, admin) == before, (
            f"creating {name!r} was refused and the identity appeared anyway")

    async def test_a_name_inside_the_rule_is_still_created(self, state, http):
        """Anti-vacuity: a rule that refused everything would pass every case above."""
        admin = mint(state, Role.admin)
        async with sdk_client(http, admin, confirm=True) as client:
            result = await client.call_tool("orbit_auth_issue",
                                            {"name": "ops-team", "role": "viewer",
                                             "ttl": 3600})
        assert not result.is_error, result.content
        assert "ops-team" in await listed_principals(http, admin)


class TestARetirementReachesEveryNameTheStoreHolds:
    """The verb's bound and the store's are different numbers, and the gap between them is
    a set of identities that can be created and never removed."""

    LONG = "a" * 300

    async def test_a_name_longer_than_the_verbs_bound_can_still_be_retired(self, state, http):
        state.store.add_principal(self.LONG, Role.viewer, 3600)
        admin = mint(state, Role.admin)
        result = await call_retire(http, admin, self.LONG)
        assert not result.is_error, (
            "an identity the store holds cannot be retired, so its credential stays live "
            f"for ever: {result.content}")
        assert self.LONG not in await listed_principals(http, admin)

    async def test_its_credential_stops_working_too(self, state, http):
        secret = state.store.add_principal(self.LONG, Role.viewer, 3600)
        issued = (await exchange(http, self.LONG, secret)).json()["access_token"]
        await call_retire(http, mint(state, Role.admin), self.LONG)
        async with http(issued) as h:
            r = await rpc(h, "tools/list")
        assert r.status_code == 401, (
            "a long-named identity was retired and its token still works: " + r.text[:200])

    async def test_the_store_really_does_hold_such_a_name(self, state, http):
        """Anti-vacuity: if the store refused the name, the two cases above would be asking
        about identities that cannot exist."""
        state.store.add_principal(self.LONG, Role.viewer, 3600)
        assert self.LONG in await listed_principals(http, mint(state, Role.admin))


class TestTheThingsTheReviewFoundUnheld:
    async def test_a_name_the_list_does_not_hold_is_applied_with_no_change(self, state,
                                                                            http):
        """This case used to require the verb to refuse the empty name, which was its lower
        bound. A review then found the store holds the empty name: it lists, it mints
        tokens, and the bound was the only thing stopping it being removed. That was the
        third round of one class on this node -- a bound on the verb is a set of identities
        that can exist and never be removed -- so the verb now carries none, and what this
        case holds is the verb's documented behaviour instead: retiring a name that is not
        there changes nothing and says so."""
        admin = mint(state, Role.admin)
        before = await listed_principals(http, admin)
        result = await call_retire(http, admin, "")
        assert not result.is_error, (
            "retiring a name the list does not hold is documented as applied with no "
            f"change, and the verb refused it: {result.content}")
        assert await listed_principals(http, admin) == before, (
            "retiring a name that was not there changed the identity list")

    async def test_the_verb_tells_a_reader_what_it_accepts(self, state, http):
        """The one operator-facing sentence that says a pre-rule name is accepted. Blanking
        it to a single letter left the suite green, so nothing held the promise the change
        exists to make."""
        admin = mint(state, Role.admin)
        async with sdk_client(http, admin) as client:
            listed = await client.list_tools()
        tools = {t.name: t for t in getattr(listed, "tools", listed)}
        verb = tools[RETIRE]
        described = json.dumps(verb.input_schema) + (verb.description or "")
        assert "predates" in described or "creation rule" in described, (
            "nothing in what the verb tells a reader says it accepts a name made before "
            "the rule existed, which is the whole point of the change")


# ------------------------------------------------------- round three, a change of shape
# Two reviews in a row have found the same thing on this node: a contract that cannot see
# its own subject. The second found it inside the repair for the first. Round two replaced
# the literal lengths with the constant under test, so every length case now moves with the
# bound and lowering it to 63 leaves all of them green -- where the commit before failed
# four. The front's rule is that a class found twice means the design is wrong, so this
# round does not repair the cases again.
#
# The reason they keep going blind is the same in both rounds, and it is not carelessness.
# Every case builds its subject with add_principal, which is the one path that enforces the
# bound. A contract written that way can only ever examine names inside the bound, so the
# claim it is making -- every name the store holds is a name retirement reaches -- is
# unfalsifiable by construction.
#
# The store is a JSON file and loading applies no rule to what it reads. A store written by
# any earlier build holds names of any length, which is exactly the situation that produced
# the legacy name this node exists to retire. So the subject is planted through the path
# that has no rule, and the bound is pinned to a number the code does not supply.
#
# That turns out to be a finding about the code, not only the cases: a name past the bound
# that the store file already holds is listed, still mints tokens, and cannot be given up.
# Any bound on the retirement verb reproduces it, so the verb should carry none. Creation
# keeps its rule; what exists must be removable.

PAST_ANY_BOUND = "legacy." + "a" * 9000


def _plant(state, name, role=Role.viewer, ttl=3600):
    """Put a principal into the store the way a store file written by an older build
    arrives: through the file, not through the one path that applies the rule. Keeps the
    secret material of a real principal so its credential can be exercised."""
    secret = state.store.add_principal("a-real-one", role, ttl)
    data = state.store.load()
    data["principals"][name] = data["principals"].pop("a-real-one")
    state.store.save(data)
    return secret


class TestWhatTheStoreHoldsCanBeGivenUp:
    """The claim the node exists to make, driven on a name the bound would refuse. Every
    case here plants its subject through the file, so none of them can be satisfied by the
    bound moving."""

    async def test_the_store_really_holds_it(self, state, http):
        """Anti-vacuity, and the premise the rest stand on: loading applies no rule, so
        this name is in the identity list exactly as an older build left it."""
        _plant(state, PAST_ANY_BOUND)
        assert PAST_ANY_BOUND in await listed_principals(http, mint(state, Role.admin)), (
            "the planted name is not in the identity list, so every case below is asking "
            "about an identity that does not exist")

    async def test_it_can_be_retired(self, state, http):
        _plant(state, PAST_ANY_BOUND)
        admin = mint(state, Role.admin)
        result = await call_retire(http, admin, PAST_ANY_BOUND)
        assert not result.is_error, (
            "a name the store file holds cannot be given up, so its credential stays live "
            f"for ever -- the defect this node exists to close: {result.content}")
        assert PAST_ANY_BOUND not in await listed_principals(http, admin)

    async def test_its_credential_stops_working(self, state, http):
        secret = _plant(state, PAST_ANY_BOUND)
        issued = (await exchange(http, PAST_ANY_BOUND, secret)).json()["access_token"]
        await call_retire(http, mint(state, Role.admin), PAST_ANY_BOUND)
        async with http(issued) as h:
            r = await rpc(h, "tools/list")
        assert r.status_code == 401, (
            "the name was retired and its token still works: " + r.text[:200])

    async def test_the_verb_carries_no_length_of_its_own(self, state, http):
        """Stated at the surface a caller sees, because that is where the refusal comes
        from. A bound here is a set of identities that can be held and never given up,
        whatever number it is: the store applies no rule on load, so no number is safe."""
        admin = mint(state, Role.admin)
        async with sdk_client(http, admin) as client:
            listed = await client.list_tools()
        tools = {t.name: t for t in getattr(listed, "tools", listed)}
        argument = tools[RETIRE].input_schema["properties"]["name"]
        assert "maxLength" not in argument, (
            "the retirement verb refuses a name longer than "
            f"{argument['maxLength']}, and the store can hold longer: those identities "
            "are listed, mint tokens, and cannot be retired")


class TestCreationKeepsItsRule:
    """The other half, and the anti-vacuity for the class above: taking the bound off
    retirement must not take it off creation. A name that does not exist yet is held to
    the rule; a name that already exists must be removable whatever it looks like."""

    async def test_a_name_past_the_bound_cannot_be_created(self, state, http):
        admin = mint(state, Role.admin)
        before = await listed_principals(http, admin)
        async with sdk_client(http, admin) as client:
            result = await client.call_tool("orbit_auth_issue",
                                            {"name": PAST_ANY_BOUND, "role": "viewer",
                                             "ttl": 3600})
        assert result.is_error, "creation accepted a name past every rule it has"
        assert await listed_principals(http, admin) == before

    async def test_the_store_still_refuses_to_create_one(self, state):
        """The bound where it belongs: on the path that makes new names, not the one that
        removes old ones. Written against a literal so it cannot be satisfied by the
        constant moving."""
        with pytest.raises(ValueError):
            state.store.add_principal("a" * 9000, Role.viewer, 3600)

    async def test_the_creation_bound_is_a_number_the_cases_do_not_supply(self):
        """What round two removed. Every length in the previous contract moved with the
        constant, so lowering it to 63 left all of them green while a forty-character
        bootstrap admin became unretirable. This is the anchor: the bound is what the
        store has actually held, and shrinking it is a change to argue for, not one to
        make quietly."""
        from orbitd.auth import PRINCIPAL_NAME_MAX_LENGTH

        assert PRINCIPAL_NAME_MAX_LENGTH == 4096, (
            "the creation bound moved. It is not a free parameter: it is the claim that "
            "no name the store has ever held is longer than this. Moving it down makes "
            "existing identities uncreatable, and the cases here will not tell you which")
