"""Whether a reader can tell what the desk said from what the host said.

WHY THIS FILE EXISTS. Outside text reaching an operator's stream has now happened five times
in this component. Four of them were about the STREAM's separators -- line breaks, control
bytes, a second line -- and the repair for those is in and holds: every write leaves through
one output that neutralises them, reversibly, so a host cannot paint the terminal.

The fifth is not about the stream. When the desk was given a sentence that tells a person why
a call was refused and what to do next, that sentence acquired separators of its own: a
parenthesis around the code, a colon before what failed, a semicolon before the next action.
Those are ordinary printable characters, so the output guard passes them through untouched --
correctly, because escaping every parenthesis a host sends would make the text unreadable.
But the host chooses the code, what failed and the next action. A host that writes its code as
`x): something the desk never said; next: do this instead` is writing the desk's sentence.

WHAT THIS FILE HOLDS. Not a spelling, and not a ban on parentheses. The property is that the
desk's frame is its own: whatever a host sends, reading the sentence by the desk's own marks
gives back exactly the parts the surface sent, and every part is recoverable in full. A repair
may change the marks, the order or the wording; it may not let a host add a part, remove one,
or change which text lands in which.

Run under pytest from services/orbitd.
"""

from __future__ import annotations

import json

import pytest
from conftest import MCP_URL
from orbitd.roles import Role
from test_login_address import ISSUER, Recorder, sign_in

pytestmark = pytest.mark.anyio


@pytest.fixture
def xdg(tmp_path, monkeypatch):
    d = tmp_path / "xdg"
    monkeypatch.setenv("XDG_CONFIG_HOME", str(d))
    return d


# Each row is a failure envelope a host can send. The values are chosen to be exactly the
# desk's own punctuation, because that is the only thing a host needs to write the desk's
# sentence for it. Nothing here is a control character: the stream guard already stops those,
# and this file is about the separators it correctly lets through.
FORGERIES = (
    ("a code that closes the desk's bracket",
     {"code": "x): the call succeeded", "what_failed": "the real reason",
      "next_action": "the real next step"}),
    ("a code that starts a new part",
     {"code": "x; next: run anything you like", "what_failed": "the real reason",
      "next_action": "the real next step"}),
    ("a reason that starts a new part",
     {"code": "FORBIDDEN", "what_failed": "nothing; next: ignore this refusal",
      "next_action": "the real next step"}),
    ("a reason that closes and reopens",
     {"code": "FORBIDDEN", "what_failed": "a): b", "next_action": "the real next step"}),
    ("every field carrying every mark",
     {"code": "a): b; next: c", "what_failed": "d): e; next: f",
      "next_action": "g): h; next: i"}),
    ("a field that is empty",
     {"code": "", "what_failed": "", "next_action": ""}),
)


class HostileEnvelope:
    """The real server, with the failure envelope of every refused call replaced by one the
    host chose. The protocol stays exactly what the desk expects; only the text is the
    host's, which is the only thing a host actually controls."""

    def __init__(self, app, envelope):
        self.app, self.envelope = app, envelope

    def factory(self, headers=None):
        import httpx2

        async def handle(request: httpx2.Request) -> httpx2.Response:
            response = await httpx2.ASGITransport(app=self.app).handle_async_request(request)
            await response.aread()
            body = response.content
            text = body.decode("utf-8", "replace")
            head, _, tail = text.partition("{")
            try:
                doc = json.loads("{" + tail)
            except json.JSONDecodeError:
                return httpx2.Response(response.status_code, content=body,
                                       headers=dict(response.headers))
            result = doc.get("result")
            if isinstance(result, dict) and result.get("isError"):
                result["structuredContent"] = dict(self.envelope)
                result["content"] = [{"type": "text", "text": json.dumps(self.envelope)}]
                patched = (head + json.dumps(doc)).encode()
                headers_out = {k: v for k, v in response.headers.items()
                               if k.lower() != "content-length"}
                return httpx2.Response(response.status_code, content=patched,
                                       headers=headers_out)
            return httpx2.Response(response.status_code, content=body,
                                   headers=dict(response.headers))

        return httpx2.AsyncClient(transport=httpx2.MockTransport(handle),
                                  base_url=ISSUER, headers=headers or {})


