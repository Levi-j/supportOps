import json
from collections.abc import Callable
from pathlib import Path
from typing import NoReturn

import httpx
import psycopg
import pytest
from typer.testing import CliRunner, Result

from supportops import health as health_module
from supportops.cli import api as api_cli
from supportops.cli import health as health_cli
from supportops.cli.main import app
from supportops.db.connection import DatabaseProbe
from supportops.http_checks import create_client
from supportops.settings import Settings
from tests.unit.fake_api import FakeApi, lab_health, respond

API_KEY = "bk_juniper01_lab_only_not_a_real_key"
OTHER_KEY = "bk_kestrel01_lab_only_not_a_real_key"
DB_PASSWORD = "CliProbePw42"
DB_URL = f"postgresql://supportops_ro:{DB_PASSWORD}@127.0.0.1:5433/billing"
READY = {"status": "ready", "checks": {"database": {"status": "up", "latency_ms": 2}}}
NOT_READY = {
    "status": "not_ready",
    "checks": {"database": {"status": "down", "error": "connection_refused", "latency_ms": 2}},
}

runner = CliRunner()


@pytest.fixture
def api(monkeypatch: pytest.MonkeyPatch) -> FakeApi:
    fake = FakeApi()

    def create_fake_client(settings: Settings) -> httpx.Client:
        return create_client(settings, transport=fake.transport)

    monkeypatch.setattr(api_cli, "create_client", create_fake_client)
    monkeypatch.setattr(health_cli, "create_client", create_fake_client)
    return fake


def invoke(*args: str, **env: str) -> Result:
    return runner.invoke(app, list(args), env={"COLUMNS": "200", **env})


def flat(text: str) -> str:
    return " ".join(text.split())


def write_file(directory: Path, name: str, content: str) -> str:
    path = directory / name
    path.write_bytes(content.encode("utf-8"))
    return str(path)


def refused() -> httpx.ConnectError:
    error = httpx.ConnectError("[WinError 10061] No connection could be made")
    error.__cause__ = ConnectionRefusedError(10061, "refused")
    return error


def reachable_database(*_args: object) -> DatabaseProbe:
    return DatabaseProbe(
        target="supportops_ro@127.0.0.1:5433/billing",
        reachable=True,
        server_answered=True,
        latency_ms=3,
        user="supportops_ro",
        server_version="18.0",
        read_only=True,
    )


def failing_connect(error: psycopg.Error) -> Callable[..., NoReturn]:
    def connect(*_args: object, **_kwargs: object) -> NoReturn:
        raise error

    return connect


def test_get_request_reports_status_request_id_and_body(api: FakeApi) -> None:
    api.on("GET", "/v1/account", respond(200, {"id": "acct_juniper", "name": "Juniper Dental"}))

    result = invoke("api", "request", "get", "/v1/account", SUPPORTOPS_API_KEY=API_KEY)

    sent = api.requests[0]
    assert result.exit_code == 0
    assert sent.method == "GET"
    assert sent.headers["Authorization"] == f"Bearer {API_KEY}"
    assert "200 OK in" in result.stdout
    assert f"Request ID: {sent.headers['X-Request-Id']} (echoed by the server)" in result.stdout
    assert "Credentials: API key bk_juniper01*** from SUPPORTOPS_API_KEY" in result.stdout
    assert '"name": "Juniper Dental"' in result.stdout
    assert API_KEY not in result.output


def test_json_output_contains_the_whole_report(api: FakeApi) -> None:
    api.on("GET", "/v1/account", respond(200, {"id": "acct_juniper"}))

    result = invoke("api", "request", "GET", "/v1/account", "--json", SUPPORTOPS_API_KEY=API_KEY)

    report = json.loads(result.stdout)
    assert report["result"]["status"] == 200
    assert report["result"]["request_id"] == api.requests[0].headers["X-Request-Id"]
    assert report["credentials"] == "API key bk_juniper01*** from SUPPORTOPS_API_KEY"
    assert report["hint"] is None


def test_rejected_key_exits_1_with_a_masked_explanation(api: FakeApi) -> None:
    problem = {
        "type": "about:blank",
        "title": "Unauthorized",
        "status": 401,
        "detail": "A valid API key is required.",
        "code": "UNAUTHENTICATED",
    }
    api.on(
        "GET",
        "/v1/account",
        respond(401, problem, problem=True, headers={"WWW-Authenticate": "Bearer"}),
    )

    result = invoke("api", "request", "GET", "/v1/account", SUPPORTOPS_API_KEY=API_KEY)

    assert result.exit_code == 1
    assert "401 Unauthorized" in result.stdout
    assert "Problem: UNAUTHENTICATED - A valid API key is required." in result.stdout
    assert "www-authenticate: Bearer" in result.stdout
    assert "rejected the credentials (API key bk_juniper01*** from SUPPORTOPS_API_KEY)" in (
        result.stdout
    )
    assert API_KEY not in result.output


