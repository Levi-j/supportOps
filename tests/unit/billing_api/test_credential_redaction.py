import json
import logging
import sys
import traceback
from collections.abc import Callable
from typing import NoReturn

import psycopg
import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr

from billing_api.config import BillingSettings, ConfigurationError, load_settings
from billing_api.database import credentials
from billing_api.logging_setup import JsonFormatter
from billing_api.main import create_app

ENCODED_PASSWORD = "Odd%25Pa%40ss%22w0rd%5C"
DECODED_PASSWORD = 'Odd%Pa@ss"w0rd\\'
DATABASE_URL = f"postgresql://billing_app:{ENCODED_PASSWORD}@postgres:5432/billing"
LAB_KEY = "bk_juniper01_lab_only_not_a_real_key"


def failing_connect(error: psycopg.Error) -> Callable[..., NoReturn]:
    def connect(*_args: object, **_kwargs: object) -> NoReturn:
        raise error

    return connect


@pytest.mark.parametrize("password", ["Fake%zzPw", "Fake%00Pw", "Fake%Pw"])
def test_unparseable_database_url_is_rejected_without_echoing_it(
    monkeypatch: pytest.MonkeyPatch, password: str
) -> None:
    monkeypatch.setenv(
        "BILLING_DATABASE_URL", f"postgresql://billing_app:{password}@postgres:5432/billing"
    )

    with pytest.raises(ConfigurationError) as excinfo:
        load_settings()

    output = "".join(traceback.format_exception(excinfo.value))
    assert "BILLING_DATABASE_URL" in output
    assert "percent-encode special characters" in output
    assert "Fake" not in output


def test_startup_with_an_unparseable_url_logs_no_password(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv(
        "BILLING_DATABASE_URL", "postgresql://billing_app:Fake%zzPw@postgres/billing"
    )

    with pytest.raises(ConfigurationError):
        create_app()

    logged = capsys.readouterr().out
    assert '"event_name": "app.config_invalid"' in logged
    assert "Fake" not in logged


def test_percent_encoded_passwords_are_still_accepted() -> None:
    settings = BillingSettings(database_url=SecretStr(DATABASE_URL), env="lab")

    assert credentials(settings) == {ENCODED_PASSWORD, DECODED_PASSWORD}


def test_registered_secrets_are_hidden_in_every_part_of_a_log_entry() -> None:
    try:
        raise RuntimeError(f"connect failed for {DECODED_PASSWORD}")
    except RuntimeError:
        record = logging.LogRecord(
            "billing_api.test",
            logging.ERROR,
            __file__,
            1,
            f"message with {ENCODED_PASSWORD}",
            None,
            sys.exc_info(),
        )
    record.detail = f"detail with {DECODED_PASSWORD}"
    record.values = [DECODED_PASSWORD]

    output = JsonFormatter([ENCODED_PASSWORD, DECODED_PASSWORD]).format(record)

    entry = json.loads(output)
    assert entry["message"] == "message with ***"
    assert entry["detail"] == "detail with ***"
    assert entry["values"] == ["***"]
    assert entry["error_message"] == "connect failed for ***"
    assert "Odd" not in output


@pytest.mark.parametrize(
    ("message", "category"),
    [
        (
            f'FATAL:  password authentication failed for user "billing_app" ({DECODED_PASSWORD})',
            "authentication_failed",
        ),
        (f"failed to resolve host 'postgres': {DECODED_PASSWORD}", "dns_failure"),
        (f"connection failed: Connection refused ({ENCODED_PASSWORD})", "connection_refused"),
    ],
)
def test_readiness_keeps_its_classification_but_hides_the_password(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    message: str,
    category: str,
) -> None:
    settings = BillingSettings(database_url=SecretStr(DATABASE_URL), env="lab")
    monkeypatch.setattr(psycopg, "connect", failing_connect(psycopg.OperationalError(message)))

    with TestClient(create_app(settings)) as client:
        response = client.get("/health/ready")

    logged = capsys.readouterr().out
    entries = [json.loads(line) for line in logged.splitlines()]
    unavailable = [entry for entry in entries if entry.get("event_name") == "db.unavailable"]
    assert response.status_code == 503
    assert response.json()["checks"]["database"]["error"] == category
    assert [entry["error"] for entry in unavailable] == [category]
    assert "***" in unavailable[0]["detail"]
    assert "Odd" not in logged


def test_unexpected_database_errors_never_log_the_password(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    settings = BillingSettings(database_url=SecretStr(DATABASE_URL), env="lab")
    echoed = psycopg.ProgrammingError(f'invalid percent-encoded token: "{ENCODED_PASSWORD}"')
    monkeypatch.setattr(psycopg, "connect", failing_connect(echoed))

    with TestClient(create_app(settings), raise_server_exceptions=False) as client:
        response = client.get("/v1/account", headers={"Authorization": f"Bearer {LAB_KEY}"})

    logged = capsys.readouterr().out
    assert response.status_code == 500
    assert '"event_name": "unhandled_exception"' in logged
    assert "Odd" not in logged
    assert "Odd" not in response.text
