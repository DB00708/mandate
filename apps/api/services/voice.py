"""Voice (and MCP) entry point: speech -> proposal -> tier -> action.

Voice is a claim, not a credential. Speaker ID resolves who *probably* spoke; that is
enough to read, to spend inside a mandate, and to tighten. Loosening is always turned into
an approval card on an owner's phone."""
import copy
import hashlib

from psycopg.types.json import Jsonb

from apps.api.services import approvals, households, mandates, purchases, queries
from apps.common import config
from apps.common.db import append_event, lock_household, notify, now, tx
from core.budget.reservations import IST
from core.mandates.diff import diff, inr
from core.policy import tiers
from core.saga.idempotency import new_ulid
from intelligence.parser.parser import FaultyProvider, ParseFailed, parse
from intelligence.planner.planner import plan
from intelligence.providers.base import get_provider


def _say(speech: str, **extra) -> dict:
    return {"speech": speech, **extra}


def resolve_speaker(conn, household_id: str, claim: str | None, score: int) -> dict | None:
    m = households.member(conn, household_id, claim)
    if m and m["active"] and score >= config.SPEAKER_MATCH_THRESHOLD:
        return m
    return None


def handle(household_id: str, text: str, speaker: str | None, speaker_score: int = 0, audio_sha256: str | None = None,
           purchase_id: str | None = None, via: str = "voice", room: str = "living room",
           llm_fault: int = 0) -> dict:
    today = now().astimezone(IST).date()
    with tx() as conn:
        lock_household(conn, household_id)
        member = resolve_speaker(conn, household_id, speaker, speaker_score)
        names = [m["member_id"] for m in households.members(conn, household_id) if m["active"]]
        # Privacy: only a hash of the audio and the transcript are retained.
        append_event(conn, household_id, "voice.utterance",
                     {"speaker_claim": speaker, "speaker_score": int(speaker_score), "resolved": member and member["member_id"],
                      "transcript": text, "audio_sha256": audio_sha256 or hashlib.sha256(text.encode()).hexdigest(),
                      "via": via, "room": room}, member["member_id"] if member else "unknown")

    provider = get_provider()
    if llm_fault and config.TEST_MODE:
        provider = FaultyProvider(provider, llm_fault)
    try:
        proposal, attempts = parse(text, today, names, provider)
    except ParseFailed as e:
        with tx() as conn:
            lock_household(conn, household_id)
            append_event(conn, household_id, "parser.failed_closed", {"error": str(e)[:200]}, "parser")
        return _say("Sorry, I didn't catch that. Could you say it again?", intent="unknown", tier=0, failed_closed=True)
    p = proposal.model_dump()
    who = member["member_id"] if member else None
    role = member["role"] if member else "guest"
    ctx = {"speaker_claim": speaker, "speaker_score": int(speaker_score), "room": room, "via": via,
           "at": now().isoformat(), "utterance": text}
    intent = p["intent"]
    out_base = {"intent": intent, "proposal": p, "speaker": who, "parse_attempts": attempts}

    # ---- tier 0: reads (anyone, minors see no amounts outside their own rules)
    if intent == "query_budget":
        return {**out_base, "tier": 0, **queries.budget_speech(household_id, who, role)}
    if intent == "explain":
        return {**out_base, "tier": 0, **queries.explain(household_id, None, hide_amounts=role == "minor")}
    if intent == "whatif":
        if not p["amount_inr"]:
            return {**out_base, "tier": 0, "speech": "What budget should I try? For example, 'what if the grocery "
                                                     "budget had been ₹3,000 last month'."}
        return {**out_base, "tier": 0, **queries.whatif(household_id, p["mandate_hint"], p["amount_inr"], p["period"])}
    if intent == "feedback":
        return {**out_base, "tier": 0, **queries.feedback(household_id, who, p["categories"], p["feedback_kind"])}
    if intent == "unknown":
        return {**out_base, "tier": 0, "speech": "Sorry, I can help with shopping, budgets and household rules."}

    # ---- confirmations
    if intent == "confirm":
        r = purchases.confirm(household_id, who, purchase_id)
        if not r["ok"]:
            return {**out_base, "tier": 1, "speech": r["speech"]}
        return {**out_base, "tier": 1, "speech": _last_speech(household_id), "purchase": _p(r["purchase"]),
                "decision": r["decision"]}
    if intent == "cancel":
        r = purchases.cancel(household_id, who, purchase_id)
        return {**out_base, "tier": 2, "speech": r["speech"], "purchase": _p(r.get("purchase"))}

    # ---- tier 1: spend
    if intent == "purchase":
        return {**out_base, "tier": 1, **_purchase(household_id, p, who, speaker, text, purchase_id, via)}

    # ---- everything below changes rules: needs a recognised adult at least
    if member is None or role in ("guest",):
        return {**out_base, "tier": tiers.classify(intent), "speech": "I can only change household rules for family "
                                                                     "members I recognise."}

    if intent == "freeze":
        return {**out_base, "tier": 2, **_freeze(household_id, member)}
    if intent == "unfreeze":
        return {**out_base, "tier": 3, **_loosen_unfreeze(household_id, member, via, ctx)}
    if intent == "remove_member":
        if role != "owner":
            return {**out_base, "tier": 2, "speech": "Only an owner can remove someone from the household."}
        with tx() as conn:
            lock_household(conn, household_id)
            res = households.remove_member(conn, household_id, p["target_member"], who)
        return {**out_base, "tier": 2, "speech": f"Removed {p['target_member'].capitalize()}. "
                                                 f"{res['closed_approvals'] + res['closed_purchases']} open requests closed."}

    if role == "minor":
        return {**out_base, "tier": tiers.classify(intent), "speech": "I'll need a parent for that. I've let them know.",
                **_notify_parents(household_id, who, text)}

    return {**out_base, **_rule_change(household_id, p, member, via, ctx)}