def test_validation_errors_are_listed_by_field(api: FakeApi) -> None:
    problem = {
        "title": "Unprocessable Content",
        "status": 422,
        "code": "VALIDATION_FAILED",
        "detail": "The request contains missing or invalid fields.",
        "errors": [
            {"location": "body", "field": "email", "message": "Field required", "type": "missing"}
        ],
    }
    api.on("GET", "/health", lab_health()).on(
        "POST", "/v1/customers", respond(422, problem, problem=True)
    )

    result = invoke(
        "api", "request", "POST", "/v1/customers", "--data", '{"name": "Acme"}', "--yes"
    )

    assert result.exit_code == 1
    assert "  body.email: Field required (missing)" in result.stdout
    assert "rejected some fields" in result.stdout


def test_write_without_yes_is_refused_before_anything_is_sent(api: FakeApi, tmp_path: Path) -> None:
    body = write_file(tmp_path, "customer.json", '{"name": "Acme"}')

    result = invoke("api", "request", "POST", "/v1/customers", "--data-file", body)

    assert result.exit_code == 2
    assert "needs --yes" in flat(result.stderr)
    assert "Nothing was sent" in flat(result.stderr)
    assert api.requests == []


@pytest.mark.parametrize("method", ["PUT", "PATCH", "DELETE"])
def test_every_write_method_needs_yes(api: FakeApi, method: str) -> None:
    result = invoke("api", "request", method, "/v1/customers/cus_1")

    assert result.exit_code == 2
    assert api.requests == []


def test_write_to_a_remote_api_is_refused_before_anything_is_sent(api: FakeApi) -> None:
    result = invoke(
        "api",
        "request",
        "POST",
        "/v1/customers",
        "--data",
        "{}",
        "--yes",
        SUPPORTOPS_API_URL="https://api.example.com",
    )

    assert result.exit_code == 2
    assert "only allowed against the local lab" in flat(result.stderr)
    assert api.requests == []


def test_write_is_refused_when_the_service_does_not_report_lab(api: FakeApi) -> None:
    api.on("GET", "/health", lab_health("production"))

    result = invoke("api", "request", "POST", "/v1/customers", "--data", "{}", "--yes")

    assert result.exit_code == 2
    assert "instead of 'lab'" in flat(result.stderr)
    assert api.methods == ["GET"]


def test_confirmed_write_to_the_lab_is_sent_once(api: FakeApi) -> None:
    api.on("GET", "/health", lab_health()).on(
        "POST", "/v1/customers", respond(201, {"id": "cus_new"})
    )
    body = '{"name": "Acme", "email": "billing@acme.example"}'

    result = invoke(
        "api",
        "request",
        "POST",
        "/v1/customers",
        "--data",
        body,
        "--yes",
        SUPPORTOPS_API_KEY=API_KEY,
    )

    post = api.requests[1]
    assert result.exit_code == 0
    assert api.methods == ["GET", "POST"]
    assert post.headers["Content-Type"] == "application/json"
    assert post.headers["Authorization"] == f"Bearer {API_KEY}"
    assert json.loads(post.content) == {"name": "Acme", "email": "billing@acme.example"}
    assert "201 Created" in result.stdout


def test_a_write_that_times_out_is_not_retried(api: FakeApi) -> None:
    api.on("GET", "/health", lab_health()).on(
        "POST", "/v1/invoices/inv_1/pay", httpx.ReadTimeout("timed out")
    )

    result = invoke("api", "request", "POST", "/v1/invoices/inv_1/pay", "--yes")

    assert result.exit_code == 1
    assert api.methods == ["GET", "POST"]
    assert "read_timeout" in result.stdout
    assert "never retries a write" in result.stdout


def test_inline_json_without_quotes_gets_the_powershell_hint(api: FakeApi) -> None:
    result = invoke("api", "request", "POST", "/v1/customers", "--data", "{name:Acme}", "--yes")

    error = flat(result.stderr)
    assert result.exit_code == 2
    assert "The --data value is not valid JSON" in error
    assert "Windows PowerShell 5.1 strips embedded double quotes" in error
    assert "--data-file" in error
    assert api.requests == []


