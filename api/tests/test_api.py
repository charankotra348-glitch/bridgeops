from tests.conftest import MESSY_CSV


def upload(client, content=MESSY_CSV, name="export.csv"):
    return client.post("/sources", files={"file": (name, content.encode(), "text/csv")})


def test_health(client):
    assert client.get("/healthz").json() == {"status": "ok"}


def test_upload_and_profile(client):
    r = upload(client)
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["row_count"] == 5
    assert {c["name"] for c in body["columns"]} >= {"cust_mail", "acct_status"}
    assert client.get(f"/sources/{body['id']}").status_code == 200


def test_upload_rejects_bad_type_and_content(client):
    exe = {"file": ("x.exe", b"MZ", "application/octet-stream")}
    assert client.post("/sources", files=exe).status_code == 415
    assert upload(client, content="a,b\n").status_code == 422


def test_job_review_and_human_approval(client):
    source_id = upload(client).json()["id"]
    job = client.post("/jobs", json={"source_id": source_id}).json()
    assert job["provider"] == "rules"
    assert job["counts"]["columns"] == 9
    assert job["counts"]["columns_auto"] >= 6

    review = client.get(f"/jobs/{job['id']}/review").json()
    pending_cols = {m["source_column"] for m in review}
    assert "internal_ref" in pending_cols and "acct_status" in pending_cols

    # Human maps the remaining lifecycle values
    status_col = next(m for m in review if m["source_column"] == "acct_status")
    r = client.patch(f"/mappings/{status_col['id']}", json={
        "status": "approved", "value_map": {"Active-ish": "customer", "hot prospect": "opportunity"}})
    assert r.status_code == 200, r.text
    assert r.json()["value_map"]["Active-ish"]["method"] == "human"

    # Invalid option is refused
    bad = client.patch(f"/mappings/{status_col['id']}", json={"status": "approved",
                                                              "value_map": {"Active": "vip"}})
    assert bad.status_code == 422

    review_after = {m["source_column"] for m in client.get(f"/jobs/{job['id']}/review").json()}
    assert "acct_status" not in review_after


def test_unknown_source_and_job(client):
    assert client.post("/jobs", json={"source_id": "nope"}).status_code == 404
    assert client.get("/jobs/nope").status_code == 404


DUP_CSV = (
    "Email,First Name,Last Name,Phone,Company\n"
    "priya@acme.com,Priya,Rao,631-555-0142,Acme\n"
    "PRIYA@acme.com,Priya,Rao,,Acme\n"
    "jlee@contoso.com,Jordan,Lee,212-555-0199,Contoso\n"
    "jordan.lee@gmail.com,Jordan,Lee,,Contoso\n"
)


def test_duplicates_flagged_and_reviewed(client):
    source_id = upload(client, content=DUP_CSV).json()["id"]
    job = client.post("/jobs", json={"source_id": source_id}).json()
    assert job["counts"]["duplicate_groups_auto"] == 1      # same email, any case
    assert job["counts"]["duplicate_groups_pending"] == 1   # similar name: rules-only sends it to review

    groups = client.get(f"/jobs/{job['id']}/duplicates").json()
    by_rows = {tuple(g["rows"]): g for g in groups}
    assert by_rows[(0, 1)]["method"] == "rule" and by_rows[(0, 1)]["status"] == "auto"
    pending = by_rows[(2, 3)]
    assert pending["status"] == "pending" and pending["records"][1]["Email"] == "jordan.lee@gmail.com"
    assert [g["id"] for g in client.get(f"/jobs/{job['id']}/duplicates?pending_only=true").json()] \
        == [pending["id"]]

    assert client.patch(f"/duplicates/{pending['id']}", json={"status": "approved"}).status_code == 422
    assert client.patch(f"/duplicates/{pending['id']}",
                        json={"status": "approved", "keep_row": 0}).status_code == 422  # not in group
    r = client.patch(f"/duplicates/{pending['id']}", json={"status": "approved", "keep_row": 2,
                                                           "actor": "charan"})
    assert r.status_code == 200, r.text
    assert r.json()["same_person"] is True and r.json()["method"] == "human" and r.json()["keep_row"] == 2
    assert client.patch("/duplicates/nope", json={"status": "rejected"}).status_code == 404


def test_jobs_list_targets_and_config(client):
    sid = upload(client).json()["id"]
    job = client.post("/jobs", json={"source_id": sid}).json()
    listed = client.get("/jobs").json()
    assert listed[0]["id"] == job["id"] and listed[0]["filename"] == "export.csv" and listed[0]["rows"] == 5

    targets = {t["name"]: t for t in client.get("/targets").json()}
    assert "email" in targets and targets["email"]["options"] == []
    stage_options = {o["value"] for o in targets["lifecyclestage"]["options"]}
    assert {"lead", "customer", "other"} <= stage_options

    cfg = client.get("/config").json()
    assert cfg["providers"][0] == "rules" and "hubspot_configured" in cfg


def test_retargeting_a_column_to_an_enum_builds_its_value_map(client):
    csv = "Email,Phase\na@x.com,Customer\nb@x.com,kinda interested\n"
    sid = upload(client, content=csv).json()["id"]
    job = client.post("/jobs", json={"source_id": sid}).json()
    phase = next(m for m in job["mappings"] if m["source_column"] == "Phase")
    r = client.patch(f"/mappings/{phase['id']}", json={"status": "approved",
                                                         "target_property": "lifecyclestage"})
    vm = r.json()["value_map"]
    assert vm["Customer"]["target"] == "customer" and vm["Customer"]["status"] == "auto"
    assert vm["kinda interested"]["status"] == "pending" and vm["kinda interested"]["target"] is None


def test_cors_allows_the_dev_ui(client):
    r = client.options("/jobs", headers={"Origin": "http://localhost:5173",
                                         "Access-Control-Request-Method": "GET"})
    assert r.headers.get("access-control-allow-origin") == "http://localhost:5173"
