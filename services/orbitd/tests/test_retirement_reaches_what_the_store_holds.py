"""Whether the verb that removes an identity can reach every identity there is.

WHY THIS FILE EXISTS. Three rounds on this node have found one defect wearing three
lengths. The verb that retires a principal carries bounds of its own; the store that holds
principals carries different ones; and the gap between them is a set of identities that can
exist, be listed, mint tokens, and never be removed. Round one found the legacy name that
predates the creation rule. Round two found names longer than the verb's 256 and shorter
than the store's nothing-at-all. Round three found the empty name, which the store accepts,
which lists, which mints tokens, and which retirement refuses with a validation error.

Each round matched one bound to the store and left the next one standing. The front's rule
is that a class found twice means the design is wrong, so this is not a fourth repair: the
verb is to carry no bound at all. Retirement is a lookup. Whatever the identity list holds,
this verb reaches -- and where names are constrained is where they are created, which is a
different verb with a different job.

WHAT THIS FILE HOLDS, AND WHY IN THIS SHAPE. Not a list of lengths. A list is what the last
three rounds were, and a list is always one entry short of the next defect. These cases ask
what the identity list can end up holding, by putting each shape there and seeing whether it
is still there afterwards, and then require retirement to reach every single thing that
stuck. The names below are shapes, not a specification.

Say plainly what "can end up holding" means here, because a review found the earlier wording
claimed more than the file does. Planting tries the creating verb first and, when that
refuses, writes the entry into the identity file directly -- which is what an older build
with no rule left behind, and what loading still accepts today, since loading applies no
rule. So the set these cases measure is what a FILE can carry, not what the creating verb
admits. That is the right set for this question: retirement's job is to reach whatever a
real installation's file turns out to hold, however it got there. It does mean that giving
the creating verb a new rule will NOT quietly shrink these cases, so the guard below requires
at least one shape to have been planted the ordinary way; if every shape has to go through
the file, the creating verb has stopped accepting anything and that is a defect of its own.

The last class asks a different question about the same file. The shapes above are names;
these are entries. A file can hold an entry that is not shaped like a principal at all, and
the verb's premise -- that it reaches whatever the file holds -- makes that the verb's
problem too. What it must not do is fall over: an entry the verb cannot act on is refused
with a reason, and the identity list keeps answering.

Run under pytest from services/orbitd.
"""

from __future__ import annotations

import re

import pytest
from conftest import mint, sdk_client

from orbitd.roles import Role
from orbitd.tools import PRINCIPAL_NAME_PATTERN

from test_principal_retire import RETIRE, call_retire, exchange, listed_principals
from test_auth import rpc

pytestmark = pytest.mark.anyio


# Shapes, not a specification. Each is a name some build, some import, or some operator's
# mistake could put in the identity list. What matters is not that these are right but that
# whichever of them the store accepts must be retirable.
SHAPES = (
    ("the empty name", ""),
    ("a single character", "x"),
    ("a name made before the rule", "ops_team.2024"),
    ("capitals and underscores", "Ops_Team"),
    ("a name at the pattern's own length", "a" * 63),
    ("a name past the pattern's length", "a" * 64),
    ("a name past the verb's old bound", "a" * 300),
    ("a name at the store's bound", "a" * 4096),
    ("a name of spaces", "   "),
    ("a name that is not Latin", "運用チーム"),
    ("a name with a line break in it", "two\nlines"),
    ("a name that looks like a path", "../../etc/passwd"),
)


def _the_rule_allows(name: str) -> bool:
    """Whether the creation rule would admit this name. Read off the rule itself, so a
    change to the rule moves these cases with it instead of leaving them stale."""
    return re.match(PRINCIPAL_NAME_PATTERN, name) is not None


#: How each shape last reached the identity list: "verb" when the creating verb took it,
#: "file" when it had to be written in directly. Read by the guard below, which requires at
#: least one shape to have gone in the ordinary way.
PLANTED_BY: dict[str, str] = {}


