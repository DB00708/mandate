"""The only thing the parser may output: a schema-bound proposal. It is a claim
about what the speaker wants, never an authorization."""
from typing import Literal, Optional

from pydantic import BaseModel, Field

Intent = Literal[
    "purchase", "create_mandate", "change_limit", "pause", "unpause", "freeze", "unfreeze",
    "block_merchant", "unblock_merchant", "add_member", "remove_member", "query_budget", "explain",
    "whatif", "feedback", "confirm", "cancel", "unknown",
]


class ProposedItem(BaseModel):
    category: str
    qty: Optional[int] = Field(default=None, ge=1, le=1000)
    product_hint: Optional[str] = None


class Proposal(BaseModel):
    intent: Intent
    items: list[ProposedItem] = []
    usual: bool = False                     # "get the usual"
    categories: list[str] = []              # for rules
    amount_inr: Optional[int] = Field(default=None, gt=0)
    per_order_inr: Optional[int] = Field(default=None, gt=0)
    limit_kind: Literal["per_month", "per_order"] = "per_month"
    mandate_hint: Optional[str] = None      # "groceries", "books", ...
    members: list[str] = []                 # people named in the request
    target_member: Optional[str] = None
    merchant: Optional[str] = None
    reason: Optional[str] = None
    start: Optional[str] = None             # ISO date
    until: Optional[str] = None             # ISO date
    period: Literal["last_month", "this_month"] = "last_month"
    feedback_kind: Optional[Literal["out", "plenty"]] = None
    auto_reorder: Optional[bool] = None
    confidence: float = Field(default=0.9, ge=0, le=1)


SCHEMA_PROMPT = """You convert one household voice command into JSON matching this schema, and nothing else.
Fields: intent (one of purchase, create_mandate, change_limit, pause, unpause, freeze, unfreeze, block_merchant,
unblock_merchant, add_member, remove_member, query_budget, explain, whatif, feedback, confirm, cancel, unknown),
items [{category, qty, product_hint}], usual (bool), categories [str], amount_inr (int rupees, null if the
speaker gave no number), per_order_inr, limit_kind (per_month|per_order), mandate_hint, members [names],
target_member, merchant, reason, start (YYYY-MM-DD), until (YYYY-MM-DD), period (last_month|this_month),
feedback_kind (out|plenty), auto_reorder, confidence (0-1).
Categories: milk, atta, diabetic_biscuits, rice, vegetables, books, electronics, toys.
Never invent a number the speaker did not say. Output JSON only."""
