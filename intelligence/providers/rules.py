"""Deterministic, offline 'model': a rule-based speech-to-proposal translator.

It is the default provider so the whole system runs and tests without cloud keys.
It sits in the untrusted zone exactly like an LLM would: its output is validated
against the Proposal schema and only ever treated as a claim."""
import json
import re
from datetime import date, timedelta

from intelligence.providers.base import Provider

CATEGORY_WORDS = [
    ("diabetic_biscuits", r"(?:diabetic\s+|sugar[- ]free\s+)?biscuits?|cookies"),
    ("milk", r"milk"),
    ("atta", r"atta|flour"),
    ("rice", r"rice|chawal"),
    ("vegetables", r"vegetables?|veggies|sabzi"),
    ("books", r"books?|novels?"),
    ("electronics", r"ps5|playstation|phone|laptop|headphones|television|tv\b|ipad"),
    ("toys", r"toys?|lego"),
]
GROUP_WORDS = {
    "groceries": r"grocer(?:y|ies)|shopping|household|kitchen|house",
    "books": r"books?|reading",
}
UNITS = {"zero": 0, "one": 1, "a": 1, "an": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7,
         "eight": 8, "nine": 9, "ten": 10, "eleven": 11, "twelve": 12, "fifteen": 15, "twenty": 20,
         "thirty": 30, "forty": 40, "fifty": 50, "sixty": 60, "seventy": 70, "eighty": 80, "ninety": 90,
         "hundred": 100}
SCALES = {"hundred": 100, "thousand": 1000, "lakh": 100000, "lakhs": 100000}
MONTHS = {m: i for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], start=1)}


def words_to_number(text: str) -> int | None:
    total, current, seen = 0, 0, False
    for w in re.findall(r"[a-z]+", text):
        if w in ("and",):
            continue
        if w in SCALES and seen:
            if SCALES[w] == 100:
                current = max(current, 1) * 100
            else:
                total += max(current, 1) * SCALES[w]
                current = 0
        elif w in UNITS and w not in ("a", "an", "hundred"):
            current += UNITS[w]
            seen = True
        elif seen:
            break
    return total + current if seen else None


def money(text: str) -> int | None:
    """First monetary amount in the text, in rupees."""
    m = re.search(r"(?:₹|rs\.?|inr|rupees)?\s*(\d[\d,]*(?:\.\d+)?)\s*(k|thousand|lakh|lakhs)?\b", text)
    if m:
        n = float(m.group(1).replace(",", ""))
        mult = {"k": 1000, "thousand": 1000, "lakh": 100000, "lakhs": 100000}.get(m.group(2) or "", 1)
        return int(n * mult)
    for m in re.finditer(r"\b((?:(?:one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|fifteen|twenty|"
                         r"thirty|forty|fifty|sixty|seventy|eighty|ninety|hundred|thousand|lakh|lakhs|and)\b[\s-]*)+)",
                         text):
        n = words_to_number(m.group(1))
        if n:
            return n
    return None


def parse_date(text: str, today: date) -> tuple[str | None, str | None]:
    """Return (start, until) ISO dates mentioned in the text."""
    start, until = None, None
    rng = re.search(r"from\s+([a-z]+)\s+(\d{1,2})(?:st|nd|rd|th)?\s+(?:to|until|till)\s+([a-z]+)?\s*(\d{1,2})", text)
    if rng:
        m1 = MONTHS.get(rng.group(1)[:3])
        m2 = MONTHS.get((rng.group(3) or rng.group(1))[:3])
        if m1 and m2:
            y1 = today.year + (m1 < today.month and today.month - m1 > 6)
            start = date(y1, m1, int(rng.group(2))).isoformat()
            until = date(y1 + (m2 < m1), m2, int(rng.group(4))).isoformat()
            return start, until
    m = re.search(r"(?:until|till|through)\s+([a-z]+)\s+(\d{1,2})", text)
    if m and m.group(1)[:3] in MONTHS:
        mo = MONTHS[m.group(1)[:3]]
        y = today.year + (mo < today.month)
        until = date(y, mo, int(m.group(2))).isoformat()
    m = re.search(r"for\s+(\w+)\s+(day|week)s?", text)
    if m:
        n = int(m.group(1)) if m.group(1).isdigit() else (words_to_number(m.group(1)) or 1)
        until = (today + timedelta(days=n * (7 if m.group(2) == "week" else 1) - 1)).isoformat()
    return start, until


