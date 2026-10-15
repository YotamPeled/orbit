#!/usr/bin/env python3
"""Regression tests for the notifier's authorization boundary and queue liveness.

Stdlib `unittest` only (no pytest), matching the service's own dependency discipline —
run with `python3 test_notifier.py`. Each test is written to go RED if its fix is reverted;
the revert it guards is named in the test docstring. A tiny in-memory fake stands in for
Postgres so the DB-dependent behaviors (head-of-line blocking, unrouted parking, one-shot
token resolution) are exercised for real without a live CNPG.
"""
import json
import os
import sys
import threading
import types
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

# The service imports psycopg at module load, but every DB call in these tests is served by
# an in-memory fake (app.psycopg.connect is monkeypatched per test), so a stub module is
# enough — no live driver or CNPG needed to run the suite locally or in CI.
if "psycopg" not in sys.modules:
    _stub = types.ModuleType("psycopg")
    _stub.connect = lambda *a, **k: (_ for _ in ()).throw(
        RuntimeError("psycopg.connect must be monkeypatched in tests"))
    sys.modules["psycopg"] = _stub

# Configure the boundary before importing the service module.
os.environ.setdefault("DATABASE_URL", "postgres://test/none")
os.environ["NOTIFIER_TOKEN"] = "s3cret-caller-token"
os.environ["NOTIFIER_CALLBACK_SECRET"] = "callback-signing-key"
os.environ["TELEGRAM_CHAT_ID"] = "931319298"          # the configured DM
os.environ["NOTIFIER_MAX_ATTEMPTS"] = "3"

import app  # noqa: E402

app.TELEGRAM_TOKEN = "bot-token-present"               # so sinks are actually invoked

CHAT = "931319298"
NOW = object()


# --------------------------------------------------------------- in-memory fake DB

class FakeStore:
    # `clock` is a virtual wall-clock in seconds. now()-based SQL (first_failed_at anchoring,
    # the transient budget, next_attempt_at backoff gating) reads it, and tests advance it to
    # simulate an outage lasting minutes or a full day WITHOUT sleeping. Count-based paths
    # (unknown-error cap, unrouted parking) never touch it, so pre-existing tests need no clock.
    def __init__(self):
        self.rows = []
        self._id = 0
        self.clock = 0.0

    def insert(self, **kw):
        self._id += 1
        row = {"id": self._id, "kind": "default", "title": "", "body": "",
               "approval_id": None, "approval_token": None, "action": None,
               "approval_state": None, "approved_by": None, "delivered_at": None,
               "attempts": 0, "first_failed_at": None, "next_attempt_at": None,
               "dead_letter_at": None, "dead_letter_reason": None}
        row.update(kw)
        self.rows.append(row)
        return row

    def _by_token(self, tok):
        return next((r for r in self.rows if r["approval_token"] == tok), None)

    def run(self, sql, params):
        if "FROM outbox WHERE delivered_at IS NULL" in sql:      # the claim
            elig = [r for r in self.rows if r["delivered_at"] is None
                    and r["dead_letter_at"] is None
                    and (r["next_attempt_at"] is None or r["next_attempt_at"] <= self.clock)]
            elig.sort(key=lambda r: r["id"])
            r = elig[0] if elig else None
            return None if r is None else (
                r["id"], r["kind"], r["title"], r["body"], r["approval_id"],
                r["approval_token"], r["action"], r["approval_state"])
        if "attempts=attempts+1, dead_letter_at=now(), dead_letter_reason" in sql:  # permanent
            reason, rid = params
            row = next(r for r in self.rows if r["id"] == rid)
            row["attempts"] += 1
            row["dead_letter_at"] = NOW
            row["dead_letter_reason"] = reason
            return (row["attempts"],)
        if "first_failed_at=COALESCE(first_failed_at, now())" in sql:               # transient step
            (rid,) = params
            row = next(r for r in self.rows if r["id"] == rid)
            row["attempts"] += 1
            if row["first_failed_at"] is None:
                row["first_failed_at"] = self.clock
            return (row["attempts"], self.clock - row["first_failed_at"])
        if "SET next_attempt_at = now() + make_interval" in sql:                    # backoff gate
            backoff, rid = params
            row = next(r for r in self.rows if r["id"] == rid)
            row["next_attempt_at"] = self.clock + backoff
            return None
        if "SET dead_letter_at=now(), dead_letter_reason" in sql:
            reason, rid = params
            row = next(r for r in self.rows if r["id"] == rid)
            row["dead_letter_at"] = NOW
            row["dead_letter_reason"] = reason
            return None
        if "SET delivered_at=now()" in sql:
            (rid,) = params
            row = next(r for r in self.rows if r["id"] == rid)
            row["delivered_at"] = NOW
            row["attempts"] += 1
            return None
        if "SET attempts=attempts+1 WHERE id=%s RETURNING attempts" in sql:
            (rid,) = params
            row = next(r for r in self.rows if r["id"] == rid)
            row["attempts"] += 1
            return (row["attempts"],)
        if "SET approval_state=%s, approved_by=%s" in sql:
            state, frm, tok = params
            row = self._by_token(tok)
            if row and row["approval_state"] == "pending":
                row["approval_state"] = state
                row["approved_by"] = frm
                return (row["id"],)
            return None
        if "SELECT approval_state, approved_by FROM outbox WHERE approval_token" in sql:
            (tok,) = params
            row = self._by_token(tok)
            return (row["approval_state"], row["approved_by"]) if row else None
        if "INSERT INTO outbox" in sql:
            kind, title, body, aid, tok, action, state = params
            row = self.insert(kind=kind, title=title, body=body, approval_id=aid,
                              approval_token=tok, action=action, approval_state=state)
            return (row["id"],)
        if sql.strip() == "SELECT 1":
            return (1,)
        raise AssertionError("unhandled SQL: " + sql)


