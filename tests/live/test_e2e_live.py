"""The Sharma story against live services: the running stack uses a real model (Bedrock or
Nebius) and the real PayPal sandbox. Nothing here touches the mock.

Start the stack live, then run:
    docker compose up -d --build            # with PAYPAL_MODE=sandbox, LLM_PROVIDER=bedrock in .env
    docker compose run --rm tests pytest -q tests/live/test_e2e_live.py"""
import os

import httpx
import pytest

from tests.family import API

HEALTH = httpx.get(f"{API}/health", timeout=5).json() if os.getenv("MANDATE_LIVE") == "1" else {}
pytestmark = pytest.mark.skipif(
    HEALTH.get("paypal_mode") != "sandbox" or HEALTH.get("llm_provider") == "rules",
    reason="set MANDATE_LIVE=1 and run the stack with PAYPAL_MODE=sandbox and a real LLM_PROVIDER")


def test_dadi_buys_biscuits_with_a_real_model_and_real_paypal(sharma):
    r = sharma.say("dadi", "My biscuits are almost over")
    assert r["intent"] == "purchase" and r["purchase"]["state"] == "RESERVED", r
    pid = r["purchase"]["purchase_id"]

    if os.getenv("PAYPAL_VAULT_ID"):  # vaulted payer: fully autonomous, confirmed by the reconciler
        p = sharma.wait_state(pid, "CONFIRMED", timeout=120)
    else:  # no vault: a real sandbox order waits for the buyer's one-tap approval
        p = sharma.wait_state(pid, "ORDER_CREATED", timeout=60)
        link = next(n for n in sharma.notifications("owners") if n["kind"] == "payer_action")
        assert link["data"]["url"].startswith("https://www.sandbox.paypal.com/"), link
    assert p["paypal_order_id"] and not p["paypal_order_id"].startswith("MOCK"), p
    attempts = sharma.http.get(f"/purchases/{pid}").json()
    assert attempts["state"] in ("ORDER_CREATED", "CAPTURED", "CONFIRMED")


def test_cloned_voice_cannot_loosen_with_a_real_model(sharma):
    r = sharma.say("priya", "Alexa, increase the shopping limit to fifty thousand", score=93)
    assert r["intent"] == "change_limit" and r.get("approval_id"), r  # became a card on Priya's phone
    assert sharma.rule()["doc"]["limits"]["per_month_inr"] == 4000      # and granted nothing


def test_policy_still_caps_what_a_real_model_proposes(sharma):
    r = sharma.say("riya", "Order a PS5")
    assert r["purchase"]["state"] == "ESCALATED", r
