"""Consumption rhythm: how long does each item last in this house?

Per-unit burn rate = days between orders / quantity bought, kept current with an
exponentially weighted average. Until 3–4 orders exist, estimates borrow a prior
scaled by household size. Spikes (festival, sale bulk-buys) are treated as outliers.
A prediction never spends money by itself."""
from datetime import datetime, timedelta

ALPHA = 0.5
MIN_ORDERS_FOR_HISTORY = 3
OUTLIER_RATIO = 3.0
# Days one unit lasts for a 4-person household.
PRIORS_DAYS_PER_UNIT = {"milk": 0.5, "atta": 16.0, "diabetic_biscuits": 3.5, "rice": 10.0, "vegetables": 1.0,
                        "books": 30.0}


def prior(category: str, household_size: int) -> float:
    base = PRIORS_DAYS_PER_UNIT.get(category, 7.0)
    return base * 4 / max(household_size, 1)


def burn_rate(orders: list[dict], category: str, household_size: int) -> dict:
    """orders: chronological [{at: datetime, qty: int}] for one item. Returns days per unit."""
    p = prior(category, household_size)
    rate, samples = None, 0
    for a, b in zip(orders, orders[1:]):
        days = (b["at"] - a["at"]).total_seconds() / 86400
        if a["qty"] <= 0 or days <= 0:
            continue
        r = days / a["qty"]
        if rate is not None and not (1 / OUTLIER_RATIO <= r / rate <= OUTLIER_RATIO):
            continue  # festival/guest spike or a sale bulk-buy
        rate = r if rate is None else ALPHA * r + (1 - ALPHA) * rate
        samples += 1
    if rate is None:
        return {"days_per_unit": p, "source": "prior", "samples": 0}
    if len(orders) < MIN_ORDERS_FOR_HISTORY:
        w = len(orders) / MIN_ORDERS_FOR_HISTORY
        return {"days_per_unit": w * rate + (1 - w) * p, "source": "blended", "samples": samples}
    return {"days_per_unit": rate, "source": "history", "samples": samples}


def forecast(orders: list[dict], category: str, household_size: int, now: datetime,
             feedback: list[dict] | None = None, paused: bool = False, away_factor: float = 1.0) -> dict:
    """Predict the run-out date for one item.
    feedback: [{at, kind: 'out'|'plenty'}] applied after the latest order.
    away_factor: <1 when a consumer is away (slower burn)."""
    if not orders:
        return {"run_out": None, "reason": "no history"}
    rate = burn_rate(orders, category, household_size)
    last = orders[-1]
    days_per_unit = rate["days_per_unit"] / max(away_factor, 0.1)
    run_out = last["at"] + timedelta(days=days_per_unit * last["qty"])
    note = None
    for f in feedback or []:
        if f["at"] < last["at"]:
            continue
        if f["kind"] == "out":
            run_out, note = f["at"], "you told me it's finished"
        elif f["kind"] == "plenty":
            run_out = max(run_out, f["at"]) + timedelta(days=days_per_unit * last["qty"] * 0.5)
            note = "you said there's still plenty"
    usual_qty = round(sum(o["qty"] for o in orders[-3:]) / len(orders[-3:]))
    return {"run_out": run_out, "days_left": (run_out - now).total_seconds() / 86400, "usual_qty": usual_qty,
            "rate": rate, "paused": paused, "note": note}


def baseline_qty(orders: list[dict]) -> float | None:
    if not orders:
        return None
    recent = orders[-4:]
    return sum(o["qty"] for o in recent) / len(recent)