def test_invalid_json_in_a_file_is_refused_without_the_powershell_hint(
    api: FakeApi, tmp_path: Path
) -> None:
    body = write_file(tmp_path, "broken.json", '{"name": "Acme",}')

    result = invoke("api", "request", "POST", "/v1/customers", "--data-file", body, "--yes")

    assert result.exit_code == 2
    assert "is not valid JSON" in flat(result.stderr)
    assert "PowerShell" not in result.stderr
    assert api.requests == []


def test_raw_sends_an_invalid_body_unchanged(api: FakeApi) -> None:
    api.on("GET", "/health", lab_health()).on(
        "POST", "/v1/customers", respond(400, {"code": "MALFORMED_REQUEST"}, problem=True)
    )

    result = invoke(
        "api", "request", "POST", "/v1/customers", "--data", "{name:Acme}", "--raw", "--yes"
    )

    assert result.exit_code == 1
    assert api.requests[1].content == b"{name:Acme}"
    assert "Problem: MALFORMED_REQUEST" in result.stdout


def test_body_from_a_file_is_sent_byte_for_byte(api: FakeApi, tmp_path: Path) -> None:
    content = '{\n  "name": "Acme",\n  "email": "billing@acme.example"\n}\n'
    body = write_file(tmp_path, "customer.json", content)
    api.on("GET", "/health", lab_health()).on(
        "POST", "/v1/customers", respond(201, {"id": "cus_new"})
    )

    result = invoke("api", "request", "POST", "/v1/customers", "--data-file", body, "--yes")

    assert result.exit_code == 0
    assert api.requests[1].content == content.encode("utf-8")
    assert api.requests[1].headers["Content-Type"] == "application/json"


@pytest.mark.parametrize(
    ("args", "message"),
    [
        (["--data", "{}", "--data-file", "body.json"], "either --data or --data-file"),
        (["--data-file", "missing.json"], "File not found: missing.json"),
    ],
)
def test_body_option_mistakes_exit_2(api: FakeApi, args: list[str], message: str) -> None:
    result = invoke("api", "request", "POST", "/v1/customers", *args, "--yes")

    assert result.exit_code == 2
    assert message in flat(result.stderr)
    assert api.requests == []


def test_extra_headers_are_sent(api: FakeApi) -> None:
    api.on("GET", "/v1/account", respond(200, {}))

    result = invoke(
        "api",
        "request",
        "GET",
        "/v1/account",
        "-H",
        "Accept-Language: de-CH",
        "--header",
        "X-Debug:1",
    )

    assert result.exit_code == 0
    assert api.requests[0].headers["Accept-Language"] == "de-CH"
    assert api.requests[0].headers["X-Debug"] == "1"


@pytest.mark.parametrize(
    ("header", "message"),
    [
        ("Authorization: Bearer sneaky-token-123", "can't be set with --header"),
        ("Cookie: session=sneaky-token-123", "can't be set with --header"),
        ("X-Request-Id: abc", "Use --request-id"),
        ("NoColonHere", "must look like 'Name: value'"),
    ],
)
def test_credential_and_malformed_headers_are_refused(
    api: FakeApi, header: str, message: str
) -> None:
    result = invoke("api", "request", "GET", "/v1/account", "-H", header)

    assert result.exit_code == 2
    assert message in flat(result.stderr)
    assert "sneaky-token-123" not in result.output
    assert api.requests == []


def test_explicit_request_id_is_sent_and_printed(api: FakeApi) -> None:
    api.on("GET", "/v1/account", respond(200, {}))

    result = invoke("api", "request", "GET", "/v1/account", "--request-id", "ticket-4711")

    assert api.requests[0].headers["X-Request-Id"] == "ticket-4711"
    assert "Request ID: ticket-4711 (echoed by the server)" in result.stdout


def test_a_request_id_replaced_by_the_server_is_pointed_out(api: FakeApi) -> None:
    api.on(
        "GET",
        "/v1/account",
        lambda _request: httpx.Response(200, json={}, headers={"X-Request-Id": "srv-123"}),
    )

    result = invoke("api", "request", "GET", "/v1/account")

    assert "(the server used srv-123 instead)" in result.stdout


def test_invalid_request_id_is_refused(api: FakeApi) -> None:
    result = invoke("api", "request", "GET", "/v1/account", "--request-id", "not valid!")

    assert result.exit_code == 2
    assert api.requests == []


