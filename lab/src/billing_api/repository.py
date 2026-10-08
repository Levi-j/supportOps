from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime

from psycopg.rows import class_row, dict_row

from billing_api.database import Connection
from billing_api.schemas import (
    Customer,
    Invoice,
    InvoiceLine,
    InvoiceLineCreate,
    InvoiceStatus,
    InvoiceSummary,
)


@dataclass(frozen=True)
class ApiKeyRecord:
    key_id: str
    account_id: str
    account_name: str
    account_status: str
    key_hash: str
    label: str
    created_at: datetime
    revoked: bool
    expired: bool


@dataclass(frozen=True)
class LockedInvoice:
    id: str
    status: str
    currency: str
    total_cents: int


@dataclass(frozen=True)
class PaymentRow:
    id: str
    invoice_id: str
    amount_cents: int
    status: str
    created_at: datetime


def find_api_key(connection: Connection, key_prefix: str) -> ApiKeyRecord | None:
    with connection.cursor(row_factory=class_row(ApiKeyRecord)) as cursor:
        cursor.execute(
            """
            SELECT api_key.id AS key_id,
                   api_key.account_id,
                   account.name AS account_name,
                   account.status AS account_status,
                   api_key.key_hash,
                   api_key.label,
                   api_key.created_at,
                   api_key.revoked_at IS NOT NULL AND api_key.revoked_at <= now() AS revoked,
                   api_key.expires_at IS NOT NULL AND api_key.expires_at <= now() AS expired
            FROM billing.api_keys AS api_key
            JOIN billing.accounts AS account ON account.id = api_key.account_id
            WHERE api_key.key_prefix = %s
            """,
            (key_prefix,),
        )
        return cursor.fetchone()


def list_customers(
    connection: Connection, account_id: str, limit: int, offset: int
) -> list[Customer]:
    with connection.cursor(row_factory=class_row(Customer)) as cursor:
        cursor.execute(
            """
            SELECT id, name, email, created_at
            FROM billing.customers
            WHERE account_id = %s
            ORDER BY created_at DESC, id DESC
            LIMIT %s OFFSET %s
            """,
            (account_id, limit, offset),
        )
        return cursor.fetchall()


def get_customer(connection: Connection, account_id: str, customer_id: str) -> Customer | None:
    with connection.cursor(row_factory=class_row(Customer)) as cursor:
        cursor.execute(
            """
            SELECT id, name, email, created_at
            FROM billing.customers
            WHERE account_id = %s AND id = %s
            """,
            (account_id, customer_id),
        )
        return cursor.fetchone()


def insert_customer(
    connection: Connection, account_id: str, customer_id: str, name: str, email: str
) -> Customer:
    with connection.cursor(row_factory=class_row(Customer)) as cursor:
        cursor.execute(
            """
            INSERT INTO billing.customers (id, account_id, name, email)
            VALUES (%s, %s, %s, %s)
            RETURNING id, name, email, created_at
            """,
            (customer_id, account_id, name, email),
        )
        return _one(cursor.fetchone())


def list_invoices(
    connection: Connection,
    account_id: str,
    status: InvoiceStatus | None,
    customer_id: str | None,
    limit: int,
    offset: int,
) -> list[InvoiceSummary]:
    with connection.cursor(row_factory=class_row(InvoiceSummary)) as cursor:
        cursor.execute(
            """
            SELECT id, number, customer_id, status, currency, total_cents,
                   due_date, created_at, paid_at
            FROM billing.invoices
            WHERE account_id = %(account_id)s
              AND (%(status)s::text IS NULL OR status = %(status)s)
              AND (%(customer_id)s::text IS NULL OR customer_id = %(customer_id)s)
            ORDER BY created_at DESC, id DESC
            LIMIT %(limit)s OFFSET %(offset)s
            """,
            {
                "account_id": account_id,
                "status": status,
                "customer_id": customer_id,
                "limit": limit,
                "offset": offset,
            },
        )
        return cursor.fetchall()


