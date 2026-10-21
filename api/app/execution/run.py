"""Execute a reviewed job against HubSpot: dry run, import, and rollback.

Import (upsert by email):
  - Refuses while anything is pending review (see plan.find_blockers).
  - Snapshots the current HubSpot values of every property it is about to write,
    so a rollback can restore them.
  - Skips records whose values already match HubSpot ("unchanged"): no write.
  - Idempotent re-runs: rows already imported with the same payload are skipped,
    and failed or invalid rows are retried. Running a job twice never creates
    duplicate contacts, because every write is an upsert keyed by email.

Rollback:
  - Archives contacts this job created, and restores the snapshotted values on
    contacts it updated.
  - Conflict check first: if a contact's current values no longer match what this
    job wrote, someone changed it since, so the record is left alone and marked
    rollback_conflict instead of overwriting their change.
  - HubSpot only moves lifecycle stage forward through the API; to restore an
    earlier stage the value is cleared first, then set.
"""

import hashlib
import json
import logging
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime

from sqlalchemy.orm import Session

from app import models
from app.execution.plan import Plan, build_plan
from app.hubspot import HubSpotClient

log = logging.getLogger(__name__)

DONE = {"created", "updated", "unchanged"}
RUNNABLE_JOB_STATES = {"mapped", "executed", "executed_with_errors"}


class ExecutionError(Exception):
    def __init__(self, status_code: int, message: str, details: list[str] | None = None):
        super().__init__(message)
        self.status_code, self.message, self.details = status_code, message, details or []


@dataclass
class Report:
    dry_run: bool
    blockers: list[str]
    counts: dict[str, int]
    records: list[dict] = field(default_factory=list)
    requests_sent: int = 0
    retries: int = 0
    reasons: list[dict] = field(default_factory=list)   # why rows can't import, most common first


_QUOTED = re.compile(r"'[^']*'")


def summarize_reasons(plan: Plan, top: int = 5) -> list[dict]:
    """Group invalid rows by error, ignoring the specific value in quotes, so one cause
    behind many rows ("invalid email ...") shows up once with its row count."""
    groups: dict[str, dict] = {}
    for r in plan.records:
        if r.status != "invalid":
            continue
        for err in r.errors:
            key = _QUOTED.sub("'…'", err)
            g = groups.setdefault(key, {"reason": key, "rows": 0, "example": err, "first_row": r.row})
            g["rows"] += 1
    return sorted(groups.values(), key=lambda g: -g["rows"])[:top]


def payload_hash(props: dict) -> str:
    return hashlib.sha256(json.dumps(props, sort_keys=True).encode()).hexdigest()


def _norm(v) -> str:
    return str(v if v is not None else "").strip().casefold()


def _same(current: dict, wanted: dict) -> bool:
    """True if HubSpot already holds these values (ignoring case and surrounding spaces,
    which HubSpot may normalize)."""
    return all(_norm(current.get(k)) == _norm(v) for k, v in wanted.items())


def _plan(job: models.Job, rows: list[dict], definitions: dict) -> Plan:
    return build_plan(rows, list(job.mappings), list(job.duplicates), definitions)


# --------------------------------------------------------------------------- dry run
def dry_run(job: models.Job, rows: list[dict], definitions: dict, client: HubSpotClient | None,
            sample: int = 50) -> Report:
    """What an import would do. Never writes anything. With a client, it also reads HubSpot
    to say which records would be created, updated or left unchanged."""
    plan = _plan(job, rows, definitions)
    ready = plan.ready()
    existing = client.read_by_email([r.email for r in ready], _props(ready)) if client and ready else {}

    out, counts = [], dict(plan.counts())
    for r in plan.records:
        action = r.status
        if r.status == "ready":
            if not client:
                action = "would_upsert"
            elif r.email not in existing:
                action = "would_create"
            elif _same(existing[r.email]["properties"], r.properties):
                action = "would_skip_unchanged"
            else:
                action = "would_update"
            counts[action] = counts.get(action, 0) + 1
        out.append({"row": r.row, "action": action, "email": r.email, "properties": r.properties,
                    "errors": r.errors, "merged_rows": r.merged_rows, "merged_into": r.merged_into})
    counts.pop("ready", None)
    return Report(True, plan.blockers, counts, out[:sample],
                  client.requests_sent if client else 0, client.retries if client else 0,
                  summarize_reasons(plan))


