import json
import socket
import threading
import time
import uuid
from collections.abc import Callable, Iterator
from typing import Any, LiteralString

import psycopg
import pytest
from psycopg import sql
from pydantic import SecretStr
from typer.testing import CliRunner

from supportops.auth_checks import lookup_key_record
from supportops.cli.main import app
from supportops.db.connection import DatabaseError, read_only_session
from supportops.db.runner import DbReport, plan_checks, run_checks
from supportops.errors import ExitCode
from supportops.settings import Settings
from tests.integration.support import LabDatabase, execute, fetch_all, open_transaction

pytestmark = pytest.mark.integration

POLL_SECONDS = 15


def run(
    database: LabDatabase,
    names: list[str],
    params: dict[str, str] | None = None,
    *,
    role: str = "supportops_ro",
    password: str | None = None,
    **kwargs: Any,
) -> DbReport:
    plan = plan_checks(names, run_all=False, parameters=params or {})
    dsn = SecretStr(database.url(role, password=password))
    return run_checks(dsn, plan, connect_timeout_seconds=5, **kwargs)


def result(report: DbReport, name: str) -> Any:
    return next(item for item in report.results if item.name == name)


def wait_for(condition: Callable[[], bool], what: str) -> None:
    deadline = time.monotonic() + POLL_SECONDS
    while not condition():
        if time.monotonic() > deadline:
            raise AssertionError(f"timed out waiting for {what}")
        time.sleep(0.1)


def test_every_check_passes_on_the_clean_seed(billing_db: LabDatabase) -> None:
    plan = plan_checks(
        [], run_all=True, parameters={"prefix": "bk_juniper01", "id": "inv_juniper_1003"}
    )

    report = run_checks(SecretStr(billing_db.url("supportops_ro")), plan, connect_timeout_seconds=5)

    statuses = {item.name: item.status for item in report.results}
    assert statuses == {
        "db.connectivity": "pass",
        "pg.connections": "info",
        "pg.long_transactions": "pass",
        "pg.blocking_sessions": "pass",
        "billing.invoice_total_mismatch": "pass",
        "billing.paid_invoice_without_payment": "pass",
        "billing.payment_on_unpaid_invoice": "pass",
        "billing.duplicate_payments": "pass",
        "billing.api_key_status": "info",
        "billing.invoice_lookup": "info",
    }
    assert report.exit_code == ExitCode.OK
    assert report.session.read_only
    assert report.session.monitoring


@pytest.mark.parametrize(
    ("statement", "check", "invoice"),
    [
        (
            "UPDATE billing.invoices SET total_cents = total_cents + 1 "
            "WHERE id = 'inv_juniper_1001'",
            "billing.invoice_total_mismatch",
            "inv_juniper_1001",
        ),
        (
            "UPDATE billing.payments SET status = 'failed' WHERE invoice_id = 'inv_juniper_1002'",
            "billing.paid_invoice_without_payment",
            "inv_juniper_1002",
        ),
        (
            "INSERT INTO billing.payments (id, invoice_id, account_id, amount_cents, status) "
            "VALUES ('pay_test_unpaid', 'inv_kestrel_2002', 'acct_kestrel', 41100, 'succeeded')",
            "billing.payment_on_unpaid_invoice",
            "inv_kestrel_2002",
        ),
        (
            "INSERT INTO billing.payments (id, invoice_id, account_id, amount_cents, status) "
            "VALUES ('pay_test_duplicate', 'inv_juniper_1001', 'acct_juniper', 9400, 'succeeded')",
            "billing.duplicate_payments",
            "inv_juniper_1001",
        ),
    ],
)
def test_planted_inconsistencies_are_detected(
    billing_db: LabDatabase, statement: LiteralString, check: str, invoice: str
) -> None:
    execute(billing_db, statement)

    report = run(
        billing_db,
        [
            "billing.invoice_total_mismatch",
            "billing.paid_invoice_without_payment",
            "billing.payment_on_unpaid_invoice",
            "billing.duplicate_payments",
        ],
    )

    failed = {item.name: item for item in report.results if item.status == "fail"}
    assert set(failed) == {check}
    assert [row["invoice_id"] for row in failed[check].rows] == [invoice]
    assert report.exit_code == ExitCode.PROBLEM


