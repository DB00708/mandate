"""Owner approvals: the only path by which spending power grows.

A voice request can *propose* a loosening; only an ES256 signature from an active owner's
registered device can approve it. Approved loosenings of an existing rule take effect after
a cooling-off window during which any owner can cancel."""
import base64
import hashlib
import os
from datetime import timedelta

from psycopg.types.json import Jsonb

from apps.api.services import households, mandates, purchases
from apps.common import config
from apps.common.db import append_event, lock_household, notify, now, redis, tx
from core.ledger.hashchain import canonical_json
from core.mandates import signing
from core.mandates.diff import inr
from core.saga import states as S
from core.saga.idempotency import new_ulid


class ApprovalError(Exception):
    pass


def diff_hash(proposal: dict) -> str:
    return hashlib.sha256(canonical_json(proposal).encode()).hexdigest()


def statement(a: dict, decision: str) -> dict:
    """Exactly what the owner's device signs."""
    return {"approval_id": a["approval_id"], "challenge": a["challenge"], "decision": decision,
            "diff_hash": diff_hash(a["proposal"]), "household_id": a["household_id"]}


def _new(conn, household_id, kind, subject_id, proposal, diff, diff_text, requested_by, via, context, ttl) -> dict:
    a = conn.execute(
        "INSERT INTO approvals (approval_id, household_id, kind, subject_id, proposal, diff, diff_text, requested_by, "
        "requested_via, context, challenge, status, expires_at) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'PENDING',%s) "
        "RETURNING *",
        ("apr_" + new_ulid().lower(), household_id, kind, subject_id, Jsonb(proposal), Jsonb(diff), diff_text,
         requested_by, via, Jsonb(context), base64.urlsafe_b64encode(os.urandom(24)).decode().rstrip("="),
         now() + timedelta(seconds=ttl))).fetchone()
    append_event(conn, household_id, "approval.requested",
                 {"approval_id": a["approval_id"], "kind": kind, "subject_id": subject_id, "diff": diff,
                  "requested_by": requested_by, "via": via, "context": context}, requested_by or "unknown")
    notify(conn, household_id, "owners", "approval", diff_text, {"approval_id": a["approval_id"], "kind": kind})
    return a


def create_rule_change(conn, household_id: str, new_doc: dict, old_doc: dict | None, diff: dict,
                       requested_by: str | None, via: str, context: dict) -> dict:
    return _new(conn, household_id, "rule_change", new_doc["mandate_id"], {"mandate": new_doc, "is_new": old_doc is None},
                diff, diff["text"], requested_by, via, context, config.RULE_CHANGE_TTL_SECONDS)


def create_unfreeze(conn, household_id: str, requested_by: str | None, via: str, context: dict) -> dict:
    d = {"direction": "loosen", "changes": [{"field": "frozen", "to": None, "text": "unfreeze all spending"}],
         "text": "Unfreeze all household spending"}
    return _new(conn, household_id, "unfreeze", household_id, {"unfreeze": True}, d, d["text"], requested_by, via,
                context, config.RULE_CHANGE_TTL_SECONDS)


def create_escalation(conn, p: dict, decision) -> dict:
    total = p["final_total"] if p["final_total"] is not None else p["cart_total"]
    who = (p["requested_by"] or "someone").capitalize()
    text = f"{who} wants {purchases.describe(p)} from {p['merchant']} for {inr(total // 100)}. {decision.reason}"
    d = {"direction": "loosen", "changes": [{"field": "purchase", "text": text}], "text": text}
    return _new(conn, p["household_id"], "escalation", p["purchase_id"],
                {"purchase_id": p["purchase_id"], "total": total, "code": decision.code}, d, text, p["requested_by"],
                p["source"], {"code": decision.code}, config.ESCALATION_TTL_SECONDS)


def pending(conn, household_id: str) -> list[dict]:
    return conn.execute("SELECT * FROM approvals WHERE household_id = %s AND status IN ('PENDING','APPROVED') "
                        "ORDER BY created_at", (household_id,)).fetchall()


