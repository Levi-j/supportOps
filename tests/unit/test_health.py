from collections.abc import Iterator
from typing import Any

import httpx
import pytest

from supportops import health as health_module
from supportops.db.connection import DatabaseProbe
from supportops.health import ApiDatabaseView, Verdict, assess, run_health_check
from supportops.http_checks import LOCALHOST_NOTE, HttpResult, TransportFailure, create_client
from supportops.settings import Settings
from supportops.targets import BILLING
from tests.unit.fake_api import FakeApi, respond


def http(status: int | None = 200, failure: str | None = None) -> HttpResult:
    return HttpResult(
        method="GET",
        url="http://127.0.0.1:8001/health",
        request_id="supportops-test",
        duration_ms=5.0,
        status=status,
        failure=TransportFailure(category=failure, summary="It failed.", detail="x", hint="Fix it.")
        if failure
        else None,
    )


def database(error: str | None = None) -> DatabaseProbe:
    answered = error in (None, "authentication_failed", "database_missing")
    return DatabaseProbe(
        target="supportops_ro@127.0.0.1:5433/billing",
        reachable=error is None,
        server_answered=answered,
        latency_ms=4,
        error=error,
    )


DOWN_DB = ApiDatabaseView(status="down", error="dns_failure")


@pytest.mark.parametrize(
    ("liveness", "readiness", "api_database", "db", "verdict", "diagnosis"),
    [
        (
            http(200),
            http(200),
            ApiDatabaseView(status="up"),
            database(),
            Verdict.HEALTHY,
            "api_ready",
        ),
        (http(200), http(503), DOWN_DB, database(), Verdict.DEGRADED, "api_cannot_reach_database"),
        (
            http(200),
            http(503),
            DOWN_DB,
            database("authentication_failed"),
            Verdict.DEGRADED,
            "api_cannot_reach_database",
        ),
        (http(200), http(503), DOWN_DB, database("timeout"), Verdict.DEGRADED, "database_outage"),
        (
            http(200),
            http(503),
            DOWN_DB,
            database("dns_failure"),
            Verdict.DEGRADED,
            "database_outage",
        ),
        (http(200), http(503), DOWN_DB, None, Verdict.DEGRADED, "readiness_failing_cause_unknown"),
        (
            http(200),
            http(503),
            DOWN_DB,
            database("error"),
            Verdict.DEGRADED,
            "readiness_failing_cause_unknown",
        ),
        (
            http(200),
            http(None, failure="read_timeout"),
            None,
            database(),
            Verdict.DEGRADED,
            "readiness_unanswered",
        ),
        (
            http(None, failure="connection_refused"),
            None,
            None,
            database(),
            Verdict.DOWN,
            "api_unreachable",
        ),
        (http(None, failure="dns_failure"), None, None, None, Verdict.DOWN, "api_unreachable"),
        (http(404), None, None, database(), Verdict.INCONCLUSIVE, "unexpected_liveness_response"),
        (
            http(200),
            http(500),
            None,
            database(),
            Verdict.INCONCLUSIVE,
            "unexpected_readiness_response",
        ),
        (
            http(200),
            http(503),
            ApiDatabaseView(status="up"),
            database(),
            Verdict.INCONCLUSIVE,
            "contradictory_readiness",
        ),
    ],
)
def test_verdict_table(
    liveness: HttpResult,
    readiness: HttpResult | None,
    api_database: ApiDatabaseView | None,
    db: DatabaseProbe | None,
    verdict: Verdict,
    diagnosis: str,
) -> None:
    assessment = assess(BILLING, liveness, readiness, api_database, db)

    assert assessment.verdict == verdict
    assert assessment.diagnosis == diagnosis
    assert assessment.summary


def test_database_outage_names_both_failing_views() -> None:
    assessment = assess(BILLING, http(200), http(503), DOWN_DB, database("timeout"))

    assert "the API reports: dns_failure" in assessment.summary
    assert "(timeout)" in assessment.summary
    assert "likely" in assessment.summary


def test_down_api_with_a_reachable_database_narrows_the_problem() -> None:
    assessment = assess(BILLING, http(None, failure="connection_refused"), None, None, database())

    assert any("limited to the API process" in note for note in assessment.notes)


def test_down_api_with_an_unreachable_database_points_wider() -> None:
    assessment = assess(
        BILLING, http(None, failure="connection_refused"), None, None, database("timeout")
    )

    assert any("isn't reachable from this machine either" in note for note in assessment.notes)


