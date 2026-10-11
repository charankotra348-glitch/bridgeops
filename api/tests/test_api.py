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
