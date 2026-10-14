"""Purchase requests and the decision step of the saga.

Every state change happens under the household row lock and is recorded as an event
in the same transaction, before any side effect is attempted."""
import json
from datetime import timedelta

from psycopg.types.json import Jsonb

from adapters.catalog.mock_catalog import Catalog
from apps.api.services import households, mandates
from apps.common import config
from apps.common.db import append_event, lock_household, notify, now, redis, tx
from core.budget.reservations import month_key
from core.mandates.diff import inr
from core.policy import engine
from core.saga import states as S
from memory.rhythm.burn import baseline_qty
from observability import tracing


def catalog() -> Catalog:
    return Catalog(redis())


def get(conn, purchase_id: str) -> dict | None:
    return conn.execute("SELECT * FROM purchases WHERE purchase_id = %s", (purchase_id,)).fetchone()


def set_state(conn, p: dict, new_state: str, actor: str, extra: dict | None = None, **fields) -> dict:
    S.check_transition(p["state"], new_state)
    sets = ["state = %s", "updated_at = now()"]
    vals = [new_state]
    for k, v in fields.items():
        sets.append(f"{k} = %s")
        vals.append(Jsonb(v) if isinstance(v, (dict, list)) else v)
    vals.append(p["purchase_id"])
    row = conn.execute(f"UPDATE purchases SET {', '.join(sets)} WHERE purchase_id = %s RETURNING *", vals).fetchone()
    append_event(conn, p["household_id"], f"purchase.{new_state.lower()}",
                 {"purchase_id": p["purchase_id"], "from": p["state"], **(extra or {})}, actor)
    return row


def add_note(conn, purchase_id: str, note: str):
    conn.execute("UPDATE purchases SET notes = notes || %s::jsonb WHERE purchase_id = %s",
                 (json.dumps([note]), purchase_id))


def describe(p: dict) -> str:
    return ", ".join(f"{i['qty']}× {i['name']}" for i in p["cart"]["items"])


def history(conn, household_id: str) -> list[dict]:
    """Completed purchases (incl. imported history), oldest first."""
    return conn.execute(
        "SELECT * FROM purchases WHERE household_id = %s AND state IN ('CONFIRMED','CAPTURED','PARTIALLY_REFUNDED') "
        "ORDER BY created_at", (household_id,)).fetchall()


def item_orders(conn, household_id: str) -> dict:
    """sku -> chronological [{at, qty}] from purchase history."""
    out = {}
    for p in history(conn, household_id):
        for it in p["cart"]["items"]:
            out.setdefault(it["sku"], []).append({"at": p["created_at"], "qty": it["qty"], "category": it["category"]})
    return out


def used_by_mandate(conn, household_id: str, key: str, exclude: str | None = None) -> dict:
    rows = conn.execute(
        "SELECT mandate_id, COALESCE(SUM(amount - refunded), 0) AS used FROM reservations "
        "WHERE household_id = %s AND month_key = %s AND status IN ('ACTIVE','CONVERTED') AND purchase_id <> %s "
        "GROUP BY mandate_id", (household_id, key, exclude or "")).fetchall()
    return {r["mandate_id"]: int(r["used"]) for r in rows}


def _context(conn, p: dict, acknowledged: bool, owner_approved: bool) -> engine.PurchaseContext:
    hh = p["household_id"]
    orders = item_orders(conn, hh)
    hist = history(conn, hh)
    member = households.member(conn, hh, p["requested_by"]) if p["requested_by"] else None
    return engine.PurchaseContext(
        member=member["member_id"] if member and member["active"] else None,
        role=member["role"] if member else None,
        items=p["cart"]["items"], merchant=p["merchant"],
        total=p["final_total"] if p["final_total"] is not None else p["cart_total"], now=now(),
        acknowledged=acknowledged, owner_approved=owner_approved,
        baselines={sku: baseline_qty(o) for sku, o in orders.items()},
        known_merchants={h["merchant"] for h in hist if h["purchase_id"] != p["purchase_id"]},
        has_history=bool(hist), odd_hour_check=config.ODD_HOUR_CHECK)


def speak(conn, household_id: str, text: str, data: dict | None = None):
    notify(conn, household_id, "speaker", "speak", text, data)


