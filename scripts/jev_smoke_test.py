"""
BridgeOps - Jev smoke test (Day 1)

Sends 20 realistic BridgeOps decisions to Jev and reports:
  - accuracy against the expected answers
  - per-call latency (p50 / p95)
  - confidence on right vs wrong answers (is confidence sensible?)
  - one batched call (all column mappings in ONE request) vs separate calls
  - token usage / cost, if the API returns usage data

Setup:
  pip install requests
  export TYPESAFE_API_KEY=your_key_here      (Windows: set TYPESAFE_API_KEY=...)
  python jev_smoke_test.py

Results are also saved to jev_smoke_results.json.
"""

import json
import os
import statistics
import sys
import time

import requests

API_URL = os.environ.get("TYPESAFE_API_URL", "https://api.typesafe.ai/v1/systemone")
MODEL = os.environ.get("JEV_MODEL", "jev-latest")
API_KEY = os.environ.get("TYPESAFE_API_KEY")
PRICE_PER_M_INPUT_TOKENS = 0.042  # USD, published price at time of writing; verify in the console

# ---------------------------------------------------------------------------
# Target schema options (subset of HubSpot contact properties)
# ---------------------------------------------------------------------------
HUBSPOT_PROPERTIES = {
    "email": "Contact's email address",
    "firstname": "Contact's first (given) name",
    "lastname": "Contact's last (family) name",
    "phone": "Contact's phone number",
    "company": "Name of the company the contact works for",
    "jobtitle": "Contact's job title or role",
    "city": "City the contact is located in",
    "lifecyclestage": "Stage of the relationship: subscriber, lead, customer, etc.",
    "createdate": "Date the record was first created",
    "none": "No matching HubSpot property; the column should not be imported",
}

LIFECYCLE_STAGES = {
    "subscriber": "Signed up for updates, no buying interest shown yet",
    "lead": "Showed interest, not yet qualified",
    "opportunity": "Actively in a sales conversation or deal",
    "customer": "Currently paying or has purchased",
    "other": "Former customer, churned, inactive, or does not fit any stage",
}

# ---------------------------------------------------------------------------
# 20 test cases: 10 column mappings, 6 value normalizations, 4 duplicate checks
# ---------------------------------------------------------------------------
MAPPING_CASES = [
    ("cust_mail", ["a.rao@gmail.com", "jlee@acme.io", "sam@contoso.com"], "email"),
    ("FName", ["Priya", "Jordan", "Sam"], "firstname"),
    ("surname", ["Rao", "Lee", "Okafor"], "lastname"),
    ("tel_no", ["+1 631-555-0142", "(212) 555 0199", "5165550123"], "phone"),
    ("org", ["Acme Inc", "Contoso Ltd", "Globex"], "company"),
    ("designation", ["Sales Manager", "CTO", "Intern"], "jobtitle"),
    ("location_city", ["Centereach", "Boston", "Austin"], "city"),
    ("acct_status", ["Active", "Prospect", "churned"], "lifecyclestage"),
    ("added_on", ["2023-04-11", "11/04/2023", "Apr 11 2023"], "createdate"),
    ("internal_notes_flag", ["Y", "N", "Y"], "none"),
]

NORMALIZE_CASES = [
    ("Active-ish", "customer"),
    ("PAYING", "customer"),
    ("hot prospect", "opportunity"),
    ("newsletter only", "subscriber"),
    ("churned 2024", "other"),
    ("interested - call back", "lead"),
]

DUPLICATE_CASES = [
    ({"name": "Acme Inc.", "domain": "acme.com", "city": "Boston"},
     {"name": "ACME Incorporated", "domain": "acme.com", "city": "Boston"}, True),
    ({"name": "Globex", "domain": "globex.com", "city": "Austin"},
     {"name": "Globex Corp", "domain": "globex.io", "city": "Austin"}, True),
    ({"name": "Delta Dental", "domain": "deltadental.com", "city": "Chicago"},
     {"name": "Delta Air Lines", "domain": "delta.com", "city": "Atlanta"}, False),
    ({"name": "Smith & Sons Plumbing", "domain": "", "city": "Albany"},
     {"name": "Smith Consulting", "domain": "smithconsult.com", "city": "Denver"}, False),
]


