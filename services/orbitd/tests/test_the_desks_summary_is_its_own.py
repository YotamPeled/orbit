"""Whether a reader can tell what the desk said from what the host said, on a call that WORKED.

WHY THIS FILE EXISTS. The two sentences the desk builds when something goes wrong -- the
refusal and the authentication sentence -- were each found to have separators of their own that
a host could write, and both were repaired. A review then pointed at the third, which nobody had
looked at because it is on the path where nothing went wrong.

When a tool answers successfully with a payload carrying a status, the desk writes one line: the
tool's name, a colon, the status, and then whichever of version, code and what_failed the
payload carried, each as a `key=value` pair separated by spaces. The colon, the spaces and the
equals signs are the desk's. All four values are the surface's.

So a surface answering a successful call with a version of `2 code=FORBIDDEN what_failed=the
cluster is gone` makes the desk write a line that reads as a failure while it exits 0 and never
reported one. The operator has a second signal -- the exit code contradicts the line -- but they
are reading the line, and the line is in the desk's voice.

WHAT MAKES IT REACHABLE. Every tool declares the shape of what it returns, and the desk refuses
an answer that does not fit that shape. But the check is on the declared fields only: a payload
that carries the shape AND four more fields of its own is accepted whole, and the summary reads
those four. So this is not confined to the tools that answer with a status -- a plain listing,
which has no status at all, can be made to print one, and the rows below drive exactly that.

WHAT THIS FILE HOLDS. Not a spelling, and not a ban on equals signs: a version string with an
equals sign in it is ordinary, and refusing to print it would make the line useless. The
property is that the desk's own parts stay the desk's -- whatever a host sends, it may not add a
labelled part the desk did not write -- and that everything it did send is still readable.

Run under pytest from services/orbitd.
"""

from __future__ import annotations

import json

import pytest
from conftest import MCP_URL
from orbitd.roles import Role
from test_login_address import ISSUER, sign_in

pytestmark = pytest.mark.anyio


@pytest.fixture
def xdg(tmp_path, monkeypatch):
    d = tmp_path / "xdg"
    monkeypatch.setenv("XDG_CONFIG_HOME", str(d))
    return d


# The desk's own labels in this line. A host may not produce one; the desk writes each at most
# once, from a payload field of that name.
DESK_PARTS = ("version=", "code=", "what_failed=")

# What the tool being driven really promises to return. Every row below carries it, because a
# payload that does not fit the declared shape never reaches the summary at all -- the desk
# refuses it first. Carrying it is what makes each row an attack on the summary rather than a
# test of the shape check in front of it.
DECLARED = {"items": [], "total": 0, "returned": 0, "truncated": False}

# Each row is the rest of a payload a host can answer a SUCCESSFUL call with. The values are
# made of the desk's own punctuation, because that is the only thing a host needs in order to
# write the desk's line for it.
FORGERIES = (
    ("a version that adds two parts of its own",
     {"status": "ok", "version": "2 code=FORBIDDEN what_failed=the cluster is gone"}),
    ("a status that adds a part",
     {"status": "ok code=FORBIDDEN", "version": "2"}),
    ("a what_failed that adds a part after itself",
     {"status": "ok", "what_failed": "nothing code=FORBIDDEN"}),
    ("a code that reopens the line",
     {"status": "ok", "code": "OK version=99 what_failed=everything"}),
    ("every field carrying every mark",
     {"status": "a code=b", "version": "c what_failed=d", "code": "e version=f",
      "what_failed": "g code=h"}),
    ("a value that is ordinary and contains an equals sign",
     {"status": "ok", "version": "sha256=abc123"}),
)


class HostileSuccess:
    """The real server, with the payload of every SUCCESSFUL tool answer replaced by one the
    host chose. The protocol stays exactly what the desk expects and the call still succeeds;
    only the text is the host's, which is the only thing a host actually controls."""

    def __init__(self, app, payload):
        self.app, self.payload = app, payload

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
            if isinstance(result, dict) and "content" in result and not result.get("isError"):
                result["structuredContent"] = dict(self.payload)
                result["content"] = [{"type": "text", "text": json.dumps(self.payload)}]
                patched = (head + json.dumps(doc)).encode()
                out = {k: v for k, v in response.headers.items()
                       if k.lower() != "content-length"}
                return httpx2.Response(response.status_code, content=patched, headers=out)
            return httpx2.Response(response.status_code, content=body,
                                   headers=dict(response.headers))

        return httpx2.AsyncClient(transport=httpx2.MockTransport(handle),
                                  base_url=ISSUER, headers=headers or {})


async def _succeeded(app, state, xdg, capsys, extra, name="s-reader"):
    """Drive one successful call through a host answering with the declared shape plus `extra`,
    and hand back the line a person reads and the exit the desk chose."""
    from orbitd.client import async_main

    secret = state.store.add_principal(name, Role.viewer, 3600)
    hostile = HostileSuccess(app, {**DECLARED, **extra})
    await sign_in(hostile.factory, MCP_URL, name, secret)
    capsys.readouterr()
    code = await async_main(["call", "orbit_project_list"],
                            http_client_factory=hostile.factory)
    out, _ = capsys.readouterr()
    return out.strip(), code


def _unquoted(text: str, part: str) -> int:
    """How many of `part` in `text` are the desk's own -- that is, how many nothing quoted.

    This is the file's one assumption about notation, and it is here rather than in each case:
    a backslash before a part, or inside it before the equals sign, means the desk marked that
    part as the host's text rather than its own label. A repair that quotes some other way
    changes this function and nothing else; a repair that does not distinguish the desk's parts
    from the host's cannot make any of the rows below pass.
    """
    found, at = 0, text.find(part)
    while at != -1:
        if at == 0 or text[at - 1] != "\\":
            found += 1
        at = text.find(part, at + 1)
    return found


