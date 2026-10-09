import secrets

import psycopg
import pytest
from psycopg import sql
from pydantic import SecretStr
from typer.testing import CliRunner

from supportops.cli.main import app
from supportops.db.catalog import CATALOG, CHECKS
from supportops.db.runner import CheckResult, PlannedCheck, run_checks
from tests.integration.orderflow.support import (
    ADMIN,
    DEFECTS,
    PRIVILEGES,
    READ_ONLY_ROLE,
    READABLE_TABLES,
    TEMPLATE_DATABASE,
    OrderflowDatabase,
    admin_execute,
)
from tests.integration.support import LabDatabase

pytestmark = pytest.mark.integration

ORDERFLOW_CHECKS = [check for check in CATALOG if check.pack == "orderflow"]
CONSISTENCY = [check.name for check in ORDERFLOW_CHECKS if check.kind == "consistency"]
runner = CliRunner()


def run(
    database: OrderflowDatabase, *names: str, order_id: int | None = None
) -> dict[str, CheckResult]:
    plan = [
        PlannedCheck(CHECKS[name], {"id": order_id} if name == "orderflow.order_lookup" else {})
        for name in names
    ]
    report = run_checks(SecretStr(database.url()), plan, connect_timeout_seconds=5)
    assert report.session.read_only
    return {result.name: result for result in report.results}


def cli(database: OrderflowDatabase, *args: str) -> tuple[int, str, str]:
    result = runner.invoke(
        app,
        ["db", "run", *args],
        env={
            "COLUMNS": "200",
            "SUPPORTOPS_TARGET": "orderflow",
            "SUPPORTOPS_DB_URL": database.url(),
        },
    )
    return result.exit_code, result.stdout, result.stderr


def test_the_role_file_grants_exactly_the_documented_privileges(
    orderflow_db: OrderflowDatabase,
) -> None:
    rows = admin_execute(orderflow_db, PRIVILEGES, {"role": READ_ONLY_ROLE})

    readable = {name for name, can_select, _ in rows if can_select}
    assert readable == READABLE_TABLES
    assert "users" in {name for name, _, _ in rows}
    assert not any(can_write for _, _, can_write in rows)


def test_the_role_file_targets_the_database_psql_was_connected_to(
    orderflow_server: OrderflowDatabase,
) -> None:
    rows = admin_execute(
        orderflow_server,
        "SELECT datacl::text FROM pg_database WHERE datname = %(name)s",
        {"name": TEMPLATE_DATABASE},
    )
    settings = admin_execute(
        orderflow_server,
        "SELECT rolconfig FROM pg_roles WHERE rolname = %(role)s",
        {"role": READ_ONLY_ROLE},
    )

    assert f"{READ_ONLY_ROLE}=c/" in rows[0][0]
    assert set(settings[0][0]) == {"default_transaction_read_only=on", "statement_timeout=5s"}


@pytest.mark.parametrize(
    ("statement", "error"),
    [
        ("SELECT email FROM public.users", psycopg.errors.InsufficientPrivilege),
        ("UPDATE public.orders SET status = 'CANCELLED'", psycopg.errors.ReadOnlySqlTransaction),
        ("DELETE FROM public.order_items", psycopg.errors.ReadOnlySqlTransaction),
    ],
)
def test_the_role_can_neither_write_nor_read_users(
    orderflow_db: OrderflowDatabase, statement: str, error: type[psycopg.Error]
) -> None:
    with (
        psycopg.connect(orderflow_db.url(), autocommit=True) as connection,
        pytest.raises(error),
    ):
        connection.execute(statement)

    assert admin_execute(orderflow_db, "SELECT count(*) FROM order_items") == [(4,)]


def test_writes_are_refused_even_inside_a_read_write_transaction(
    orderflow_db: OrderflowDatabase,
) -> None:
    with psycopg.connect(orderflow_db.url()) as connection:
        connection.execute("SET TRANSACTION READ WRITE")
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            connection.execute("DELETE FROM public.order_items")


def test_connectivity_reports_a_read_only_role_without_monitoring(
    orderflow_db: OrderflowDatabase,
) -> None:
    result = run(orderflow_db, "db.connectivity")["db.connectivity"]

    row = result.rows[0]
    assert result.status == "pass"
    assert (row["transaction_read_only"], row["superuser"], row["can_monitor"]) == (
        "on",
        False,
        False,
    )
    assert row["writable_tables"] == []


def test_clean_data_passes_every_consistency_check(orderflow_db: OrderflowDatabase) -> None:
    results = run(orderflow_db, *CONSISTENCY)

    assert {name: result.status for name, result in results.items()} == dict.fromkeys(
        CONSISTENCY, "pass"
    )


@pytest.mark.parametrize(
    ("order_id", "expected"),
    [
        (
            1,
            {
                "status": "PENDING",
                "item_count": 2,
                "units_ordered": 5,
                "units_reserved": 5,
                "units_restored": 0,
                "total_amount": 81,
                "items_total": 81,
            },
        ),
        (
            2,
            {
                "status": "CANCELLED",
                "item_count": 1,
                "units_ordered": 2,
                "units_reserved": 2,
                "units_restored": 2,
            },
        ),
    ],
)
def test_the_order_lookup_reconciles_items_and_stock_movements(
    orderflow_db: OrderflowDatabase, order_id: int, expected: dict[str, object]
) -> None:
    result = run(orderflow_db, "orderflow.order_lookup", order_id=order_id)[
        "orderflow.order_lookup"
    ]

    [row] = result.rows
    assert result.status == "info"
    assert {name: row[name] for name in expected} == expected
    assert row["has_idempotency_key"] is False
    assert "idempotency_key" not in row
    assert "request_hash" not in row


