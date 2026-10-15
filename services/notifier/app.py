"""Orbit notifier — one outbox, adapters per sink (AGENT-PLANE.md).

A single service every instance runs: events land in a Postgres outbox (CNPG), a
poller delivers each to the sinks its route names, and approval requests come back
through the same table so an ask-gate resolves from chat OR the console. Telegram is
the first adapter and the only one with BOTH capabilities — notify (fire-and-forget)
and interactive-approval (inline buttons that resolve a gate).

Standard library only save psycopg, same discipline as the intake service: this
process holds the bot token and resolves approval gates, so its dependency surface is
kept deliberately small. Delivery is at-least-once (a claimed-but-uncommitted row is
redelivered), so adapters and consumers must tolerate a duplicate.

Trust model (this is the authorization boundary, mirroring the intake service):
  * POST /notify is the intake for a decision, not just a delivery. It is authenticated
    with a shared bearer secret (NOTIFIER_TOKEN) verified in constant time. Unauthenticated
    callers are refused 401 — an anonymous caller must not be able to mint an approval gate
    and phish an Approve press.
  * A resolved approval is a one-shot token that authorizes a SPECIFIC action. The button's
    callback_data carries an unguessable per-row token (not the sequential row id) and an
    HMAC that binds the decision to that token, so the payload cannot be forged or flipped;
    resolution only ever fires from state 'pending', so a gate cannot be re-read and re-flipped.
  * A callback is accepted only from the configured Telegram chat/approver — getUpdates
    returns presses from every chat the bot is in, so the chat/user identity is checked
    before the DB is touched.
"""
import hashlib
import hmac
import json
import os
import secrets
import sys
import threading
import time
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import psycopg

DB_URL = os.environ.get("DATABASE_URL", "")
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT = os.environ.get("TELEGRAM_CHAT_ID", "")
PORT = int(os.environ.get("PORT", "8080"))
# Shared secret a caller must present on POST /notify (Authorization: Bearer <token>).
# Held by the service and by the agents allowed to enqueue; verified with compare_digest.
NOTIFIER_TOKEN = os.environ.get("NOTIFIER_TOKEN", "")
# Key that signs the button callback_data so a decision cannot be forged or flipped. Never
# leaves the process; defaults to NOTIFIER_TOKEN so the deploy needs only one new secret,
# but a distinct value is preferred so a leaked caller token cannot also forge callbacks.
CALLBACK_SECRET = os.environ.get("NOTIFIER_CALLBACK_SECRET", "") or NOTIFIER_TOKEN
# Telegram user ids allowed to resolve gates. Defaults to the configured chat id (the
# chat is a single-user DM, so chat id == user id). CSV to allow a small operator set.
APPROVER_IDS = [s.strip() for s in os.environ.get(
    "NOTIFIER_APPROVER_IDS", TELEGRAM_CHAT).split(",") if s.strip()]
# Dead-letter policy. A failed delivery is CLASSIFIED, never just counted (see
# classify_failure): a PERMANENT failure (a 4xx that is not 429 — genuine bad request /
# poison) is dropped on the first failure; a TRANSIENT failure (429, 5xx, network/timeout —
# an outage, not a poison row) is retried with exponential backoff over a LARGE wall-clock
# budget and is dead-lettered ONLY after that whole window elapses, so a Telegram outage or a
# 429 burst can never burn a small attempt count and silently drop a legitimate alert.
#   MAX_ATTEMPTS is now only the fallback cap for an UNCLASSIFIABLE error (not a known
#   transient/permanent shape): retry a few times, then give up loudly. It does NOT bound
#   transient retries — those are bounded by wall-clock, not by count.
MAX_ATTEMPTS = int(os.environ.get("NOTIFIER_MAX_ATTEMPTS", "5"))
# Exponential backoff for transient retries: base * 2**(attempts-1), capped. The row is made
# ineligible for reclaim until next_attempt_at, so backing off never head-of-line blocks its
# successors (the claim simply skips it and delivers the next id).
BACKOFF_BASE_S = float(os.environ.get("NOTIFIER_BACKOFF_BASE_S", "1"))
BACKOFF_CAP_S = float(os.environ.get("NOTIFIER_BACKOFF_CAP_S", "300"))
# The wall-clock budget a transient failure is retried across before it is finally
# dead-lettered. Deliberately large (24h default): a real multi-hour outage keeps retrying
# and never drops a good message; only a sink that is down for a full day gives up.
TRANSIENT_DEADLINE_S = float(os.environ.get("NOTIFIER_TRANSIENT_DEADLINE_S", "86400"))
# Which event kinds go to which sinks — a JSON object from instance config, e.g.
# {"alert": ["telegram"], "approval": ["telegram"], "info": ["telegram"]}. An event
# whose kind is unrouted is parked (never silently marked delivered) — see resolve_sinks.
ROUTES = json.loads(os.environ.get("NOTIFIER_ROUTES", '{"default": ["telegram"]}'))