def decide(conn, p: dict, actor: str, acknowledged: bool = False, owner_approved: bool = False) -> dict:
    """Run the policy engine and act on its decision. Caller holds the household lock."""
    hh = p["household_id"]
    hold = conn.execute("SELECT * FROM reservations WHERE purchase_id = %s", (p["purchase_id"],)).fetchone()
    key = hold["month_key"] if hold else month_key(now())
    frozen = conn.execute("SELECT frozen FROM households WHERE id = %s", (hh,)).fetchone()["frozen"]
    ctx = _context(conn, p, acknowledged, owner_approved)
    with tracing.observe("policy engine", as_type="guardrail",
                         input={"purchase_id": p["purchase_id"], "merchant": p["merchant"], "items": p["cart"]["items"],
                                "total_paise": p["final_total"] if p["final_total"] is not None else p["cart_total"],
                                "requested_by": p["requested_by"], "acknowledged": acknowledged,
                                "owner_approved": owner_approved, "frozen": frozen, "month_key": key}) as g:
        d = engine.evaluate(ctx, mandates.current(conn, hh), used_by_mandate(conn, hh, key, p["purchase_id"]),
                            frozen, mandates.meta(conn, hh))
        dd = d.as_dict()
        g.update(output=dd, level="WARNING" if d.outcome == engine.DENY else None)
    append_event(conn, hh, "policy.decided", {"purchase_id": p["purchase_id"], "decision": dd,
                                              "acknowledged": acknowledged, "owner_approved": owner_approved}, "policy")
    fields = {"decision": dd, "mandate_id": d.mandate_id, "mandate_version": d.mandate_version}

    if d.outcome == engine.ALLOW:
        p = set_state(conn, p, S.CHECKED, "policy", {"code": d.code}, **fields)
        total = p["final_total"] if p["final_total"] is not None else p["cart_total"]
        t = now()
        if hold:
            conn.execute("UPDATE reservations SET amount = %s, mandate_id = %s, status = 'ACTIVE', expires_at = %s "
                         "WHERE purchase_id = %s", (total, d.mandate_id or "one-off", t + timedelta(seconds=config.HOLD_TTL_SECONDS),
                                                    p["purchase_id"]))
        else:
            conn.execute("INSERT INTO reservations (purchase_id, household_id, mandate_id, amount, month_key, "
                         "reserved_at, expires_at, status) VALUES (%s,%s,%s,%s,%s,%s,%s,'ACTIVE')",
                         (p["purchase_id"], hh, d.mandate_id or "one-off", total, key, t,
                          t + timedelta(seconds=config.HOLD_TTL_SECONDS)))
        # Hold + outbox row in the same transaction: a crash after commit is picked up by the worker.
        conn.execute("INSERT INTO outbox (household_id, purchase_id, step) VALUES (%s,%s,'create') "
                     "ON CONFLICT (purchase_id, step) DO UPDATE SET status = 'PENDING', next_attempt_at = now()",
                     (hh, p["purchase_id"]))
        p = set_state(conn, p, S.RESERVED, "policy", {"amount": total, "month_key": key, "mandate_id": d.mandate_id})
        speak(conn, hh, f"Okay, ordering {describe(p)} for {inr(total // 100)}.", {"purchase_id": p["purchase_id"]})
    elif d.outcome == engine.CONFIRM:
        p = set_state(conn, p, S.AWAITING_CONFIRMATION, "policy", {"code": d.code}, **fields)
        speak(conn, hh, d.reason, {"purchase_id": p["purchase_id"], "needs": "confirm"})
    elif d.outcome == engine.ESCALATE:
        p = set_state(conn, p, S.ESCALATED, "policy", {"code": d.code}, **fields)
        if hold:  # nothing is held while waiting for an owner; approval re-reserves
            conn.execute("UPDATE reservations SET status = 'RELEASED' WHERE purchase_id = %s", (p["purchase_id"],))
        from apps.api.services import approvals
        approvals.create_escalation(conn, p, d)
        speak(conn, hh, d.reason, {"purchase_id": p["purchase_id"], "needs": "owner"})
    else:
        p = set_state(conn, p, S.DENIED, "policy", {"code": d.code}, **fields)
        if hold:
            conn.execute("UPDATE reservations SET status = 'RELEASED' WHERE purchase_id = %s", (p["purchase_id"],))
        conn.execute("UPDATE outbox SET status = 'CANCELLED' WHERE purchase_id = %s AND status = 'PENDING'",
                     (p["purchase_id"],))
        speak(conn, hh, d.reason, {"purchase_id": p["purchase_id"]})
    return {"purchase": p, "decision": dd}


def request(household_id: str, purchase_id: str, requested_by: str | None, speaker_claim: str | None,
            source: str, utterance: str | None, merchant: str, lines: list[dict], notes: list[str] | None = None,
            force_confirm: str | None = None) -> dict:
    """Create a purchase (idempotent on purchase_id) and decide it."""
    priced = catalog().price_cart(household_id, lines)
    with tx() as conn:
        lock_household(conn, household_id)
        existing = get(conn, purchase_id)
        if existing:
            if existing["household_id"] != household_id:
                raise PermissionError("purchase id belongs to another household")
            append_event(conn, household_id, "purchase.duplicate_request", {"purchase_id": purchase_id}, source)
            return {"purchase": existing, "decision": existing["decision"], "duplicate": True}
        cart = {"items": priced["items"], "subtotal": priced["subtotal"], "fees": priced["fees"]}
        p = conn.execute(
            "INSERT INTO purchases (purchase_id, household_id, requested_by, speaker_claim, source, utterance, "
            "merchant, cart, cart_total, state, notes) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING *",
            (purchase_id, household_id, requested_by, speaker_claim, source, utterance, merchant, Jsonb(cart),
             priced["total"], S.REQUESTED, Jsonb(notes or []))).fetchone()
        append_event(conn, household_id, "purchase.requested",
                     {"purchase_id": purchase_id, "requested_by": requested_by, "speaker_claim": speaker_claim,
                      "source": source, "merchant": merchant, "cart": cart, "cart_total": priced["total"]},
                     requested_by or "unknown")
        if force_confirm:
            p = set_state(conn, p, S.AWAITING_CONFIRMATION, "assistant", {"code": "AMBIGUOUS"},
                          decision={"outcome": "CONFIRM", "code": "AMBIGUOUS", "reason": force_confirm})
            speak(conn, household_id, force_confirm, {"purchase_id": purchase_id, "needs": "confirm"})
            return {"purchase": p, "decision": p["decision"], "duplicate": False}
        out = decide(conn, p, "policy")
        out["duplicate"] = False
        return out


