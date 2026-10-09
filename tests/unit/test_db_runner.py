from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

import psycopg
import pytest
from pydantic import SecretStr

from supportops.db import connection as db_connection
from supportops.db import runner
from supportops.db.catalog import CHECKS
from supportops.db.connection import (
    DatabaseError,
    SessionInfo,
    classify_query_error,
    connection_error,
    read_only_session,
)
from supportops.db.runner import (
    MAX_ROWS,
    PlannedCheck,
    parse_param_options,
    plan_checks,
    privilege_warnings,
    run_checks,
)
from supportops.errors import ConfigError, ExitCode

PASSWORD = "RunnerTestPw%41"
DSN = SecretStr(f"postgresql://supportops_ro:{PASSWORD}@127.0.0.1:5433/billing")
MONITORING = SessionInfo(
    role="supportops_ro",
    database="billing",
    server_version="18.6",
    read_only=True,
    monitoring=True,
)


@dataclass
class Column:
    name: str


@dataclass
class FakeCursor:
    columns: list[str]
    rows: list[tuple[Any, ...]]

    @property
    def description(self) -> list[Column]:
        return [Column(name) for name in self.columns]

    def fetchmany(self, size: int) -> list[tuple[Any, ...]]:
        return self.rows[:size]


@dataclass
class FakeConnection:
    answers: dict[str, FakeCursor | psycopg.Error] = field(default_factory=dict)
    executed: list[tuple[str, Any]] = field(default_factory=list)
    broken: bool = False
    transactions: int = 0

    @contextmanager
    def transaction(self) -> Iterator[None]:
        self.transactions += 1
        yield

    def execute(self, sql: str, params: Any = None) -> FakeCursor:
        self.executed.append((sql, params))
        for name, check in CHECKS.items():
            if check.sql == sql:
                answer = self.answers.get(name, FakeCursor(["value"], []))
                if isinstance(answer, psycopg.Error):
                    raise answer
                return answer
        raise AssertionError("not a catalog query")


def install(
    monkeypatch: pytest.MonkeyPatch, fake: FakeConnection, session: SessionInfo = MONITORING
) -> None:
    @contextmanager
    def session_factory(*_args: object) -> Iterator[FakeConnection]:
        yield fake

    monkeypatch.setattr(runner, "read_only_session", session_factory)
    monkeypatch.setattr(runner, "session_info", lambda _connection: session)


def run(names: list[str], params: dict[str, str] | None = None, **kwargs: Any) -> Any:
    plan = plan_checks(names, run_all=False, parameters=params or {})
    return run_checks(DSN, plan, connect_timeout_seconds=1, **kwargs)


def connectivity_row(**overrides: Any) -> tuple[Any, ...]:
    values: dict[str, Any] = {
        "role": "supportops_ro",
        "database": "billing",
        "server_version": "18.6",
        "transaction_read_only": "on",
        "statement_timeout": "5s",
        "superuser": False,
        "can_create_roles": False,
        "can_create_databases": False,
        "bypasses_row_security": False,
        "replication": False,
        "can_monitor": True,
        "writable_tables": [],
    }
    values.update(overrides)
    return tuple(values.values())


CONNECTIVITY_COLUMNS = [
    "role",
    "database",
    "server_version",
    "transaction_read_only",
    "statement_timeout",
    "superuser",
    "can_create_roles",
    "can_create_databases",
    "bypasses_row_security",
    "replication",
    "can_monitor",
    "writable_tables",
]


def test_param_options_are_parsed() -> None:
    assert parse_param_options(["prefix=bk_juniper01", "min_seconds=5", "prefix=bk_juniper01"]) == {
        "prefix": "bk_juniper01",
        "min_seconds": "5",
    }


@pytest.mark.parametrize(
    ("items", "message"),
    [
        (["prefix"], "name=value"),
        (["=x"], "name=value"),
        (["prefix=a", "prefix=b"], "twice with different values"),
    ],
)
def test_bad_param_options(items: list[str], message: str) -> None:
    with pytest.raises(ConfigError, match=message):
        parse_param_options(items)