# Telegram caps a message at 4096 chars; a longer body is a permanent 400 ("message is too
# long"), so truncate before sending. Body/title are also caller-supplied, so they are sent
# as plain text (no parse_mode) — a stray * or _ must not make Telegram reject the message,
# and unescaped markup must not become a phishing link in an approval prompt.
TG_MAX = 4096

# ------------------------------------------------------------------------- metrics
# A dropped alert/approval must never be invisible. We expose a Prometheus text endpoint at
# /metrics (ServiceMonitor-scraped like every other Orbit service)
# whose key series is orbit_notifier_dead_letter_total, labelled by why the drop happened.
# ALERTING: a PrometheusRule on `increase(orbit_notifier_dead_letter_total[15m]) > 0` pages —
# any dead-letter is a delivered-decision guarantee (at-least-once) being broken and warrants
# a human. reason="permanent"/"unrouted" is a producer bug; reason="transient-exhausted" is a
# sink that has been down for the full budget; reason="unknown" is an unexpected error shape.
_metrics_lock = threading.Lock()
# Seed every reason at 0 so the alert's series always exists (a missing series can't fire).
_dead_letter = {"permanent": 0, "transient-exhausted": 0, "unknown": 0, "unrouted": 0}
_delivered = 0


def _count_dead_letter(reason: str) -> None:
    with _metrics_lock:
        _dead_letter[reason] = _dead_letter.get(reason, 0) + 1


def _count_delivered() -> None:
    global _delivered
    with _metrics_lock:
        _delivered += 1


def render_metrics() -> str:
    with _metrics_lock:
        lines = [
            "# HELP orbit_notifier_dead_letter_total Messages permanently given up on, by reason.",
            "# TYPE orbit_notifier_dead_letter_total counter",
        ]
        for reason, n in sorted(_dead_letter.items()):
            lines.append(f'orbit_notifier_dead_letter_total{{reason="{reason}"}} {n}')
        lines.append("# HELP orbit_notifier_delivered_total Messages delivered to all sinks.")
        lines.append("# TYPE orbit_notifier_delivered_total counter")
        lines.append(f"orbit_notifier_delivered_total {_delivered}")
    return "\n".join(lines) + "\n"


SCHEMA = """
CREATE TABLE IF NOT EXISTS outbox (
  id           bigserial PRIMARY KEY,
  kind         text NOT NULL,
  title        text NOT NULL,
  body         text NOT NULL DEFAULT '',
  -- approval rows carry a gate the delivered buttons resolve; NULL for plain notices.
  -- approval_token is an unguessable per-row secret (NOT the sequential id) used in the
  -- callback_data and the /approval/<token> path; action names the specific thing the
  -- press authorizes, so the decision is bound to an action, not a re-readable flag.
  approval_id    text,
  approval_token text UNIQUE,
  action         text,
  approval_state text,           -- NULL | pending | approved | denied
  approved_by    text,           -- Telegram user id that resolved it (audit trail)
  approved_at    timestamptz,
  created_at   timestamptz NOT NULL DEFAULT now(),
  delivered_at timestamptz,
  attempts     int NOT NULL DEFAULT 0,
  -- transient-retry bookkeeping. first_failed_at anchors the wall-clock budget (the row is
  -- dead-lettered only once now()-first_failed_at exceeds TRANSIENT_DEADLINE_S, not after a
  -- small attempt count). next_attempt_at is the backoff gate: the claim ignores a row until
  -- now() passes it, so a backing-off row never head-of-line blocks its successors.
  first_failed_at timestamptz,
  next_attempt_at timestamptz,
  -- set when a row is given up on (delivery exhausted) or cannot be routed; such a row is
  -- excluded from the claim so the queue drains past it, but stays visible for the console.
  dead_letter_at timestamptz,
  dead_letter_reason text
);
CREATE INDEX IF NOT EXISTS outbox_undelivered ON outbox (id)
  WHERE delivered_at IS NULL AND dead_letter_at IS NULL;
-- Idempotent add for an outbox created before transient-retry bookkeeping existed.
ALTER TABLE outbox ADD COLUMN IF NOT EXISTS first_failed_at timestamptz;
ALTER TABLE outbox ADD COLUMN IF NOT EXISTS next_attempt_at timestamptz;
"""

