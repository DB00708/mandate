"""Bitemporal mandate memory and counterfactual replay (pure functions).

Every mandate-version row carries two clocks:
  valid time    [valid_from, valid_to)       when the rule applied in the household
  recorded time [recorded_from, recorded_to) when Mandate learned / believed it
"what rule applied on Oct 12?"            -> as_of(valid=Oct 12, recorded=now)
"what did we believe on Oct 12?"          -> as_of(valid=Oct 12, recorded=Oct 12)
"""
from datetime import datetime

from core.mandates.diff import fmt_date, inr


def _within(t: datetime, start: datetime, end: datetime | None) -> bool:
    return start <= t and (end is None or t < end)


def as_of(rows: list[dict], valid: datetime, recorded: datetime) -> list[dict]:
    """rows: {mandate_id, version, doc, valid_from, valid_to, recorded_from, recorded_to} (datetimes)."""
    out = {}
    for r in rows:
        if _within(recorded, r["recorded_from"], r["recorded_to"]) and _within(valid, r["valid_from"], r["valid_to"]):
            cur = out.get(r["mandate_id"])
            if cur is None or r["version"] > cur["version"]:
                out[r["mandate_id"]] = r
    return list(out.values())


def counterfactual(purchases: list[dict], categories: set[str], monthly_limit_inr: int,
                   per_order_limit_inr: int | None = None) -> dict:
    """Replay purchases (chronological, {purchase_id, at, total, categories, summary}) under a proposed
    limit. Returns the orders that would have needed approval."""
    used, flagged, counted = 0, [], 0
    limit = monthly_limit_inr * 100
    for p in sorted(purchases, key=lambda p: p["at"]):
        if not set(p["categories"]) <= categories:
            continue
        counted += 1
        over_order = per_order_limit_inr is not None and p["total"] > per_order_limit_inr * 100
        if used + p["total"] > limit or over_order:
            flagged.append({**p, "used_before": used})
            continue  # an escalated order is not spent unless approved
        used += p["total"]
    if flagged:
        dates = ", ".join(fmt_date(f["at"].date().isoformat()) for f in flagged)
        speech = (f"With a {inr(monthly_limit_inr)} budget, {len(flagged)} of {counted} orders would have needed "
                  f"approval, on {dates}.")
    else:
        speech = f"With a {inr(monthly_limit_inr)} budget, all {counted} orders would still have gone through."
    return {"orders": counted, "would_escalate": flagged, "spent_within": used, "speech": speech}