@pytest.mark.parametrize(
    ("names", "run_all", "params", "message"),
    [
        ([], False, {}, "Name at least one check"),
        (["pg.connections"], True, {}, "not both"),
        (["pg.nothing"], False, {}, "Unknown check"),
        (["pg.connections"], False, {"prefix": "bk_juniper01"}, "Unknown parameter 'prefix'"),
        (["billing.api_key_status"], False, {}, "needs a parameter"),
        (["billing.invoice_lookup"], False, {}, "needs a parameter"),
        (
            ["billing.invoice_lookup"],
            False,
            {"id": "inv_juniper_1003", "number": "INV-1003"},
            "not both",
        ),
        (["pg.long_transactions"], False, {"min_seconds": "soon"}, "whole number"),
    ],
)
def test_invalid_plans_are_usage_errors(
    names: list[str], run_all: bool, params: dict[str, str], message: str
) -> None:
    with pytest.raises(ConfigError, match=message) as excinfo:
        plan_checks(names, run_all=run_all, parameters=params)

    assert excinfo.value.exit_code == ExitCode.USAGE


def test_all_skips_checks_without_their_parameters() -> None:
    plan = plan_checks([], run_all=True, parameters={})

    skipped = {item.check.name: item.skip_reason for item in plan if item.skip_reason}
    assert set(skipped) == {"billing.api_key_status", "billing.invoice_lookup"}
    assert len(plan) == len(CHECKS)


def test_all_uses_parameters_that_are_given() -> None:
    plan = plan_checks([], run_all=True, parameters={"prefix": "bk_juniper01", "min_seconds": "5"})

    by_name = {item.check.name: item for item in plan}
    assert by_name["billing.api_key_status"].skip_reason is None
    assert by_name["billing.api_key_status"].parameters == {"prefix": "bk_juniper01"}
    assert by_name["pg.long_transactions"].parameters == {"min_seconds": 5}
    assert by_name["billing.invoice_lookup"].skip_reason is not None


def test_duplicate_names_run_once() -> None:
    plan = plan_checks(["pg.connections", "pg.connections"], run_all=False, parameters={})

    assert [item.check.name for item in plan] == ["pg.connections"]


