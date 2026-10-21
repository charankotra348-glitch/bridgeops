from fastapi.testclient import TestClient

from app import models
from app.config import Settings
from app.db import SessionLocal


def test_host_database_urls_get_the_psycopg_driver():
    for given in ("postgres://u:p@db.example.com/x?sslmode=require",
                  "postgresql://u:p@db.example.com/x?sslmode=require"):
        assert Settings(database_url=given).database_url == \
            "postgresql+psycopg://u:p@db.example.com/x?sslmode=require"
    assert Settings(database_url="sqlite:///./x.db").database_url == "sqlite:///./x.db"


def test_uploads_live_in_the_database(client):
    r = client.post("/sources", files={"file": ("e.csv", b"Email\na@x.com\n", "text/csv")})
    with SessionLocal() as db:
        source = db.get(models.Source, r.json()["id"])
        assert source.content == b"Email\na@x.com\n" and source.storage_path is None


def test_a_job_whose_file_is_gone_says_so(client):
    sid = client.post("/sources", files={"file": ("e.csv", b"Email\na@x.com\n", "text/csv")}).json()["id"]
    with SessionLocal() as db:        # an old on-disk upload whose file vanished with the disk
        source = db.get(models.Source, sid)
        source.content, source.storage_path = None, "/nonexistent/upload.csv"
        db.commit()
    r = client.post("/jobs", json={"source_id": sid})
    assert r.status_code == 410 and "upload the export again" in r.json()["detail"]


def test_security_headers(client):
    r = client.get("/healthz")
    assert r.headers["x-content-type-options"] == "nosniff"
    assert "frame-ancestors 'none'" in r.headers["content-security-policy"]


def test_serves_the_built_ui_when_configured(tmp_path):
    from fastapi import FastAPI

    from app.main import mount_web

    (tmp_path / "assets").mkdir()
    (tmp_path / "index.html").write_text("<!doctype html><title>BridgeOps</title>")
    (tmp_path / "assets" / "app.js").write_text("console.log(1)")
    web = FastAPI()
    assert mount_web(web, str(tmp_path))
    c = TestClient(web)
    assert "BridgeOps" in c.get("/").text
    assert c.get("/assets/app.js").status_code == 200
    assert not mount_web(FastAPI(), str(tmp_path / "missing"))