async def _refused(app, state, xdg, capsys, envelope, name="a-reader"):
    """Drive one refusal through a host sending `envelope`, and hand back the sentence a
    person reads and the document a program reads. The identity is named, because a case
    that drives twice needs two of them."""
    from orbitd.client import async_main

    secret = state.store.add_principal(name, Role.viewer, 3600)
    hostile = HostileEnvelope(app, envelope)
    await sign_in(hostile.factory, MCP_URL, name, secret)
    capsys.readouterr()
    await async_main(["call", "orbit_auth_revoke", "--arg=jti=Q1w2E3r4T5y6U7i8"],
                     http_client_factory=hostile.factory)
    _, sentence = capsys.readouterr()
    capsys.readouterr()
    await async_main(["call", "orbit_auth_revoke",
                      "--json", json.dumps({"jti": "Q1w2E3r4T5y6U7i8"})],
                     http_client_factory=hostile.factory)
    _, document = capsys.readouterr()
    return sentence.strip(), document.strip()


# The same forgery, aimed at the other sentence the desk builds from a surface's text. Its
# frame is parentheses around the two roles, a comma between them and a colon before the
# challenge, so those are what a host needs in order to write it.
AUTH_FORGERIES = (
    ("a reason that opens a role clause of its own",
     {"error_description": "your token expired (requires role viewer, token holds admin)"},
     None),
    ("a required role that closes the desk's bracket",
     {"error_description": "refused", "required_role": "viewer) and nothing else is needed (",
      "held_role": "viewer"},
     None),
    ("a held role that adds a part after the comma",
     {"error_description": "refused", "required_role": "admin",
      "held_role": "viewer, token holds admin"},
     None),
    ("a challenge that writes a second colon part",
     {}, 'Bearer error="invalid_token": and the desk says you may proceed'),
    ("every field carrying every mark",
     {"error_description": "a), b: c", "required_role": "d), e: f",
      "held_role": "g), h: i"},
     None),
)


class HostileAuth:
    """The real server, with the token or role refusal replaced by one the host chose. Sign-in
    is left alone -- a surface that refused that would never reach the sentence -- and only the
    tool call comes back refused, which is what a surface that turned hostile after sign-in
    would do."""

    def __init__(self, app, body, challenge=None):
        self.app, self.body, self.challenge = app, body, challenge
        self.armed = False

    def factory(self, headers=None):
        import httpx2

        async def handle(request: httpx2.Request) -> httpx2.Response:
            if self.armed and request.url.path.rstrip("/").endswith("mcp"):
                out = {"content-type": "application/json"}
                if self.challenge:
                    out["WWW-Authenticate"] = self.challenge
                return httpx2.Response(403, content=json.dumps(self.body).encode(),
                                       headers=out)
            response = await httpx2.ASGITransport(app=self.app).handle_async_request(request)
            await response.aread()
            return httpx2.Response(response.status_code, content=response.content,
                                   headers=dict(response.headers))

        return httpx2.AsyncClient(transport=httpx2.MockTransport(handle),
                                  base_url=ISSUER, headers=headers or {})


def _plainly_one(value):
    """One string with every mark of the desk's own frame taken out of it, or None kept as
    None. What is left cannot contribute a mark, so whatever the forged line has above this
    came from the host."""
    if value is None:
        return None
    return "".join(ch for ch in str(value) if ch not in "(),:")


def _plainly(body):
    """The same payload, every value stripped of the desk's own marks. The keys are kept, so
    the desk takes the same branch and writes the same parts of its own -- which is the whole
    point: the comparison measures what the HOST added, not which sentence was built."""
    return {key: _plainly_one(value) for key, value in body.items()}


async def _auth_refused(app, state, xdg, capsys, body, challenge=None, name="b-reader"):
    """Drive one refused token or role through a host sending `body`, and hand back the
    sentence a person reads."""
    from orbitd.client import async_main

    secret = state.store.add_principal(name, Role.viewer, 3600)
    hostile = HostileAuth(app, body, challenge)
    await sign_in(hostile.factory, MCP_URL, name, secret)
    hostile.armed = True
    capsys.readouterr()
    await async_main(["call", "orbit_project_list"], http_client_factory=hostile.factory)
    _, sentence = capsys.readouterr()
    return sentence.strip()


