"""The real PayPal sandbox, no mock: the behaviours the saga depends on.

Runs only with PAYPAL_MODE=sandbox and PAYPAL_CLIENT_ID/SECRET set:
    PAYPAL_MODE=sandbox docker compose run --rm tests pytest -q tests/live/test_paypal_sandbox_live.py"""
import os

import pytest

from adapters.paypal.client import PayPalClient, PayPalRejected, approve_link, capture_id_of
from core.saga.idempotency import new_ulid, request_id

pytestmark = pytest.mark.skipif(
    os.getenv("PAYPAL_MODE") != "sandbox" or not os.getenv("PAYPAL_CLIENT_ID"),
    reason="PAYPAL_MODE=sandbox with PAYPAL_CLIENT_ID/SECRET required")


@pytest.fixture(scope="module")
def pp():
    return PayPalClient()


@pytest.fixture(scope="module")
def order(pp):
    pid = new_ulid()
    rid = request_id(pid, "create")
    first = pp.create_order(rid, pid, "hh_live_sharma", 26500, "Sugar-free Marie x2 (Mandate live test)")
    again = pp.create_order(rid, pid, "hh_live_sharma", 26500, "Sugar-free Marie x2 (Mandate live test)")
    return {"pid": pid, "first": first, "again": again}


def test_oauth_against_sandbox(pp):
    assert pp.base == "https://api-m.sandbox.paypal.com"
    assert pp._auth()["Authorization"].startswith("Bearer ")


def test_create_is_idempotent_on_paypal_request_id(order):
    """Same PayPal-Request-Id twice -> PayPal returns the original order, never a second one."""
    assert order["first"]["id"] == order["again"]["id"]
    assert order["first"]["status"] in ("CREATED", "PAYER_ACTION_REQUIRED", "APPROVED")


def test_order_carries_purchase_and_amount(pp, order):
    o = pp.get_order(order["first"]["id"])
    unit = o["purchase_units"][0]
    assert unit["custom_id"] == order["pid"] and unit["reference_id"] == "hh_live_sharma"
    assert unit["amount"] == pp.amount(26500)  # ₹265, or its sandbox-currency equivalent


def test_unapproved_capture_is_a_clean_rejection(pp, order):
    """Without a vaulted payer the saga waits for buyer approval; it recognises this exact error."""
    if pp.vault_id:
        pytest.skip("vaulted payer: capture is expected to succeed")
    assert approve_link(order["first"]), order["first"]
    with pytest.raises(PayPalRejected) as e:
        pp.capture(request_id(order["pid"], "capture"), order["first"]["id"])
    assert "ORDER_NOT_APPROVED" in str(e.value) or "PAYER_ACTION_REQUIRED" in str(e.value)


@pytest.mark.skipif(not os.getenv("PAYPAL_VAULT_ID"), reason="PAYPAL_VAULT_ID (vaulted sandbox buyer) not set")
def test_vaulted_capture_then_partial_refund(pp):
    pid = new_ulid()
    o = pp.create_order(request_id(pid, "create"), pid, "hh_live_sharma", 53000, "Atta 10kg (Mandate live test)")
    captured = pp.capture(request_id(pid, "capture"), o["id"])
    assert captured["status"] == "COMPLETED"
    again = pp.capture(request_id(pid, "capture"), o["id"])  # retry with the same key: no second charge
    assert capture_id_of(again) == capture_id_of(captured)
    refund = pp.refund(request_id(pid, "refund"), capture_id_of(captured), 10000)
    assert refund["status"] in ("COMPLETED", "PENDING")
