"""Mandate projections and bitemporal writes."""
from datetime import datetime

from psycopg.types.json import Jsonb

from apps.common.db import append_event, notify, now
from core.mandates import conflicts as conflict_rules
from core.mandates.diff import diff, inr
from core.mandates.schema import validate_doc
from memory.temporal.bitemporal import as_of

GROUPS = {"groceries": {"milk", "atta", "diabetic_biscuits", "rice", "vegetables"}, "books": {"books"}}


def all_rows(conn, household_id: str) -> list[dict]:
    return conn.execute("SELECT * FROM mandate_versions WHERE household_id = %s ORDER BY row_id",
                        (household_id,)).fetchall()


def current_rows(conn, household_id: str) -> list[dict]:
    """What we currently believe is the latest version of every mandate."""
    rows = conn.execute(
        "SELECT * FROM mandate_versions WHERE household_id = %s AND recorded_to IS NULL AND valid_to IS NULL "
        "ORDER BY mandate_id, version", (household_id,)).fetchall()
    latest = {}
    for r in rows:
        latest[r["mandate_id"]] = r
    return list(latest.values())


def current(conn, household_id: str) -> list[dict]:
    return [r["doc"] for r in current_rows(conn, household_id)]


def meta(conn, household_id: str) -> dict:
    return {r["mandate_id"]: {"set_by": r["set_by"], "recorded_from": r["recorded_from"].isoformat()}
            for r in current_rows(conn, household_id)}


def get(conn, household_id: str, mandate_id: str) -> dict | None:
    for d in current(conn, household_id):
        if d["mandate_id"] == mandate_id:
            return d
    return None


def find(docs: list[dict], hint: str | None = None, categories: list[str] | None = None,
         owner: str | None = None) -> dict | None:
    """Resolve which mandate a spoken request refers to."""
    cats = set(categories or [])
    if hint and hint in GROUPS:
        cats |= GROUPS[hint] if not cats else set()
    if hint:
        for d in docs:
            if d["label"] == hint:
                return d
    candidates = [d for d in docs if cats & set(d["scope"]["categories"])] if cats else list(docs)
    if owner:
        own = [d for d in candidates if d["owner"] == owner]
        candidates = own or candidates
    if not candidates:
        return None
    return sorted(candidates, key=lambda d: (-len(cats & set(d["scope"]["categories"])), d["valid_from"]))[0]


def write_version(conn, household_id: str, new_doc: dict, set_by: str, effective_at: datetime,
                  event_type: str, extra: dict | None = None) -> dict:
    """Bitemporal write. Caller holds the household lock.
    The open-ended row of the old version stops being believed (recorded_to = now) and is
    re-recorded with its valid period closed at effective_at; the new version is valid from then."""
    t = now()
    doc = validate_doc(new_doc)
    old = conn.execute(
        "SELECT * FROM mandate_versions WHERE mandate_id = %s AND recorded_to IS NULL AND valid_to IS NULL "
        "ORDER BY version DESC LIMIT 1", (doc["mandate_id"],)).fetchone()
    if old:
        conn.execute("UPDATE mandate_versions SET recorded_to = %s WHERE row_id = %s", (t, old["row_id"]))
        conn.execute(
            "INSERT INTO mandate_versions (mandate_id, version, household_id, doc, set_by, valid_from, valid_to, "
            "recorded_from) VALUES (%s,%s,%s,%s,%s,%s,%s,%s)",
            (old["mandate_id"], old["version"], household_id, Jsonb(old["doc"]), old["set_by"], old["valid_from"],
             effective_at, t))
    conn.execute(
        "INSERT INTO mandate_versions (mandate_id, version, household_id, doc, set_by, valid_from, recorded_from) "
        "VALUES (%s,%s,%s,%s,%s,%s,%s)",
        (doc["mandate_id"], doc["version"], household_id, Jsonb(doc), set_by, effective_at, t))
    d = diff(old["doc"] if old else None, doc)
    ev = append_event(conn, household_id, event_type,
                      {"mandate": doc, "diff": d, "effective_at": effective_at.isoformat(), **(extra or {})}, set_by)
    _detect_conflicts(conn, household_id, doc)
    return {"doc": doc, "diff": d, "event": ev}


def _detect_conflicts(conn, household_id: str, doc: dict):
    others = current(conn, household_id)
    found = conflict_rules.conflicts(doc, others)
    for m in found:
        if m["owner"] == doc["owner"]:
            continue
        both = sorted({m["owner"], doc["owner"]})
        lim = conflict_rules.effective_limits([m, doc])
        text = (f"Conflicting rules: {doc['owner'].capitalize()}'s '{doc['label']}' and {m['owner'].capitalize()}'s "
                f"'{m['label']}' both cover {', '.join(sorted(set(doc['scope']['categories']) & set(m['scope']['categories'])))}. "
                f"The stricter limits apply ({inr(lim['per_month_inr'])}/month, {inr(lim['per_order_inr'])}/order) "
                f"until you resolve it.")
        append_event(conn, household_id, "mandate.conflict_detected",
                     {"mandates": [doc["mandate_id"], m["mandate_id"]], "owners": both, "effective_limits": lim},
                     "system")
        for o in both:
            notify(conn, household_id, o, "conflict", text, {"mandates": [doc["mandate_id"], m["mandate_id"]]})


def believed_at(conn, household_id: str, valid: datetime, recorded: datetime) -> list[dict]:
    return as_of(all_rows(conn, household_id), valid, recorded)
