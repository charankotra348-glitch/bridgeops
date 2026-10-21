"""HubSpot CRM client: the only code that talks to HubSpot.

- Batches: every write and read goes through the v3 batch endpoints, at most
  100 records per request (HubSpot's batch limit).
- Rate limiting: a client-side sliding window keeps requests under the private-app
  limit (100 per 10 s on Free/Starter), so a large import never trips 429s by design.
- Retries: 429 and 5xx are retried with backoff, honouring Retry-After when sent.
  Other 4xx are not retried; they are data or permission problems.
- Bad records: when HubSpot rejects a whole batch (one invalid value fails all
  100), the batch is split in half until the bad records are isolated, so good
  records still import and each failure is attributed to its own row.

Docs: https://developers.hubspot.com/docs/api-reference/crm-contacts-v3
"""

import logging
import threading
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field

import httpx

log = logging.getLogger(__name__)

BATCH_LIMIT = 100
RETRY_STATUSES = {429, 500, 502, 503, 504}
CONTACTS = "/crm/v3/objects/contacts"


class HubSpotError(Exception):
    """A request HubSpot refused or that kept failing after retries."""

    def __init__(self, status: int | None, message: str):
        super().__init__(f"HubSpot {status or 'request'} error: {message}")
        self.status = status
        self.message = message


@dataclass
class UpsertResult:
    created: dict[str, str] = field(default_factory=dict)   # email -> contact id
    updated: dict[str, str] = field(default_factory=dict)   # email -> contact id
    failed: dict[str, str] = field(default_factory=dict)    # email -> error message


class RateLimiter:
    """At most `max_requests` per `window_s` seconds (sliding window). Thread-safe."""

    def __init__(self, max_requests: int, window_s: float = 10.0,
                 clock: Callable[[], float] = time.monotonic, sleep: Callable[[float], None] = time.sleep):
        self.max_requests, self.window_s = max(1, max_requests), window_s
        self._clock, self._sleep = clock, sleep
        self._sent: deque[float] = deque()
        self._lock = threading.Lock()

    def acquire(self) -> float:
        """Block until a request may be sent. Returns seconds waited."""
        waited = 0.0
        while True:
            with self._lock:
                now = self._clock()
                while self._sent and now - self._sent[0] >= self.window_s:
                    self._sent.popleft()
                if len(self._sent) < self.max_requests:
                    self._sent.append(now)
                    return waited
                pause = self.window_s - (now - self._sent[0])
            self._sleep(pause)
            waited += pause


