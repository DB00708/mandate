"""MCP server (Streamable HTTP, MCP spec 2025-11-25) — the Alexa+ entry point.

Every tool is a thin proxy to the Mandate API, so an assistant gets exactly the same
powers as a voice command: it can propose, never authorize. Each tool call is one
Langfuse trace, and the API's handling of it nests underneath."""
import functools
import os

import httpx
from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations

from observability import tracing

API = os.getenv("MANDATE_API_URL", "http://api:8000")
mcp = FastMCP("mandate", instructions="Household spending inside signed, owner-approved rules. Tools propose; "
                                      "Mandate's policy engine decides; loosening a rule always needs an owner's passkey.",
              host="0.0.0.0", port=int(os.getenv("MCP_PORT", "8001")), stateless_http=True, json_response=True)

READ = ToolAnnotations(readOnlyHint=True, idempotentHint=True, openWorldHint=False)
SPEND = ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=False, openWorldHint=True)
TIGHTEN = ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=True, openWorldHint=False)


def traced(fn):
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        hh = kwargs.get("household_id")
        with tracing.trace(f"mcp {fn.__name__}", session_id=hh, user_id=kwargs.get("speaker") or "assistant",
                           tags=["mcp", fn.__name__], as_type="tool", input=kwargs) as t:
            out = fn(*args, **kwargs)
            t.update(output=out)
            return out
    return wrapper


def _voice(household_id: str, speaker: str, text: str, speaker_score: int = 90, purchase_id: str | None = None):
    r = httpx.post(f"{API}/households/{household_id}/voice", timeout=30, headers=tracing.outgoing_headers(),
                   json={"text": text, "speaker": speaker, "speaker_score": speaker_score, "via": "mcp",
                         "purchase_id": purchase_id})
    r.raise_for_status()
    return r.json()


def _get(path: str, params: dict | None = None):
    r = httpx.get(f"{API}{path}", params=params or {}, timeout=30, headers=tracing.outgoing_headers())
    r.raise_for_status()
    return r.json()


@mcp.tool(title="Household voice command", annotations=SPEND)
@traced
def voice_command(household_id: str, speaker: str, text: str, speaker_score: int = 90) -> dict:
    """Send any household voice command (spend, ask, tighten, propose a rule change)."""
    return _voice(household_id, speaker, text, speaker_score)


@mcp.tool(title="Propose a spending rule", annotations=TIGHTEN)
@traced
def create_mandate(household_id: str, speaker: str, description: str) -> dict:
    """Propose a new spending rule, e.g. 'keep the house stocked with milk and atta, 4000 a month'.
    It only takes effect after an owner approves it with their passkey."""
    return _voice(household_id, speaker, description)


@mcp.tool(title="Request a purchase", annotations=SPEND)
@traced
def request_purchase(household_id: str, speaker: str, request: str, purchase_id: str | None = None) -> dict:
    """Ask to buy something ('my biscuits are almost over'). The policy engine decides."""
    return _voice(household_id, speaker, request, purchase_id=purchase_id)


@mcp.tool(title="Explain a purchase", annotations=READ)
@traced
def explain(household_id: str, purchase_id: str | None = None) -> dict:
    """Why did Mandate buy (or refuse) that? Returns the rule, who set it, and the hash-chain proof."""
    return _get(f"/households/{household_id}/explain", {"purchase_id": purchase_id} if purchase_id else None)


@mcp.tool(title="Pause items or a rule", annotations=TIGHTEN)
@traced
def pause_mandate(household_id: str, speaker: str, what: str) -> dict:
    """Pause items or a rule ('Dadi's biscuits until Nov 15'). Tightening works by voice."""
    return _voice(household_id, speaker, f"pause {what}")


@mcp.tool(title="Remove a household member",
          annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=True, idempotentHint=True,
                                      openWorldHint=False))
@traced
def revoke(household_id: str, speaker: str, member: str) -> dict:
    """Remove a member from the household (owner only); their open requests close."""
    return _voice(household_id, speaker, f"remove {member} from the household")


@mcp.tool(title="Freeze all spending", annotations=TIGHTEN)
@traced
def freeze_all(household_id: str, speaker: str) -> dict:
    """Panic phrase: freeze all spending immediately. Unfreezing needs an owner passkey."""
    return _voice(household_id, speaker, "freeze all spending")


@mcp.tool(title="What-if budget replay", annotations=READ)
@traced
def whatif(household_id: str, limit_inr: int, mandate: str = "groceries", period: str = "last_month") -> dict:
    """Counterfactual replay: what if the budget had been limit_inr?"""
    return _get(f"/households/{household_id}/whatif", {"limit_inr": limit_inr, "mandate": mandate, "period": period})


@mcp.tool(title="Household spending rules", annotations=READ)
@traced
def constitution(household_id: str) -> dict:
    """Every active spending rule in plain language with what's left this month."""
    return _get(f"/households/{household_id}/constitution")


if __name__ == "__main__":
    try:
        mcp.run(transport="streamable-http")
    finally:
        tracing.flush()
