"""Pure unit tests for core/ (no I/O)."""
from datetime import datetime, timedelta, timezone

import pytest

from core.budget.reservations import IST, month_key, used_in_month
from core.ledger.hashchain import GENESIS, compute_hash, event_body, verify_chain
from core.mandates import signing
from core.mandates.diff import diff, inr
from core.mandates.schema import validate_doc
from core.policy import engine, tiers
from core.policy.engine import PurchaseContext, evaluate
from core.saga import states as S
from core.saga.idempotency import new_ulid, request_id
from memory.rhythm.burn import burn_rate, forecast
from memory.temporal.bitemporal import as_of, counterfactual

NOW = datetime(2026, 10, 12, 11, 0, tzinfo=IST)


def mandate(**kw):
    d = {"mandate_id": "mdt_1", "household_id": "hh_sharma", "label": "groceries", "owner": "priya",
         "scope": {"categories": ["milk", "atta", "diabetic_biscuits"]},
         "limits": {"per_month_inr": 4000, "per_order_inr": 1500}, "merchants": {"block": [], "block_reason": {}},
         "members_allowed": ["priya", "arjun", "dadi"], "valid_from": "2026-10-01T00:00:00+05:30", "valid_to": None,
         "paused": [], "version": 1}
    d.update(kw)
    return validate_doc(d)


def ctx(**kw):
    base = dict(member="dadi", role="adult", merchant="dailymart", now=NOW, total=26500,
                items=[{"sku": "b", "name": "Biscuits", "category": "diabetic_biscuits", "qty": 2, "unit_price": 12000}],
                baselines={"b": 2}, known_merchants={"dailymart"}, has_history=True)
    base.update(kw)
    return PurchaseContext(**base)


# ---------------------------------------------------------------- policy
def test_allow_inside_mandate():
    d = evaluate(ctx(), [mandate()], {}, None)
    assert d.outcome == engine.ALLOW and d.mandate_id == "mdt_1"


def test_frozen_denies_everything():
    d = evaluate(ctx(), [mandate()], {}, {"by": "dadi", "at": NOW.isoformat()})
    assert d.outcome == engine.DENY and d.code == "FROZEN"


def test_out_of_scope_escalates_and_member_not_allowed_escalates():
    ps5 = [{"sku": "ps5", "name": "PS5", "category": "electronics", "qty": 1, "unit_price": 5499000}]
    assert evaluate(ctx(member="riya", items=ps5), [mandate()], {}, None).code == "OUT_OF_SCOPE"
    assert evaluate(ctx(member="riya"), [mandate()], {}, None).code == "MEMBER_NOT_ALLOWED"


def test_limits_per_order_and_month():
    assert evaluate(ctx(total=160000), [mandate()], {}, None).code == "PER_ORDER_LIMIT"
    assert evaluate(ctx(), [mandate()], {"mdt_1": 390000}, None).code == "MONTHLY_LIMIT"
    assert evaluate(ctx(), [mandate()], {"mdt_1": 373500}, None).outcome == engine.ALLOW  # exactly at the limit


def test_owner_approval_overrides_limits_but_not_freeze_or_block():
    assert evaluate(ctx(total=160000, owner_approved=True), [mandate()], {}, None).outcome == engine.ALLOW
    blocked = mandate(merchants={"block": ["dailymart"], "block_reason": {"dailymart": "milk went bad"}})
    d = evaluate(ctx(owner_approved=True), [blocked], {}, None)
    assert d.outcome == engine.DENY and "milk went bad" in d.reason


def test_pause_by_category_and_dates():
    m = mandate(paused=[{"from": "2026-10-10", "to": "2026-10-15", "categories": ["diabetic_biscuits"]}])
    d = evaluate(ctx(), [m], {}, None)
    assert d.code == "PAUSED" and "Oct 15" in d.reason
    milk = [{"sku": "m", "name": "Milk", "category": "milk", "qty": 2, "unit_price": 6800}]
    assert evaluate(ctx(items=milk, baselines={"m": 2}), [m], {}, None).outcome == engine.ALLOW
    assert evaluate(ctx(now=NOW + timedelta(days=4)), [m], {}, None).outcome == engine.ALLOW


def test_expired_mandate_is_not_active():
    m = mandate(valid_to="2026-10-05T00:00:00+05:30")
    assert evaluate(ctx(), [m], {}, None).code == "OUT_OF_SCOPE"