# ------------------------------------------------------------- pure decision logic
# These functions touch neither the DB nor the network so the authorization boundary is
# unit-testable; the loops below are thin wrappers that inject a connection and a sender.


def notify_authorized(token: str, auth_header: str) -> bool:
    """True iff the caller presented the shared secret. Fail closed when unconfigured."""
    if not token:
        return False
    prefix = "Bearer "
    presented = auth_header[len(prefix):] if auth_header.startswith(prefix) else ""
    return bool(presented) and hmac.compare_digest(presented, token)


def sign_callback(secret: str, decision: str, tok: str) -> str:
    return hmac.new(secret.encode(), f"{decision}:{tok}".encode(),
                    hashlib.sha256).hexdigest()[:32]


def build_callback(secret: str, decision: str, tok: str) -> str:
    """<decision>:<token>:<hmac> — the mac binds the decision to this exact token so a
    presser cannot change 'no' to 'ok' or aim the press at another row."""
    return f"{decision}:{tok}:{sign_callback(secret, decision, tok)}"


def parse_callback(secret: str, data: str):
    """Return (decision, token) iff the mac verifies and the decision is known, else None."""
    parts = (data or "").split(":")
    if len(parts) != 3:
        return None
    decision, tok, mac = parts
    if decision not in ("ok", "no") or not tok:
        return None
    if not hmac.compare_digest(mac, sign_callback(secret, decision, tok)):
        return None
    return decision, tok


def callback_authorized(cb: dict, chat_id: str, approvers) -> bool:
    """A press is honored only from the configured chat AND an allowed approver. getUpdates
    hands back presses from every chat the bot is in, so this is checked before the DB."""
    if not chat_id:
        return False
    chat = str(((cb.get("message") or {}).get("chat") or {}).get("id", ""))
    frm = str((cb.get("from") or {}).get("id", ""))
    return chat == str(chat_id) and frm in {str(a) for a in approvers}


def classify_failure(exc: Exception) -> str:
    """'permanent' | 'transient' | 'unknown' for a delivery exception, so the poison budget
    is spent only on genuine poison. HTTP status (urllib.error.HTTPError carries .code):
      * 4xx other than 429 → permanent (bad request / forbidden / not found — retrying can't
        help; drop fast rather than head-of-line block behind it).
      * 429 or any 5xx → transient (rate-limited or a server-side outage — retry).
    No status but an OSError (URLError/timeout/connection reset — HTTPError and URLError are
    both OSError, and .code is checked first) → transient network failure. Anything else is
    'unknown': we can't reason about it, so fall back to the count cap, never a silent drop."""
    code = getattr(exc, "code", None)
    if isinstance(code, int):
        if code == 429:
            return "transient"
        if 400 <= code < 500:
            return "permanent"
        if code >= 500:
            return "transient"
    if isinstance(exc, OSError):
        return "transient"
    return "unknown"


def backoff_seconds(attempts: int) -> float:
    """base * 2**(attempts-1), capped. attempts is 1 on the first failure."""
    return min(BACKOFF_BASE_S * (2 ** max(attempts - 1, 0)), BACKOFF_CAP_S)


def resolve_sinks(routes: dict, kind: str):
    """(sinks, routed). routed is False when neither the kind nor a 'default' route names
    any sink — that row must be parked and logged, never silently marked delivered."""
    if kind in routes:
        return list(routes[kind]), True
    if "default" in routes:
        return list(routes["default"]), True
    return [], False


# ------------------------------------------------------------------ Telegram sink

def _tg(method: str, params: dict) -> dict:
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/{method}"
    data = urllib.parse.urlencode(params).encode()
    req = urllib.request.Request(url, data=data, method="POST")
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.loads(r.read())


def deliver_telegram(row: dict) -> None:
    """notify: a plain-text message. interactive-approval: the same, plus Approve/Deny
    buttons whose callback data carries the unguessable token and a decision-binding mac."""
    text = f"{row['title']}\n{row['body']}" if row["body"] else row["title"]
    if row.get("action"):
        text += f"\n\nAction: {row['action']}"
    params = {"chat_id": TELEGRAM_CHAT, "text": text[:TG_MAX]}
    if row.get("approval_token") and row.get("approval_state") == "pending":
        tok = row["approval_token"]
        params["reply_markup"] = json.dumps({"inline_keyboard": [[
            {"text": "✅ Approve", "callback_data": build_callback(CALLBACK_SECRET, "ok", tok)},
            {"text": "🚫 Deny", "callback_data": build_callback(CALLBACK_SECRET, "no", tok)},
        ]]})
    _tg("sendMessage", params)


