import json

import httpx

from app.decisions.agents import AgentResolver, ChatReply, FileTools, GeminiChat
from app.decisions.base import CallUsage, Decision, Question
from app.mapping.pipeline import MAPPING_OPTIONS, plan_mappings

STAGES = {
    "lead": "Showed interest",
    "customer": "Paying",
    "other": "Former or lost",
    "opportunity": "In a deal",
}
PROFILE = {
    "columns": [
        {
            "name": "CUST_STAT",
            "inferred_type": "string",
            "samples": ["ACTV", "PROSP"],
            "nulls": 0,
            "distinct": 2,
            "value_counts": {"ACTV": 4, "PROSP": 2},
        },
        {
            "name": "COMPANY_NM",
            "inferred_type": "string",
            "samples": ["Maple Rock"],
            "nulls": 0,
            "distinct": 6,
            "value_counts": {},
        },
    ]
}


class ScriptedBase:
    """Base provider answering every question with a fixed decision."""

    name = "gemini"

    def __init__(self, answers: dict[str, tuple[str | None, float]], error: str | None = None):
        self.answers, self.error = answers, error
        self.usage = [CallUsage("gemini", "gemini-3.5-flash-lite", 1, 10.0, 100, 10)]
        self.calls = 0

    def decide(self, questions):
        self.calls += 1
        return [Decision(q.key, *self.answers[q.key], provider="gemini", error=self.error) for q in questions]


class ScriptedModel:
    """Chat model that returns the next scripted reply (a dict, a raw string, or an exception)."""

    model = "gemini-3.5-flash-lite"

    def __init__(self, replies):
        self.replies = list(replies)
        self.prompts = []

    def complete(self, system, user):
        self.prompts.append((system, json.loads(user)))
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        text = reply if isinstance(reply, str) else json.dumps(reply)
        return ChatReply(text=text, model=self.model, latency_ms=5.0, input_tokens=200, output_tokens=20)


def value_q(value="PROSP"):
    return Question(
        f"val:CUST_STAT:{value}",
        "choice",
        {"source_column": "CUST_STAT", "source_value": value},
        "Which Lifecycle stage option best matches this source value?",
        STAGES,
    )


def resolver(base, model, **kw):
    r = AgentResolver(base, model, **kw)
    r.set_context(PROFILE)
    return r


def test_confident_answers_are_not_escalated():
    model = ScriptedModel([])
    r = resolver(ScriptedBase({"val:CUST_STAT:PROSP": ("opportunity", 0.95)}), model)
    (d,) = r.decide([value_q()])
    assert (d.answer, d.confidence, d.provider) == ("opportunity", 0.95, "gemini")
    assert model.prompts == [] and r.trace == []


def test_investigator_uses_a_tool_and_an_accepted_answer_replaces_the_base_decision():
    model = ScriptedModel(
        [
            {"action": "tool", "tool": "column_values", "args": {"column": "CUST_STAT"}},
            {
                "action": "answer",
                "choice": "opportunity",
                "confidence": 0.93,
                "evidence": "PROSP next to ACTV",
            },
            {"verdict": "accept", "confidence": 0.91, "reason": "prospect is an open deal"},
        ]
    )
    r = resolver(ScriptedBase({"val:CUST_STAT:PROSP": ("lead", 0.80)}), model)
    (d,) = r.decide([value_q()])
    assert (d.answer, d.confidence, d.provider) == ("opportunity", 0.91, "agents")  # the lower confidence
    # the investigator's second turn saw the tool result; the verifier saw the candidate and the evidence
    assert model.prompts[1][1]["tool_results_so_far"][0]["result"]["values"] == {"ACTV": 4, "PROSP": 2}
    assert model.prompts[2][1]["candidate"]["choice"] == "opportunity"
    assert r.trace[0]["outcome"] == "accepted" and r.trace[0]["tools"] == ["column_values"]
    assert [u.provider for u in r.usage] == ["gemini", "agents", "agents", "agents"]


def test_rejected_answer_keeps_the_base_decision():
    model = ScriptedModel(
        [
            {"action": "answer", "choice": "customer", "confidence": 0.9, "evidence": "guess"},
            {"verdict": "reject", "confidence": 0.3, "reason": "nothing says they pay"},
        ]
    )
    r = resolver(ScriptedBase({"val:CUST_STAT:PROSP": ("lead", 0.80)}), model)
    (d,) = r.decide([value_q()])
    assert (d.answer, d.confidence, d.provider) == ("lead", 0.80, "gemini")
    assert r.trace[0]["outcome"] == "rejected by the verifier"


def test_step_limit_stops_the_investigator_and_keeps_the_base_decision():
    model = ScriptedModel([{"action": "tool", "tool": "list_columns", "args": {}}] * 3)
    r = resolver(ScriptedBase({"val:CUST_STAT:PROSP": ("lead", 0.80)}), model, max_steps=2)
    (d,) = r.decide([value_q()])
    assert d.answer == "lead" and d.provider == "gemini"
    assert len(model.prompts) == 3  # two tool calls, then one turn that still didn't answer
    assert model.prompts[2][1]["tool_calls_left"] == 0
    assert r.trace[0]["outcome"] == "no answer within 2 tool calls"


