import errno
import re
import socket
import ssl
from collections.abc import Iterator
from typing import Any

import httpx
import pytest
from pydantic import SecretStr

from supportops.errors import ConfigError
from supportops.http_checks import (
    BODY_PREVIEW_CHARACTERS,
    Credentials,
    HttpResult,
    auth_headers,
    create_client,
    resolve_credentials,
    response_hint,
    send,
)
from supportops.settings import Settings
from supportops.targets import BILLING
from tests.unit.fake_api import FakeApi, respond

API_KEY = "bk_juniper01_lab_only_not_a_real_key"


def fake_clock(*values: float) -> Iterator[float]:
    return iter(values)


def client_for(api: FakeApi, **settings: Any) -> httpx.Client:
    return create_client(Settings(**settings), transport=api.transport)


def chained(error: httpx.TransportError, cause: BaseException) -> httpx.TransportError:
    error.__cause__ = cause
    return error


def test_client_uses_separate_timeouts_and_never_follows_redirects() -> None:
    client = create_client(Settings(connect_timeout_seconds=2, http_timeout_seconds=7))

    assert client.timeout.connect == 2
    assert client.timeout.read == 7
    assert client.follow_redirects is False
    assert client.headers["User-Agent"].startswith("supportops/")
    assert str(client.base_url) == "http://127.0.0.1:8001/"


def test_successful_json_response_is_captured_with_timing() -> None:
    api = FakeApi().on("GET", "/v1/account", respond(200, {"id": "acct_juniper"}))
    clock = fake_clock(10.0, 10.0425)

    result = send(client_for(api), "GET", "/v1/account", clock=lambda: next(clock))

    assert result.status == 200
    assert result.reason == "OK"
    assert result.duration_ms == 42.5
    assert result.url == "http://127.0.0.1:8001/v1/account"
    assert result.json_body() == {"id": "acct_juniper"}
    assert result.headers["content-type"] == "application/json"
    assert result.failure is None


def test_a_request_id_is_generated_sent_and_checked() -> None:
    api = FakeApi().on("GET", "/health", respond(200, {"status": "ok"}))

    result = send(client_for(api), "GET", "/health")

    sent = api.requests[0].headers["X-Request-Id"]
    assert re.fullmatch(r"supportops-[0-9a-f]{12}", sent)
    assert result.request_id == sent
    assert result.response_request_id == sent


def test_an_explicit_request_id_is_used() -> None:
    api = FakeApi().on("GET", "/health", respond(200, {"status": "ok"}))

    result = send(client_for(api), "GET", "/health", request_id="ticket-4711")

    assert api.requests[0].headers["X-Request-Id"] == "ticket-4711"
    assert result.request_id == "ticket-4711"


def test_missing_response_request_id_is_reported() -> None:
    api = FakeApi().on("GET", "/health", respond(200, {"status": "ok"}, echo_request_id=False))

    result = send(client_for(api), "GET", "/health")

    assert result.response_request_id is None


def test_problem_details_are_interpreted() -> None:
    body = {
        "type": "about:blank",
        "title": "Unprocessable Content",
        "status": 422,
        "detail": "The request contains missing or invalid fields.",
        "code": "VALIDATION_FAILED",
        "request_id": "req-1",
        "errors": [{"location": "body", "field": "email", "message": "Field required"}],
    }
    api = FakeApi().on("POST", "/v1/customers", respond(422, body, problem=True))

    result = send(client_for(api), "POST", "/v1/customers", content=b"{}")

    assert result.problem is not None
    assert result.problem.code == "VALIDATION_FAILED"
    assert result.problem.status == 422
    assert result.problem.request_id == "req-1"
    assert result.problem.errors == body["errors"]


def test_camel_case_request_id_in_problem_details_is_understood() -> None:
    api = FakeApi().on(
        "GET", "/x", respond(404, {"code": "NOT_FOUND", "requestId": "r-9"}, problem=True)
    )

    result = send(client_for(api), "GET", "/x")

    assert result.problem is not None
    assert result.problem.request_id == "r-9"


