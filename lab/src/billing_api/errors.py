import logging
from collections.abc import Mapping, Sequence
from http import HTTPStatus
from typing import Any

import psycopg
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from billing_api.database import classify_error, first_line
from billing_api.logging_setup import request_id_var

PROBLEM_JSON = "application/problem+json"
RETRY_AFTER_SECONDS = "5"

_CODES = {
    HTTPStatus.NOT_FOUND: "RESOURCE_NOT_FOUND",
    HTTPStatus.METHOD_NOT_ALLOWED: "METHOD_NOT_ALLOWED",
}

logger = logging.getLogger("billing_api.errors")

FieldError = dict[str, str]


class ApiError(Exception):
    def __init__(
        self,
        status: int,
        code: str,
        detail: str,
        *,
        headers: Mapping[str, str] | None = None,
        errors: Sequence[FieldError] | None = None,
    ) -> None:
        super().__init__(detail)
        self.status = status
        self.code = code
        self.detail = detail
        self.headers = dict(headers or {})
        self.errors = list(errors or [])


def not_found(kind: str, resource_id: str) -> ApiError:
    return ApiError(404, "RESOURCE_NOT_FOUND", f"{kind} {resource_id} not found.")


def validation_failed(errors: Sequence[FieldError]) -> ApiError:
    logger.warning(
        "Request validation failed",
        extra={
            "event_name": "request.validation_failed",
            "fields": [f"{error['location']}.{error['field']}" for error in errors],
            "error_types": [error["type"] for error in errors],
        },
    )
    return ApiError(
        422,
        "VALIDATION_FAILED",
        "The request contains missing or invalid fields.",
        errors=errors,
    )


def problem_response(
    status: int,
    code: str,
    detail: str,
    *,
    headers: Mapping[str, str] | None = None,
    errors: Sequence[FieldError] | None = None,
) -> JSONResponse:
    body: dict[str, Any] = {
        "type": "about:blank",
        "title": HTTPStatus(status).phrase,
        "status": status,
        "detail": detail,
        "code": code,
    }
    if errors:
        body["errors"] = list(errors)
    request_id = request_id_var.get()
    if request_id is not None:
        body["request_id"] = request_id
    return JSONResponse(body, status_code=status, media_type=PROBLEM_JSON, headers=headers)


def _field_error(error: Mapping[str, Any]) -> FieldError:
    location = [str(part) for part in error["loc"]]
    return {
        "location": location[0],
        "field": ".".join(location[1:]) or location[0],
        "message": str(error["msg"]),
        "type": str(error["type"]),
    }


def install_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(StarletteHTTPException)
    async def handle_http_exception(request: Request, exc: StarletteHTTPException) -> JSONResponse:
        detail = exc.detail if isinstance(exc.detail, str) else HTTPStatus(exc.status_code).phrase
        return problem_response(
            exc.status_code,
            _CODES.get(HTTPStatus(exc.status_code), "HTTP_ERROR"),
            detail,
            headers=exc.headers,
        )

    @app.exception_handler(ApiError)
    async def handle_api_error(request: Request, exc: ApiError) -> JSONResponse:
        return problem_response(
            exc.status, exc.code, exc.detail, headers=exc.headers, errors=exc.errors
        )

    @app.exception_handler(RequestValidationError)
    async def handle_validation_error(
        request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        errors = exc.errors()
        invalid_json = next((error for error in errors if error["type"] == "json_invalid"), None)
        if invalid_json is not None:
            location = invalid_json["loc"]
            logger.warning(
                "Request body is not valid JSON",
                extra={
                    "event_name": "request.invalid_json",
                    "error_message": invalid_json.get("ctx", {}).get("error"),
                    "error_position": location[1] if len(location) > 1 else None,
                    "content_type": request.headers.get("content-type"),
                    "content_length": request.headers.get("content-length"),
                },
            )
            return problem_response(400, "MALFORMED_REQUEST", "The request body is not valid JSON.")
        failure = validation_failed([_field_error(error) for error in errors])
        return problem_response(failure.status, failure.code, failure.detail, errors=failure.errors)

    @app.exception_handler(psycopg.errors.LockNotAvailable)
    async def handle_lock_timeout(request: Request, exc: psycopg.Error) -> JSONResponse:
        logger.warning(
            "Database lock timeout",
            extra={"event_name": "db.lock_timeout", "detail": first_line(exc)},
        )
        return _database_busy()

    @app.exception_handler(psycopg.errors.QueryCanceled)
    async def handle_statement_timeout(request: Request, exc: psycopg.Error) -> JSONResponse:
        logger.warning(
            "Database statement timeout",
            extra={"event_name": "db.statement_timeout", "detail": first_line(exc)},
        )
        return _database_busy()

    @app.exception_handler(psycopg.OperationalError)
    async def handle_database_unavailable(request: Request, exc: psycopg.Error) -> JSONResponse:
        logger.error(
            "Database unavailable",
            extra={
                "event_name": "db.unavailable",
                "error": classify_error(exc),
                "detail": first_line(exc),
            },
        )
        return problem_response(
            503,
            "SERVICE_UNAVAILABLE",
            "The service is temporarily unavailable. Please try again later.",
        )


def _database_busy() -> JSONResponse:
    return problem_response(
        503,
        "DATABASE_BUSY",
        "The database is busy. Please retry the request shortly.",
        headers={"Retry-After": RETRY_AFTER_SECONDS},
    )