class FakeCursor:
    def __init__(self, store):
        self.store = store
        self._last = None

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, params=()):
        self._last = self.store.run(sql, params)
        return self

    def fetchone(self):
        return self._last


class FakeConn:
    def __init__(self, store):
        self.store = store

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def cursor(self):
        return FakeCursor(self.store)

    def execute(self, sql, params=()):
        return FakeCursor(self.store).execute(sql, params)

    def commit(self):
        pass

    def rollback(self):
        pass


def signed(decision, tok):
    return app.build_callback(app.CALLBACK_SECRET, decision, tok)


def press(decision, tok, chat=CHAT, frm=CHAT, raw=None):
    return {"id": "cbq1", "from": {"id": frm}, "message": {"chat": {"id": chat}},
            "data": raw if raw is not None else signed(decision, tok)}


# --------------------------------------------------------------------- the tests

class NotifyAuth(unittest.TestCase):
    """Fix 1 — POST /notify must reject unauthenticated callers with 401."""

    def _server(self, store):
        app.psycopg.connect = lambda *a, **k: FakeConn(store)
        srv = ThreadingHTTPServer(("127.0.0.1", 0), app.Handler)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        self.addCleanup(srv.shutdown)
        return srv.server_address[1]

    def _post(self, port, headers):
        req = urllib.request.Request(
            f"http://127.0.0.1:{port}/notify",
            data=json.dumps({"title": "deploy", "approval_id": "a1"}).encode(),
            headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=5) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            e.close()
            return e.code, None

    def test_no_auth_header_is_401(self):
        """RED on revert of the auth guard: an anonymous POST would mint a gate (202)."""
        store = FakeStore()
        port = self._server(store)
        code, _ = self._post(port, {"Content-Type": "application/json"})
        self.assertEqual(code, 401)
        self.assertEqual(store.rows, [], "unauthenticated call must not enqueue a row")

    def test_wrong_token_is_401(self):
        port = self._server(FakeStore())
        code, _ = self._post(port, {"Authorization": "Bearer wrong"})
        self.assertEqual(code, 401)

    def test_valid_token_mints_gate(self):
        store = FakeStore()
        port = self._server(store)
        code, body = self._post(port, {"Authorization": "Bearer s3cret-caller-token"})
        self.assertEqual(code, 202)
        self.assertTrue(body["approval_token"], "a token must be returned to the caller")
        self.assertEqual(len(store.rows), 1)
        # unguessable, not the sequential row id
        self.assertNotEqual(body["approval_token"], str(store.rows[0]["id"]))