def test_consistency_checks_pass_on_empty_results(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakeConnection()
    install(monkeypatch, fake)

    report = run(["billing.invoice_total_mismatch", "billing.duplicate_payments"])

    assert [result.status for result in report.results] == ["pass", "pass"]
    assert report.results[0].summary == "Every invoice total matches the sum of its lines."
    assert report.exit_code == ExitCode.OK
    assert fake.transactions == 2


def test_detected_problems_fail_with_their_rows(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakeConnection(
        answers={
            "billing.invoice_total_mismatch": FakeCursor(
                ["invoice_id", "stored_total_cents", "line_total_cents"],
                [("inv_juniper_1001", Decimal(9500), Decimal("9400"))],
            )
        }
    )
    install(monkeypatch, fake)

    report = run(["billing.invoice_total_mismatch"])

    result = report.results[0]
    assert result.status == "fail"
    assert result.summary == "Invoices whose total differs from the sum of their lines: 1."
    assert result.rows == [
        {"invoice_id": "inv_juniper_1001", "stored_total_cents": 9500, "line_total_cents": 9400}
    ]
    assert report.exit_code == ExitCode.PROBLEM


def test_parameters_are_bound_not_inserted(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakeConnection()
    install(monkeypatch, fake)

    run(["billing.api_key_status"], {"prefix": "bk_juniper00"})

    sql, params = fake.executed[0]
    assert sql == CHECKS["billing.api_key_status"].sql
    assert "%(prefix)s" in sql
    assert "bk_juniper00" not in sql
    assert params == {"prefix": "bk_juniper00"}


def test_checks_without_parameters_send_none(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakeConnection()
    install(monkeypatch, fake)

    run(["pg.connections"])

    assert fake.executed[0][1] is None


def test_lookups_report_found_and_missing_records(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakeConnection(
        answers={"billing.api_key_status": FakeCursor(["key_prefix"], [("bk_juniper00",)])}
    )
    install(monkeypatch, fake)

    found = run(["billing.api_key_status"], {"prefix": "bk_juniper00"}).results[0]
    missing = run(["billing.invoice_lookup"], {"number": "INV-9999"}).results[0]

    assert (found.status, found.summary) == ("info", "Found the API key with prefix bk_juniper00.")
    assert (missing.status, missing.summary) == ("fail", "No invoice matches number INV-9999.")
    assert missing.parameters == {"id": None, "number": "INV-9999"}


def test_activity_without_monitoring_is_not_reported_as_healthy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    install(monkeypatch, FakeConnection(), MONITORING.model_copy(update={"monitoring": False}))

    result = run(["pg.blocking_sessions"]).results[0]

    assert result.status == "info"
    assert result.complete is False
    assert "pg_monitor" in result.notes[0]


def test_activity_findings_fail(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakeConnection(
        answers={
            "pg.long_transactions": FakeCursor(["pid", "state"], [(42, "idle in transaction")])
        }
    )
    install(monkeypatch, fake)

    result = run(["pg.long_transactions"], {"min_seconds": "30"}).results[0]

    assert result.status == "fail"
    assert result.summary == "Transactions open longer than 30 seconds: 1."


def test_inventory_is_informational(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakeConnection(
        answers={
            "pg.connections": FakeCursor(
                ["role", "sessions"], [("billing_app", 3), ("supportops_ro", 1)]
            )
        }
    )
    install(monkeypatch, fake)

    result = run(["pg.connections"]).results[0]

    assert (result.status, result.summary) == ("info", "Other sessions on this database: 4.")


@pytest.mark.parametrize(
    ("error", "category"),
    [
        (
            psycopg.errors.QueryCanceled("canceling statement due to statement timeout"),
            "statement_timeout",
        ),
        (
            psycopg.errors.InsufficientPrivilege("permission denied for table invoices"),
            "permission_denied",
        ),
        (
            psycopg.errors.UndefinedTable('relation "billing.invoices" does not exist'),
            "missing_object",
        ),
        (psycopg.errors.InternalError_("something odd"), "error"),
    ],
)
def test_query_errors_become_error_results(
    monkeypatch: pytest.MonkeyPatch, error: psycopg.Error, category: str
) -> None:
    fake = FakeConnection(answers={"billing.duplicate_payments": error})
    install(monkeypatch, fake)

    report = run(["billing.duplicate_payments", "billing.invoice_total_mismatch"])

    assert [result.status for result in report.results] == ["error", "pass"]
    assert report.results[0].error == category
    assert report.exit_code == ExitCode.INCOMPLETE


def test_lost_connection_stops_the_run(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakeConnection(
        answers={"pg.connections": psycopg.OperationalError("server closed the connection")},
        broken=True,
    )
    install(monkeypatch, fake)

    with pytest.raises(DatabaseError, match="connection was lost"):
        run(["pg.connections"])


def test_duration_uses_the_clock(monkeypatch: pytest.MonkeyPatch) -> None:
    install(monkeypatch, FakeConnection())
    ticks = iter([1.0, 1.0125])

    result = run(["pg.connections"], clock=lambda: next(ticks)).results[0]

    assert result.duration_ms == 12.5


def test_rows_are_capped(monkeypatch: pytest.MonkeyPatch) -> None:
    rows = [(f"inv_{number}",) for number in range(MAX_ROWS + 5)]
    install(
        monkeypatch,
        FakeConnection(answers={"billing.duplicate_payments": FakeCursor(["invoice_id"], rows)}),
    )

    result = run(["billing.duplicate_payments"]).results[0]

    assert result.row_count == MAX_ROWS
    assert result.truncated
    assert f"first {MAX_ROWS} rows" in result.notes[-1]


def test_skipped_and_show_sql(monkeypatch: pytest.MonkeyPatch) -> None:
    install(monkeypatch, FakeConnection())
    plan = [
        PlannedCheck(CHECKS["billing.api_key_status"], {"prefix": None}, "Needs --param prefix."),
        PlannedCheck(CHECKS["pg.connections"]),
    ]

    report = run_checks(DSN, plan, connect_timeout_seconds=1, show_sql=True)

    assert report.results[0].status == "skipped"
    assert report.results[0].summary == "Skipped. Needs --param prefix."
    assert report.results[1].sql is not None
    assert report.results[1].sql.startswith("SELECT")
    assert report.counts == {"info": 1, "skipped": 1}
    assert report.exit_code == ExitCode.OK


def test_connectivity_passes_for_a_least_privilege_role(monkeypatch: pytest.MonkeyPatch) -> None:
    install(
        monkeypatch,
        FakeConnection(
            answers={"db.connectivity": FakeCursor(CONNECTIVITY_COLUMNS, [connectivity_row()])}
        ),
    )

    result = run(["db.connectivity"]).results[0]

    assert result.status == "pass"
    assert result.summary == (
        "Connected to billing as supportops_ro (PostgreSQL 18.6). The session is read-only."
    )
    assert result.notes == []


def test_connectivity_warns_about_a_superuser(monkeypatch: pytest.MonkeyPatch) -> None:
    row = connectivity_row(
        role="lab_admin", superuser=True, writable_tables=["billing.invoices", "billing.payments"]
    )
    install(
        monkeypatch,
        FakeConnection(answers={"db.connectivity": FakeCursor(CONNECTIVITY_COLUMNS, [row])}),
    )

    report = run(["db.connectivity"])

    result = report.results[0]
    assert result.status == "warn"
    assert "superuser" in result.notes[0]
    assert "Tables this role could change outside SupportOps: 2" in result.notes[1]
    assert report.exit_code == ExitCode.PROBLEM


def test_privilege_warnings_cover_every_risk() -> None:
    columns = CONNECTIVITY_COLUMNS
    row = dict(
        zip(
            columns,
            connectivity_row(
                transaction_read_only="off",
                can_create_roles=True,
                replication=True,
                writable_tables=[f"billing.t{number}" for number in range(7)],
            ),
            strict=True,
        )
    )

    warnings = privilege_warnings(row)

    assert "not read-only" in warnings[0]
    assert "create roles, start replication" in warnings[1]
    assert "Tables this role could change outside SupportOps: 7" in warnings[2]
    assert warnings[2].count("billing.t") == 5


def test_query_error_classification_has_actionable_text() -> None:
    category, explanation = classify_query_error(psycopg.errors.QueryCanceled("x"))

    assert category == "statement_timeout"
    assert "pg.blocking_sessions" in explanation


@pytest.mark.parametrize(
    ("message", "expected"),
    [
        (
            f'password authentication failed for user "supportops_ro" {PASSWORD}',
            "rejected the login",
        ),
        ('database "billingx" does not exist', "doesn't exist"),
        ("connection refused", "didn't accept the connection"),
        ("connection timeout expired", "didn't accept the connection"),
        ("failed to resolve host 'db': nope", "couldn't be resolved"),
        (f"weird failure mentioning {PASSWORD}", "Couldn't connect"),
    ],
)
def test_connection_errors_never_reveal_the_password(message: str, expected: str) -> None:
    error = connection_error(psycopg.OperationalError(message), DSN.get_secret_value())

    assert expected in error.message
    assert "RunnerTestPw" not in error.message
    assert "RunnerTestPw" not in (error.hint or "")
    assert error.exit_code == ExitCode.INCOMPLETE


def test_sessions_are_closed_even_after_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    closed: list[bool] = []

    class Closable:
        autocommit = False

        def close(self) -> None:
            closed.append(True)

    monkeypatch.setattr(db_connection, "connect_read_only", lambda *_args: Closable())

    with pytest.raises(RuntimeError), read_only_session(DSN, 1):
        raise RuntimeError("boom")

    assert closed == [True]


def test_failed_connections_raise_database_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    def refuse(*_args: object) -> None:
        raise psycopg.OperationalError("connection refused")

    monkeypatch.setattr(db_connection, "connect_read_only", refuse)

    with (
        pytest.raises(DatabaseError, match="didn't accept the connection"),
        read_only_session(DSN, 1),
    ):
        pass
