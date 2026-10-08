from types import TracebackType
from typing import NoReturn

import psycopg
import pytest
from pydantic import SecretStr

from supportops.db.connection import classify_error, describe_dsn, probe_database

PASSWORD = "ProbeTestPw"
DSN = f"postgresql://supportops_ro:{PASSWORD}@127.0.0.1:5433/billing"


class FakeCursor:
    def __init__(self, row: tuple[str, str, str]) -> None:
        self.row = row

    def fetchone(self) -> tuple[str, str, str]:
        return self.row


class FakeConnection:
    def __init__(self, row: tuple[str, str, str]) -> None:
        self.row = row
        self.read_only = False
        self.statements: list[str] = []

    def __enter__(self) -> "FakeConnection":
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        return None

    def execute(self, statement: str) -> FakeCursor:
        self.statements.append(statement)
        return FakeCursor(self.row)


def test_describe_dsn_never_includes_the_password() -> None:
    assert describe_dsn(DSN) == "supportops_ro@127.0.0.1:5433/billing"


@pytest.mark.parametrize(
    ("message", "expected"),
    [
        (
            'FATAL:  password authentication failed for user "supportops_ro"',
            "authentication_failed",
        ),
        ('FATAL:  database "billing_prod" does not exist', "database_missing"),
        ("connection to server at 127.0.0.1 failed: Connection refused", "connection_refused"),
        ("failed to resolve host 'db': [Errno 11001] getaddrinfo failed", "dns_failure"),
        ("connection timeout expired", "timeout"),
        ("something new", "error"),
    ],
)
def test_classifies_connection_errors(message: str, expected: str) -> None:
    assert classify_error(psycopg.OperationalError(message)) == expected


def test_successful_probe_reports_a_read_only_session(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakeConnection(("supportops_ro", "18.6", "on"))
    calls: list[dict[str, object]] = []

    def connect(dsn: str, **kwargs: object) -> FakeConnection:
        calls.append(kwargs)
        return fake

    monkeypatch.setattr(psycopg, "connect", connect)

    probe = probe_database(SecretStr(DSN), 3)

    assert probe.reachable
    assert probe.server_answered
    assert probe.host == "127.0.0.1"
    assert probe.user == "supportops_ro"
    assert probe.server_version == "18.6"
    assert probe.read_only is True
    assert fake.read_only is True
    assert "default_transaction_read_only=on" in str(calls[0]["options"])
    assert "statement_timeout=" in str(calls[0]["options"])
    assert calls[0]["application_name"] == "supportops"
    assert fake.statements[0].lstrip().upper().startswith("SELECT")


@pytest.mark.parametrize(
    ("message", "error", "answered"),
    [
        ('FATAL:  password authentication failed for user "x"', "authentication_failed", True),
        ('FATAL:  database "nope" does not exist', "database_missing", True),
        ("connection timeout expired", "timeout", False),
        (
            "failed to resolve host 'postgres': [Errno -2] Name or service not known",
            "dns_failure",
            False,
        ),
    ],
)
def test_failed_probe_says_whether_the_server_answered(
    monkeypatch: pytest.MonkeyPatch, message: str, error: str, answered: bool
) -> None:
    def fail(*_args: object, **_kwargs: object) -> NoReturn:
        raise psycopg.OperationalError(message)

    monkeypatch.setattr(psycopg, "connect", fail)

    probe = probe_database(SecretStr(DSN), 3)

    assert not probe.reachable
    assert probe.error == error
    assert probe.server_answered is answered
    assert PASSWORD not in probe.model_dump_json()


@pytest.mark.parametrize(
    ("password", "echoed"),
    [("Pr0be%zzSecret", "Pr0be%zzSecret"), ("Pr0be%40Secret", "Pr0be@Secret")],
)
def test_a_password_echoed_by_the_driver_is_hidden(
    monkeypatch: pytest.MonkeyPatch, password: str, echoed: str
) -> None:
    def fail(*_args: object, **_kwargs: object) -> NoReturn:
        raise psycopg.ProgrammingError(f'invalid percent-encoded token: "{echoed}"')

    monkeypatch.setattr(psycopg, "connect", fail)

    probe = probe_database(
        SecretStr(f"postgresql://supportops_ro:{password}@127.0.0.1:5433/billing"), 3
    )

    assert probe.error == "error"
    assert probe.detail == 'invalid percent-encoded token: "***"'
    assert "Pr0be" not in probe.model_dump_json()