def confirm(household_id: str, confirmer: str | None, purchase_id: str | None = None) -> dict:
    with tx() as conn:
        lock_household(conn, household_id)
        p = _pending_confirmation(conn, household_id, purchase_id)
        if not p:
            return {"ok": False, "speech": "There's nothing waiting for a yes."}
        m = households.member(conn, household_id, confirmer)
        code = (p["decision"] or {}).get("code")
        if not m or not m["active"]:
            return {"ok": False, "speech": "I need a family member I recognise to confirm."}
        if m["role"] not in ("owner", "adult") and not (code != "UNKNOWN_SPEAKER" and confirmer == p["requested_by"]):
            return {"ok": False, "speech": "An adult needs to confirm this one."}
        append_event(conn, household_id, "purchase.confirmed_by_member",
                     {"purchase_id": p["purchase_id"], "member": confirmer, "code": code}, confirmer)
        if p["requested_by"] is None:
            conn.execute("UPDATE purchases SET requested_by = %s WHERE purchase_id = %s", (confirmer, p["purchase_id"]))
            p = get(conn, p["purchase_id"])
        out = decide(conn, p, confirmer, acknowledged=True)
        return {"ok": True, **out}


def cancel(household_id: str, member_id: str | None, purchase_id: str | None = None) -> dict:
    with tx() as conn:
        lock_household(conn, household_id)
        p = _pending_confirmation(conn, household_id, purchase_id)
        if not p:
            return {"ok": False, "speech": "There's nothing to cancel."}
        p = set_state(conn, p, S.DENIED, member_id or "unknown", {"code": "CANCELLED_BY_USER"})
        conn.execute("UPDATE reservations SET status = 'RELEASED' WHERE purchase_id = %s AND status = 'ACTIVE'",
                     (p["purchase_id"],))
        conn.execute("UPDATE outbox SET status = 'CANCELLED' WHERE purchase_id = %s AND status = 'PENDING'",
                     (p["purchase_id"],))
        return {"ok": True, "purchase": p, "speech": "Okay, cancelled."}


def _pending_confirmation(conn, household_id, purchase_id):
    if purchase_id:
        p = get(conn, purchase_id)
        return p if p and p["household_id"] == household_id and p["state"] == S.AWAITING_CONFIRMATION else None
    return conn.execute("SELECT * FROM purchases WHERE household_id = %s AND state = %s "
                        "ORDER BY updated_at DESC LIMIT 1", (household_id, S.AWAITING_CONFIRMATION)).fetchone()


def record_delivery(purchase_id: str, delivered: list[dict]) -> dict:
    """Merchant delivery report. Missing items are refunded with their own idempotent request id."""
    with tx() as conn:
        p = get(conn, purchase_id)
        if not p:
            raise KeyError(purchase_id)
        lock_household(conn, p["household_id"])
        p = get(conn, purchase_id)
        if p["state"] not in (S.CONFIRMED, S.CAPTURED, S.PARTIALLY_REFUNDED):
            raise ValueError(f"cannot record delivery in state {p['state']}")
        got = {d["sku"]: d["qty"] for d in delivered}
        missing, amount = [], 0
        for it in p["cart"]["items"]:
            short = it["qty"] - min(it["qty"], got.get(it["sku"], 0))
            if short > 0:
                missing.append({"sku": it["sku"], "name": it["name"], "qty": short})
                amount += short * it["unit_price"]
        if all(got.get(i["sku"], 0) == 0 for i in p["cart"]["items"]):
            amount = (p["final_total"] or p["cart_total"]) - p["refunded"]  # nothing arrived: refund fees too
        append_event(conn, p["household_id"], "purchase.delivery_reported",
                     {"purchase_id": purchase_id, "delivered": delivered, "missing": missing, "refund": amount},
                     "merchant")
        if amount <= 0:
            return {"refund": 0, "missing": []}
        n = conn.execute("SELECT COUNT(*) AS n FROM outbox WHERE purchase_id = %s AND step LIKE 'refund%%'",
                         (purchase_id,)).fetchone()["n"]
        step = "refund" if n == 0 else f"refund:{n + 1}"
        conn.execute("INSERT INTO outbox (household_id, purchase_id, step, payload) VALUES (%s,%s,%s,%s)",
                     (p["household_id"], purchase_id, step, Jsonb({"amount": amount, "missing": missing})))
        return {"refund": amount, "missing": missing, "step": step}
