"""The policy engine: plain, deterministic code that alone decides whether money moves.

Outcomes:
  ALLOW     — inside a mandate; the saga may reserve and pay
  CONFIRM   — inside a mandate but needs a known adult to say yes (anomaly, unknown speaker, ambiguity)
  ESCALATE  — outside what any mandate permits; needs an owner's signed approval
  DENY      — a rule forbids it (frozen, paused, blocked merchant)
Fail closed: if an input is missing, the answer is ESCALATE, never ALLOW.
"""
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

from core.budget.reservations import IST
from core.mandates.conflicts import effective_limits
from core.mandates.diff import fmt_date, inr
from core.policy import anomaly

ALLOW, CONFIRM, ESCALATE, DENY = "ALLOW", "CONFIRM", "ESCALATE", "DENY"


@dataclass
class PurchaseContext:
    member: Optional[str]          # resolved speaker, None if unknown
    role: Optional[str]            # owner/adult/minor/guest
    items: list[dict]              # [{sku, name, category, qty, unit_price}] unit_price in paise
    merchant: Optional[str]
    total: Optional[int]           # paise, including fees
    now: datetime
    acknowledged: bool = False     # a known adult confirmed after a CONFIRM
    owner_approved: bool = False   # owner signed an escalation approval
    baselines: dict = field(default_factory=dict)       # sku -> typical qty
    known_merchants: set = field(default_factory=set)
    has_history: bool = False
    odd_hour_check: bool = True


@dataclass
class Decision:
    outcome: str
    code: str
    reason: str
    mandate_id: Optional[str] = None
    mandate_version: Optional[int] = None
    rule: Optional[dict] = None
    details: dict = field(default_factory=dict)

    def as_dict(self):
        return {"outcome": self.outcome, "code": self.code, "reason": self.reason, "mandate_id": self.mandate_id,
                "mandate_version": self.mandate_version, "rule": self.rule, "details": self.details}


def _rule_ref(m: dict, meta: dict | None) -> dict:
    meta = meta or {}
    return {"mandate_id": m["mandate_id"], "version": m["version"], "label": m["label"], "owner": m["owner"],
            "set_by": meta.get("set_by", m["owner"]), "set_at": meta.get("recorded_from", m["valid_from"])}


def _active(m: dict, now: datetime) -> bool:
    start = datetime.fromisoformat(m["valid_from"])
    end = datetime.fromisoformat(m["valid_to"]) if m.get("valid_to") else None
    return start <= now and (end is None or now < end)


def pause_hit(m: dict, categories: set[str], now: datetime) -> Optional[dict]:
    today = now.astimezone(IST).date().isoformat()
    for p in m.get("paused", []):
        if p["from"] <= today and (p.get("to") is None or today <= p["to"]):
            if not p.get("categories") or categories & set(p["categories"]):
                return p
    return None