def test_healthy_api_with_broken_support_access_adds_a_caveat() -> None:
    assessment = assess(
        BILLING, http(200), http(200), ApiDatabaseView(status="up"), database("timeout")
    )

    assert assessment.verdict == Verdict.HEALTHY
    assert any("although the API can" in note for note in assessment.notes)


def test_skipped_database_check_is_explained() -> None:
    assessment = assess(BILLING, http(200), http(200), ApiDatabaseView(status="up"), None)

    assert assessment.notes == ["Database check skipped: SUPPORTOPS_DB_URL is not set."]


def run(
    api: FakeApi,
    monkeypatch: pytest.MonkeyPatch,
    probe: DatabaseProbe | None = None,
    clock: Iterator[float] | None = None,
    **settings: Any,
) -> Any:
    probes: list[str] = []

    def fake_probe(dsn: object, timeout: float) -> DatabaseProbe:
        probes.append("called")
        return probe or database()

    monkeypatch.setattr(health_module, "probe_database", fake_probe)
    config = Settings(**settings)
    client = create_client(config, transport=api.transport)
    if clock is None:
        return run_health_check(config, BILLING, client), probes
    return run_health_check(config, BILLING, client, clock=lambda: next(clock)), probes


def healthy_api() -> FakeApi:
    ready = {"status": "ready", "checks": {"database": {"status": "up", "latency_ms": 2}}}
    return (
        FakeApi()
        .on("GET", "/health", respond(200, {"status": "ok", "environment": "lab"}))
        .on("GET", "/health/ready", respond(200, ready))
    )


def test_slow_localhost_connections_are_explained(monkeypatch: pytest.MonkeyPatch) -> None:
    slow_probe = database().model_copy(update={"host": "localhost", "latency_ms": 3047})

    report, _ = run(
        healthy_api(),
        monkeypatch,
        probe=slow_probe,
        clock=iter([0.0, 2.1, 3.0, 3.01]),
        api_url="http://localhost:8001",
        db_url="postgresql://u:p@localhost:5433/billing",
    )

    assert report.verdict == Verdict.HEALTHY
    assert report.notes == [
        "SUPPORTOPS_API_URL and SUPPORTOPS_DB_URL use 'localhost', and connecting took over a "
        "second. " + LOCALHOST_NOTE
    ]


@pytest.mark.parametrize(
    ("api_url", "host", "latency_ms"),
    [
        ("http://127.0.0.1:8001", "127.0.0.1", 3047),
        ("http://localhost:8001", "localhost", 25),
    ],
)
def test_no_localhost_note_for_fast_or_numeric_addresses(
    monkeypatch: pytest.MonkeyPatch, api_url: str, host: str, latency_ms: int
) -> None:
    probe = database().model_copy(update={"host": host, "latency_ms": latency_ms})

    report, _ = run(
        healthy_api(),
        monkeypatch,
        probe=probe,
        clock=iter([0.0, 0.005, 1.0, 1.01]),
        api_url=api_url,
        db_url=f"postgresql://u:p@{host}:5433/billing",
    )

    assert report.notes == []


def test_health_check_only_sends_get_requests(monkeypatch: pytest.MonkeyPatch) -> None:
    api = (
        FakeApi()
        .on("GET", "/health", respond(200, {"status": "ok", "environment": "lab"}))
        .on(
            "GET",
            "/health/ready",
            respond(
                503,
                {
                    "status": "not_ready",
                    "checks": {
                        "database": {
                            "status": "down",
                            "error": "connection_refused",
                            "latency_ms": 2,
                        }
                    },
                },
            ),
        )
    )

    report, probes = run(api, monkeypatch, db_url="postgresql://u:p@127.0.0.1:5433/billing")

    assert api.methods == ["GET", "GET"]
    assert report.api_database == ApiDatabaseView(status="down", error="connection_refused")
    assert report.diagnosis == "api_cannot_reach_database"
    assert probes == ["called"]


def test_readiness_is_skipped_when_liveness_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    api = FakeApi().on("GET", "/health", httpx.ConnectTimeout("timed out"))

    report, probes = run(api, monkeypatch)

    assert [request.url.path for request in api.requests] == ["/health"]
    assert report.readiness is None
    assert report.verdict == Verdict.DOWN
    assert probes == []
