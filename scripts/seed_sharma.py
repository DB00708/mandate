"""Seed the demo household `hh_sharma` through the real API (idempotent).

  docker compose run --rm tests python scripts/seed_sharma.py

Creates Priya/Arjun/Dadi/Riya, registers a CLI passkey for each owner (private keys in
.seed_keys/, git-ignored), imports last month's orders, and creates + passkey-approves the
grocery rule and Riya's book allowance."""
import os
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
from core.mandates import signing  # noqa: E402
from tests.family import MEMBERS, Family, last_month_history  # noqa: E402

KEYS = pathlib.Path(__file__).resolve().parents[1] / ".seed_keys"
TOKEN = {"x-onboarding-token": os.getenv("ONBOARDING_TOKEN", "sharma-onboarding-dev")}


def main():
    fam = Family("hh_sharma")
    if fam.http.get("/households/hh_sharma").status_code == 200:
        print("hh_sharma already exists; nothing to do. Open http://localhost:8000")
        return
    fam.http.post("/households", headers=TOKEN, json={"household_id": "hh_sharma", "name": "The Sharma family",
                                                      "members": MEMBERS}).raise_for_status()
    KEYS.mkdir(exist_ok=True)
    for owner in ("priya", "arjun"):
        k = signing.generate_key()
        (KEYS / f"{owner}.pem").write_text(signing.private_pem(k))
        r = fam.http.post("/households/hh_sharma/devices", headers=TOKEN,
                          json={"member_id": owner, "label": f"{owner}'s phone (CLI)", "public_key": signing.public_pem(k)})
        r.raise_for_status()
        fam.keys[owner], fam.devices[owner] = k, r.json()["device_id"]
    fam.http.post("/households/hh_sharma/import-history", headers=TOKEN, json=last_month_history()).raise_for_status()
    r = fam.say("priya", "Keep the house stocked with milk, atta and Dadi's diabetic biscuits, 4000 a month, "
                         "1500 per order, for Priya, Arjun and Dadi")
    fam.decide(r["approval_id"], "priya", "approve")
    r = fam.say("priya", "Allow Riya to buy books, 500 a month")
    fam.decide(r["approval_id"], "priya", "approve")
    for rule in fam.constitution()["rules"]:
        print("✔", rule["sentence"])
    print("Seeded hh_sharma. Open http://localhost:8000")


if __name__ == "__main__":
    main()