@pytest.mark.parametrize(
    ("prefix", "status"),
    [("bk_juniper01", "active"), ("bk_juniper00", "revoked"), ("bk_kestrel01", "expired")],
)
def test_api_key_lifecycle_states(billing_db: LabDatabase, prefix: str, status: str) -> None:
    execute(
        billing_db,
        "UPDATE billing.api_keys SET expires_at = now() - interval '1 day' "
        "WHERE key_prefix = 'bk_kestrel01'",
    )

    item = result(
        run(billing_db, ["billing.api_key_status"], {"prefix": prefix}), "billing.api_key_status"
    )

    assert item.status == "info"
    assert item.rows[0]["key_status"] == status
    assert "key_hash" not in item.columns


def test_unknown_key_prefix_is_not_found(billing_db: LabDatabase) -> None:
    item = result(
        run(billing_db, ["billing.api_key_status"], {"prefix": "bk_nosuchkey"}),
        "billing.api_key_status",
    )

    assert item.status == "fail"
    assert item.summary == "No API key has the prefix bk_nosuchkey."


@pytest.mark.parametrize(("name", "value"), [("id", "inv_juniper_1003"), ("number", "INV-1003")])
def test_invoice_lookup(billing_db: LabDatabase, name: str, value: str) -> None:
    item = result(
        run(billing_db, ["billing.invoice_lookup"], {name: value}), "billing.invoice_lookup"
    )

    row = item.rows[0]
    assert item.status == "info"
    assert (row["invoice_id"], row["status"], row["total_cents"]) == (
        "inv_juniper_1003",
        "open",
        14900,
    )
    assert (row["succeeded_payments"], row["failed_payments"]) == (0, 1)
    assert "email" not in item.columns


def test_missing_invoice_lookup_fails(billing_db: LabDatabase) -> None:
    report = run(billing_db, ["billing.invoice_lookup"], {"id": "inv_missing_0000"})

    assert result(report, "billing.invoice_lookup").status == "fail"
    assert report.exit_code == ExitCode.PROBLEM


def test_long_transactions_are_found_and_idle_sessions_are_not(billing_db: LabDatabase) -> None:
    idle = psycopg.connect(
        billing_db.url("lab_admin"), application_name="idle-client", autocommit=True
    )
    try:
        with open_transaction(
            billing_db,
            "SELECT id FROM billing.invoices WHERE id = 'inv_juniper_1003' FOR UPDATE",
            "invoice-backfill",
        ):
            found = result(
                run(billing_db, ["pg.long_transactions"], {"min_seconds": "0"}),
                "pg.long_transactions",
            )
            later = result(
                run(billing_db, ["pg.long_transactions"], {"min_seconds": "3600"}),
                "pg.long_transactions",
            )
    finally:
        idle.close()

    assert found.status == "fail"
    assert [row["application"] for row in found.rows] == ["invoice-backfill"]
    assert found.rows[0]["state"] == "idle in transaction"
    assert found.rows[0]["locks_held"] >= 1
    assert later.status == "pass"


