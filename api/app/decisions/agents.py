"""Agent resolver: a second look at the decisions one model call left uncertain.

The base provider (Gemini) answers every question in one batched call. Most answers
are confident and go through the usual threshold gate untouched. For the rest, two
agents take a closer look, one question at a time:

  investigator  may call tools on the uploaded file (every column with samples, one
                column's full value counts, a HubSpot property's definition) before it
                answers with a choice, a confidence and the evidence it relied on
  verifier      sees the question, the candidate answer and the tool results, and
                accepts or rejects it; it is told to reject anything the evidence
                doesn't support

An accepted answer replaces the base decision, with the lower of the two confidences,
and still has to clear the pipeline's threshold. A rejected answer, a step limit, an
invalid choice or any model failure keeps the base decision unchanged, so an
uncertain decision still goes to a person: the agents can only add automation they
can justify, never remove a review.

The graph (LangGraph) is plain routing code; the model never decides how many steps
run. Limits per run: `max_steps` tool calls per question and `max_escalations`
questions, so a bad file can't turn into an unbounded bill.

Only column mappings and value normalizations are escalated. Duplicate pairs keep the
base decision: the agents have nothing to look up for two rows beyond the rows.
"""

import json
import logging
import time
from dataclasses import dataclass
from typing import Protocol, TypedDict

import httpx
from langgraph.graph import END, START, StateGraph

from app.decisions.base import CallUsage, Decision, DecisionProvider, Question
from app.mapping.target_schema import BY_NAME

log = logging.getLogger(__name__)

API_ROOT = "https://generativelanguage.googleapis.com/v1beta/models"
ESCALATED_PREFIXES = ("map:", "val:")

INVESTIGATOR_PROMPT = """You investigate one uncertain decision in a CRM data-migration tool:
which HubSpot contact property a source column belongs to, or which option a source value means.
You can look things up before answering. Reply with JSON only, one of:
  {"action": "tool", "tool": "<name>", "args": {...}}
  {"action": "answer", "choice": "<an offered option>", "confidence": <0..1>, "evidence": "<the support>"}
Tools:
  list_columns {}                      every column in the file: type and a few sample values
  column_values {"column": "<name>"}   one column's distinct values with counts, and how many are empty
  property {"name": "<property>"}      a HubSpot property's description, synonyms and options
Look up what you need, then answer. "confidence" is your probability that the choice is correct;
keep it low when the evidence is thin, because a confident wrong answer is imported unchecked."""

VERIFIER_PROMPT = """You check another agent's answer to one decision in a CRM data-migration tool.
You get the question, the offered options, the candidate answer and the tool results it was based on.
Accept only if the evidence clearly supports the candidate over every other option; otherwise reject.
A rejected answer goes to a human reviewer, which is cheap; a wrong accepted one is imported unchecked.
Reply with JSON only: {"verdict": "accept" | "reject", "confidence": <0..1>, "reason": "<one sentence>"}"""


@dataclass
class ChatReply:
    text: str
    model: str
    latency_ms: float
    input_tokens: int | None = None
    output_tokens: int | None = None


class ChatModel(Protocol):
    def complete(self, system: str, user: str) -> ChatReply:
        """One JSON-mode completion. May raise: the resolver turns any failure into
        'keep the base decision'."""
        ...


class GeminiChat:
    """Single JSON completions from Gemini, for the agents."""

    def __init__(
        self,
        api_key: str,
        model: str = "gemini-3.5-flash-lite",
        timeout_s: float = 30.0,
        client: httpx.Client | None = None,
    ):
        if not api_key:
            raise ValueError("GeminiChat needs GEMINI_API_KEY")
        self.model = model
        self._url = f"{API_ROOT}/{model}:generateContent"
        self._client = client or httpx.Client(timeout=timeout_s, headers={"x-goog-api-key": api_key})

    def complete(self, system: str, user: str) -> ChatReply:
        start = time.perf_counter()
        resp = self._client.post(
            self._url,
            json={
                "systemInstruction": {"parts": [{"text": system}]},
                "contents": [{"role": "user", "parts": [{"text": user}]}],
                "generationConfig": {"temperature": 0, "responseMimeType": "application/json"},
            },
        )
        resp.raise_for_status()
        data = resp.json()
        meta = data.get("usageMetadata") or {}
        prompt, output = meta.get("promptTokenCount"), meta.get("candidatesTokenCount")
        return ChatReply(
            text=data["candidates"][0]["content"]["parts"][0]["text"],
            model=data.get("modelVersion") or self.model,
            latency_ms=(time.perf_counter() - start) * 1000,
            input_tokens=prompt if output is not None else None,
            output_tokens=(output + (meta.get("thoughtsTokenCount") or 0))
            if prompt is not None and output is not None
            else None,
        )