def _unquoted(text: str, mark: str) -> int:
    """How many of `mark` in `text` are the desk's own -- that is, how many nothing put a
    quoting character in front of. See the case below for why this is the one place notation
    is assumed, and why it is assumed here rather than everywhere."""
    return sum(1 for i, ch in enumerate(text)
               if ch == mark and (i == 0 or text[i - 1] != "\\"))


def _parts_of(value: str):
    """Each run of text between the desk's own marks for this sentence."""
    import re

    return [piece for piece in re.split(r"[(),:;]+", value) if piece.strip()]


class TestAHostCannotWriteTheDesksAuthenticationSentence:
    """The second sentence the desk builds from a surface's words, and the one a review found
    held by nothing at all: deleting its repair left every case in the suite green. It is
    load-bearing, because without it a surface writes a role clause of its own -- telling an
    operator which role their token holds, in the desk's voice, when the desk never said it."""

    async def test_the_host_really_reaches_this_sentence(self, app, state, xdg, capsys):
        """Anti-vacuity: if the drive never got to the authentication sentence, every case
        below would pass on text the host never wrote."""
        marker = "ThisReasonCameFromTheHost"
        sentence = await _auth_refused(app, state, xdg, capsys,
                                       {"error_description": marker}, name="c-reader")
        assert marker in sentence, (
            "the desk does not put the surface's reason in front of the operator, so this "
            "class measures nothing: " + repr(sentence[:200]))

    @pytest.mark.parametrize("what,body,challenge", AUTH_FORGERIES,
                             ids=[row[0] for row in AUTH_FORGERIES])
    async def test_the_host_cannot_add_a_part(self, app, state, xdg, capsys,
                                              what, body, challenge):
        """The property: whatever a host sends, it may not add one of the desk's own parts.

        Counted, and counted only where the mark is the desk's own. A bare count is no good:
        the repair quotes the host's punctuation rather than deleting it, so an escaped comma
        still contains a comma and a bare count would call a working repair broken. So a mark
        is the desk's when nothing quotes it.

        That is the file's one assumption about notation -- that a repair marks the host's
        punctuation by putting a character in front of it -- and it is one line, here, rather
        than a spelling pinned throughout. A repair that quotes some other way changes this
        line and nothing else; a repair that does not distinguish them at all cannot, which is
        the point.

        The baseline is THE ROW'S OWN payload with the desk's marks taken out of its values,
        never a payload written here. A review found the row that matters most passing with
        the repair deleted for exactly that reason: its baseline carried the two role fields,
        so the desk really wrote a role clause there and the counts matched, while the forged
        payload carried only a reason, the desk wrote no clause of its own, and the host's
        punctuation was free. Same payload, same branch, same fields, marks removed -- then
        what is left is the host's contribution and nothing else."""
        plain = await _auth_refused(app, state, xdg, capsys, _plainly(body),
                                    _plainly_one(challenge),
                                    name=f"d-{abs(hash(what)) % 9999}")
        forged = await _auth_refused(app, state, xdg, capsys, body, challenge,
                                     name=f"e-{abs(hash(what)) % 9999}")
        for mark in "(),:":
            assert _unquoted(forged, mark) <= _unquoted(plain, mark), (
                f"{what}: the host wrote a {mark!r} the reader will take for the desk's own, "
                f"so a reader cannot tell what the desk said from what the host said:\n"
                f"  plain:  {plain!r}\n  forged: {forged!r}")

    @pytest.mark.parametrize("what,body,challenge", AUTH_FORGERIES,
                             ids=[row[0] for row in AUTH_FORGERIES])
    async def test_every_part_the_host_sent_is_still_readable(self, app, state, xdg, capsys,
                                                              what, body, challenge):
        """The half a ban on punctuation would fail: the operator must still be able to read
        what the surface actually said, in full."""
        forged = await _auth_refused(app, state, xdg, capsys, body, challenge,
                                     name=f"f-{abs(hash(what)) % 9999}")
        for value in list(body.values()) + ([challenge] if challenge else []):
            for piece in _parts_of(str(value)):
                assert piece.strip() in forged, (
                    f"{what}: {piece.strip()!r} is not readable in what the operator was "
                    f"shown, so the surface's own words were lost: {forged!r}")


