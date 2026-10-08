from http import HTTPStatus
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from billing_api.logging_setup import request_id_var

PROBLEM_JSON = "application/problem+json"

_CODES = {
    HTTPStatus.NOT_FOUND: "RESOURCE_NOT_FOUND",
    HTTPStatus.METHOD_NOT_ALLOWED: "METHOD_NOT_ALLOWED",
}


def problem_response(status: int, code: str, detail: str) -> JSONResponse:
    body: dict[str, Any] = {
        "type": "about:blank",
        "title": HTTPStatus(status).phrase,
        "status": status,
        "detail": detail,
        "code": code,
    }
    request_id = request_id_var.get()
    if request_id is not None:
        body["request_id"] = request_id
    return JSONResponse(body, status_code=status, media_type=PROBLEM_JSON)


def install_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(StarletteHTTPException)
    async def handle_http_exception(request: Request, exc: StarletteHTTPException) -> JSONResponse:
        detail = exc.detail if isinstance(exc.detail, str) else HTTPStatus(exc.status_code).phrase
        response = problem_response(
            exc.status_code, _CODES.get(HTTPStatus(exc.status_code), "HTTP_ERROR"), detail
        )
        response.headers.update(exc.headers or {})
        return response