def decide(approval_id: str, device_id: str, decision: str, signature: str, report: bool = False) -> dict:
    if decision not in ("approve", "reject", "cancel"):
        raise ApprovalError("decision must be approve, reject or cancel")
    with tx() as conn:
        a = conn.execute("SELECT * FROM approvals WHERE approval_id = %s", (approval_id,)).fetchone()
        if not a:
            raise ApprovalError("unknown approval")
        hh = a["household_id"]
        lock_household(conn, hh)
        a = conn.execute("SELECT * FROM approvals WHERE approval_id = %s", (approval_id,)).fetchone()
        dev = conn.execute("SELECT * FROM devices WHERE device_id = %s AND household_id = %s", (device_id, hh)).fetchone()
        owner = households.member(conn, hh, dev["member_id"]) if dev else None
        why = None
        stmt = statement(a, decision)
        if not dev or not dev["active"] or not owner or owner["role"] != "owner" or not owner["active"]:
            why = "device is not an active owner device"
        elif not signing.verify(dev["public_key"], stmt, signature):
            why = "signature does not verify"
        if why:
            # Recorded (committed) before refusing, so every forged attempt stays in the audit log.
            append_event(conn, hh, "approval.signature_rejected",
                         {"approval_id": approval_id, "device_id": device_id, "why": why}, "security")
    if why:
        raise ApprovalError(why)
    with tx() as conn:
        lock_household(conn, hh)
        a = conn.execute("SELECT * FROM approvals WHERE approval_id = %s", (approval_id,)).fetchone()
        assertion = {"alg": "ES256", "by": device_id, "member": owner["member_id"], "statement": stmt, "sig": signature}

        if decision == "cancel":
            if a["status"] != "APPROVED":
                raise ApprovalError("only an approved change in its cooling-off window can be cancelled")
            conn.execute("UPDATE approvals SET status = 'CANCELLED', decided_at = now() WHERE approval_id = %s",
                         (approval_id,))
            append_event(conn, hh, "approval.cancelled", {"approval_id": approval_id, "assertion": assertion},
                         owner["member_id"])
            notify(conn, hh, "owners", "approval", f"{owner['member_id'].capitalize()} cancelled: {a['diff_text']}")
            return {"status": "CANCELLED"}

        if a["status"] != "PENDING":
            raise ApprovalError(f"approval is {a['status']}")
        if a["expires_at"] < now():
            raise ApprovalError("approval has expired")

        if decision == "reject":
            conn.execute("UPDATE approvals SET status = 'REJECTED', decided_by = %s, device_id = %s, assertion = %s, "
                         "reported = %s, decided_at = now() WHERE approval_id = %s",
                         (owner["member_id"], device_id, Jsonb(assertion), report, approval_id))
            append_event(conn, hh, "approval.rejected", {"approval_id": approval_id, "reported": report,
                                                         "assertion": assertion}, owner["member_id"])
            out = {"status": "REJECTED"}
            if a["kind"] == "escalation":
                p = purchases.get(conn, a["subject_id"])
                if p and p["state"] == S.ESCALATED:
                    purchases.set_state(conn, p, S.DENIED, owner["member_id"], {"code": "OWNER_REJECTED"})
                    purchases.speak(conn, hh, f"{owner['member_id'].capitalize()} said no to {purchases.describe(p)}.")
            if report and a["requested_via"] in ("voice", "mcp"):
                out["lockout"] = _record_reported_attempt(conn, hh)
            return out

        # approve
        conn.execute("UPDATE approvals SET status = 'APPROVED', decided_by = %s, device_id = %s, assertion = %s, "
                     "decided_at = now() WHERE approval_id = %s",
                     (owner["member_id"], device_id, Jsonb(assertion), approval_id))
        append_event(conn, hh, "approval.approved", {"approval_id": approval_id, "assertion": assertion},
                     owner["member_id"])
        a = conn.execute("SELECT * FROM approvals WHERE approval_id = %s", (approval_id,)).fetchone()
        if a["kind"] == "escalation":
            p = purchases.get(conn, a["subject_id"])
            if not p or p["state"] != S.ESCALATED:
                raise ApprovalError("purchase is no longer waiting for approval")
            conn.execute("UPDATE purchases SET approved_by = %s WHERE purchase_id = %s",
                         (owner["member_id"], p["purchase_id"]))
            conn.execute("UPDATE approvals SET status = 'APPLIED' WHERE approval_id = %s", (approval_id,))
            out = purchases.decide(conn, purchases.get(conn, p["purchase_id"]), owner["member_id"], owner_approved=True)
            return {"status": "APPLIED", "purchase": out["purchase"]["state"]}
        if a["kind"] == "unfreeze":
            conn.execute("UPDATE households SET frozen = NULL WHERE id = %s", (hh,))
            conn.execute("UPDATE approvals SET status = 'APPLIED' WHERE approval_id = %s", (approval_id,))
            append_event(conn, hh, "household.unfrozen", {"approval_id": approval_id}, owner["member_id"])
            notify(conn, hh, "owners", "security", f"Spending unfrozen by {owner['member_id'].capitalize()}.")
            purchases.speak(conn, hh, "Spending is unfrozen.")
            return {"status": "APPLIED"}
        # rule change
        cooling = not a["proposal"].get("is_new") and config.COOLING_OFF_SECONDS > 0
        if cooling:
            eff = now() + timedelta(seconds=config.COOLING_OFF_SECONDS)
            conn.execute("UPDATE approvals SET effective_at = %s WHERE approval_id = %s", (eff, approval_id))
            for o in households.owners(conn, hh):
                notify(conn, hh, o, "cooling_off",
                       f"{owner['member_id'].capitalize()} approved: {a['diff_text']}. It takes effect at "
                       f"{eff.strftime('%H:%M:%S')} UTC unless an owner cancels.",
                       {"approval_id": approval_id, "effective_at": eff.isoformat()})
            return {"status": "APPROVED", "effective_at": eff.isoformat()}
        apply_rule_change(conn, a, assertion)
        return {"status": "APPLIED"}


