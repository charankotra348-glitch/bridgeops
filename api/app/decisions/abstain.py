from app.decisions.base import Decision, Question


class AbstainProvider:
    """Rules-only baseline: never guesses. Anything the deterministic rules
    could not settle goes straight to human review."""

    name = "rules"

    def decide(self, questions: list[Question]) -> list[Decision]:
        return [Decision(key=q.key, answer=None, confidence=0.0, provider=self.name) for q in questions]