def test_choice_outside_the_options_is_not_accepted():
    model = ScriptedModel(
        [
            {"action": "answer", "choice": "prospect", "confidence": 0.95, "evidence": "x"},
            {"verdict": "accept", "confidence": 0.95, "reason": "x"},
        ]
    )
    r = resolver(ScriptedBase({"val:CUST_STAT:PROSP": ("lead", 0.80)}), model)
    (d,) = r.decide([value_q()])
    assert d.answer == "lead" and r.trace[0]["outcome"] == "invalid choice or confidence"


def test_model_failures_never_raise_and_are_recorded():
    model = ScriptedModel([httpx.ConnectError("down"), "not json at all"])
    r = resolver(
        ScriptedBase({"val:CUST_STAT:PROSP": ("lead", 0.80), "val:CUST_STAT:ACTV": ("customer", 0.7)}), model
    )
    d1, d2 = r.decide([value_q(), value_q("ACTV")])
    assert (d1.answer, d2.answer) == ("lead", "customer")
    assert r.agent_usage[0].error == "ConnectError" and r.agent_usage[1].error is None
    assert [t["outcome"] for t in r.trace] == ["investigator call failed", "investigator call failed"]


def test_base_errors_duplicates_and_the_escalation_cap_are_left_alone():
    dup = Question("dup:0:1", "yes_no", {"record_a": {}, "record_b": {}}, "Same person?")
    answers = {
        "dup:0:1": ("yes", 0.5),
        "val:CUST_STAT:PROSP": ("lead", 0.8),
        "val:CUST_STAT:ACTV": ("customer", 0.8),
    }
    model = ScriptedModel(
        [
            {"action": "answer", "choice": "opportunity", "confidence": 0.95, "evidence": "x"},
            {"verdict": "accept", "confidence": 0.95, "reason": "x"},
        ]
    )
    r = resolver(ScriptedBase(answers), model, max_escalations=1)
    d_dup, d1, d2 = r.decide([dup, value_q(), value_q("ACTV")])
    assert (
        d_dup.provider == "gemini" and d2.provider == "gemini"
    )  # duplicates and the second escalation: base
    assert d1.provider == "agents" and len(r.trace) == 1

    failing = resolver(ScriptedBase({"val:CUST_STAT:PROSP": (None, 0.0)}, error="timeout"), ScriptedModel([]))
    (d,) = failing.decide([value_q()])
    assert d.error == "timeout" and failing.trace == []  # a failed base call is not papered over


def test_a_repeated_question_is_not_escalated_twice():
    model = ScriptedModel(
        [
            {"action": "answer", "choice": "opportunity", "confidence": 0.95, "evidence": "x"},
            {"verdict": "accept", "confidence": 0.93, "reason": "x"},
        ]
    )
    r = resolver(ScriptedBase({"val:CUST_STAT:PROSP": ("lead", 0.80)}), model)
    first = r.decide([value_q()])[0]
    again = r.decide([value_q()])[0]  # e.g. the eval's threshold sweep re-planning the same file
    assert (first.answer, again.answer, again.confidence) == ("opportunity", "opportunity", 0.93)
    assert len(model.prompts) == 2 and len(r.trace) == 1


def test_tools_answer_from_the_profile_and_report_bad_calls():
    tools = FileTools(PROFILE)
    assert [c["name"] for c in tools.run("list_columns", {})["columns"]] == ["CUST_STAT", "COMPANY_NM"]
    assert tools.run("column_values", {"column": "COMPANY_NM"})["values"] == {"Maple Rock": None}
    assert "error" in tools.run("column_values", {"column": "missing"})
    assert tools.run("property", {"name": "lifecyclestage"})["options"][0] == "subscriber"
    assert "error" in tools.run("property", {"name": "favorite_color"})
    assert "error" in tools.run("rm_rf", {})


def test_pipeline_gives_the_resolver_the_whole_file():
    seen = []

    class Recorder(ScriptedBase):
        def set_context(self, profile):
            seen.append(profile)

    base = Recorder({f"map:{c['name']}": ("none", 0.5) for c in PROFILE["columns"]})
    plan_mappings(PROFILE, base)
    assert seen == [PROFILE]
    assert "lifecyclestage" in MAPPING_OPTIONS


def test_gemini_chat_reports_tokens_and_raises_for_errors():
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        assert body["generationConfig"]["responseMimeType"] == "application/json"
        return httpx.Response(
            200,
            json={
                "candidates": [{"content": {"parts": [{"text": '{"verdict": "accept"}'}]}}],
                "usageMetadata": {
                    "promptTokenCount": 300,
                    "candidatesTokenCount": 12,
                    "thoughtsTokenCount": 3,
                },
            },
        )

    chat = GeminiChat("k", client=httpx.Client(transport=httpx.MockTransport(handler)))
    reply = chat.complete("system", "{}")
    assert (reply.text, reply.input_tokens, reply.output_tokens) == ('{"verdict": "accept"}', 300, 15)

    down = GeminiChat("k", client=httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(503))))
    try:
        down.complete("s", "{}")
    except httpx.HTTPStatusError:
        pass
    else:
        raise AssertionError("expected an HTTP error")
