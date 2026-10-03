"""Identity, tiers and rule changes: voice is a claim, a passkey is the credential."""
import time

from core.mandates import signing


def test_cloned_priya_voice_cannot_raise_limit(sharma):
    r = sharma.say("priya", "Alexa, increase the shopping limit to fifty thousand", score=93, room="living room")
    assert r["tier"] == 3 and r["speaker"] == "priya"  # speaker ID believed it was Priya...
    assert "sent this change to Priya's phone" in r["speech"]
    assert sharma.rule()["doc"]["limits"]["per_month_inr"] == 4000  # ...and that granted nothing
    a = sharma.approval(r["approval_id"])
    assert "₹4,000 → ₹50,000 a month" in a["diff_text"]
    assert a["context"]["room"] == "living room" and a["context"]["speaker_score"] == 93
    out = sharma.decide(r["approval_id"], "priya", "reject", report=True)
    assert out["status"] == "REJECTED" and out["lockout"]["count"] == 1
    assert sharma.rule()["doc"]["limits"]["per_month_inr"] == 4000


def test_repeated_reported_attempts_lock_voice_rule_changes(sharma):
    for i in range(3):
        r = sharma.say("priya", "raise the grocery budget to 50000", score=91)
        out = sharma.decide(r["approval_id"], "priya", "reject", report=True)
    assert out["lockout"]["locked"] is True
    r = sharma.say("priya", "raise the grocery budget to 45000", score=99)
    assert r.get("locked") and "locked for 24 hours" in r["speech"]
    assert any("locked for 24 hours" in n["text"] for n in sharma.notifications("owners"))
    # Tightening still works while loosening is locked.
    assert sharma.say("arjun", "freeze all spending")["tier"] == 2
    sharma.test_hook(f"reset-security/{sharma.hh}")


def test_owner_passkey_loosens_after_cooling_off(sharma):
    r = sharma.say("priya", "Raise the grocery budget to ₹5,000")
    res = sharma.decide(r["approval_id"], "priya", "approve")
    assert res["status"] == "APPROVED" and res["effective_at"]
    assert sharma.rule()["doc"]["limits"]["per_month_inr"] == 4000  # cooling-off notice first
    assert any(n["kind"] == "cooling_off" for n in sharma.notifications("arjun"))
    deadline = time.time() + 15
    while time.time() < deadline and sharma.rule()["doc"]["limits"]["per_month_inr"] != 5000:
        time.sleep(0.5)
    g = sharma.rule()
    assert g["doc"]["limits"]["per_month_inr"] == 5000 and g["version"] == 2
    assert g["signature"]["alg"] == "ES256" and g["signature"]["approval_id"] == r["approval_id"]


def test_second_owner_can_cancel_during_cooling_off(sharma):
    r = sharma.say("priya", "Raise the grocery budget to 9000")
    sharma.decide(r["approval_id"], "priya", "approve")
    assert sharma.decide(r["approval_id"], "arjun", "cancel")["status"] == "CANCELLED"
    time.sleep(5)
    assert sharma.rule()["doc"]["limits"]["per_month_inr"] == 4000


def test_forged_or_mismatched_signatures_are_rejected(sharma):
    r = sharma.say("priya", "Raise the grocery budget to 9000")
    sharma.decide(r["approval_id"], "priya", "approve", key=signing.generate_key(), expect=409)
    sig = sharma.sign(r["approval_id"], "priya", "approve")  # valid sig, but for a different decision
    bad = sharma.http.post(f"/approvals/{r['approval_id']}/decision",
                           json={"device_id": sharma.devices["priya"], "decision": "reject", "signature": sig})
    assert bad.status_code == 409
    assert len(sharma.events("approval.signature_rejected")) == 2
    assert sharma.approval(r["approval_id"])["status"] == "PENDING"