def _p(row):
    if not row:
        return None
    return {k: row[k] for k in ("purchase_id", "state", "merchant", "cart_total", "final_total", "requested_by",
                                "mandate_id")} | {"items": row["cart"]["items"], "decision": row["decision"]}


def _last_speech(household_id):
    with tx() as conn:
        r = conn.execute("SELECT text FROM notifications WHERE household_id = %s AND audience = 'speaker' "
                         "ORDER BY id DESC LIMIT 1", (household_id,)).fetchone()
    return r["text"] if r else "Okay."


def _notify_parents(household_id, who, text):
    with tx() as conn:
        lock_household(conn, household_id)
        append_event(conn, household_id, "request.minor_rule_change", {"member": who, "utterance": text}, who)
        notify(conn, household_id, "owners", "request", f"{who.capitalize()} asked: “{text}”")
    return {}


def _purchase(household_id, p, who, speaker, text, purchase_id, via):
    purchase_id = purchase_id or new_ulid()
    cat = purchases.catalog()
    with tx() as conn:
        orders = purchases.item_orders(conn, household_id)
        usual = {sku: round(sum(o["qty"] for o in os_[-3:]) / len(os_[-3:])) for sku, os_ in orders.items()}
        hist = purchases.history(conn, household_id)
    if p["usual"]:
        mine = [h for h in hist if h["requested_by"] == who] or hist
        if not mine:
            return {"speech": "I don't have a usual order for you yet. What would you like?"}
        last = mine[-1]
        lines = [{"sku": i["sku"], "qty": i["qty"]} for i in last["cart"]["items"]]
        priced = cat.price_cart(household_id, lines)
        listed = ", ".join(f"{i['name']} ×{i['qty']}" for i in priced["items"])
        ask = f"Your usual: {listed}, {inr(priced['total'] // 100)}?"
        r = purchases.request(household_id, purchase_id, who, speaker, via, text, last["merchant"], lines,
                              force_confirm=ask)
        return {"speech": ask, "purchase": _p(r["purchase"]), "decision": r["decision"]}
    # Quarantined planner: reads untrusted listings, proposes (sku, qty) only.
    cart = plan([{"category": i["category"], "qty": i["qty"], "product_hint": i["product_hint"]} for i in p["items"]],
                cat.listings(household_id), usual, p.get("merchant"))
    if cart is None:
        return {"speech": "I couldn't find that in the shop."}
    r = purchases.request(household_id, purchase_id, who, speaker, via, text, cart.merchant,
                          [ln.model_dump() for ln in cart.lines], notes=cart.notes)
    if r.get("duplicate"):
        return {"speech": "I already have that request.", "purchase": _p(r["purchase"]), "duplicate": True}
    return {"speech": _last_speech(household_id), "purchase": _p(r["purchase"]), "decision": r["decision"],
            "planner_notes": cart.notes}