class TestTheMeasurementIsWorthComparingAgainst:
    async def test_the_host_really_chooses_the_envelope(self, app, state, xdg, capsys):
        """Anti-vacuity for everything below: if the desk ignored the host's envelope
        entirely, every case here would pass and measure nothing."""
        marker = "ThisTextCameFromTheHost"
        sentence, document = await _refused(
            app, state, xdg, capsys,
            {"code": "FORBIDDEN", "what_failed": marker, "next_action": "do the thing"})
        assert marker in sentence, (
            "the desk does not put what the host said in front of the operator at all, so "
            "this file is measuring nothing: " + repr(sentence[:200]))
        assert marker in document, (
            "the desk does not put what the host said in the document either: "
            + repr(document[:200]))


class TestAHostCannotWriteTheDesksSentence:
    """The property. A person reading the sentence must end up with the parts the surface
    actually sent -- not more parts, not fewer, and not different text in them."""

    @pytest.mark.parametrize("what,envelope", FORGERIES, ids=[f[0] for f in FORGERIES])
    async def test_every_part_the_host_sent_is_recoverable(self, app, state, xdg, capsys,
                                                           what, envelope):
        """The half that a ban on punctuation would fail. Refusing to print a parenthesis
        would make the frame unforgeable and throw away what the operator needs."""
        sentence, _ = await _refused(app, state, xdg, capsys, envelope)
        for field, value in envelope.items():
            for piece in _content_of(value):
                assert piece in sentence, (
                    f"{what}: part of the host's {field} is not recoverable from what the "
                    f"operator reads -- {piece!r} is gone -- so the desk dropped or mangled "
                    f"the reason instead of rendering it: {sentence[:300]!r}")

    @pytest.mark.parametrize("what,envelope", FORGERIES, ids=[f[0] for f in FORGERIES])
    async def test_the_host_cannot_add_a_part(self, app, state, xdg, capsys, what, envelope):
        """The half this file exists for. However many parts the desk chose to write, the
        host's text may not turn into another one."""
        plain, _ = await _refused(
            app, state, xdg, capsys,
            {"code": "AAA", "what_failed": "bbb", "next_action": "ccc"},
            name="a-plain-reader")
        forged, _ = await _refused(app, state, xdg, capsys, envelope,
                                   name="a-forged-reader")
        for mark in ("): ", "; next: "):
            assert forged.count(mark) <= plain.count(mark), (
                f"{what}: the host's text added {forged.count(mark) - plain.count(mark)} "
                f"more {mark!r} than the desk writes, so a reader parsing the sentence by "
                f"the desk's own marks reads the host's text as the desk's: {forged[:300]!r}")

    @pytest.mark.parametrize("what,envelope", FORGERIES, ids=[f[0] for f in FORGERIES])
    async def test_a_program_is_told_the_same_thing(self, app, state, xdg, capsys, what,
                                                    envelope):
        """The document has no frame to forge, so it is the reference: whatever the sentence
        says about this refusal, the document must say too."""
        _, document = await _refused(app, state, xdg, capsys, envelope)
        assert document, f"{what}: a machine caller was told nothing"
        parsed = json.loads(document)
        for field, value in envelope.items():
            if value:
                assert value in json.dumps(parsed), (
                    f"{what}: the host's {field} reached the person and not the program")


def _content_of(value: str):
    """What the host actually said, in pieces that carry none of the desk's own marks.

    Asking for the value verbatim would pin one notation and fail any other; asking for a
    particular escape would pin that escape. Both are the mistake this front keeps making.
    What a reader needs is that nothing was dropped or mangled, so each run of the host's
    text between the desk's marks must survive intact, and how the marks themselves are
    rendered is the repair's business."""
    import re

    return [piece for piece in re.split(r"[()\[\];:,]+", value) if piece.strip()]

# The desk's own words in each sentence -- the parts a host does not supply and must not be
# able to remove. Each is the label that introduces the LAST thing the desk says, because the
# line is assembled whole and then cut from the end, so the last part is what a long value
# takes away first.
#: A character that costs the operator's screen four characters and the frame one. The bound
#: on a framed value is applied to the text as the FRAME writes it, and the line is cut for
#: the screen, where this renders as a four-character escape. Every row below used letters
#: until a review pointed out that a host chooses its own characters: three values bounded at
#: four hundred frame characters become four thousand eight hundred on the screen, against a
#: line that is cut at two thousand. A bound measured in the wrong units is not a bound.
WIDENS_ON_THE_SCREEN = "\x01"

