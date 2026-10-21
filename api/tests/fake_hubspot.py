"""In-memory stand-in for the HubSpot contacts API, served through httpx.MockTransport.

It mimics the behaviour the client has to cope with:
  - one invalid record makes HubSpot reject the whole batch (HTTP 400)
  - duplicate ids in one batch are rejected
  - lifecycle stage can only move forward unless it is cleared first
  - queued failures (429, 503, ...) are returned before real handling
"""

import itertools
import json
import re

import httpx

from app.hubspot import HubSpotClient

STAGES = ["subscriber", "lead", "marketingqualifiedlead", "salesqualifiedlead", "opportunity",
          "customer", "evangelist", "other"]
WRITABLE = ["email", "firstname", "lastname", "phone", "mobilephone", "company", "jobtitle", "website",
            "address", "city", "state", "zip", "country", "lifecyclestage"]
EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[A-Za-z]{2,}$")


class FakeHubSpot:
    def __init__(self):
        self.contacts: dict[str, dict] = {}      # id -> properties
        self.archived: set[str] = set()
        self.failures: list[int] = []            # statuses to return before handling, in order
        self.path_failures: dict[str, list[int]] = {}   # same, only for paths ending with the key
        self.calls: list[str] = []
        self._ids = itertools.count(1001)

    # -- helpers for tests ----------------------------------------------------
    def add(self, **props) -> str:
        cid = str(next(self._ids))
        self.contacts[cid] = dict(props)
        return cid

    def by_email(self, email: str) -> dict | None:
        for cid, p in self.contacts.items():
            if p.get("email", "").lower() == email.lower():
                return {"id": cid, **p}
        return None

    def client(self, **kw) -> HubSpotClient:
        return HubSpotClient("test-token", client=httpx.Client(base_url="https://api.hubapi.com",
                                                               transport=httpx.MockTransport(self.handle)),
                             sleep=lambda s: None, **kw)

    # -- transport --------------------------------------------------------------
    def handle(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        self.calls.append(path)
        if self.failures:
            return httpx.Response(self.failures.pop(0), json={"message": "injected failure"})
        for suffix, queue in self.path_failures.items():
            if path.endswith(suffix) and queue:
                return httpx.Response(queue.pop(0), json={"message": "injected failure"})
        body = json.loads(request.content) if request.content else {}
        routes = {
            "/crm/v3/properties/contacts": self._properties,
            "/crm/v3/objects/contacts/batch/read": self._read,
            "/crm/v3/objects/contacts/batch/upsert": self._upsert,
            "/crm/v3/objects/contacts/batch/update": self._update,
            "/crm/v3/objects/contacts/batch/archive": self._archive,
        }
        if path not in routes:
            return httpx.Response(404, json={"message": f"no route {path}"})
        return routes[path](body)

    def _properties(self, _):
        results = [{"name": n, "type": "string", "fieldType": "text"}
                   for n in WRITABLE if n != "lifecyclestage"]
        results.append({"name": "lifecyclestage", "type": "enumeration", "fieldType": "radio",
                        "options": [{"value": s, "label": s} for s in STAGES]})
        results.append({"name": "hs_object_id", "type": "number", "fieldType": "number",
                        "modificationMetadata": {"readOnlyValue": True}})
        return httpx.Response(200, json={"results": results})

    def _invalid(self, props: dict) -> str | None:
        for k, v in props.items():
            if k not in WRITABLE:
                return f"Property {k!r} does not exist"
            if k == "lifecyclestage" and v not in STAGES and v != "":
                return f"{v!r} was not one of the allowed options"
        if "email" in props and not EMAIL.match(props["email"]):
            return f"Email address {props['email']} is invalid"
        return None

    def _read(self, body):
        results = []
        for inp in body["inputs"]:
            c = self.by_email(inp["id"]) if body.get("idProperty") == "email" else (
                {"id": inp["id"], **self.contacts[inp["id"]]} if inp["id"] in self.contacts else None)
            if c:
                cid = c.pop("id")
                results.append({"id": cid, "properties": {k: c.get(k) for k in body.get("properties", [])}})
        return httpx.Response(200, json={"status": "COMPLETE", "results": results})

    def _upsert(self, body):
        ids = [i["id"].lower() for i in body["inputs"]]
        if len(ids) != len(set(ids)):
            return httpx.Response(400, json={"message": "Duplicate IDs found in batch input"})
        for inp in body["inputs"]:
            if err := self._invalid({**inp["properties"], "email": inp["id"]}):
                return httpx.Response(400, json={"category": "VALIDATION_ERROR", "message": err})
        results = []
        for inp in body["inputs"]:
            existing = self.by_email(inp["id"])
            if existing:
                cid = existing["id"]
                self.contacts[cid].update(inp["properties"])
                new = False
            else:
                cid = self.add(**{**inp["properties"], "email": inp["id"]})
                new = True
            results.append({"id": cid, "new": new, "properties": dict(self.contacts[cid])})
        return httpx.Response(200, json={"status": "COMPLETE", "results": results})

    def _update(self, body):
        for inp in body["inputs"]:
            if inp["id"] not in self.contacts:
                return httpx.Response(400, json={"message": f"contact {inp['id']} not found"})
            if err := self._invalid(inp["properties"]):
                return httpx.Response(400, json={"message": err})
            new_stage, old_stage = inp["properties"].get("lifecyclestage"), \
                self.contacts[inp["id"]].get("lifecyclestage")
            if new_stage and old_stage and STAGES.index(new_stage) < STAGES.index(old_stage):
                return httpx.Response(400, json={"message": "lifecycle stage can only move forward"})
        for inp in body["inputs"]:
            self.contacts[inp["id"]].update(inp["properties"])
        return httpx.Response(200, json={"status": "COMPLETE"})

    def _archive(self, body):
        for inp in body["inputs"]:
            if self.contacts.pop(inp["id"], None) is not None:
                self.archived.add(inp["id"])
        return httpx.Response(204)