def _freeze(household_id, member):
    with tx() as conn:
        lock_household(conn, household_id)
        frozen = {"by": member["member_id"], "at": now().isoformat()}
        conn.execute("UPDATE households SET frozen = %s WHERE id = %s", (Jsonb(frozen), household_id))
        append_event(conn, household_id, "household.frozen", frozen, member["member_id"])
        # Stop anything not yet paid for.
        rows = conn.execute("SELECT * FROM purchases WHERE household_id = %s AND state IN "
                            "('RESERVED','AWAITING_CONFIRMATION','ESCALATED')", (household_id,)).fetchall()
        for p in rows:
            conn.execute("UPDATE reservations SET status = 'RELEASED' WHERE purchase_id = %s AND status = 'ACTIVE'",
                         (p["purchase_id"],))
            conn.execute("UPDATE outbox SET status = 'CANCELLED' WHERE purchase_id = %s AND status = 'PENDING'",
                         (p["purchase_id"],))
            purchases.set_state(conn, p, "DENIED", member["member_id"], {"code": "FROZEN"})
        notify(conn, household_id, "owners", "security",
               f"{member['member_id'].capitalize()} froze all spending. Unfreezing needs an owner's passkey.")
    return {"speech": "All spending is frozen. An owner can unfreeze it from their phone.", "stopped": len(rows)}


def _loosen_unfreeze(household_id, member, via, ctx):
    if approvals.tier3_locked(household_id):
        return _locked(household_id, member)
    with tx() as conn:
        lock_household(conn, household_id)
        a = approvals.create_unfreeze(conn, household_id, member["member_id"], via, ctx)
    return {"speech": "I've sent the unfreeze to the owners' phones to confirm.", "approval_id": a["approval_id"]}


def _locked(household_id, member):
    with tx() as conn:
        lock_household(conn, household_id)
        append_event(conn, household_id, "security.tier3_voice_blocked", {"member": member["member_id"]}, "security")
    return {"speech": "Rule changes by voice are locked for 24 hours after repeated rejected attempts. Please use the "
                      "app.", "locked": True}


def _new_mandate_doc(household_id, p, member, docs, text):
    cats = p["categories"]
    label = p["mandate_hint"] or ("groceries" if set(cats) & mandates.GROUPS["groceries"] else cats[0])
    members_allowed = sorted(set(p["members"]) | {member["member_id"]})
    monthly = p["amount_inr"]
    per_order = p["per_order_inr"] or monthly
    return {
        "mandate_id": "mdt_" + new_ulid().lower(), "household_id": household_id, "label": label,
        "owner": member["member_id"], "scope": {"categories": cats},
        "limits": {"per_month_inr": monthly, "per_order_inr": min(per_order, monthly)},
        "merchants": {"block": [], "block_reason": {}}, "members_allowed": members_allowed,
        "requires_approval_from": [], "valid_from": now().isoformat(),
        "valid_to": (p["until"] + "T23:59:59+05:30") if p["until"] else None, "paused": [],
        "auto_reorder": bool(p.get("auto_reorder")), "utterance": text,
        "utterance_audio_hash": hashlib.sha256(text.encode()).hexdigest(), "version": 1, "supersedes": None,
        "signature": None,
    }


def _bump(doc):
    new = copy.deepcopy(doc)
    new["version"] = doc["version"] + 1
    new["supersedes"] = f"{doc['mandate_id']}@v{doc['version']}"
    new["signature"] = None
    return new