def test_key_env_reads_the_key_from_another_variable(api: FakeApi) -> None:
    api.on("GET", "/v1/account", respond(200, {}))

    result = invoke(
        "api",
        "request",
        "GET",
        "/v1/account",
        "--key-env",
        "KESTREL_KEY",
        SUPPORTOPS_API_KEY=API_KEY,
        KESTREL_KEY=OTHER_KEY,
    )

    assert api.requests[0].headers["Authorization"] == f"Bearer {OTHER_KEY}"
    assert "API key bk_kestrel01*** from environment variable KESTREL_KEY" in result.stdout
    assert OTHER_KEY not in result.output


def test_no_auth_sends_no_authorization_header(api: FakeApi) -> None:
    api.on("GET", "/health", lab_health())

    result = invoke("api", "request", "GET", "/health", "--no-auth", SUPPORTOPS_API_KEY=API_KEY)

    assert "authorization" not in api.requests[0].headers
    assert "Credentials: no credentials (--no-auth)" in result.stdout


def test_missing_key_env_variable_exits_2(api: FakeApi) -> None:
    result = invoke("api", "request", "GET", "/v1/account", "--key-env", "SUPPORTOPS_TEST_UNSET")

    assert result.exit_code == 2
    assert "SUPPORTOPS_TEST_UNSET is not set" in flat(result.stderr)
    assert api.requests == []


@pytest.mark.parametrize(
    ("args", "message"),
    [
        (["TRACE", "/v1/account"], "Unsupported HTTP method 'TRACE'"),
        (["GET", "v1/account"], "The path must start with '/'"),
    ],
)
def test_usage_mistakes_exit_2(api: FakeApi, args: list[str], message: str) -> None:
    result = invoke("api", "request", *args)

    assert result.exit_code == 2
    assert message in flat(result.stderr)
    assert api.requests == []


def test_long_bodies_are_truncated_unless_full_is_given(api: FakeApi) -> None:
    api.on("GET", "/v1/invoices", respond(200, {"data": ["x" * 50] * 200}))

    preview = invoke("api", "request", "GET", "/v1/invoices")
    full = invoke("api", "request", "GET", "/v1/invoices", "--full")

    assert "truncated after 4000 of" in preview.stdout
    assert "use --full to see everything" in preview.stdout
    assert "truncated" not in full.stdout
    assert full.stdout.count("x" * 50) == 200


def test_secrets_in_a_response_body_are_redacted_in_text_and_json(api: FakeApi) -> None:
    leaky = {"token": "abcd1234efgh5678", "api_key": API_KEY, "contact": "billing@acme.example"}
    api.on("GET", "/v1/debug", respond(200, leaky))

    text = invoke("api", "request", "GET", "/v1/debug")
    as_json = invoke("api", "request", "GET", "/v1/debug", "--json")

    assert json.loads(as_json.stdout)["result"]["status"] == 200
    for output in (text.output, as_json.output):
        assert "abcd1234efgh5678" not in output
        assert API_KEY not in output
        assert "billing@acme.example" not in output


def test_connection_failure_exits_1_and_explains_it(api: FakeApi) -> None:
    api.on("GET", "/v1/account", refused())

    result = invoke("api", "request", "GET", "/v1/account")

    assert result.exit_code == 1
    assert "connection_refused" in result.stdout
    assert "Nothing accepted the connection on 127.0.0.1:8001." in result.stdout
    assert "Detail: [WinError 10061]" in result.stdout
    assert "no response, so the API has nothing logged under it" in result.stdout
    assert "Hint: The service isn't running" in result.stdout


def test_latency_sends_only_get_requests_and_reports_percentiles(api: FakeApi) -> None:
    api.on("GET", "/v1/invoices", respond(200, {"data": []}))

    result = invoke("api", "latency", "/v1/invoices", "--count", "5", SUPPORTOPS_API_KEY=API_KEY)

    assert result.exit_code == 0
    assert api.methods == ["GET"] * 5
    assert all(request.headers["Authorization"] == f"Bearer {API_KEY}" for request in api.requests)
    assert "Responses: 200 x5" in result.stdout
    assert "p95" in result.stdout
    assert "All requests succeeded within the threshold." in result.stdout
    assert API_KEY not in result.output


@pytest.mark.parametrize("count", ["0", "101"])
def test_latency_count_is_limited_to_100(api: FakeApi, count: str) -> None:
    result = invoke("api", "latency", "/v1/invoices", "--count", count)

    assert result.exit_code == 2
    assert api.requests == []


