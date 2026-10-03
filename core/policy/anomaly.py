"""Anomaly checks for tier-1 spends: quantity spikes, new merchants, odd hours.
The per-item baseline is the same one the consumption-rhythm model uses."""
from datetime import datetime

from core.budget.reservations import IST

QUIET_START, QUIET_END = 0, 5  # 00:00–04:59 IST counts as an odd hour for spending
NO_HISTORY_MAX_QTY = 5


def quantity_spike(qty: int, baseline_qty: float | None) -> bool:
    if baseline_qty is None or baseline_qty <= 0:
        return qty > NO_HISTORY_MAX_QTY
    return qty > max(3 * baseline_qty, baseline_qty + 2)


def odd_hour(now: datetime) -> bool:
    return QUIET_START <= now.astimezone(IST).hour < QUIET_END


def check(items: list[dict], merchant: str, now: datetime, baselines: dict, known_merchants: set[str],
          has_history: bool, odd_hour_check: bool = True) -> list[dict]:
    """Return a list of anomalies; empty means nothing unusual."""
    found = []
    for it in items:
        base = baselines.get(it["sku"])
        if quantity_spike(it["qty"], base):
            found.append({"code": "QUANTITY_ANOMALY", "sku": it["sku"],
                          "text": f"{it['qty']}× {it['name']} is unusual (usually {round(base) if base else 'a few'})"})
    if has_history and merchant not in known_merchants:
        found.append({"code": "NEW_MERCHANT", "text": f"first order from {merchant}"})
    if odd_hour_check and odd_hour(now):
        found.append({"code": "ODD_HOUR", "text": "it is the middle of the night"})
    return found
