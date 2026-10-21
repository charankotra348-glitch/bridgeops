"""Turn reviewed decisions into the exact records that would be sent to HubSpot.

Pure functions, no network: the same plan backs the dry run and the real import.

  1. Blockers: execution refuses while any mapping, value or duplicate group is
     still pending review. A dry run reports them instead.
  2. Transform: each row becomes {hubspot_property: value} from the accepted
     column mappings and value maps. Rejected mappings and values mapped to
     nothing are left out.
  3. Duplicates: accepted duplicate groups collapse to one record (the reviewer's
     keep_row, or the first row), filling its empty fields from the others.
  4. Validate: against HubSpot's live property definitions: the property must
     exist and be writable, enum values must be real options, and every record
     needs a valid email because the import upserts by email.
"""

import re
from collections import defaultdict
from dataclasses import dataclass, field

from app.profiling import is_null

EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[A-Za-z]{2,}$")
ACCEPTED = {"auto", "approved"}


@dataclass
class PlannedRecord:
    row: int                                    # 0-based data row in the source
    email: str | None
    properties: dict[str, str]
    status: str                                 # ready | invalid | merged
    errors: list[str] = field(default_factory=list)
    merged_rows: list[int] = field(default_factory=list)   # other rows folded into this one
    merged_into: int | None = None


@dataclass
class Plan:
    records: list[PlannedRecord]
    blockers: list[str]

    def ready(self) -> list[PlannedRecord]:
        return [r for r in self.records if r.status == "ready"]

    def counts(self) -> dict[str, int]:
        out = {"rows": len(self.records)}
        for r in self.records:
            out[r.status] = out.get(r.status, 0) + 1
        return out


def find_blockers(mappings: list, duplicates: list) -> list[str]:
    out = []
    for m in mappings:
        if m.status == "pending":
            out.append(f"column {m.source_column!r} mapping is pending review")
        elif m.status in ACCEPTED:
            pending = [v for v, d in m.value_map.items() if d["status"] == "pending"]
            if pending:
                out.append(f"column {m.source_column!r} has {len(pending)} value(s) pending review")
    out += [f"duplicate group rows {g.rows} is pending review" for g in duplicates if g.status == "pending"]
    return out


def transform_row(row: dict, mappings: list) -> dict[str, str]:
    props: dict[str, str] = {}
    for m in mappings:
        if m.status not in ACCEPTED or not m.target_property or m.source_column not in row:
            continue
        raw = row.get(m.source_column)
        if is_null(raw):
            continue
        value = str(raw).strip()
        if m.value_map:                       # enum column: use the reviewed value map
            decision = m.value_map.get(value)
            if not decision or decision["status"] not in ACCEPTED or not decision["target"]:
                continue
            value = decision["target"]
        elif m.target_property == "email":
            value = value.lower()
        props.setdefault(m.target_property, value)   # first mapped column wins for a property
    return props


def _duplicate_targets(duplicates: list) -> dict[int, int]:
    """row -> row to keep, for every accepted 'same person' group (overlapping groups merged)."""
    parent: dict[int, int] = {}

    def find(i):
        parent.setdefault(i, i)
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    keep_choice: dict[int, int] = {}
    for g in duplicates:
        if g.same_person and g.status in ACCEPTED:
            for r in g.rows[1:]:
                parent[find(r)] = find(g.rows[0])
            if g.keep_row is not None:
                keep_choice[g.rows[0]] = g.keep_row

    clusters: dict[int, list[int]] = defaultdict(list)
    for r in list(parent):
        clusters[find(r)].append(r)
    target = {}
    for members in clusters.values():
        chosen = [keep_choice[m] for m in members if m in keep_choice]
        keep = chosen[0] if chosen else min(members)
        for m in members:
            target[m] = keep
    return target


def validate(props: dict[str, str], definitions: dict[str, dict]) -> list[str]:
    errors = []
    email = props.get("email")
    if not email:
        errors.append("no email: the import upserts by email, so every record needs one")
    elif not EMAIL.match(email):
        errors.append(f"invalid email {email!r}")
    for name, value in props.items():
        d = definitions.get(name)
        if d is None:
            errors.append(f"HubSpot has no contact property {name!r}")
        elif d.get("readOnly"):
            errors.append(f"HubSpot property {name!r} is read-only")
        elif d.get("options") and d.get("type") == "enumeration" and value not in d["options"]:
            errors.append(f"{value!r} is not an option of {name!r} in this HubSpot account")
    return errors


def build_plan(rows: list[dict], mappings: list, duplicates: list, definitions: dict[str, dict]) -> Plan:
    blockers = find_blockers(mappings, duplicates)
    props = [transform_row(r, mappings) for r in rows]
    targets = _duplicate_targets(duplicates)

    records = []
    for i, p in enumerate(props):
        keep = targets.get(i, i)
        if keep != i:
            records.append(PlannedRecord(i, p.get("email"), p, "merged", merged_into=keep))
        else:
            records.append(PlannedRecord(i, None, dict(p), "ready"))

    by_row = {r.row: r for r in records}
    for i, keep in sorted(targets.items()):
        if keep != i:                         # fill the kept record's gaps from the merged row
            kept = by_row[keep]
            kept.merged_rows.append(i)
            for k, v in props[i].items():
                kept.properties.setdefault(k, v)

    seen_emails: dict[str, int] = {}
    for r in records:
        if r.status != "ready":
            continue
        r.email = r.properties.get("email")
        r.errors = validate(r.properties, definitions)
        if r.email and r.email in seen_emails:
            r.errors.append(f"same email as row {seen_emails[r.email]}, which is not marked as a duplicate")
        elif r.email:
            seen_emails[r.email] = r.row
        if r.errors:
            r.status = "invalid"
    return Plan(records, blockers)
