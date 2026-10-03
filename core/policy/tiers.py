"""Risk tiers: what each command needs."""
READ, SPEND, TIGHTEN, LOOSEN, OUT_OF_SCOPE = 0, 1, 2, 3, 4

TIER_NAMES = {READ: "0 · Read", SPEND: "1 · Spend inside a mandate", TIGHTEN: "2 · Tighten",
              LOOSEN: "3 · Loosen", OUT_OF_SCOPE: "4 · Out of scope"}

INTENT_TIERS = {
    "query_budget": READ, "explain": READ, "whatif": READ, "constitution": READ, "feedback": READ,
    "unknown": READ, "confirm": SPEND, "cancel": TIGHTEN,
    "purchase": SPEND,
    "pause": TIGHTEN, "freeze": TIGHTEN, "block_merchant": TIGHTEN, "remove_member": TIGHTEN,
    "unpause": LOOSEN, "unfreeze": LOOSEN, "unblock_merchant": LOOSEN, "add_member": LOOSEN,
    "create_mandate": LOOSEN,
}


def classify(intent: str, direction: str | None = None) -> int:
    """change_limit is tier 2 when lowering and tier 3 when raising."""
    if intent == "change_limit":
        return TIGHTEN if direction == "tighten" else LOOSEN
    return INTENT_TIERS.get(intent, READ)
