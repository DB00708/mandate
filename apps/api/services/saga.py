"""Payment saga execution: outbox dispatch, reconciliation, hold expiry and webhooks.

Rules that make money exactly-once:
  * every step is recorded (outbox row) before it acts;
  * PayPal-Request-Id is a pure function of (purchase_id, step) — retries never mint a new key;
  * an unknown outcome is retried with the same key or reconciled by reading the order;
  * state only moves forward (late/duplicate webhooks are ignored)."""
import logging
from datetime import timedelta

from psycopg.types.json import Jsonb

from adapters.paypal.client import (PayPalClient, PayPalRejected, PayPalTimeout, PayPalUnavailable,
                                    approve_link, capture_id_of)
from apps.api.services import approvals, purchases
from apps.common import config
from apps.common.db import append_event, lock_household, notify, now, redis, tx
from core.mandates.diff import inr
from core.saga import states as S
from core.saga.idempotency import request_id

log = logging.getLogger("mandate.saga")
_paypal = None


def paypal() -> PayPalClient:
    global _paypal
    if _paypal is None:
        _paypal = PayPalClient()
    return _paypal


# ---------------------------------------------------------------- circuit breaker
def breaker_open() -> bool:
    return bool(redis().get("breaker:paypal:open"))


def breaker_failure():
    r = redis()
    n = r.incr("breaker:paypal:fails")
    r.expire("breaker:paypal:fails", 120)
    if n >= config.BREAKER_THRESHOLD:
        r.set("breaker:paypal:open", "1", ex=max(1, int(config.BREAKER_COOLDOWN_SECONDS)))
        r.delete("breaker:paypal:fails")
        log.warning("circuit breaker OPEN for %ss", config.BREAKER_COOLDOWN_SECONDS)


def breaker_success():
    redis().delete("breaker:paypal:fails")


def backoff(attempts: int) -> float:
    return min(30.0, 0.5 * (2 ** attempts))


# ---------------------------------------------------------------- outbox
def claim_due(limit: int = 10) -> list[dict]:
    """Lease due outbox rows (SKIP LOCKED so several workers can run)."""
    with tx() as conn:
        rows = conn.execute(
            "SELECT * FROM outbox WHERE status = 'PENDING' AND next_attempt_at <= now() ORDER BY id LIMIT %s "
            "FOR UPDATE SKIP LOCKED", (limit,)).fetchall()
        for r in rows:  # lease: nobody else picks it up while we call PayPal
            conn.execute("UPDATE outbox SET next_attempt_at = now() + interval '60 seconds', attempts = attempts + 1 "
                         "WHERE id = %s", (r["id"],))
        return rows


def _reschedule(conn, item, error: str, delay: float | None = None):
    attempts = item["attempts"] + 1
    if attempts >= config.MAX_OUTBOX_ATTEMPTS:
        conn.execute("UPDATE outbox SET status = 'DEAD', last_error = %s WHERE id = %s", (error, item["id"]))
        return False
    conn.execute("UPDATE outbox SET next_attempt_at = now() + %s * interval '1 second', last_error = %s WHERE id = %s",
                 (delay if delay is not None else backoff(attempts), error, item["id"]))
    return True


def _attempt(conn, pid, step, rid, outcome, detail=None, order_id=None):
    conn.execute("INSERT INTO payment_attempts (purchase_id, step, request_id, paypal_order_id, outcome, detail) "
                 "VALUES (%s,%s,%s,%s,%s,%s)", (pid, step, rid, order_id, outcome, (detail or "")[:500]))


def _done(conn, item):
    conn.execute("UPDATE outbox SET status = 'DONE', last_error = NULL WHERE id = %s", (item["id"],))


def process(item: dict):
    step = item["step"]
    if breaker_open():
        with tx() as conn:
            _reschedule(conn, item, "circuit open", config.BREAKER_COOLDOWN_SECONDS)
            conn.execute("UPDATE outbox SET attempts = attempts - 1 WHERE id = %s", (item["id"],))
        return
    if step == "create":
        _step_create(item)
    elif step == "capture":
        _step_capture(item)
    elif step.startswith("refund"):
        _step_refund(item)


