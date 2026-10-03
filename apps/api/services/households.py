"""Households, members and owner devices."""
from apps.common.db import append_event, create_household_row, lock_household, notify, tx
from core.mandates.signing import load_public_key
from core.saga.idempotency import new_ulid


def create(household_id: str, name: str, members: list[dict]):
    with tx() as conn:
        create_household_row(conn, household_id, name)
        lock_household(conn, household_id)
        append_event(conn, household_id, "household.created", {"name": name}, "onboarding")
        for m in members:
            conn.execute("INSERT INTO members (household_id, member_id, name, role, voice_profile_ref) "
                         "VALUES (%s,%s,%s,%s,%s)",
                         (household_id, m["member_id"], m["name"], m["role"], f"voice:{m['member_id']}"))
            append_event(conn, household_id, "member.added",
                         {"member_id": m["member_id"], "name": m["name"], "role": m["role"]}, "onboarding")


def register_device(household_id: str, member_id: str, label: str, public_key: str) -> str:
    load_public_key(public_key)  # validates P-256
    with tx() as conn:
        lock_household(conn, household_id)
        m = member(conn, household_id, member_id)
        if not m or m["role"] != "owner" or not m["active"]:
            raise PermissionError("only active owners can register approval devices")
        device_id = "dev_" + new_ulid().lower()
        conn.execute("INSERT INTO devices (device_id, household_id, member_id, label, public_key) "
                     "VALUES (%s,%s,%s,%s,%s)", (device_id, household_id, member_id, label, public_key))
        append_event(conn, household_id, "device.registered",
                     {"device_id": device_id, "member_id": member_id, "label": label}, member_id)
        return device_id


def revoke_device(household_id: str, device_id: str, by: str):
    with tx() as conn:
        lock_household(conn, household_id)
        conn.execute("UPDATE devices SET active = false WHERE device_id = %s AND household_id = %s",
                     (device_id, household_id))
        append_event(conn, household_id, "device.revoked", {"device_id": device_id}, by)
        notify(conn, household_id, "owners", "security", f"Device {device_id} was revoked by {by.capitalize()}.")


def members(conn, household_id: str) -> list[dict]:
    return conn.execute("SELECT * FROM members WHERE household_id = %s ORDER BY member_id", (household_id,)).fetchall()


def member(conn, household_id: str, member_id: str | None) -> dict | None:
    if not member_id:
        return None
    return conn.execute("SELECT * FROM members WHERE household_id = %s AND member_id = %s",
                        (household_id, member_id)).fetchone()


def owners(conn, household_id: str) -> list[str]:
    return [m["member_id"] for m in members(conn, household_id) if m["role"] == "owner" and m["active"]]


def remove_member(conn, household_id: str, member_id: str, by: str) -> dict:
    """Member revocation: they can no longer be resolved as a speaker, and their open
    escalations/requests close. Caller holds the household lock."""
    conn.execute("UPDATE members SET active = false WHERE household_id = %s AND member_id = %s",
                 (household_id, member_id))
    closed = conn.execute(
        "UPDATE approvals SET status = 'CANCELLED', decided_at = now(), decided_by = %s "
        "WHERE household_id = %s AND requested_by = %s AND status = 'PENDING' RETURNING approval_id, subject_id, kind",
        (by, household_id, member_id)).fetchall()
    purchases = conn.execute(
        "UPDATE purchases SET state = 'DENIED', updated_at = now(), notes = notes || %s::jsonb "
        "WHERE household_id = %s AND requested_by = %s AND state IN ('ESCALATED', 'AWAITING_CONFIRMATION') "
        "RETURNING purchase_id",
        ('["requester removed from household"]', household_id, member_id)).fetchall()
    devices = conn.execute("UPDATE devices SET active = false WHERE household_id = %s AND member_id = %s "
                           "RETURNING device_id", (household_id, member_id)).fetchall()
    append_event(conn, household_id, "member.removed",
                 {"member_id": member_id, "closed_approvals": [c["approval_id"] for c in closed],
                  "closed_purchases": [p["purchase_id"] for p in purchases],
                  "revoked_devices": [d["device_id"] for d in devices]}, by)
    notify(conn, household_id, "owners", "member", f"{member_id.capitalize()} was removed from the household by "
                                                   f"{by.capitalize()}; {len(closed) + len(purchases)} open requests closed.")
    return {"closed_approvals": len(closed), "closed_purchases": len(purchases)}