def find_items(text: str) -> list[dict]:
    items = []
    for cat, pat in CATEGORY_WORDS:
        m = re.search(rf"(?:\b(\d+|{'|'.join(k for k in UNITS if k not in ('hundred',))})\s+(?:(kg|kilos?|litres?|liters?|l|packets?|packs?|units?|bottles?)\s+(?:of\s+)?)?)?(?:\w+'s\s+)?(?:{pat})\b", text)
        if m:
            qty = None
            if m.group(1):
                qty = int(m.group(1)) if m.group(1).isdigit() else UNITS.get(m.group(1))
                if cat == "atta" and (m.group(2) or "").startswith(("kg", "kilo")):
                    qty = max(1, -(-qty // 10))  # 10 kg packs
            items.append({"category": cat, "qty": qty, "product_hint": m.group(0)})
    return items


def find_members(text: str, members: list[str]) -> list[str]:
    return [m for m in members if re.search(rf"\b{re.escape(m)}(?:'s)?\b", text)]


def find_group(text: str) -> str | None:
    for g, pat in GROUP_WORDS.items():
        if re.search(rf"\b(?:{pat})\b", text):
            return g
    return None


def parse(text: str, today: date, members: list[str]) -> dict:
    t = " " + text.lower().strip().replace("’", "'") + " "
    t = re.sub(r"^\s*(?:hey\s+)?alexa[,]?\s*", " ", t)
    words = t.split()
    items = find_items(t)
    named = find_members(t, members)
    cats = [i["category"] for i in items]
    p: dict = {"intent": "unknown", "confidence": 0.9}

    if re.match(r"^\s*(yes|yeah|yep|go ahead|confirm|ok|okay|haan|sure|do it)\b", t) and len(words) <= 5:
        return {"intent": "confirm"}
    if re.match(r"^\s*(no|nope|cancel|don't|stop|never ?mind)\b", t) and len(words) <= 4:
        return {"intent": "cancel"}
    if "unfreeze" in t:
        return {"intent": "unfreeze"}
    if re.search(r"\bfreeze\b", t):
        return {"intent": "freeze"}
    if "what if" in t:
        p = {"intent": "whatif", "amount_inr": money(t), "mandate_hint": find_group(t) or "groceries",
             "period": "this_month" if "this month" in t else "last_month", "categories": cats}
        return p
    if re.search(r"\bwhy\b", t):
        return {"intent": "explain", "categories": cats}
    if re.search(r"how much|what'?s left|remaining|budget left|spent", t) and not re.search(r"raise|increase|set|lower", t):
        return {"intent": "query_budget", "mandate_hint": find_group(t)}
    if re.search(r"(already|we'?re|we are|completely)\s+(out of|finished)|ran out of|run out of", t) and cats:
        return {"intent": "feedback", "feedback_kind": "out", "categories": cats}
    if re.search(r"still (have|got) (plenty|lots|enough)|plenty of", t) and cats:
        return {"intent": "feedback", "feedback_kind": "plenty", "categories": cats}

    merchant = re.search(r"\b(?:un)?block\s+([a-z0-9_-]+)", t)
    if re.search(r"\bunblock\b", t) and merchant:
        return {"intent": "unblock_merchant", "merchant": merchant.group(1)}
    if re.search(r"\bblock\b", t) and merchant:
        reason = re.search(r"(?:because|since|as)\s+(.+?)\s*$", t)
        return {"intent": "block_merchant", "merchant": merchant.group(1),
                "reason": reason.group(1).strip(" .") if reason else None}

    start, until = parse_date(t, today)
    if re.search(r"\b(resume|unpause|restart)\b", t):
        return {"intent": "unpause", "categories": cats, "mandate_hint": find_group(t)}
    if re.search(r"\b(pause|hold off|stop buying)\b", t):
        return {"intent": "pause", "categories": cats, "mandate_hint": find_group(t) if not cats else None,
                "start": start, "until": until, "members": named}

    if re.search(r"\bremove\b", t) and named and re.search(r"household|family|home|house", t):
        return {"intent": "remove_member", "target_member": named[0]}
    m = re.search(r"\badd\s+(\w+)\s+to\s+(?:the\s+)?(.+)", t)
    if m and m.group(1) in members:
        return {"intent": "add_member", "target_member": m.group(1),
                "mandate_hint": find_group(m.group(2)) or (cats[0] if cats else None), "categories": cats}

    amount = money(t)
    if re.search(r"\b(raise|increase|lower|reduce|decrease|set|change|make|bump|cut)\b", t) and re.search(r"limit|budget|cap", t):
        return {"intent": "change_limit", "amount_inr": amount, "mandate_hint": find_group(t) or (cats[0] if cats else None),
                "limit_kind": "per_order" if re.search(r"per order|each order|single order", t) else "per_month",
                "categories": cats}

    if re.search(r"keep .*stocked|stocked with|create a (rule|mandate)|set up a (rule|mandate)|new rule|allow\b|"
                 r"let (\w+ )?(buy|order)|allowance|budget for|can spend", t) and cats:
        per_order = None
        po = re.search(r"(?:per|each|an?)\s+order\s+(?:of\s+|up to\s+|max\s+)?(?:₹|rs\.?)?\s*(\d[\d,]*)", t) or \
            re.search(r"(?:₹|rs\.?)?\s*(\d[\d,]*)\s+(?:per|each|an?)\s+order", t)
        if po:
            per_order = int(po.group(1).replace(",", ""))
        monthly = re.search(r"(?:₹|rs\.?)?\s*(\d[\d,]*\s*k?)\s*(?:rupees\s+)?(?:a|per|each)\s+month", t)
        amt = money(monthly.group(1)) if monthly else (amount if amount != per_order else None)
        return {"intent": "create_mandate", "categories": cats, "amount_inr": amt, "per_order_inr": per_order,
                "members": named, "until": until, "mandate_hint": find_group(t),
                "auto_reorder": bool(re.search(r"auto|automatically|reorder", t))}

    if re.search(r"\b(the usual|usual order|same as last time)\b", t):
        return {"intent": "purchase", "usual": True}
    if cats and re.search(r"\b(order|buy|get|need|want|almost over|almost finished|running (out|low)|finished|over|"
                          r"out of|purchase|reorder)\b", t):
        frm = re.search(r"\bfrom\s+([a-z0-9_-]+)", t)
        return {"intent": "purchase", "items": items, "merchant": frm.group(1) if frm else None}
    return {"intent": "unknown", "confidence": 0.2}


class RulesProvider(Provider):
    name = "rules"

    def complete(self, system: str, user: str) -> str:
        req = json.loads(user)
        return json.dumps(parse(req["text"], date.fromisoformat(req["today"]), req.get("members", [])))