def _unavailable(item, pid, step, rid, e, hh):
    breaker_failure()
    with tx() as conn:
        lock_household(conn, hh)
        _attempt(conn, pid, step, rid, "UNAVAILABLE", str(e))
        _reschedule(conn, item, str(e))
        p = purchases.get(conn, pid)
        if "queued: PayPal unavailable" not in p["notes"]:
            purchases.add_note(conn, pid, "queued: PayPal unavailable")
            purchases.speak(conn, hh, "PayPal isn't reachable right now. I've queued the order and will keep "
                                      "trying.", {"purchase_id": pid})


def _unknown(item, pid, step, rid, e, hh):
    breaker_failure()
    with tx() as conn:
        lock_household(conn, hh)
        _attempt(conn, pid, step, rid, "UNKNOWN", str(e))
        conn.execute("UPDATE purchases SET uncertain = %s, updated_at = now() WHERE purchase_id = %s "
                     "AND state IN ('RESERVED', 'ORDER_CREATED')", (step, pid))
        append_event(conn, hh, "payment.outcome_unknown", {"purchase_id": pid, "step": step, "request_id": rid},
                     "saga")
        _reschedule(conn, item, f"unknown: {e}")


def _step_create(item):
    pid, hh = item["purchase_id"], item["household_id"]
    rid = request_id(pid, "create")
    with tx() as conn:
        lock_household(conn, hh)
        p = purchases.get(conn, pid)
        if p["state"] != S.RESERVED:
            conn.execute("UPDATE outbox SET status = 'CANCELLED' WHERE id = %s", (item["id"],))
            return
        if p["uncertain"] != "create":
            # Price the cart at the merchant now: limits apply to the final total incl. fees.
            q = purchases.catalog().quote(hh, p["merchant"], p["cart"]["items"])
            hold = conn.execute("SELECT * FROM reservations WHERE purchase_id = %s", (pid,)).fetchone()
            if q["total"] > hold["amount"]:
                conn.execute("UPDATE purchases SET final_total = %s WHERE purchase_id = %s", (q["total"], pid))
                _done(conn, item)
                old = hold["amount"]
                p = purchases.set_state(conn, p, S.AWAITING_CONFIRMATION, "saga",
                                        {"code": "PRICE_CHANGED", "from": old, "to": q["total"]},
                                        decision={"outcome": "CONFIRM", "code": "PRICE_CHANGED",
                                                  "reason": f"price changed from {inr(old // 100)} to {inr(q['total'] // 100)}"})
                purchases.speak(conn, hh, f"The price of {purchases.describe(p)} went up from {inr(old // 100)} to "
                                          f"{inr(q['total'] // 100)} at checkout. Should I still buy it?",
                                {"purchase_id": pid, "needs": "confirm"})
                return
            if q["total"] != p["final_total"]:
                conn.execute("UPDATE purchases SET final_total = %s WHERE purchase_id = %s", (q["total"], pid))
                conn.execute("UPDATE reservations SET amount = %s WHERE purchase_id = %s", (q["total"], pid))
                p = purchases.get(conn, pid)
    total = p["final_total"]
    try:
        order = paypal().create_order(rid, pid, hh, total, purchases.describe(p))
    except PayPalTimeout as e:
        return _unknown(item, pid, "create", rid, e, hh)
    except PayPalUnavailable as e:
        return _unavailable(item, pid, "create", rid, e, hh)
    except PayPalRejected as e:
        return _failed(item, pid, hh, "create", rid, e)
    breaker_success()
    with tx() as conn:
        lock_household(conn, hh)
        p = purchases.get(conn, pid)
        _attempt(conn, pid, "create", rid, "OK", order.get("status"), order["id"])
        _done(conn, item)
        if p["state"] != S.RESERVED:
            return  # expired/cancelled while we were calling; the order is never captured
        conn.execute("UPDATE purchases SET paypal_order_id = %s, uncertain = NULL WHERE purchase_id = %s",
                     (order["id"], pid))
        purchases.set_state(conn, p, S.ORDER_CREATED, "saga", {"paypal_order_id": order["id"], "request_id": rid,
                                                               "status": order.get("status")})
        link = approve_link(order)
        if link:  # sandbox without vaulting: one-tap buyer approval fallback
            notify(conn, hh, "owners", "payer_action", f"Approve the PayPal payment for {purchases.describe(p)}",
                   {"purchase_id": pid, "url": link})
        conn.execute("INSERT INTO outbox (household_id, purchase_id, step) VALUES (%s,%s,'capture') "
                     "ON CONFLICT (purchase_id, step) DO NOTHING", (hh, pid))


