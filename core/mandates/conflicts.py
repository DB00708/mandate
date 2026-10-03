"""Conflicting rules: when two active mandates cover the same purchase, the
stricter one wins until owners resolve it."""


def overlapping(new: dict, others: list[dict]) -> list[dict]:
    cats = set(new["scope"]["categories"])
    return [m for m in others if m["mandate_id"] != new["mandate_id"] and cats & set(m["scope"]["categories"])]


def conflicts(new: dict, others: list[dict]) -> list[dict]:
    """Overlapping mandates with different limits or members."""
    return [m for m in overlapping(new, others)
            if m["limits"] != new["limits"] or set(m["members_allowed"]) != set(new["members_allowed"])]


def effective_limits(mandates: list[dict]) -> dict:
    return {
        "per_month_inr": min(m["limits"]["per_month_inr"] for m in mandates),
        "per_order_inr": min(m["limits"]["per_order_inr"] for m in mandates),
    }