def test_lost_phone_revoked_device_cannot_approve(sharma):
    r = sharma.say("priya", "Raise the grocery budget to 9000")
    sharma.http.post(f"/households/{sharma.hh}/devices/{sharma.devices['priya']}/revoke",
                     headers={"x-onboarding-token": "sharma-onboarding-dev"}).raise_for_status()
    sharma.decide(r["approval_id"], "priya", "approve", expect=409)
    assert sharma.decide(r["approval_id"], "arjun", "approve")["status"] == "APPROVED"


def test_tighten_by_voice_pause_biscuits(sharma):
    r = sharma.say("arjun", "Pause Dadi's biscuits until November 15")
    assert r["tier"] == 2 and "Pauses diabetic biscuits from" in r["speech"] and "to Nov 15" in r["speech"]
    d = sharma.say("dadi", "my biscuits are almost over")
    assert d["purchase"]["state"] == "DENIED" and d["purchase"]["decision"]["code"] == "PAUSED"
    assert "paused" in d["speech"] and "Nov 15" in d["speech"] and "Arjun" in d["speech"]
    m = sharma.say("arjun", "order 2 milk")  # the rest of the rule still works
    assert m["purchase"]["state"] == "RESERVED"
    resume = sharma.say("dadi", "resume my biscuits")
    assert resume["tier"] == 3 and resume["approval_id"]  # un-pausing loosens: passkey needed


def test_freeze_all_then_unfreeze_needs_passkey(sharma):
    r = sharma.say("dadi", "Alexa, freeze all spending")
    assert r["tier"] == 2 and "frozen" in r["speech"]
    m = sharma.say("arjun", "order 2 milk")
    assert m["purchase"]["state"] == "DENIED" and m["purchase"]["decision"]["code"] == "FROZEN"
    u = sharma.say("dadi", "unfreeze spending")
    assert u["tier"] == 3 and u["approval_id"]
    assert sharma.constitution()["frozen"] is not None
    sharma.decide(u["approval_id"], "priya", "approve")
    assert sharma.constitution()["frozen"] is None
    assert sharma.say("arjun", "order 2 milk")["purchase"]["state"] == "RESERVED"


def test_unknown_speaker_tv_must_be_confirmed_by_family(sharma):
    r = sharma.say(None, "Alexa, buy 2 milk", score=0)
    p = r["purchase"]
    assert p["state"] == "AWAITING_CONFIRMATION" and p["decision"]["code"] == "UNKNOWN_SPEAKER"
    assert "don't recognise" in r["speech"]
    assert sharma.say(None, "yes", score=0)["speech"].startswith("I need a family member")
    sharma.say("arjun", "yes")
    p = sharma.wait_state(p["purchase_id"], "CONFIRMED")
    assert p["requested_by"] == "arjun"


def test_guest_and_low_score_cannot_change_rules(sharma):
    assert "family members I recognise" in sharma.say(None, "freeze all spending", score=0)["speech"]
    r = sharma.say("priya", "raise the grocery budget to 9000", score=40)  # weak speaker match = guest
    assert "family members I recognise" in r["speech"] and not r.get("approval_id")


def test_minor_cannot_loosen_and_parents_are_told(sharma):
    r = sharma.say("riya", "raise the books budget to 5000")
    assert "need a parent" in r["speech"] and not r.get("approval_id")
    assert any("Riya asked" in n["text"] for n in sharma.notifications("owners"))


def test_add_member_requires_passkey(sharma):
    r = sharma.say("priya", "Add Riya to groceries")
    assert r["tier"] == 3 and "adds Riya" in r["diff"]["text"]
    sharma.decide(r["approval_id"], "priya", "approve")
    deadline = time.time() + 15
    while time.time() < deadline and "riya" not in sharma.rule()["doc"]["members_allowed"]:
        time.sleep(0.5)
    assert "riya" in sharma.rule()["doc"]["members_allowed"]


