"""End-to-end check of the import against a real HubSpot TEST account.

Runs the whole BridgeOps flow in-process on a throwaway SQLite database:
upload -> map -> dry run -> import -> re-run (must write nothing) -> rollback,
then confirms in HubSpot that every step did what it reported.

It only touches contacts with emails ending @bridgeops-check.example, and the
rollback at the end archives every contact it created.

    cp .env.example .env              # set HUBSPOT_TOKEN (test account private app token)
    python scripts/live_import_check.py

Use a developer TEST account, never a production portal.
"""

import os
import sys
import tempfile
import time
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
_tmp = tempfile.mkdtemp(prefix="bridgeops-live-")
os.environ["DATABASE_URL"] = f"sqlite:///{_tmp}/live.db"
os.environ["UPLOAD_DIR"] = f"{_tmp}/uploads"
os.environ.setdefault("DECISION_PROVIDER", "rules")
os.chdir(ROOT)                                  # so .env at the repo root is picked up
sys.path.insert(0, str(ROOT / "api"))

from fastapi.testclient import TestClient  # noqa: E402

from app.config import get_settings  # noqa: E402
from app.hubspot import HubSpotClient  # noqa: E402
from app.main import app  # noqa: E402

RUN = uuid.uuid4().hex[:6]
DOMAIN = "bridgeops-check.example"


def email(name: str) -> str:
    return f"{name}.{RUN}@{DOMAIN}"


def check(ok: bool, what: str) -> None:
    print(f"  {'PASS' if ok else 'FAIL'}  {what}")
    if not ok:
        raise SystemExit(1)


def main() -> None:
    settings = get_settings()
    if not settings.hubspot_token:
        sys.exit("Set HUBSPOT_TOKEN in .env first (private app token from a HubSpot TEST account).")
    hs = HubSpotClient(settings.hubspot_token, settings.hubspot_base_url)

    print(f"Run {RUN}: seeding one existing contact")
    hs.upsert_by_email({email("existing"): {"firstname": "Existing", "lastname": "Contact",
                                            "lifecyclestage": "lead"}})

    csv = (
        "Email,First Name,Last Name,Company,Lifecycle Stage\n"
        f"{email('existing')},Existing,Contact,Acme,Customer\n"
        f"{email('new1')},New,One,Globex,Lead\n"
        f"{email('new1').upper()},New,One,Globex Inc,Lead\n"
        f"{email('new2')},New,Two,,Subscriber\n"
        "not-an-email,Bad,Row,Initech,Lead\n"
    )

    with TestClient(app) as c:
        sid = c.post("/sources", files={"file": ("live.csv", csv.encode(), "text/csv")}).json()["id"]
        job = c.post("/jobs", json={"source_id": sid, "provider": "rules"}).json()
        jid = job["id"]
        print(f"\nJob {jid}: {job['counts']}")
        check(job["counts"]["columns_pending"] == 0 and job["counts"]["values_pending"] == 0,
              "every column and value mapped by rules")

        print("\nDry run")
        dry = c.post(f"/jobs/{jid}/dry-run").json()
        print(f"  {dry['counts']}  (definitions: {dry['definitions']})")
        check(dry["definitions"] == "hubspot", "validated against live HubSpot property definitions")
        check(dry["counts"].get("would_create") == 2 and dry["counts"].get("would_update") == 1,
              "2 to create, 1 to update")
        check(hs.read_by_email([email("new1")], ["email"]) == {}, "dry run wrote nothing")

        print("\nImport")
        t = time.perf_counter()
        r = c.post(f"/jobs/{jid}/execute").json()
        print(f"  {r['counts']}  ({time.perf_counter() - t:.1f} s, {r['requests_sent']} requests, "
              f"{r['retries']} retries)")
        check(r["counts"].get("created") == 2 and r["counts"].get("updated") == 1, "2 created, 1 updated")
        check(r["counts"].get("merged") == 1 and r["counts"].get("invalid") == 1,
              "duplicate merged, bad email not sent")
        live = hs.read_by_email([email("existing"), email("new1"), email("new2")],
                                ["firstname", "company", "lifecyclestage"])
        check(len(live) == 3, "all three contacts exist in HubSpot")
        check(live[email("existing")]["properties"]["lifecyclestage"] == "customer",
              "existing contact moved lead -> customer")
        check(live[email("new1")]["properties"]["company"] == "Globex", "merged row kept the first value")

        print("\nRe-run")
        again = c.post(f"/jobs/{jid}/execute").json()
        check(again["requests_sent"] <= 1, f"re-run wrote nothing ({again['requests_sent']} requests)")

        print("\nRollback")
        rb = c.post(f"/jobs/{jid}/rollback").json()
        print(f"  {rb['counts']}")
        after = hs.read_by_email([email("existing"), email("new1"), email("new2")], ["lifecyclestage"])
        check(email("new1") not in after and email("new2") not in after, "created contacts archived")
        check(after[email("existing")]["properties"]["lifecyclestage"] == "lead",
              "existing contact restored to lead (cleared, then set back)")

    hs.archive([after[email("existing")]["id"]])
    print("\nAll checks passed. Test contacts cleaned up.")


if __name__ == "__main__":
    main()
