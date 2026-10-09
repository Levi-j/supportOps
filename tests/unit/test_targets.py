import json
from collections.abc import Iterator
from contextlib import contextmanager

import httpx
import pytest
from typer.testing import CliRunner, Result

from supportops.cli import api as api_cli
from supportops.cli import health as health_cli
from supportops.cli.main import app
from supportops.db import runner
from supportops.db.catalog import CATALOG
from supportops.db.connection import DatabaseProbe, SessionInfo
from supportops.db.runner import plan_checks
from supportops.errors import ConfigError, ExitCode
from supportops.health import Verdict, assess_actuator
from supportops.http_checks import HttpResult, TransportFailure, create_client
from supportops.logs.parser import parse_line
from supportops.settings import Settings, load_settings
from supportops.targets import BILLING, ORDERFLOW, TARGETS, get_target
from supportops.write_guard import WriteRefused, check_method_allowed, check_write_request
from tests.unit.fake_api import FakeApi, lab_health, respond
from tests.unit.test_db_auth_cli import FakeConnection, FakeCursor

TOKEN = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiI1In0.c2lnbmF0dXJlLW5vdC1yZWFs"
ORDERFLOW_ENV = {"SUPPORTOPS_TARGET": "orderflow", "SUPPORTOPS_API_URL": "http://127.0.0.1:8080"}
UP = {"status": "UP", "groups": ["liveness", "readiness"]}
DOWN = {"status": "DOWN"}

runner_cli = CliRunner()


def invoke(*args: str, **env: str) -> Result:
    return runner_cli.invoke(app, list(args), env={"COLUMNS": "200", **env})


def flat(text: str) -> str:
    return " ".join(text.split())


@pytest.fixture
def api(monkeypatch: pytest.MonkeyPatch) -> FakeApi:
    fake = FakeApi()

    def create_fake_client(settings: Settings) -> httpx.Client:
        return create_client(settings, transport=fake.transport)

    monkeypatch.setattr(api_cli, "create_client", create_fake_client)
    monkeypatch.setattr(health_cli, "create_client", create_fake_client)
    return fake


def test_billing_stays_the_default_target() -> None:
    assert Settings().target == "billing"
    assert get_target("billing") is BILLING
    assert set(TARGETS) == {"billing", "orderflow"}