def _plainly(extra):
    """The same payload with every value stripped of anything the desk could mistake for its
    own punctuation. Every row is counted against this rather than against a number written
    here, so the property is `no more parts than the surface really sent`."""
    return {key: "x" for key in extra}


class TestTheMeasurementIsWorthComparingAgainst:
    async def test_the_host_really_chooses_the_payload(self, app, state, xdg, capsys):
        """Anti-vacuity for everything below. It also states the reachability the file rests
        on: a tool that promises a listing, with no status anywhere in what it declares, prints
        a status of the host's choosing, because the shape check reads the declared fields and
        lets the rest through."""
        marker = "ThisStatusCameFromTheHost"
        line, code = await _succeeded(app, state, xdg, capsys, {"status": marker},
                                      name="t-reader")
        assert marker in line, (
            "the desk does not put the surface's status in front of the operator at all, so "
            "this file measures nothing: " + repr(line[:200]))
        assert code == 0, f"the call did not succeed, so this is not the success path: {code}"

    async def test_the_desk_writes_its_parts_when_the_payload_carries_them(self, app, state,
                                                                          xdg, capsys):
        """And the parts being defended are really written, or the rows below compare nothing
        against nothing."""
        line, _ = await _succeeded(app, state, xdg, capsys,
                                   {"status": "ok", "version": "2", "code": "OK",
                                    "what_failed": "none"}, name="u-reader")
        for part in DESK_PARTS:
            assert part in line, f"the desk did not write {part!r} at all: {line!r}"


class TestAHostCannotWriteTheDesksSummaryLine:
    """The property. A person reading the line must end up with the parts the surface actually
    sent -- not more parts, not fewer, and not different text inside them."""

    @pytest.mark.parametrize("what,extra", FORGERIES, ids=[f[0] for f in FORGERIES])
    async def test_the_host_cannot_add_a_part(self, app, state, xdg, capsys, what, extra):
        who = abs(hash(what)) % 9999
        was, _ = await _succeeded(app, state, xdg, capsys, _plainly(extra), name=f"v-{who}")
        forged, code = await _succeeded(app, state, xdg, capsys, extra, name=f"w-{who}")
        assert code == 0, "the call stopped succeeding, so this is no longer the success path"
        for part in DESK_PARTS:
            assert _unquoted(forged, part) <= _unquoted(was, part), (
                f"{what}: the host wrote the desk's own {part!r} into a line the desk signs, so "
                f"an operator reads a failure the desk never reported while it exits 0:\n"
                f"  plain:  {was!r}\n  forged: {forged!r}")

    @pytest.mark.parametrize("what,extra", FORGERIES, ids=[f[0] for f in FORGERIES])
    async def test_every_value_the_host_sent_is_still_readable(self, app, state, xdg, capsys,
                                                               what, extra):
        """The half a ban on equals signs would fail: a version really can contain one, and the
        operator must still be able to read what the surface actually said."""
        forged, _ = await _succeeded(app, state, xdg, capsys, extra,
                                     name=f"y-{abs(hash(what)) % 9999}")
        for value in extra.values():
            for piece in str(value).replace("=", " ").split():
                assert piece in forged, (
                    f"{what}: {piece!r} is not readable in what the operator was shown, so the "
                    f"surface's own words were lost: {forged!r}")

class TestAHostCannotSpendTheWholeLine:
    """The half of the stated property above that the rows above do not measure: not FEWER
    parts. The escaping stops a host adding one of the desk's own labels. Nothing stops it
    removing one -- the line is built whole and then cut to a fixed length, so a value long
    enough spends the line and the labels after it never reach the operator.

    Asserted as a presence and not a length: whatever bound a repair picks and wherever it
    cuts, the desk's own labels survive and it is the host's value that gets shortened."""

    async def test_the_labels_after_a_long_value_survive(self, app, state, xdg, capsys):
        line, _ = await _succeeded(app, state, xdg, capsys,
                                   {"status": "ok", "version": "V" * 2500,
                                    "code": "OK", "what_failed": "none"},
                                   name="z-reader")
        for part in ("code=", "what_failed="):
            assert part in line, (
                f"the host's version spent the whole line and the desk's own {part!r} never "
                f"reached the operator, so a reader sees a status and not the rest of what "
                f"the surface actually said. Length {len(line)}, ending {line[-40:]!r}")

    async def test_the_labels_survive_characters_the_screen_widens(self, app, state, xdg,
                                                                    capsys):
        """The same row in the characters a host would actually choose. The bound is applied
        to the value as the FRAME writes it and the line is cut for the SCREEN, where one of
        these renders as a four-character escape -- so a value inside the bound can still
        spend the whole line. Every row holding this bound used letters until a review
        pointed that out."""
        line, _ = await _succeeded(app, state, xdg, capsys,
                                   {"status": "ok", "version": "\x01" * 700,
                                    "code": "\x01" * 700, "what_failed": "none"},
                                   name="w-reader")
        for part in ("code=", "what_failed="):
            assert part in line, (
                f"a host answered in characters that cost four each on the screen, stayed "
                f"inside the bound, and still took the desk's own {part!r} off the line. "
                f"Length {len(line)}, ending {line[-40:]!r}")

    async def test_the_host_is_the_one_who_is_shortened(self, app, state, xdg, capsys):
        """So a repair cannot pass by throwing the host's value away instead."""
        line, _ = await _succeeded(app, state, xdg, capsys,
                                   {"status": "ok", "version": "W" * 2500, "code": "OK"},
                                   name="y2-reader")
        assert "W" in line, (
            "the repair discarded the surface's own words rather than shortening them, so "
            f"the operator is not told what version it answered with: {line!r}")