ADAPTERS = {"telegram": deliver_telegram}


# ------------------------------------------------------------------- delivery pass

def deliver_pass(conn, deliver=None) -> str:
    """Claim ONE eligible row and deliver it. Returns a short tag naming what happened
    (idle | delivered | parked-unrouted | retry | dead-lettered) — the poller ignores it,
    the tests assert on it. The claim predicate excludes both delivered and dead-lettered
    rows and rows past the attempts ceiling, so a permanently failing row cannot starve its
    successors: the next pass claims the next id, not the poison one again."""
    deliver = deliver or ADAPTERS
    with conn.cursor() as cur:
        # Eligibility is delivered? no + dead-lettered? no + past its backoff gate. There is
        # NO `attempts < cap` here anymore: a transient row is bounded by wall-clock, not by
        # count, so capping the claim would abandon a legitimate message mid-outage. A backing
        # -off row (next_attempt_at in the future) is simply skipped, and its successor served.
        cur.execute(
            "SELECT id, kind, title, body, approval_id, approval_token, action, "
            "approval_state FROM outbox "
            "WHERE delivered_at IS NULL AND dead_letter_at IS NULL "
            "AND (next_attempt_at IS NULL OR next_attempt_at <= now()) "
            "ORDER BY id FOR UPDATE SKIP LOCKED LIMIT 1")
        r = cur.fetchone()
        if not r:
            conn.rollback()
            return "idle"
        row = dict(zip(["id", "kind", "title", "body", "approval_id", "approval_token",
                        "action", "approval_state"], r))
        sinks, routed = resolve_sinks(ROUTES, row["kind"])
        if not routed:
            # An unrouted kind is data loss if marked delivered. Park it (visible to the
            # console as an undelivered-but-terminal row) and log loudly instead.
            cur.execute("UPDATE outbox SET dead_letter_at=now(), "
                        "dead_letter_reason=%s WHERE id=%s",
                        (f"unrouted kind {row['kind']!r}", row["id"]))
            conn.commit()
            _count_dead_letter("unrouted")
            print(json.dumps({"level": "WARN", "msg": "unrouted kind parked",
                              "event": "dead_letter", "reason": "unrouted",
                              "id": row["id"], "kind": row["kind"]}), file=sys.stderr)
            return "parked-unrouted"
        try:
            for s in sinks:
                if s in deliver and TELEGRAM_TOKEN:
                    deliver[s](row)
            cur.execute("UPDATE outbox SET delivered_at=now(), attempts=attempts+1 "
                        "WHERE id=%s", (row["id"],))
            conn.commit()
            _count_delivered()
            return "delivered"
        except Exception as e:
            return _handle_failure(conn, cur, row, e)


