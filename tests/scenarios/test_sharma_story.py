"""The Sharma story end to end: spending inside mandates, escalation, explain, what-if."""
import time


def test_onboarding_creates_signed_mandates(sharma):
    rules = {r["label"]: r for r in sharma.constitution()["rules"]}
    g = rules["groceries"]
    assert g["doc"]["limits"] == {"per_month_inr": 4000, "per_order_inr": 1500}
    assert set(g["doc"]["members_allowed"]) == {"priya", "arjun", "dadi"}
    assert g["doc"]["scope"]["categories"] == ["atta", "diabetic_biscuits", "milk"]
    assert g["signature"]["alg"] == "ES256" and g["signature"]["member"] == "priya"
    assert set(rules["books"]["doc"]["members_allowed"]) == {"priya", "riya"}
    assert rules["books"]["doc"]["limits"]["per_month_inr"] == 500


def test_dadi_biscuits_happy_path_and_why(sharma):
    r = sharma.say("dadi", "My biscuits are almost over")
    assert r["tier"] == 1 and r["purchase"]["state"] == "RESERVED"
    pid = r["purchase"]["purchase_id"]
    p = sharma.wait_state(pid, "CONFIRMED")
    assert p["final_total"] == 2 * 12000 + 2500  # usual quantity from memory + delivery fee
    assert p["reservation"]["status"] == "CONVERTED"
    assert [a["step"] for a in p["attempts"]] == ["create", "capture"]
    assert sharma.paypal_stats(pid)["captures"] == 1
    assert any("Done — ordered 2× Sugar-free Marie" in s for s in sharma.spoken())
    why = sharma.say("priya", "Why did you buy that?")
    assert "Priya's groceries rule" in why["speech"] and "keep the house stocked" in why["speech"].lower()
    assert len(why["proof"]) >= 5 and all(len(e["hash"]) == 64 for e in why["proof"])
    assert sharma.rule()["used"] == 26500


def test_riya_ps5_escalates_and_is_denied_on_expiry(sharma):
    r = sharma.say("riya", "Order a PS5")
    p = r["purchase"]
    assert p["state"] == "ESCALATED" and p["decision"]["code"] == "OUT_OF_SCOPE"
    pending = [a for a in sharma.approvals() if a["subject_id"] == p["purchase_id"]]
    assert len(pending) == 1 and pending[0]["kind"] == "escalation"
    assert any(n["kind"] == "approval" for n in sharma.notifications("owners"))
    sharma.test_hook(f"expire-approval/{pending[0]['approval_id']}")
    p = sharma.wait_state(p["purchase_id"], "DENIED")
    assert p["decision"]["code"] == "OUT_OF_SCOPE"
    assert sharma.paypal_stats(p["purchase_id"])["captures"] == 0


def test_riya_ps5_owner_approves_one_off(sharma):
    p = sharma.say("riya", "Order a PS5")["purchase"]
    a = next(a for a in sharma.approvals() if a["subject_id"] == p["purchase_id"])
    assert sharma.decide(a["approval_id"], "arjun", "approve")["status"] == "APPLIED"
    p = sharma.wait_state(p["purchase_id"], "CONFIRMED")
    assert p["approved_by"] == "arjun" and p["decision"]["code"] == "OWNER_APPROVED_ONE_OFF"


def test_riya_book_allowance_inside_and_over(sharma):
    r = sharma.say("riya", "Can you buy a book for me")
    p = r["purchase"]
    # First order from a new merchant: Riya is asked once, and can confirm her own allowance spend.
    assert p["state"] == "AWAITING_CONFIRMATION" and p["decision"]["code"] == "NEW_MERCHANT"
    r = sharma.say("riya", "yes")
    p = sharma.wait_state(p["purchase_id"], "CONFIRMED")
    assert p["final_total"] == 37500
    r2 = sharma.say("riya", "buy a book")
    assert r2["purchase"]["state"] == "ESCALATED" and r2["purchase"]["decision"]["code"] == "MONTHLY_LIMIT"
    assert "budget" in r2["speech"]


def test_get_the_usual_resolves_from_memory_then_confirms(sharma):
    r = sharma.say("dadi", "Alexa, get the usual")
    assert r["purchase"]["state"] == "AWAITING_CONFIRMATION"
    assert "Sugar-free Marie" in r["speech"] and "×2" in r["speech"] and "₹265" in r["speech"]
    sharma.say("dadi", "yes")
    sharma.wait_state(r["purchase"]["purchase_id"], "CONFIRMED")


def test_quantity_drift_asks_before_buying_and_no_cancels(sharma):
    r = sharma.say("arjun", "order 20 milk")
    p = r["purchase"]
    assert p["state"] == "AWAITING_CONFIRMATION" and p["decision"]["code"] == "QUANTITY_ANOMALY"
    assert "unusual" in r["speech"]
    sharma.say("arjun", "no")
    assert sharma.purchase(p["purchase_id"])["state"] == "DENIED"


def test_explain_whatif_counterfactual(sharma):
    r = sharma.say("priya", "What if the grocery budget had been ₹3,000 last month?")
    assert r["tier"] == 0 and r["orders"] > 10
    assert len(r["would_escalate"]) >= 1 and "would have needed approval" in r["speech"]
    big = sharma.http.get(f"/households/{sharma.hh}/whatif", params={"limit_inr": 20000}).json()
    assert big["would_escalate"] == [] and "still have gone through" in big["speech"]


def test_budget_query_hides_household_amounts_from_minor(sharma):
    assert "groceries" in sharma.say("dadi", "How much is left this month?")["speech"]
    riya = sharma.say("riya", "how much is left")
    assert "books" in riya["speech"] and "groceries" not in riya["speech"]
    assert riya.get("budgets") is None


def test_vague_limit_refused_with_suggestion(sharma):
    r = sharma.say("priya", "Keep the house stocked with rice, just don't spend too much")
    assert r["refused"] == "VAGUE_LIMIT" and r["suggested_limit_inr"] >= 500
    assert "I need a number" in r["speech"]
    assert "rice" not in str([x["doc"]["scope"] for x in sharma.constitution()["rules"]])


def test_rhythm_learns_burn_rate_and_feedback(sharma):
    rh = {x["sku"]: x for x in sharma.http.get(f"/households/{sharma.hh}/rhythm").json()}
    milk = rh["milk_amul_1l"]
    assert milk["source"] == "history" and 0.8 <= milk["days_per_unit"] <= 1.2  # 2 L every 2 days
    assert rh["atta_aashirvaad_10kg"]["days_per_unit"] > 10
    r = sharma.say("priya", "We're already out of milk")
    assert "Should I order" in r["speech"]
    milk = {x["sku"]: x for x in sharma.http.get(f"/households/{sharma.hh}/rhythm").json()}["milk_amul_1l"]
    assert milk["running_low"] and milk["note"] == "you told me it's finished"


def test_month_boundary_hold_counts_in_month_it_was_placed(sharma):
    r = sharma.say("arjun", "order 2 milk")
    pid = r["purchase"]["purchase_id"]
    sharma.wait_state(pid, "CONFIRMED")
    before = sharma.rule()["used"]
    sharma.test_hook(f"backdate/{pid}", {"at": "2026-09-30T23:59:00+05:30"})
    assert sharma.rule()["used"] == before - 16100
    time.sleep(0.1)