def evaluate(ctx: PurchaseContext, mandates: list[dict], used_by_mandate: dict, frozen: Optional[dict],
             meta: dict | None = None) -> Decision:
    """mandates: current mandate docs for the household.
    used_by_mandate: mandate_id -> paise already held or spent this month (excluding this purchase).
    frozen: None or {"by": member, "at": iso}.  meta: mandate_id -> {set_by, recorded_from}."""
    meta = meta or {}
    if frozen:
        return Decision(DENY, "FROZEN", f"All spending is frozen (by {frozen['by'].capitalize()} on "
                                        f"{fmt_date(frozen['at'])}). Unfreezing needs an owner's passkey.")
    if not ctx.items or ctx.total is None or not ctx.merchant:
        return Decision(ESCALATE, "INCOMPLETE", "I could not price this order, so an owner needs to approve it.")
    if ctx.member is None and not ctx.owner_approved and not ctx.acknowledged:
        return Decision(CONFIRM, "UNKNOWN_SPEAKER",
                        "I don't recognise that voice. A family member needs to confirm this order.")

    cats = {it["category"] for it in ctx.items}
    active = [m for m in mandates if _active(m, ctx.now)]
    covering = [m for m in active if cats <= set(m["scope"]["categories"])]
    if not covering and ctx.owner_approved:
        return Decision(ALLOW, "OWNER_APPROVED_ONE_OFF", "An owner approved this one-off purchase with their passkey.")
    if not covering:
        names = ", ".join(sorted(cats))
        return Decision(ESCALATE, "OUT_OF_SCOPE", f"No rule covers {names}. I've asked the owners.",
                        details={"categories": sorted(cats)})

    allowed = [m for m in covering if ctx.member in m["members_allowed"]]
    if not allowed and not ctx.owner_approved:
        m = covering[0]
        return Decision(ESCALATE, "MEMBER_NOT_ALLOWED",
                        f"{(ctx.member or 'This speaker').capitalize()} isn't on {m['owner'].capitalize()}'s "
                        f"{m['label']} rule. I've asked the owners.", m["mandate_id"], m["version"],
                        _rule_ref(m, meta.get(m["mandate_id"])))
    binding = allowed or covering
    # Stricter wins: every covering mandate's blocks, pauses and limits apply.
    for m in covering:
        if ctx.merchant in m["merchants"]["block"]:
            why = m["merchants"].get("block_reason", {}).get(ctx.merchant)
            return Decision(DENY, "MERCHANT_BLOCKED",
                            f"{ctx.merchant} is blocked on the {m['label']} rule" + (f" because {why}" if why else "") + ".",
                            m["mandate_id"], m["version"], _rule_ref(m, meta.get(m["mandate_id"])))
        p = pause_hit(m, cats, ctx.now)
        if p:
            what = ", ".join(c.replace("_", " ") for c in p.get("categories") or []) or f"the {m['label']} rule"
            until = f" until {fmt_date(p['to'])}" if p.get("to") else " until an owner resumes it"
            return Decision(DENY, "PAUSED", f"{what[0].upper() + what[1:]} is paused from {fmt_date(p['from'])}{until}"
                            + (f" (paused by {p['set_by'].capitalize()})" if p.get("set_by") else "") + ".",
                            m["mandate_id"], m["version"], _rule_ref(m, meta.get(m["mandate_id"])),
                            details={"pause": p})

    primary = sorted(binding, key=lambda m: m["valid_from"])[0]
    limits = effective_limits(covering)
    ref = _rule_ref(primary, meta.get(primary["mandate_id"]))
    details = {"effective_limits": limits, "covering": [m["mandate_id"] for m in covering]}
    if len(covering) > 1:
        details["stricter_rule_applied"] = True

    per_order = limits["per_order_inr"] * 100
    if ctx.total > per_order and not ctx.owner_approved:
        return Decision(ESCALATE, "PER_ORDER_LIMIT",
                        f"{inr(ctx.total // 100)} is over the {inr(limits['per_order_inr'])} per-order limit. "
                        f"I've asked the owners.", primary["mandate_id"], primary["version"], ref, details)
    used = max(used_by_mandate.get(m["mandate_id"], 0) for m in covering)
    per_month = limits["per_month_inr"] * 100
    details.update({"used": used, "limit": per_month, "remaining_after": per_month - used - ctx.total})
    if used + ctx.total > per_month and not ctx.owner_approved:
        return Decision(ESCALATE, "MONTHLY_LIMIT",
                        f"That would exceed this month's {inr(limits['per_month_inr'])} {primary['label']} budget "
                        f"({inr((per_month - used) // 100)} left). I've asked the owners.",
                        primary["mandate_id"], primary["version"], ref, details)

    if not ctx.acknowledged and not ctx.owner_approved:
        found = anomaly.check(ctx.items, ctx.merchant, ctx.now, ctx.baselines, ctx.known_merchants, ctx.has_history,
                              ctx.odd_hour_check)
        if found:
            details["anomalies"] = found
            return Decision(CONFIRM, found[0]["code"], "; ".join(a["text"] for a in found).capitalize()
                            + ". Should I go ahead?", primary["mandate_id"], primary["version"], ref, details)

    return Decision(ALLOW, "WITHIN_MANDATE",
                    f"Allowed by {primary['owner'].capitalize()}'s {primary['label']} rule (v{primary['version']}).",
                    primary["mandate_id"], primary["version"], ref, details)