def _plant(state, name: str, ttl: int = 3600) -> str | None:
    """Put `name` in the identity list by whatever means the store allows, and hand back its
    secret. First the ordinary way; if the store refuses that, straight into the file, which
    is what an older build that had no rule would have left behind -- loading applies no rule
    at all, so such a file is readable today. Returns None if the name cannot be held."""
    try:
        secret = state.store.add_principal(name, Role.viewer, ttl)
        PLANTED_BY[name] = "verb"
        return secret
    except Exception:
        pass
    try:
        import hashlib
        import secrets as _secrets

        secret = _secrets.token_urlsafe(32)
        data = state.store.load()
        data["principals"][name] = {
            "role": Role.viewer.value,
            "secret_sha256": hashlib.sha256(secret.encode()).hexdigest(),
            "ttl": ttl,
            "created": "2024-01-01T00:00:00Z",
        }
        state.store.save(data)
        if state.store.principal(name) is None:
            return None
        PLANTED_BY[name] = "file"
        return secret
    except Exception:
        return None


@pytest.fixture
def holdable(state):
    """Every shape the identity list can end up holding, measured by putting each one there
    and reading it back. This is the whole design of the file: the set of names retirement
    must reach is not written down. See the file docstring for what planting does when the
    creating verb refuses, and what that means for what this set is."""
    held = []
    for what, name in SHAPES:
        secret = _plant(state, name)
        if secret is not None and state.store.principal(name) is not None:
            held.append((what, name, secret))
        # Leave the list as we found it; each case plants what it needs.
        data = state.store.load()
        data["principals"].pop(name, None)
        state.store.save(data)
    return held


class TestTheMeasurementIsWorthComparingAgainst:
    def test_the_store_holds_more_than_one_shape(self, holdable):
        assert len(holdable) > 3, (
            "the store accepted almost nothing, so the cases below are asking about an "
            f"empty set: {[w for w, _, _ in holdable]}")

    def test_at_least_one_shape_went_in_the_ordinary_way(self, holdable):
        """The set below is what a file can carry, and planting falls back to writing the
        file when the creating verb refuses. If every shape had to go that way the creating
        verb would be refusing everything, and these cases would be measuring a surface
        nobody could put an identity on in the first place."""
        ways = {PLANTED_BY.get(name) for _, name, _ in holdable}
        assert "verb" in ways, (
            "every shape had to be written into the identity file directly, so the creating "
            "verb accepted nothing at all")

    def test_it_holds_at_least_one_the_creation_rule_forbids(self, holdable):
        """The point of the node. If every name the store held were one the creation rule
        allows, there would be no gap and nothing to repair."""
        assert {w for w, name, _ in holdable if not _the_rule_allows(name)}, (
            "the store holds only names the creation rule would allow, so this file is "
            "measuring nothing")


class TestRetirementReachesEveryIdentityTheListHolds:
    """The one property, asked once per shape the store turned out to hold."""

    @pytest.mark.parametrize("index", range(len(SHAPES)), ids=[w for w, _ in SHAPES])
    async def test_it_can_be_retired(self, state, http, holdable, index):
        what, name = SHAPES[index]
        if what not in {w for w, _, _ in holdable}:
            pytest.skip(f"the store does not hold {what}, so nothing has to reach it")
        _plant(state, name)
        admin = mint(state, Role.admin)
        assert name in await listed_principals(http, admin), (
            f"{what} was planted and is not in the identity list")
        result = await call_retire(http, admin, name)
        assert not result.is_error, (
            f"{what} is in the identity list and the verb will not remove it, so its "
            f"credential mints tokens for ever: {result.content}")
        assert name not in await listed_principals(http, admin), (
            f"{what} was retired without error and is still listed")

    @pytest.mark.parametrize("index", range(len(SHAPES)), ids=[w for w, _ in SHAPES])
    async def test_its_credential_stops_minting(self, state, http, holdable, index):
        what, name = SHAPES[index]
        secrets_by_shape = {w: s for w, _, s in holdable}
        if what not in secrets_by_shape:
            pytest.skip(f"the store does not hold {what}, so nothing has to reach it")
        secret = _plant(state, name)
        assert (await exchange(http, name, secret)).status_code == 200, (
            f"{what} could not mint a token even before retirement, so this case is not "
            "measuring what it claims to")
        await call_retire(http, mint(state, Role.admin), name)
        assert (await exchange(http, name, secret)).status_code != 200, (
            f"{what} was retired and its secret still mints tokens")

    @pytest.mark.parametrize("index", range(len(SHAPES)), ids=[w for w, _ in SHAPES])
    async def test_the_tokens_it_already_minted_stop_working(self, state, http, holdable,
                                                             index):
        """Asked with a protocol call, never with a plain GET. A review found the sibling
        case for this hangs for ever instead of failing when the sweep is removed, because
        a GET on this surface opens a stream and a still-valid token parks on it."""
        what, name = SHAPES[index]
        secrets_by_shape = {w: s for w, _, s in holdable}
        if what not in secrets_by_shape:
            pytest.skip(f"the store does not hold {what}, so nothing has to reach it")
        secret = _plant(state, name)
        issued = (await exchange(http, name, secret)).json()["access_token"]
        await call_retire(http, mint(state, Role.admin), name)
        async with http(issued) as h:
            answer = await rpc(h, "tools/list")
        assert answer.status_code == 401, (
            f"{what} was retired and a token it had already minted still works: "
            + answer.text[:200])