class FileTools:
    """Read-only lookups on one uploaded file's profile. Never raises: a bad call returns an error message."""

    def __init__(self, profile: dict | None):
        self._columns = {c["name"]: c for c in (profile or {}).get("columns", [])}

    def run(self, tool: str, args: dict) -> dict:
        if tool == "list_columns":
            return {
                "columns": [
                    {"name": c["name"], "type": c["inferred_type"], "samples": c["samples"][:3]}
                    for c in self._columns.values()
                ]
            }
        if tool == "column_values":
            col = self._columns.get(str(args.get("column", "")))
            if col is None:
                return {"error": f"no column named {args.get('column')!r}"}
            return {
                "column": col["name"],
                "values": col.get("value_counts") or {v: None for v in col["samples"]},
                "empty": col.get("nulls", 0),
                "distinct": col.get("distinct"),
            }
        if tool == "property":
            prop = BY_NAME.get(str(args.get("name", "")))
            if prop is None:
                return {"error": f"no HubSpot property named {args.get('name')!r}"}
            return {
                "name": prop.name,
                "description": prop.description,
                "synonyms": list(prop.synonyms),
                "options": list(prop.options),
            }
        return {"error": f"unknown tool {tool!r}"}


class TurnState(TypedDict, total=False):
    question: Question
    transcript: list[dict]  # tool calls and their results, oldest first
    steps: int
    candidate: dict | None  # the investigator's answer
    verdict: dict | None  # the verifier's ruling
    failed: str | None  # why the agents gave up, if they did


