"""MCP server (Streamable HTTP) — the Alexa+ entry point.

Every tool is a thin proxy to the Mandate API, so an assistant gets exactly the same
powers as a voice command: it can propose, never authorize."""
import os

import httpx
from mcp.server.fastmcp import FastMCP

API = os.getenv("MANDATE_API_URL", "http://api:8000")
mcp = FastMCP("mandate", host="0.0.0.0", port=int(os.getenv("MCP_PORT", "8001")), stateless_http=True,
              json_response=True)


def _voice(household_id: str, speaker: str, text: str, speaker_score: int = 90, purchase_id: str | None = None):
    r = httpx.post(f"{API}/households/{household_id}/voice", timeout=30,
                   json={"text": text, "speaker": speaker, "speaker_score": speaker_score, "via": "mcp",
                         "purchase_id": purchase_id})
    r.raise_for_status()
    return r.json()


@mcp.tool()
def voice_command(household_id: str, speaker: str, text: str, speaker_score: int = 90) -> dict:
    """Send any household voice command (spend, ask, tighten, propose a rule change)."""
    return _voice(household_id, speaker, text, speaker_score)


@mcp.tool()
def create_mandate(household_id: str, speaker: str, description: str) -> dict:
    """Propose a new spending rule, e.g. 'keep the house stocked with milk and atta, 4000 a month'.
    It only takes effect after an owner approves it with their passkey."""
    return _voice(household_id, speaker, description)


@mcp.tool()
def request_purchase(household_id: str, speaker: str, request: str, purchase_id: str | None = None) -> dict:
    """Ask to buy something ('my biscuits are almost over'). The policy engine decides."""
    return _voice(household_id, speaker, request, purchase_id=purchase_id)


@mcp.tool()
def explain(household_id: str, purchase_id: str | None = None) -> dict:
    """Why did Mandate buy (or refuse) that? Returns the rule, who set it, and the hash-chain proof."""
    r = httpx.get(f"{API}/households/{household_id}/explain", params={"purchase_id": purchase_id} if purchase_id else {})
    r.raise_for_status()
    return r.json()


@mcp.tool()
def pause_mandate(household_id: str, speaker: str, what: str) -> dict:
    """Pause items or a rule ('Dadi's biscuits until Nov 15'). Tightening works by voice."""
    return _voice(household_id, speaker, f"pause {what}")


@mcp.tool()
def revoke(household_id: str, speaker: str, member: str) -> dict:
    """Remove a member from the household (owner only); their open requests close."""
    return _voice(household_id, speaker, f"remove {member} from the household")


@mcp.tool()
def freeze_all(household_id: str, speaker: str) -> dict:
    """Panic phrase: freeze all spending immediately. Unfreezing needs an owner passkey."""
    return _voice(household_id, speaker, "freeze all spending")


@mcp.tool()
def whatif(household_id: str, limit_inr: int, mandate: str = "groceries", period: str = "last_month") -> dict:
    """Counterfactual replay: what if the budget had been limit_inr?"""
    r = httpx.get(f"{API}/households/{household_id}/whatif",
                  params={"limit_inr": limit_inr, "mandate": mandate, "period": period})
    r.raise_for_status()
    return r.json()


@mcp.tool()
def constitution(household_id: str) -> dict:
    """Every active spending rule in plain language with what's left this month."""
    r = httpx.get(f"{API}/households/{household_id}/constitution")
    r.raise_for_status()
    return r.json()


if __name__ == "__main__":
    mcp.run(transport="streamable-http")