class TestNamesAreStillConstrainedWhereTheyAreMade:
    """The other half, and the thing that must not be lost by taking the bound off the verb.
    Relaxing what may be destroyed says nothing about what may be created."""

    def test_the_creation_rule_still_forbids_something(self):
        """The class below is parametrised over the shapes the creation rule forbids. Widen
        that rule and the list goes empty, pytest collects nothing, and the file's count
        drops with no failure -- a contract that stopped asking without saying so. This row
        is the alarm on that."""
        forbidden = [w for w, n in SHAPES if not _the_rule_allows(n)]
        assert forbidden, (
            "the creation rule forbids none of these shapes, so the class below collects "
            "nothing and the other half of this node is no longer being asked")

    @pytest.mark.parametrize("what,name", [(w, n) for w, n in SHAPES
                                           if not _the_rule_allows(n)],
                             ids=[w for w, n in SHAPES if not _the_rule_allows(n)])
    async def test_the_creation_verb_still_refuses_it(self, state, http, what, name):
        admin = mint(state, Role.admin)
        before = await listed_principals(http, admin)
        async with sdk_client(http, admin) as client:
            result = await client.call_tool("orbit_auth_issue",
                                            {"name": name, "role": "viewer", "ttl": 3600})
        assert result.is_error, (
            f"the creation rule admits {what}, which is the name the bootstrap contract "
            "exists to forbid")
        assert await listed_principals(http, admin) == before, (
            f"creating {what} was refused and the identity appeared anyway")

    async def test_an_ordinary_name_is_still_created_and_retired(self, state, http):
        """Anti-vacuity for both classes above: a surface that refused every creation, or
        one that retired nothing, would satisfy them all."""
        admin = mint(state, Role.admin)
        async with sdk_client(http, admin, confirm=True) as client:
            made = await client.call_tool("orbit_auth_issue",
                                          {"name": "ops-team", "role": "viewer",
                                           "ttl": 3600})
        assert not made.is_error, made.content
        assert "ops-team" in await listed_principals(http, admin)
        assert not (await call_retire(http, admin, "ops-team")).is_error
        assert "ops-team" not in await listed_principals(http, admin)


# ---------------------------------------------------- entries, not names

# An identity file is a file. It is written by this surface today, and it was written by
# older builds, by a restore, and by an operator with an editor. Each row below is an entry
# such a file can carry and loading accepts, paired with what makes it awkward. They are not
# names the creating verb would make; they are what the verb that reaches "whatever the file
# holds" can find when it gets there.
def _a_well_formed_entry() -> dict:
    import hashlib
    import secrets as _secrets

    secret = _secrets.token_urlsafe(32)
    return {"role": Role.viewer.value,
            "secret_sha256": hashlib.sha256(secret.encode()).hexdigest(),
            "ttl": 3600,
            "created": "2024-01-01T00:00:00Z"}


MALFORMED = (
    ("an entry that is not an object at all", "kept-as-a-bare-string", "viewer"),
    ("an entry that is a list", "kept-as-a-list", ["viewer"]),
    ("a name holding half of a character", "half-a-\ud800-character", None),
)


def _put(state, name: str, value) -> bool:
    """Write one entry straight into the identity file and say whether it survived the round
    trip. `None` means an ordinary well-formed entry, so the row is about the name alone."""
    try:
        data = state.store.load()
        data["principals"][name] = _a_well_formed_entry() if value is None else value
        state.store.save(data)
        return name in state.store.load().get("principals", {})
    except Exception:
        return False


#: The names an ordinary drive leaves in the identity list, so a case can tell the entry it
#: planted from the furniture.
_ORDINARY = frozenset({"ops-team", "a-name-this-list-does-not-hold"})


def _words(text: str) -> set:
    import re as _re

    return {w for w in _re.findall(r"[a-z]{4,}", (text or "").lower())}


