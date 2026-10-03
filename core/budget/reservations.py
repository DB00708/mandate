"""Budget arithmetic. Spend counts against the month the hold was placed
(reservation timestamp, not capture time)."""
from datetime import datetime, timedelta, timezone

IST = timezone(timedelta(hours=5, minutes=30))


def month_key(ts: datetime) -> str:
    return ts.astimezone(IST).strftime("%Y-%m")


def month_bounds(key: str) -> tuple[datetime, datetime]:
    y, m = map(int, key.split("-"))
    start = datetime(y, m, 1, tzinfo=IST)
    end = datetime(y + (m == 12), m % 12 + 1, 1, tzinfo=IST)
    return start, end


def previous_month_key(ts: datetime) -> str:
    first = ts.astimezone(IST).replace(day=1)
    return month_key(first - timedelta(days=1))


def used_in_month(reservations: list[dict], mandate_id: str, key: str) -> int:
    """Sum of ACTIVE holds + CONVERTED spend (net of refunds) for a mandate in a month."""
    total = 0
    for r in reservations:
        if r["mandate_id"] == mandate_id and r["month_key"] == key and r["status"] in ("ACTIVE", "CONVERTED"):
            total += r["amount"] - r.get("refunded", 0)
    return total


def available(limit: int, used: int) -> int:
    return limit - used
