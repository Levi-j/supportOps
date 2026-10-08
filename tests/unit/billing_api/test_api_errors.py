from collections.abc import Iterator
from contextlib import AbstractContextManager, nullcontext
from datetime import UTC, datetime
from typing import Any, NoReturn

import psycopg
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from psycopg import errors

from billing_api import repository
from billing_api.auth import hash_api_key
from billing_api.dependencies import get_connection
from billing_api.repository import ApiKeyRecord
from tests.log_capture import JsonCapture
from tests.unit.billing_api.support import AppFactory

API_KEY = "bk_juniper01_lab_only_not_a_real_key"
AUTH = {"Authorization": f"Bearer {API_KEY}"}


class FakeConnection:
    def transaction(self) -> AbstractContextManager[None]:
        return nullcontext()


def fake_connection() -> Iterator[FakeConnection]:
    yield FakeConnection()


def key_record(**changes: Any) -> ApiKeyRecord:
    values: dict[str, Any] = {
        "key_id": "key_juniper_main",
        "account_id": "acct_juniper",
        "account_name": "Juniper Dental Group",
        "account_status": "active",
        "key_hash": hash_api_key(API_KEY),
        "label": "Practice software",
        "created_at": datetime(2026, 1, 1, tzinfo=UTC),
        "revoked": False,
        "expired": False,
    }
    return ApiKeyRecord(**{**values, **changes})


@pytest.fixture
def app(make_app: AppFactory, monkeypatch: pytest.MonkeyPatch) -> FastAPI:
    application = make_app()
    application.dependency_overrides[get_connection] = fake_connection
    monkeypatch.setattr(repository, "find_api_key", lambda _connection, _prefix: key_record())
    return application


def unreachable_database() -> NoReturn:
    raise psycopg.OperationalError(
        "failed to resolve host 'postgres': [Errno -5] No address associated with hostname"
    )


def raising(exc: Exception) -> Any:
    def fail(*_args: object, **_kwargs: object) -> NoReturn:
        raise exc

    return fail


def test_invalid_json_is_a_400_even_before_authentication(app: FastAPI, logs: JsonCapture) -> None:
    response = TestClient(app).post(
        "/v1/customers",
        headers={"Content-Type": "application/json", "X-Request-Id": "json-1"},
        content=b'{"name": "Lakeside", "email": }',
    )

    assert response.status_code == 400
    assert response.headers["content-type"] == "application/problem+json"
    assert response.json() == {
        "type": "about:blank",
        "title": "Bad Request",
        "status": 400,
        "detail": "The request body is not valid JSON.",
        "code": "MALFORMED_REQUEST",
        "request_id": "json-1",
    }
    [invalid] = logs.events("request.invalid_json")
    assert invalid["error_message"] == "Expecting value"
    assert invalid["error_position"] == 30
    assert invalid["content_type"] == "application/json"
    assert "Lakeside" not in str(logs.entries)


def test_form_encoded_body_is_a_422_on_the_whole_body(app: FastAPI) -> None:
    response = TestClient(app).post(
        "/v1/customers", headers=AUTH, data={"name": "Lakeside", "email": "a@b.example"}
    )

    assert response.status_code == 422
    [error] = response.json()["errors"]
    assert (error["location"], error["field"]) == ("body", "body")


def test_query_validation_errors_name_the_parameter(app: FastAPI) -> None:
    response = TestClient(app).get("/v1/customers?limit=1000", headers=AUTH)

    assert response.status_code == 422
    [error] = response.json()["errors"]
    assert (error["location"], error["field"], error["type"]) == (
        "query",
        "limit",
        "less_than_equal",
    )


def test_unauthenticated_and_suspended_responses(
    app: FastAPI, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = TestClient(app)

    missing = client.get("/v1/account")
    monkeypatch.setattr(
        repository, "find_api_key", lambda _c, _p: key_record(account_status="suspended")
    )
    suspended = client.get("/v1/account", headers=AUTH)

    assert missing.status_code == 401
    assert missing.headers["WWW-Authenticate"] == 'Bearer realm="billing-api"'
    assert suspended.status_code == 403
    assert suspended.json()["code"] == "ACCOUNT_SUSPENDED"


def test_access_log_names_the_authenticated_account(app: FastAPI, logs: JsonCapture) -> None:
    TestClient(app).get("/v1/account", headers=AUTH)

    [access] = logs.events("http.request")
    assert access["account_id"] == "acct_juniper"
    assert access["status"] == 200


def test_unreachable_database_is_a_503_with_the_classified_cause(
    app: FastAPI, logs: JsonCapture
) -> None:
    app.dependency_overrides[get_connection] = unreachable_database

    response = TestClient(app).get("/v1/account", headers=AUTH)

    assert response.status_code == 503
    assert response.json()["code"] == "SERVICE_UNAVAILABLE"
    assert "Retry-After" not in response.headers
    assert "postgres" not in response.text
    [event] = logs.events("db.unavailable")
    assert event["level"] == "ERROR"
    assert event["error"] == "dns_failure"


@pytest.mark.parametrize(
    ("exc", "event_name"),
    [
        (errors.LockNotAvailable("canceling statement due to lock timeout"), "db.lock_timeout"),
        (
            errors.QueryCanceled("canceling statement due to statement timeout"),
            "db.statement_timeout",
        ),
    ],
)
def test_database_timeouts_are_a_503_database_busy(
    app: FastAPI,
    logs: JsonCapture,
    monkeypatch: pytest.MonkeyPatch,
    exc: Exception,
    event_name: str,
) -> None:
    monkeypatch.setattr(repository, "lock_invoice", raising(exc))

    response = TestClient(app).post("/v1/invoices/inv_juniper_1003/pay", headers=AUTH)

    assert response.status_code == 503
    assert response.json()["code"] == "DATABASE_BUSY"
    assert response.headers["Retry-After"] == "5"
    [event] = logs.events(event_name)
    assert event["level"] == "WARNING"


def test_openapi_documents_every_endpoint_and_the_api_key_scheme(app: FastAPI) -> None:
    spec = TestClient(app).get("/openapi.json").json()

    assert set(spec["paths"]) >= {
        "/v1/account",
        "/v1/customers",
        "/v1/customers/{customer_id}",
        "/v1/invoices",
        "/v1/invoices/{invoice_id}",
        "/v1/invoices/{invoice_id}/pay",
    }
    assert spec["components"]["securitySchemes"]["ApiKey"] == {
        "type": "apiKey",
        "in": "header",
        "name": "Authorization",
        "description": "Send your API key as `Bearer <api key>`, for example `Bearer bk_...`.",
    }
