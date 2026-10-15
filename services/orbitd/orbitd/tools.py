"""The admin trio behind the confirmation step. Viewer cluster tools live in
`tools_viewer`. Tools carry no workflow (SKILLS-ADDENDUM): each is one verb with
typed input and an evidence-shaped result.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from typing import Annotated, Any

from mcp.server.mcpserver import Context
from mcp_types import ElicitRequest, ElicitRequestFormParams, ElicitResult, InputRequiredResult
from pydantic import BaseModel, Field

from .auth import MIN_TTL
from .roles import Listing, Mutation, Registry, Role, Step, failure
from .state import AppState

CONFIRM_KEY = "confirm"
CONFIRM_SCHEMA = {"type": "object",
                  "properties": {"confirm": {"type": "boolean", "title": "Confirm",
                                             "description": "true to apply, false to refuse"}},
                  "required": ["confirm"]}

# The rule for what a principal may be CALLED when it is created. Enforced on
# orbit_auth_issue and carried on orbit_auth_retire as CreationRule metadata
# (advertised, not enforced). Bootstrap carries the same literal, and a
# contract reads the pattern off the retirement verb's Python type metadata,
# not off its schema — the schema a client receives carries only a
# description, a title and a type — so the three cannot drift
# apart. One rule, one literal here.
PRINCIPAL_NAME_PATTERN = r"^[a-z0-9][a-z0-9-]{0,62}$"

# Retirement carries no length bound of its own. Loading applies no rule to
# what the store file holds, so a file written by an older build may hold a
# name longer than any bound, and any bound on this verb would leave such an
# identity listed, still minting tokens, and un-retirable. Creation keeps its
# bound on the store path (auth.PRINCIPAL_NAME_MAX_LENGTH); removal must reach
# whatever exists.


@dataclass(frozen=True)
class CreationRule:
    """The creation rule for a principal name, carried as metadata, not a constraint.

    Attached to verbs that NAME a principal without CREATING one. Retirement
    accepts any name — a reader can always name something that exists,
    including an identity created before the rule — so its argument enforces
    nothing. The rule itself still rides along so the bootstrap
    contract, which reads the pattern off this verb's Python type metadata
    (get_type_hints with extras), keeps holding creation (issue, bootstrap) to
    exactly this rule. It is not in the schema a client receives: that carries
    only a description, a title and a type.
    """

    pattern: str


def confirmation(ctx: Context, message: str) -> InputRequiredResult | bool:
    """The MRTR confirmation step. First round: an `InputRequiredResult` carrying one
    `elicitation/create`; the client answers and retries with `inputResponses` and the
    echoed `requestState`. The SDK seals that state to this principal, this tool, these
    exact arguments and a ten-minute window, so a retry cannot swap arguments or be
    replayed by another token. Returns True (apply), False (refuse) on the retry."""
    if not ctx.input_responses or CONFIRM_KEY not in ctx.input_responses:
        return InputRequiredResult(
            input_requests={CONFIRM_KEY: ElicitRequest(
                params=ElicitRequestFormParams(message=message, requested_schema=CONFIRM_SCHEMA))},
            request_state=CONFIRM_KEY)
    answer = ctx.input_responses[CONFIRM_KEY]
    if ctx.request_state != CONFIRM_KEY:
        return False
    return (isinstance(answer, ElicitResult) and answer.action == "accept"
            and bool((answer.content or {}).get("confirm")) is True)


def refused(what: str) -> Mutation:
    return Mutation(status="refused", steps=[Step(name="confirm", status="skipped", detail=what)])


#: Longest caller-controlled name echoed back in an answer. The retirement verb
#: below takes an unbounded argument, so every string it hands back that names
#: the argument is cut here, with a marker, rather than dropped: the answer
#: still names the identity well enough for an operator to recognise which one
#: was asked about. One place for every echo, not one per call site.
MAX_ECHO_NAME_LEN = 256

ECHO_TRUNCATED = "...[truncated]"


#: Half of a character: an unpaired surrogate, which a real identity file can hold
#: in a name and which no JSON transport can carry (encoding one fails client- or
#: server-side before any verb runs). Rendered as the replacement character, never
#: dropped: the answer still names the identity, and stays serializable.
_UNPAIRED_SURROGATE = re.compile(r"[\ud800-\udfff]")


def renderable(text: str) -> str:
    """Swap half-characters for U+FFFD so a string survives JSON serialization."""
    return _UNPAIRED_SURROGATE.sub("\ufffd", text)


def echo_name(name: str) -> str:
    """Render one caller-controlled name for an answer, bounded. Cut, not dropped."""
    if len(name) > MAX_ECHO_NAME_LEN:
        return renderable(name[:MAX_ECHO_NAME_LEN] + ECHO_TRUNCATED)
    return renderable(name)


def _retire_key(principals: dict[str, Any], name: str) -> tuple[str | None, list[str]]:
    """Resolve what the caller sent to the store key to remove.

    Exact match first: a name that is really there is that entry, even when it
    also happens to be what another entry renders as. Otherwise the rendered
    form (see renderable): the identity list shows transport-safe names, and a
    name no transport can carry can only ever arrive in that form. Returns the
    key to remove, or None when nothing matches, plus every rendered match so
    the caller can tell "nothing" (idempotent, applied with no change) from
    "more than one" (refused, so one displayed name never removes another
    entry than the one shown).
    """
    if name in principals:
        return name, [name]
    shown = renderable(name)
    matches = [k for k in principals if renderable(k) == shown]
    if len(matches) == 1:
        return matches[0], matches
    return None, matches


class Principal(BaseModel):
    name: str
    role: str
    created: str
    token_ttl_seconds: int


class IssuedToken(BaseModel):
    jti: str
    sub: str
    role: str
    issued_at: str
    expires_at: str
    revoked: bool


class AuthListing(BaseModel):
    principals: Listing
    tokens: Listing


AUTH_LIST_CAP = 500


def _token_issued(entry: Any) -> float:
    """The mint time of a stored token row as unix seconds (the store writes `iat` as an
    RFC 3339 string; an integer is accepted too); an unreadable row sorts last."""
    iat = entry.get("iat") if isinstance(entry, dict) else None
    if isinstance(iat, (int, float)):
        return float(iat)
    try:
        return datetime.fromisoformat(str(iat).replace("Z", "+00:00")).timestamp()
    except (TypeError, ValueError):
        return 0.0


def _token_row(jti: str, entry: Any, revoked: bool) -> IssuedToken:
    """One token row, or an "unreadable" row for an entry not shaped like a token:
    the same rule as _listed_row, so one bad row cannot take the listing down."""
    try:
        return IssuedToken(jti=renderable(jti), sub=renderable(entry["sub"]),
                           role=renderable(entry["role"]), issued_at=renderable(entry["iat"]),
                           expires_at=renderable(entry["exp"]), revoked=revoked)
    except Exception:  # noqa: BLE001 - any shape failure is the same answer
        return IssuedToken(jti=renderable(jti), sub="unreadable", role="unreadable", issued_at="unreadable",
                           expires_at="unreadable", revoked=revoked)


def _listed_row(name: str, entry: Any) -> Principal:
    """Render one store row for the identity list without falling over.

    One bad entry must not take the whole list down: an admin who cannot list
    cannot find the bad entry. A row that cannot be rendered as a principal is
    reported as a row that cannot be read -- under its real name, so the admin
    can act on what they see and retire it -- instead of failing the call or
    silently hiding the row. Names are made transport-safe (see renderable); a
    well-formed row is otherwise unchanged.
    """
    try:
        row = Principal(name=name, role=entry["role"], created=entry["created"],
                        token_ttl_seconds=int(entry["ttl"]))
    except Exception:
        return Principal(name=renderable(name), role="unreadable",
                         created="unreadable", token_ttl_seconds=0)
    return Principal(name=renderable(row.name), role=renderable(row.role),
                     created=renderable(row.created),
                     token_ttl_seconds=row.token_ttl_seconds)


def register(registry: Registry, state: AppState) -> None:

    @registry.tool(role=Role.admin, read_only=False, destructive=False)
    def orbit_auth_issue(
        name: Annotated[str, Field(pattern=PRINCIPAL_NAME_PATTERN,
                                   description="Principal name; becomes the token `sub` and the client_id.")],
        role: Annotated[Role, Field(description="viewer, operator or admin; each includes the ones below.")],
        ttl: Annotated[int, Field(ge=MIN_TTL, le=366 * 86400,
                                  description="Lifetime, in seconds, of every access token this principal is granted.")]
            = 30 * 86400,
        ctx: Context = None,  # type: ignore[assignment]
    ) -> Mutation | InputRequiredResult:
        """Register a machine principal and return its client credentials once. Asks for
        confirmation first (an elicitation the client answers), then writes the principal
        to the store. The secret in `evidence.client_secret` is never readable again; the
        principal exchanges it for access tokens at the token endpoint.
        Example: orbit_auth_issue(name="ci-deployer", role="operator", ttl=2592000)."""
        decision = confirmation(ctx, f"Issue principal '{name}' with role {role.value}, token ttl {ttl}s?")
        if isinstance(decision, InputRequiredResult):
            return decision
        if not decision:
            return refused(f"issuing principal '{name}' was not confirmed")
        try:
            secret = state.store.add_principal(name, role, ttl)
        except ValueError as e:
            return Mutation(status="failed", steps=[Step(name="store", status="failed", detail=str(e))])
        return Mutation(status="applied",
                        steps=[Step(name="confirm", status="ok"), Step(name="store", status="ok")],
                        evidence={"client_id": name, "client_secret": secret, "role": role.value,
                                  "token_ttl_seconds": ttl,
                                  "token_endpoint": state.cfg.issuer_url + "/token"})

    @registry.tool(role=Role.admin, read_only=False, destructive=True, idempotent=True)
    def orbit_auth_revoke(
        jti: Annotated[str, Field(min_length=8, max_length=64, pattern=r"^[A-Za-z0-9_-]+$",
                                  description="The `jti` claim of the token to revoke (see orbit_auth_list).")],
        ctx: Context = None,  # type: ignore[assignment]
    ) -> Mutation | InputRequiredResult:
        """Revoke one access token by its `jti`. Asks for confirmation first, then appends
        the id to the revocation list; every later request bearing that token gets 401.
        Idempotent: revoking a revoked token is applied with no change. An id no token on
        record carries is still appended (a token is refused by its id, whether or not
        this orbitd remembers issuing it) and `evidence.known_token` is false: the step
        says so, since a mistyped id cuts nothing off.
        Example: orbit_auth_revoke(jti="Q1w2E3r4T5y6U7i8")."""
        decision = confirmation(ctx, f"Revoke token {jti}? Every request bearing it will be refused.")
        if isinstance(decision, InputRequiredResult):
            return decision
        if not decision:
            return refused(f"revoking token {jti} was not confirmed")
        already = jti in state.revocations.read()
        if not already:
            state.revocations.add(jti)
        known = state.store.load()["tokens"].get(jti)
        return Mutation(status="applied",
                        steps=[Step(name="confirm", status="ok"),
                               Step(name="revocations", status="ok",
                                    detail=("already revoked" if already else "appended")
                                    + ("" if known is not None else
                                       "; no token with this jti is on record here (orbit_auth_list shows the "
                                       "ids of the tokens that are), so check the id: nothing in use was cut off"))],
                        evidence={"jti": jti, "known_token": known is not None, "sub": (known or {}).get("sub")})

    @registry.tool(role=Role.admin, read_only=False, destructive=True, idempotent=True)
    def orbit_auth_retire(
        name: Annotated[str, CreationRule(PRINCIPAL_NAME_PATTERN),
                        Field(description="Principal name to take out of service; any name the "
                                          "identity list may hold, including one that predates "
                                          "the creation rule.")],
        ctx: Context = None,  # type: ignore[assignment]
    ) -> Mutation | InputRequiredResult:
        """Retire a machine principal: its secret stops minting tokens, every token it
        minted is revoked, and its name leaves the identity list. Asks for confirmation
        first, then removes the principal and appends each of its tokens' ids to the
        revocation list. Idempotent: retiring a name that is not there is applied with
        no change, as is retiring twice. A name that predates the creation rule retires
        like any other: the rule constrains what may be created, not what may be named.
        A name the identity list shows with half-characters replaced retires the one
        entry it was shown as; when it renders more than one entry it is refused
        rather than removing another entry than the one shown. Refuses the last
        admin principal, since nobody
        could issue another afterwards. Example: orbit_auth_retire(name="leaked-ci")."""
        decision = confirmation(ctx, f"Retire principal '{echo_name(name)}'? Its secret will stop minting "
                                     "tokens and every token it minted will be refused.")
        if isinstance(decision, InputRequiredResult):
            return decision
        if not decision:
            return refused(f"retiring principal '{echo_name(name)}' was not confirmed")
        principals = state.store.load()["principals"]
        raw_name, matches = _retire_key(principals, name)
        if raw_name is None and len(matches) > 1:
            return failure(  # type: ignore[return-value]
                "ambiguous_name",
                f"refusing to retire principal '{echo_name(name)}': "
                f"{len(matches)} entries render as that name, so it does not "
                "identify one identity",
                next_action="remove the intended entry by editing the identity file directly",
                name=echo_name(name))
        target = principals.get(raw_name) if raw_name is not None else None
        # A row that is not shaped like a principal is still removable: it carries
        # no role, so it is never the last admin, and remove_principal (a dict
        # pop) reaches it. Only dict rows are inspected; anything else skips the
        # guard instead of failing on it.
        target_role = target.get("role") if isinstance(target, dict) else None
        if (target is not None and target_role == Role.admin.value
                and state.store.admins() == [raw_name]):
            return failure(  # type: ignore[return-value]
                "last_admin",
                f"refusing to retire principal '{echo_name(name)}': it is the last admin, "
                "and nobody could issue another",
                next_action="issue another admin with orbit_auth_issue(name, role, ttl), "
                            "then retire this one",
                name=echo_name(name))
        gone = raw_name if raw_name is not None else name
        removed = state.store.remove_principal(gone)
        # Swept unconditionally: tokens recorded under a name with no principal
        # (no product path makes one today) must stop working all the same.
        revoked = 0
        known = state.revocations.read()
        for jti, tok in state.store.load()["tokens"].items():
            if tok.get("sub") == gone and jti not in known:
                state.revocations.add(jti)
                known.add(jti)
                revoked += 1
        return Mutation(status="applied",
                        steps=[Step(name="confirm", status="ok"),
                               Step(name="principals", status="ok",
                                    detail="removed" if removed is not None else "not present"),
                               Step(name="revocations", status="ok",
                                    detail=f"{revoked} appended" if revoked else
                                    ("already revoked" if removed is not None else "nothing to revoke"))],
                        evidence={"name": echo_name(name), "known_principal": removed is not None,
                                  "revoked_tokens": revoked})

    @registry.tool(role=Role.admin, read_only=True, idempotent=True)
    def orbit_auth_list(
        ctx: Context,
        limit: Annotated[int, Field(ge=1, le=AUTH_LIST_CAP,
                                    description="Page size for the tokens listing; capped at 500.")] = AUTH_LIST_CAP,
        cursor: Annotated[str | None, Field(pattern=r"^[0-9]{1,9}$",
                                            description="Tokens pagination cursor from a previous next_cursor.")] = None,
    ) -> AuthListing:
        """List every principal and every access token this server has issued, with
        revocation state. Read-only. Two listings come back: `principals` (all of them)
        and `tokens` (paged: `limit`, `cursor`, newest first); the `jti` values are what
        orbit_auth_revoke accepts. Example: orbit_auth_list()."""
        from .tools_viewer.common import paginate

        data = state.store.load()
        revoked = state.revocations.read()
        principals = [_listed_row(n, p) for n, p in sorted(data["principals"].items())]
        rows = sorted(data["tokens"].items(), key=lambda kv: _token_issued(kv[1]), reverse=True)
        tokens = [_token_row(j, t, j in revoked) for j, t in rows]
        return AuthListing(principals=Listing.of([p.model_dump() for p in principals]),
                           tokens=paginate([t.model_dump() for t in tokens],
                                           cursor=cursor, limit=limit, cap=AUTH_LIST_CAP))


__all__: list[str] = ["register", "confirmation", "failure", "AuthListing"]