class HubSpotClient:
    def __init__(self, token: str, base_url: str = "https://api.hubapi.com", requests_per_10s: int = 90,
                 max_attempts: int = 4, timeout_s: float = 30.0, client: httpx.Client | None = None,
                 sleep: Callable[[float], None] = time.sleep):
        if not token:
            raise ValueError("HubSpot needs HUBSPOT_TOKEN (a private app token from your test account)")
        self._http = client or httpx.Client(base_url=base_url, timeout=timeout_s,
                                            headers={"Authorization": f"Bearer {token}"})
        self.max_attempts = max(1, max_attempts)
        self._sleep = sleep
        self.limiter = RateLimiter(requests_per_10s, 10.0, sleep=sleep)
        self.requests_sent = 0
        self.retries = 0

    # -- transport ------------------------------------------------------------
    def request(self, method: str, path: str, json: dict | None = None) -> dict:
        for attempt in range(1, self.max_attempts + 1):
            self.limiter.acquire()
            self.requests_sent += 1
            try:
                resp = self._http.request(method, path, json=json)
            except httpx.TransportError as exc:
                if attempt == self.max_attempts:
                    raise HubSpotError(None, type(exc).__name__) from exc
                self._backoff(attempt, None)
                continue
            if resp.status_code in RETRY_STATUSES and attempt < self.max_attempts:
                self._backoff(attempt, resp.headers.get("Retry-After"))
                continue
            if resp.status_code >= 400:
                raise HubSpotError(resp.status_code, _error_message(resp))
            return resp.json() if resp.content else {}
        raise HubSpotError(None, "retries exhausted")  # pragma: no cover

    def _backoff(self, attempt: int, retry_after: str | None) -> None:
        self.retries += 1
        try:
            pause = float(retry_after) if retry_after else min(2 ** attempt, 20)
        except ValueError:
            pause = min(2 ** attempt, 20)
        self._sleep(pause)

    # -- schema ---------------------------------------------------------------
    def contact_properties(self) -> dict[str, dict]:
        """Live contact property definitions: name -> {type, fieldType, options, readOnly}."""
        data = self.request("GET", "/crm/v3/properties/contacts")
        out = {}
        for p in data.get("results", []):
            out[p["name"]] = {
                "type": p.get("type"),
                "fieldType": p.get("fieldType"),
                "options": [o["value"] for o in p.get("options", []) if not o.get("hidden")],
                "readOnly": bool((p.get("modificationMetadata") or {}).get("readOnlyValue")),
            }
        return out

    # -- reads ----------------------------------------------------------------
    def read_by_email(self, emails: list[str], properties: list[str]) -> dict[str, dict]:
        """Existing contacts for these emails: email -> {"id", "properties"}. Missing emails are absent."""
        found: dict[str, dict] = {}
        props = sorted(set(properties) | {"email"})
        for chunk in _chunks(emails, BATCH_LIMIT):
            data = self.request("POST", f"{CONTACTS}/batch/read", json={
                "idProperty": "email", "properties": props, "inputs": [{"id": e} for e in chunk]})
            for r in data.get("results", []):
                email = (r.get("properties") or {}).get("email")
                if email:
                    found[email.lower()] = {"id": str(r["id"]), "properties": r.get("properties") or {}}
        return found

    # -- writes ---------------------------------------------------------------
    def upsert_by_email(self, records: dict[str, dict]) -> UpsertResult:
        """Create or update contacts keyed by email. records: email -> properties."""
        result = UpsertResult()
        for chunk in _chunks(list(records), BATCH_LIMIT):
            self._upsert_chunk(chunk, records, result)
        return result

    def _upsert_chunk(self, emails: list[str], records: dict[str, dict], result: UpsertResult) -> None:
        # email is sent in properties too: HubSpot does not allow partial upserts keyed by email
        inputs = [{"id": e, "idProperty": "email", "properties": {**records[e], "email": e}} for e in emails]
        try:
            data = self.request("POST", f"{CONTACTS}/batch/upsert", json={"inputs": inputs})
        except HubSpotError as exc:
            if exc.status is not None and 400 <= exc.status < 500 and exc.status != 429 and len(emails) > 1:
                mid = len(emails) // 2          # isolate the bad record(s)
                self._upsert_chunk(emails[:mid], records, result)
                self._upsert_chunk(emails[mid:], records, result)
                return
            for e in emails:
                result.failed[e] = exc.message
            return

        seen = set()
        unmatched = []
        for r in data.get("results", []):
            email = ((r.get("properties") or {}).get("email") or "").lower()
            if email in records:
                (result.created if r.get("new") else result.updated)[email] = str(r["id"])
                seen.add(email)
            else:
                unmatched.append(r)
        if unmatched:   # response did not echo the email: look those contacts up by id
            emails_by_id = self._emails_for_ids([str(r["id"]) for r in unmatched])
            for r in unmatched:
                email = emails_by_id.get(str(r["id"]), "")
                if email in records and email not in seen:
                    (result.created if r.get("new") else result.updated)[email] = str(r["id"])
                    seen.add(email)
        for err in data.get("errors", []):           # 207 multi-status
            for e in _emails_in_error(err, emails):
                if e not in seen:
                    result.failed[e] = err.get("message", "rejected")
                    seen.add(e)
        for e in emails:
            if e not in seen:
                result.failed[e] = "not confirmed by HubSpot response"

    def _emails_for_ids(self, ids: list[str]) -> dict[str, str]:
        data = self.request("POST", f"{CONTACTS}/batch/read", json={
            "properties": ["email"], "inputs": [{"id": i} for i in ids]})
        return {str(r["id"]): ((r.get("properties") or {}).get("email") or "").lower()
                for r in data.get("results", [])}

    def update(self, updates: dict[str, dict]) -> dict[str, str]:
        """Update contacts by id. Returns id -> error for the ones that failed."""
        failed: dict[str, str] = {}
        for chunk in _chunks(list(updates), BATCH_LIMIT):
            failed |= self._split_on_error(
                chunk, lambda ids: self.request("POST", f"{CONTACTS}/batch/update", json={
                    "inputs": [{"id": i, "properties": updates[i]} for i in ids]}))
        return failed

    def archive(self, ids: list[str]) -> dict[str, str]:
        """Archive (soft-delete) contacts by id. Returns id -> error for the ones that failed."""
        failed: dict[str, str] = {}
        for chunk in _chunks(ids, BATCH_LIMIT):
            failed |= self._split_on_error(
                chunk, lambda c: self.request("POST", f"{CONTACTS}/batch/archive",
                                              json={"inputs": [{"id": i} for i in c]}))
        return failed

    def _split_on_error(self, ids: list[str], send: Callable[[list[str]], dict]) -> dict[str, str]:
        try:
            send(ids)
            return {}
        except HubSpotError as exc:
            if exc.status is not None and 400 <= exc.status < 500 and len(ids) > 1:
                mid = len(ids) // 2
                return self._split_on_error(ids[:mid], send) | self._split_on_error(ids[mid:], send)
            return {i: exc.message for i in ids}


def _chunks(items: list, size: int):
    for i in range(0, len(items), size):
        yield items[i:i + size]


def _error_message(resp: httpx.Response) -> str:
    try:
        body = resp.json()
        return str(body.get("message") or body)[:500]
    except ValueError:
        return resp.text[:500] or f"HTTP {resp.status_code}"


def _emails_in_error(err: dict, emails: list[str]) -> list[str]:
    """Best-effort: which of our emails a 207 error refers to (HubSpot puts ids in context)."""
    ctx = err.get("context") or {}
    ids = [str(v).lower() for vals in ctx.values() if isinstance(vals, list) for v in vals]
    return [e for e in emails if e in ids]
