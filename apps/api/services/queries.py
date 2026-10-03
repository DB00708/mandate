"""Read side: budgets, the spending constitution, 'why', 'what if', rhythm and the ledger."""
from datetime import datetime, timedelta

from apps.api.services import households, mandates, purchases
from apps.common import config
from apps.common.db import append_event, lock_household, notify, now, tx
from core.budget.reservations import IST, month_bounds, month_key, previous_month_key
from core.ledger.hashchain import verify_chain
from core.mandates.diff import fmt_date, inr
from core.policy.engine import pause_hit
from memory.rhythm.burn import forecast
from memory.temporal.bitemporal import counterfactual


def budgets(conn, household_id: str) -> list[dict]:
    key = month_key(now())
    used = purchases.used_by_mandate(conn, household_id, key)
    meta = mandates.meta(conn, household_id)
    out = []
    for d in mandates.current(conn, household_id):
        limit = d["limits"]["per_month_inr"] * 100
        u = used.get(d["mandate_id"], 0)
        out.append({"mandate_id": d["mandate_id"], "label": d["label"], "owner": d["owner"], "limit": limit,
                    "used": u, "left": limit - u, "per_order": d["limits"]["per_order_inr"] * 100,
                    "members_allowed": d["members_allowed"], "version": d["version"], "meta": meta.get(d["mandate_id"])})
    return out


def budget_speech(household_id: str, who: str | None, role: str) -> dict:
    with tx() as conn:
        b = budgets(conn, household_id)
    if not b:
        return {"speech": "There are no spending rules yet."}
    parts = []
    for x in b:
        if role == "minor" and who not in x["members_allowed"]:
            continue  # minors don't hear the household's amounts
        if role in ("guest",) or who is None:
            continue
        parts.append(f"{inr(x['left'] // 100)} left of {inr(x['limit'] // 100)} on the {x['label']} rule")
    if not parts:
        return {"speech": "I can't share household budgets with you, sorry."}
    return {"speech": "; ".join(parts) + " this month.", "budgets": b if role != "minor" else None}


def constitution(household_id: str) -> dict:
    """Every active rule in plain language: who set it, when, how much is left."""
    with tx() as conn:
        hh = conn.execute("SELECT * FROM households WHERE id = %s", (household_id,)).fetchone()
        b = {x["mandate_id"]: x for x in budgets(conn, household_id)}
        rules = []
        for d in mandates.current(conn, household_id):
            x = b[d["mandate_id"]]
            sentence = (f"{', '.join(m.capitalize() for m in d['members_allowed'])} may buy "
                        f"{', '.join(c.replace('_', ' ') for c in d['scope']['categories'])} up to "
                        f"{inr(d['limits']['per_order_inr'])} per order and {inr(d['limits']['per_month_inr'])} a month")
            if d.get("valid_to"):
                sentence += f", until {fmt_date(d['valid_to'])}"
            if d["merchants"]["block"]:
                sentence += ". Blocked: " + ", ".join(
                    f"{m} ({d['merchants']['block_reason'].get(m, 'no reason given')})" for m in d["merchants"]["block"])
            hit = pause_hit(d, set(d["scope"]["categories"]), now())
            pauses = [p for p in d.get("paused", []) if p.get("to") is None or p["to"] >= now().astimezone(IST).date().isoformat()]
            rules.append({"mandate_id": d["mandate_id"], "label": d["label"], "owner": d["owner"],
                          "version": d["version"], "sentence": sentence + ".", "set_by": x["meta"]["set_by"],
                          "set_at": x["meta"]["recorded_from"], "utterance": d.get("utterance"),
                          "signature": d.get("signature"), "limit": x["limit"], "used": x["used"], "left": x["left"],
                          "paused_now": bool(hit), "pauses": pauses, "auto_reorder": d.get("auto_reorder", False),
                          "doc": d})
        members = [{"member_id": m["member_id"], "name": m["name"], "role": m["role"], "active": m["active"]}
                   for m in households.members(conn, household_id)]
    return {"household_id": household_id, "name": hh["name"], "frozen": hh["frozen"], "rules": rules,
            "members": members}


