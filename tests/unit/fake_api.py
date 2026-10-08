import json
from collections.abc import Callable, Mapping
from typing import Any

import httpx

Responder = Callable[[httpx.Request], httpx.Response]


class FakeApi:
    def __init__(self) -> None:
        self.routes: dict[tuple[str, str], Responder | Exception] = {}
        self.requests: list[httpx.Request] = []

    def on(self, method: str, path: str, responder: Responder | Exception) -> "FakeApi":
        self.routes[(method.upper(), path)] = responder
        return self

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        responder = self.routes.get((request.method, request.url.path))
        if responder is None:
            return respond(404, {"code": "RESOURCE_NOT_FOUND"}, problem=True)(request)
        if isinstance(responder, Exception):
            raise responder
        return responder(request)

    @property
    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handle)

    @property
    def methods(self) -> list[str]:
        return [request.method for request in self.requests]


def respond(
    status: int,
    body: Any = None,
    *,
    problem: bool = False,
    headers: Mapping[str, str] | None = None,
    echo_request_id: bool = True,
    text: str | None = None,
) -> Responder:
    def build(request: httpx.Request) -> httpx.Response:
        response_headers = dict(headers or {})
        if echo_request_id and "X-Request-Id" in request.headers:
            response_headers["X-Request-Id"] = request.headers["X-Request-Id"]
        if text is not None:
            response_headers.setdefault("content-type", "text/plain")
            return httpx.Response(status, text=text, headers=response_headers)
        if body is None:
            return httpx.Response(status, headers=response_headers)
        response_headers["content-type"] = (
            "application/problem+json" if problem else "application/json"
        )
        return httpx.Response(status, content=json.dumps(body).encode(), headers=response_headers)

    return build


def lab_health(environment: str = "lab") -> Responder:
    return respond(200, {"status": "ok", "environment": environment})
