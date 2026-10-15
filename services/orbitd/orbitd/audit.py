"""The process's one logging sink.

One formatter on the root logger emits every record — orbitd's own, the
vendored transport's, a library nobody has read — as exactly one line of JSON.
Caller text lands inside a JSON string value (with ``=`` escaped as
``\\u003d``, which decodes back to ``=``), so a separator inside it cannot end
the line on the raw stream. The encoding is reversible, so a reader can tell a
caller's own question mark from a neutralised byte. An exception travels in a
field of the same record, and one record is bounded as a whole
(:data:`MAX_RECORD_LEN`, counted in bytes on the wire).

An audit decision is fields, not a sentence. Earlier the record carried the
audit line as ``actor=... ... verdict=...`` inside ``message``; after a reader
decoded it, the caller's text and the format's text were the same kind of
thing, so a caller could erase the decision with a long name or write it by
ending its name with ``verdict=ok``. Now the decision (``verdict``) and who it
is about (``actor``, ``role``, ``tool``/``resource``) are top-level fields set
by the code through :func:`log_audit`, which a caller cannot occupy, while
what the caller sent stays in its own field where it may say anything. Readers
must read the fields, never parse the sentence.

Whether the record is all of it is in that same company. A record too big for
the bound is cut, and it says so in :data:`TRUNCATED_FIELD`, a name the
formatter reserves on every record so a caller's field of that name never
takes its place. The mark the cutter leaves in a shortened value is only a
mark: it is text, a caller can type it, and one did — so a reader asks the
field, the same way it asks for the decision instead of parsing the sentence.

There is deliberately no per-call-site rendering step: an earlier one mapped
everything outside a small Latin set onto ``?``, which destroyed Hebrew and
Chinese names and left a reader unable to distinguish a typed ``?`` from a
neutralised byte. Call sites log caller text raw; the secret scrubber in
``deploy.py`` still redacts secret VALUES underneath this, which is secrecy,
not separator handling. A new call site that interpolates caller text into a
log format or a header directly is the defect class this module exists to
prevent — but the fix for that class lives here, once, not at every call site.
"""

from __future__ import annotations

import json
import logging
import math
import sys
import threading
import traceback
from collections.abc import Callable
from datetime import datetime, timezone
from typing import Any


#: Hard ceiling on one emitted record, in bytes on the wire including the newline.
#: A 4_000_000-character value in one log call must still leave the trace
#: around it readable; the stream contract requires under 16_384.
MAX_RECORD_LEN = 8192

#: Marker left where a record was cut to fit the bound. Plain ASCII with no
#: ``=`` so it survives the sink's own escaping unchanged.
RECORD_TRUNCATED = "...[truncated]"

#: The field a record says it was cut in. The mark above is ordinary text: it
#: is plain characters chosen to survive escaping, so a caller can type it, and
#: a caller did — with the mark sent as the value of one of its own fields, a
#: record that had dropped three hundred and sixty-two of them read as whole,
#: because the fitting looked for the mark anywhere in the record and found the
#: caller's copy. So what a reader trusts is not the mark but where it sits: a
#: field the record places on its own account and a caller cannot occupy, the
#: same standing the time and the decision already have. The formatter reserves
#: the name whether or not the record is cut (see :class:`JsonOneLineFormatter`),
#: so a sealed field of this name never takes its place; the fitting is its only
#: writer, and a reader asks this field rather than reading the line.
TRUNCATED_FIELD = "truncated"

#: Prefix once read by the sink for one audit field per LogRecord attribute,
#: set by :func:`log_audit`. The sink no longer reads it: any record attribute
#: under this prefix — set through ``extra`` by any logger, ours or a
#: library's — is ignored, so it is not a way to write a decision. Kept so the
#: forgery cases can name what they are not allowed to use.
AUDIT_PREFIX = "audit_"

#: Attribute carrying the sealed audit payload to the sink, set only by
#: :func:`log_audit`. The formatter reads the decision fields from this one
#: mapping and from nowhere else, so passing through the helper is the only
#: way to write them. (Any logger can still set an attribute of this name
#: through ``extra`` if it imports this module and copies the mapping by
#: hand — but so can it call :func:`log_audit` directly, or write its own
#: line to the stream. What this closes is the second way that needed no
#: such step: ordinary logging that happens to carry audit-shaped extras.)
_SEALED_AUDIT_ATTR = "_orbitd_audit_sealed"

#: Record fields the fitter cuts last and marks where it cuts them: what the
#: record says on its own account — the decision the surface made, and its
#: statement that it was cut. A caller cannot choose the length and thereby
#: erase either, so every shorten-able string is shortened and every droppable
#: field is dropped first; a decision longer than any record could hold is cut
#: like anything else, with the mark, because silently corrupting it is worse
#: than either. (No decision the code chooses is anywhere near that long, so in
#: practice the decision is never cut. The statement is the mark itself, which
#: the cutter refuses to shorten because the mark costs more than the cut would
#: save, so it is never cut at all.) A record fitted with no formatter behind it
#: declares no bounds, and this is what keeps its statement from being spent
#: there.
PROTECTED_FIELDS = frozenset({"verdict", TRUNCATED_FIELD})

#: Record fields the fitter shortens with the mark when it must but never drops
#: whole: who the decision is about. A record that cannot answer who acted is
#: not a record, so these survive even when everything droppable beside them is
#: gone. (They are still shortened in place, with the mark, when they carry the
#: weight themselves.)
IDENTITY_FIELDS = frozenset({"actor", "role", "tool", "resource", "needs"})

