from app.decisions.base import CallUsage, Decision, Question


class AbstainProvider:
    """Rules-only baseline: never guesses. Anything the deterministic rules
    could not settle goes straight to human review."""

    name = "rules"

    def __init__(self) -> None:
        self.usage: list[CallUsage] = []  # never calls a model

    def decide(self, questions: list[Question]) -> list[Decision]:
        return [Decision(key=q.key, answer=None, confidence=0.0, provider=self.name) for q in questions]
