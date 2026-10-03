"""A PayPal Orders v2-compatible mock with fault injection, for offline end-to-end tests.

Honours PayPal-Request-Id exactly like PayPal: a repeated id returns the stored response
instead of acting twice. State lives in Redis so it survives restarts.

Faults (POST /_faults), scoped to one household (purchase_units[].reference_id):
  {"household_id", "op": "create|capture|refund", "mode": "drop_response|error_503", "count": n}
  {"household_id", "op": "webhook", "mode": "duplicate|delay|drop", "delay_s": s, "count": n}
  {"mode": "down", "seconds": s}   -> every call 503s for s seconds
"""
import json
import os
import threading
import time
import uuid

import httpx
import redis
from fastapi import Body, FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse

r = redis.Redis.from_url(os.getenv("PAYPAL_MOCK_REDIS_URL", "redis://redis:6379/1"), decode_responses=True)
WEBHOOK_URL = os.getenv("WEBHOOK_TARGET_URL", "http://api:8000/webhooks/paypal")
WEBHOOK_SECRET = os.getenv("PAYPAL_WEBHOOK_SECRET", "mock-webhook-secret")
DROP_SLEEP = float(os.getenv("MOCK_DROP_SLEEP_SECONDS", "8"))
WEBHOOK_DELAY = float(os.getenv("MOCK_WEBHOOK_DELAY_SECONDS", "0.3"))

app = FastAPI(title="PayPal mock")


def _now():
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _take_fault(op: str, household_id: str | None):
    if r.get("fault:down"):
        return "error_503"
    key = f"fault:{op}:{household_id}"
    raw = r.get(key)
    if not raw:
        return None
    f = json.loads(raw)
    f["count"] -= 1
    if f["count"] <= 0:
        r.delete(key)
    else:
        r.set(key, json.dumps(f))
    return f


def _idempotent(request_id: str | None):
    if request_id:
        raw = r.get(f"rid:{request_id}")
        if raw:
            r.incr("stats:idempotent_replays")
            return json.loads(raw)
    return None


def _store(request_id: str | None, body: dict):
    if request_id:
        r.set(f"rid:{request_id}", json.dumps(body))


def _respond(body: dict, fault):
    """Apply a response-side fault AFTER the action committed."""
    mode = fault["mode"] if isinstance(fault, dict) else fault
    if mode == "drop_response":
        time.sleep(DROP_SLEEP)  # client times out: request landed, response lost
    return JSONResponse(body, status_code=201 if body.get("status") in ("CREATED", "COMPLETED") else 200)


def _pre_fault(fault):
    mode = fault["mode"] if isinstance(fault, dict) else fault
    if mode in ("error_503", "down"):
        raise HTTPException(503, "Service Unavailable (injected)")


def _send_webhook(event: dict, household_id: str):
    f = _take_fault("webhook", household_id)
    mode = f["mode"] if isinstance(f, dict) else None
    if mode == "drop":
        return
    delay = f.get("delay_s", 5) if mode == "delay" else WEBHOOK_DELAY
    copies = 2 if mode == "duplicate" else 1

    def run():
        time.sleep(delay)
        for _ in range(copies):
            try:
                httpx.post(WEBHOOK_URL, json=event, headers={"x-mock-webhook-secret": WEBHOOK_SECRET}, timeout=5)
            except httpx.HTTPError:
                pass
    threading.Thread(target=run, daemon=True).start()


@app.post("/v1/oauth2/token")
def token():
    if r.get("fault:down"):
        raise HTTPException(503, "down")
    return {"access_token": "mock-" + uuid.uuid4().hex, "token_type": "Bearer", "expires_in": 32400}


@app.post("/v2/checkout/orders")
def create_order(body: dict = Body(...), paypal_request_id: str | None = Header(default=None)):
    unit = body["purchase_units"][0]
    hh = unit.get("reference_id")
    fault = _take_fault("create", hh)
    _pre_fault(fault)
    stored = _idempotent(paypal_request_id)
    if stored:
        return _respond(stored, fault)
    oid = "MOCK-" + uuid.uuid4().hex[:17].upper()
    vaulted = bool(body.get("payment_source", {}).get("paypal", {}).get("vault_id"))
    order = {"id": oid, "intent": "CAPTURE", "status": "APPROVED" if vaulted else "PAYER_ACTION_REQUIRED",
             "create_time": _now(), "purchase_units": [unit],
             "links": [] if vaulted else [{"rel": "payer-action", "href": f"http://localhost:8002/checkoutnow?token={oid}"}]}
    r.set(f"order:{oid}", json.dumps(order))
    r.incr("stats:orders_created")
    _store(paypal_request_id, order)
    return _respond(order, fault)


