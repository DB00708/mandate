"""Tracing must never change behaviour: off by default, secrets masked when on."""
from observability import tracing


def test_tracing_is_a_noop_without_keys(monkeypatch):
    monkeypatch.setattr(tracing, "_enabled", None)
    monkeypatch.setattr(tracing, "_client", None)
    monkeypatch.delenv("LANGFUSE_PUBLIC_KEY", raising=False)
    monkeypatch.setenv("LANGFUSE_TRACING", "1")
    assert tracing.enabled() is False
    with tracing.trace("voice command", session_id="hh_x") as t:
        with tracing.observe("policy engine", as_type="guardrail") as g:
            g.update(output={"outcome": "ALLOW"})
        t.update(output="ok")
    tracing.score("tier", 1)
    tracing.generation_usage("m", {"input": 1})
    assert tracing.outgoing_headers() == {}
    assert tracing._client is None  # langfuse was never even constructed
    monkeypatch.setattr(tracing, "_enabled", None)


def test_mask_redacts_secrets_at_any_depth():
    data = {"approval": {"signature": "MEUCIQ...", "statement": "approve diff 9f2c"},
            "headers": [{"Authorization": "Bearer abc"}], "paypal_request_id": "req_1",
            "payment_source": {"paypal": {"vault_id": "8kk8451t"}}}
    out = tracing.mask(data=data)
    assert out["approval"] == {"signature": "[redacted]", "statement": "approve diff 9f2c"}
    assert out["headers"] == [{"Authorization": "[redacted]"}]
    assert out["paypal_request_id"] == "req_1"
    assert out["payment_source"]["paypal"]["vault_id"] == "[redacted]"


def test_incoming_parent_needs_both_ids():
    assert tracing.incoming_parent({}) is None
    assert tracing.incoming_parent({tracing.TRACE_HEADER: "t"}) is None
    assert tracing.incoming_parent({tracing.TRACE_HEADER: "t", tracing.PARENT_HEADER: "p"}) == \
        {"trace_id": "t", "parent_span_id": "p"}