#: The bound each of the formatter's own fields carries, declared where the
#: fields are set (see :class:`JsonOneLineFormatter`) and read by the fitter
#: and the last resort alike. There is no second list to keep in step: a
#: field added to the formatter later declares its bound on the same lines
#: that set it. A ``BOUNDED`` field is sized by this code's choosing and is
#: never shortened, dropped or renamed at any size; a ``WIRE_SIZED`` field
#: carries text from outside this code, so the fitter shortens it like any
#: other value when there is no other way to fit — but still never drops or
#: renames it, because a record that cannot say where it came from is not a
#: record.
BOUNDED = True
WIRE_SIZED = False

#: The shortest non-mark core :func:`_shorten_planned` will cut a string down
#: to, in characters. Shortening past this spends what the caller sent to keep
#: the bound: past a few hundred fields every value the caller sent ends as
#: the bare mark — field names and marks, saying nothing — because once the
#: mark is on, each further cut still "saves" half of what is left. A core at
#: or under this still reads as what was sent; the pressure past it drops
#: whole fields instead, so the survivors stay readable. The verdict clamp
#: and the last resort are outside this: once the record is lost anyway, the
#: bound is the point.
_MIN_READABLE_CORE = 32

log = logging.getLogger(__name__)


def _dump(payload: dict[str, Any]) -> str:
    """Encode one record as one line: no raw newline can survive, because the
    encoder escapes every control character; ``=`` is escaped as ``\\u003d``
    (which decodes back to ``=``) so echoed caller text never reads as a
    second ``key=value`` field on the raw line. ``=`` needs no other treatment:
    it never occurs as JSON syntax, so every ``=`` in the dump is string
    content. ``allow_nan=False``: the three float values with no JSON spelling
    raise here instead of being written as ``NaN``/``Infinity``/``-Infinity``,
    which no reader implementing only JSON can parse; the sanitizer replaces
    them with text before any encode, so this raises only for a value that
    escaped it."""
    return json.dumps(payload, ensure_ascii=True, sort_keys=True, default=str,
                      allow_nan=False).replace("=", "\\u003d")


def _wire_len(value: Any) -> int:
    """How many bytes `value` costs on the wire, as this sink encodes it.

    Never raises: a value the encoder refuses counts as the heaviest thing in
    the record, so the fitter sheds it first."""
    try:
        return len(json.dumps(value, ensure_ascii=True, default=str).encode("utf-8"))
    except Exception:
        pass
    try:
        return len(str(value).encode("utf-8", "replace"))
    except Exception:
        return MAX_RECORD_LEN


def _str_wire_len(text: str) -> int:
    """Bytes one string costs inside the dump: ASCII-escaped JSON, plus the
    sink's own ``=`` escaping, which adds five bytes per ``=``."""
    return len(json.dumps(text, ensure_ascii=True).encode("utf-8")) + 5 * text.count("=")


def _encoded_size(payload: dict[str, Any]) -> int | None:
    """Bytes `payload` costs on the wire, or None when it cannot be encoded.

    The rounds plan from estimates; this is the arbiter they answer to. A
    round that did not make this number smaller is not progress, whatever it
    did."""
    try:
        return len(_dump(payload).encode("utf-8"))
    except Exception:
        return None


def _resolve(payload: dict[str, Any], path: tuple) -> Any:
    """The mapping or list `path` leads to from the record top."""
    node = payload
    for step in path:
        node = node[step]
    return node


def _iter_spots(payload: dict[str, Any]) -> list:
    """Every string in the record as ``(kind, parent path, slot, text)``.

    `kind` is ``"key"`` for a mapping key and ``"value"`` otherwise; the parent
    path leads to the mapping or list holding it and `slot` is the key or index
    within. The list is rebuilt from the live record at the start of each
    planning round, so a spot names what is there when the round decides."""
    spots: list = []

    def visit_value(value: Any, path: tuple) -> None:
        if isinstance(value, str):
            spots.append(("value", path[:-1], path[-1], value))
        elif isinstance(value, dict):
            visit_mapping(value, path)
        elif isinstance(value, list):
            for index, item in enumerate(value):
                visit_value(item, path + (index,))

    def visit_mapping(node: dict, path: tuple) -> None:
        for key, value in node.items():
            spots.append(("key", path, key, key))
            visit_value(value, path + (key,))

    visit_mapping(payload, ())
    return spots


#: Nesting deeper than this is replaced rather than walked: far below what the
#: encoder itself tolerates, so encoding the replacement always succeeds.
_MAX_SANITIZE_DEPTH = 100


def _safe_type_name(value: Any) -> str:
    """A type name safe to print: from code, never from the wire."""
    try:
        name = type(value).__name__
    except Exception:
        return "value"
    return name if isinstance(name, str) else "value"


def _int_too_long(value: int) -> bool:
    """Whether encoding `value` would hit the interpreter's digit limit."""
    try:
        limit = sys.get_int_max_str_digits()
    except Exception:
        limit = 4300
    if not limit:
        return False
    return value.bit_length() * 0.30103 + 1 > limit


