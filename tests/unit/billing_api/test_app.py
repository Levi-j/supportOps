import re

from fastapi.testclient import TestClient

from billing_api.database import DatabaseStatus
from tests.log_capture import JsonCapture
from tests.unit.billing_api.support import FAKE_DB_PASSWORD, AppFactory


def app_entries(logs: JsonCapture) -> list[dict[str, object]]:
    return [entry for entry in logs.entries if str(entry["logger"]).startswith("billing_api")]


UUID_PATTERN = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")


def test_liveness_reports_the_environment(make_app: AppFactory) -> None:
    response = TestClient(make_app()).get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok", "environment": "lab"}


def test_generates_a_request_id_when_none_is_sent(make_app: AppFactory) -> None:
    response = TestClient(make_app()).get("/health")

    assert UUID_PATTERN.fullmatch(response.headers["X-Request-Id"])


def test_echoes_a_valid_client_request_id(make_app: AppFactory) -> None:
    response = TestClient(make_app()).get("/health", headers={"X-Request-Id": "ticket-4711_a"})

    assert response.headers["X-Request-Id"] == "ticket-4711_a"


def test_replaces_an_invalid_request_id_without_logging_it(
    make_app: AppFactory, logs: JsonCapture
) -> None:
    invalid = "bad id <script>"

    response = TestClient(make_app()).get("/health", headers={"X-Request-Id": invalid})

    assert UUID_PATTERN.fullmatch(response.headers["X-Request-Id"])
    assert all(invalid not in str(entry) for entry in logs.entries)


def test_rejects_request_ids_longer_than_64_characters(make_app: AppFactory) -> None:
    response = TestClient(make_app()).get("/health", headers={"X-Request-Id": "a" * 65})

    assert response.headers["X-Request-Id"] != "a" * 65


def test_writes_one_access_log_without_the_query_string(
    make_app: AppFactory, logs: JsonCapture
) -> None:
    TestClient(make_app()).get(
        "/health?token=secret-value",
        headers={"X-Request-Id": "access-1", "User-Agent": "curl/8.9.1"},
    )

    [access] = logs.events("http.request")
    assert access["request_id"] == "access-1"
    assert access["method"] == "GET"
    assert access["path"] == "/health"
    assert access["status"] == 200
    assert access["user_agent"] == "curl/8.9.1"
    assert isinstance(access["duration_ms"], int)
    assert "secret-value" not in str(app_entries(logs))


def test_readiness_when_the_database_is_up(make_app: AppFactory) -> None:
    response = TestClient(make_app(DatabaseStatus(up=True, latency_ms=3))).get("/health/ready")

    assert response.status_code == 200
    assert response.json() == {
        "status": "ready",
        "checks": {"database": {"status": "up", "latency_ms": 3}},
    }


def test_readiness_returns_503_when_the_database_is_down(make_app: AppFactory) -> None:
    down = DatabaseStatus(up=False, latency_ms=12, error="connection_refused")

    response = TestClient(make_app(down)).get("/health/ready")

    assert response.status_code == 503
    assert response.json() == {
        "status": "not_ready",
        "checks": {"database": {"status": "down", "latency_ms": 12, "error": "connection_refused"}},
    }


def test_liveness_stays_up_when_the_database_is_down(make_app: AppFactory) -> None:
    down = DatabaseStatus(up=False, latency_ms=12, error="connection_refused")

    assert TestClient(make_app(down)).get("/health").status_code == 200


def test_unknown_route_returns_problem_json(make_app: AppFactory) -> None:
    response = TestClient(make_app()).get("/v1/nothing", headers={"X-Request-Id": "missing-1"})

    assert response.status_code == 404
    assert response.headers["content-type"] == "application/problem+json"
    body = response.json()
    assert body["code"] == "RESOURCE_NOT_FOUND"
    assert body["status"] == 404
    assert body["request_id"] == "missing-1"


def test_wrong_method_returns_problem_json_with_allow_header(make_app: AppFactory) -> None:
    response = TestClient(make_app()).post("/health")

    assert response.status_code == 405
    assert response.json()["code"] == "METHOD_NOT_ALLOWED"
    assert "GET" in response.headers["allow"]


def test_unexpected_errors_return_a_generic_500_and_log_one_stack_trace(
    make_app: AppFactory, logs: JsonCapture
) -> None:
    app = make_app()

    @app.get("/boom")
    def boom() -> dict[str, str]:
        raise RuntimeError("internal detail that must not reach the client")

    response = TestClient(app).get("/boom", headers={"X-Request-Id": "boom-1"})

    assert response.status_code == 500
    body = response.json()
    assert body["code"] == "INTERNAL_ERROR"
    assert body["request_id"] == "boom-1"
    assert "internal detail" not in response.text
    assert response.headers["X-Request-Id"] == "boom-1"
    [error] = [entry for entry in logs.entries if "stack_trace" in entry]
    assert error["event_name"] == "unhandled_exception"
    assert error["request_id"] == "boom-1"
    assert error["error_type"] == "RuntimeError"
    [access] = logs.events("http.request")
    assert access["status"] == 500


def test_startup_logs_a_redacted_configuration_summary(
    make_app: AppFactory, logs: JsonCapture
) -> None:
    with TestClient(make_app()):
        pass

    [started] = logs.events("app.started")
    assert started["environment"] == "lab"
    assert started["database_host"] == "postgres"
    assert started["database_name"] == "billing"
    assert started["database_user"] == "billing_app"
    assert FAKE_DB_PASSWORD not in str(logs.entries)
