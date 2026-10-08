import hashlib
from typing import LiteralString

import psycopg
import pytest

from tests.integration.support import LAB_KEYS, LabDatabase

pytestmark = pytest.mark.integration


def query(database: LabDatabase, sql: LiteralString) -> list[tuple[object, ...]]:
    with psycopg.connect(database.url("supportops_ro")) as connection:
        return connection.execute(sql).fetchall()


def test_accounts_cover_active_and_suspended(lab_database: LabDatabase) -> None:
    rows = query(lab_database, "SELECT id, status FROM billing.accounts ORDER BY id")

    assert rows == [
        ("acct_alder", "suspended"),
        ("acct_juniper", "active"),
        ("acct_kestrel", "active"),
    ]


def test_invoice_totals_match_their_lines(lab_database: LabDatabase) -> None:
    mismatches = query(
        lab_database,
        """
        SELECT invoice.id
        FROM billing.invoices AS invoice
        LEFT JOIN billing.invoice_lines AS line ON line.invoice_id = invoice.id
        GROUP BY invoice.id, invoice.total_cents
        HAVING invoice.total_cents <> coalesce(sum(line.amount_cents), 0)
        """,
    )

    assert mismatches == []


def test_payments_are_consistent_with_invoice_status(lab_database: LabDatabase) -> None:
    rows = query(
        lab_database,
        """
        SELECT invoice.id, invoice.status, invoice.total_cents,
               count(payment.id), coalesce(sum(payment.amount_cents), 0)
        FROM billing.invoices AS invoice
        LEFT JOIN billing.payments AS payment
               ON payment.invoice_id = invoice.id AND payment.status = 'succeeded'
        GROUP BY invoice.id
        """,
    )

    for invoice_id, status, total, payments, paid in rows:
        if status == "paid":
            assert (payments, paid) == (1, total), invoice_id
        else:
            assert payments == 0, invoice_id


def test_api_keys_store_only_prefix_and_hash(lab_database: LabDatabase) -> None:
    rows = query(
        lab_database,
        "SELECT id, key_prefix, key_hash, revoked_at IS NOT NULL FROM billing.api_keys",
    )

    assert len(rows) == len(LAB_KEYS)
    for key_id, prefix, key_hash, revoked in rows:
        lab_key = LAB_KEYS[str(key_id)]
        assert prefix == lab_key[:12]
        assert key_hash == hashlib.sha256(lab_key.encode()).hexdigest()
        assert revoked == (key_id == "key_juniper_old")
