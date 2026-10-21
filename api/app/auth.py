"""Sign-in for BridgeOps.

- Passwords are hashed with scrypt (Python's standard library), with a random salt per user.
- Sessions are server-side: the browser holds a random token in an httpOnly cookie, and
  the database stores only its SHA-256. Signing out or expiry revokes it immediately,
  which a self-contained token (JWT) could not do.
- The cookie is SameSite=Lax, so other sites cannot make a signed-in browser send
  imports or rollbacks (cross-site POSTs carry no cookie). Set COOKIE_SECURE=true
  behind HTTPS.
- Repeated failed sign-ins for one email are slowed down (see LoginThrottle).
- Whoever is signed in is the actor in the audit log; clients no longer say who they are.
"""

import hashlib
import hmac
import secrets
import threading
import time
from datetime import UTC, datetime, timedelta

from fastapi import Depends, HTTPException, Request
from sqlalchemy.orm import Session

from app import models
from app.config import get_settings
from app.db import get_db

COOKIE = "bridgeops_session"
_SCRYPT = {"n": 2 ** 14, "r": 8, "p": 1, "dklen": 32}
MIN_PASSWORD = 10


# --------------------------------------------------------------------------- passwords
def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, **_SCRYPT)
    return f"scrypt${_SCRYPT['n']}${_SCRYPT['r']}${_SCRYPT['p']}${salt.hex()}${digest.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        scheme, n, r, p, salt, digest = stored.split("$")
        if scheme != "scrypt":
            return False
        got = hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt), n=int(n), r=int(r), p=int(p),
                             dklen=len(bytes.fromhex(digest)))
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(got.hex(), digest)


# Compared against when the email is unknown, so a wrong email takes as long as a wrong password
_DUMMY_HASH = hash_password(secrets.token_hex(8))


def normalize_email(email: str) -> str:
    return email.strip().lower()


def create_user(db: Session, email: str, password: str) -> models.User:
    email = normalize_email(email)
    if "@" not in email:
        raise ValueError("enter an email address")
    if len(password) < MIN_PASSWORD:
        raise ValueError(f"password must be at least {MIN_PASSWORD} characters")
    if db.query(models.User).filter_by(email=email).first():
        raise ValueError(f"{email} already has an account")
    user = models.User(email=email, password_hash=hash_password(password))
    db.add(user)
    db.commit()
    return user


def bootstrap_admin(db: Session) -> None:
    """Create the first account from ADMIN_EMAIL / ADMIN_PASSWORD if there are no users yet,
    so a fresh deployment can be signed into without shell access."""
    s = get_settings()
    if s.admin_email and s.admin_password and db.query(models.User).count() == 0:
        create_user(db, s.admin_email, s.admin_password)


# --------------------------------------------------------------------------- throttling
class LoginThrottle:
    """After FREE_ATTEMPTS failures for one email within WINDOW_S, further attempts are
    refused until the window passes. In-memory: fine for one API process."""

    FREE_ATTEMPTS = 5
    WINDOW_S = 15 * 60

    def __init__(self, clock=time.monotonic):
        self._clock = clock
        self._failures: dict[str, list[float]] = {}
        self._lock = threading.Lock()

    def _recent(self, key: str) -> list[float]:
        now = self._clock()
        recent = [t for t in self._failures.get(key, []) if now - t < self.WINDOW_S]
        self._failures[key] = recent
        return recent

    def retry_after(self, key: str) -> int:
        with self._lock:
            recent = self._recent(key)
            if len(recent) < self.FREE_ATTEMPTS:
                return 0
            return max(1, int(self.WINDOW_S - (self._clock() - recent[0])))

    def failed(self, key: str) -> None:
        with self._lock:
            self._recent(key).append(self._clock())

    def succeeded(self, key: str) -> None:
        with self._lock:
            self._failures.pop(key, None)


throttle = LoginThrottle()


# --------------------------------------------------------------------------- sessions
def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def start_session(db: Session, user: models.User) -> str:
    token = secrets.token_urlsafe(32)
    now = datetime.now(UTC)
    db.add(models.AuthSession(id=_token_hash(token), user_id=user.id, created_at=now,
                              expires_at=now + timedelta(hours=get_settings().session_hours)))
    user.last_login_at = now
    db.commit()
    return token


def end_session(db: Session, token: str | None) -> None:
    if token:
        db.query(models.AuthSession).filter_by(id=_token_hash(token)).delete()
        db.commit()


def user_for_token(db: Session, token: str | None) -> models.User | None:
    if not token:
        return None
    session = db.get(models.AuthSession, _token_hash(token))
    if session is None:
        return None
    expires = session.expires_at if session.expires_at.tzinfo else session.expires_at.replace(tzinfo=UTC)
    if expires <= datetime.now(UTC):
        db.delete(session)
        db.commit()
        return None
    return session.user


def current_user(request: Request, db: Session = Depends(get_db)) -> models.User:
    """Dependency for every protected route: the signed-in user, or 401."""
    user = user_for_token(db, request.cookies.get(COOKIE))
    if user is None:
        raise HTTPException(401, "Sign in to continue")
    request.state.user = user.email          # picked up by the request log
    return user


def cookie_args() -> dict:
    s = get_settings()
    return {"key": COOKIE, "httponly": True, "samesite": "lax", "secure": s.cookie_secure,
            "max_age": s.session_hours * 3600, "path": "/"}