def test_blocked_and_blocking_sessions_are_identified(billing_db: LabDatabase) -> None:
    errors: list[BaseException] = []

    def wait_for_lock() -> None:
        try:
            with psycopg.connect(
                billing_db.url("lab_admin"), application_name="payment-worker"
            ) as connection:
                connection.execute("SET lock_timeout = '20s'")
                connection.execute(
                    "UPDATE billing.invoices SET updated_at = now() WHERE id = 'inv_juniper_1003'"
                )
                connection.rollback()
        except BaseException as exc:
            errors.append(exc)

    worker = threading.Thread(target=wait_for_lock, daemon=True)
    findings: list[Any] = []
    with open_transaction(
        billing_db,
        "SELECT id FROM billing.invoices WHERE id = 'inv_juniper_1003' FOR UPDATE",
        "invoice-backfill",
    ):
        worker.start()

        def blocked() -> bool:
            item = result(run(billing_db, ["pg.blocking_sessions"]), "pg.blocking_sessions")
            findings[:] = [item]
            return bool(item.status == "fail")

        wait_for(blocked, "the blocked session")
    worker.join(timeout=POLL_SECONDS)

    row = findings[0].rows[0]
    assert not worker.is_alive()
    assert errors == []
    assert (row["blocked_application"], row["blocking_application"]) == (
        "payment-worker",
        "invoice-backfill",
    )
    assert row["waiting_for"].startswith("Lock:")
    assert row["blocking_state"] == "idle in transaction"
    assert "UPDATE billing.invoices" in row["blocked_query"]


@pytest.mark.parametrize("role", ["supportops_ro", "lab_admin"])
def test_writes_fail_even_for_a_superuser(billing_db: LabDatabase, role: str) -> None:
    dsn = SecretStr(billing_db.url(role))
    before = fetch_all(billing_db, "SELECT count(*) FROM billing.payments")

    with read_only_session(dsn, 5) as connection:
        with connection.transaction():
            setting = connection.execute(
                "SELECT current_setting('transaction_read_only')"
            ).fetchone()
        with pytest.raises(psycopg.errors.ReadOnlySqlTransaction), connection.transaction():
            connection.execute("UPDATE billing.invoices SET total_cents = 0")
        with pytest.raises(psycopg.errors.ReadOnlySqlTransaction), connection.transaction():
            connection.execute("DELETE FROM billing.payments")

    assert setting == ("on",)
    assert fetch_all(billing_db, "SELECT count(*) FROM billing.payments") == before


def test_superuser_connection_is_warned_about_and_changes_nothing(billing_db: LabDatabase) -> None:
    before = fetch_all(billing_db, "SELECT count(*), sum(total_cents) FROM billing.invoices")
    plan = plan_checks(
        [], run_all=True, parameters={"prefix": "bk_juniper01", "number": "INV-1001"}
    )

    report = run_checks(SecretStr(billing_db.url("lab_admin")), plan, connect_timeout_seconds=5)

    connectivity = result(report, "db.connectivity")
    assert connectivity.status == "warn"
    assert "superuser" in connectivity.notes[0]
    assert report.exit_code == ExitCode.PROBLEM
    assert (
        fetch_all(billing_db, "SELECT count(*), sum(total_cents) FROM billing.invoices") == before
    )


def test_write_privileges_are_warned_about(billing_db: LabDatabase) -> None:
    app_role = result(run(billing_db, ["db.connectivity"], role="billing_app"), "db.connectivity")
    support_role = result(run(billing_db, ["db.connectivity"]), "db.connectivity")

    assert app_role.status == "warn"
    assert "Tables this role could change outside SupportOps: 6" in app_role.notes[0]
    assert "superuser" not in " ".join(app_role.notes)
    assert support_role.status == "pass"
    assert support_role.notes == []


@pytest.fixture
def unmonitored_role(billing_db: LabDatabase) -> Iterator[tuple[str, str]]:
    role = f"support_nomon_{uuid.uuid4().hex[:8]}"
    password = "it_nomon_password"
    statements = [
        sql.SQL("CREATE ROLE {} LOGIN PASSWORD {}").format(
            sql.Identifier(role), sql.Literal(password)
        ),
        sql.SQL("GRANT CONNECT ON DATABASE {} TO {}").format(
            sql.Identifier(billing_db.database), sql.Identifier(role)
        ),
        sql.SQL("GRANT USAGE ON SCHEMA billing TO {}").format(sql.Identifier(role)),
        sql.SQL("GRANT SELECT ON ALL TABLES IN SCHEMA billing TO {}").format(sql.Identifier(role)),
    ]
    with psycopg.connect(billing_db.url("lab_admin"), autocommit=True) as connection:
        for statement in statements:
            connection.execute(statement)
    try:
        yield role, password
    finally:
        with psycopg.connect(billing_db.url("lab_admin"), autocommit=True) as connection:
            connection.execute(sql.SQL("DROP OWNED BY {}").format(sql.Identifier(role)))
            connection.execute(sql.SQL("DROP ROLE {}").format(sql.Identifier(role)))


