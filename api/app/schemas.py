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
    provider: Literal["rules", "jev"] | None = None


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
    actor: str = "reviewer"
