"""Mandate diffs: classify a change as tighten/loosen and render a human diff card."""
from datetime import date


def inr(n: int) -> str:
    s = str(int(n))
    if len(s) > 3:  # Indian grouping: 1,23,456
        head, tail = s[:-3], s[-3:]
        parts = []
        while len(head) > 2:
            parts.insert(0, head[-2:])
            head = head[:-2]
        if head:
            parts.insert(0, head)
        s = ",".join(parts) + "," + tail
    return "₹" + s


def fmt_date(s):
    if not s:
        return None
    try:
        d = date.fromisoformat(s[:10])
        return f"{d:%b} {d.day}"
    except ValueError:
        return s


def _pause_key(p):
    return (p.get("from"), p.get("to"), tuple(p.get("categories") or ()))


def diff(old: dict | None, new: dict) -> dict:
    """Return {"direction": "tighten"|"loosen", "changes": [...], "text": str}."""
    if old is None:
        cats = ", ".join(new["scope"]["categories"])
        txt = (f"New rule '{new['label']}': {cats} · {inr(new['limits']['per_month_inr'])}/month · "
               f"{inr(new['limits']['per_order_inr'])}/order · for {', '.join(new['members_allowed'])}")
        return {"direction": "loosen", "changes": [{"field": "mandate", "op": "create", "text": txt}], "text": txt}

    changes, loosen, tighten = [], False, False
    label = new.get("label", "rule").capitalize()

    for key, name, suffix in (("per_month_inr", "limit", " a month"), ("per_order_inr", "per-order limit", "")):
        a, b = old["limits"][key], new["limits"][key]
        if a != b:
            if b > a:
                loosen = True
            else:
                tighten = True
            changes.append({"field": f"limits.{key}", "from": a, "to": b,
                            "text": f"{label} {name} {inr(a)} → {inr(b)}{suffix}"})

    for m in sorted(set(new["members_allowed"]) - set(old["members_allowed"])):
        loosen = True
        changes.append({"field": "members_allowed", "op": "add", "value": m, "text": f"adds {m.capitalize()}"})
    for m in sorted(set(old["members_allowed"]) - set(new["members_allowed"])):
        tighten = True
        changes.append({"field": "members_allowed", "op": "remove", "value": m, "text": f"removes {m.capitalize()}"})

    cat_add = sorted(set(new["scope"]["categories"]) - set(old["scope"]["categories"]))
    cat_rm = sorted(set(old["scope"]["categories"]) - set(new["scope"]["categories"]))
    if cat_add:
        loosen = True
        changes.append({"field": "scope", "op": "add", "value": cat_add,
                        "text": "adds " + ", ".join(c.replace("_", " ") for c in cat_add)})
    if cat_rm:
        tighten = True
        changes.append({"field": "scope", "op": "remove", "value": cat_rm, "text": "removes " + ", ".join(cat_rm)})

    ob, nb = set(old["merchants"]["block"]), set(new["merchants"]["block"])
    for m in sorted(nb - ob):
        tighten = True
        reason = new["merchants"].get("block_reason", {}).get(m)
        changes.append({"field": "merchants.block", "op": "add", "value": m,
                        "text": f"blocks {m}" + (f" ({reason})" if reason else "")})
    for m in sorted(ob - nb):
        loosen = True
        reason = old["merchants"].get("block_reason", {}).get(m)
        changes.append({"field": "merchants.block", "op": "remove", "value": m,
                        "text": f"unblocks {m}" + (f" — blocked because {reason}" if reason else "")})

    old_p = {_pause_key(p) for p in old.get("paused", [])}
    new_p = {_pause_key(p) for p in new.get("paused", [])}
    for p in new.get("paused", []):
        if _pause_key(p) not in old_p:
            tighten = True
            what = ", ".join(c.replace("_", " ") for c in p.get("categories") or []) or "whole rule"
            until = f" to {fmt_date(p['to'])}" if p.get("to") else " until resumed"
            changes.append({"field": "paused", "op": "add", "value": p,
                            "text": f"pauses {what} from {fmt_date(p['from'])}{until}"})
    for p in old.get("paused", []):
        if _pause_key(p) not in new_p:
            loosen = True
            what = ", ".join(c.replace("_", " ") for c in p.get("categories") or []) or "whole rule"
            changes.append({"field": "paused", "op": "remove", "value": p, "text": f"resumes {what}"})

    if old.get("valid_to") != new.get("valid_to"):
        a, b = old.get("valid_to"), new.get("valid_to")
        if b is None or (a is not None and b > a):
            loosen = True
        else:
            tighten = True
        changes.append({"field": "valid_to", "from": a, "to": b, "text": f"until {fmt_date(b) or 'no end date'}"})

    if bool(old.get("auto_reorder")) != bool(new.get("auto_reorder")):
        if new.get("auto_reorder"):
            loosen = True
        else:
            tighten = True
        changes.append({"field": "auto_reorder", "to": bool(new.get("auto_reorder")),
                        "text": "auto-reorder " + ("on" if new.get("auto_reorder") else "off")})

    direction = "loosen" if loosen else "tighten"  # mixed changes count as loosening
    return {"direction": direction, "changes": changes,
            "text": " · ".join(c["text"] for c in changes) or "no change"}