def _sanitize_key(key: Any) -> str:
    """A key the dump can sort and encode: plain text, never raising.

    A string subclass comes back as an exact ``str`` with the same text: the
    dump sorts keys, and a subclass whose comparison raises would escape every
    guard below and lose the record inside the sink's silent failure route."""
    if isinstance(key, str):
        try:
            return "".join(key)
        except Exception:
            return "<unencodable key>"
    if key is None:
        return "null"
    if key is True:
        return "true"
    if key is False:
        return "false"
    if isinstance(key, int):
        if _int_too_long(key):
            try:
                return "<integer too long to encode (%d bits)>" % key.bit_length()
            except Exception:
                return "<unencodable key>"
        return str(key)
    if isinstance(key, float):
        try:
            return str(key)
        except Exception:
            return "<unencodable key>"
    try:
        text = str(key)
    except Exception:
        return "<unencodable key>"
    return text if isinstance(text, str) else "<unencodable key>"


def _sanitize_value(value: Any, seen: set, depth: int) -> Any:
    """A value the encoder can take, same shape, never raising.

    Anything already encodable comes back as itself; a value the encoder would
    refuse — an object whose text form raises, an integer past the digit
    limit, a cycle, nesting deeper than walked — comes back as a short note
    saying what it was, so the record survives it. A float with no JSON
    spelling comes back as text naming it, so the line stays readable to a
    reader implementing only JSON."""
    try:
        return _sanitize_open(value, seen, depth)
    except Exception:
        try:
            return "<unencodable %s>" % _safe_type_name(value)
        except Exception:
            return "<unencodable value>"


def _sanitize_open(value: Any, seen: set, depth: int) -> Any:
    if value is None or isinstance(value, (str, bool)):
        return value
    if isinstance(value, int):
        if _int_too_long(value):
            return "<integer too long to encode (%d bits)>" % value.bit_length()
        return value
    if isinstance(value, float):
        # The three float values with no JSON spelling come back as text
        # naming them, at any depth the walk below reaches: written raw they
        # are tokens no reader implementing only JSON accepts, and the whole
        # line — actor, decision, time — goes with the field. The name stays
        # on the field, so nothing is silently dropped.
        if math.isfinite(value):
            return value
        if math.isnan(value):
            return "NaN"
        return "Infinity" if value > 0 else "-Infinity"
    if depth >= _MAX_SANITIZE_DEPTH:
        return "<too deeply nested>"
    if isinstance(value, dict):
        if id(value) in seen:
            return "<circular reference>"
        seen.add(id(value))
        try:
            return {_sanitize_key(key): _sanitize_value(item, seen, depth + 1)
                    for key, item in value.items()}
        finally:
            seen.discard(id(value))
    if isinstance(value, (list, tuple)):
        if id(value) in seen:
            return "<circular reference>"
        seen.add(id(value))
        try:
            return [_sanitize_value(item, seen, depth + 1) for item in value]
        finally:
            seen.discard(id(value))
    try:
        text = str(value)
    except Exception:
        return "<unencodable %s>" % _safe_type_name(value)
    if not isinstance(text, str):
        return "<unencodable %s>" % _safe_type_name(value)
    return text


def _sanitize_record(payload: Any) -> dict[str, Any]:
    """The record rebuilt from plain JSON-native mappings, lists and scalars.

    Encodable values come back equal; only what would make the encoder raise
    comes back different, as a short note. Rebuilding also means the fitter
    never mutates the caller's mappings and lists while it cuts."""
    try:
        if isinstance(payload, dict):
            seen = {id(payload)}
            return {_sanitize_key(key): _sanitize_value(item, seen, 1)
                    for key, item in payload.items()}
        return {"record": _sanitize_value(payload, set(), 0)}
    except Exception:
        return {"record": "<unencodable record>"}


def _not_worth_shortening(text: str) -> bool:
    """Whether `text` as a whole is shorter than the mark that would replace
    it, so writing the mark in its place spends room to say less.

    Shortening is worth doing only when the replacement costs less than what
    it replaces. A value already shorter than the mark can never meet that:
    the verdict is the extreme case, a word of a few characters that the mark
    would spend more bytes to erase than the word itself costs. This is the
    first of the two guards :func:`_cut_string` applies, not a question any
    caller asks on its own: a route that decides keep-or-mark asks the cutter,
    which weighs the mark against what it would replace in full. Asking only
    this half is how the last resort came to erase a decision the cutter
    keeps — a word longer than the mark that still costs less than cutting
    it."""
    return len(text) < len(RECORD_TRUNCATED)