class CallbackAuth(unittest.TestCase):
    """Fix 2 — a callback is honored only from the configured chat, only with an authentic
    signature, and only against its own unguessable token (never a guessed row id)."""

    def _pending(self):
        store = FakeStore()
        row = store.insert(kind="approval", title="sync", approval_id="a1",
                           approval_token="TOKEN-abc123", action="argo sync checkers",
                           approval_state="pending")
        return store, row

    def test_wrong_chat_refused(self):
        """RED on revert of the chat check: original code resolved regardless of chat."""
        store, row = self._pending()
        tag = app.handle_callback(FakeConn(store), press("ok", "TOKEN-abc123", chat="-100999"))
        self.assertEqual(tag, "rejected-chat")
        self.assertEqual(row["approval_state"], "pending")

    def test_wrong_user_refused(self):
        store, row = self._pending()
        tag = app.handle_callback(FakeConn(store), press("ok", "TOKEN-abc123", frm="42"))
        self.assertEqual(tag, "rejected-chat")
        self.assertEqual(row["approval_state"], "pending")

    def test_guessed_sequential_id_refused(self):
        """RED on revert of the token scheme: original split 'ok:<id>' and UPDATE'd by id,
        so a guessed sequential id (here the raw row id 1) resolved the gate."""
        store, row = self._pending()
        tag = app.handle_callback(FakeConn(store), press("ok", "1", raw="ok:1"))
        self.assertEqual(tag, "rejected-sig")
        self.assertEqual(row["approval_state"], "pending")

    def test_forged_signature_refused(self):
        store, row = self._pending()
        forged = "ok:TOKEN-abc123:deadbeefdeadbeefdeadbeefdeadbeef"
        tag = app.handle_callback(FakeConn(store), press("ok", "TOKEN-abc123", raw=forged))
        self.assertEqual(tag, "rejected-sig")
        self.assertEqual(row["approval_state"], "pending")

    def test_authentic_press_resolves_once(self):
        store, row = self._pending()
        tag = app.handle_callback(FakeConn(store), press("ok", "TOKEN-abc123"))
        self.assertEqual(tag, "resolved")
        self.assertEqual(row["approval_state"], "approved")
        self.assertEqual(row["approved_by"], CHAT)          # actor recorded
        # one-shot: a second press (e.g. flip to deny) finds nothing pending
        tag2 = app.handle_callback(FakeConn(store), press("no", "TOKEN-abc123"))
        self.assertEqual(tag2, "noop")
        self.assertEqual(row["approval_state"], "approved")

    def test_decision_cannot_be_flipped(self):
        """The mac binds decision to token: 'no'+a mac minted for 'ok' must not verify."""
        store, row = self._pending()
        ok_mac = signed("ok", "TOKEN-abc123").split(":")[-1]
        tampered = f"no:TOKEN-abc123:{ok_mac}"
        tag = app.handle_callback(FakeConn(store), press("no", "TOKEN-abc123", raw=tampered))
        self.assertEqual(tag, "rejected-sig")


class QueueLiveness(unittest.TestCase):
    """Fix 3 — one undeliverable row must not head-of-line block later rows."""

    def test_poison_row_does_not_block_successor(self):
        """RED on revert of the attempts cap: without it the poller re-claims row 1 forever
        and row 2 is never delivered."""
        store = FakeStore()
        poison = store.insert(kind="alert", title="poison")     # id 1, always fails
        good = store.insert(kind="alert", title="good")         # id 2, must get through

        def sink(row):
            if row["id"] == poison["id"]:
                raise RuntimeError("permanent 400 from telegram")

        delivered = None
        for _ in range(10):
            tag = app.deliver_pass(FakeConn(store), deliver={"telegram": sink})
            if good["delivered_at"] is not None:
                delivered = tag
                break
        self.assertIsNotNone(good["delivered_at"], "successor row was never delivered")
        self.assertIsNotNone(poison["dead_letter_at"], "poison row was not dead-lettered")
        self.assertGreaterEqual(poison["attempts"], app.MAX_ATTEMPTS)


class UnroutedKind(unittest.TestCase):
    """Fix 4 — an event whose kind routes nowhere must be parked and logged, not silently
    marked delivered (data loss)."""

    def test_unrouted_is_parked_not_delivered(self):
        """RED on revert: original code left sinks empty, the deliver loop was a no-op, and
        the row was UPDATE'd delivered_at=now() — gone with no trace."""
        saved = app.ROUTES
        app.ROUTES = {"alert": ["telegram"]}                    # no 'default'
        self.addCleanup(lambda: setattr(app, "ROUTES", saved))
        store = FakeStore()
        row = store.insert(kind="mystery", title="from an unknown producer")
        tag = app.deliver_pass(FakeConn(store), deliver={"telegram": lambda r: None})
        self.assertEqual(tag, "parked-unrouted")
        self.assertIsNone(row["delivered_at"], "unrouted row must NOT be marked delivered")
        self.assertIsNotNone(row["dead_letter_at"], "unrouted row must be parked/visible")
        self.assertIn("unrouted", row["dead_letter_reason"])


def _http_error(code):
    """A urllib HTTPError carrying a status code — the exact shape deliver_telegram raises
    when Telegram answers non-2xx, so classify_failure sees a real .code."""
    return urllib.error.HTTPError("https://api.telegram.org", code, "err", {}, None)


