"""Preflight for live runs: is every real service reachable with the configured credentials?
Prints what works and what's missing; never prints a secret.

    docker compose run --rm tests python scripts/live_check.py"""
import asyncio
import json
import os
import sys
import time
from datetime import date

import httpx

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

OK, NO, SKIP = "PASS", "FAIL", "----"
results = []


def check(name):
    def wrap(fn):
        t = time.time()
        try:
            status, detail = fn()
        except Exception as e:  # report, keep going
            status, detail = NO, f"{type(e).__name__}: {str(e)[:300]}"
        results.append((status, name, detail, time.time() - t))
        return fn
    return wrap


@check("Bedrock (Amazon) parser call")
def _bedrock():
    if not (os.getenv("AWS_ACCESS_KEY_ID") or os.getenv("AWS_PROFILE")):
        return SKIP, "no AWS credentials (AWS_ACCESS_KEY_ID/AWS_SECRET_ACCESS_KEY)"
    from intelligence.parser.parser import parse
    from intelligence.providers.bedrock import BedrockProvider
    prov = BedrockProvider()
    prop, attempts = parse("Freeze all spending", date.today(), ["priya"], prov)
    return (OK if prop.intent == "freeze" else NO), f"{prov.model} in {prov.region}: intent={prop.intent} attempts={attempts}"


@check("Nebius Token Factory (NVIDIA Nemotron) parser call")
def _nebius():
    if not os.getenv("NEBIUS_API_KEY"):
        return SKIP, "NEBIUS_API_KEY not set"
    from intelligence.parser.parser import parse
    from intelligence.providers.nemotron_nebius import NebiusProvider
    prov = NebiusProvider()
    r = httpx.get(f"{prov.base}/models", headers={"Authorization": f"Bearer {prov.key}"}, timeout=20)
    r.raise_for_status()
    models = sorted(m["id"] for m in r.json().get("data", []))
    nemotron = [m for m in models if "nemotron" in m.lower()]
    if prov.model not in models:
        return NO, f"model {prov.model} not offered; Nemotron models here: {nemotron}"
    prop, attempts = parse("Freeze all spending", date.today(), ["priya"], prov)
    return (OK if prop.intent == "freeze" else NO), f"{prov.model}: intent={prop.intent} attempts={attempts}"


@check("PayPal sandbox OAuth + order")
def _paypal():
    if os.getenv("PAYPAL_MODE") != "sandbox":
        return SKIP, "PAYPAL_MODE is not sandbox"
    if not (os.getenv("PAYPAL_CLIENT_ID") and os.getenv("PAYPAL_CLIENT_SECRET")):
        return NO, "PAYPAL_CLIENT_ID / PAYPAL_CLIENT_SECRET not set"
    from adapters.paypal.client import PayPalClient, approve_link
    from core.saga.idempotency import new_ulid, request_id
    pp = PayPalClient()
    pp._auth()
    pid = new_ulid()
    o = pp.create_order(request_id(pid, "create"), pid, "hh_live_check", 26500, "Mandate live check")
    how = "vaulted payer: autonomous capture" if pp.vault_id else f"buyer approval needed ({bool(approve_link(o))})"
    return OK, f"order {o['id']} {o['status']} in {pp.currency}; {how}"


@check("Langfuse (self-hosted) auth")
def _langfuse():
    if os.getenv("LANGFUSE_TRACING") != "1":
        return SKIP, "LANGFUSE_TRACING is off"
    base = os.getenv("LANGFUSE_BASE_URL", "http://langfuse-web:3000")
    r = httpx.get(f"{base}/api/public/projects", auth=(os.getenv("LANGFUSE_PUBLIC_KEY", ""),
                                                      os.getenv("LANGFUSE_SECRET_KEY", "")), timeout=10)
    r.raise_for_status()
    return OK, f"{base}: project {[p['name'] for p in r.json().get('data', [])]}"


@check("MCP server protocol (Alexa+ needs 2025-11-25)")
def _mcp():
    from mcp import ClientSession
    from mcp.client.streamable_http import streamablehttp_client

    async def run():
        async with streamablehttp_client(os.getenv("MCP_URL", "http://mcp:8001/mcp")) as (read, write, _):
            async with ClientSession(read, write) as s:
                return (await s.initialize()).protocolVersion
    v = asyncio.run(run())
    return (OK if v >= "2025-11-25" else NO), f"negotiated {v}"


@check("Mandate API")
def _api():
    h = httpx.get(f"{os.getenv('MANDATE_API_URL', 'http://api:8000')}/health", timeout=5).json()
    return OK, json.dumps(h)


width = max(len(n) for _, n, _, _ in results)
for status, name, detail, secs in results:
    print(f"[{status}] {name.ljust(width)}  {detail}  ({secs:.1f}s)")
sys.exit(1 if any(s == NO for s, *_ in results) else 0)
