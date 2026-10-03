"""Postgres access, the hash-chained event append, and notifications."""
import json
import pathlib
from contextlib import contextmanager
from datetime import datetime, timezone

import psycopg
import redis as redis_lib
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from psycopg_pool import ConnectionPool

from apps.common import config
from core.ledger.hashchain import GENESIS, compute_hash, event_body

_pool: ConnectionPool | None = None
_redis = None


def pool() -> ConnectionPool:
    global _pool
    if _pool is None:
        _pool = ConnectionPool(config.DATABASE_URL, min_size=1, max_size=20, kwargs={"row_factory": dict_row},
                               open=True)
    return _pool


def redis():
    global _redis
    if _redis is None:
        _redis = redis_lib.Redis.from_url(config.REDIS_URL, decode_responses=True)
    return _redis


@contextmanager
def tx():
    """One transaction. Commits on success, rolls back on exception."""
    with pool().connection() as conn:
        with conn.transaction():
            yield conn


def migrate():
    sql_dir = pathlib.Path(__file__).resolve().parents[2] / "infra" / "migrations"
    with psycopg.connect(config.DATABASE_URL, autocommit=True) as conn:
        conn.execute("SELECT pg_advisory_lock(424242)")
        try:
            for f in sorted(sql_dir.glob("*.sql")):
                conn.execute(f.read_text(encoding="utf-8"))
        finally:
            conn.execute("SELECT pg_advisory_unlock(424242)")


def now() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def lock_household(conn, household_id: str) -> dict:
    """Per-household serialization point: every write path takes this row lock first,
    so budget checks never race (the second request sees the first one's hold)."""
    row = conn.execute("SELECT * FROM households WHERE id = %s FOR UPDATE", (household_id,)).fetchone()
    if row is None:
        raise KeyError(f"household {household_id} not found")
    return row


def append_event(conn, household_id: str, type_: str, payload: dict, actor: str,
                 valid_at: datetime | None = None) -> dict:
    """Append to the household's hash chain. Caller must hold lock_household()."""
    hh = conn.execute("SELECT last_seq, head_hash FROM households WHERE id = %s", (household_id,)).fetchone()
    t = now()
    ev = {"seq": hh["last_seq"] + 1, "household_id": household_id, "type": type_,
          "payload": json.loads(json.dumps(payload, default=str)), "actor": actor,
          "recorded_at": iso(t), "valid_at": iso(valid_at or t)}
    ev["prev_hash"] = hh["head_hash"]
    ev["hash"] = compute_hash(ev["prev_hash"], event_body(ev))
    conn.execute(
        "INSERT INTO events (household_id, seq, type, payload, actor, recorded_at, valid_at, prev_hash, hash) "
        "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)",
        (household_id, ev["seq"], type_, Jsonb(ev["payload"]), actor, ev["recorded_at"], ev["valid_at"],
         ev["prev_hash"], ev["hash"]))
    conn.execute("UPDATE households SET last_seq = %s, head_hash = %s WHERE id = %s",
                 (ev["seq"], ev["hash"], household_id))
    return ev


def create_household_row(conn, household_id: str, name: str):
    conn.execute("INSERT INTO households (id, name, head_hash) VALUES (%s, %s, %s)", (household_id, name, GENESIS))


def notify(conn, household_id: str, audience: str, kind: str, text: str, data: dict | None = None):
    """Store a notification and publish it after commit (best-effort live push)."""
    row = conn.execute(
        "INSERT INTO notifications (household_id, audience, kind, text, data) VALUES (%s,%s,%s,%s,%s) "
        "RETURNING id, created_at", (household_id, audience, kind, text, Jsonb(data or {}))).fetchone()
    msg = json.dumps({"id": row["id"], "audience": audience, "kind": kind, "text": text, "data": data or {},
                      "created_at": iso(row["created_at"])}, default=str)
    try:
        redis().publish(f"hh:{household_id}", msg)
    except redis_lib.RedisError:
        pass  # the notification is durable in Postgres; live push is a convenience
