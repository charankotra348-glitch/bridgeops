import logging
import uuid
from contextlib import asynccontextmanager
from dataclasses import asdict
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, UploadFile
from sqlalchemy.orm import Session

from app import models
from app.config import get_settings
from app.db import Base, engine, get_db
from app.decisions import get_provider
from app.mapping.pipeline import plan_mappings
from app.mapping.target_schema import BY_NAME
from app.profiling import ParseError, profile_file
from app.schemas import JobCreate, JobOut, MappingOut, MappingUpdate, SourceOut

logging.basicConfig(level=logging.INFO, format='{"level":"%(levelname)s","msg":"%(message)s"}')
log = logging.getLogger("bridgeops")

ALLOWED_SUFFIXES = {".csv", ".json", ".tsv", ".txt"}


@asynccontextmanager
async def lifespan(_: FastAPI):
    # Week 1: create tables directly. Alembic migrations replace this in Week 2.
    Base.metadata.create_all(engine)
    Path(get_settings().upload_dir).mkdir(parents=True, exist_ok=True)
    yield


app = FastAPI(title="BridgeOps", version="0.1.0", lifespan=lifespan)


def audit(db: Session, action: str, obj_type: str, obj_id: str, before=None, after=None, actor="system"):
    db.add(models.AuditLog(actor=actor, action=action, object_type=obj_type, object_id=obj_id,
                           before=before, after=after))


@app.get("/healthz")
def healthz():
    return {"status": "ok"}


# --------------------------------------------------------------------------- sources
@app.post("/sources", response_model=SourceOut, status_code=201)
async def upload_source(file: UploadFile, db: Session = Depends(get_db)):
    settings = get_settings()
    name = Path(file.filename or "upload.csv").name
    if Path(name).suffix.lower() not in ALLOWED_SUFFIXES:
        raise HTTPException(415, f"Unsupported file type; use one of {sorted(ALLOWED_SUFFIXES)}")

    raw = await file.read(settings.max_upload_mb * 1024 * 1024 + 1)
    if len(raw) > settings.max_upload_mb * 1024 * 1024:
        raise HTTPException(413, f"File larger than {settings.max_upload_mb} MB")
    try:
        profile = profile_file(raw, name)
    except ParseError as exc:
        raise HTTPException(422, str(exc)) from exc

    source_id = uuid.uuid4().hex
    path = Path(settings.upload_dir) / f"{source_id}{Path(name).suffix.lower()}"
    path.write_bytes(raw)

    source = models.Source(id=source_id, filename=name, storage_path=str(path),
                           file_format=profile.file_format, row_count=profile.row_count,
                           schema_json=profile.to_dict())
    db.add(source)
    audit(db, "upload", "source", source_id, after={"filename": name, "rows": profile.row_count})
    db.commit()
    log.info("source %s uploaded: %s rows, %s columns", source_id, profile.row_count, len(profile.columns))
    return _source_out(source)


@app.get("/sources/{source_id}", response_model=SourceOut)
def get_source(source_id: str, db: Session = Depends(get_db)):
    source = db.get(models.Source, source_id)
    if not source:
        raise HTTPException(404, "Source not found")
    return _source_out(source)


def _source_out(source: models.Source) -> SourceOut:
    return SourceOut(id=source.id, filename=source.filename, file_format=source.file_format,
                     row_count=source.row_count, created_at=source.created_at,
                     columns=source.schema_json["columns"])


# --------------------------------------------------------------------------- jobs
@app.post("/jobs", response_model=JobOut, status_code=201)
def create_job(body: JobCreate, db: Session = Depends(get_db)):
    settings = get_settings()
    source = db.get(models.Source, body.source_id)
    if not source:
        raise HTTPException(404, "Source not found")
    try:
        provider = get_provider(settings, body.provider)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc

    plan = plan_mappings(source.schema_json, provider, settings.mapping_threshold,
                         settings.normalize_threshold)

    job = models.Job(source_id=source.id, provider=provider.name, status="mapped", stage="review",
                     counts=plan.counts())
    db.add(job)
    db.flush()
    for col in plan.columns:
        db.add(models.Mapping(job_id=job.id, source_column=col.column, target_property=col.target,
                              method=col.method, confidence=col.confidence, status=col.status,
                              reason=col.reason,
                              value_map={v: asdict(d) for v, d in col.value_map.items()}))
    for rec in plan.decisions:
        db.add(models.Decision(job_id=job.id, kind=rec.kind, input_hash=rec.question.input_hash(),
                               subject=rec.subject[:255], answer=rec.decision.answer,
                               confidence=rec.decision.confidence, provider=rec.decision.provider,
                               latency_ms=rec.decision.latency_ms, status=rec.status))
    audit(db, "create", "job", job.id, after={"source_id": source.id, "provider": provider.name,
                                              **plan.counts()})
    db.commit()
    db.refresh(job)
    log.info("job %s mapped: %s", job.id, plan.counts())
    return job


@app.get("/jobs/{job_id}", response_model=JobOut)
def get_job(job_id: str, db: Session = Depends(get_db)):
    job = db.get(models.Job, job_id)
    if not job:
        raise HTTPException(404, "Job not found")
    return job


@app.get("/jobs/{job_id}/review", response_model=list[MappingOut])
def review_queue(job_id: str, db: Session = Depends(get_db)):
    """Columns needing a human: pending columns, or columns with any pending value."""
    job = db.get(models.Job, job_id)
    if not job:
        raise HTTPException(404, "Job not found")
    return [m for m in job.mappings
            if m.status == "pending" or any(v["status"] == "pending" for v in m.value_map.values())]


@app.patch("/mappings/{mapping_id}", response_model=MappingOut)
def update_mapping(mapping_id: str, body: MappingUpdate, db: Session = Depends(get_db)):
    mapping = db.get(models.Mapping, mapping_id)
    if not mapping:
        raise HTTPException(404, "Mapping not found")
    if body.target_property is not None and body.target_property not in BY_NAME:
        raise HTTPException(422, f"Unknown target property {body.target_property!r}")

    before = {"target_property": mapping.target_property, "status": mapping.status,
              "value_map": mapping.value_map}
    if body.status == "approved":
        if body.target_property is not None:
            mapping.target_property = body.target_property
        mapping.method, mapping.confidence = "human", 1.0
    mapping.status = body.status

    if body.value_map:
        target = mapping.target_property
        options = BY_NAME[target].options if target else ()
        new_map = dict(mapping.value_map)
        for value, option in body.value_map.items():
            if option is not None and option not in options:
                raise HTTPException(422, f"{option!r} is not a valid option for {target}")
            new_map[value] = {"target": option, "confidence": 1.0, "method": "human", "status": "approved"}
        mapping.value_map = new_map

    audit(db, "review", "mapping", mapping.id, before=before,
          after={"target_property": mapping.target_property, "status": mapping.status,
                 "value_map": mapping.value_map}, actor=body.actor)
    db.commit()
    return mapping
