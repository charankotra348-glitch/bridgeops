import pytest

from app import main
from tests.fake_hubspot import FakeHubSpot

CSV = (
    "Email,First Name,Last Name,Company,Lifecycle Stage\n"
    "priya@acme.com,Priya,Rao,Acme,Customer\n"          # 0 exists in HubSpot as a lead -> updated
    "PRIYA@acme.com,Priya,Rao,Acme Inc,Customer\n"      # 1 same email as row 0 -> merged into it
    "jordan@contoso.com,Jordan,Lee,Contoso,Lead\n"      # 2 exists with these exact values -> unchanged
    "sam@globex.com,Sam,Okafor,,Lead\n"                 # 3 new -> created
    "not-an-email,Mia,Chen,Globex,Lead\n"               # 4 invalid email -> not sent
)


@pytest.fixture
def hubspot(client):
    hs = FakeHubSpot()
    hs.add(email="priya@acme.com", firstname="Priya", lastname="Rao", company="Acme",
           lifecyclestage="lead")
    hs.add(email="jordan@contoso.com", firstname="Jordan", lastname="Lee", company="Contoso",
           lifecyclestage="lead")
    main.app.dependency_overrides[main.get_hubspot] = lambda: hs.client()
    main._definitions_cache.clear()
    yield hs
    main.app.dependency_overrides.pop(main.get_hubspot, None)


def make_job(client, csv=CSV):
    sid = client.post("/sources", files={"file": ("e.csv", csv.encode(), "text/csv")}).json()["id"]
    job = client.post("/jobs", json={"source_id": sid}).json()
    assert job["counts"]["columns_pending"] == 0 and job["counts"]["values_pending"] == 0, job["counts"]
    return job


def test_dry_run_without_token_uses_built_in_definitions(client):
    job = make_job(client)
    r = client.post(f"/jobs/{job['id']}/dry-run").json()
    assert r["dry_run"] and r["definitions"] == "built-in" and r["blockers"] == []
    by_row = {x["row"]: x for x in r["records"]}
    assert by_row[0]["action"] == "would_upsert" and by_row[0]["merged_rows"] == [1]
    assert by_row[1]["action"] == "merged" and by_row[1]["merged_into"] == 0
    assert by_row[4]["action"] == "invalid" and "invalid email" in by_row[4]["errors"][0]
    assert by_row[3]["properties"] == {"email": "sam@globex.com", "firstname": "Sam",
                                       "lastname": "Okafor", "lifecyclestage": "lead"}
    assert client.post(f"/jobs/{job['id']}/execute").status_code == 503   # no token: dry runs only


def test_execute_refuses_while_review_is_pending(client, hubspot):
    csv = "Email,First Name,Last Name,Company\na@x.com,Li,Wang,Initech\nb@y.com,Li,Wang,Initech\n"
    job = client.post("/jobs", json={"source_id": client.post(
        "/sources", files={"file": ("e.csv", csv.encode(), "text/csv")}).json()["id"]}).json()
    r = client.post(f"/jobs/{job['id']}/execute")
    assert r.status_code == 409 and "duplicate group" in r.json()["detail"]["blockers"][0]
    assert not any(c.endswith("/batch/upsert") for c in hubspot.calls)

    dry = client.post(f"/jobs/{job['id']}/dry-run").json()
    assert dry["blockers"] and dry["definitions"] == "hubspot"


def test_import_rerun_and_rollback(client, hubspot):
    job = make_job(client)

    dry = client.post(f"/jobs/{job['id']}/dry-run").json()
    assert dry["counts"] == {"rows": 5, "merged": 1, "invalid": 1, "would_update": 1,
                             "would_skip_unchanged": 1, "would_create": 1}
    assert not any(c.endswith("/batch/upsert") for c in hubspot.calls)         # dry run never writes

    r = client.post(f"/jobs/{job['id']}/execute")
    assert r.status_code == 200, r.text
    assert r.json()["counts"] == {"invalid": 1, "merged": 1, "updated": 1, "unchanged": 1, "created": 1}
    priya, sam = hubspot.by_email("priya@acme.com"), hubspot.by_email("sam@globex.com")
    assert priya["lifecyclestage"] == "customer" and sam["firstname"] == "Sam"
    assert client.get(f"/jobs/{job['id']}").json()["status"] == "executed_with_errors"  # the invalid row

    recs = {x["row"]: x for x in client.get(f"/jobs/{job['id']}/records").json()}
    assert recs[0]["before"]["lifecyclestage"] == "lead" and recs[3]["before"] is None
    assert recs[4]["status"] == "invalid" and "invalid email" in recs[4]["error"]

    # Re-running changes nothing and sends no writes
    upserts = hubspot.calls.count("/crm/v3/objects/contacts/batch/upsert")
    again = client.post(f"/jobs/{job['id']}/execute").json()
    assert again["counts"] == r.json()["counts"]
    assert hubspot.calls.count("/crm/v3/objects/contacts/batch/upsert") == upserts
    assert len(hubspot.contacts) == 3

    rb = client.post(f"/jobs/{job['id']}/rollback")
    assert rb.status_code == 200, rb.text
    assert hubspot.by_email("sam@globex.com") is None                           # created -> archived
    assert hubspot.by_email("priya@acme.com")["lifecyclestage"] == "lead"       # restored, moving back
    assert hubspot.by_email("jordan@contoso.com") is not None                   # untouched
    assert rb.json()["counts"]["rolled_back"] == 2
    assert client.post(f"/jobs/{job['id']}/rollback").status_code == 409       # only once


def test_rollback_leaves_contacts_changed_since_the_import(client, hubspot):
    job = make_job(client)
    client.post(f"/jobs/{job['id']}/execute")
    sam_id = hubspot.by_email("sam@globex.com")["id"]
    hubspot.contacts[sam_id]["firstname"] = "Samuel"                            # someone edited it

    rb = client.post(f"/jobs/{job['id']}/rollback").json()
    assert rb["counts"]["rollback_conflict"] == 1
    assert hubspot.by_email("sam@globex.com")["firstname"] == "Samuel"          # not archived
    rec = next(x for x in client.get(f"/jobs/{job['id']}/records").json() if x["row"] == 3)
    assert rec["status"] == "rollback_conflict"


def test_failed_rows_are_retried_on_the_next_run(client, hubspot):
    job = make_job(client)
    # The batch of 2 is rejected, then each record alone is rejected: both fail this run
    hubspot.path_failures["/batch/upsert"] = [400, 400, 400]
    first = client.post(f"/jobs/{job['id']}/execute").json()
    assert first["counts"]["failed"] == 2 and hubspot.by_email("sam@globex.com") is None

    second = client.post(f"/jobs/{job['id']}/execute").json()
    assert "failed" not in second["counts"] and second["counts"]["created"] == 1
    assert hubspot.by_email("sam@globex.com") is not None


def test_transient_hubspot_outage_returns_502_and_job_can_run_again(client, hubspot):
    job = make_job(client)
    hubspot.failures = [503] * 10
    assert client.post(f"/jobs/{job['id']}/execute").status_code == 502
    assert client.get(f"/jobs/{job['id']}").json()["status"] == "mapped"
    hubspot.failures = []
    assert client.post(f"/jobs/{job['id']}/execute").status_code == 200


def test_review_is_locked_after_the_import(client, hubspot):
    job = make_job(client)
    client.post(f"/jobs/{job['id']}/execute")
    mapping_id = client.get(f"/jobs/{job['id']}").json()["mappings"][0]["id"]
    assert client.patch(f"/mappings/{mapping_id}", json={"status": "rejected"}).status_code == 409
