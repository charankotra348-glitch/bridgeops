import os
import tempfile

_tmp = tempfile.mkdtemp(prefix="bridgeops-test-")
os.environ["DATABASE_URL"] = f"sqlite:///{_tmp}/test.db"
os.environ["UPLOAD_DIR"] = f"{_tmp}/uploads"
os.environ["DECISION_PROVIDER"] = "rules"

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app.main import app  # noqa: E402


@pytest.fixture
def client():
    with TestClient(app) as c:
        yield c


MESSY_CSV = (
    "cust_mail,FName,surname,tel_no,org,designation,acct_status,email_opt_in,internal_ref\n"
    "a.rao@gmail.com,Priya,Rao,+1 631-555-0142,Acme Inc,Sales Manager,Active,Y,X-001\n"
    "jlee@acme.io,Jordan,Lee,(212) 555 0199,Contoso Ltd,CTO,PAYING,N,X-002\n"
    "sam@contoso.com,Sam,Okafor,5165550123,Globex,Intern,churned 2024,Y,X-003\n"
    "mia@globex.com,Mia,Chen,N/A,Globex,Analyst,Active-ish,N,X-004\n"
    "li@initech.com,Li,Wang,631 555 0177,Initech,,hot prospect,Y,X-005\n"
)