def test_redirects_are_reported_but_not_followed() -> None:
    api = FakeApi().on(
        "GET", "/old", respond(302, headers={"Location": "http://127.0.0.1:8001/new"})
    )

    result = send(client_for(api), "GET", "/old")

    assert result.status == 302
    assert api.methods == ["GET"]
    assert result.headers["location"] == "http://127.0.0.1:8001/new"
    hint = response_hint(result, Credentials(api_key=None, source="none"), BILLING)
    assert hint is not None
    assert "doesn't follow redirects" in hint


def test_large_bodies_are_truncated_unless_full_is_requested() -> None:
    api = FakeApi().on("GET", "/big", respond(200, {"items": ["x" * 50] * 400}))

    preview = send(client_for(api), "GET", "/big")
    full = send(client_for(api), "GET", "/big", full_body=True)

    assert preview.body_truncated
    assert preview.body is not None
    assert len(preview.body) == BODY_PREVIEW_CHARACTERS
    assert preview.json_body() is None
    assert not full.body_truncated
    assert full.json_body() == {"items": ["x" * 50] * 400}


def test_plain_text_bodies_are_kept_as_text() -> None:
    api = FakeApi().on("GET", "/text", respond(500, text="Internal Server Error"))

    result = send(client_for(api), "GET", "/text")

    assert result.body == "Internal Server Error"
    assert result.problem is None


@pytest.mark.parametrize(
    ("error", "category"),
    [
        (
            chained(httpx.ConnectError("getaddrinfo failed"), socket.gaierror(11001, "failed")),
            "dns_failure",
        ),
        (
            chained(httpx.ConnectError("[Errno -5] No address"), socket.gaierror(-5, "No address")),
            "dns_failure",
        ),
        (
            chained(httpx.ConnectError("refused"), ConnectionRefusedError(10061, "refused")),
            "connection_refused",
        ),
        (httpx.ConnectTimeout("timed out"), "connect_timeout"),
        (httpx.ReadTimeout("timed out"), "read_timeout"),
        (httpx.WriteTimeout("timed out"), "timeout"),
        (
            chained(httpx.ConnectError("wrong version"), ssl.SSLError(1, "WRONG_VERSION_NUMBER")),
            "tls_error",
        ),
        (
            chained(httpx.ReadError("reset"), ConnectionResetError(errno.ECONNRESET, "reset")),
            "connection_closed",
        ),
        (httpx.RemoteProtocolError("Server disconnected"), "connection_closed"),
        (
            chained(httpx.ConnectError("unreachable"), OSError(errno.ENETUNREACH, "unreachable")),
            "connection_failed",
        ),
        (httpx.UnsupportedProtocol("Request URL has an unsupported protocol"), "transport_error"),
    ],
)
def test_transport_failures_are_classified_by_type(
    error: httpx.TransportError, category: str
) -> None:
    api = FakeApi().on("GET", "/health", error)

    result = send(client_for(api), "GET", "/health")

    assert result.status is None
    assert result.failure is not None
    assert result.failure.category == category
    assert result.failure.summary
    assert result.failure.hint
    assert "127.0.0.1" in result.failure.summary


def test_certificate_failures_get_a_certificate_specific_explanation() -> None:
    error = chained(
        httpx.ConnectError("certificate verify failed"),
        ssl.SSLCertVerificationError(1, "certificate verify failed"),
    )
    api = FakeApi().on("GET", "/health", error)

    result = send(client_for(api), "GET", "/health")

    assert result.failure is not None
    assert result.failure.category == "tls_error"
    assert "certificate" in result.failure.summary


@pytest.mark.parametrize(
    "error",
    [httpx.ReadTimeout("timed out"), httpx.RemoteProtocolError("Server disconnected")],
)
def test_a_write_that_may_have_arrived_warns_against_resending(
    error: httpx.TransportError,
) -> None:
    api = FakeApi().on("POST", "/v1/invoices/inv_1/pay", error)

    result = send(client_for(api), "POST", "/v1/invoices/inv_1/pay")

    assert result.failure is not None
    assert "may already have reached the server" in result.failure.hint
    assert api.methods == ["POST"]


