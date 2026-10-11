import json

import pytest

from app.profiling import ParseError, infer_type, profile_file
from tests.conftest import MESSY_CSV


def cols(profile):
    return {c.name: c for c in profile.columns}


def test_profiles_messy_csv():
    p = profile_file(MESSY_CSV.encode(), "export.csv")
    c = cols(p)
    assert p.file_format == "csv" and p.row_count == 5
    assert c["cust_mail"].inferred_type == "email"
    assert c["tel_no"].inferred_type == "phone"
    assert c["tel_no"].nulls == 1  # "N/A" counts as null
    assert c["email_opt_in"].inferred_type == "boolean"
    assert c["acct_status"].value_counts["Active"] == 1
    assert c["designation"].nulls == 1


def test_semicolon_csv_and_bom():
    raw = "﻿Name;Email\nAna;ana@x.com\nBo;bo@y.org\n".encode()
    c = cols(profile_file(raw, "x.csv"))
    assert set(c) == {"Name", "Email"}
    assert c["Email"].inferred_type == "email"


def test_json_list_and_wrapped():
    rows = [{"email": "a@b.com", "n": "1"}, {"email": "c@d.com", "n": "2"}]
    assert profile_file(json.dumps(rows).encode(), "x.json").row_count == 2
    assert profile_file(json.dumps({"records": rows}).encode(), "x.json").row_count == 2


@pytest.mark.parametrize("values,expected", [
    (["2023-04-11", "11/04/2023", "Apr 11, 2023", "2024-01-01"], "date"),
    (["https://acme.com", "www.globex.io"], "url"),
    (["12", "7", "300"], "integer"),
    (["hello", "world"], "string"),
    ([], "empty"),
])
def test_infer_type(values, expected):
    assert infer_type(values) == expected


def test_rejects_bad_files():
    with pytest.raises(ParseError):
        profile_file(b"{not json", "x.json")
    with pytest.raises(ParseError):
        profile_file(b"a,b\n", "x.csv")