def explain(household_id: str, purchase_id: str | None, hide_amounts: bool = False) -> dict:
    """'Why did you buy that?' — answered with the rule, who set it, when, and the proof."""
    with tx() as conn:
        if purchase_id:
            p = purchases.get(conn, purchase_id)
        else:
            p = conn.execute("SELECT * FROM purchases WHERE household_id = %s AND source <> 'import' "
                             "AND state NOT IN ('REQUESTED') ORDER BY updated_at DESC LIMIT 1",
                             (household_id,)).fetchone()
        if not p:
            return {"speech": "I haven't bought anything yet."}
        events = conn.execute("SELECT seq, type, hash, recorded_at, payload FROM events WHERE household_id = %s "
                              "AND payload->>'purchase_id' = %s ORDER BY seq", (household_id, p["purchase_id"])).fetchall()
        row = None
        if p["mandate_id"]:
            row = conn.execute("SELECT * FROM mandate_versions WHERE mandate_id = %s AND version = %s "
                               "ORDER BY row_id LIMIT 1", (p["mandate_id"], p["mandate_version"])).fetchone()
    d = p["decision"] or {}
    when = p["created_at"].astimezone(IST)
    what = purchases.describe(p)
    money = "" if hide_amounts else f" for {inr((p['final_total'] or p['cart_total']) // 100)}"
    if p["state"] in ("CONFIRMED", "CAPTURED", "PARTIALLY_REFUNDED", "REFUNDED", "RESERVED", "ORDER_CREATED"):
        speech = f"I bought {what}{money} on {when:%b} {when.day} because "
    else:
        speech = f"I didn't buy {what} ({p['state'].lower().replace('_', ' ')}): "
    if row:
        doc = row["doc"]
        set_at = row["recorded_from"].astimezone(IST)
        speech += (f"{doc['owner'].capitalize()}'s {doc['label']} rule (version {doc['version']}), set by "
                   f"{row['set_by'].capitalize()} on {set_at:%b} {set_at.day} at {set_at:%I:%M %p}"
                   + (f" — “{doc['utterance']}”" if doc.get("utterance") else "") + ". ")
    speech += d.get("reason", "")
    proof = [{"seq": e["seq"], "type": e["type"], "hash": e["hash"]} for e in events]
    return {"speech": speech.strip(), "purchase_id": p["purchase_id"], "state": p["state"], "decision": d,
            "proof": proof, "mandate": row["doc"] if row else None}


def _period(period: str) -> tuple[datetime, datetime, str]:
    key = month_key(now()) if period == "this_month" else previous_month_key(now())
    s, e = month_bounds(key)
    return s, e, key


def whatif(household_id: str, hint: str | None, limit_inr: int, period: str = "last_month") -> dict:
    """Counterfactual replay of a month of purchases under a proposed monthly limit."""
    start, end, key = _period(period)
    with tx() as conn:
        docs = mandates.current(conn, household_id)
        target = mandates.find(docs, hint)
        cats = set(target["scope"]["categories"]) if target else set(mandates.GROUPS.get(hint or "groceries", []))
        rows = conn.execute(
            "SELECT * FROM purchases WHERE household_id = %s AND state IN ('CONFIRMED','CAPTURED','PARTIALLY_REFUNDED') "
            "AND created_at >= %s AND created_at < %s ORDER BY created_at", (household_id, start, end)).fetchall()
    ps = [{"purchase_id": r["purchase_id"], "at": r["created_at"].astimezone(IST),
           "total": (r["final_total"] or r["cart_total"]) - r["refunded"],
           "categories": sorted({i["category"] for i in r["cart"]["items"]}), "summary": purchases.describe(r)}
          for r in rows]
    res = counterfactual(ps, cats, limit_inr)
    label = target["label"] if target else (hint or "grocery")
    month = start.strftime("%B")
    res["speech"] = f"In {month}: " + res["speech"].replace("budget", f"{label} budget", 1)
    res["would_escalate"] = [{"purchase_id": f["purchase_id"], "at": f["at"].isoformat(), "total": f["total"],
                              "summary": f["summary"]} for f in res["would_escalate"]]
    res.update({"period": key, "limit_inr": limit_inr, "mandate": target and target["mandate_id"]})
    return res


def suggest_limit(hist: list[dict], cats: set[str]) -> int:
    cutoff = now() - timedelta(days=60)
    spent = sum((h["final_total"] or h["cart_total"]) for h in hist
                if h["created_at"] >= cutoff and {i["category"] for i in h["cart"]["items"]} <= cats)
    monthly = spent / 2 / 100 * 1.2
    return max(500, int(round(monthly / 500.0)) * 500) if monthly else 2000