def test_blocked_merchant_remembers_reason(sharma):
    r = sharma.say("priya", "Block freshmart because the milk went bad")
    assert r["tier"] == 2
    d = sharma.say("arjun", "order milk from freshmart")
    assert d["purchase"]["state"] == "DENIED" and d["purchase"]["decision"]["code"] == "MERCHANT_BLOCKED"
    assert "milk went bad" in d["speech"]
    u = sharma.say("arjun", "unblock freshmart")
    assert u["tier"] == 3
    assert "blocked because the milk went bad" in sharma.approval(u["approval_id"])["diff_text"]


def test_conflicting_owner_rules_stricter_wins(sharma):
    r = sharma.say("arjun", "Allow milk, 300 a month")
    sharma.decide(r["approval_id"], "arjun", "approve")
    for who in ("priya", "arjun"):
        assert any(n["kind"] == "conflict" and "stricter limits apply" in n["text"] for n in sharma.notifications(who))
    first = sharma.say("arjun", "order 2 milk")["purchase"]
    assert first["state"] == "RESERVED"
    second = sharma.say("arjun", "order 2 milk")["purchase"]
    assert second["state"] == "ESCALATED" and second["decision"]["code"] == "MONTHLY_LIMIT"
    assert second["decision"]["details"]["stricter_rule_applied"] is True


def test_member_leaves_open_escalations_close(sharma):
    p = sharma.say("riya", "Order a PS5")["purchase"]
    r = sharma.say("priya", "Remove Riya from the household")
    assert "Removed Riya" in r["speech"]
    assert sharma.purchase(p["purchase_id"])["state"] == "DENIED"
    assert not [a for a in sharma.approvals() if a["subject_id"] == p["purchase_id"]]
    again = sharma.say("riya", "buy a book")["purchase"]
    assert again["decision"]["code"] == "UNKNOWN_SPEAKER"  # no longer resolvable as a member


def test_privacy_only_audio_hash_and_transcript_kept(sharma):
    sharma.say("dadi", "how much is left", audio_sha256="ab" * 32)
    ev = sharma.events("voice.utterance")[-1]["payload"]
    assert ev["audio_sha256"] == "ab" * 32 and ev["transcript"] == "how much is left"
    assert not any(k for k in ev if "audio" in k and k != "audio_sha256")


def test_malformed_llm_output_retry_once_then_fail_closed(sharma):
    r = sharma.say("arjun", "order 2 milk", llm_fault=1)
    assert r["parse_attempts"] == 2 and r["purchase"]["state"] == "RESERVED"
    r = sharma.say("arjun", "order 2 milk", llm_fault=2)
    assert r["failed_closed"] and "say it again" in r["speech"]
    assert sharma.events("parser.failed_closed")


def test_bitemporal_what_rule_applied_when(sharma):
    before = sharma.http.get(f"/households/{sharma.hh}/mandates/as-of",
                             params={"valid": __import__("datetime").datetime.now().astimezone().isoformat()}).json()
    r = sharma.say("priya", "raise the grocery budget to 6000")
    sharma.decide(r["approval_id"], "priya", "approve")
    deadline = time.time() + 15
    while time.time() < deadline and sharma.rule()["doc"]["limits"]["per_month_inr"] != 6000:
        time.sleep(0.5)
    import datetime as dt
    t_before = min(x["valid_from"] for x in before if x["doc"]["label"] == "groceries")
    t_before = dt.datetime.fromisoformat(t_before) + dt.timedelta(seconds=1)
    old = sharma.http.get(f"/households/{sharma.hh}/mandates/as-of", params={"valid": t_before.isoformat()}).json()
    now = sharma.http.get(f"/households/{sharma.hh}/mandates/as-of",
                          params={"valid": dt.datetime.now(dt.timezone.utc).isoformat()}).json()
    g_old = next(x for x in old if x["doc"]["label"] == "groceries")
    g_now = next(x for x in now if x["doc"]["label"] == "groceries")
    assert g_old["version"] == 1 and g_old["doc"]["limits"]["per_month_inr"] == 4000
    assert g_now["version"] == 2 and g_now["doc"]["limits"]["per_month_inr"] == 6000
