import pytest

from app import auth, models
from app.db import SessionLocal
from tests.conftest import TEST_EMAIL, TEST_PASSWORD


def test_passwords_are_salted_scrypt_hashes():
    a, b = auth.hash_password("correct horse battery"), auth.hash_password("correct horse battery")
    assert a != b and a.startswith("scrypt$")
    assert auth.verify_password("correct horse battery", a)
    assert not auth.verify_password("wrong password!", a)
    assert not auth.verify_password("x", "not-a-hash")


def test_every_route_but_health_and_sign_in_needs_a_session(anon):
    assert anon.get("/healthz").status_code == 200
    for method, path in [("get", "/jobs"), ("get", "/config"), ("get", "/targets"), ("post", "/sources"),
                         ("post", "/jobs/x/execute"), ("post", "/jobs/x/rollback"), ("patch", "/mappings/x")]:
        assert getattr(anon, method)(path).status_code == 401, path
    assert anon.get("/auth/me").status_code == 401


def test_sign_in_sets_a_safe_cookie_and_sign_out_revokes_it(anon):
    r = anon.post("/auth/login", json={"email": "  Reviewer@Example.com ", "password": TEST_PASSWORD})
    assert r.status_code == 200 and r.json() == {"email": TEST_EMAIL}
    cookie = r.headers["set-cookie"].lower()
    assert "httponly" in cookie and "samesite=lax" in cookie
    token = anon.cookies.get(auth.COOKIE)
    with SessionLocal() as db:   # only the token's hash is stored
        assert db.get(models.AuthSession, token) is None
        assert db.get(models.AuthSession, auth._token_hash(token)) is not None

    assert anon.get("/auth/me").json() == {"email": TEST_EMAIL}
    assert anon.post("/auth/logout").status_code == 204
    anon.cookies.set(auth.COOKIE, token)          # replaying the old cookie no longer works
    assert anon.get("/jobs").status_code == 401


def test_wrong_password_and_unknown_email_get_the_same_answer(anon):
    a = anon.post("/auth/login", json={"email": TEST_EMAIL, "password": "nope nope nope"})
    b = anon.post("/auth/login", json={"email": "nobody@example.com", "password": "nope nope nope"})
    assert a.status_code == b.status_code == 401 and a.json() == b.json()


def test_repeated_failures_are_throttled(anon):
    for _ in range(auth.LoginThrottle.FREE_ATTEMPTS):
        r = anon.post("/auth/login", json={"email": TEST_EMAIL, "password": "wrong guess!"})
        assert r.status_code == 401
    r = anon.post("/auth/login", json={"email": TEST_EMAIL, "password": TEST_PASSWORD})
    assert r.status_code == 429 and int(r.headers["retry-after"]) > 0   # even the right password waits


def test_throttle_window_expires():
    now = [0.0]
    t = auth.LoginThrottle(clock=lambda: now[0])
    for _ in range(t.FREE_ATTEMPTS):
        t.failed("a@x.com")
    assert t.retry_after("a@x.com") > 0 and t.retry_after("b@x.com") == 0
    now[0] += t.WINDOW_S
    assert t.retry_after("a@x.com") == 0


def test_expired_sessions_are_refused(client):
    with SessionLocal() as db:
        for s in db.query(models.AuthSession):
            s.expires_at = s.created_at
        db.commit()
    assert client.get("/jobs").status_code == 401


def test_audit_log_records_the_signed_in_user(client):
    csv = "Email,Name\na@x.com,A\n"
    sid = client.post("/sources", files={"file": ("e.csv", csv.encode(), "text/csv")}).json()["id"]
    job = client.post("/jobs", json={"source_id": sid}).json()
    name = next(m for m in job["mappings"] if m["source_column"] == "Name")
    # a client can no longer claim to be someone else
    client.patch(f"/mappings/{name['id']}", json={"status": "rejected", "actor": "someone-else"})
    with SessionLocal() as db:
        entry = db.query(models.AuditLog).filter_by(object_id=name["id"]).one()
    assert entry.actor == TEST_EMAIL


def test_create_user_rules():
    with SessionLocal() as db:
        with pytest.raises(ValueError, match="at least"):
            auth.create_user(db, "short@example.com", "short")
        with pytest.raises(ValueError, match="already has an account"):
            auth.create_user(db, TEST_EMAIL.upper(), "another long password")


def test_requests_carry_an_id_back(client):
    r = client.get("/jobs", headers={"X-Request-ID": "abc-123"})
    assert r.headers["x-request-id"] == "abc-123"
    replaced = client.get("/jobs", headers={"X-Request-ID": "bad id with spaces"}).headers["x-request-id"]
    assert len(replaced) == 32
