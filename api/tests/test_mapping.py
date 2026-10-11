import httpx

from app.decisions.abstain import AbstainProvider
from app.decisions.base import Decision, Question
from app.decisions.jev import JevProvider
from app.mapping import rules
from app.mapping.pipeline import plan_mappings
from app.profiling import profile_file
from tests.conftest import MESSY_CSV


def test_rules_map_known_names():
    cases = {
        ("cust_mail", "email"): "email",
        ("FName", "string"): "firstname",
        ("surname", "string"): "lastname",
        ("tel_no", "phone"): "phone",
        ("org", "string"): "company",
        ("designation", "string"): "jobtitle",
        ("acct_status", "string"): "lifecyclestage",
        ("Postal Code", "string"): "zip",
        ("CompanyWebsite", "url"): "website",
    }
    for (name, typ), expected in cases.items():
        assert rules.map_column(name, typ).target == expected, name


def test_rules_refuse_type_conflicts():
    # A Y/N "email_opt_in" column must not be mapped to email.
    r = rules.map_column("email_opt_in", "boolean")
    assert r.target is None and "needs review" in r.reason


def test_conflicts_demote_the_weaker_column():
    out = rules.resolve_conflicts([
        rules.RuleResult("email", "email", 0.97, ""),
        rules.RuleResult("backup_contact", "email", 0.82, ""),
    ])
    assert out[0].target == "email" and out[1].target is None


def test_enum_value_rules():
    assert rules.map_enum_value("lifecyclestage", "PAYING")[0] == "customer"
    assert rules.map_enum_value("lifecyclestage", "churned 2024")[0] == "other"
    assert rules.map_enum_value("lifecyclestage", "Active-ish")[0] is None  # left for the model


def test_rules_only_plan_sends_unknowns_to_review():
    profile = profile_file(MESSY_CSV.encode(), "x.csv").to_dict()
    plan = plan_mappings(profile, AbstainProvider())
    by_col = {c.column: c for c in plan.columns}
    assert by_col["cust_mail"].target == "email" and by_col["cust_mail"].status == "auto"
    assert by_col["internal_ref"].status == "pending"
    status_map = by_col["acct_status"].value_map
    assert status_map["PAYING"].target == "customer"
    assert status_map["Active-ish"].status == "pending"


class FakeProvider:
    """Stands in for Jev: answers from a lookup table with a fixed confidence."""

    name = "fake"

    def __init__(self, answers: dict[str, tuple[str, float]]):
        self.answers = answers

    def decide(self, questions: list[Question]) -> list[Decision]:
        out = []
        for q in questions:
            ans, conf = self.answers.get(q.key, (None, 0.0))
            out.append(Decision(q.key, ans, conf, provider=self.name))
        return out


def test_provider_fills_gaps_with_gating():
    profile = profile_file(MESSY_CSV.encode(), "x.csv").to_dict()
    provider = FakeProvider({
        "map:internal_ref": ("none", 0.93),
        "map:email_opt_in": ("none", 0.60),          # below threshold -> pending
        "val:acct_status:Active-ish": ("customer", 0.91),
        "val:acct_status:hot prospect": ("opportunity", 0.70),
    })
    plan = plan_mappings(profile, provider)
    by_col = {c.column: c for c in plan.columns}
    assert by_col["internal_ref"].status == "auto" and by_col["internal_ref"].target is None
    assert by_col["email_opt_in"].status == "pending"
    vm = by_col["acct_status"].value_map
    assert vm["Active-ish"].target == "customer" and vm["Active-ish"].status == "auto"
    assert vm["hot prospect"].status == "pending"


def test_jev_provider_batches_and_parses():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        import json
        body = json.loads(request.content)
        seen["body"] = body
        return httpx.Response(200, json={"answers": {
            "q0": {"type": "choice", "choice": "email", "confidence": 0.95,
                   "probabilities": {"email": 0.97}},
            "q1": {"type": "noul", "noul": 0.1},
            "q2": {"type": "choice", "choice": "not-an-option", "confidence": 0.99},
        }})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    jev = JevProvider("test-key", "https://jev.test/v1/systemone", client=client)
    qs = [
        Question("a", "choice", {"name": "cust_mail"}, "Which property?", {"email": "", "none": ""}),
        Question("b", "yes_no", {"a": 1, "b": 2}, "Same company?"),
        Question("c", "choice", {"name": "x"}, "Which property?", {"email": "", "none": ""}),
    ]
    a, b, c = jev.decide(qs)
    assert set(seen["body"]["questions"]) == {"q0", "q1", "q2"}  # one request for all three
    assert a.answer == "email" and a.confidence == 0.95
    assert b.answer == "no" and round(b.confidence, 2) == 0.8
    assert c.abstained and "unexpected option" in c.error  # never trust an out-of-set answer


def test_jev_provider_fails_soft():
    client = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(503)))
    jev = JevProvider("k", "https://jev.test/v1/systemone", client=client)
    (d,) = jev.decide([Question("a", "choice", {}, "?", {"x": ""})])
    assert d.abstained and d.error
