"""Provider-agnostic decision interface.

The pipeline never calls Jev (or any model) directly. It builds `Question`s and
hands them to a `DecisionProvider`, which returns one `Decision` per question.
Swapping Jev for an LLM, a local model or "abstain and ask a human" is a config change.
"""

import hashlib
import json
from dataclasses import dataclass, field
from typing import Literal, Protocol

QuestionType = Literal["choice", "yes_no"]


@dataclass(frozen=True)
class Question:
    key: str                      # caller's id for this question, e.g. "map:cust_mail"
    type: QuestionType
    state: dict                   # the evidence the decision is about
    instructions: str             # the question, in plain words
    options: dict[str, str] = field(default_factory=dict)  # choice only: option -> description

    def input_hash(self) -> str:
        payload = json.dumps(
            {"t": self.type, "s": self.state, "i": self.instructions, "o": self.options},
            sort_keys=True, default=str,
        )
        return hashlib.sha256(payload.encode()).hexdigest()


@dataclass
class Decision:
    key: str
    answer: str | None            # chosen option, "yes"/"no", or None when the provider abstains
    confidence: float             # 0..1; 0 means "no opinion"
    probabilities: dict[str, float] = field(default_factory=dict)
    provider: str = ""
    latency_ms: float = 0.0
    error: str | None = None

    @property
    def abstained(self) -> bool:
        return self.answer is None


@dataclass
class CallUsage:
    """One request to a decision model, as the provider reported it.

    Token counts are None when the provider did not report them; they are never estimated.
    """

    provider: str
    model: str
    questions: int
    latency_ms: float
    input_tokens: int | None = None
    output_tokens: int | None = None
    error: str | None = None


class DecisionProvider(Protocol):
    name: str
    usage: list[CallUsage]       # one entry per request actually sent (cache hits add nothing)

    def decide(self, questions: list[Question]) -> list[Decision]:
        """Answer every question; must return one Decision per question, in order.
        Must never raise for a model failure: return an abstaining Decision with `error` set."""
        ...