def test_latency_failures_exit_1(api: FakeApi) -> None:
    api.on("GET", "/v1/invoices", respond(503, {"code": "SERVICE_UNAVAILABLE"}, problem=True))

    result = invoke("api", "latency", "/v1/invoices", "-n", "3")

    assert result.exit_code == 1
    assert "Responses: 503 x3" in result.stdout
    assert "No successful responses" in result.stdout


def test_latency_threshold_defaults_to_the_setting(api: FakeApi) -> None:
    api.on("GET", "/v1/invoices", respond(200, {"data": []}))

    result = invoke(
        "api", "latency", "/v1/invoices", "-n", "2", "--json", SUPPORTOPS_SLOW_REQUEST_MS="250"
    )

    report = json.loads(result.stdout)
    assert report["threshold_ms"] == 250
    assert report["requested"] == 2
    assert report["successful"] == 2


def test_health_reports_healthy_and_exits_0(api: FakeApi, monkeypatch: pytest.MonkeyPatch) -> None:
    api.on("GET", "/health", lab_health()).on("GET", "/health/ready", respond(200, READY))
    monkeypatch.setattr(health_module, "probe_database", reachable_database)

    result = invoke("health", SUPPORTOPS_DB_URL=DB_URL)

    output = flat(result.stdout)
    assert result.exit_code == 0
    assert "HEALTHY The API is running and ready to serve requests." in output
    assert "GET /health -> 200 OK" in output
    assert "GET /health/ready -> 200 OK (database: up)" in output
    assert "reachable as supportops_ro@127.0.0.1:5433/billing" in output
    assert api.methods == ["GET", "GET"]


def test_health_points_to_the_api_when_postgres_answers(
    api: FakeApi, monkeypatch: pytest.MonkeyPatch
) -> None:
    api.on("GET", "/health", lab_health()).on("GET", "/health/ready", respond(503, NOT_READY))
    monkeypatch.setattr(health_module, "probe_database", reachable_database)

    result = invoke("health", SUPPORTOPS_DB_URL=DB_URL)

    output = flat(result.stdout)
    assert result.exit_code == 1
    assert "DEGRADED" in output
    assert "while PostgreSQL answers from this machine" in output


def test_health_reports_a_likely_database_outage(
    api: FakeApi, monkeypatch: pytest.MonkeyPatch
) -> None:
    api.on("GET", "/health", lab_health()).on("GET", "/health/ready", respond(503, NOT_READY))
    monkeypatch.setattr(
        psycopg, "connect", failing_connect(psycopg.OperationalError("connection timeout expired"))
    )

    result = invoke("health", "--json", SUPPORTOPS_DB_URL=DB_URL)

    report = json.loads(result.stdout)
    assert result.exit_code == 1
    assert report["verdict"] == "DEGRADED"
    assert report["diagnosis"] == "database_outage"
    assert report["database"]["error"] == "timeout"
    assert DB_PASSWORD not in result.output


def test_health_reports_down_when_the_api_is_unreachable(
    api: FakeApi, monkeypatch: pytest.MonkeyPatch
) -> None:
    api.on("GET", "/health", refused())
    monkeypatch.setattr(health_module, "probe_database", reachable_database)

    result = invoke("health", SUPPORTOPS_DB_URL=DB_URL)

    output = flat(result.stdout)
    assert result.exit_code == 1
    assert "DOWN The API could not be reached." in output
    assert "not checked (the API isn't live)" in output
    assert "API liveness: [WinError 10061]" in output
    assert "limited to the API process" in output
    assert api.methods == ["GET"]


def test_health_never_prints_a_password_echoed_by_the_driver(
    api: FakeApi, monkeypatch: pytest.MonkeyPatch
) -> None:
    password = "Pr0be%zzSecret"
    url = f"postgresql://supportops_ro:{password}@127.0.0.1:5433/billing"
    api.on("GET", "/health", lab_health()).on("GET", "/health/ready", respond(200, READY))
    echoed = psycopg.ProgrammingError(f'invalid percent-encoded token: "{password}"')
    monkeypatch.setattr(psycopg, "connect", failing_connect(echoed))

    text = invoke("health", SUPPORTOPS_DB_URL=url)
    as_json = invoke("health", "--json", SUPPORTOPS_DB_URL=url)

    assert text.exit_code == 0
    assert 'PostgreSQL: invalid percent-encoded token: "***"' in text.stdout
    for result in (text, as_json):
        assert "Pr0be" not in result.output