def test_an_unknown_order_fails_the_lookup(orderflow_db: OrderflowDatabase) -> None:
    result = run(orderflow_db, "orderflow.order_lookup", order_id=999)["orderflow.order_lookup"]

    assert result.status == "fail"
    assert result.summary == "No order has ID 999."


@pytest.mark.parametrize(("check", "statement"), list(DEFECTS.items()), ids=list(DEFECTS))
def test_each_planted_defect_fails_its_check(
    orderflow_db: OrderflowDatabase, check: str, statement: str
) -> None:
    admin_execute(orderflow_db, statement)

    results = run(orderflow_db, *CONSISTENCY)

    failing = {name for name, result in results.items() if result.status == "fail"}
    assert failing == {check}
    assert results[check].row_count == 1


def test_the_inventory_check_shows_both_sides(orderflow_db: OrderflowDatabase) -> None:
    admin_execute(orderflow_db, DEFECTS["orderflow.inventory_mismatch"])

    [row] = run(orderflow_db, "orderflow.inventory_mismatch")["orderflow.inventory_mismatch"].rows

    assert (row["product_id"], row["sku"], row["quantity_on_hand"], row["movement_total"]) == (
        1,
        "WIDGET-1",
        90,
        95,
    )


def test_a_missing_reservation_shows_in_the_order_lookup(orderflow_db: OrderflowDatabase) -> None:
    admin_execute(
        orderflow_db,
        "DELETE FROM inventory_movements WHERE order_id = 3 AND reason = 'ORDER_PLACED'",
    )

    [row] = run(orderflow_db, "orderflow.order_lookup", order_id=3)["orderflow.order_lookup"].rows

    assert (row["status"], row["units_ordered"], row["units_reserved"]) == ("CONFIRMED", 2, 0)


def test_db_run_all_skips_the_lookup_and_reports_limited_visibility(
    orderflow_db: OrderflowDatabase,
) -> None:
    exit_code, stdout, _ = cli(orderflow_db, "--all")

    output = " ".join(stdout.split())
    assert exit_code == 0, stdout
    assert "SKIPPED orderflow.order_lookup" in output
    assert "Needs --param id" in output
    assert "activity results are incomplete, not clean" in output
    assert "billing." not in output
    assert "PASS pg.blocking_sessions" not in output
    assert orderflow_db.role_password not in stdout


def test_db_run_all_runs_the_lookup_when_given_an_id(orderflow_db: OrderflowDatabase) -> None:
    exit_code, stdout, _ = cli(orderflow_db, "--all", "--param", "id=1")

    assert exit_code == 0, stdout
    assert "INFO     orderflow.order_lookup" in stdout


def test_db_run_all_fails_on_a_planted_defect(orderflow_db: OrderflowDatabase) -> None:
    admin_execute(orderflow_db, DEFECTS["orderflow.order_total_mismatch"])

    exit_code, stdout, _ = cli(orderflow_db, "--all")

    assert exit_code == 1
    assert "FAIL     orderflow.order_total_mismatch" in stdout


def test_billing_checks_are_refused_for_the_orderflow_target(
    orderflow_db: OrderflowDatabase,
) -> None:
    exit_code, _, stderr = cli(orderflow_db, "billing.duplicate_payments")

    assert exit_code == 2
    assert "can't run against the 'orderflow' target" in " ".join(stderr.split())


def test_orderflow_checks_against_another_schema_report_missing_objects(
    lab_database: LabDatabase,
) -> None:
    plan = [
        PlannedCheck(check, {"id": 1} if check.parameters else {}) for check in ORDERFLOW_CHECKS
    ]

    report = run_checks(
        SecretStr(lab_database.url("supportops_ro")), plan, connect_timeout_seconds=5
    )

    assert {result.status for result in report.results} == {"error"}
    assert {result.error for result in report.results} == {"missing_object"}


def test_a_role_without_grants_gets_permission_denied(orderflow_db: OrderflowDatabase) -> None:
    role = f"no_grants_{secrets.token_hex(4)}"
    password = secrets.token_urlsafe(18)
    with psycopg.connect(orderflow_db.url(ADMIN), autocommit=True) as connection:
        connection.execute(
            sql.SQL("CREATE ROLE {} LOGIN PASSWORD {}").format(
                sql.Identifier(role), sql.Literal(password)
            )
        )
        connection.execute(
            sql.SQL("GRANT CONNECT ON DATABASE {} TO {}").format(
                sql.Identifier(orderflow_db.database), sql.Identifier(role)
            )
        )

    report = run_checks(
        SecretStr(orderflow_db.url(role, password=password)),
        [PlannedCheck(CHECKS["orderflow.order_total_mismatch"])],
        connect_timeout_seconds=5,
    )

    [result] = report.results
    assert (result.status, result.error) == ("error", "permission_denied")
