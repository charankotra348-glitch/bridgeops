"""Structured logging: one JSON object per line, with the request id on every line.

Each request gets an id (a safe incoming X-Request-ID is kept, otherwise a new one),
returned in the X-Request-ID response header and attached to every log line written
while handling it, so one import's HubSpot calls and results can be pulled out of the log
together. When the request finishes, one access line records method, path, status,
duration and who was signed in.
"""

import contextvars
import json
import logging
import re
import time
import uuid
from datetime import UTC, datetime

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request

request_id: contextvars.ContextVar[str | None] = contextvars.ContextVar("request_id", default=None)
_SAFE_ID = re.compile(r"^[A-Za-z0-9._-]{1,64}$")
HEADER = "X-Request-ID"

access = logging.getLogger("bridgeops.access")


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        line = {
            "time": datetime.fromtimestamp(record.created, UTC).isoformat(timespec="milliseconds"),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        if rid := request_id.get():
            line["request_id"] = rid
        line.update(getattr(record, "fields", {}))
        if record.exc_info:
            line["error"] = self.formatException(record.exc_info)
        return json.dumps(line, default=str)


def configure(level: str = "INFO") -> None:
    handler = logging.StreamHandler()
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(level)
    for name in ("uvicorn", "uvicorn.error"):
        logging.getLogger(name).handlers = []
        logging.getLogger(name).propagate = True
    logging.getLogger("uvicorn.access").disabled = True   # replaced by the access line below
    logging.getLogger("httpx").setLevel(logging.WARNING)  # HubSpot calls are summarized per job


class RequestLogMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        incoming = request.headers.get(HEADER, "")
        rid = incoming if _SAFE_ID.match(incoming) else uuid.uuid4().hex
        token = request_id.set(rid)
        start = time.perf_counter()
        status = 500
        try:
            response = await call_next(request)
            status = response.status_code
            response.headers[HEADER] = rid
            return response
        finally:
            access.info("request", extra={"fields": {
                "method": request.method,
                "path": request.url.path,
                "status": status,
                "duration_ms": round((time.perf_counter() - start) * 1000, 1),
                "user": getattr(request.state, "user", None),
            }})
            request_id.reset(token)