def _step_capture(item):
    pid, hh = item["purchase_id"], item["household_id"]
    rid = request_id(pid, "capture")
    with tx() as conn:
        p = purchases.get(conn, pid)
    if p["state"] != S.ORDER_CREATED:
        with tx() as conn:
            conn.execute("UPDATE outbox SET status = 'CANCELLED' WHERE id = %s", (item["id"],))
        return
    try:
        order = paypal().capture(rid, p["paypal_order_id"])
    except PayPalTimeout as e:
        return _unknown(item, pid, "capture", rid, e, hh)
    except PayPalUnavailable as e:
        return _unavailable(item, pid, "capture", rid, e, hh)
    except PayPalRejected as e:
        if "ORDER_NOT_APPROVED" in str(e):  # waiting for the buyer-approval fallback
            with tx() as conn:
                _reschedule(conn, item, "awaiting buyer approval", 5)
            return
        if "ORDER_ALREADY_CAPTURED" in str(e):
            return reconcile_one(pid)
        return _failed(item, pid, hh, "capture", rid, e)
    breaker_success()
    with tx() as conn:
        lock_household(conn, hh)
        _attempt(conn, pid, "capture", rid, "OK", order.get("status"), order["id"])
        _done(conn, item)
        apply_captured(conn, purchases.get(conn, pid), order, "capture-response")


def apply_captured(conn, p: dict, order: dict, via: str):
    """Idempotently record that money moved. Caller holds the household lock."""
    hh, pid = p["household_id"], p["purchase_id"]
    cap = capture_id_of(order)
    conn.execute("UPDATE purchases SET capture_id = COALESCE(capture_id, %s), uncertain = NULL WHERE purchase_id = %s",
                 (cap, pid))
    conn.execute("UPDATE outbox SET status = 'DONE' WHERE purchase_id = %s AND step = 'capture' AND status = 'PENDING'",
                 (pid,))
    if p["state"] == S.EXPIRED:
        # Captured after we gave up on it (in-flight race): give the money back.
        purchases.add_note(conn, pid, "captured after expiry: refunding")
        conn.execute("INSERT INTO outbox (household_id, purchase_id, step, payload) VALUES (%s,%s,'refund',%s) "
                     "ON CONFLICT (purchase_id, step) DO NOTHING", (hh, pid, Jsonb({"amount": p["final_total"]})))
        return
    if p["state"] == S.ORDER_CREATED:
        conn.execute("UPDATE reservations SET status = 'CONVERTED' WHERE purchase_id = %s", (pid,))
        p = purchases.set_state(conn, p, S.CAPTURED, "saga", {"capture_id": cap, "via": via})
    if via in ("webhook", "reconciler") and p["state"] == S.CAPTURED:
        confirm(conn, p, via)