def test_orderflow_is_selected_explicitly(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SUPPORTOPS_TARGET", "orderflow")

    assert load_settings().settings.target == "orderflow"


def test_unknown_targets_are_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SUPPORTOPS_TARGET", "payments")

    with pytest.raises(ConfigError, match="SUPPORTOPS_TARGET"):
        load_settings()
    with pytest.raises(ConfigError, match="Unknown target"):
        get_target("payments")


def test_the_orderflow_profile_matches_the_service() -> None:
    assert ORDERFLOW.liveness_path == "/actuator/health/liveness"
    assert ORDERFLOW.readiness_path == "/actuator/health"
    assert ORDERFLOW.account_path == "/api/v1/users/me"
    assert ORDERFLOW.lab_writes_allowed is False
    assert ORDERFLOW.allowed_methods == {"GET", "HEAD", "OPTIONS"}
    assert ORDERFLOW.check_packs == {"generic", "orderflow"}
    assert ORDERFLOW.api_key is None
    assert ORDERFLOW.jwt is not None
    assert ORDERFLOW.jwt.issuer == "orderflow"
    assert ORDERFLOW.credential_name == "bearer token"
    assert BILLING.credential_name == "API key"
    assert BILLING.check_packs == {"generic", "billing"}


@pytest.mark.parametrize("method", ["POST", "PUT", "PATCH", "DELETE"])
@pytest.mark.parametrize("confirmed", [False, True])
def test_orderflow_refuses_every_write_even_with_yes(method: str, confirmed: bool) -> None:
    with pytest.raises(WriteRefused, match="not allowed against the 'orderflow' target") as excinfo:
        check_write_request(
            method, confirmed=confirmed, api_url="http://127.0.0.1:8080", profile=ORDERFLOW
        )

    assert excinfo.value.exit_code == ExitCode.USAGE
    with pytest.raises(WriteRefused):
        check_method_allowed(method, ORDERFLOW)


@pytest.mark.parametrize("method", ["GET", "head", "OPTIONS"])
def test_orderflow_allows_read_methods(method: str) -> None:
    check_method_allowed(method, ORDERFLOW)


@pytest.mark.parametrize("method", ["POST", "PUT", "PATCH", "DELETE"])
@pytest.mark.parametrize("extra", [[], ["--yes"]])
def test_no_orderflow_write_reaches_the_network(
    api: FakeApi, method: str, extra: list[str]
) -> None:
    api.on("GET", "/health", lab_health("lab")).on(method, "/api/v1/orders", respond(201, {}))

    result = invoke(
        "api", "request", method, "/api/v1/orders", "--data", "{}", *extra, **ORDERFLOW_ENV
    )

    assert result.exit_code == 2
    assert "Nothing was sent" in flat(result.stderr)
    assert "--yes doesn't change this" in flat(result.stderr)
    assert api.requests == []


def test_billing_writes_keep_their_lab_guard(api: FakeApi) -> None:
    api.on("GET", "/health", lab_health()).on("POST", "/v1/customers", respond(201, {"id": "c"}))

    refused = invoke("api", "request", "POST", "/v1/customers", "--data", "{}")
    allowed = invoke("api", "request", "POST", "/v1/customers", "--data", "{}", "--yes")

    assert refused.exit_code == 2
    assert "needs --yes" in flat(refused.stderr)
    assert allowed.exit_code == 0
    assert api.methods == ["GET", "POST"]


def test_orderflow_gets_use_the_bearer_token_and_never_print_it(
    api: FakeApi, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ORDERFLOW_TOKEN", TOKEN)
    api.on("GET", "/api/v1/products", respond(200, {"content": []}))

    result = invoke(
        "api", "request", "GET", "/api/v1/products", "--key-env", "ORDERFLOW_TOKEN", **ORDERFLOW_ENV
    )

    assert result.exit_code == 0
    assert api.requests[0].headers["Authorization"] == f"Bearer {TOKEN}"
    assert "bearer token *** from environment variable ORDERFLOW_TOKEN" in flat(result.stdout)
    assert TOKEN not in result.output
    assert TOKEN.split(".")[1] not in result.output


def test_a_rejected_orderflow_token_points_to_local_inspection(
    api: FakeApi, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ORDERFLOW_TOKEN", TOKEN)
    problem = {"status": 401, "code": "UNAUTHENTICATED", "requestId": "abc"}
    api.on(
        "GET",
        "/api/v1/users/me",
        respond(
            401, problem, problem=True, headers={"WWW-Authenticate": 'Bearer error="invalid_token"'}
        ),
    )

    result = invoke(
        "api",
        "request",
        "GET",
        "/api/v1/users/me",
        "--key-env",
        "ORDERFLOW_TOKEN",
        "--json",
        **ORDERFLOW_ENV,
    )

    report = json.loads(result.stdout)
    assert result.exit_code == 1
    assert "sent but refused (invalid_token)" in report["hint"]
    assert "supportops auth check --key-env" in report["hint"]
    assert TOKEN not in result.output


def http(path: str, status: int | None = 200, failure: str | None = None) -> HttpResult:
    return HttpResult(
        method="GET",
        url=f"http://127.0.0.1:8080{path}",
        request_id="supportops-test",
        duration_ms=4.0,
        status=status,
        failure=TransportFailure(category=failure, summary="It failed.", detail="x", hint="Fix it.")
        if failure
        else None,
    )


def probe(error: str | None = None) -> DatabaseProbe:
    return DatabaseProbe(
        target="supportops_orderflow_ro@127.0.0.1:5432/orderflow",
        reachable=error is None,
        server_answered=error in (None, "authentication_failed"),
        latency_ms=3,
        error=error,
    )


LIVE = http("/actuator/health/liveness")


@pytest.mark.parametrize(
    ("liveness", "health", "database", "verdict", "diagnosis"),
    [
        (http("/x", None, "connection_refused"), None, probe(), Verdict.DOWN, "api_unreachable"),
        (
            http("/x", 404),
            http("/actuator/health"),
            probe(),
            Verdict.INCONCLUSIVE,
            "liveness_endpoint_unavailable",
        ),
        (http("/x", 500), None, probe(), Verdict.INCONCLUSIVE, "unexpected_liveness_response"),
        (LIVE, http("/actuator/health"), probe(), Verdict.HEALTHY, "api_ready"),
        (
            LIVE,
            http("/actuator/health", 503),
            probe(),
            Verdict.DEGRADED,
            "health_down_database_reachable",
        ),
        (
            LIVE,
            http("/actuator/health", 503),
            probe("authentication_failed"),
            Verdict.DEGRADED,
            "health_down_database_reachable",
        ),
        (
            LIVE,
            http("/actuator/health", 503),
            probe("timeout"),
            Verdict.DEGRADED,
            "health_down_database_unreachable",
        ),
        (
            LIVE,
            http("/actuator/health", 503),
            None,
            Verdict.DEGRADED,
            "health_down_database_not_checked",
        ),
        (
            LIVE,
            http("/actuator/health", None, "read_timeout"),
            probe(),
            Verdict.DEGRADED,
            "readiness_unanswered",
        ),
        (
            LIVE,
            http("/actuator/health", 418),
            probe(),
            Verdict.INCONCLUSIVE,
            "unexpected_readiness_response",
        ),
    ],
)
def test_actuator_health_diagnoses(
    liveness: HttpResult,
    health: HttpResult | None,
    database: DatabaseProbe | None,
    verdict: Verdict,
    diagnosis: str,
) -> None:
    assessment = assess_actuator(ORDERFLOW, liveness, health, database)

    assert (assessment.verdict, assessment.diagnosis) == (verdict, diagnosis)
    text = " ".join([assessment.summary, *assessment.notes, *assessment.next_steps]).lower()
    assert "docker compose" not in text
    if diagnosis.startswith("health_down"):
        assert "doesn't say which component failed" in text
        assert "outage is likely" not in text
        assert "confirmed database outage" not in text or "not a confirmed" in text


def test_an_unreachable_database_is_not_called_an_outage() -> None:
    assessment = assess_actuator(
        ORDERFLOW, LIVE, http("/actuator/health", 503), probe("connection_refused")
    )

    assert "This is not a confirmed database outage." in assessment.summary
    assert "consistent with a database problem" in assessment.summary


def test_health_command_queries_the_actuator_paths(api: FakeApi) -> None:
    api.on("GET", "/actuator/health/liveness", respond(200, {"status": "UP"})).on(
        "GET", "/actuator/health", respond(503, DOWN)
    )

    result = invoke("health", **ORDERFLOW_ENV)

    output = flat(result.stdout)
    assert result.exit_code == 1
    assert [request.url.path for request in api.requests] == [
        "/actuator/health/liveness",
        "/actuator/health",
    ]
    assert "API health (aggregate)" in output
    assert "doesn't say which component failed" in output
    assert "PostgreSQL wasn't checked" in output


def test_a_missing_liveness_group_still_shows_the_aggregate(api: FakeApi) -> None:
    api.on("GET", "/actuator/health", respond(200, UP))

    result = invoke("health", "--json", **ORDERFLOW_ENV)

    report = json.loads(result.stdout)
    assert report["diagnosis"] == "liveness_endpoint_unavailable"
    assert report["readiness"]["status"] == 200
    assert result.exit_code == 1


def test_billing_health_keeps_its_readiness_label(api: FakeApi) -> None:
    api.on("GET", "/health", lab_health()).on(
        "GET",
        "/health/ready",
        respond(200, {"status": "ready", "checks": {"database": {"status": "up"}}}),
    )

    result = invoke("health")

    assert result.exit_code == 0
    assert "API readiness" in result.stdout
    assert "aggregate" not in result.stdout


def test_orderflow_access_logs_keep_their_duration() -> None:
    line = (
        '{"@timestamp":"2026-10-08T08:56:41.152Z","log":{"level":"INFO"},"message":"HTTP request",'
        '"requestId":"demo-1","eventName":"http.request","method":"GET",'
        '"path":"/actuator/health","status":200,"durationMs":5,"ecs":{"version":"8.11"}}'
    )

    event = parse_line(line, "of.jsonl", 1)

    assert not isinstance(event, str)
    assert (event.format, event.request_id, event.status, event.duration_ms) == (
        "ecs",
        "demo-1",
        200,
        5.0,
    )
    assert "durationMs" not in event.extra


def test_all_runs_only_generic_and_the_targets_pack() -> None:
    billing = plan_checks([], run_all=True, parameters={}, packs=BILLING.check_packs)
    orderflow = plan_checks([], run_all=True, parameters={}, packs=ORDERFLOW.check_packs)

    assert [item.check.name for item in billing] == [
        check.name for check in CATALOG if check.pack in ("generic", "billing")
    ]
    assert all(item.check.pack in ("generic", "orderflow") for item in orderflow)
    assert not any(item.check.name.startswith("billing.") for item in orderflow)


def test_a_check_from_another_pack_is_refused_by_name() -> None:
    with pytest.raises(ConfigError, match="can't run against the 'orderflow' target"):
        plan_checks(
            ["billing.invoice_lookup"],
            run_all=False,
            parameters={"id": "inv_1"},
            packs=ORDERFLOW.check_packs,
            target="orderflow",
        )


def test_db_run_refuses_foreign_checks_before_connecting(monkeypatch: pytest.MonkeyPatch) -> None:
    def never(*_args: object) -> None:
        raise AssertionError("no connection may be opened")

    monkeypatch.setattr(runner, "read_only_session", never)

    result = invoke(
        "db",
        "run",
        "billing.duplicate_payments",
        **ORDERFLOW_ENV,
        SUPPORTOPS_DB_URL="postgresql://ro:pw@127.0.0.1:5432/orderflow",
    )

    assert result.exit_code == 2
    assert "billing checks" in flat(result.stderr)


def test_db_run_without_a_database_url_is_a_usage_error() -> None:
    result = invoke("db", "run", "--all", **ORDERFLOW_ENV)

    assert result.exit_code == 2
    assert "SUPPORTOPS_DB_URL is not set" in flat(result.stderr)
    assert "orderflow.env" in flat(result.stderr)


def test_limited_session_visibility_is_reported_as_incomplete(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    unmonitored = SessionInfo(
        role="ro", database="orderflow", server_version="18.6", read_only=True, monitoring=False
    )

    @contextmanager
    def session(*_args: object) -> Iterator[FakeConnection]:
        yield FakeConnection({"db.connectivity": FakeCursor(["role"], [])})

    monkeypatch.setattr(runner, "read_only_session", session)
    monkeypatch.setattr(runner, "session_info", lambda _connection: unmonitored)

    result = invoke(
        "db",
        "run",
        "pg.connections",
        "pg.blocking_sessions",
        **ORDERFLOW_ENV,
        SUPPORTOPS_DB_URL="postgresql://ro:pw@127.0.0.1:5432/orderflow",
    )

    output = flat(result.stdout)
    assert result.exit_code == 0
    assert "PASS" not in output
    assert "activity results are incomplete, not clean" in output


def test_full_visibility_has_no_incomplete_warning(monkeypatch: pytest.MonkeyPatch) -> None:
    monitored = SessionInfo(
        role="ro", database="billing", server_version="18.6", read_only=True, monitoring=True
    )

    @contextmanager
    def session(*_args: object) -> Iterator[FakeConnection]:
        yield FakeConnection({})

    monkeypatch.setattr(runner, "read_only_session", session)
    monkeypatch.setattr(runner, "session_info", lambda _connection: monitored)

    result = invoke(
        "db",
        "run",
        "pg.blocking_sessions",
        SUPPORTOPS_DB_URL="postgresql://ro:pw@127.0.0.1:5433/billing",
    )

    assert result.exit_code == 0
    assert "incomplete" not in result.stdout
