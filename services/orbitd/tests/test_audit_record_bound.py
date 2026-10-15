"""The ceiling has to be a ceiling.

The audit module states the bound without qualification: one record is bounded as a whole,
counted in bytes on the wire. The fitting loop keeps that promise only when the record's
weight is in a top-level string value. It considers a field a candidate only if the value
is a non-empty string, so a record carrying a list, a mapping, a long key, or an object
whose text form is long has nothing to shrink: the loop finds no candidate, breaks, and
returns the line as it stands, over the bound, with no mark and no complaint.

Two shapes are worse than over-long. A value the encoder cannot encode raises inside the
fitting, which is not inside the formatter's guard, and the handler's failure route is
silent by design -- so the record is lost with no trace anywhere.

None of this is reachable from the surface today. Both gates convert a caller's tool name
to text before logging, every call site passes strings, and every key is chosen by code.
That is why these cases are written against the fitting directly rather than driven through
the server: the guarantee is stated unconditionally, the condition that makes it true is
invisible at the call sites that have to preserve it, and a single future call site passing
a list is enough to break it silently.

A review found this by construction, not by observation, and said so. These cases say the
same thing in a form that fails.

ONE CASE THAT BELONGS HERE AND LIVES ELSEWHERE. When the fitter cuts a long key, keys that
share a prefix cut to the same text and two fields want one slot. That case was deferred from
this file while the loop's behaviour under collision was still wrong, and the note saying so
was deleted in a later round rather than replaced by the case -- which is how a deferral
becomes an omission. The behaviour was repaired and the case exists: it is
`test_two_colliding_keys_keep_both_values` in test_a_cut_record_is_still_a_record.py, and it
goes red when the repair is removed. The note stays here so a reader of this file does not
look for it and conclude it was never written.
"""

from __future__ import annotations

import json

import pytest

from orbitd import audit


class Unstringable:
    def __str__(self):
        raise RuntimeError("this value refuses to become text")


WEIGHTY = (
    ("a list value", {"actor": "a", "verdict": "ok", "tool": ["x" * 50_000]}),
    ("a mapping value", {"actor": "a", "verdict": "ok", "tool": {"name": "x" * 50_000}}),
    ("a long key", {"actor": "a", "verdict": "ok", "x" * 50_000: "a value"}),
    ("a long protected field", {"actor": "a", "verdict": "x" * 50_000}),
    ("many short fields", {"actor": "a", "verdict": "ok",
                           **{f"f{i}": "v" for i in range(4000)}}),
)


@pytest.mark.parametrize("what,payload", WEIGHTY, ids=[w for w, _ in WEIGHTY])
def test_a_record_is_bounded_whatever_carries_its_weight(what, payload):
    line = audit._fit(dict(payload))
    size = len(line.encode("utf-8"))
    assert size <= audit.MAX_RECORD_LEN, (
        f"a record whose weight is {what} came out at {size} bytes against a "
        f"{audit.MAX_RECORD_LEN} ceiling, with nothing to say it was cut")


@pytest.mark.parametrize("what,payload", WEIGHTY, ids=[w for w, _ in WEIGHTY])
def test_it_is_still_one_readable_record(what, payload):
    """Anti-vacuity: returning an empty string, or a fragment, would satisfy the bound and
    destroy the stream. Whatever the fitting does to make room, what comes out is one line
    a reader can decode."""
    line = audit._fit(dict(payload))
    assert "\n" not in line.rstrip("\n"), f"a record whose weight is {what} became several lines"
    json.loads(line)


@pytest.mark.parametrize("payload,what", [
    ({"actor": "a", "verdict": "ok", "tool": 10 ** 20_000}, "a number too long to encode"),
    ({"actor": "a", "verdict": "ok", "tool": Unstringable()}, "a value that refuses to become text"),
])
def test_a_record_the_encoder_cannot_take_is_not_lost(payload, what):
    """The worse half. These raise inside the fitting, which sits outside the formatter's
    guard, and the handler's failure route is silent by design -- so the record disappears
    with no trace on any stream. An audit trail that drops a record silently is worse than
    one that writes an ugly record."""
    try:
        line = audit._fit(dict(payload))
    except Exception as exc:  # noqa: BLE001 -- the point of the case
        raise AssertionError(
            f"{what} made the fitting raise {type(exc).__name__}, and the record is then "
            "lost with no trace anywhere") from None
    assert len(line.encode("utf-8")) <= audit.MAX_RECORD_LEN
    json.loads(line)


def test_the_decision_survives_whatever_had_to_go():
    """The rule every round of this node has kept: whatever the fitting drops to make room,
    the decision is not it. A last-resort clamp must not be a licence to lose the verdict."""
    line = audit._fit({"actor": "an-actor", "verdict": "unknown_tool",
                       "tool": ["x" * 50_000]})
    assert json.loads(line)["verdict"] == "unknown_tool", line[:300]


def test_an_ordinary_record_is_untouched():
    """Anti-vacuity for the whole file: a fitting that rewrote every record would pass
    everything above."""
    payload = {"actor": "a-reader", "role": "viewer", "tool": "orbit_project_list",
               "verdict": "ok"}
    assert json.loads(audit._fit(dict(payload))) == payload