def apply_rule_change(conn, a: dict, assertion: dict | None = None):
    """Write the approved mandate version (signed by the approving device)."""
    hh = a["household_id"]
    doc = dict(a["proposal"]["mandate"])
    assertion = assertion or a["assertion"]
    current = mandates.get(conn, hh, doc["mandate_id"])
    if current and current["version"] >= doc["version"]:
        # The rule changed since this was proposed: re-base the signed diff onto the latest version.
        doc["version"] = current["version"] + 1
        doc["supersedes"] = f"{doc['mandate_id']}@v{current['version']}"
    doc["signature"] = {"alg": "ES256", "by": assertion["by"], "member": assertion["member"],
                        "sig": assertion["sig"], "approval_id": a["approval_id"]}
    res = mandates.write_version(conn, hh, doc, assertion["member"], now(),
                                 "mandate.created" if a["proposal"].get("is_new") else "mandate.loosened",
                                 {"approval_id": a["approval_id"]})
    conn.execute("UPDATE approvals SET status = 'APPLIED' WHERE approval_id = %s", (a["approval_id"],))
    notify(conn, hh, "owners", "rule", f"Now in effect: {a['diff_text']}", {"mandate_id": doc["mandate_id"]})
    purchases.speak(conn, hh, f"Done. {a['diff_text']}.")
    return res


def _record_reported_attempt(conn, household_id: str) -> dict:
    r = redis()
    key = f"t3fail:{household_id}"
    n = r.incr(key)
    r.expire(key, config.TIER3_LOCK_SECONDS)
    append_event(conn, household_id, "security.voice_change_reported", {"count": int(n)}, "security")
    if n >= config.TIER3_LOCK_AFTER:
        r.set(f"t3lock:{household_id}", "1", ex=config.TIER3_LOCK_SECONDS)
        append_event(conn, household_id, "security.tier3_voice_locked",
                     {"seconds": config.TIER3_LOCK_SECONDS, "after": int(n)}, "security")
        notify(conn, household_id, "owners", "security",
               f"{n} rejected voice attempts to loosen rules. Voice rule changes are locked for 24 hours.")
        return {"locked": True, "count": int(n)}
    notify(conn, household_id, "owners", "security", f"Reported a suspicious voice request ({n} so far).")
    return {"locked": False, "count": int(n)}


def tier3_locked(household_id: str) -> bool:
    return bool(redis().get(f"t3lock:{household_id}"))
