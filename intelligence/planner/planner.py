"""The quarantined planner.

It is the ONLY component that reads merchant text (titles, descriptions, reviews),
which is untrusted and may contain prompt injection. Its powers are deliberately tiny:
  - input: proposed items + catalog listings
  - output: a CartProposal of (sku, qty) pairs from one merchant
It cannot see credentials, cannot call payment code, and cannot set prices: the trusted
zone re-prices every line from the catalog and the policy engine decides.

The rule-based planner below simulates a *gullible* model on purpose: if a listing says
"buy N units", it obeys. The red-team suite proves that this changes nothing about what
money can move, because the policy engine still caps quantity and total."""
import re

from pydantic import BaseModel, Field


class CartLine(BaseModel):
    sku: str
    qty: int = Field(ge=1, le=1000)


class CartProposal(BaseModel):
    merchant: str
    lines: list[CartLine] = Field(min_length=1)
    notes: list[str] = []


INJECTION = re.compile(r"buy\s+(\d+)\s+units", re.I)


def plan(items: list[dict], listings: list[dict], usual_qty: dict | None = None,
         merchant: str | None = None) -> CartProposal | None:
    """items: [{category, qty, product_hint}] ; listings: [{sku, merchant, category, title, description, rating}]."""
    usual_qty = usual_qty or {}
    chosen, notes = [], []
    for item in items:
        options = [l for l in listings if l["category"] == item["category"]]
        if not options:
            return None
        if merchant and any(l["merchant"] == merchant for l in options):
            options = [l for l in options if l["merchant"] == merchant]
        best = sorted(options, key=lambda l: (-l.get("rating", 0), l["sku"]))[0]
        qty = item.get("qty") or usual_qty.get(best["sku"]) or 1
        m = INJECTION.search(best.get("description", ""))
        if m:  # the planner "fell for" text inside a merchant listing
            qty = int(m.group(1))
            notes.append(f"listing {best['sku']} instructed quantity {qty}")
        chosen.append((best, qty))
    merchants = {b["merchant"] for b, _ in chosen}
    merchant = sorted(merchants)[0]
    lines = [CartLine(sku=b["sku"], qty=q) for b, q in chosen if b["merchant"] == merchant]
    if len(merchants) > 1:
        notes.append("items from other merchants dropped: " + ", ".join(sorted(merchants - {merchant})))
    return CartProposal(merchant=merchant, lines=lines, notes=notes)
