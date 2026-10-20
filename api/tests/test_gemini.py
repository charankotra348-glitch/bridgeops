import json

import httpx

from app.decisions.base import Question
from app.decisions.gemini import GeminiProvider

OPTS = {"email": "Email address", "none": "Do not import"}


def gemini_reply(answers: list[dict], usage: dict | None = None) -> dict:
    reply = {"candidates": [{"content": {"parts": [{"text": json.dumps({"answers": answers})}]}}],
             "modelVersion": "gemini-3.5-flash-lite"}
    if usage is not None:
        reply["usageMetadata"] = usage
    return reply


def provider(handler, **kw) -> GeminiProvider:
    client = httpx.Client(transport=httpx.MockTransport(handler))
    return GeminiProvider("test-key", client=client, **kw)


def test_batches_validates_and_records_usage():
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(json.loads(request.content))
        return httpx.Response(200, json=gemini_reply(
            [{"id": "q0", "choice": "email", "confidence": 0.94},
             {"id": "q1", "choice": "no", "confidence": 0.7},
             {"id": "q2", "choice": "phone", "confidence": 0.99}],  # not an offered option
            usage={"promptTokenCount": 410, "candidatesTokenCount": 50, "thoughtsTokenCount": 12}))

    gem = provider(handler)
    a, b, c, d = gem.decide([
        Question("a", "choice", {"column_name": "cust_mail"}, "Which property?", OPTS),
        Question("b", "yes_no", {"x": 1}, "Same person?"),
        Question("c", "choice", {"column_name": "tel"}, "Which property?", OPTS),
        Question("d", "choice", {"column_name": "zz"}, "Which property?", OPTS),  # no answer returned
    ])

    assert len(seen) == 1  # one request for the whole batch
    sent = json.loads(seen[0]["contents"][0]["parts"][0]["text"])["questions"]
    assert [q["id"] for q in sent] == ["q0", "q1", "q2", "q3"]
    assert sent[0]["options"] == OPTS and "options" not in sent[1]
    assert seen[0]["generationConfig"]["temperature"] == 0

    assert a.answer == "email" and a.confidence == 0.94 and a.provider == "gemini"
    assert b.answer == "no"
    assert c.abstained and "unexpected option" in c.error  # never trust an out-of-set answer
    assert d.abstained and "no answer" in d.error

    (call,) = gem.usage
    assert call.questions == 4 and call.input_tokens == 410 and call.output_tokens == 62


def test_missing_usage_is_not_invented():
    gem = provider(lambda r: httpx.Response(200, json=gemini_reply([{"id": "q0", "choice": "none",
                                                                     "confidence": 0.9}])))
    gem.decide([Question("a", "choice", {}, "?", OPTS)])
    assert gem.usage[0].input_tokens is None and gem.usage[0].output_tokens is None


def test_cache_skips_repeat_questions():
    calls = []

    def handler(request):
        calls.append(1)
        return httpx.Response(200, json=gemini_reply([{"id": "q0", "choice": "email", "confidence": 0.9}]))

    gem = provider(handler)
    q = Question("a", "choice", {"column_name": "mail"}, "Which property?", OPTS)
    gem.decide([q])
    (again,) = gem.decide([Question("other-key", q.type, q.state, q.instructions, q.options)])
    assert len(calls) == 1 and len(gem.usage) == 1
    assert again.key == "other-key" and again.answer == "email"


def test_bad_confidence_abstains():
    gem = provider(lambda r: httpx.Response(200, json=gemini_reply([{"id": "q0", "choice": "email",
                                                                     "confidence": 7}])))
    (d,) = gem.decide([Question("a", "choice", {}, "?", OPTS)])
    assert d.abstained


def test_retries_once_then_succeeds(monkeypatch):
    monkeypatch.setattr("app.decisions.gemini.time.sleep", lambda s: None)
    replies = iter([httpx.Response(429),
                    httpx.Response(200, json=gemini_reply([{"id": "q0", "choice": "email",
                                                            "confidence": 0.9}]))])
    gem = provider(lambda r: next(replies))
    (d,) = gem.decide([Question("a", "choice", {}, "?", OPTS)])
    assert d.answer == "email"


def test_fails_soft_without_leaking_the_key(monkeypatch):
    monkeypatch.setattr("app.decisions.gemini.time.sleep", lambda s: None)
    gem = provider(lambda r: httpx.Response(503))
    (d,) = gem.decide([Question("a", "choice", {}, "?", OPTS)])
    assert d.abstained and d.error == "HTTP 503"
    assert gem.usage[0].error == "HTTP 503"


def test_malformed_json_fails_soft():
    bad = {"candidates": [{"content": {"parts": [{"text": "not json"}]}}]}
    gem = provider(lambda r: httpx.Response(200, json=bad))
    (d,) = gem.decide([Question("a", "choice", {}, "?", OPTS)])
    assert d.abstained and d.error.startswith("bad response")