def test_anomalies_and_unknown_speaker_need_confirmation():
    assert evaluate(ctx(member=None, role=None), [mandate()], {}, None).code == "UNKNOWN_SPEAKER"
    big = [{"sku": "b", "name": "Biscuits", "category": "diabetic_biscuits", "qty": 9, "unit_price": 12000}]
    assert evaluate(ctx(items=big, total=110500), [mandate()], {}, None).code == "QUANTITY_ANOMALY"
    assert evaluate(ctx(items=big, total=110500, acknowledged=True), [mandate()], {}, None).outcome == engine.ALLOW
    assert evaluate(ctx(merchant="newshop"), [mandate()], {}, None).code == "NEW_MERCHANT"
    night = NOW.replace(hour=2)
    assert evaluate(ctx(now=night), [mandate()], {}, None).code == "ODD_HOUR"


def test_conflicting_mandates_stricter_wins():
    arjun_milk = mandate(mandate_id="mdt_2", owner="arjun", label="milk", scope={"categories": ["milk"]},
                         limits={"per_month_inr": 300, "per_order_inr": 300}, members_allowed=["arjun"])
    milk = [{"sku": "m", "name": "Milk", "category": "milk", "qty": 2, "unit_price": 6800}]
    d = evaluate(ctx(member="arjun", items=milk, total=16100, baselines={"m": 2}), [mandate(), arjun_milk],
                 {"mdt_1": 0, "mdt_2": 16100}, None)
    assert d.code == "MONTHLY_LIMIT" and d.details["effective_limits"]["per_month_inr"] == 300


def test_missing_inputs_fail_closed():
    assert evaluate(ctx(total=None), [mandate()], {}, None).outcome == engine.ESCALATE
    assert evaluate(ctx(items=[]), [mandate()], {}, None).outcome == engine.ESCALATE


def test_tiers():
    assert tiers.classify("query_budget") == tiers.READ
    assert tiers.classify("purchase") == tiers.SPEND
    assert tiers.classify("freeze") == tiers.TIGHTEN
    assert tiers.classify("unfreeze") == tiers.LOOSEN
    assert tiers.classify("change_limit", "loosen") == tiers.LOOSEN
    assert tiers.classify("change_limit", "tighten") == tiers.TIGHTEN


# ---------------------------------------------------------------- mandates
def test_schema_requires_numeric_limits():
    with pytest.raises(Exception):
        mandate(limits={"per_month_inr": None, "per_order_inr": 100})
    with pytest.raises(Exception):
        mandate(limits={"per_month_inr": 100, "per_order_inr": 500})


def test_diff_direction_and_text():
    old = mandate()
    up = {**old, "limits": {"per_month_inr": 5000, "per_order_inr": 1500}, "members_allowed": old["members_allowed"] + ["riya"]}
    d = diff(old, up)
    assert d["direction"] == "loosen" and d["text"] == "Groceries limit ₹4,000 → ₹5,000 a month · adds Riya"
    down = {**old, "limits": {"per_month_inr": 3000, "per_order_inr": 1500}}
    assert diff(old, down)["direction"] == "tighten"
    paused = {**old, "paused": [{"from": "2026-11-01", "to": "2026-11-15", "categories": None}]}
    assert diff(old, paused)["direction"] == "tighten" and diff(paused, old)["direction"] == "loosen"
    assert inr(5000000) == "₹50,00,000"


def test_signatures_der_and_raw():
    k = signing.generate_key()
    stmt = {"approval_id": "a", "decision": "approve"}
    sig = signing.sign(k, stmt)
    assert signing.verify(signing.public_pem(k), stmt, sig)
    assert not signing.verify(signing.public_pem(k), {**stmt, "decision": "reject"}, sig)
    assert not signing.verify(signing.public_pem(signing.generate_key()), stmt, sig)
    # WebCrypto-style raw r||s
    import base64
    from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature
    r, s = decode_dss_signature(base64.b64decode(sig))
    raw = base64.b64encode(r.to_bytes(32, "big") + s.to_bytes(32, "big")).decode()
    assert signing.verify(signing.public_pem(k), stmt, raw)


# ---------------------------------------------------------------- ledger
def chain(n):
    prev, out = GENESIS, []
    for i in range(1, n + 1):
        e = {"seq": i, "household_id": "hh", "type": "t", "payload": {"i": i, "₹": "x"}, "actor": "a",
             "recorded_at": "2026-10-01T00:00:00Z", "valid_at": "2026-10-01T00:00:00Z", "prev_hash": prev}
        e["hash"] = compute_hash(prev, event_body(e))
        prev = e["hash"]
        out.append(e)
    return out