class AgentResolver:
    """A DecisionProvider: `base` answers everything; the agents re-examine what it was unsure about."""

    name = "agents"

    def __init__(
        self,
        base: DecisionProvider,
        model: ChatModel,
        *,
        escalate_below: float = 0.90,
        max_steps: int = 4,
        max_escalations: int = 30,
    ):
        self.base = base
        self.chat = model
        self.model = getattr(base, "model", None)   # reported with results, like other providers
        self.escalate_below = escalate_below
        self.max_steps = max_steps
        self.max_escalations = max_escalations
        self.agent_usage: list[CallUsage] = []
        self.trace: list[dict] = []  # one entry per escalated question, for the eval report
        self._tools = FileTools(None)
        self._escalations = 0
        self._resolved: dict[
            str, Decision
        ] = {}  # by question input: a re-run (threshold sweep) costs nothing
        self._graph = self._build_graph()

    @property
    def usage(self) -> list[CallUsage]:
        return self.base.usage + self.agent_usage

    def set_context(self, profile: dict) -> None:
        """The file the next questions are about; the pipeline calls this before deciding."""
        self._tools = FileTools(profile)

    def decide(self, questions: list[Question]) -> list[Decision]:
        decisions = self.base.decide(questions)
        out = []
        for q, d in zip(questions, decisions, strict=True):
            if not (
                q.key.startswith(ESCALATED_PREFIXES)
                and d.error is None
                and d.confidence < self.escalate_below
            ):
                out.append(d)
                continue
            cached = self._resolved.get(q.input_hash())
            if cached is not None:
                d = Decision(**{**cached.__dict__, "key": q.key})
            elif self._escalations < self.max_escalations:
                self._escalations += 1
                d = self._resolve(q, d)
                self._resolved[q.input_hash()] = d
            out.append(d)
        return out

    # -- the graph ------------------------------------------------------------
    def _build_graph(self):
        g = StateGraph(TurnState)
        g.add_node("investigate", self._investigate)
        g.add_node("verify", self._verify)
        g.add_edge(START, "investigate")
        g.add_conditional_edges(
            "investigate",
            self._after_investigate,
            {"investigate": "investigate", "verify": "verify", "end": END},
        )
        g.add_edge("verify", END)
        return g.compile()

    def _after_investigate(self, state: TurnState) -> str:
        if state.get("failed"):
            return "end"
        if state.get("candidate") is not None:
            return "verify"
        return "investigate"

    def _investigate(self, state: TurnState) -> TurnState:
        q = state["question"]
        reply = self._ask(
            "investigator",
            INVESTIGATOR_PROMPT,
            json.dumps(
                {
                    "question": q.instructions,
                    "evidence": q.state,
                    "options": q.options,
                    "tool_results_so_far": state["transcript"],
                    "tool_calls_left": max(0, self.max_steps - state["steps"]),
                },
                default=str,
            ),
        )
        if reply is None:
            return {"failed": "investigator call failed"}
        if reply.get("action") == "tool" and state["steps"] < self.max_steps:
            tool, args = str(reply.get("tool", "")), reply.get("args") or {}
            result = self._tools.run(tool, args if isinstance(args, dict) else {})
            return {
                "transcript": state["transcript"] + [{"tool": tool, "args": args, "result": result}],
                "steps": state["steps"] + 1,
            }
        if reply.get("action") == "answer":
            return {"candidate": reply}
        if reply.get("action") == "tool":
            return {"failed": f"no answer within {self.max_steps} tool calls"}
        return {"failed": "investigator reply was neither a tool call nor an answer"}

    def _verify(self, state: TurnState) -> TurnState:
        q, cand = state["question"], state["candidate"] or {}
        reply = self._ask(
            "verifier",
            VERIFIER_PROMPT,
            json.dumps(
                {
                    "question": q.instructions,
                    "evidence": q.state,
                    "options": q.options,
                    "candidate": {"choice": cand.get("choice"), "evidence": cand.get("evidence")},
                    "tool_results": state["transcript"],
                },
                default=str,
            ),
        )
        if reply is None:
            return {"failed": "verifier call failed"}
        return {"verdict": reply}

    # -- helpers ----------------------------------------------------------------
    def _ask(self, role: str, system: str, user: str) -> dict | None:
        try:
            reply = self.chat.complete(system, user)
        except Exception as exc:  # any model failure means: keep the base decision
            err = f"{type(exc).__name__}"
            log.warning("agent %s call failed: %s", role, err)
            self.agent_usage.append(
                CallUsage(self.name, getattr(self.chat, "model", "?"), 1, 0.0, error=err)
            )
            return None
        self.agent_usage.append(
            CallUsage(self.name, reply.model, 1, reply.latency_ms, reply.input_tokens, reply.output_tokens)
        )
        try:
            data = json.loads(reply.text)
        except ValueError:
            return None
        return data if isinstance(data, dict) else None

    def _resolve(self, q: Question, base: Decision) -> Decision:
        state = self._graph.invoke(
            {"question": q, "transcript": [], "steps": 0, "candidate": None, "verdict": None, "failed": None}
        )
        decision, outcome = base, state.get("failed") or "rejected by the verifier"
        cand, verdict = state.get("candidate") or {}, state.get("verdict") or {}
        if not state.get("failed") and str(verdict.get("verdict", "")).lower() == "accept":
            parsed = _valid_answer(q, cand, verdict)
            if parsed is None:
                outcome = "invalid choice or confidence"
            else:
                choice, confidence = parsed
                decision = Decision(key=q.key, answer=choice, confidence=confidence, provider=self.name)
                outcome = "accepted"
        self.trace.append(
            {
                "key": q.key,
                "base": {"answer": base.answer, "confidence": base.confidence},
                "tools": [t["tool"] for t in state.get("transcript", [])],
                "candidate": cand.get("choice"),
                "outcome": outcome,
                "final": {"answer": decision.answer, "confidence": decision.confidence},
            }
        )
        return decision


def _valid_answer(q: Question, cand: dict, verdict: dict) -> tuple[str, float] | None:
    choice = str(cand.get("choice", "")).strip()
    allowed = q.options if q.type == "choice" else {"yes": "", "no": ""}
    try:
        confidence = min(float(cand["confidence"]), float(verdict["confidence"]))
    except (KeyError, TypeError, ValueError):
        return None
    if choice not in allowed or not 0.0 <= confidence <= 1.0:
        return None
    return choice, confidence