def _rule_change(household_id, p, member, via, ctx) -> dict:
    intent, who = p["intent"], member["member_id"]
    today = now().astimezone(IST).date()
    with tx() as conn:
        docs = mandates.current(conn, household_id)
        hist = purchases.history(conn, household_id)

    if intent == "create_mandate":
        if not p["categories"]:
            return {"tier": 3, "speech": "Which items should the rule cover?"}
        if not p["amount_inr"]:
            suggestion = queries.suggest_limit(hist, set(p["categories"]))
            return {"tier": 3, "speech": f"I need a number before I can make a rule. Based on your history, how about "
                                         f"{inr(suggestion)} a month?", "suggested_limit_inr": suggestion,
                    "refused": "VAGUE_LIMIT"}
        new = _new_mandate_doc(household_id, p, member, docs, ctx["utterance"])
        old = None
    else:
        target = mandates.find(docs, p.get("mandate_hint"), p.get("categories"), None)
        if intent in ("block_merchant", "unblock_merchant"):
            targets = docs
        else:
            targets = [target] if target else []
        if not targets:
            return {"tier": tiers.classify(intent), "speech": "I couldn't find a rule that matches."}
        if intent in ("block_merchant", "unblock_merchant"):
            return _merchant_change(household_id, p, member, via, ctx, targets)
        old = target
        new = _bump(old)
        new["utterance"] = ctx["utterance"]
        new["utterance_audio_hash"] = hashlib.sha256(ctx["utterance"].encode()).hexdigest()
        if intent == "change_limit":
            if not p["amount_inr"]:
                return {"tier": 3, "speech": "What should the new limit be? I need a number."}
            key = "per_order_inr" if p["limit_kind"] == "per_order" else "per_month_inr"
            new["limits"][key] = p["amount_inr"]
            if key == "per_month_inr" and new["limits"]["per_order_inr"] > p["amount_inr"]:
                new["limits"]["per_order_inr"] = p["amount_inr"]
        elif intent == "add_member":
            new["members_allowed"] = sorted(set(new["members_allowed"]) | {p["target_member"]})
        elif intent == "pause":
            cats = [c for c in p["categories"] if c in old["scope"]["categories"]] or None
            start = p["start"] or today.isoformat()
            new["paused"] = new["paused"] + [{"from": start, "to": p["until"], "categories": cats, "set_by": who}]
        elif intent == "unpause":
            today_s = today.isoformat()
            cats = set(p["categories"])
            keep = []
            for x in new["paused"]:
                hit = not cats or not x.get("categories") or cats & set(x["categories"])
                if hit and (x.get("to") is None or x["to"] >= today_s):
                    continue
                keep.append(x)
            new["paused"] = keep

    d = diff(old, new)
    if d["text"] == "no change":
        return {"tier": 2, "speech": "That rule is already set that way."}
    tier = tiers.LOOSEN if d["direction"] == "loosen" else tiers.TIGHTEN
    if tier == tiers.TIGHTEN:
        with tx() as conn:
            lock_household(conn, household_id)
            new["signature"] = {"alg": "none", "by": f"voice:{who}", "note": "tightening needs no passkey"}
            res = mandates.write_version(conn, household_id, new, who, now(), "mandate.tightened",
                                         {"utterance": ctx["utterance"]})
            for o in households.owners(conn, household_id):
                if o != who:
                    notify(conn, household_id, o, "rule", f"{who.capitalize()} tightened a rule: {d['text']}.")
        return {"tier": 2, "speech": f"Done. {d['text'][0].upper() + d['text'][1:]}.", "mandate": res["doc"], "diff": d}

    if approvals.tier3_locked(household_id):
        return {"tier": 3, **_locked(household_id, member)}
    with tx() as conn:
        lock_household(conn, household_id)
        a = approvals.create_rule_change(conn, household_id, new, old, d, who, via, ctx)
        target_owner = (old or new)["owner"]
        owners = households.owners(conn, household_id)
        phone_owner = target_owner if target_owner in owners else (owners[0] if owners else None)
    return {"tier": 3, "speech": f"I've sent this change to {phone_owner.capitalize() if phone_owner else 'the owners'}'s "
                                 f"phone to confirm.", "approval_id": a["approval_id"], "diff": d}


def _merchant_change(household_id, p, member, via, ctx, docs):
    who, merchant = member["member_id"], p["merchant"]
    today = now().astimezone(IST).date()
    if p["intent"] == "block_merchant":
        reason = f"{p['reason']} (set {today.strftime('%b')} {today.day})" if p.get("reason") else None
        with tx() as conn:
            lock_household(conn, household_id)
            n = 0
            for old in docs:
                if merchant in old["merchants"]["block"]:
                    continue
                new = _bump(old)
                new["utterance"] = ctx["utterance"]
                new["merchants"]["block"] = sorted(set(new["merchants"]["block"]) | {merchant})
                if reason:
                    new["merchants"]["block_reason"][merchant] = reason
                new["signature"] = {"alg": "none", "by": f"voice:{who}", "note": "tightening needs no passkey"}
                mandates.write_version(conn, household_id, new, who, now(), "mandate.tightened",
                                       {"utterance": ctx["utterance"]})
                n += 1
        return {"tier": 2, "speech": f"Blocked {merchant}" + (f" because {p['reason']}" if p.get("reason") else "") + "."}
    if approvals.tier3_locked(household_id):
        return {"tier": 3, **_locked(household_id, member)}
    created = []
    with tx() as conn:
        lock_household(conn, household_id)
        for old in docs:
            if merchant not in old["merchants"]["block"]:
                continue
            new = _bump(old)
            new["utterance"] = ctx["utterance"]
            new["merchants"]["block"] = [m for m in new["merchants"]["block"] if m != merchant]
            new["merchants"]["block_reason"].pop(merchant, None)
            d = diff(old, new)
            created.append(approvals.create_rule_change(conn, household_id, new, old, d, who, via, ctx)["approval_id"])
    if not created:
        return {"tier": 3, "speech": f"{merchant} isn't blocked."}
    return {"tier": 3, "speech": "I've sent this change to the owners' phones to confirm.",
            "approval_id": created[0], "approval_ids": created}
