"""Exactly-once money under failure: concurrency, Wi-Fi cuts, webhooks, outages, refunds."""
import os
import threading
import time

import httpx

from core.saga.idempotency import new_ulid, request_id

PAYPAL = os.getenv("PAYPAL_MOCK_URL", "http://localhost:8002")


def test_double_submit_same_purchase_id_is_one_purchase(sharma):
    pid = new_ulid()
    a = sharma.say("arjun", "order 2 milk", purchase_id=pid)
    b = sharma.say("arjun", "order 2 milk", purchase_id=pid)
    assert a["purchase"]["purchase_id"] == b["purchase"]["purchase_id"] == pid and b["duplicate"]
    sharma.wait_state(pid, "CONFIRMED")
    assert sharma.paypal_stats(pid)["captures"] == 1
    assert len(sharma.events("purchase.duplicate_request")) == 1


def test_two_members_order_at_the_same_second(family_factory):
    fam = family_factory(grocery_month=1500, grocery_order=1500)
    results = {}

    def go(who):
        pid = new_ulid()
        r = fam.http.post(f"/households/{fam.hh}/purchases",
                          json={"purchase_id": pid, "requested_by": who, "lines": [{"sku": "atta_aashirvaad_10kg", "qty": 2}]})
        results[who] = r.json()["purchase"]
    ts = [threading.Thread(target=go, args=(w,)) for w in ("priya", "arjun")]
    [t.start() for t in ts]
    [t.join() for t in ts]
    states = sorted(p["state"] for p in results.values())
    assert states == ["ESCALATED", "RESERVED"], results
    loser = next(p for p in results.values() if p["state"] == "ESCALATED")
    assert loser["decision"]["code"] == "MONTHLY_LIMIT"


def test_wifi_cut_after_capture_no_double_charge(sharma):
    sharma.fault(op="capture", mode="drop_response")
    sharma.fault(op="webhook", mode="delay", delay_s=20)  # force the retry path, not a webhook rescue
    r = sharma.say("arjun", "order 2 milk")
    pid = r["purchase"]["purchase_id"]
    p = sharma.wait_state(pid, "CONFIRMED", timeout=40)
    caps = [a for a in p["attempts"] if a["step"] == "capture"]
    assert caps[0]["outcome"] == "UNKNOWN" and caps[-1]["outcome"] == "OK"
    assert {a["request_id"] for a in caps} == {request_id(pid, "capture")}  # never a new key
    s = sharma.paypal_stats(pid)
    assert s["captures"] == 1 and s["captured_paise"] == 16100
    assert sharma.events("payment.outcome_unknown")


def test_wifi_cut_after_create_order(sharma):
    sharma.fault(op="create", mode="drop_response")
    pid = sharma.say("arjun", "order 2 milk")["purchase"]["purchase_id"]
    p = sharma.wait_state(pid, "CONFIRMED", timeout=40)
    creates = [a for a in p["attempts"] if a["step"] == "create"]
    assert [a["outcome"] for a in creates][:1] == ["UNKNOWN"] and creates[-1]["outcome"] == "OK"
    assert {a["request_id"] for a in creates} == {request_id(pid, "create")}
    assert sharma.paypal_stats(pid)["captures"] == 1


def test_device_offline_replay_lands_once(sharma):
    """The edge device queued a signed request while offline and replays it on reconnect."""
    pid = new_ulid()
    body = {"purchase_id": pid, "requested_by": "dadi", "lines": [{"sku": "biscuits_sugarfree_marie", "qty": 2}]}
    first = sharma.http.post(f"/households/{sharma.hh}/purchases", json=body).json()
    replay = sharma.http.post(f"/households/{sharma.hh}/purchases", json=body).json()
    assert replay["duplicate"] and first["purchase"]["purchase_id"] == pid
    sharma.wait_state(pid, "CONFIRMED")
    assert sharma.paypal_stats(pid)["captures"] == 1


def test_duplicate_webhook_applied_once(sharma):
    sharma.fault(op="webhook", mode="duplicate")
    pid = sharma.say("arjun", "order 2 milk")["purchase"]["purchase_id"]
    sharma.wait_state(pid, "CONFIRMED")
    deadline = time.time() + 5
    while time.time() < deadline and not sharma.events("webhook.duplicate_ignored"):
        time.sleep(0.3)
    assert len(sharma.events("webhook.duplicate_ignored")) == 1
    assert len([e for e in sharma.events("purchase.confirmed") if e["payload"]["purchase_id"] == pid]) == 1


def test_webhook_lost_reconciler_confirms(sharma):
    sharma.fault(op="webhook", mode="drop")
    pid = sharma.say("arjun", "order 2 milk")["purchase"]["purchase_id"]
    sharma.wait_state(pid, "CAPTURED")
    p = sharma.wait_state(pid, "CONFIRMED", timeout=30)
    ev = [e for e in sharma.events("purchase.confirmed") if e["payload"]["purchase_id"] == pid]
    assert ev[0]["payload"]["via"] == "reconciler"
    assert sharma.events("payment.reconciled") and p["reservation"]["status"] == "CONVERTED"


