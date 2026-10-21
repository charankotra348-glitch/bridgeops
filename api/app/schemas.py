from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict


class ColumnProfileOut(BaseModel):
    name: str
    inferred_type: str
    non_null: int
    nulls: int
    null_ratio: float
    distinct: int
    samples: list[str]
    value_counts: dict[str, int]


class SourceOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    filename: str
    file_format: str
    row_count: int
    created_at: datetime
    columns: list[ColumnProfileOut]


class JobCreate(BaseModel):
    source_id: str
    provider: Literal["rules", "gemini", "jev"] | None = None


class ValueDecisionOut(BaseModel):
    target: str | None
    confidence: float
    method: str
    status: str


class MappingOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    source_column: str
    target_property: str | None
    method: str
    confidence: float
    status: str
    reason: str
    value_map: dict[str, ValueDecisionOut]


class JobOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    source_id: str
    target: str
    status: str
    stage: str
    provider: str
    counts: dict
    created_at: datetime
    mappings: list[MappingOut]


class MappingUpdate(BaseModel):
    """A human decision on one column (and optionally its value map)."""

    target_property: str | None = None
    status: Literal["approved", "rejected"]
    value_map: dict[str, str | None] | None = None  # source value -> enum option


class DuplicateOut(BaseModel):
    id: str
    rows: list[int]                 # 0-based data row indices in the source
    reason: str
    method: str
    confidence: float
    same_person: bool | None
    status: str
    keep_row: int | None
    records: list[dict]             # the rows themselves, for side-by-side review


class DuplicateUpdate(BaseModel):
    """approved = same person, import only keep_row; rejected = different people, import all."""

    status: Literal["approved", "rejected"]
    keep_row: int | None = None


class ExecutionReportOut(BaseModel):
    dry_run: bool
    blockers: list[str]
    counts: dict[str, int]
    records: list[dict]             # dry run only: what would happen to each row (first 50)
    definitions: str                # "hubspot" (live) or "built-in"
    requests_sent: int
    retries: int
    reasons: list[dict] = []        # dry run: why rows can't import, grouped, most common first


class ImportRecordOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    row: int
    email: str | None
    status: str
    hubspot_id: str | None
    payload: dict
    before: dict | None
    error: str


class JobSummaryOut(BaseModel):
    id: str
    filename: str
    rows: int
    provider: str
    status: str
    created_at: datetime
    counts: dict


class TargetOut(BaseModel):
    name: str
    label: str
    description: str
    type: str
    options: list[dict]             # [{"value", "description"}] for enum properties


class LoginIn(BaseModel):
    email: str
    password: str


class UserOut(BaseModel):
    email: str
