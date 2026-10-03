"""The Sharma family, onboarded through the real API.

Priya (owner) and Arjun (owner) each have a phone holding a P-256 passkey; Dadi is an
adult; Riya is a minor. September's order history is imported so memory, anomaly
baselines, rhythm and counterfactual replay have something to learn from."""
import os
import random
import string
import time
from datetime import datetime, timedelta, timezone

import httpx

from core.mandates import signing

API = os.getenv("MANDATE_API_URL", "http://localhost:8000")
PAYPAL = os.getenv("PAYPAL_MOCK_URL", "http://localhost:8002")
TOKEN = os.getenv("ONBOARDING_TOKEN", "sharma-onboarding-dev")
IST = timezone(timedelta(hours=5, minutes=30))

MEMBERS = [
    {"member_id": "priya", "name": "Priya Sharma", "role": "owner"},
    {"member_id": "arjun", "name": "Arjun Sharma", "role": "owner"},
    {"member_id": "dadi", "name": "Kamla Sharma (Dadi)", "role": "adult"},
    {"member_id": "riya", "name": "Riya Sharma", "role": "minor"},
]


def last_month_history(now: datetime | None = None) -> list[dict]:
    """Milk 2L every 2 days, atta 10kg twice, Dadi's biscuits weekly — all last month."""
    now = (now or datetime.now(IST)).astimezone(IST)
    first = now.replace(day=1, hour=9, minute=0, second=0, microsecond=0)
    start = (first - timedelta(days=1)).replace(day=1)
    out = []
    d = start
    while d < first:
        day = (d - start).days
        if day % 2 == 0:
            out.append({"sku": "milk_amul_1l", "qty": 2, "at": d.isoformat(), "requested_by": "priya"})
        if day in (2, 17):
            out.append({"sku": "atta_aashirvaad_10kg", "qty": 1, "at": (d + timedelta(hours=1)).isoformat(),
                        "requested_by": "arjun"})
        if day % 7 == 3:
            out.append({"sku": "biscuits_sugarfree_marie", "qty": 2, "at": (d + timedelta(hours=2)).isoformat(),
                        "requested_by": "dadi"})
        d += timedelta(days=1)
    return out


class Family:
    def __init__(self, household_id: str | None = None, api: str = API):
        suffix = "".join(random.choices(string.ascii_lowercase + string.digits, k=8))
        self.hh = household_id or f"hh_sharma_{suffix}"
        self.http = httpx.Client(base_url=api, timeout=30)
        self.keys = {}
        self.devices = {}

    # ------------------------------------------------------------ onboarding
    def onboard(self, history: bool = True, grocery_month: int = 4000, grocery_order: int = 1500,
                riya_books: bool = True) -> "Family":
        h = {"x-onboarding-token": TOKEN}
        r = self.http.post("/households", headers=h, json={"household_id": self.hh, "name": "The Sharma family",
                                                           "members": MEMBERS})
        r.raise_for_status()
        for owner in ("priya", "arjun"):
            k = signing.generate_key()
            self.keys[owner] = k
            r = self.http.post(f"/households/{self.hh}/devices", headers=h,
                               json={"member_id": owner, "label": f"{owner}'s phone", "public_key": signing.public_pem(k)})
            r.raise_for_status()
            self.devices[owner] = r.json()["device_id"]
        if history:
            self.http.post(f"/households/{self.hh}/import-history", headers=h,
                           json=last_month_history()).raise_for_status()
        r = self.say("priya", f"Keep the house stocked with milk, atta and Dadi's diabetic biscuits, "
                              f"{grocery_month} a month, {grocery_order} per order, for Priya, Arjun and Dadi")
        assert r.get("approval_id"), r
        assert self.decide(r["approval_id"], "priya", "approve")["status"] == "APPLIED"
        if riya_books:
            r = self.say("priya", "Allow Riya to buy books, 500 a month")
            assert r.get("approval_id"), r
            assert self.decide(r["approval_id"], "priya", "approve")["status"] == "APPLIED"
        return self

    # ------------------------------------------------------------ actions
    def say(self, speaker: str | None, text: str, score: int = 95, **kw) -> dict:
        r = self.http.post(f"/households/{self.hh}/voice",
                           json={"text": text, "speaker": speaker, "speaker_score": score, **kw})
        r.raise_for_status()
        return r.json()

    def approvals(self) -> list[dict]:
        return self.http.get(f"/households/{self.hh}/approvals").json()

    def approval(self, approval_id: str) -> dict:
        for a in self.http.get(f"/households/{self.hh}/approvals", params={"all": True}).json():
            if a["approval_id"] == approval_id:
                return a
        raise KeyError(approval_id)

    def sign(self, approval_id: str, owner: str, decision: str, key=None) -> str:
        a = self.approval(approval_id)
        return signing.sign(key or self.keys[owner], a["sign"][decision])

    def decide(self, approval_id: str, owner: str, decision: str, report: bool = False, key=None,
               device: str | None = None, expect: int = 200) -> dict:
        sig = self.sign(approval_id, owner, decision, key)
        r = self.http.post(f"/approvals/{approval_id}/decision",
                           json={"device_id": device or self.devices[owner], "decision": decision, "signature": sig,
                                 "report": report})
        assert r.status_code == expect, r.text
        return r.json()

    def purchase(self, pid: str) -> dict:
        return self.http.get(f"/purchases/{pid}").json()

    def wait_state(self, pid: str, states, timeout: float = 25) -> dict:
        states = {states} if isinstance(states, str) else set(states)
        deadline = time.time() + timeout
        p = None
        while time.time() < deadline:
            p = self.purchase(pid)
            if p["state"] in states:
                return p
            time.sleep(0.3)
        raise AssertionError(f"purchase {pid} stuck in {p and p['state']} (wanted {states}): {p}")

    def constitution(self) -> dict:
        return self.http.get(f"/households/{self.hh}/constitution").json()

    def rule(self, label: str = "groceries") -> dict:
        return next(r for r in self.constitution()["rules"] if r["label"] == label)

    def notifications(self, audience: str | None = None) -> list[dict]:
        return self.http.get(f"/households/{self.hh}/notifications", params={"audience": audience} if audience else {}).json()

    def spoken(self) -> list[str]:
        return [n["text"] for n in self.notifications("speaker")]

    def ledger_ok(self) -> bool:
        return self.http.get(f"/households/{self.hh}/ledger/verify").json()["ok"]

    def events(self, type_prefix: str = "") -> list[dict]:
        return [e for e in self.http.get(f"/households/{self.hh}/ledger", params={"limit": 100000}).json()
                if e["type"].startswith(type_prefix)]

    def paypal_stats(self, pid: str) -> dict:
        return httpx.get(f"{PAYPAL}/_stats/{pid}").json()

    def fault(self, **f):
        httpx.post(f"{PAYPAL}/_faults", json={"household_id": self.hh, **f}).raise_for_status()

    def test_hook(self, path: str, json: dict | None = None):
        r = self.http.post(f"/_test/{path}", json=json or {})
        r.raise_for_status()
        return r.json()
