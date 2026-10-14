"""The red-team panel: real attacks, each blocked."""
import asyncio
import os

import psycopg
import pytest

from tests.family import Family


def test_redteam_cloned_voice(sharma):
    r = sharma.http.post(f"/households/{sharma.hh}/redteam/cloned_voice").json()
    assert r["blocked"] and r["limit_after"]["per_month_inr"] == 4000


def test_redteam_poisoned_listing(sharma):
    r = sharma.http.post(f"/households/{sharma.hh}/redteam/poisoned_listing").json()
    assert r["blocked"], r
    assert any("instructed quantity 50" in n for n in r["planner_notes"])  # the planner fell for it...
    p = r["response"]["purchase"]
    assert p["items"][0]["qty"] == 50 and p["decision"]["code"] in ("PER_ORDER_LIMIT", "QUANTITY_ANOMALY")
    assert sharma.paypal_stats(p["purchase_id"])["captures"] == 0  # ...and no money moved


def test_redteam_double_submit(sharma):
    r = sharma.http.post(f"/households/{sharma.hh}/redteam/double_submit").json()
    assert r["blocked"] and r["purchases_created"] == 1


def test_redteam_wifi_cut(sharma):
    r = sharma.http.post(f"/households/{sharma.hh}/redteam/wifi_cut").json()
    assert r["blocked"] and r["outcome_was_unknown"] and r["captures"] == 1, r
    sharma.wait_state(r["purchase_id"], "CONFIRMED", timeout=40)
    assert sharma.paypal_stats(r["purchase_id"])["captures"] == 1


def test_redteam_tamper_copy(sharma):
    r = sharma.http.post(f"/households/{sharma.hh}/redteam/tamper_ledger").json()
    assert r["blocked"] and r["verify"]["ok"] is False


def test_tampered_database_row_is_detected():
    fam = Family().onboard(riya_books=False)
    assert fam.ledger_ok()
    fam.test_hook(f"tamper/{fam.hh}/3")
    v = fam.http.get(f"/households/{fam.hh}/ledger/verify").json()
    assert v["ok"] is False and v["broken_at"] == 3


def test_events_table_is_append_only_in_postgres():
    fam = Family().onboard(history=False, riya_books=False)
    url = os.getenv("DATABASE_URL", "postgresql://mandate:mandate@localhost:5433/mandate")
    with psycopg.connect(url) as conn:
        with pytest.raises(psycopg.errors.RaiseException):
            conn.execute("UPDATE events SET actor = 'mallory' WHERE household_id = %s AND seq = 1", (fam.hh,))
        conn.rollback()
        with pytest.raises(psycopg.errors.RaiseException):
            conn.execute("DELETE FROM events WHERE household_id = %s", (fam.hh,))


def test_mcp_server_tools(sharma):
    from mcp import ClientSession
    from mcp.client.streamable_http import streamablehttp_client

    async def run():
        async with streamablehttp_client(os.getenv("MCP_URL", "http://localhost:8001/mcp")) as (read, write, _):
            async with ClientSession(read, write) as s:
                await s.initialize()
                tools = {t.name for t in (await s.list_tools()).tools}
                assert {"create_mandate", "request_purchase", "explain", "pause_mandate", "revoke", "freeze_all",
                        "whatif"} <= tools
                res = await s.call_tool("request_purchase", {"household_id": sharma.hh, "speaker": "dadi",
                                                             "request": "my biscuits are almost over"})
                return res
    res = asyncio.run(run())
    import json
    body = json.loads(res.content[0].text)
    assert body["purchase"]["state"] == "RESERVED"
    sharma.wait_state(body["purchase"]["purchase_id"], "CONFIRMED")
    ev = sharma.events("voice.utterance")[-1]
    assert ev["payload"]["via"] == "mcp"


def test_mcp_server_speaks_spec_2025_11_25():
    """Amazon's Alexa+ track requires MCP spec 2025-11-25 or later over Streamable HTTP."""
    from mcp import ClientSession
    from mcp.client.streamable_http import streamablehttp_client

    async def run():
        async with streamablehttp_client(os.getenv("MCP_URL", "http://localhost:8001/mcp")) as (read, write, _):
            async with ClientSession(read, write) as s:
                init = await s.initialize()
                tools = (await s.list_tools()).tools
                return init, tools
    init, tools = asyncio.run(run())
    assert init.protocolVersion == "2025-11-25"
    assert init.serverInfo.name == "mandate"
    by_name = {t.name: t for t in tools}
    assert by_name["explain"].annotations.readOnlyHint is True
    assert by_name["revoke"].annotations.destructiveHint is True
    assert by_name["request_purchase"].title == "Request a purchase"
