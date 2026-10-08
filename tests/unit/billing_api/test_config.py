import pytest
from pydantic import SecretStr, ValidationError

from billing_api.config import BillingSettings, ConfigurationError, load_settings
from billing_api.faults import Fault
from billing_api.main import create_app
from tests.log_capture import JsonCapture

DATABASE_URL = "postgresql://billing_app:ConfigTestPw@postgres:5432/billing"


def test_faults_are_off_by_default() -> None:
    settings = BillingSettings(database_url=SecretStr(DATABASE_URL), env="lab")

    assert settings.faults == frozenset()


def test_faults_are_read_from_a_comma_separated_variable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("BILLING_DATABASE_URL", DATABASE_URL)
    monkeypatch.setenv("BILLING_ENV", "lab")
    monkeypatch.setenv("BILLING_FAULTS", " payment_partial_commit , ")

    assert load_settings().faults == {Fault.PAYMENT_PARTIAL_COMMIT}


def test_unknown_fault_names_are_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BILLING_DATABASE_URL", DATABASE_URL)
    monkeypatch.setenv("BILLING_ENV", "lab")
    monkeypatch.setenv("BILLING_FAULTS", "drop_all_tables")

    with pytest.raises(ConfigurationError, match="BILLING_FAULTS"):
        load_settings()


def test_faults_cannot_be_enabled_outside_the_lab() -> None:
    with pytest.raises(ValidationError, match="only be enabled when BILLING_ENV=lab"):
        BillingSettings(
            database_url=SecretStr(DATABASE_URL),
            env="production",
            faults=frozenset({Fault.PAYMENT_PARTIAL_COMMIT}),
        )


def test_the_api_refuses_to_start_with_faults_outside_the_lab(
    monkeypatch: pytest.MonkeyPatch, logs: JsonCapture
) -> None:
    monkeypatch.setenv("BILLING_DATABASE_URL", DATABASE_URL)
    monkeypatch.setenv("BILLING_FAULTS", "payment_partial_commit")
    monkeypatch.delenv("BILLING_ENV", raising=False)

    with pytest.raises(ConfigurationError, match="only be enabled when BILLING_ENV=lab"):
        create_app()

    [event] = logs.events("app.config_invalid")
    assert event["level"] == "CRITICAL"
    assert "ConfigTestPw" not in str(logs.entries)


def test_configuration_errors_never_contain_the_database_password(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("BILLING_DATABASE_URL", "mysql://root:ConfigTestPw@db/billing")

    with pytest.raises(ConfigurationError) as excinfo:
        load_settings()

    assert "BILLING_DATABASE_URL" in str(excinfo.value)
    assert "ConfigTestPw" not in str(excinfo.value)
    assert excinfo.value.__cause__ is None