def _handle_failure(conn, cur, row, e) -> str:
    """Classify the delivery error and act: permanent → drop now; transient → backoff and
    only drop after the wall-clock budget; unknown → the legacy count cap. Every drop
    increments orbit_notifier_dead_letter_total and logs a countable structured line."""
    cls = classify_failure(e)

    if cls == "permanent":
        cur.execute("UPDATE outbox SET attempts=attempts+1, dead_letter_at=now(), "
                    "dead_letter_reason=%s WHERE id=%s RETURNING attempts",
                    (f"permanent delivery failure: {str(e)[:200]}", row["id"]))
        attempts = cur.fetchone()[0]
        conn.commit()
        _count_dead_letter("permanent")
        _log_drop(row, attempts, "permanent", e)
        return "dead-lettered"

    if cls == "transient":
        # Advance the attempt count and anchor the wall-clock window on the first failure.
        cur.execute(
            "UPDATE outbox SET attempts=attempts+1, "
            "first_failed_at=COALESCE(first_failed_at, now()) WHERE id=%s "
            "RETURNING attempts, EXTRACT(EPOCH FROM (now()-first_failed_at))", (row["id"],))
        attempts, elapsed = cur.fetchone()
        if elapsed is not None and elapsed >= TRANSIENT_DEADLINE_S:
            cur.execute("UPDATE outbox SET dead_letter_at=now(), dead_letter_reason=%s "
                        "WHERE id=%s",
                        (f"transient failures for {int(elapsed)}s exceeded "
                         f"{int(TRANSIENT_DEADLINE_S)}s budget: {str(e)[:150]}", row["id"]))
            conn.commit()
            _count_dead_letter("transient-exhausted")
            _log_drop(row, attempts, "transient-exhausted", e)
            return "dead-lettered"
        backoff = backoff_seconds(attempts)
        cur.execute("UPDATE outbox SET next_attempt_at = now() + make_interval(secs => %s) "
                    "WHERE id=%s", (backoff, row["id"]))
        conn.commit()
        print(json.dumps({"level": "WARN", "msg": "delivery failed (transient, will retry)",
                          "id": row["id"], "attempts": attempts, "backoff_s": backoff,
                          "elapsed_s": elapsed, "err": str(e)}), file=sys.stderr)
        return "retry"

    # unknown: can't classify — retry a few times then give up loudly (legacy count cap).
    cur.execute("UPDATE outbox SET attempts=attempts+1 WHERE id=%s "
                "RETURNING attempts", (row["id"],))
    attempts = cur.fetchone()[0]
    tag = "retry"
    if attempts >= MAX_ATTEMPTS:
        cur.execute("UPDATE outbox SET dead_letter_at=now(), dead_letter_reason=%s "
                    "WHERE id=%s",
                    (f"delivery failed {attempts}x (unclassified): {str(e)[:200]}",
                     row["id"]))
        tag = "dead-lettered"
    conn.commit()
    if tag == "dead-lettered":
        _count_dead_letter("unknown")
        _log_drop(row, attempts, "unknown", e)
    else:
        print(json.dumps({"level": "ERROR", "msg": "delivery failed",
                          "id": row["id"], "attempts": attempts, "dead": tag,
                          "err": str(e)}), file=sys.stderr)
    return tag


def _log_drop(row, attempts, reason, e) -> None:
    """The structured, countable dead-letter line — the log-based twin of the metric, so a
    dropped decision is alertable from Loki too, never merely on stderr."""
    print(json.dumps({"level": "ERROR", "msg": "message dead-lettered",
                      "event": "dead_letter", "reason": reason, "id": row["id"],
                      "kind": row.get("kind"), "attempts": attempts, "err": str(e)}),
          file=sys.stderr)


def poller() -> None:
    while True:
        try:
            with psycopg.connect(DB_URL, autocommit=False) as conn:
                tag = deliver_pass(conn)
            if tag == "idle":
                time.sleep(2)
            elif tag in ("retry", "dead-lettered"):
                time.sleep(1)
        except Exception as e:
            print(json.dumps({"level": "ERROR", "msg": "poller loop", "err": str(e)}),
                  file=sys.stderr)
            time.sleep(5)


# ------------------------------------------------------------------ callback intake

def handle_callback(conn, cb: dict) -> str:
    """Resolve the gate a button press names — after checking who pressed and that the
    payload is authentic. Returns rejected-chat | rejected-sig | resolved | noop."""
    if not callback_authorized(cb, TELEGRAM_CHAT, APPROVER_IDS):
        print(json.dumps({"level": "WARN", "msg": "callback from unauthorized chat/user",
                          "from": (cb.get("from") or {}).get("id"),
                          "chat": ((cb.get("message") or {}).get("chat") or {}).get("id")}),
              file=sys.stderr)
        return "rejected-chat"
    parsed = parse_callback(CALLBACK_SECRET, cb.get("data", ""))
    if not parsed:
        print(json.dumps({"level": "WARN", "msg": "callback with bad/forged signature"}),
              file=sys.stderr)
        return "rejected-sig"
    decision, tok = parsed
    state = "approved" if decision == "ok" else "denied"
    frm = str((cb.get("from") or {}).get("id", ""))
    # One-shot: match by unguessable token and only from 'pending'. A guessed row id can't
    # be used (there is no id path), and a second press finds nothing pending to flip.
    row = conn.execute(
        "UPDATE outbox SET approval_state=%s, approved_by=%s, approved_at=now() "
        "WHERE approval_token=%s AND approval_state='pending' RETURNING id",
        (state, frm, tok)).fetchone()
    return "resolved" if row else "noop"


