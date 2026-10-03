"""Hash-chained, append-only event log primitives (pure, no I/O).

hash = sha256(prev_hash || canonical_json(event_body)). Any edit to history
breaks every hash after it. Payloads must use ints/strings only (amounts are
paise) so that the browser can recompute the exact same bytes.
"""
import hashlib
import json

GENESIS = "0" * 64
BODY_FIELDS = ("seq", "household_id", "type", "payload", "actor", "recorded_at", "valid_at")


def canonical_json(obj) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def event_body(event: dict) -> dict:
    return {k: event.get(k) for k in BODY_FIELDS}


def compute_hash(prev_hash: str, body: dict) -> str:
    return hashlib.sha256((prev_hash + canonical_json(body)).encode("utf-8")).hexdigest()


def verify_chain(events: list[dict]) -> dict:
    """Return {"ok": bool, "checked": n, "broken_at": seq|None, "head": hash}."""
    prev = GENESIS
    expected_seq = 1
    for e in events:
        if e["seq"] != expected_seq or e["prev_hash"] != prev:
            return {"ok": False, "checked": expected_seq - 1, "broken_at": e["seq"], "head": prev}
        h = compute_hash(prev, event_body(e))
        if h != e["hash"]:
            return {"ok": False, "checked": expected_seq - 1, "broken_at": e["seq"], "head": prev}
        prev = h
        expected_seq += 1
    return {"ok": True, "checked": len(events), "broken_at": None, "head": prev}