def confirm(conn, p: dict, via: str):
    hh, pid = p["household_id"], p["purchase_id"]
    p = purchases.set_state(conn, p, S.CONFIRMED, via, {"via": via}, confirmed_at=now())
    m = conn.execute("SELECT doc FROM mandate_versions WHERE mandate_id = %s AND recorded_to IS NULL "
                     "ORDER BY version DESC LIMIT 1", (p["mandate_id"],)).fetchone()
    rule = f" under {m['doc']['owner'].capitalize()}'s {m['doc']['label']} rule" if m else ""
    used = purchases.used_by_mandate(conn, hh, conn.execute(
        "SELECT month_key FROM reservations WHERE purchase_id = %s", (pid,)).fetchone()["month_key"])
    left = ""
    if m:
        remaining = m["doc"]["limits"]["per_month_inr"] * 100 - used.get(p["mandate_id"], 0)
        left = f" {inr(max(remaining, 0) // 100)} left this month."
    purchases.speak(conn, hh, f"Done — ordered {purchases.describe(p)} for {inr(p['final_total'] // 100)}{rule}.{left}",
                    {"purchase_id": pid})


def _failed(item, pid, hh, step, rid, e):
    with tx() as conn:
        lock_household(conn, hh)
        _attempt(conn, pid, step, rid, "REJECTED", str(e))
        conn.execute("UPDATE outbox SET status = 'DEAD', last_error = %s WHERE id = %s", (str(e)[:500], item["id"]))
        p = purchases.get(conn, pid)
        if p["state"] in (S.RESERVED, S.ORDER_CREATED):
            conn.execute("UPDATE reservations SET status = 'RELEASED' WHERE purchase_id = %s", (pid,))
            purchases.set_state(conn, p, S.FAILED, "saga", {"step": step, "error": str(e)[:200]})
            purchases.speak(conn, hh, f"PayPal declined the payment for {purchases.describe(p)}. Nothing was charged.")


def _step_refund(item):
    pid, hh = item["purchase_id"], item["household_id"]
    rid = request_id(pid, item["step"])
    amount = item["payload"]["amount"]
    with tx() as conn:
        p = purchases.get(conn, pid)
    try:
        paypal().refund(rid, p["capture_id"], amount)
    except PayPalTimeout as e:
        return _unknown(item, pid, item["step"], rid, e, hh)
    except PayPalUnavailable as e:
        return _unavailable(item, pid, item["step"], rid, e, hh)
    except PayPalRejected as e:
        with tx() as conn:
            _attempt(conn, pid, item["step"], rid, "REJECTED", str(e))
            conn.execute("UPDATE outbox SET status = 'DEAD', last_error = %s WHERE id = %s", (str(e)[:500], item["id"]))
        return
    breaker_success()
    with tx() as conn:
        lock_household(conn, hh)
        _attempt(conn, pid, item["step"], rid, "OK")
        _done(conn, item)
        p = purchases.get(conn, pid)
        refunded = p["refunded"] + amount
        conn.execute("UPDATE purchases SET refunded = %s, uncertain = NULL WHERE purchase_id = %s", (refunded, pid))
        conn.execute("UPDATE reservations SET refunded = refunded + %s WHERE purchase_id = %s", (amount, pid))
        target = S.REFUNDED if refunded >= (p["final_total"] or 0) else S.PARTIALLY_REFUNDED
        if S.can_transition(p["state"], target):
            purchases.set_state(conn, p, target, "saga", {"amount": amount, "step": item["step"]})
        else:
            append_event(conn, hh, "purchase.refunded", {"purchase_id": pid, "amount": amount}, "saga")
        missing = item["payload"].get("missing") or []
        what = ", ".join(f"{m['qty']}× {m['name']}" for m in missing) or "the order"
        purchases.speak(conn, hh, f"Refunded {inr(amount // 100)} for {what}; that budget is available again.",
                        {"purchase_id": pid})


# ---------------------------------------------------------------- reconciler
def reconcile_due():
    with tx() as conn:
        rows = conn.execute(
            "SELECT purchase_id FROM purchases WHERE paypal_order_id IS NOT NULL AND "
            "((state = 'CAPTURED') OR (state = 'ORDER_CREATED' AND uncertain = 'capture')) "
            "AND updated_at < now() - %s * interval '1 second' LIMIT 20",
            (config.RECONCILE_AFTER_SECONDS,)).fetchall()
    for r in rows:
        reconcile_one(r["purchase_id"])