def _cut_string(text: str) -> str | None:
    """`text` shortened from the tail with the mark at the cut, or None when
    shortening would not save room (the mark costs more than the cut)."""
    if _not_worth_shortening(text):
        return None
    if text.endswith(RECORD_TRUNCATED):
        core = text[: -len(RECORD_TRUNCATED)]
    else:
        core = text
    if not core:
        return None
    new = core[: len(core) // 2] + RECORD_TRUNCATED
    if _str_wire_len(new) >= _str_wire_len(text):
        return None
    return new


#: Rounds of decide-then-encode before the last resort. Each round strictly
#: shrinks what the next round sees, so the loop cannot spin; the cap is there
#: so that a miscounted saving ends the fitting instead of the process.
_MAX_FIT_ROUNDS = 50


def _rename_key(holder: dict, old: str, new: str) -> None:
    """Move `holder[old]` to the shortened `new`, keeping every value.

    Shortened keys collide: long keys sharing a prefix cut to the same text.
    The earlier repair kept them apart with a ``~N`` suffix costing more than
    the cut saved, so each round's saving became the next round's weight and
    the loop ran out of rounds. Now colliding entries share the one shortened
    key with their values side by side as a list, which still loses nothing:
    the mark on the key says names were shortened, and every value is still
    there to read."""
    kept = holder.pop(old)
    if new == old:
        holder[old] = kept
        return
    if new in holder:
        existing = holder[new]
        holder[new] = existing + [kept] if isinstance(existing, list) else [existing, kept]
    else:
        holder[new] = kept


def _shorten_planned(payload: dict[str, Any], excess: int,
                     own: dict[str, bool] | None = None) -> bool:
    """Shorten the longest strings until the planned savings cover `excess`.

    One walk, one greedy choice of what to cut and by how much, then the cuts
    are applied deepest-first so a renamed key cannot pull the path out from
    under a nested cut decided in the same round, values before keys at the
    same depth so a renamed key cannot orphan its own row's value cut (which
    would land on the missing slot and re-insert the long key beside its
    shortened self). Replacing a string changes the encoded record by exactly
    the difference of the two wire lengths, so the savings add up. Returns
    whether the encoded record got smaller: a round that took an action
    without shrinking anything is not progress, and the loop must hear that.

    `own` maps each top-level key the formatter set itself to its bound (see
    ``BOUNDED``/``WIRE_SIZED``), declared where the fields are set. A bounded
    field is not the caller's weight at any size, so it is never a candidate
    to shorten or rename. A wire-sized one is still never renamed, but its
    value is a candidate to shorten like any other value: when one of them
    alone passes the ceiling there is nothing else to spend, and leaving it
    verbatim destroys the record it was kept to explain. Only the top level
    is exempt — a caller mapping nested deeper that happens to use the same
    name is still ordinary weight. No string is cut past ``_MIN_READABLE_CORE``
    characters of non-mark core: keeping the bound by spending everything
    readable is not a record, so the pressure past that drops whole fields
    instead and the survivors stay readable."""
    bounds = own or {}
    cands: list = []
    for kind, parent_path, slot, found in _iter_spots(payload):
        if parent_path == () and slot in bounds:
            if kind == "key":
                continue
            if bounds[slot]:
                continue
        if kind == "value" and parent_path == () and slot in PROTECTED_FIELDS:
            continue
        core = found[:-len(RECORD_TRUNCATED)] if found.endswith(RECORD_TRUNCATED) else found
        if len(core) <= _MIN_READABLE_CORE:
            continue
        new = _cut_string(found)
        if new is None:
            continue
        saving = _str_wire_len(found) - _str_wire_len(new)
        if saving <= 0:
            continue
        cands.append((saving, kind, parent_path, slot, new))
    if not cands:
        return False
    cands.sort(key=lambda cand: cand[0], reverse=True)
    planned: list = []
    saved = 0
    for cand in cands:
        planned.append(cand)
        saved += cand[0]
        if saved >= excess:
            break
    planned.sort(key=lambda cand: (-len(cand[2]), cand[1] == "key"))
    before = _encoded_size(payload)
    applied = 0
    for _, kind, parent_path, slot, new in planned:
        try:
            holder = _resolve(payload, parent_path)
        except (KeyError, IndexError, TypeError):
            continue
        try:
            if kind == "key":
                if not isinstance(holder, dict) or slot not in holder:
                    continue
                _rename_key(holder, slot, new)
            else:
                if isinstance(holder, dict):
                    # A planned cut never inserts: the slot was there at
                    # planning time, and nothing applied earlier in this
                    # round removes it.
                    if slot not in holder:
                        continue
                elif isinstance(holder, list):
                    if not isinstance(slot, int) or not 0 <= slot < len(holder):
                        continue
                else:
                    continue
                holder[slot] = new
        except (KeyError, IndexError, TypeError, AttributeError):
            continue
        applied += 1
    if not applied:
        return False
    after = _encoded_size(payload)
    if before is None or after is None:
        return True
    return after < before


def _field_saving(payload: dict[str, Any], key: str) -> int:
    """Bytes the encoded record sheds when top-level field `key` goes.

    Exact, not estimated: the pair encodes the same alone as in place, and
    every removal but the last one also sheds its comma. The earlier estimate
    undercounted the encoder's per-field cost, so one sweep ran off the end of
    the list instead of stopping at the excess."""
    try:
        full = len(_dump({key: payload[key]}).encode("utf-8"))
    except Exception:
        return _wire_len(payload[key]) + 1
    return full if len(payload) > 1 else full - 2


def _mark_cut(payload: dict[str, Any]) -> bool:
    """Say, in the record's own field, that this record was cut. Returns
    whether that changed the record, so a caller re-measures only when it did.

    The only writer of :data:`TRUNCATED_FIELD`, and idempotent: the statement
    is the same one however many rounds of cutting follow it. What it replaces
    asked instead whether the mark appeared anywhere among the record's
    strings, which a caller could answer by sending the mark as a value of its
    own — and then the record that dropped most of itself said nothing."""
    if payload.get(TRUNCATED_FIELD) == RECORD_TRUNCATED:
        return False
    payload[TRUNCATED_FIELD] = RECORD_TRUNCATED
    return True


def _drop_planned(payload: dict[str, Any], excess: int,
                 own: dict[str, bool] | None = None) -> bool:
    """Drop whole fields, largest first, until the planned savings cover
    `excess`.

    Who acted is never dropped: the identity fields survive even when
    everything droppable beside them is gone. The formatter's own fields
    (`own`, a bound per key declared where they are set) are never dropped
    either: they are not weight, and the wire-sized ones among them are
    shortened by the round above instead of dropped here. Nor is the record's
    own statement that it was cut, which the fitting places before any round
    runs; this round is pure removal and pays for nothing. Returns whether the
    encoded record got smaller."""
    bounds = own or {}
    droppable = [key for key in payload
                 if key not in PROTECTED_FIELDS and key not in IDENTITY_FIELDS
                 and key not in bounds]
    if not droppable:
        return False
    savings = sorted(((_field_saving(payload, key), key) for key in droppable),
                     reverse=True)
    if sum(saving for saving, _ in savings) < excess:
        # Shedding every droppable field would still not cover the excess,
        # so the weight sits in the decision or the identities, which drops
        # must not spend. Leave the record untouched for the round that can
        # move it; dropping now would only destroy evidence to no effect.
        return False
    before = _encoded_size(payload)
    state = dict(payload)
    saved = 0
    for saving, key in savings:
        del payload[key]
        saved += saving
        if saved >= excess:
            break
    after = _encoded_size(payload)
    if before is None or after is None:
        return True
    if after >= before:
        # Removals that did not shrink the record. Put it back and say no
        # progress: a round that changed nothing must not be counted as one.
        payload.clear()
        payload.update(state)
        return False
    return True


def _clamp_verdict(payload: dict[str, Any]) -> bool:
    """Shorten the decision one step. It is cut only here, after every
    shorten-able string is shortened and every droppable field is dropped, and
    it is marked where it was cut.

    Returns whether the decision moved. When it is down to the bare mark there
    is nothing left to halve — ``_cut_string`` answers None and this answers
    False — so the loop ends instead of re-encoding the same record forever.
    The earlier loop had no such guard: its transformation was the identity
    once the mark was all that was left, and it terminated only because the
    loop before it had already emptied the record."""
    if "verdict" not in payload:
        return False
    current = payload["verdict"]
    if not isinstance(current, str):
        try:
            rendered = json.dumps(current, ensure_ascii=True, sort_keys=True,
                                  default=str)
        except Exception:
            rendered = "<unencodable verdict>"
        if rendered == current:
            return False
        before = _encoded_size(payload)
        payload["verdict"] = rendered
        after = _encoded_size(payload)
        if before is not None and after is not None and after >= before:
            payload["verdict"] = current
            return False
        return True
    new = _cut_string(current)
    if new is None:
        return False
    payload["verdict"] = new
    return True


def _the_least_a_record_can_say() -> str:
    """The line written when even the floor could not be encoded: nothing of
    the record survives, so all it can say is that it was cut and that no
    decision came through. It says it in the record's own field, like every
    other route, rather than leaving a reader to find a mark in a value and
    guess whose it was."""
    return '{"%s": "%s", "verdict": "%s"}' % (
        TRUNCATED_FIELD, RECORD_TRUNCATED, RECORD_TRUNCATED)


def _what_the_caller_said(value: Any) -> str:
    """What the floor writes for one value the caller sent: the text itself.

    The floor asks nothing here about length. It once asked the cutter whether
    it would cut and then wrote the bare mark whenever the answer was yes,
    which spent a decision and a name to save room the record did not need —
    the floor's own line is a handful of fields, and a value the cutter would
    happily halve still leaves it far under the ceiling. Room is a question
    about the whole record, and it is asked once, below, where the line is
    measured; what is cut there is cut by the cutter, the same rule every
    other route shortens by. The bare mark is left only where there is
    genuinely nothing to keep: a value that is not text at all, whose
    rendering is the work of the rounds above, and this floor is reached when
    those have already failed."""
    if not isinstance(value, str):
        return RECORD_TRUNCATED
    return value


def _what_this_floor_may_shorten(out: dict[str, Any],
                                 bounds: dict[str, bool]) -> list[str]:
    """The keys of `out` the floor may cut a step further to hold the bound:
    everything carrying text from outside this code.

    The wire-sized own fields say where the record came from, who acted says
    who it is about, the decision says what was decided and the sentence says
    what happened; all four are the caller's text at whatever length the fitter
    let through, so all four are shortened here rather than one of them. A
    bounded own field is sized by this code and is never shortened at any
    size."""
    return [key for key, bounded in bounds.items() if not bounded] \
        + sorted(IDENTITY_FIELDS & out.keys()) + ["verdict", "message"]


def _last_resort(payload: dict[str, Any],
                 own: dict[str, bool] | None = None) -> str:
    """A record that fits no matter what the rounds above miscounted: the
    formatter's own fields verbatim, who acted and the decision as the cutter
    would write them, and the mark beside it. Reached only when the arithmetic
    above surprises; the bound holds regardless.

    Who acted and the decision are written as the caller said them, and are
    shortened only if the line then passes the ceiling — by the cutter
    (:func:`_cut_string`), the same rule every other route shortens by, never
    a width of this floor's own, which would be a second judgment to keep in
    step. What this replaces asked the cutter a question about one value and
    answered it with the bare mark: any text the cutter would halve was erased
    whole, though the floor's line is a handful of fields and had room for all
    of it. That is how the record of a deployment that landed and never became
    healthy came to say nothing about what was decided or who did it, on the
    one day a reader has nothing else to read. The bare mark is left only
    where there is genuinely nothing to keep: a value that is not text at all.
    The sentence the route wrote rides along like the decision does, because
    for a deployment it is the only field that names what was deployed where.

    The own fields ride along because a last resort that dropped the timestamp
    would repeat the defect this fitting exists to close: a record no reader
    can place in time. The bounded ones ride along verbatim, small beside the
    ceiling in every reachable case; the wire-sized ones are shortened with
    the mark until the record fits, because copying them verbatim is the
    overflow this resort exists to survive. Which is which comes from `own`,
    the bound per key declared where the fields are set — not from a second
    list. Who acted comes from :data:`IDENTITY_FIELDS`, the same set the
    dropping round reads — not from a second list either, so a name added
    there reaches this floor. Should even that ever exceed the ceiling, the
    floor below still holds the bound.

    Keeping what the caller said costs more room than the bare mark did, so
    every field here that carries text from outside — the wire-sized own
    fields, who acted, the decision and the sentence — is shortened a step at a time
    until the record fits. Only the bounded own fields are exempt, being
    sized by this code. That is what keeps the bound this floor exists to
    hold: a name the fitter let through at any size is halved here until
    there is nothing left to halve, and only then does the floor below
    take over."""
    bounds = own or {}
    out: dict[str, Any] = {}
    for key in bounds:
        if key in payload:
            out[key] = payload[key]
    for key in IDENTITY_FIELDS:
        if key not in payload:
            continue
        out[key] = _what_the_caller_said(payload[key])
    out["verdict"] = _what_the_caller_said(payload.get("verdict"))
    # The sentence the route wrote is the caller's text like the decision is,
    # and for a deployment it is the only place the project, environment,
    # component and revision reach a reader: the floor carries the own fields,
    # who acted and the decision, and drops every other field the caller sent.
    # It was once replaced with the bare mark whatever its size, so the record
    # of a deployment that reached this floor said who deployed and not what.
    out["message"] = _what_the_caller_said(payload.get("message"))
    # A record that reaches this floor lost more than any other, so it says so
    # here too, in its own field rather than in the marks scattered through
    # what is left. Set after the own fields are copied, so it holds whatever
    # the rounds above already wrote.
    _mark_cut(out)
    while True:
        try:
            text = _dump(out)
        except Exception:
            break
        if len(text.encode("utf-8")) <= MAX_RECORD_LEN - 1:
            return text
        # Everything here that came from outside this code — the wire-sized
        # own fields, who acted, and the decision — was written as it was
        # said, and may alone pass the ceiling; shorten the longest one step
        # and measure again. This is the one place the floor spends a value
        # to hold the bound, and it spends the biggest one first, so a record
        # oversized by a single name keeps every other answer whole. The
        # bounded own fields are never shortened, and the marks the cutter
        # refuses to cut fall out of the choice on their own.
        longest: str | None = None
        longest_len = -1
        for key in _what_this_floor_may_shorten(out, bounds):
            value = out.get(key)
            if isinstance(value, str) and _cut_string(value) is not None:
                size = _str_wire_len(value)
                if size > longest_len:
                    longest, longest_len = key, size
        if longest is None:
            break
        narrowed = _cut_string(out[longest])
        if narrowed is None:
            break
        out[longest] = narrowed
    return _the_least_a_record_can_say()


def _fit_open(payload: dict[str, Any], own: dict[str, bool] | None = None) -> str:
    """Encode, deciding what to cut and by how much before mutating.

    The bound is on the whole record in bytes on the wire, without
    qualification, so the fitter looks everywhere the weight can be: top-level
    string values, strings nested in lists and mappings, long keys, and sheer
    field count. Each round walks the record once, plans cuts whose savings
    cover the excess, applies them, and confirms with one encode — work
    proportional to the record, not to the record squared. The earlier fitting
    halved exactly one string per round trip through a full walk and a full
    encode, so its cost grew with the number of strings times the size of the
    record, and one oversized record stalled every other thread behind the
    sink's lock.

    Values the encoder cannot take never reach the rounds: they are replaced
    up front with a short note saying what they were, so the record survives
    as an ugly line rather than vanishing inside the sink's silent failure
    route. A record that already fits comes out exactly as encoded, byte for
    byte."""
    try:
        text = _dump(payload)
    except Exception:
        text = None
    if text is not None and len(text.encode("utf-8")) <= MAX_RECORD_LEN - 1:
        return text
    payload = _sanitize_record(payload)
    # What the caller said, before any round has cut it. The floor is reached
    # when the rounds have failed, and by then they have also spent the record:
    # the clamp halves the decision once a round, so a decision arriving at the
    # floor is the bare mark and no judgment the floor makes can bring it back.
    # The floor's promise is to answer the same questions as any other record,
    # which it can only do from what it was handed rather than from what the
    # rounds left. A shallow copy is enough: the rounds replace top-level
    # values and rename top-level keys, and the floor reads nothing deeper.
    as_it_was_said = dict(payload)
    for _ in range(_MAX_FIT_ROUNDS):
        text = _dump(payload)
        size = len(text.encode("utf-8"))
        if size <= MAX_RECORD_LEN - 1:
            return text
        # The record does not fit, so something of it is about to be lost and
        # the record must say so. Saying so costs a field, and that cost is
        # paid here, once, before any round measures anything — not at the end
        # of the round that made the loss. The earlier fitting wrote the
        # statement after dropping, which made it the heaviest droppable thing
        # left, so the next round dropped it and putting it back cost exactly
        # what dropping it had saved. That round then reported no progress,
        # correctly, and the loop's answer to no-progress was to hand the
        # record over: seven hundred of the caller's fields unspent, a hundred
        # and forty-five bytes against a ceiling of eight thousand. Paid up
        # front the statement is part of the record the rounds are fitting,
        # inside the excess they are working against, and every round after it
        # is pure removal — so no-progress now means what it says.
        if _mark_cut(payload):
            size = len(_dump(payload).encode("utf-8"))
        excess = size - (MAX_RECORD_LEN - 1)
        if _shorten_planned(payload, excess, own):
            continue
        if _drop_planned(payload, excess, own):
            continue
        if _clamp_verdict(payload):
            continue
        break
    return _last_resort(as_it_was_said, own)


def _fit(payload: dict[str, Any], own: dict[str, bool] | None = None) -> str:
    """Fit one record under the ceiling. Never raises: the sink's failure
    route is silent by design, so a fitting that raised would lose the record
    with no trace anywhere, and an audit trail that drops a record silently is
    worse than one that writes an ugly record.

    `own` maps each top-level key the formatter set itself to its bound; see
    :func:`_shorten_planned`. Callers fitting a bare mapping with no
    formatter behind it leave it empty, and every key stays a candidate."""
    try:
        return _fit_open(payload, own)
    except Exception:
        return _the_least_a_record_can_say()


def log_audit(logger: logging.Logger, message: str, *args: Any, actor: str,
              verdict: str, role: str | None = None, tool: str | None = None,
              resource: str | None = None, needs: str | None = None,
              **fields: Any) -> None:
    """Log one audit record. This is the only way to write one.

    The decision and who it is about travel as record fields the caller cannot
    occupy; what the caller sent stays in its own field where it may say
    anything. `message` is a human sentence naming what happened — it still
    carries the caller text a reader needs to see what was refused, but no
    reader may parse a decision out of it. Extra `fields` become further
    top-level fields (the deployment record's project, env, component, tag).
    """
    sealed: dict[str, Any] = {"actor": actor, "verdict": verdict}
    if role is not None:
        sealed["role"] = role
    if tool is not None:
        sealed["tool"] = tool
    if resource is not None:
        sealed["resource"] = resource
    if needs is not None:
        sealed["needs"] = needs
    for key, value in fields.items():
        sealed[key] = value
    logger.info(message, *args, extra={_SEALED_AUDIT_ATTR: sealed})


def guarded_thread(*, target: Callable[..., Any], name: str,
                   args: tuple[Any, ...] = (), kwargs: dict[str, Any] | None = None,
                   daemon: bool = True) -> threading.Thread:
    """A background loop as a thread whose death stays inside the sink.

    A bare daemon thread that raises hands its traceback to the
    interpreter's own hook, which writes raw multi-line text straight to the
    error stream, past every handler the sink installed. The wrapper catches
    the failure and reports it as one ordinary record through the sink, so a
    dying loop is as bounded and as neutralised as any other record. Every
    background loop in this package starts through this guard.
    """
    call_kwargs = dict(kwargs) if kwargs else {}

    def _run() -> None:
        try:
            target(*args, **call_kwargs)
        except Exception:
            log.exception("background loop %s failed", name)

    thread = threading.Thread(target=_run, name=name, daemon=daemon)  # guarded: the one construction this module may use
    return thread


class _GuardedTimer(threading.Timer):
    """A timer whose firing stays inside the sink.

    It is a real timer, so scheduling and cancelling are exactly
    :class:`threading.Timer`'s; only a firing that raises is reported as one
    ordinary record through the sink, the way :func:`guarded_thread` reports a
    dying loop. Like that guard it catches ``Exception``, nothing wider.
    """

    def run(self) -> None:
        try:
            super().run()
        except Exception:
            log.exception("background loop %s failed", self.name)


def guarded_timer(*, interval: float, function: Callable[..., Any], name: str,
                  args: tuple[Any, ...] = (), kwargs: dict[str, Any] | None = None,
                  daemon: bool = True) -> threading.Timer:
    """A one-shot background body as a timer whose death stays inside the sink.

    A bare timer that raises hands its traceback to the interpreter's own
    hook, which writes raw multi-line text straight to the error stream, past
    every handler the sink installed. The timer returned here keeps its
    scheduling and its :meth:`~threading.Timer.cancel`, so a caller cancels
    it on the happy route exactly as before; only the failure route changes.
    Every timer in this package starts through this guard.
    """
    timer = _GuardedTimer(interval, function,
                          args=args, kwargs=dict(kwargs) if kwargs else None)
    timer.name = name
    timer.daemon = daemon
    return timer


def _safe_repr(value: Any) -> str:
    """A representation of one value, or the type's name when the value will
    not produce one. Each value is asked on its own, so one that refuses
    costs only itself."""
    try:
        return repr(value)
    except Exception:
        try:
            return "<unrepresentable %s>" % _safe_type_name(value)
        except Exception:
            return "<unrepresentable>"


def _unformattable_message(record: logging.LogRecord) -> str:
    """What the sink writes when the record's own text refuses to render.

    Interpolation fails for two unlike reasons and the reader needs different
    things from each. When a value refuses to become text, asking it again
    raises the same refusal, so the answer can only name its type. When the
    caller merely passed the wrong number of arguments, nothing refuses
    anything: the format and the arguments are ordinary values, and throwing
    them away loses the only evidence of what the caller meant. So each part
    is asked separately, and a part that will not answer costs only itself."""
    try:
        parts = ["unformattable message of type %s" % _safe_type_name(record.msg)]
        parts.append("format=" + _safe_repr(record.msg))
        args = record.args
        if args:
            # Each argument on its own. Asking the tuple as a whole would let one value
            # that will not answer cost every other value in the record, which is the
            # loss this function exists to stop.
            if not isinstance(args, tuple):
                args = (args,)
            parts.append("args=" + ", ".join(_safe_repr(one) for one in args))
        return " ".join(parts)
    except Exception:
        return "unformattable message"


def _exception_text(exc_info: Any) -> str | None:
    try:
        if not exc_info:
            return None
        if isinstance(exc_info, tuple) and (len(exc_info) != 3 or exc_info[0] is None):
            return None
        text = "".join(traceback.format_exception(*exc_info))
        return text or None
    except Exception:
        return None


class JsonOneLineFormatter(logging.Formatter):
    """One record in, one JSON line out. The schema is fixed — ``time``,
    ``logger``, ``level``, ``message``, plus ``exception``/``stack`` when
    present — with the audit fields (``actor``, ``role``, ``tool``,
    ``resource``, ``verdict``, ``needs``, ...) alongside them when the record
    was written through :func:`log_audit`. A caller can never choose a field
    name: fields arrive sealed on the record object, never parsed out of
    text, and ``extra`` keys under the audit prefix from any other call are
    ignored."""

    def format(self, record: logging.LogRecord) -> str:
        try:
            message = record.getMessage()
        except Exception:
            # The guard above caught the message refusing to render — and the
            # old fallback interpolated the same object again, raising the
            # same refusal out of the formatter, so nothing was written on
            # either stream. The fallback below touches no caller object, only
            # the type name, which comes from code.
            message = _unformattable_message(record)
        # The record's own fields: set here, never taken from the caller, each
        # carrying its bound with it. The time is bounded by construction —
        # this code chooses its size — so the fitter never shortens, drops
        # or renames it at any size. The level, the logger name, the
        # exception and the stack carry text from outside this code: this
        # sink serves every library on the root logger, a logger name is
        # whatever the code that made it passed, and a level name is
        # whatever the level table last received. They are still the
        # formatter's own, so the fitter never drops or renames them, but
        # when there is no other way to fit it shortens them like any other
        # value: protecting a field is not the same as bounding it, and an
        # unbounded one destroys the record it was kept to explain. The
        # bounds travel beside the values, read by the fitter and the last
        # resort alike, so a field added here declares its bound on these
        # lines and nowhere else. The sentence is one of them, wire-sized: its
        # text is the caller's and it is cut like any other value, but it is
        # this code that places it and a record that says a call was refused
        # without saying what was refused is not worth much. Dropped whole it
        # was the largest single field in a crowded record, so it went first
        # and hundreds of one-character fields of the caller's outlived the
        # only sentence in the record. What the sealed mapping carries is the
        # caller's and stays out.
        own: dict[str, Any] = {
            "time": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
            "logger": record.name,
            "level": record.levelname,
        }
        bounds: dict[str, bool] = {
            "time": BOUNDED,
            "logger": WIRE_SIZED,
            "level": WIRE_SIZED,
            # Reserved, and reserved whether or not this record turns out to
            # be cut: the field the fitting says so in. A name is reserved by
            # standing in this mapping, which is what keeps a caller's field
            # of the same name from taking its place below — and the fitting
            # reads the same mapping, so what says the record was cut is
            # neither shortened, renamed nor dropped at any size. Its value is
            # written by the fitting and by nothing else, so there is no value
            # to set here.
            TRUNCATED_FIELD: BOUNDED,
            "message": WIRE_SIZED,
        }
        exc_text = _exception_text(record.exc_info)
        if exc_text is not None:
            own["exception"] = exc_text
            bounds["exception"] = WIRE_SIZED
        if record.stack_info:
            own["stack"] = str(record.stack_info)
            bounds["stack"] = WIRE_SIZED
        # A field added above with no bound line is treated as text somebody
        # else chose the length of: shortened with the mark when there is no
        # other way to fit, never dropped or renamed. Of the two ways to be
        # wrong — losing some text, or losing the record — losing some text
        # is the safe one. A field this code sizes says BOUNDED on the lines
        # that set it; everything else says WIRE_SIZED, or says nothing and
        # is shortened all the same.
        for key in own:
            bounds.setdefault(key, WIRE_SIZED)
        payload: dict[str, Any] = dict(own)
        payload["message"] = message
        sealed = vars(record).get(_SEALED_AUDIT_ATTR)
        if isinstance(sealed, dict):
            for key, value in sealed.items():
                if value is None:
                    continue
                if key in payload or key in bounds:
                    # The formatter's own fields and the message are already
                    # placed above; a sealed field of the same name is the
                    # caller's, so it must not replace them. A reserved name
                    # with no value yet is held the same way: the field the
                    # fitting says a record was cut in is empty until there is
                    # something to say, and a caller who could fill it could
                    # make a whole record read as cut.
                    continue
                payload[key] = value
        return _fit(payload, bounds)


class _SilentOnFailureHandler(logging.StreamHandler):
    """A stream handler whose own failure writes nothing raw.

    The default handler answers an unwritable stream by writing
    ``--- Logging error ---`` and a raw traceback carrying the record's caller
    text straight to the error stream — reachable on a closed pipe in the
    container. A sink must not have a failure route that bypasses itself, so
    this one stays silent: the record is lost and nothing raw leaves.
    """

    def handleError(self, record: logging.LogRecord) -> None:
        return None


def configure_logging(stream: Any) -> None:
    """Install the process's logging: one handler on the root logger emitting
    :class:`JsonOneLineFormatter` lines to `stream`. `serve()` calls this
    instead of ``basicConfig``."""
    handler = _SilentOnFailureHandler(stream)
    handler.setFormatter(JsonOneLineFormatter())
    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(logging.INFO)
