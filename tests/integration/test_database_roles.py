import psycopg
import pytest
from psycopg import errors, sql

from tests.integration.support import LabDatabase

pytestmark = pytest.mark.integration

TABLES = ["accounts", "api_keys", "customers", "invoices", "invoice_lines", "payments"]


@pytest.mark.parametrize("table", TABLES)
def test_readonly_role_can_read_every_table(lab_database: LabDatabase, table: str) -> None:
    with psycopg.connect(lab_database.url("supportops_ro")) as connection:
        statement = sql.SQL("SELECT count(*) FROM billing.{}").format(sql.Identifier(table))
        row = connection.execute(statement).fetchone()

    assert row is not None
    assert row[0] > 0


def test_readonly_role_is_read_only_by_default(lab_database: LabDatabase) -> None:
    with (
        psycopg.connect(lab_database.url("supportops_ro")) as connection,
        pytest.raises(errors.ReadOnlySqlTransaction),
    ):
        connection.execute("DELETE FROM billing.payments")


def test_readonly_role_has_no_write_privileges(lab_database: LabDatabase) -> None:
    with psycopg.connect(lab_database.url("supportops_ro"), autocommit=True) as connection:
        connection.execute("SET default_transaction_read_only = off")
        with pytest.raises(errors.InsufficientPrivilege):
            connection.execute("DELETE FROM billing.payments")


def test_readonly_role_settings(lab_database: LabDatabase) -> None:
    with psycopg.connect(lab_database.url("supportops_ro")) as connection:
        timeout = connection.execute("SHOW statement_timeout").fetchone()
        monitor = connection.execute("SELECT pg_has_role('pg_monitor', 'MEMBER')").fetchone()

    assert timeout == ("5s",)
    assert monitor == (True,)


def test_app_role_can_insert_and_update(lab_database: LabDatabase) -> None:
    with psycopg.connect(lab_database.url("billing_app")) as connection:
        connection.execute(
            "INSERT INTO billing.customers (id, account_id, name, email)"
            " VALUES ('cus_it_tmp', 'acct_juniper', 'Temporary', 'tmp@example.com')"
        )
        connection.execute("UPDATE billing.customers SET name = 'Renamed' WHERE id = 'cus_it_tmp'")
        connection.rollback()


def test_app_role_cannot_delete(lab_database: LabDatabase) -> None:
    with (
        psycopg.connect(lab_database.url("billing_app")) as connection,
        pytest.raises(errors.InsufficientPrivilege),
    ):
        connection.execute("DELETE FROM billing.payments")


def test_app_role_cannot_change_the_schema(lab_database: LabDatabase) -> None:
    with (
        psycopg.connect(lab_database.url("billing_app")) as connection,
        pytest.raises(errors.InsufficientPrivilege),
    ):
        connection.execute("CREATE TABLE billing.scratch (id int)")
