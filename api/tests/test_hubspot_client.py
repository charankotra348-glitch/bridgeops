import httpx
import pytest

from app.hubspot import HubSpotError, RateLimiter
from tests.fake_hubspot import FakeHubSpot


def test_rate_limiter_waits_for_the_window():
    now = [0.0]
    waits = []

    def sleep(s):
        waits.append(s)
        now[0] += s

    lim = RateLimiter(3, 10.0, clock=lambda: now[0], sleep=sleep)
    for _ in range(3):
        assert lim.acquire() == 0.0
    now[0] = 4.0
    assert lim.acquire() == pytest.approx(6.0)   # 4th request waits until the first leaves the window
    assert waits == [pytest.approx(6.0)]


def test_upsert_creates_and_updates_by_email_in_batches():
    hs = FakeHubSpot()
    existing = hs.add(email="old@acme.com", firstname="Old")
    client = hs.client()
    records = {f"user{i}@acme.com": {"firstname": f"U{i}"} for i in range(150)}
    records["old@acme.com"] = {"firstname": "Renamed"}

    res = client.upsert_by_email(records)
    assert len(res.created) == 150 and res.updated == {"old@acme.com": existing} and not res.failed
    assert hs.calls.count("/crm/v3/objects/contacts/batch/upsert") == 2     # 151 records -> 100 + 51
    assert hs.contacts[existing]["firstname"] == "Renamed"


def test_one_bad_record_does_not_sink_the_batch():
    hs = FakeHubSpot()
    records = {f"u{i}@acme.com": {"firstname": f"U{i}"} for i in range(8)}
    records["u5@acme.com"] = {"lifecyclestage": "not-a-stage"}
    res = hs.client().upsert_by_email(records)
    assert set(res.failed) == {"u5@acme.com"} and "allowed options" in res.failed["u5@acme.com"]
    assert len(res.created) == 7


def test_retries_rate_limits_and_server_errors():
    hs = FakeHubSpot()
    hs.failures = [429, 503]
    client = hs.client()
    res = client.upsert_by_email({"a@x.com": {"firstname": "A"}})
    assert "a@x.com" in res.created and client.retries == 2


def test_gives_up_after_max_attempts_and_does_not_retry_4xx():
    hs = FakeHubSpot()
    hs.failures = [503] * 3
    with pytest.raises(HubSpotError) as exc:
        hs.client(max_attempts=3).read_by_email(["a@x.com"], ["email"])
    assert exc.value.status == 503

    hs.failures = [401]
    client = hs.client()
    with pytest.raises(HubSpotError):
        client.contact_properties()
    assert client.retries == 0


def test_property_definitions_and_read_by_email():
    hs = FakeHubSpot()
    cid = hs.add(email="Mia@Globex.com", firstname="Mia")
    client = hs.client()
    defs = client.contact_properties()
    assert "customer" in defs["lifecyclestage"]["options"] and defs["hs_object_id"]["readOnly"]
    found = client.read_by_email(["mia@globex.com", "nobody@x.com"], ["firstname"])
    assert found == {"mia@globex.com": {"id": cid, "properties": {"email": "Mia@Globex.com",
                                                                  "firstname": "Mia"}}}


def test_update_and_archive_isolate_failures():
    hs = FakeHubSpot()
    a, b = hs.add(email="a@x.com", lifecyclestage="customer"), hs.add(email="b@x.com")
    client = hs.client()
    failed = client.update({a: {"lifecyclestage": "lead"}, b: {"firstname": "B"}})  # a moves backward
    assert set(failed) == {a} and hs.contacts[b]["firstname"] == "B"
    assert client.archive([a, b]) == {} and not hs.contacts


def test_upsert_matches_results_by_id_when_email_is_not_echoed():
    hs = FakeHubSpot()
    original = hs._upsert

    def no_email_echo(body):
        resp = original(body)
        data = resp.json()
        for r in data["results"]:
            r["properties"] = {}
        return httpx.Response(200, json=data)

    hs._upsert = no_email_echo
    res = hs.client().upsert_by_email({"a@x.com": {"firstname": "A"}, "b@x.com": {"firstname": "B"}})
    assert set(res.created) == {"a@x.com", "b@x.com"} and not res.failed