def get_invoice(connection: Connection, account_id: str, invoice_id: str) -> Invoice | None:
    with connection.cursor(row_factory=dict_row) as cursor:
        cursor.execute(
            """
            SELECT id, number, customer_id, status, currency, total_cents,
                   due_date, created_at, updated_at, paid_at
            FROM billing.invoices
            WHERE account_id = %s AND id = %s
            """,
            (account_id, invoice_id),
        )
        invoice = cursor.fetchone()
    if invoice is None:
        return None
    with connection.cursor(row_factory=class_row(InvoiceLine)) as cursor:
        cursor.execute(
            """
            SELECT description, quantity, unit_amount_cents, amount_cents
            FROM billing.invoice_lines
            WHERE invoice_id = %s
            ORDER BY id
            """,
            (invoice_id,),
        )
        lines = cursor.fetchall()
    return Invoice(**invoice, lines=lines)


def lock_account(connection: Connection, account_id: str) -> None:
    connection.execute("SELECT id FROM billing.accounts WHERE id = %s FOR UPDATE", (account_id,))


def next_invoice_number(connection: Connection, account_id: str) -> str:
    row = connection.execute(
        """
        SELECT coalesce(max(substring(number FROM '^INV-([0-9]+)$')::integer), 0) + 1
        FROM billing.invoices
        WHERE account_id = %s
        """,
        (account_id,),
    ).fetchone()
    return f"INV-{_one(row)[0]:04d}"


def insert_invoice(
    connection: Connection,
    *,
    invoice_id: str,
    account_id: str,
    customer_id: str,
    number: str,
    currency: str,
    due_date: date | None,
    total_cents: int,
    lines: Sequence[InvoiceLineCreate],
) -> None:
    connection.execute(
        """
        INSERT INTO billing.invoices
            (id, account_id, customer_id, number, status, currency, total_cents, due_date)
        VALUES (%s, %s, %s, %s, 'open', %s, %s, coalesce(%s::date, current_date + 30))
        """,
        (invoice_id, account_id, customer_id, number, currency, total_cents, due_date),
    )
    with connection.cursor() as cursor:
        cursor.executemany(
            """
            INSERT INTO billing.invoice_lines
                (invoice_id, description, quantity, unit_amount_cents, amount_cents)
            VALUES (%s, %s, %s, %s, %s)
            """,
            [
                (
                    invoice_id,
                    line.description,
                    line.quantity,
                    line.unit_amount_cents,
                    line.quantity * line.unit_amount_cents,
                )
                for line in lines
            ],
        )


def lock_invoice(connection: Connection, account_id: str, invoice_id: str) -> LockedInvoice | None:
    with connection.cursor(row_factory=class_row(LockedInvoice)) as cursor:
        cursor.execute(
            """
            SELECT id, status, currency, total_cents
            FROM billing.invoices
            WHERE account_id = %s AND id = %s
            FOR UPDATE
            """,
            (account_id, invoice_id),
        )
        return cursor.fetchone()


def insert_payment(
    connection: Connection, payment_id: str, account_id: str, invoice_id: str, amount_cents: int
) -> PaymentRow:
    with connection.cursor(row_factory=class_row(PaymentRow)) as cursor:
        cursor.execute(
            """
            INSERT INTO billing.payments (id, invoice_id, account_id, amount_cents, status)
            VALUES (%s, %s, %s, %s, 'succeeded')
            RETURNING id, invoice_id, amount_cents, status, created_at
            """,
            (payment_id, invoice_id, account_id, amount_cents),
        )
        return _one(cursor.fetchone())


def mark_invoice_paid(connection: Connection, account_id: str, invoice_id: str) -> None:
    connection.execute(
        """
        UPDATE billing.invoices
        SET status = 'paid', paid_at = now(), updated_at = now()
        WHERE account_id = %s AND id = %s
        """,
        (account_id, invoice_id),
    )


def _one[T](row: T | None) -> T:
    if row is None:
        raise RuntimeError("The database returned no row where one was expected.")
    return row