# --------------------------------------------------------------------------- import
def execute(db: Session, job: models.Job, rows: list[dict], definitions: dict,
            client: HubSpotClient) -> Report:
    if job.status not in RUNNABLE_JOB_STATES:
        raise ExecutionError(409, f"job is {job.status}; it can be executed only when mapped or executed")
    plan = _plan(job, rows, definitions)
    if plan.blockers:
        raise ExecutionError(409, "review is not finished", plan.blockers)

    previous_status = job.status
    job.status = "executing"
    db.commit()

    prior = {rec.row: rec for rec in job.records}
    to_send = []
    for r in plan.ready():
        rec = prior.get(r.row)
        if rec and rec.status in DONE and rec.payload_hash == payload_hash(r.properties):
            continue                                         # already imported, nothing changed
        to_send.append(r)

    try:
        before = client.read_by_email([r.email for r in to_send], _props(to_send)) if to_send else {}
        writes = {r.email: r.properties for r in to_send
                  if not (r.email in before and _same(before[r.email]["properties"], r.properties))}
        result = client.upsert_by_email(writes) if writes else None
    except Exception:
        # Nothing was recorded for this run. Upserts are keyed by email, so re-running
        # after the failure is safe even if HubSpot applied part of a batch.
        job.status = previous_status
        db.commit()
        raise

    def save(row: int, **values):
        rec = prior.get(row) or models.ImportRecord(job_id=job.id, row=row)
        for k, v in values.items():
            setattr(rec, k, v)
        if row not in prior:
            job.records.append(rec)
            prior[row] = rec

    for r in plan.records:
        if r.status in ("invalid", "merged"):
            save(r.row, email=r.email, status=r.status, payload=r.properties,
                 payload_hash=payload_hash(r.properties), hubspot_id=None, before=None,
                 error="; ".join(r.errors) if r.errors else (f"merged into row {r.merged_into}"
                                                             if r.merged_into is not None else ""))
    for r in to_send:
        snap = before.get(r.email)
        prev = {k: (snap["properties"].get(k) if snap else None) for k in r.properties} if snap else None
        common = dict(email=r.email, payload=r.properties, payload_hash=payload_hash(r.properties),
                      before=prev)
        if r.email not in writes:
            save(r.row, status="unchanged", hubspot_id=snap["id"], error="", **common)
        elif r.email in result.created:
            save(r.row, status="created", hubspot_id=result.created[r.email], error="", **common)
        elif r.email in result.updated:
            save(r.row, status="updated", hubspot_id=result.updated[r.email], error="", **common)
        else:
            save(r.row, status="failed", hubspot_id=snap["id"] if snap else None,
                 error=result.failed.get(r.email, "unknown error"), **common)

    counts = _record_counts(job)
    failed = counts.get("failed", 0) + counts.get("invalid", 0)
    job.status = "executed_with_errors" if failed else "executed"
    job.stage = "done"
    job.finished_at = datetime.now(UTC)
    job.counts = {**(job.counts or {}), "import": counts}
    db.commit()
    log.info("job %s executed: %s", job.id, counts)
    return Report(False, [], counts, [], client.requests_sent, client.retries)


# --------------------------------------------------------------------------- rollback
def rollback(db: Session, job: models.Job, client: HubSpotClient) -> Report:
    if job.status not in {"executed", "executed_with_errors"}:
        raise ExecutionError(409, f"job is {job.status}; only an executed job can be rolled back")
    previous_status = job.status
    job.status = "rolling_back"
    db.commit()
    try:
        return _rollback(db, job, client)
    except Exception:
        job.status = previous_status
        db.commit()
        raise


def _rollback(db: Session, job: models.Job, client: HubSpotClient) -> Report:
    created =[r for r in job.records if r.status == "created" and r.hubspot_id]
    updated = [r for r in job.records if r.status == "updated" and r.hubspot_id and r.before is not None]
    touched = created + updated
    current = client.read_by_email([r.email for r in touched], _props(touched)) if touched else {}

    safe_created, safe_updated = [], []
    for r in touched:
        now = current.get(r.email)
        if now is None:                             # already gone: nothing to undo
            r.status, r.error = "rolled_back", "contact no longer exists"
        elif not _same(now["properties"], r.payload):
            r.status = "rollback_conflict"
            r.error = "changed in HubSpot after the import; left as is"
        else:
            (safe_created if r.status == "created" else safe_updated).append(r)

    failed = client.archive([r.hubspot_id for r in safe_created]) if safe_created else {}
    restore = {r.hubspot_id: {k: (v if v is not None else "") for k, v in r.before.items()}
               for r in safe_updated}
    stage_back = {i: p for i, p in restore.items() if "lifecyclestage" in p}
    if stage_back:                                  # lifecycle stage only moves forward unless cleared
        failed |= client.update({i: {"lifecyclestage": ""} for i in stage_back})
    failed |= client.update({i: p for i, p in restore.items() if i not in failed}) if restore else {}

    for r in safe_created + safe_updated:
        if r.hubspot_id in failed:
            r.status, r.error = "rollback_failed", failed[r.hubspot_id]
        else:
            r.status, r.error = "rolled_back", ""

    counts = _record_counts(job)
    job.status = "rolled_back"
    job.counts = {**(job.counts or {}), "rollback": counts}
    db.commit()
    log.info("job %s rolled back: %s", job.id, counts)
    return Report(False, [], counts, [], client.requests_sent, client.retries)


def _props(records) -> list[str]:
    names = set()
    for r in records:
        names |= set(getattr(r, "properties", None) or getattr(r, "payload", None) or {})
    return sorted(names)


def _record_counts(job: models.Job) -> dict[str, int]:
    out: dict[str, int] = {}
    for r in job.records:
        out[r.status] = out.get(r.status, 0) + 1
    return out
