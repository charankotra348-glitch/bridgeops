"""Mapping stage: rules first, then the decision provider, then confidence gating.

Output for each source column: a target property (or none), how it was decided,
a confidence, and whether it is auto-accepted or waiting for human review.
Enum columns also get a per-value map (e.g. "Active-ish" -> "customer").
"""

from dataclasses import dataclass, field

from app.decisions.base import Decision, DecisionProvider, Question
from app.mapping import rules
from app.mapping.target_schema import BY_NAME, MAPPING_OPTIONS, OPTION_DESCRIPTIONS


@dataclass
class ValueDecision:
    target: str | None
    confidence: float
    method: str
    status: str


@dataclass
class ColumnPlan:
    column: str
    target: str | None
    confidence: float
    method: str            # rule | <provider name> | none
    status: str            # auto | pending
    reason: str
    value_map: dict[str, ValueDecision] = field(default_factory=dict)


@dataclass
class DecisionRecord:
    kind: str
    subject: str
    question: Question
    decision: Decision
    status: str


@dataclass
class MappingPlan:
    columns: list[ColumnPlan]
    decisions: list[DecisionRecord]

    def counts(self) -> dict[str, int]:
        auto = sum(c.status == "auto" for c in self.columns)
        values = [v for c in self.columns for v in c.value_map.values()]
        return {
            "columns": len(self.columns),
            "columns_auto": auto,
            "columns_pending": len(self.columns) - auto,
            "values": len(values),
            "values_auto": sum(v.status == "auto" for v in values),
            "values_pending": sum(v.status == "pending" for v in values),
            "model_decisions": len(self.decisions),
        }


def _gate(confidence: float, threshold: float) -> str:
    return "auto" if confidence >= threshold else "pending"


def mapping_question(col: dict) -> Question:
    return Question(
        key=f"map:{col['name']}",
        type="choice",
        state={"column_name": col["name"], "value_type": col["inferred_type"],
               "sample_values": col["samples"][:8]},
        instructions="Which HubSpot contact property should this source column be imported into, "
                     "judging by the column name and its sample values?",
        options=MAPPING_OPTIONS,
    )


def value_question(column: str, target: str, value: str) -> Question:
    return Question(
        key=f"val:{column}:{value}",
        type="choice",
        state={"source_column": column, "source_value": value},
        instructions=f"Which {BY_NAME[target].label} option best matches this source value?",
        options=OPTION_DESCRIPTIONS[target],
    )


def plan_mappings(profile: dict, provider: DecisionProvider, mapping_threshold: float = 0.80,
                  normalize_threshold: float = 0.90) -> MappingPlan:
    columns = profile["columns"]
    by_name = {c["name"]: c for c in columns}

    # 1) Rules
    rule_results = rules.resolve_conflicts([rules.map_column(c["name"], c["inferred_type"]) for c in columns])
    plans: dict[str, ColumnPlan] = {}
    for r in rule_results:
        if r.target:
            plans[r.column] = ColumnPlan(r.column, r.target, r.confidence, "rule",
                                         _gate(r.confidence, mapping_threshold), r.reason)

    # 2) Decision provider for everything the rules left open
    decisions: list[DecisionRecord] = []
    open_cols = [r for r in rule_results if not r.target]
    taken = {p.target for p in plans.values() if p.status == "auto"}
    if open_cols:
        questions = [mapping_question(by_name[r.column]) for r in open_cols]
        for r, q, d in zip(open_cols, questions, provider.decide(questions), strict=True):
            target = None if d.answer in (None, "none") else d.answer
            if d.abstained:
                status, reason = "pending", r.reason + "; no model decision, needs review"
            elif target in taken:
                status, reason = "pending", f"model chose {target}, already mapped by a rule; needs review"
            else:
                status, reason = _gate(d.confidence, mapping_threshold), f"model chose {d.answer}"
                if status == "auto" and target:
                    taken.add(target)
            plans[r.column] = ColumnPlan(r.column, target, d.confidence,
                                         provider.name if not d.abstained else "none", status, reason)
            decisions.append(DecisionRecord("mapping", r.column, q, d, status))

    # 3) Value normalization for enum targets
    value_questions: list[tuple[ColumnPlan, str, Question]] = []
    for plan in plans.values():
        if not plan.target or BY_NAME[plan.target].type != "enum":
            continue
        col = by_name[plan.column]
        for value in (col["value_counts"] or {v: 1 for v in col["samples"]}):
            target, conf, _ = rules.map_enum_value(plan.target, value)
            if target:
                plan.value_map[value] = ValueDecision(target, conf, "rule", _gate(conf, normalize_threshold))
            else:
                value_questions.append((plan, value, value_question(plan.column, plan.target, value)))

    if value_questions:
        answers = provider.decide([q for _, _, q in value_questions])
        for (plan, value, q), d in zip(value_questions, answers, strict=True):
            status = "pending" if d.abstained else _gate(d.confidence, normalize_threshold)
            plan.value_map[value] = ValueDecision(d.answer, d.confidence,
                                                  provider.name if not d.abstained else "none", status)
            decisions.append(DecisionRecord("normalize", f"{plan.column}={value}", q, d, status))

    ordered = [plans[c["name"]] for c in columns]
    return MappingPlan(columns=ordered, decisions=decisions)