def rhythm(household_id: str) -> list[dict]:
    with tx() as conn:
        orders = purchases.item_orders(conn, household_id)
        n = len([m for m in households.members(conn, household_id) if m["active"] and m["role"] != "guest"])
        away = {m["member_id"] for m in households.members(conn, household_id)
                if m["away_until"] and m["away_until"] >= now().date()}
        fb = conn.execute("SELECT payload, valid_at FROM events WHERE household_id = %s AND type = 'rhythm.feedback' "
                          "ORDER BY seq", (household_id,)).fetchall()
        docs = mandates.current(conn, household_id)
        catalog = purchases.catalog().products(household_id)
    out = []
    t = now()
    for sku, os_ in orders.items():
        cat = os_[-1]["category"]
        feedback = [{"at": datetime.fromisoformat(f["valid_at"].replace("Z", "+00:00")), "kind": f["payload"]["kind"]}
                    for f in fb if cat in f["payload"]["categories"]]
        paused = any(pause_hit(d, {cat}, t) for d in docs if cat in d["scope"]["categories"])
        away_factor = 0.75 if (cat == "diabetic_biscuits" and "dadi" in away) else 1.0
        f = forecast(os_, cat, n, t, feedback, paused, away_factor)
        if f["run_out"] is None:
            continue
        price = catalog.get(sku, {}).get("price", 0)
        out.append({"sku": sku, "name": catalog.get(sku, {}).get("title", sku), "category": cat,
                    "run_out": f["run_out"].isoformat(), "days_left": round(f["days_left"], 1),
                    "usual_qty": f["usual_qty"], "est_cost": price * f["usual_qty"],
                    "days_per_unit": round(f["rate"]["days_per_unit"], 2), "source": f["rate"]["source"],
                    "paused": paused, "note": f["note"],
                    "running_low": (not paused) and f["days_left"] <= config.RHYTHM_REMIND_DAYS})
    return sorted(out, key=lambda x: x["days_left"])


def feedback(household_id: str, who: str | None, categories: list[str], kind: str) -> dict:
    with tx() as conn:
        lock_household(conn, household_id)
        append_event(conn, household_id, "rhythm.feedback", {"categories": categories, "kind": kind, "member": who},
                     who or "unknown")
    names = ", ".join(c.replace("_", " ") for c in categories)
    if kind == "out":
        r = [x for x in rhythm(household_id) if x["category"] in categories]
        tail = ""
        if r:
            x = r[0]
            tail = f" Should I order {x['usual_qty']}× {x['name']} for about {inr((x['est_cost'] + 2500) // 100)}?"
        return {"speech": f"Got it, you're out of {names}. I'll learn from that.{tail}"}
    return {"speech": f"Thanks — I'll expect the {names} to last longer."}


def ledger(household_id: str, limit: int = 500, after: int = 0) -> list[dict]:
    with tx() as conn:
        return conn.execute("SELECT * FROM events WHERE household_id = %s AND seq > %s ORDER BY seq LIMIT %s",
                            (household_id, after, limit)).fetchall()


def verify_ledger(household_id: str) -> dict:
    with tx() as conn:
        events = conn.execute("SELECT * FROM events WHERE household_id = %s ORDER BY seq", (household_id,)).fetchall()
        head = conn.execute("SELECT head_hash FROM households WHERE id = %s", (household_id,)).fetchone()["head_hash"]
    res = verify_chain([dict(e) for e in events])
    res["head_matches"] = res["ok"] and res["head"] == head
    res["ok"] = res["ok"] and res["head_matches"]
    return res


def remind_running_low():
    """Proactive reminders (and auto-reorders where a mandate allows it), outside quiet hours."""
    hour = now().astimezone(IST).hour
    qs, qe = config.QUIET_HOURS
    if hour >= qs or hour < qe:
        return
    with tx() as conn:
        hhs = [r["id"] for r in conn.execute("SELECT id FROM households WHERE frozen IS NULL").fetchall()]
    r = purchases.redis()
    for hh in hhs:
        for x in rhythm(hh):
            if not x["running_low"]:
                continue
            key = f"remind:{hh}:{x['sku']}:{x['run_out'][:10]}"
            if not r.set(key, "1", nx=True, ex=86400 * 3):
                continue
            with tx() as conn:
                docs = mandates.current(conn, hh)
                auto = [d for d in docs if x["category"] in d["scope"]["categories"] and d.get("auto_reorder")]
                lock_household(conn, hh)
                append_event(conn, hh, "rhythm.reminder", {"sku": x["sku"], "run_out": x["run_out"],
                                                           "auto": bool(auto)}, "rhythm")
                if not auto:
                    notify(conn, hh, "speaker", "speak",
                           f"{x['name']} usually runs out around {fmt_date(x['run_out'])}. Reorder {x['usual_qty']} "
                           f"for about {inr((x['est_cost'] + 2500) // 100)}? It fits the {x['category'].replace('_', ' ')} rule.",
                           {"sku": x["sku"], "rhythm": True})
            if auto:
                from core.saga.idempotency import new_ulid
                purchases.request(hh, new_ulid(), auto[0]["owner"], None, "rhythm", None,
                                  purchases.catalog().products(hh)[x["sku"]]["merchant"],
                                  [{"sku": x["sku"], "qty": x["usual_qty"]}], notes=["auto-reorder from rhythm"])