# ---------------------------------------------------------------------------
# HTTP helper
# ---------------------------------------------------------------------------
def call_jev(state, questions):
    """POST one request; returns (json_response, latency_ms)."""
    body = {"model": MODEL, "state": state, "questions": questions}
    headers = {"Authorization": f"Bearer {API_KEY}", "Content-Type": "application/json"}
    start = time.perf_counter()
    resp = requests.post(API_URL, headers=headers, json=body, timeout=30)
    latency_ms = (time.perf_counter() - start) * 1000
    if resp.status_code != 200:
        raise RuntimeError(f"HTTP {resp.status_code}: {resp.text[:500]}")
    return resp.json(), latency_ms


def extract_usage(resp_json):
    """Return input tokens if the response reports usage, else None."""
    usage = resp_json.get("usage") or {}
    for key in ("input_tokens", "prompt_tokens", "total_tokens"):
        if isinstance(usage.get(key), (int, float)):
            return usage[key]
    return None


def answer_of(resp_json, qid):
    answers = resp_json.get("answers", resp_json)
    return answers.get(qid, {})


# ---------------------------------------------------------------------------
# Question builders
# ---------------------------------------------------------------------------
def mapping_question(col_path):
    return {
        "type": "choice",
        "instructions": (
            f"Which HubSpot contact property should the source column `{col_path}` be imported into, "
            f"judging by the column name and its sample values?"
        ),
        "criteria": HUBSPOT_PROPERTIES,
    }


def normalize_question():
    return {
        "type": "choice",
        "instructions": "Which HubSpot lifecycle stage best matches the source status value `value`?",
        "criteria": LIFECYCLE_STAGES,
    }


def duplicate_question():
    return {
        "type": "noul",
        "instructions": "Do `record_a` and `record_b` refer to the same real-world company?",
    }


