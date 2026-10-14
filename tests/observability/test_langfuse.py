"""End to end: an Alexa+-style MCP call shows up in the self-hosted Langfuse as one trace with
the parser, the quarantined planner and the policy guardrail nested under it, and the PayPal
calls in the purchase's own trace. Runs only with the observability profile up.

Langfuse v4 is observation-first ("events_only"): everything is read from /api/public/v2/observations."""
import asyncio
import json
import os
import time

import httpx
import pytest

LANGFUSE = os.getenv("LANGFUSE_BASE_URL", "http://localhost:3000")
AUTH = (os.getenv("LANGFUSE_PUBLIC_KEY", "pk-lf-mandate-local"), os.getenv("LANGFUSE_SECRET_KEY", "sk-lf-mandate-local"))

pytestmark = pytest.mark.skipif(os.getenv("LANGFUSE_TRACING") != "1", reason="LANGFUSE_TRACING is off")


def _decode(o: dict) -> dict:
    for k in ("input", "output"):
        if isinstance(o.get(k), str):
            try:
                o[k] = json.loads(o[k])
            except ValueError:
                pass
    return o


def observations(ready, timeout: float = 90, **params) -> list[dict]:
    """Poll until ingestion (async: SDK batch -> worker -> ClickHouse) has what we need."""
    deadline, last = time.time() + timeout, None
    while time.time() < deadline:
        r = httpx.get(f"{LANGFUSE}/api/public/v2/observations", auth=AUTH, timeout=10,
                      params={"limit": 100, "fields": "core,basic,io", **params})
        if r.status_code == 200:
            last = [_decode(o) for o in r.json().get("data", [])]
            if ready(last):
                return last
        else:
            last = f"HTTP {r.status_code}: {r.text[:300]}"
        time.sleep(2)
    raise AssertionError(f"Langfuse never returned what we waited for ({params}): {str(last)[:600]}")


def names(obs) -> set[str]:
    return {o["name"] for o in obs}


def test_mcp_purchase_is_traced_end_to_end(sharma):
    from langfuse import Langfuse
    from mcp import ClientSession
    from mcp.client.streamable_http import streamablehttp_client

    async def run():
        async with streamablehttp_client(os.getenv("MCP_URL", "http://localhost:8001/mcp")) as (read, write, _):
            async with ClientSession(read, write) as s:
                await s.initialize()
                return await s.call_tool("request_purchase", {"household_id": sharma.hh, "speaker": "dadi",
                                                              "request": "my biscuits are almost over"})
    body = json.loads(asyncio.run(run()).content[0].text)
    pid = body["purchase"]["purchase_id"]
    sharma.wait_state(pid, "CONFIRMED")

    root = observations(lambda d: "mcp request_purchase" in names(d), sessionId=sharma.hh, name="mcp request_purchase")[0]
    assert root["type"] == "TOOL" and root["userId"] == "dadi"

    want = {"mcp request_purchase", "mcp command", "parser", "parse attempt 1", "planner (quarantined)", "policy engine"}
    trace = observations(lambda d: want <= names(d), traceId=root["traceId"])
    by_name = {o["name"]: o for o in trace}
    # the API's work (another container) nests under the MCP tool call
    assert by_name["mcp command"]["parentObservationId"] == root["id"]
    assert by_name["mcp command"]["type"] == "AGENT"
    assert by_name["parse attempt 1"]["type"] == "GENERATION"
    assert by_name["policy engine"]["type"] == "GUARDRAIL"
    assert by_name["policy engine"]["output"]["outcome"] == "ALLOW"
    assert {o["sessionId"] for o in trace} == {sharma.hh}

    # create + capture ran in the worker at different times: one trace, keyed by the purchase id
    paypal = observations(lambda d: {"PayPal POST /v2/checkout/orders",
                                     "PayPal POST /v2/checkout/orders/{id}/capture"} <= names(d),
                          traceId=Langfuse.create_trace_id(seed=pid))
    create = next(o for o in paypal if o["name"] == "PayPal POST /v2/checkout/orders")
    assert create["type"] == "TOOL" and create["input"]["paypal_request_id"]
    assert create["input"]["body"]["purchase_units"][0]["custom_id"] == pid
    assert create["input"]["body"]["payment_source"]["paypal"]["vault_id"] == "[redacted]"  # masked before export