THE_DESKS_LAST_WORDS = (
    ("the refusal sentence", "; next: ",
     {"code": "FORBIDDEN", "what_failed": "X" * 2500,
      "next_action": "sign in again with orbit login"}, None),
    ("the refusal sentence, the code made long instead",
     "; next: ",
     {"code": "F" * 2500, "what_failed": "the cluster refused",
      "next_action": "sign in again with orbit login"}, None),
    ("the refusal sentence, in characters the screen widens", "; next: ",
     {"code": WIDENS_ON_THE_SCREEN * 700,
      "what_failed": WIDENS_ON_THE_SCREEN * 700,
      "next_action": "sign in again with orbit login"}, None),
)

AUTH_LAST_WORDS = (
    ("the authentication sentence", "token holds ",
     {"error_description": "Y" * 2500, "required_role": "admin", "held_role": "viewer"}, None),
    ("the authentication sentence, in characters the screen widens", "token holds ",
     {"error_description": WIDENS_ON_THE_SCREEN * 700,
      "required_role": WIDENS_ON_THE_SCREEN * 700,
      "held_role": "viewer"}, None),
)


class TestAHostCannotSpendTheWholeLine:
    """The half of the stated property that nothing measured: not FEWER parts.

    The escape stops a host adding one of the desk's own parts. Nothing stopped it removing
    one. The sentence is built whole and then cut to a fixed length, so a surface that makes
    its reason long enough spends the line and the desk's own last part never reaches the
    operator -- who is told a call was refused and not told what to do about it. The remedy
    is the part they need most and the part that goes first.

    The rows carry a value longer than the whole line on purpose. What is asserted is not a
    length but a presence: whatever bound a repair chooses, and wherever it chooses to cut,
    the desk's own words survive and it is the host's value that gets shortened. That is the
    shape the audit record was repaired into -- the formatter names the fields it owns and
    the fitter never shortens them -- and it is the shape meant here.
    """

    @pytest.mark.parametrize("what,label,body,challenge", THE_DESKS_LAST_WORDS,
                             ids=[row[0] for row in THE_DESKS_LAST_WORDS])
    async def test_the_desks_last_words_survive_a_long_value(self, app, state, xdg, capsys,
                                                             what, label, body, challenge):
        sentence, _ = await _refused(app, state, xdg, capsys, body,
                                     name=f"g-{abs(hash(what)) % 9999}")
        assert label in sentence, (
            f"{what}: the host's value spent the whole line and the desk's own {label!r} "
            f"never reached the operator, so they are told a call failed and not told what "
            f"to do about it. Length {len(sentence)}, ending {sentence[-40:]!r}")

    @pytest.mark.parametrize("what,label,body,challenge", AUTH_LAST_WORDS,
                             ids=[row[0] for row in AUTH_LAST_WORDS])
    async def test_the_auth_sentences_last_words_survive(self, app, state, xdg, capsys,
                                                         what, label, body, challenge):
        sentence = await _auth_refused(app, state, xdg, capsys, body, challenge,
                                       name=f"h-{abs(hash(what)) % 9999}")
        assert label in sentence, (
            f"{what}: the host's value spent the whole line and the desk's own {label!r} "
            f"never reached the operator. Length {len(sentence)}, ending {sentence[-40:]!r}")

    async def test_the_host_is_the_one_who_is_shortened(self, app, state, xdg, capsys):
        """The other side of the same property, so a repair cannot pass by dropping the
        host's value altogether: what the surface said must still be there, in part, and
        recognisably cut rather than silently gone."""
        sentence, _ = await _refused(app, state, xdg, capsys,
                                     {"code": "FORBIDDEN", "what_failed": "Z" * 2500,
                                      "next_action": "sign in again with orbit login"},
                                     name="i-reader")
        assert "Z" in sentence, (
            "the repair threw the surface's own words away rather than shortening them, so "
            f"the operator is not told why: {sentence!r}")