@pytest.mark.parametrize(
    ("method", "error"),
    [
        ("POST", httpx.ConnectTimeout("timed out")),
        ("GET", httpx.ReadTimeout("timed out")),
    ],
)
def test_no_resend_warning_when_nothing_changed_or_nothing_arrived(
    method: str, error: httpx.TransportError
) -> None:
    api = FakeApi().on(method, "/v1/customers", error)

    result = send(client_for(api), method, "/v1/customers")

    assert result.failure is not None
    assert "may already have reached the server" not in result.failure.hint


def test_connection_refused_hint_names_the_port() -> None:
    error = chained(httpx.ConnectError("refused"), ConnectionRefusedError(10061, "refused"))
    api = FakeApi().on("GET", "/health", error)

    result = send(client_for(api), "GET", "/health")

    assert result.failure is not None
    assert result.failure.summary == "Nothing accepted the connection on 127.0.0.1:8001."


def test_credentials_come_from_settings_and_are_masked() -> None:
    settings = Settings(api_key=SecretStr(API_KEY))

    credentials = resolve_credentials(settings, no_auth=False, key_env=None)

    assert credentials.describe() == "API key bk_juniper01*** from SUPPORTOPS_API_KEY"
    assert API_KEY not in repr(credentials)
    assert auth_headers(credentials, BILLING) == {"Authorization": f"Bearer {API_KEY}"}


def test_credentials_from_another_environment_variable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OTHER_KEY", API_KEY)

    credentials = resolve_credentials(Settings(), no_auth=False, key_env="OTHER_KEY")

    assert credentials.describe() == "API key bk_juniper01*** from environment variable OTHER_KEY"


def test_missing_key_environment_variable_is_a_config_error() -> None:
    with pytest.raises(ConfigError, match="MISSING_KEY is not set"):
        resolve_credentials(Settings(), no_auth=False, key_env="MISSING_KEY")


def test_no_auth_and_key_env_together_is_a_config_error() -> None:
    with pytest.raises(ConfigError, match="not both"):
        resolve_credentials(Settings(), no_auth=True, key_env="X")


def test_no_auth_sends_no_authorization_header() -> None:
    credentials = resolve_credentials(Settings(), no_auth=True, key_env=None)

    assert auth_headers(credentials, BILLING) == {}
    assert credentials.describe() == "no credentials (--no-auth)"


def test_keys_with_whitespace_are_rejected_before_sending() -> None:
    settings = Settings(api_key=SecretStr(API_KEY + " "))

    with pytest.raises(ConfigError, match="spaces or line breaks") as excinfo:
        resolve_credentials(settings, no_auth=False, key_env=None)

    assert API_KEY not in excinfo.value.message


def result_with(status: int, code: str | None = None, **headers: str) -> HttpResult:
    from supportops.http_checks import Problem

    return HttpResult(
        method="GET",
        url="http://127.0.0.1:8001/v1/account",
        request_id="supportops-abc",
        response_request_id="supportops-abc",
        duration_ms=1.0,
        status=status,
        headers=headers,
        problem=Problem(code=code) if code else None,
    )


def test_401_hint_names_the_masked_key_and_the_request_id() -> None:
    credentials = Credentials(api_key=API_KEY, source="SUPPORTOPS_API_KEY")

    hint = response_hint(result_with(401, "UNAUTHENTICATED"), credentials, BILLING)

    assert hint is not None
    assert "bk_juniper01***" in hint
    assert "supportops-abc" in hint
    assert API_KEY not in hint


@pytest.mark.parametrize(
    ("status", "code", "headers", "expected"),
    [
        (200, None, {}, None),
        (403, "ACCOUNT_SUSPENDED", {}, "suspended"),
        (404, "RESOURCE_NOT_FOUND", {}, "supportops api request GET /v1/account"),
        (409, "INVOICE_NOT_PAYABLE", {}, "conflicts"),
        (422, "VALIDATION_FAILED", {}, "rejected some fields"),
        (500, "INTERNAL_ERROR", {}, "supportops-abc"),
        (503, "DATABASE_BUSY", {"retry-after": "5"}, "retrying after 5 seconds"),
    ],
)
def test_status_specific_hints(
    status: int, code: str | None, headers: dict[str, str], expected: str | None
) -> None:
    hint = response_hint(
        result_with(status, code, **headers), Credentials(api_key=None, source="none"), BILLING
    )

    if expected is None:
        assert hint is None
    else:
        assert hint is not None
        assert expected in hint