def test_hash_chain_verifies_and_detects_edits():
    events = chain(5)
    assert verify_chain(events)["ok"]
    events[2]["payload"]["i"] = 99
    res = verify_chain(events)
    assert not res["ok"] and res["broken_at"] == 3
    events = chain(5)
    del events[1]
    assert verify_chain(events)["broken_at"] == 3


# ---------------------------------------------------------------- saga
def test_idempotency_keys_are_pure():
    assert request_id("p1", "capture") == request_id("p1", "capture")
    assert request_id("p1", "capture") != request_id("p1", "create")
    a, b = new_ulid(), new_ulid()
    assert len(a) == 26 and a != b


def test_state_machine_is_monotonic():
    assert S.can_transition(S.ORDER_CREATED, S.CAPTURED)
    assert not S.can_transition(S.CONFIRMED, S.CAPTURED)
    assert not S.can_transition(S.DENIED, S.CHECKED)
    with pytest.raises(S.IllegalTransition):
        S.check_transition(S.CAPTURED, S.RESERVED)


def test_month_boundary_uses_reservation_time():
    late = datetime(2026, 10, 31, 23, 59, tzinfo=IST)
    assert month_key(late) == "2026-10"
    assert month_key(late.astimezone(timezone.utc)) == "2026-10"  # 18:29 UTC still October in IST
    rs = [{"mandate_id": "m", "month_key": "2026-10", "status": "CONVERTED", "amount": 100},
          {"mandate_id": "m", "month_key": "2026-11", "status": "CONVERTED", "amount": 50},
          {"mandate_id": "m", "month_key": "2026-10", "status": "RELEASED", "amount": 70},
          {"mandate_id": "m", "month_key": "2026-10", "status": "ACTIVE", "amount": 30, "refunded": 0}]
    assert used_in_month(rs, "m", "2026-10") == 130


# ---------------------------------------------------------------- memory
def test_bitemporal_as_of():
    t0, t1, t2 = (datetime(2026, 10, d, tzinfo=timezone.utc) for d in (1, 10, 11))
    rows = [
        {"mandate_id": "m", "version": 1, "doc": {"v": 1}, "valid_from": t0, "valid_to": None, "recorded_from": t0, "recorded_to": t2},
        {"mandate_id": "m", "version": 1, "doc": {"v": 1}, "valid_from": t0, "valid_to": t1, "recorded_from": t2, "recorded_to": None},
        {"mandate_id": "m", "version": 2, "doc": {"v": 2}, "valid_from": t1, "valid_to": None, "recorded_from": t2, "recorded_to": None},
    ]
    later = t2 + timedelta(days=1)
    assert as_of(rows, t0 + timedelta(days=2), later)[0]["version"] == 1      # what applied on Oct 3
    assert as_of(rows, later, later)[0]["version"] == 2                       # what applies now
    assert as_of(rows, t1 + timedelta(hours=1), t1 + timedelta(hours=2))[0]["version"] == 1  # what we believed then


def test_counterfactual_replay():
    ps = [{"purchase_id": str(i), "at": datetime(2026, 9, i + 1, tzinfo=IST), "total": 100000,
           "categories": ["milk"], "summary": "x"} for i in range(5)]
    res = counterfactual(ps, {"milk"}, 3000)
    assert len(res["would_escalate"]) == 2 and "Sep 4, Sep 5" in res["speech"]


def test_rhythm_burn_rate_outliers_and_feedback():
    t = datetime(2026, 9, 1, tzinfo=IST)
    orders = [{"at": t + timedelta(days=2 * i), "qty": 2} for i in range(10)]
    r = burn_rate(orders, "milk", 4)
    assert r["source"] == "history" and abs(r["days_per_unit"] - 1.0) < 1e-6
    spiky = orders + [{"at": orders[-1]["at"] + timedelta(hours=6), "qty": 20}]  # festival bulk-buy
    assert abs(burn_rate(spiky, "milk", 4)["days_per_unit"] - 1.0) < 1e-6
    assert burn_rate(orders[:1], "milk", 4)["source"] == "prior"
    now = orders[-1]["at"] + timedelta(hours=12)
    f = forecast(orders, "milk", 4, now)
    assert f["usual_qty"] == 2 and abs(f["days_left"] - 1.5) < 0.01
    f2 = forecast(orders, "milk", 4, now, feedback=[{"at": now, "kind": "out"}])
    assert f2["days_left"] == 0 and f2["note"]
