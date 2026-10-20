"""Gemini provider: the LLM baseline that Jev is benchmarked against.

A batch of questions goes out as one JSON request; the model must answer every
question with one of the offered options and a confidence. The pipeline treats
that confidence like any other provider's: it gates auto-acceptance, and the
thresholds are tuned on the eval set. It is the model's self-reported number,
not a calibrated probability, and the eval measures how well it holds up.

Like every provider this never raises for a model failure. Timeouts, HTTP
errors, malformed JSON, missing answers and options outside the offered set
all become abstaining decisions, which go to human review.
"""

import json
import logging
import time

import httpx

from app.decisions.base import CallUsage, Decision, Question

log = logging.getLogger(__name__)

API_ROOT = "https://generativelanguage.googleapis.com/v1beta/models"
MAX_QUESTIONS_PER_CALL = 25
RETRY_STATUSES = {429, 500, 502, 503, 504}

SYSTEM_PROMPT = """You are the decision step in a CRM data-migration tool.
Each question is about one column or one value in a customer's data export.
Answer every question, using only the evidence given with it.

Rules:
- "choice" questions: "choice" must be exactly one of that question's option keys.
- "yes_no" questions: "choice" must be "yes" or "no".
- If no option fits, and "none" is an option, choose "none".
- "confidence" is your probability, from 0 to 1, that your choice is correct.
  Use low values when the evidence is thin or ambiguous: a low-confidence
  answer goes to a human reviewer, a high-confidence one is imported unchecked.
Return JSON only."""

RESPONSE_SCHEMA = {
    "type": "OBJECT",
    "properties": {
        "answers": {
            "type": "ARRAY",
            "items": {
                "type": "OBJECT",
                "properties": {
                    "id": {"type": "STRING"},
                    "choice": {"type": "STRING"},
                    "confidence": {"type": "NUMBER"},
                },
                "required": ["id", "choice", "confidence"],
            },
        }
    },
    "required": ["answers"],
}


class GeminiProvider:
    name = "gemini"

    def __init__(self, api_key: str, model: str = "gemini-3.5-flash-lite", timeout_s: float = 30.0,
                 max_attempts: int = 2, client: httpx.Client | None = None):
        if not api_key:
            raise ValueError("GeminiProvider needs GEMINI_API_KEY")
        self.model = model
        self.max_attempts = max(1, max_attempts)
        self._url = f"{API_ROOT}/{model}:generateContent"
        self._client = client or httpx.Client(timeout=timeout_s, headers={"x-goog-api-key": api_key})
        self._cache: dict[str, Decision] = {}
        self.usage: list[CallUsage] = []

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
    def _body(self, batch: list[Question]) -> dict:
        items = []
        for i, q in enumerate(batch):
            item = {"id": f"q{i}", "type": q.type, "question": q.instructions, "evidence": q.state}
            if q.type == "choice":
                item["options"] = q.options
            items.append(item)
        return {
            "systemInstruction": {"parts": [{"text": SYSTEM_PROMPT}]},
            "contents": [{"role": "user", "parts": [{"text": json.dumps({"questions": items})}]}],
            "generationConfig": {
                "temperature": 0,
                "responseMimeType": "application/json",
                "responseSchema": RESPONSE_SCHEMA,
            },
        }

    def _post(self, body: dict) -> dict:
        """POST with a small retry on rate limits and server errors. Raises on final failure."""
        for attempt in range(1, self.max_attempts + 1):
            try:
                resp = self._client.post(self._url, json=body)
            except httpx.TransportError:
                if attempt == self.max_attempts:
                    raise
            else:
                if resp.status_code not in RETRY_STATUSES or attempt == self.max_attempts:
                    resp.raise_for_status()
                    return resp.json()
            time.sleep(min(2 ** attempt, 8))
        raise RuntimeError("unreachable")

    def _call(self, batch: list[Question]) -> list[Decision]:
        start = time.perf_counter()
        try:
            data = self._post(self._body(batch))
            text = data["candidates"][0]["content"]["parts"][0]["text"]
            answers = {a["id"]: a for a in json.loads(text)["answers"] if isinstance(a, dict) and "id" in a}
        except (httpx.HTTPError, ValueError, KeyError, IndexError, TypeError) as exc:
            call_ms = (time.perf_counter() - start) * 1000
            err = _describe(exc)
            log.warning("Gemini call failed: %s", err)
            self.usage.append(CallUsage(self.name, self.model, len(batch), call_ms, error=err))
            return [Decision(key=q.key, answer=None, confidence=0.0, provider=self.name, error=err)
                    for q in batch]

        call_ms = (time.perf_counter() - start) * 1000
        meta = data.get("usageMetadata") or {}
        self.usage.append(CallUsage(self.name, data.get("modelVersion") or self.model, len(batch), call_ms,
                                    **_token_counts(meta)))

        latency = call_ms / max(len(batch), 1)
        out = []
        for i, q in enumerate(batch):
            out.append(self._parse(q, answers.get(f"q{i}"), latency))
            if out[-1].error is None:
                self._cache[q.input_hash()] = out[-1]
        return out

    def _parse(self, q: Question, a: dict | None, latency_ms: float) -> Decision:
        try:
            if a is None:
                raise ValueError("no answer returned for this question")
            choice = str(a["choice"]).strip()
            allowed = q.options if q.type == "choice" else {"yes": "", "no": ""}
            if choice not in allowed:  # never trust an answer outside the offered options
                raise ValueError(f"unexpected option {choice!r}")
            confidence = float(a["confidence"])
            if not 0.0 <= confidence <= 1.0:
                raise ValueError(f"confidence {confidence} outside 0..1")
            return Decision(key=q.key, answer=choice, confidence=confidence, provider=self.name,
                            latency_ms=latency_ms)
        except (KeyError, TypeError, ValueError) as exc:
            return Decision(key=q.key, answer=None, confidence=0.0, provider=self.name,
                            latency_ms=latency_ms, error=f"unparseable answer: {exc}")


def _token_counts(meta: dict) -> dict:
    prompt, output = meta.get("promptTokenCount"), meta.get("candidatesTokenCount")
    if prompt is None or output is None:
        return {"input_tokens": None, "output_tokens": None}
    return {"input_tokens": prompt, "output_tokens": output + (meta.get("thoughtsTokenCount") or 0)}


def _describe(exc: Exception) -> str:
    """Error text that never includes the request (which carries the API key header)."""
    if isinstance(exc, httpx.HTTPStatusError):
        return f"HTTP {exc.response.status_code}"
    if isinstance(exc, httpx.TimeoutException):
        return "timeout"
    if isinstance(exc, httpx.HTTPError):
        return type(exc).__name__
    return f"bad response: {type(exc).__name__}: {exc}"