class DeadLetterClassification(unittest.TestCase):
    """Fix 5 — a transient sink failure (429/5xx/network) is retried over a LARGE wall-clock
    budget and never silently dropped by a small attempt count; only genuine poison (a 4xx
    that is not 429) is dead-lettered fast. Every drop increments the dead_letter metric."""

    def _pass(self, store, sink):
        return app.deliver_pass(FakeConn(store), deliver={"telegram": sink})

    def test_permanent_400_dead_letters_on_first_failure(self):
        """RED on revert to count-based: a 400 would take MAX_ATTEMPTS tries, not 1, and emit
        no metric."""
        store = FakeStore()
        row = store.insert(kind="alert", title="malformed request")
        before = app._dead_letter.get("permanent", 0)
        tag = self._pass(store, lambda r: (_ for _ in ()).throw(_http_error(400)))
        self.assertEqual(tag, "dead-lettered")
        self.assertIsNotNone(row["dead_letter_at"])
        self.assertEqual(row["attempts"], 1, "genuine poison drops on first failure")
        self.assertEqual(app._dead_letter.get("permanent", 0), before + 1,
                         "dead-letter must emit the metric")

    def test_transient_503_not_dropped_within_budget_then_dropped_after(self):
        """RED on revert to count-based: a 503 outage lasting past MAX_ATTEMPTS would be
        dead-lettered (assert tag=='retry' / not dead-lettered would fail). Proves a real
        Telegram outage retries for a long time and never silently drops a good alert."""
        store = FakeStore()
        row = store.insert(kind="alert", title="alert during a telegram outage")
        sink = lambda r: (_ for _ in ()).throw(_http_error(503))
        # A long outage: 30 passes, each advancing wall-clock past the capped backoff so the
        # row is re-eligible every pass. Well inside the 24h budget → always a retry.
        for _ in range(30):
            store.clock += app.BACKOFF_CAP_S + 1
            self.assertEqual(self._pass(store, sink), "retry")
        self.assertIsNone(row["dead_letter_at"],
                          "a real outage must never silently drop a good alert")
        self.assertGreater(row["attempts"], app.MAX_ATTEMPTS,
                           "transient retries are NOT bounded by the attempt count")
        self.assertGreater(app.TRANSIENT_DEADLINE_S, 3600, "budget must be large (hours+)")
        # Only once wall-clock exceeds the whole budget does it finally, observably, give up.
        before = app._dead_letter.get("transient-exhausted", 0)
        store.clock += app.TRANSIENT_DEADLINE_S
        self.assertEqual(self._pass(store, sink), "dead-lettered")
        self.assertIsNotNone(row["dead_letter_at"])
        self.assertEqual(app._dead_letter.get("transient-exhausted", 0), before + 1)

    def test_429_burst_survives_past_attempt_cap(self):
        """RED on revert: a 429 rate-limit burst longer than MAX_ATTEMPTS would be
        dead-lettered by count under the old code."""
        store = FakeStore()
        row = store.insert(kind="approval", title="approve deploy")
        sink = lambda r: (_ for _ in ()).throw(_http_error(429))
        for _ in range(app.MAX_ATTEMPTS + 3):
            store.clock += app.BACKOFF_CAP_S + 1
            self._pass(store, sink)
        self.assertIsNone(row["dead_letter_at"],
                          "a 429 burst must not exhaust the poison budget by count")
        self.assertGreater(row["attempts"], app.MAX_ATTEMPTS)

    def test_transient_backoff_does_not_block_successor(self):
        """A backing-off transient row is skipped by the claim so a later good row still gets
        through immediately — the poison-drain guarantee holds for transient rows too."""
        store = FakeStore()
        bad = store.insert(kind="alert", title="stuck behind an outage")   # id 1, 503s
        good = store.insert(kind="alert", title="good")                    # id 2
        seen = {"bad": 0}

        def sink(r):
            if r["id"] == bad["id"]:
                seen["bad"] += 1
                raise _http_error(503)
        # First pass claims id 1, fails, schedules its backoff into the future. Second pass
        # (same clock) must skip id 1 and deliver id 2.
        self.assertEqual(self._pass(store, sink), "retry")
        self.assertEqual(self._pass(store, sink), "delivered")
        self.assertIsNotNone(good["delivered_at"])
        self.assertIsNone(bad["dead_letter_at"])

    def test_metrics_endpoint_exposes_dead_letter_counter(self):
        txt = app.render_metrics()
        self.assertIn("# TYPE orbit_notifier_dead_letter_total counter", txt)
        self.assertIn('orbit_notifier_dead_letter_total{reason="permanent"}', txt)
        self.assertIn('orbit_notifier_dead_letter_total{reason="transient-exhausted"}', txt)


if __name__ == "__main__":
    unittest.main(verbosity=2)