def test_missing_monitoring_rights_are_reported_honestly(
    billing_db: LabDatabase, unmonitored_role: tuple[str, str]
) -> None:
    role, password = unmonitored_role

    with open_transaction(billing_db, "SELECT 1", "invoice-backfill"):
        report = run(
            billing_db,
            ["db.connectivity", "pg.long_transactions"],
            {"min_seconds": "0"},
            role=role,
            password=password,
        )

    connectivity = result(report, "db.connectivity")
    long_transactions = result(report, "pg.long_transactions")
    assert connectivity.status == "pass"
    assert any("pg_monitor" in note for note in connectivity.notes)
    assert long_transactions.status == "info"
    assert long_transactions.complete is False
    assert not report.session.monitoring


def test_unreachable_database_is_a_clear_error() -> None:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    closed = LabDatabase(host="127.0.0.1", port=port)

    with pytest.raises(DatabaseError, match="didn't accept the connection") as excinfo:
        run(closed, ["pg.connections"])

    assert "it_readonly_password" not in excinfo.value.message


def test_statement_timeout_is_reported_and_the_run_continues(billing_db: LabDatabase) -> None:
    with open_transaction(
        billing_db, "LOCK TABLE billing.invoice_lines IN ACCESS EXCLUSIVE MODE", "schema-migration"
    ):
        report = run(
            billing_db,
            ["billing.invoice_total_mismatch", "billing.duplicate_payments"],
            statement_timeout_ms=500,
        )

    timed_out = result(report, "billing.invoice_total_mismatch")
    assert (timed_out.status, timed_out.error) == ("error", "statement_timeout")
    assert result(report, "billing.duplicate_payments").status == "pass"
    assert report.exit_code == ExitCode.INCOMPLETE


def test_sessions_are_closed_after_a_run(billing_db: LabDatabase) -> None:
    run(billing_db, ["db.connectivity", "pg.connections"])

    def no_support_sessions() -> bool:
        rows = fetch_all(
            billing_db,
            "SELECT count(*) FROM pg_stat_activity "
            "WHERE application_name = 'supportops' AND datname = current_database()",
        )
        return rows == [(0,)]

    wait_for(no_support_sessions, "SupportOps sessions to close")


def test_cli_never_shows_database_credentials(billing_db: LabDatabase) -> None:
    runner = CliRunner()
    wrong = billing_db.url("supportops_ro", password="Wr%25ong%40Pw-9917")
    good = billing_db.url("supportops_ro")

    rejected = runner.invoke(app, ["db", "run", "--all"], env={"SUPPORTOPS_DB_URL": wrong})
    accepted = runner.invoke(
        app,
        ["db", "run", "billing.api_key_status", "--param", "prefix=bk_juniper01", "--json"],
        env={"SUPPORTOPS_DB_URL": good},
    )

    report = json.loads(accepted.stdout)
    assert rejected.exit_code == 3
    assert "rejected the login" in rejected.stderr
    assert "Wr%25ong" not in rejected.output
    assert "Wr%ong" not in rejected.output
    assert accepted.exit_code == 0
    assert "it_readonly_password" not in accepted.output
    assert "key_hash" not in json.dumps(report)


def test_key_lookup_for_auth_check_reads_real_metadata(billing_db: LabDatabase) -> None:
    settings = Settings(db_url=SecretStr(billing_db.url("supportops_ro")))

    revoked = lookup_key_record(settings, "bk_juniper00")
    unknown = lookup_key_record(settings, "bk_nosuchkey")

    assert revoked.status == "found"
    assert revoked.record is not None
    assert revoked.record["key_status"] == "revoked"
    assert unknown.status == "not_found"
