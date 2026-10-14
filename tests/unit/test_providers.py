"""Provider request/response shapes, checked offline.

Bedrock: botocore's Stubber validates our Converse request against the real service model, so a
malformed call fails here instead of on demo day. Nebius: the OpenAI-compatible fallbacks."""
from datetime import date

import httpx
import pytest
from botocore.stub import Stubber

from intelligence.parser.parser import parse
from intelligence.providers import nemotron_nebius
from intelligence.providers.bedrock import DEFAULT_MODEL, BedrockProvider


def test_bedrock_converse_request_matches_the_service_model(monkeypatch):
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "test")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "test")
    monkeypatch.delenv("BEDROCK_MODEL_ID", raising=False)
    prov = BedrockProvider()
    assert prov.model == DEFAULT_MODEL and prov.model.startswith("us.")  # inference profile, not a bare id
    with Stubber(prov.client) as stub:
        stub.add_response("converse", {
            "output": {"message": {"role": "assistant", "content": [{"text": '{"intent": "freeze"}'}]}},
            "stopReason": "end_turn", "usage": {"inputTokens": 410, "outputTokens": 9, "totalTokens": 419},
            "metrics": {"latencyMs": 312}},
            {"modelId": DEFAULT_MODEL, "system": [{"text": "SYS"}],
             "messages": [{"role": "user", "content": [{"text": "USER"}]}],
             "inferenceConfig": {"maxTokens": 600, "temperature": 0}})
        assert prov.complete("SYS", "USER") == '{"intent": "freeze"}'
        stub.assert_no_pending_responses()


def _fake_post(responses, calls):
    def post(url, timeout, headers, json):
        calls.append(json)
        status, body = responses.pop(0)
        return httpx.Response(status, json=body, request=httpx.Request("POST", url))
    return post


def test_nebius_drops_json_mode_once_and_strips_reasoning(monkeypatch):
    monkeypatch.setenv("NEBIUS_API_KEY", "test")
    calls = []
    reply = {"choices": [{"message": {"content": '<think>user wants {freeze}</think>{"intent": "freeze"}'}}],
             "usage": {"prompt_tokens": 400, "completion_tokens": 30}}
    monkeypatch.setattr(nemotron_nebius.httpx, "post", _fake_post(
        [(400, {"error": {"message": "response_format is not supported for this model"}}), (200, reply), (200, reply)],
        calls))
    prov = nemotron_nebius.NebiusProvider()
    assert prov.base == "https://api.tokenfactory.nebius.com/v1"
    proposal, attempts = parse("freeze all spending", date(2026, 10, 12), ["priya"], prov)
    assert proposal.intent == "freeze" and attempts == 1
    assert "response_format" in calls[0] and "response_format" not in calls[1]
    parse("freeze all spending", date(2026, 10, 12), ["priya"], prov)
    assert "response_format" not in calls[2]  # remembered


def test_nebius_http_errors_fail_closed(monkeypatch):
    monkeypatch.setenv("NEBIUS_API_KEY", "test")
    monkeypatch.setattr(nemotron_nebius.httpx, "post", _fake_post(
        [(401, {"error": "bad key"}), (401, {"error": "bad key"})], []))
    from intelligence.parser.parser import ParseFailed
    with pytest.raises(ParseFailed, match="HTTP 401"):
        parse("freeze all spending", date(2026, 10, 12), ["priya"], nemotron_nebius.NebiusProvider())