def test_stale_and_unsigned_webhooks(sharma):
    pid = sharma.say("arjun", "order 2 milk")["purchase"]["purchase_id"]
    sharma.wait_state(pid, "CONFIRMED")
    ev = {"id": "WH-STALE-" + new_ulid(), "event_type": "PAYMENT.CAPTURE.COMPLETED",
          "resource": {"id": "CAP-OLD", "custom_id": pid, "status": "COMPLETED"}}
    ok = sharma.http.post("/webhooks/paypal", json=ev, headers={"x-mock-webhook-secret": "mock-webhook-secret"}).json()
    assert ok["status"] == "stale"
    bad = sharma.http.post("/webhooks/paypal", json=ev, headers={"x-mock-webhook-secret": "nope"})
    assert bad.status_code == 401
    assert sharma.purchase(pid)["state"] == "CONFIRMED"


def test_paypal_down_queues_retries_and_recovers(sharma):
    httpx.post(f"{PAYPAL}/_faults", json={"mode": "down", "seconds": 6}).raise_for_status()
    r = sharma.say("arjun", "order 2 milk")
    pid = r["purchase"]["purchase_id"]
    deadline = time.time() + 10
    while time.time() < deadline and not any("isn't reachable" in s for s in sharma.spoken()):
        time.sleep(0.3)
    assert any("queued the order" in s for s in sharma.spoken())
    p = sharma.wait_state(pid, "CONFIRMED", timeout=60)
    assert any(a["outcome"] == "UNAVAILABLE" for a in p["attempts"])
    assert sharma.paypal_stats(pid)["captures"] == 1


def test_crash_between_db_write_and_paypal_call(sharma):
    sharma.test_hook("worker/pause")
    try:
        pid = sharma.say("arjun", "order 2 milk")["purchase"]["purchase_id"]
        time.sleep(2)
        p = sharma.purchase(pid)
        assert p["state"] == "RESERVED" and p["outbox"][0]["status"] == "PENDING" and p["attempts"] == []
    finally:
        sharma.test_hook("worker/resume")
    sharma.wait_state(pid, "CONFIRMED")
    assert sharma.paypal_stats(pid)["captures"] == 1


def test_long_outage_hold_expires_and_budget_released(sharma):
    used0 = sharma.rule()["used"]
    httpx.post(f"{PAYPAL}/_faults", json={"mode": "down", "seconds": 60}).raise_for_status()
    try:
        pid = sharma.say("arjun", "order 2 milk")["purchase"]["purchase_id"]
        assert sharma.rule()["used"] == used0 + 16100  # held
        sharma.test_hook(f"expire-hold/{pid}")
        p = sharma.wait_state(pid, "EXPIRED")
        assert p["reservation"]["status"] == "RELEASED"
        assert sharma.rule()["used"] == used0
        assert any("couldn't complete" in s for s in sharma.spoken())
    finally:
        httpx.post(f"{PAYPAL}/_faults", json={"mode": "down", "seconds": 0})
    time.sleep(1)
    assert sharma.paypal_stats(pid)["captures"] == 0


def test_price_change_between_cart_and_capture_asks_first(sharma):
    sharma.test_hook("worker/pause")
    try:
        pid = sharma.say("arjun", "order atta")["purchase"]["purchase_id"]
        sharma.test_hook(f"catalog/{sharma.hh}", {"prices": {"atta_aashirvaad_10kg": 58000}})
    finally:
        sharma.test_hook("worker/resume")
    p = sharma.wait_state(pid, "AWAITING_CONFIRMATION")
    assert p["decision"]["code"] == "PRICE_CHANGED"
    assert any("went up from ₹545 to ₹605" in s for s in sharma.spoken())
    sharma.say("arjun", "yes")
    p = sharma.wait_state(pid, "CONFIRMED")
    assert p["final_total"] == 60500 and sharma.paypal_stats(pid)["captured_paise"] == 60500
    sharma.test_hook(f"catalog/{sharma.hh}", {"clear": True})


def test_surprise_fee_checked_against_final_total(sharma):
    sharma.test_hook("worker/pause")
    try:
        pid = sharma.say("arjun", "order 2 atta")["purchase"]["purchase_id"]  # ₹1,065 estimate
        sharma.test_hook(f"catalog/{sharma.hh}", {"surcharge": 60000})      # +₹600 at checkout
    finally:
        sharma.test_hook("worker/resume")
    sharma.wait_state(pid, "AWAITING_CONFIRMATION")
    r = sharma.say("arjun", "yes")
    p = sharma.purchase(pid)
    assert p["state"] == "ESCALATED" and p["decision"]["code"] == "PER_ORDER_LIMIT"  # ₹1,665 > ₹1,500
    assert sharma.paypal_stats(pid)["captures"] == 0
    sharma.test_hook(f"catalog/{sharma.hh}", {"clear": True})


def test_partial_delivery_refunds_missing_items(sharma):
    pid = sharma.say("arjun", "order 2 milk")["purchase"]["purchase_id"]
    sharma.wait_state(pid, "CONFIRMED")
    used = sharma.rule()["used"]
    out = sharma.http.post(f"/purchases/{pid}/delivery", json={"delivered": [{"sku": "milk_amul_1l", "qty": 1}]}).json()
    assert out["refund"] == 6800
    p = sharma.wait_state(pid, "PARTIALLY_REFUNDED")
    assert p["refunded"] == 6800 and sharma.paypal_stats(pid)["refunded_paise"] == 6800
    assert sharma.rule()["used"] == used - 6800
    refunds = [a for a in p["attempts"] if a["step"] == "refund"]
    assert refunds[0]["request_id"] == request_id(pid, "refund")
