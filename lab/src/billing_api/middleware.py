import logging
import re
import uuid
from time import perf_counter
from typing import Any

from starlette.datastructures import Headers, MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from billing_api.errors import problem_response
from billing_api.logging_setup import request_id_var

REQUEST_ID_HEADER = "X-Request-Id"

_VALID_REQUEST_ID = re.compile(r"[A-Za-z0-9_-]{1,64}")
_MAX_USER_AGENT_LENGTH = 200

logger = logging.getLogger("billing_api.http")


def accepted_request_id(value: str | None) -> str | None:
    if value is not None and _VALID_REQUEST_ID.fullmatch(value):
        return value
    return None


class RequestContextMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        headers = Headers(scope=scope)
        request_id = accepted_request_id(headers.get(REQUEST_ID_HEADER)) or str(uuid.uuid4())
        token = request_id_var.set(request_id)
        started = perf_counter()
        status_code = 500
        response_started = False

        async def send_with_request_id(message: Message) -> None:
            nonlocal status_code, response_started
            if message["type"] == "http.response.start":
                response_started = True
                status_code = message["status"]
                MutableHeaders(scope=message)[REQUEST_ID_HEADER] = request_id
            await send(message)

        try:
            await self.app(scope, receive, send_with_request_id)
        except Exception:
            logger.exception("Unhandled exception", extra={"event_name": "unhandled_exception"})
            if response_started:
                raise
            response = problem_response(500, "INTERNAL_ERROR", "An unexpected error occurred.")
            await response(scope, receive, send_with_request_id)
        finally:
            access: dict[str, Any] = {
                "event_name": "http.request",
                "method": scope["method"],
                "path": scope["path"],
                "status": status_code,
                "duration_ms": round((perf_counter() - started) * 1000),
                "user_agent": (headers.get("user-agent") or "")[:_MAX_USER_AGENT_LENGTH],
            }
            state = scope.get("state")
            if isinstance(state, dict) and state.get("account_id"):
                access["account_id"] = state["account_id"]
            logger.info("HTTP request", extra=access)
            request_id_var.reset(token)
