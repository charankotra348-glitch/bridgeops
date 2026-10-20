from app.decisions.abstain import AbstainProvider
from app.decisions.base import Decision, Question
from app.dedupe import find_duplicates, norm_phone

COLS = {"mail": "email", "first": "firstname", "last": "lastname", "tel": "phone", "org": "company"}


def row(mail="", first="", last="", tel="", org=""):
    return {"mail": mail, "first": first, "last": last, "tel": tel, "org": org}


class ScriptedProvider:
    """Answers yes/no duplicate questions from a table keyed by row pair."""

    name = "fake"

    def __init__(self, answers: dict[tuple[int, int], tuple[str, float]]):
        self.answers, self.usage, self.asked = answers, [], []

    def decide(self, questions: list[Question]) -> list[Decision]:
        out = []
        for q in questions:
            _, i, j = q.key.split(":")
            self.asked.append((int(i), int(j)))
            ans, conf = self.answers.get((int(i), int(j)), (None, 0.0))
            out.append(Decision(q.key, ans, conf, provider=self.name))
        return out


def test_phone_normalization_ignores_formatting_and_country_code():
    assert norm_phone("+1 (631) 555-0142") == norm_phone("631.555.0142") == "6315550142"
    assert norm_phone("N/A") == ""


def test_same_email_is_a_certain_duplicate_case_insensitive():
    rows = [row("Priya@Acme.com ", "Priya", "Rao"), row("x@y.com", "Jo", "Lee"),
            row("priya@acme.com", "P", "Rao")]
    res = find_duplicates(rows, COLS, AbstainProvider())
    (g,) = res.groups
    assert g.rows == [0, 2] and g.method == "rule" and g.confidence == 1.0 and g.status == "auto"


def test_phone_and_last_name_rule_and_transitive_grouping():
    rows = [row("a@x.com", "Sam", "Okafor", "516-555-0123"),
            row("sam@home.net", "Samuel", "Okafor", "+1 516 555 0123"),
            row("sam@home.net", "S", "Okafor")]
    (g,) = find_duplicates(rows, COLS, AbstainProvider()).groups
    assert g.rows == [0, 1, 2]
    assert g.confidence == 0.90 and g.reason == "same phone number and last name"  # weakest link


def test_same_phone_different_last_name_is_not_a_duplicate():
    rows = [row("a@x.com", "Ann", "Lee", "5165550123"), row("b@x.com", "Bo", "Kim", "5165550123")]
    assert find_duplicates(rows, COLS, AbstainProvider()).groups == []


def test_similar_names_go_to_the_provider_and_are_gated():
    rows = [row("bob@acme.com", "Bob", "Smith", org="Acme"),
            row("robert.smith@gmail.com", "Robert", "Smith", org="Acme"),
            row("ann@acme.com", "Ann", "Patel", org="Acme"),
            row("ann.patel@globex.com", "Ann", "Patel", org="Globex")]
    provider = ScriptedProvider({(0, 1): ("yes", 0.92), (2, 3): ("no", 0.60)})
    res = find_duplicates(rows, COLS, provider)
    by_rows = {tuple(g.rows): g for g in res.groups}

    assert by_rows[(0, 1)].same_person is True and by_rows[(0, 1)].status == "auto"
    assert by_rows[(2, 3)].same_person is False and by_rows[(2, 3)].status == "pending"
    assert res.counts()["duplicate_groups_auto"] == 1      # a "different people" answer is not a duplicate
    q = res.questions[0][0]
    assert q.type == "yes_no" and q.state["record_a"]["firstname"] == "Bob"


def test_pairs_settled_by_rules_are_not_asked():
    rows = [row("bob@acme.com", "Bob", "Smith", org="Acme"), row("BOB@acme.com", "Bob", "Smith", org="Acme")]
    provider = ScriptedProvider({})
    find_duplicates(rows, COLS, provider)
    assert provider.asked == []


def test_rules_only_sends_similar_names_to_review():
    rows = [row("a@x.com", "Li", "Wang", org="Initech"), row("b@y.com", "Li", "Wang", org="Initech")]
    (g,) = find_duplicates(rows, COLS, AbstainProvider()).groups
    assert g.status == "pending" and g.same_person is None and g.method == "none"


def test_unmapped_columns_are_ignored():
    rows = [{"mail": "a@x.com", "ref": "1"}, {"mail": "b@x.com", "ref": "1"}]
    assert find_duplicates(rows, {"mail": "email"}, AbstainProvider()).groups == []