def reconcile_one(pid: str):
    with tx() as conn:
        p = purchases.get(conn, pid)
    if not p or not p["paypal_order_id"]:
        return
    try:
        order = paypal().get_order(p["paypal_order_id"])
    except (PayPalTimeout, PayPalUnavailable, PayPalRejected) as e:
        log.info("reconcile %s: %s", pid, e)
        with tx() as conn:
            conn.execute("UPDATE purchases SET updated_at = now() WHERE purchase_id = %s", (pid,))
        return
    with tx() as conn:
        lock_household(conn, p["household_id"])
        p = purchases.get(conn, pid)
        append_event(conn, p["household_id"], "payment.reconciled",
                     {"purchase_id": pid, "paypal_status": order.get("status")}, "reconciler")
        if order.get("status") == "COMPLETED":
            apply_captured(conn, p, order, "reconciler")
        else:
            conn.execute("UPDATE purchases SET updated_at = now() WHERE purchase_id = %s", (pid,))


# ---------------------------------------------------------------- expiry
def expire_holds():
    with tx() as conn:
        rows = conn.execute("SELECT r.purchase_id, r.household_id FROM reservations r JOIN purchases p USING "
                            "(purchase_id) WHERE r.status = 'ACTIVE' AND r.expires_at < now() AND p.state IN "
                            "('RESERVED','AWAITING_CONFIRMATION','ORDER_CREATED') LIMIT 20").fetchall()
    for r in rows:
        with tx() as conn:
            p = purchases.get(conn, r["purchase_id"])
        if p["state"] == S.ORDER_CREATED:
            if p["uncertain"] == "capture":
                reconcile_one(p["purchase_id"])  # never expire money that might have moved
                continue
            try:
                order = paypal().get_order(p["paypal_order_id"])
                if order.get("status") == "COMPLETED":
                    with tx() as conn:
                        lock_household(conn, r["household_id"])
                        apply_captured(conn, purchases.get(conn, p["purchase_id"]), order, "reconciler")
                    continue
            except (PayPalTimeout, PayPalUnavailable, PayPalRejected):
                pass  # not captured by us (capture not attempted or definitively failed)
        with tx() as conn:
            lock_household(conn, r["household_id"])
            p = purchases.get(conn, r["purchase_id"])
            if p["state"] not in (S.RESERVED, S.AWAITING_CONFIRMATION, S.ORDER_CREATED) or p["uncertain"] == "capture":
                continue
            conn.execute("UPDATE reservations SET status = 'RELEASED' WHERE purchase_id = %s", (p["purchase_id"],))
            conn.execute("UPDATE outbox SET status = 'CANCELLED' WHERE purchase_id = %s AND status = 'PENDING'",
                         (p["purchase_id"],))
            purchases.set_state(conn, p, S.EXPIRED, "saga", {"code": "HOLD_EXPIRED"})
            purchases.speak(conn, r["household_id"], f"I couldn't complete the order for {purchases.describe(p)}; "
                                                     f"the budget hold has been released.", {"purchase_id": p["purchase_id"]})