# ---------------------------------------------------------------------------
# Run
# ---------------------------------------------------------------------------
def main():
    if not API_KEY:
        sys.exit("Set TYPESAFE_API_KEY first (get one at https://console.typesafe.ai/keys).")

    results = []
    total_tokens = 0
    tokens_seen = False

    print(f"Model: {MODEL}   Endpoint: {API_URL}\n")

    # 1) Column mappings, one call each
    print("== Column mapping (Choice) ==")
    for col, samples, expected in MAPPING_CASES:
        state = {"column": {"name": col, "sample_values": samples}}
        resp, ms = call_jev(state, {"target": mapping_question("column")})
        a = answer_of(resp, "target")
        got, conf = a.get("choice"), a.get("confidence")
        results.append({"kind": "mapping", "input": col, "expected": expected, "got": got,
                        "confidence": conf, "correct": got == expected, "latency_ms": ms})
        t = extract_usage(resp)
        if t is not None:
            tokens_seen, total_tokens = True, total_tokens + t
        print(f"  {col:22} -> {str(got):15} conf={conf}  expected={expected:15} {ms:6.0f} ms")

    # 2) Value normalization
    print("\n== Value normalization (Choice) ==")
    for value, expected in NORMALIZE_CASES:
        resp, ms = call_jev({"value": value}, {"stage": normalize_question()})
        a = answer_of(resp, "stage")
        got, conf = a.get("choice"), a.get("confidence")
        results.append({"kind": "normalize", "input": value, "expected": expected, "got": got,
                        "confidence": conf, "correct": got == expected, "latency_ms": ms})
        t = extract_usage(resp)
        if t is not None:
            tokens_seen, total_tokens = True, total_tokens + t
        print(f"  {value:22} -> {str(got):15} conf={conf}  expected={expected:15} {ms:6.0f} ms")

    # 3) Duplicate detection (Noul = probability of yes)
    print("\n== Duplicate detection (Noul) ==")
    for rec_a, rec_b, expected in DUPLICATE_CASES:
        resp, ms = call_jev({"record_a": rec_a, "record_b": rec_b}, {"same": duplicate_question()})
        p = answer_of(resp, "same").get("noul")
        got = (p is not None and p >= 0.5)
        # For Noul, "confidence" = distance from 0.5, rescaled to 0..1
        conf = None if p is None else abs(p - 0.5) * 2
        results.append({"kind": "duplicate", "input": f"{rec_a['name']} | {rec_b['name']}",
                        "expected": expected, "got": got, "p_yes": p, "confidence": conf,
                        "correct": got == expected, "latency_ms": ms})
        t = extract_usage(resp)
        if t is not None:
            tokens_seen, total_tokens = True, total_tokens + t
        print(f"  {rec_a['name'][:20]:20} vs {rec_b['name'][:20]:20} p(yes)={p}  expected={expected}  {ms:6.0f} ms")

    # 4) Batched: all 10 column mappings in ONE request
    print("\n== Batched call: all 10 column mappings in one request ==")
    batch_state = {"columns": {col: samples for col, samples, _ in MAPPING_CASES}}
    batch_questions = {f"q{i}": mapping_question(f"columns.{col}")
                       for i, (col, _, _) in enumerate(MAPPING_CASES)}
    resp, batch_ms = call_jev(batch_state, batch_questions)
    batch_correct = sum(
        answer_of(resp, f"q{i}").get("choice") == expected
        for i, (_, _, expected) in enumerate(MAPPING_CASES)
    )
    print(f"  {batch_correct}/10 correct in {batch_ms:.0f} ms (one request)")

    # ---------------- Summary ----------------
    lat = sorted(r["latency_ms"] for r in results)
    p50 = statistics.median(lat)
    p95 = lat[max(0, int(round(0.95 * len(lat))) - 1)]
    correct = [r for r in results if r["correct"]]
    wrong = [r for r in results if not r["correct"]]

    def avg_conf(rows):
        vals = [r["confidence"] for r in rows if isinstance(r.get("confidence"), (int, float))]
        return round(statistics.mean(vals), 3) if vals else None

    sep_mapping_ms = sum(r["latency_ms"] for r in results if r["kind"] == "mapping")

    print("\n================ SUMMARY ================")
    print(f"Accuracy:            {len(correct)}/{len(results)}")
    for kind in ("mapping", "normalize", "duplicate"):
        rows = [r for r in results if r["kind"] == kind]
        print(f"  {kind:10} {sum(r['correct'] for r in rows)}/{len(rows)}")
    print(f"Latency per call:    p50 {p50:.0f} ms, p95 {p95:.0f} ms")
    print(f"Avg confidence:      right answers {avg_conf(correct)}, wrong answers {avg_conf(wrong)}")
    print("   -> sensible if confidence is clearly higher on right answers than wrong ones")
    print(f"Batching:            10 separate calls {sep_mapping_ms:.0f} ms total vs 1 batched call {batch_ms:.0f} ms")
    if tokens_seen:
        print(f"Input tokens:        {total_tokens}  (~${total_tokens / 1e6 * PRICE_PER_M_INPUT_TOKENS:.6f})")
    else:
        print("Usage/cost:          not in response; check usage in the TypeSafe console")
    if wrong:
        print("\nMisses (review these):")
        for r in wrong:
            print(f"  [{r['kind']}] {r['input']}: expected {r['expected']}, got {r['got']} (conf {r['confidence']})")

    with open("jev_smoke_results.json", "w") as f:
        json.dump({"model": MODEL, "results": results, "batch_ms": batch_ms,
                   "batch_correct": batch_correct, "raw_last_response": resp}, f, indent=2)
    print("\nSaved jev_smoke_results.json")


if __name__ == "__main__":
    main()