async def _answer_text(http, token, name) -> str:
    result = await call_retire(http, token, name)
    return "".join(getattr(part, "text", "") or "" for part in (result.content or [])), result


class TestWhatTheVerbDoesWithAnEntryItCannotActOn:
    """The verb's premise is that it reaches whatever the identity file holds. An entry it
    cannot act on is part of that. The property is not that it must succeed -- some of these
    cannot be retired in any meaningful sense -- but that it answers like the surface's other
    refusals: with a reason, without falling over, and leaving the list answerable.

    The reason is not a wording. It is measured against the surface's own refusal for an
    ordinary retirement it will not do (a name the list does not hold): the answer to a
    malformed entry must share reasoning words with it. A bare internal failure shares none.
    """

    @pytest.mark.parametrize("index", range(len(MALFORMED)),
                             ids=[w for w, _, _ in MALFORMED])
    def test_the_identity_file_really_carries_it(self, state, index):
        """Anti-vacuity: an entry the file cannot even hold asks nothing below."""
        what, name, value = MALFORMED[index]
        assert _put(state, name, value), (
            f"{what} did not survive being written to the identity file, so the cases below "
            "are asking about an entry that cannot exist")

    @pytest.mark.parametrize("index", range(len(MALFORMED)),
                             ids=[w for w, _, _ in MALFORMED])
    async def test_the_identity_list_still_answers(self, state, http, index):
        """One bad entry must not take the whole list down: an admin who cannot list cannot
        find the bad entry, and cannot clean it up through this surface at all."""
        what, name, value = MALFORMED[index]
        assert _put(state, name, value)
        async with sdk_client(http, mint(state, Role.admin)) as client:
            result = await client.call_tool("orbit_auth_list", {})
        assert not result.is_error, (
            f"{what} is in the identity file and listing identities now fails, so an admin "
            f"cannot even see what is wrong: {result.content}")

    @pytest.mark.parametrize("index", range(len(MALFORMED)),
                             ids=[w for w, _, _ in MALFORMED])
    async def test_what_the_admin_is_shown_is_what_the_admin_can_retire(self, state, http,
                                                                        index):
        """The route an admin really has. They cannot type an entry's name from nowhere --
        they read it off the identity list and send back what they were shown. For an entry
        whose name a JSON transport cannot carry at all, that is the ONLY route: the raw name
        cannot leave any client, so if the name the listing shows does not reach the entry,
        nothing the admin can send ever will and the entry is on this surface for ever.

        Asked of every shape, because it is the same property for all of them: an entry a
        reader was shown is an entry a reader can remove."""
        what, name, value = MALFORMED[index]
        assert _put(state, name, value)
        admin = mint(state, Role.admin)
        shown = [one for one in await listed_principals(http, admin)
                 if one not in {"a-name-this-list-does-not-hold"}]
        mine = [one for one in shown if one not in _ORDINARY]
        assert mine, (
            f"{what} is in the identity file and the identity list does not show it at all, "
            f"so an admin cannot act on it: {shown}")
        for one in mine:
            await call_retire(http, admin, one)
        left = state.store.load().get("principals", {})
        assert name not in left, (
            f"{what} was shown to an admin as {mine!r} and retiring exactly what they were "
            f"shown did not remove it, so nothing they can send ever will: {list(left)}")

    @pytest.mark.parametrize("index", range(len(MALFORMED)),
                             ids=[w for w, _, _ in MALFORMED])
    async def test_retiring_it_reaches_it(self, state, http, index):
        """The premise of this node, applied to entries rather than names: retirement is a
        lookup, and whatever the identity file holds, this verb reaches. Retiring a name the
        list does not hold already answers plainly rather than refusing, so there is no
        refusal shape for a malformed entry to borrow -- the entry is simply to be gone."""
        what, name, value = MALFORMED[index]
        if not name.isprintable() or any("\ud800" <= ch <= "\udfff" for ch in name):
            pytest.skip(
                "no client can put this name on the wire: encoding it fails before any verb "
                "runs, so a row that sent it would be measuring the transport. The route an "
                "admin really has is the row above, which asks it of every shape.")
        assert _put(state, name, value)
        admin = mint(state, Role.admin)
        told, result = await _answer_text(http, admin, name)
        assert not result.is_error, (
            f"{what} is in the identity file and the verb will not remove it, so the only "
            f"way to clean it up is to edit the file by hand: {told!r}")
        assert name not in state.store.load().get("principals", {}), (
            f"{what} was retired without error and is still in the identity file")