def telegram_webhook_poller() -> None:
    """Long-poll Telegram for button presses and resolve the matching gate. offset advances
    past handled updates; a duplicate press on an already-resolved gate is a no-op."""
    if not TELEGRAM_TOKEN:
        return
    offset = 0
    while True:
        try:
            upd = _tg("getUpdates", {"offset": offset, "timeout": 25})
            for u in upd.get("result", []):
                offset = u["update_id"] + 1
                cb = u.get("callback_query")
                if not cb:
                    continue
                with psycopg.connect(DB_URL, autocommit=True) as conn:
                    tag = handle_callback(conn, cb)
                if tag.startswith("rejected"):
                    _tg("answerCallbackQuery", {"callback_query_id": cb["id"],
                                                "text": "Not authorized"})
                elif tag == "resolved":
                    decision = cb.get("data", "").split(":")[0]
                    _tg("answerCallbackQuery", {"callback_query_id": cb["id"],
                        "text": "Recorded: " + ("approved" if decision == "ok" else "denied")})
                else:  # noop — nothing pending matched; don't claim a false resolution
                    _tg("answerCallbackQuery", {"callback_query_id": cb["id"],
                                                "text": "No pending approval"})
        except Exception as e:
            print(json.dumps({"level": "ERROR", "msg": "tg webhook", "err": str(e)}),
                  file=sys.stderr)
            time.sleep(5)


class Handler(BaseHTTPRequestHandler):
    timeout = 15

    def _send(self, code, body):
        p = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(p)))
        self.end_headers()
        self.wfile.write(p)

    def do_GET(self):
        if self.path == "/metrics":
            body = render_metrics().encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; version=0.0.4")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if self.path == "/healthz":
            try:
                with psycopg.connect(DB_URL, autocommit=True) as conn:
                    conn.execute("SELECT 1")
                self._send(200, {"ok": True})
            except Exception as e:
                self._send(503, {"ok": False, "err": str(e)})
        elif self.path.startswith("/approval/"):
            # The console reads a resolution by its unguessable token (not an enumerable
            # id). The token IS the capability, so a guessed/sequential value finds nothing.
            tok = self.path.rsplit("/", 1)[-1]
            with psycopg.connect(DB_URL, autocommit=True) as conn:
                r = conn.execute(
                    "SELECT approval_state, approved_by FROM outbox WHERE approval_token=%s",
                    (tok,)).fetchone()
            self._send(200 if r else 404,
                       {"state": r[0] if r else None, "approved_by": r[1] if r else None})
        else:
            self._send(404, {"error": "not found"})

    def do_POST(self):
        if self.path != "/notify":
            self._send(404, {"error": "not found"})
            return
        # Authenticate BEFORE reading the body: an anonymous caller must not be able to mint
        # an approval gate. Fail closed (503) when no secret is configured.
        if not NOTIFIER_TOKEN:
            self._send(503, {"error": "auth not configured"})
            return
        if not notify_authorized(NOTIFIER_TOKEN, self.headers.get("Authorization", "")):
            self._send(401, {"error": "unauthorized"})
            return
        try:
            n = int(self.headers.get("Content-Length", "0"))
            if n <= 0 or n > 100_000:
                self._send(400, {"error": "bad length"})
                return
            ev = json.loads(self.rfile.read(n))
        except Exception:
            self._send(400, {"error": "unparseable"})
            return
        kind = ev.get("kind", "default")
        title = ev.get("title")
        if not title:
            self._send(400, {"error": "title required"})
            return
        approval_id = ev.get("approval_id")
        # Mint an unguessable token for an approval row; the caller gets it back and hands it
        # to the console. The sequential row id is never used as the capability.
        token = secrets.token_urlsafe(24) if approval_id else None
        with psycopg.connect(DB_URL, autocommit=True) as conn:
            r = conn.execute(
                "INSERT INTO outbox (kind, title, body, approval_id, approval_token, "
                "action, approval_state) VALUES (%s,%s,%s,%s,%s,%s,%s) RETURNING id",
                (kind, title, ev.get("body", ""), approval_id, token,
                 ev.get("action"), "pending" if approval_id else None)).fetchone()
        self._send(202, {"id": r[0], "approval_id": approval_id, "approval_token": token})

    def log_message(self, *a):
        pass


def main() -> None:
    with psycopg.connect(DB_URL, autocommit=True) as conn:
        conn.execute(SCHEMA)
    threading.Thread(target=poller, daemon=True).start()
    threading.Thread(target=telegram_webhook_poller, daemon=True).start()
    print(json.dumps({"level": "INFO", "msg": "notifier up", "port": PORT}),
          file=sys.stderr)
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()


if __name__ == "__main__":
    main()