@app.post("/v2/checkout/orders/{oid}/capture")
def capture(oid: str, paypal_request_id: str | None = Header(default=None)):
    raw = r.get(f"order:{oid}")
    if not raw:
        raise HTTPException(404, "RESOURCE_NOT_FOUND")
    order = json.loads(raw)
    unit = order["purchase_units"][0]
    hh = unit.get("reference_id")
    fault = _take_fault("capture", hh)
    _pre_fault(fault)
    stored = _idempotent(paypal_request_id)
    if stored:
        return _respond(stored, fault)
    if order["status"] == "COMPLETED":
        raise HTTPException(422, "ORDER_ALREADY_CAPTURED")
    if order["status"] != "APPROVED":
        raise HTTPException(422, "ORDER_NOT_APPROVED")
    cap_id = "CAP-" + uuid.uuid4().hex[:15].upper()
    unit["payments"] = {"captures": [{"id": cap_id, "status": "COMPLETED", "amount": unit["amount"],
                                      "custom_id": unit.get("custom_id"), "create_time": _now()}]}
    order["status"] = "COMPLETED"
    r.set(f"order:{oid}", json.dumps(order))
    r.set(f"capture:{cap_id}", oid)
    r.incr("stats:captures")
    r.incr(f"stats:captures_for:{unit.get('custom_id')}")
    r.incrby(f"stats:captured_paise:{unit.get('custom_id')}", int(round(float(unit["amount"]["value"]) * 100)))
    _store(paypal_request_id, order)
    _send_webhook({"id": "WH-" + uuid.uuid4().hex[:20].upper(), "event_type": "PAYMENT.CAPTURE.COMPLETED",
                   "create_time": _now(), "resource_type": "capture",
                   "resource": {**unit["payments"]["captures"][0],
                                "supplementary_data": {"related_ids": {"order_id": oid}}}}, hh)
    return _respond(order, fault)


@app.get("/v2/checkout/orders/{oid}")
def get_order(oid: str):
    if r.get("fault:down"):
        raise HTTPException(503, "down")
    raw = r.get(f"order:{oid}")
    if not raw:
        raise HTTPException(404, "RESOURCE_NOT_FOUND")
    return json.loads(raw)


@app.post("/v2/payments/captures/{cap_id}/refund")
def refund(cap_id: str, body: dict = Body(...), paypal_request_id: str | None = Header(default=None)):
    oid = r.get(f"capture:{cap_id}")
    if not oid:
        raise HTTPException(404, "RESOURCE_NOT_FOUND")
    order = json.loads(r.get(f"order:{oid}"))
    hh = order["purchase_units"][0].get("reference_id")
    fault = _take_fault("refund", hh)
    _pre_fault(fault)
    stored = _idempotent(paypal_request_id)
    if stored:
        return _respond(stored, fault)
    ref = {"id": "REF-" + uuid.uuid4().hex[:14].upper(), "status": "COMPLETED", "amount": body["amount"]}
    custom = order["purchase_units"][0].get("custom_id")
    r.incr(f"stats:refunds_for:{custom}")
    r.incrby(f"stats:refunded_paise:{custom}", int(round(float(body["amount"]["value"]) * 100)))
    _store(paypal_request_id, ref)
    return _respond(ref, fault)


@app.post("/_faults")
async def set_fault(request: Request):
    f = await request.json()
    if f.get("mode") == "down":
        if f.get("seconds", 0) > 0:
            r.set("fault:down", "1", ex=int(f["seconds"]))
        else:
            r.delete("fault:down")
        return {"ok": True}
    f.setdefault("count", 1)
    r.set(f"fault:{f['op']}:{f['household_id']}", json.dumps(f))
    return {"ok": True}


@app.delete("/_faults")
def clear_faults():
    for k in r.scan_iter("fault:*"):
        r.delete(k)
    return {"ok": True}


@app.get("/_stats/{purchase_id}")
def stats(purchase_id: str):
    return {"captures": int(r.get(f"stats:captures_for:{purchase_id}") or 0),
            "captured_paise": int(r.get(f"stats:captured_paise:{purchase_id}") or 0),
            "refunds": int(r.get(f"stats:refunds_for:{purchase_id}") or 0),
            "refunded_paise": int(r.get(f"stats:refunded_paise:{purchase_id}") or 0),
            "idempotent_replays_total": int(r.get("stats:idempotent_replays") or 0)}


@app.get("/health")
def health():
    r.ping()
    return {"ok": True}
