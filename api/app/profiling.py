"""Schema inference for uploaded CSV / JSON exports.

Deterministic only: no model calls. Produces, per column, an inferred type,
null stats, distinct counts, sample values and (for low-cardinality columns)
the value distribution that the normalizer works from.
"""

import csv
import io
import json
import re
from collections import Counter
from dataclasses import asdict, dataclass, field

NULL_TOKENS = {"", "n/a", "na", "null", "none", "nil", "-", "--", "—", "?", "unknown"}

PATTERNS: dict[str, re.Pattern] = {
    "email": re.compile(r"^[^@\s]+@[^@\s]+\.[A-Za-z]{2,}$"),
    "url": re.compile(r"^(https?://|www\.)\S+$", re.I),
    "phone": re.compile(r"^\+?[\d\s().\-]{7,20}$"),
    "boolean": re.compile(r"^(true|false|yes|no|y|n|t|f|0|1)$", re.I),
    "integer": re.compile(r"^-?\d{1,18}$"),
    "number": re.compile(r"^-?[\d,]*\.?\d+$"),
    "date": re.compile(
        r"^(\d{4}-\d{1,2}-\d{1,2}([ T]\d{1,2}:\d{2}(:\d{2})?)?"
        r"|\d{1,2}[/.\-]\d{1,2}[/.\-]\d{2,4}"
        r"|[A-Za-z]{3,9}\.? \d{1,2},? \d{4}"
        r"|\d{1,2} [A-Za-z]{3,9}\.? \d{4})$"
    ),
}
# Order matters: the first type that fits >= TYPE_SHARE of values wins.
TYPE_ORDER = ["email", "url", "date", "boolean", "integer", "number", "phone"]
TYPE_SHARE = 0.8
LOW_CARDINALITY = 30
MAX_SAMPLES = 8


class ParseError(ValueError):
    pass


@dataclass
class ColumnProfile:
    name: str
    inferred_type: str
    non_null: int
    nulls: int
    null_ratio: float
    distinct: int
    samples: list[str]
    value_counts: dict[str, int] = field(default_factory=dict)  # only for low-cardinality columns


@dataclass
class SourceProfile:
    file_format: str
    row_count: int
    columns: list[ColumnProfile]

    def to_dict(self) -> dict:
        return {"file_format": self.file_format, "row_count": self.row_count,
                "columns": [asdict(c) for c in self.columns]}


def is_null(value: object) -> bool:
    return value is None or str(value).strip().lower() in NULL_TOKENS


def infer_type(values: list[str]) -> str:
    if not values:
        return "empty"
    for type_name in TYPE_ORDER:
        pattern = PATTERNS[type_name]
        hits = sum(1 for v in values if pattern.match(v))
        if hits / len(values) >= TYPE_SHARE:
            # A column of 0/1 digits is boolean; longer digit strings are integers or phones.
            if type_name == "boolean" and all(v in {"0", "1"} for v in values) and len(set(values)) > 2:
                continue
            return type_name
    return "string"


def decode(raw: bytes) -> str:
    for encoding in ("utf-8-sig", "cp1252", "latin-1"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    raise ParseError("Could not decode file as text")


def read_rows(raw: bytes, filename: str) -> tuple[str, list[dict[str, object]]]:
    """Return (format, rows) for a CSV or JSON upload."""
    text = decode(raw)
    lower = filename.lower()
    if lower.endswith(".json") or text.lstrip().startswith(("[", "{")):
        try:
            data = json.loads(text)
        except json.JSONDecodeError as exc:
            raise ParseError(f"Invalid JSON: {exc.msg}") from exc
        if isinstance(data, dict):
            # Common export shape: {"records": [...]} or {"data": [...]}
            lists = [v for v in data.values() if isinstance(v, list)]
            data = lists[0] if lists else [data]
        if not isinstance(data, list) or not all(isinstance(r, dict) for r in data):
            raise ParseError("JSON must be a list of objects")
        return "json", data

    try:
        dialect = csv.Sniffer().sniff(text[:4096], delimiters=",;\t|")
    except csv.Error:
        dialect = csv.excel
    reader = csv.DictReader(io.StringIO(text), dialect=dialect)
    if not reader.fieldnames:
        raise ParseError("CSV has no header row")
    rows = [r for r in reader if any((v or "").strip() for v in r.values() if isinstance(v, str))]
    return "csv", rows


def profile_rows(file_format: str, rows: list[dict[str, object]]) -> SourceProfile:
    names: list[str] = []
    for row in rows:
        for key in row:
            if key is not None and key not in names:
                names.append(key)

    columns: list[ColumnProfile] = []
    for name in names:
        raw_values = [row.get(name) for row in rows]
        values = [str(v).strip() for v in raw_values if not is_null(v)]
        counts = Counter(values)
        samples = [v for v, _ in counts.most_common(MAX_SAMPLES)]
        columns.append(ColumnProfile(
            name=name.strip(),
            inferred_type=infer_type(values),
            non_null=len(values),
            nulls=len(raw_values) - len(values),
            null_ratio=round((len(raw_values) - len(values)) / len(raw_values), 4) if raw_values else 0.0,
            distinct=len(counts),
            samples=samples,
            value_counts=dict(counts.most_common()) if 0 < len(counts) <= LOW_CARDINALITY else {},
        ))
    return SourceProfile(file_format=file_format, row_count=len(rows), columns=columns)


def profile_file(raw: bytes, filename: str) -> SourceProfile:
    file_format, rows = read_rows(raw, filename)
    if not rows:
        raise ParseError("File has no data rows")
    return profile_rows(file_format, rows)
