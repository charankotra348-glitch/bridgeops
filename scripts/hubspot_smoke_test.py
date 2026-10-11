"""
BridgeOps - HubSpot smoke test (Day 1)

Proves you can talk to the target CRM:
  1. Lists contact property definitions (you'll validate against these later)
  2. Creates ONE test contact (or finds it if it already exists, so re-runs are safe)
  3. Reads it back
  4. Optionally archives it again (--cleanup)

Setup:
  1. Create a free HubSpot developer account, then a developer TEST account inside it.
  2. In the test account: Settings -> Integrations -> Private Apps (or Legacy Apps) -> Create,
     with scopes crm.objects.contacts.read, crm.objects.contacts.write, crm.schemas.contacts.read
  3. Copy the access token, then:
       pip install requests
       export HUBSPOT_TOKEN=pat-xxx           (Windows: set HUBSPOT_TOKEN=...)
       python hubspot_smoke_test.py            (add --cleanup to archive the test contact)
"""

import os
import sys
import time

import requests

BASE = "https://api.hubapi.com"
TOKEN = os.environ.get("HUBSPOT_TOKEN")
TEST_EMAIL = "bridgeops.smoketest@example.com"


def hs(method, path, **kwargs):
    headers = {"Authorization": f"Bearer {TOKEN}", "Content-Type": "application/json"}
    start = time.perf_counter()
    resp = requests.request(method, BASE + path, headers=headers, timeout=30, **kwargs)
    ms = (time.perf_counter() - start) * 1000
    if resp.status_code >= 400:
        raise RuntimeError(f"{method} {path} -> HTTP {resp.status_code}: {resp.text[:500]}")
    return (resp.json() if resp.content else {}), ms


def main():
    if not TOKEN:
        sys.exit("Set HUBSPOT_TOKEN first (private app access token from your test account).")

    # 1) Property definitions
    props, ms = hs("GET", "/crm/v3/properties/contacts")
    names = {p["name"]: p for p in props.get("results", [])}
    print(f"1) {len(names)} contact properties ({ms:.0f} ms)")
    stage = names.get("lifecyclestage")
    if stage:
        options = [o["value"] for o in stage.get("options", [])]
        print(f"   lifecyclestage options: {options}")

    # 2) Find-or-create (idempotent: safe to run twice)
    search_body = {"filterGroups": [{"filters": [
        {"propertyName": "email", "operator": "EQ", "value": TEST_EMAIL}]}]}
    found, ms = hs("POST", "/crm/v3/objects/contacts/search", json=search_body)
    if found.get("total", 0) > 0:
        contact_id = found["results"][0]["id"]
        print(f"2) Contact already exists, id={contact_id} ({ms:.0f} ms) - no duplicate created")
    else:
        body = {"properties": {
            "email": TEST_EMAIL,
            "firstname": "Smoke",
            "lastname": "Test",
            "company": "BridgeOps",
            "lifecyclestage": "lead",
        }}
        created, ms = hs("POST", "/crm/v3/objects/contacts", json=body)
        contact_id = created["id"]
        print(f"2) Created contact id={contact_id} ({ms:.0f} ms)")

    # 3) Read back
    got, ms = hs("GET", f"/crm/v3/objects/contacts/{contact_id}",
                 params={"properties": "email,firstname,lastname,company,lifecyclestage"})
    print(f"3) Read back ({ms:.0f} ms): {got.get('properties')}")

    # 4) Optional cleanup
    if "--cleanup" in sys.argv:
        _, ms = hs("DELETE", f"/crm/v3/objects/contacts/{contact_id}")
        print(f"4) Archived contact {contact_id} ({ms:.0f} ms)")

    print("\nHubSpot connection works.")


if __name__ == "__main__":
    main()