def expire_approvals():
    with tx() as conn:
        rows = conn.execute("SELECT * FROM approvals WHERE status = 'PENDING' AND expires_at < now() LIMIT 20").fetchall()
    for a in rows:
        with tx() as conn:
            lock_household(conn, a["household_id"])
            n = conn.execute("UPDATE approvals SET status = 'EXPIRED' WHERE approval_id = %s AND status = 'PENDING' "
                             "RETURNING approval_id", (a["approval_id"],)).fetchone()
            if not n:
                continue
            append_event(conn, a["household_id"], "approval.expired", {"approval_id": a["approval_id"]}, "system")
            if a["kind"] == "escalation":
                p = purchases.get(conn, a["subject_id"])
                if p and p["state"] == S.ESCALATED:
                    purchases.set_state(conn, p, S.DENIED, "system", {"code": "ESCALATION_EXPIRED"})
                    purchases.speak(conn, a["household_id"], f"Nobody approved {purchases.describe(p)} in time, so I "
                                                             f"didn't buy it.", {"purchase_id": p["purchase_id"]})
    with tx() as conn:  # unconfirmed asks without a hold
        rows = conn.execute(
            "SELECT p.* FROM purchases p LEFT JOIN reservations r USING (purchase_id) WHERE p.state = "
            "'AWAITING_CONFIRMATION' AND r.purchase_id IS NULL AND p.updated_at < now() - %s * interval '1 second' "
            "LIMIT 20", (config.CONFIRM_TTL_SECONDS,)).fetchall()
        for p in rows:
            lock_household(conn, p["household_id"])
            purchases.set_state(conn, p, S.EXPIRED, "system", {"code": "CONFIRMATION_EXPIRED"})


def apply_cooled_off():
    with tx() as conn:
        rows = conn.execute("SELECT * FROM approvals WHERE status = 'APPROVED' AND kind = 'rule_change' "
                            "AND effective_at <= now() LIMIT 20").fetchall()
    for a in rows:
        with tx() as conn:
            lock_household(conn, a["household_id"])
            a = conn.execute("SELECT * FROM approvals WHERE approval_id = %s AND status = 'APPROVED'",
                             (a["approval_id"],)).fetchone()
            if a:
                approvals.apply_rule_change(conn, a)


# ---------------------------------------------------------------- webhooks
def handle_webhook(event: dict) -> dict:
    eid, etype = event.get("id"), event.get("event_type")
    res = event.get("resource", {})
    pid = res.get("custom_id")
    if not eid or not pid:
        return {"status": "ignored", "why": "no event id / custom_id"}
    with tx() as conn:
        p = purchases.get(conn, pid)
        if not p:
            return {"status": "ignored", "why": "unknown purchase"}
        lock_household(conn, p["household_id"])
        inserted = conn.execute("INSERT INTO webhook_events (event_id, event_type, purchase_id) VALUES (%s,%s,%s) "
                                "ON CONFLICT DO NOTHING RETURNING event_id", (eid, etype, pid)).fetchone()
        if not inserted:
            append_event(conn, p["household_id"], "webhook.duplicate_ignored", {"event_id": eid, "type": etype},
                         "paypal")
            return {"status": "duplicate"}
        p = purchases.get(conn, pid)
        append_event(conn, p["household_id"], "webhook.received",
                     {"event_id": eid, "type": etype, "purchase_id": pid, "state_before": p["state"]}, "paypal")
        if etype == "PAYMENT.CAPTURE.COMPLETED":
            if p["state"] in (S.ORDER_CREATED, S.CAPTURED, S.EXPIRED):
                order = {"status": "COMPLETED", "purchase_units": [{"payments": {"captures": [res]}}]}
                apply_captured(conn, p, order, "webhook")
                return {"status": "applied"}
            return {"status": "stale", "state": p["state"]}  # monotonic: never move backwards
        return {"status": "ignored", "why": f"unhandled {etype}"}


def tick():
    """One worker iteration."""
    for item in claim_due():
        try:
            process(item)
        except Exception:  # keep the loop alive; the lease makes it retry later
            log.exception("outbox item %s failed", item["id"])
    for fn in (reconcile_due, expire_holds, expire_approvals, apply_cooled_off):
        try:
            fn()
        except Exception:
            log.exception("%s failed", fn.__name__)


def stuck_summary(conn, household_id: str) -> list[dict]:
    return conn.execute("SELECT purchase_id, step, status, attempts, last_error FROM outbox WHERE household_id = %s "
                        "AND status <> 'DONE' ORDER BY id", (household_id,)).fetchall()


def hold_ttl() -> timedelta:
    return timedelta(seconds=config.HOLD_TTL_SECONDS)
