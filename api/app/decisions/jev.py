"""Jev (TypeSafe System One) provider.

All questions in a call share one state, so a batch is sent as
state = {"q0": <state of question 0>, "q1": ...} with each question's
instructions pointing at its own slot (`q0`, `q1`, ...). One HTTP request
answers the whole batch.
"""

import logging
import time

import httpx

from app.decisions.base import Decision, Question

log = logging.getLogger(__name__)

MAX_QUESTIONS_PER_CALL = 50


class JevProvider:
    name = "jev"

    def __init__(self, api_key: str, url: str, model: str = "jev-latest", timeout_s: float = 10.0,
                 client: httpx.Client | None = None):
        if not api_key:
            raise ValueError("JevProvider needs TYPESAFE_API_KEY")
        self.url = url
        self.model = model
        self._client = client or httpx.Client(
            timeout=timeout_s, headers={"Authorization": f"Bearer {api_key}"}
        )
        self._cache: dict[str, Decision] = {}

    # -- public -------------------------------------------------------------
    def decide(self, questions: list[Question]) -> list[Decision]:
        results: dict[str, Decision] = {}
        todo = []
        for q in questions:
            cached = self._cache.get(q.input_hash())
            if cached:
                results[q.key] = Decision(**{**cached.__dict__, "key": q.key, "latency_ms": 0.0})
            else:
                todo.append(q)

        for start in range(0, len(todo), MAX_QUESTIONS_PER_CALL):
            for d in self._call(todo[start:start + MAX_QUESTIONS_PER_CALL]):
                results[d.key] = d

        return [results[q.key] for q in questions]

    # -- internals ----------------------------------------------------------
    def _call(self, batch: list[Question]) -> list[Decision]:
        state = {f"q{i}": q.state for i, q in enumerate(batch)}
        body_questions = {}
        for i, q in enumerate(batch):
            instructions = f"About `q{i}`: {q.instructions}"
            if q.type == "choice":
                body_questions[f"q{i}"] = {"type": "choice", "instructions": instructions,
                                           "criteria": q.options}
            else:
                body_questions[f"q{i}"] = {"type": "noul", "instructions": instructions}

        start = time.perf_counter()
        try:
            resp = self._client.post(self.url, json={"model": self.model, "state": state,
                                                     "questions": body_questions})
            resp.raise_for_status()
            data = resp.json()
        except (httpx.HTTPError, ValueError) as exc:
            log.warning("Jev call failed: %s", exc)
            return [Decision(key=q.key, answer=None, confidence=0.0, provider=self.name, error=str(exc))
                    for q in batch]
        latency = (time.perf_counter() - start) * 1000 / max(len(batch), 1)

        answers = data.get("answers", data)
        out = []
        for i, q in enumerate(batch):
            out.append(self._parse(q, answers.get(f"q{i}") or {}, latency))
            if out[-1].error is None:
                self._cache[q.input_hash()] = out[-1]
        return out

    def _parse(self, q: Question, a: dict, latency_ms: float) -> Decision:
        try:
            if q.type == "choice":
                choice = a["choice"]
                if choice not in q.options:  # never trust an answer outside the offered options
                    raise ValueError(f"unexpected option {choice!r}")
                return Decision(key=q.key, answer=choice, confidence=float(a.get("confidence", 0.0)),
                                probabilities=a.get("probabilities", {}), provider=self.name,
                                latency_ms=latency_ms)
            p_yes = float(a["noul"])
            answer = "yes" if p_yes >= 0.5 else "no"
            return Decision(key=q.key, answer=answer, confidence=abs(p_yes - 0.5) * 2,
                            probabilities={"yes": p_yes, "no": 1 - p_yes}, provider=self.name,
                            latency_ms=latency_ms)
        except (KeyError, TypeError, ValueError) as exc:
            return Decision(key=q.key, answer=None, confidence=0.0, provider=self.name,
                            latency_ms=latency_ms, error=f"unparseable answer: {exc}")
