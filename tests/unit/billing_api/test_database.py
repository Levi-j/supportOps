from typing import NoReturn

import psycopg
import pytest
from pydantic import SecretStr, ValidationError

from billing_api import database
from billing_api.config import BillingSettings
from tests.log_capture import JsonCapture
from tests.unit.billing_api.support import FAKE_DB_PASSWORD


@pytest.mark.parametrize(
    ("message", "expected"),
    [
        (
            'connection to server at "127.0.0.1", port 5432 failed: Connection refused',
            "connection_refused",
        ),
        ('FATAL:  password authentication failed for user "billing_app"', "authentication_failed"),
        ('FATAL:  role "billing_ap" does not exist', "authentication_failed"),
        ('FATAL:  database "billing_prod" does not exist', "database_missing"),
        ('could not translate host name "postgress" to address', "dns_failure"),
        ("connection timeout expired", "timeout"),
        ("something unexpected", "error"),
    ],
)
def test_classifies_connection_errors(message: str, expected: str) -> None:
    assert database.classify_error(psycopg.OperationalError(message)) == expected


@pytest.mark.parametrize(
    "resolver_error",
    [
        "[Errno -2] Name or service not known",
        "[Errno -5] No address associated with hostname",
        "[Errno -3] Temporary failure in name resolution",
        "[Errno 11001] getaddrinfo failed",
    ],
)
def test_any_host_resolution_failure_is_a_dns_failure(resolver_error: str) -> None:
    error = psycopg.OperationalError(f"failed to resolve host 'postgres': {resolver_error}")

    assert database.classify_error(error) == "dns_failure"


def test_failed_check_is_reported_and_logged(
    settings: BillingSettings, logs: JsonCapture, monkeypatch: pytest.MonkeyPatch
) -> None:
    def refuse(*_args: object, **_kwargs: object) -> NoReturn:
        raise psycopg.OperationalError(
            'connection to server at "127.0.0.1", port 5432 failed: Connection refused'
        )

    monkeypatch.setattr(psycopg, "connect", refuse)

    status = database.check_database(settings)

    assert not status.up
    assert status.error == "connection_refused"
    [event] = logs.events("db.unavailable")
    assert event["level"] == "WARNING"
    assert event["error"] == "connection_refused"
    assert FAKE_DB_PASSWORD not in str(logs.entries)


def test_connection_options_carry_timeouts_and_application_name(
    settings: BillingSettings,
) -> None:
    options = database.connection_kwargs(settings)

    assert options["application_name"] == "billing-api"
    assert options["connect_timeout"] == 3
    assert "statement_timeout=10000" in options["options"]
    assert "lock_timeout=3000" in options["options"]


def test_describe_target_never_includes_the_password(settings: BillingSettings) -> None:
    target = database.describe_target(settings)

    assert target == {
        "database_host": "postgres",
        "database_port": "5432",
        "database_name": "billing",
        "database_user": "billing_app",
    }


def test_settings_default_to_production(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BILLING_DATABASE_URL", "postgresql://app:pw@db:5432/billing")

    assert BillingSettings().env == "production"


def test_settings_require_a_postgres_url() -> None:
    with pytest.raises(ValidationError):
        BillingSettings(database_url=SecretStr("mysql://app:pw@db/billing"))
