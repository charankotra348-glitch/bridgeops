"""Database tables: sources, jobs, mappings, decisions, duplicate_groups, import_records and audit_log."""

import uuid
from datetime import UTC, datetime

from sqlalchemy import JSON, Boolean, DateTime, Float, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base


def _id() -> str:
    return uuid.uuid4().hex


def _now() -> datetime:
    return datetime.now(UTC)


class Source(Base):
    __tablename__ = "sources"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_id)
    workspace_id: Mapped[str] = mapped_column(String(32), default="default", index=True)
    filename: Mapped[str] = mapped_column(String(255))
    storage_path: Mapped[str] = mapped_column(String(512))
    file_format: Mapped[str] = mapped_column(String(10))
    row_count: Mapped[int] = mapped_column(Integer)
    schema_json: Mapped[dict] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)

    jobs: Mapped[list["Job"]] = relationship(back_populates="source")


class Job(Base):
    __tablename__ = "jobs"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_id)
    workspace_id: Mapped[str] = mapped_column(String(32), default="default", index=True)
    source_id: Mapped[str] = mapped_column(ForeignKey("sources.id"))
    target: Mapped[str] = mapped_column(String(50), default="hubspot.contacts")
    # mapped | executing | executed | executed_with_errors | rolling_back | rolled_back
    status: Mapped[str] = mapped_column(String(30), default="created")
    stage: Mapped[str] = mapped_column(String(20), default="profile")
    provider: Mapped[str] = mapped_column(String(20))
    counts: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    source: Mapped[Source] = relationship(back_populates="jobs")
    mappings: Mapped[list["Mapping"]] = relationship(back_populates="job", cascade="all, delete-orphan")
    duplicates: Mapped[list["DuplicateGroup"]] = relationship(back_populates="job",
                                                               cascade="all, delete-orphan")
    records: Mapped[list["ImportRecord"]] = relationship(back_populates="job", cascade="all, delete-orphan")


class Mapping(Base):
    __tablename__ = "mappings"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_id)
    job_id: Mapped[str] = mapped_column(ForeignKey("jobs.id"), index=True)
    source_column: Mapped[str] = mapped_column(String(255))
    target_property: Mapped[str | None] = mapped_column(String(100), nullable=True)
    method: Mapped[str] = mapped_column(String(20))  # rule | jev | llm | human
    confidence: Mapped[float] = mapped_column(Float)
    status: Mapped[str] = mapped_column(String(20))  # auto | pending | approved | rejected
    reason: Mapped[str] = mapped_column(Text, default="")
    value_map: Mapped[dict] = mapped_column(JSON, default=dict)  # source value -> target enum

    job: Mapped[Job] = relationship(back_populates="mappings")


class Decision(Base):
    """Every model/rule decision, for audit, cost tracking and the eval."""

    __tablename__ = "decisions"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_id)
    job_id: Mapped[str] = mapped_column(ForeignKey("jobs.id"), index=True)
    kind: Mapped[str] = mapped_column(String(20))  # mapping | normalize | duplicate
    input_hash: Mapped[str] = mapped_column(String(64), index=True)
    subject: Mapped[str] = mapped_column(String(255))
    answer: Mapped[str | None] = mapped_column(String(100), nullable=True)
    confidence: Mapped[float] = mapped_column(Float)
    provider: Mapped[str] = mapped_column(String(20))
    latency_ms: Mapped[float] = mapped_column(Float, default=0.0)
    status: Mapped[str] = mapped_column(String(20))  # auto | pending | approved | rejected
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


class DuplicateGroup(Base):
    """Rows in one source that look like the same person. Flags only; execution decides
    what to do with approved groups."""

    __tablename__ = "duplicate_groups"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_id)
    job_id: Mapped[str] = mapped_column(ForeignKey("jobs.id"), index=True)
    rows: Mapped[list] = mapped_column(JSON)            # 0-based data row indices
    reason: Mapped[str] = mapped_column(Text, default="")
    method: Mapped[str] = mapped_column(String(20))     # rule | gemini | jev | human | none
    confidence: Mapped[float] = mapped_column(Float)
    same_person: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    status: Mapped[str] = mapped_column(String(20))     # auto | pending | approved | rejected
    keep_row: Mapped[int | None] = mapped_column(Integer, nullable=True)  # row to import, set on review

    job: Mapped[Job] = relationship(back_populates="duplicates")


class ImportRecord(Base):
    """What happened to one source row in the import, and what it overwrote (for rollback)."""

    __tablename__ = "import_records"
    __table_args__ = (UniqueConstraint("job_id", "row", name="uq_import_records_job_row"),)

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_id)
    job_id: Mapped[str] = mapped_column(ForeignKey("jobs.id"), index=True)
    row: Mapped[int] = mapped_column(Integer)
    email: Mapped[str | None] = mapped_column(String(320), nullable=True, index=True)
    # created | updated | unchanged | failed | invalid | merged | rolled_back | rollback_failed
    # | rollback_conflict
    status: Mapped[str] = mapped_column(String(30))
    hubspot_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    payload: Mapped[dict] = mapped_column(JSON, default=dict)          # properties sent
    payload_hash: Mapped[str] = mapped_column(String(64), default="")
    before: Mapped[dict | None] = mapped_column(JSON, nullable=True)   # prior values of those properties
    error: Mapped[str] = mapped_column(Text, default="")
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, onupdate=_now)

    job: Mapped[Job] = relationship(back_populates="records")


class AuditLog(Base):
    __tablename__ = "audit_log"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    actor: Mapped[str] = mapped_column(String(100), default="system")
    action: Mapped[str] = mapped_column(String(50))
    object_type: Mapped[str] = mapped_column(String(50))
    object_id: Mapped[str] = mapped_column(String(32))
    before: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    after: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
