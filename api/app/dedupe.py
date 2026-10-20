"""Duplicate detection within one export, before anything is imported.

HubSpot upserts contacts by email, so two rows with the same email would silently
overwrite each other, and two rows for the same person under different emails
become two contacts. Both need catching before execution.

Same layering as mapping:
  1. Rules, for what is certain:
       same normalized email                      -> duplicate (confidence 1.00)
       same phone digits and same last name       -> duplicate (confidence 0.90)
  2. The decision provider, for judgment calls only: rows with the same last name
     and company, or the same first and last name, that the rules did not already
     group, become yes/no "same person?" questions.
  3. Confidence gating: confident answers are auto-accepted, the rest go to review.

Detection only flags. Nothing is merged or dropped here; execution decides that
from the reviewed groups.
"""

import re
from collections import defaultdict
from dataclasses import dataclass, field
from itertools import combinations

from app.decisions.base import Decision, DecisionProvider, Question

PHONE_RULE_CONFIDENCE = 0.90
MAX_PAIRS_PER_BLOCK = 10        # caps model questions from one very common name
EVIDENCE_FIELDS = ("email", "firstname", "lastname", "phone", "mobilephone", "company", "jobtitle",
                   "city", "country")
_NON_DIGITS = re.compile(r"\D")
_SPACES = re.compile(r"\s+")


@dataclass
class DuplicateGroup:
    rows: list[int]                     # 0-based data row indices, ascending
    reason: str
    method: str                         # rule | <provider name> | none
    confidence: float
    status: str                         # auto | pending
    same_person: bool | None = True     # model answer for model pairs; None when it abstained


@dataclass
class DedupeResult:
    groups: list[DuplicateGroup]
    questions: list[tuple[Question, Decision, str]] = field(default_factory=list)  # (q, d, status)

    def counts(self) -> dict[str, int]:
        flagged = [g for g in self.groups if g.same_person is not False]
        return {
            "duplicate_groups": len(flagged),
            "duplicate_groups_auto": sum(g.status == "auto" for g in flagged),
            "duplicate_groups_pending": sum(g.status == "pending" for g in self.groups),
            "duplicate_rows": len({r for g in flagged for r in g.rows}),
        }


# --------------------------------------------------------------------------- normalization
def norm_email(v: object) -> str:
    s = str(v or "").strip().lower()
    return s if "@" in s else ""


def norm_phone(v: object) -> str:
    digits = _NON_DIGITS.sub("", str(v or ""))
    return digits[-10:] if len(digits) >= 7 else ""   # last 10 digits ignores country-code styles


def norm_text(v: object) -> str:
    s = _SPACES.sub(" ", str(v or "").strip().lower())
    return "" if s in {"n/a", "na", "null", "none", "-", "?"} else s


def project(rows: list[dict], columns: dict[str, str]) -> list[dict[str, str]]:
    """Each row as {target_property: raw value}, using the column -> target mapping."""
    out = []
    for row in rows:
        rec = {}
        for col, target in columns.items():
            value = row.get(col)
            if value not in (None, "") and target not in rec:
                rec[target] = str(value).strip()
        out.append(rec)
    return out


# --------------------------------------------------------------------------- union-find
class _Groups:
    def __init__(self, n: int):
        self.parent = list(range(n))
        self.reason: dict[int, tuple[str, float]] = {}

    def find(self, i: int) -> int:
        while self.parent[i] != i:
            self.parent[i] = self.parent[self.parent[i]]
            i = self.parent[i]
        return i

    def union(self, a: int, b: int, reason: str, confidence: float) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra == rb:
            return
        root, child = min(ra, rb), max(ra, rb)
        self.parent[child] = root
        # A group is as certain as its weakest link
        reasons = [r for r in (self.reason.pop(child, None), self.reason.get(root)) if r]
        reasons.append((reason, confidence))
        self.reason[root] = min(reasons, key=lambda r: r[1])

    def clusters(self) -> dict[int, list[int]]:
        out: dict[int, list[int]] = defaultdict(list)
        for i in range(len(self.parent)):
            out[self.find(i)].append(i)
        return {k: v for k, v in out.items() if len(v) > 1}


# --------------------------------------------------------------------------- detection
def pair_question(i: int, j: int, a: dict, b: dict) -> Question:
    def evidence(rec):
        return {k: rec[k] for k in EVIDENCE_FIELDS if rec.get(k)}

    return Question(
        key=f"dup:{i}:{j}",
        type="yes_no",
        state={"record_a": evidence(a), "record_b": evidence(b)},
        instructions="Do these two rows from the same customer export describe the same real person? "
                     "Different emails can belong to one person (work vs personal), and two different "
                     "people can share a name.",
    )


def find_duplicates(rows: list[dict], columns: dict[str, str], provider: DecisionProvider,
                    threshold: float = 0.85) -> DedupeResult:
    recs = project(rows, columns)
    groups = _Groups(len(recs))

    # 1) Rules
    by_email: dict[str, list[int]] = defaultdict(list)
    by_phone_last: dict[tuple[str, str], list[int]] = defaultdict(list)
    for i, r in enumerate(recs):
        if e := norm_email(r.get("email")):
            by_email[e].append(i)
        last = norm_text(r.get("lastname"))
        for p in {norm_phone(r.get("phone")), norm_phone(r.get("mobilephone"))} - {""}:
            if last:
                by_phone_last[(p, last)].append(i)

    for idx in by_email.values():
        for k in idx[1:]:
            groups.union(idx[0], k, "same email address", 1.0)
    for idx in by_phone_last.values():
        for k in idx[1:]:
            groups.union(idx[0], k, "same phone number and last name", PHONE_RULE_CONFIDENCE)

    result_groups = []
    for members in groups.clusters().values():
        reason, conf = groups.reason[members[0]]
        result_groups.append(DuplicateGroup(members, reason, "rule", conf,
                                            "auto" if conf >= threshold else "pending"))

    # 2) Candidate pairs for the provider: blocked on name keys, skipping pairs the rules settled
    blocks: dict[tuple, list[int]] = defaultdict(list)
    for i, r in enumerate(recs):
        first, last, company = (norm_text(r.get(k)) for k in ("firstname", "lastname", "company"))
        if last and company:
            blocks[("last+company", last, company)].append(i)
        if first and last:
            blocks[("first+last", first, last)].append(i)

    pairs: list[tuple[int, int]] = []
    seen: set[tuple[int, int]] = set()
    for idx in blocks.values():
        for i, j in list(combinations(idx, 2))[:MAX_PAIRS_PER_BLOCK]:
            if (i, j) in seen or groups.find(i) == groups.find(j):
                continue
            seen.add((i, j))
            pairs.append((i, j))

    questions = []
    if pairs:
        qs = [pair_question(i, j, recs[i], recs[j]) for i, j in pairs]
        for (i, j), q, d in zip(pairs, qs, provider.decide(qs), strict=True):
            if d.abstained:
                status, same, method = "pending", None, "none"
                reason = "similar name; no model decision, needs review"
            else:
                status, same, method = ("auto" if d.confidence >= threshold else "pending",
                                        d.answer == "yes", provider.name)
                reason = f"similar name; model says {'same person' if same else 'different people'}"
            questions.append((q, d, status))
            result_groups.append(DuplicateGroup([i, j], reason, method, d.confidence, status, same))

    result_groups.sort(key=lambda g: g.rows)
    return DedupeResult(result_groups, questions)
