import time
from typing import Any

import psycopg
import pytest
from fastapi.testclient import TestClient

from billing_api.faults import Fault
from tests.integration.support import (
    JUNIPER_KEY,
    KESTREL_KEY,
    ClientFactory,
    LabDatabase,
    bearer,
    execute,
    fetch_all,
)
from tests.log_capture import JsonCapture

pytestmark = pytest.mark.integration

JUNIPER = bearer(JUNIPER_KEY)
KESTREL = bearer(KESTREL_KEY)
OPEN_INVOICE = "inv_juniper_1003"


def invoice_state(database: LabDatabase, invoice_id: str) -> tuple[Any, ...]:
    [row] = fetch_all(
        database,
        """
        SELECT invoice.status,
               invoice.paid_at IS NOT NULL,
               count(payment.id) FILTER (WHERE payment.status = 'succeeded')
        FROM billing.invoices AS invoice
        LEFT JOIN billing.payments AS payment ON payment.invoice_id = invoice.id
        WHERE invoice.id = %s
        GROUP BY invoice.id
        """,
        (invoice_id,),
    )
    return row


def test_paying_an_open_invoice_records_one_payment_and_marks_it_paid(
    client: TestClient, billing_db: LabDatabase, logs: JsonCapture
) -> None:
    response = client.post(f"/v1/invoices/{OPEN_INVOICE}/pay", headers=JUNIPER)

    assert response.status_code == 200
    result = response.json()
    assert result["payment"]["amount_cents"] == 14900
    assert result["payment"]["currency"] == "EUR"
    assert result["payment"]["status"] == "succeeded"
    assert result["invoice"]["status"] == "paid"
    assert result["invoice"]["paid_at"] is not None
    assert invoice_state(billing_db, OPEN_INVOICE) == ("paid", True, 1)
    [recorded] = logs.events("payment.recorded")
    [paid] = logs.events("invoice.paid")
    assert recorded["payment_id"] == paid["payment_id"] == result["payment"]["id"]


def test_paying_twice_is_a_conflict(client: TestClient, billing_db: LabDatabase) -> None:
    client.post(f"/v1/invoices/{OPEN_INVOICE}/pay", headers=JUNIPER)

    response = client.post(f"/v1/invoices/{OPEN_INVOICE}/pay", headers=JUNIPER)

    assert response.status_code == 409
    assert response.json()["code"] == "INVOICE_NOT_PAYABLE"
    assert response.json()["detail"] == f"Invoice {OPEN_INVOICE} is already paid."
    assert invoice_state(billing_db, OPEN_INVOICE) == ("paid", True, 1)


@pytest.mark.parametrize(
    ("headers", "invoice_id", "status"),
    [(JUNIPER, "inv_juniper_1004", "draft"), (KESTREL, "inv_kestrel_2004", "void")],
)
def test_draft_and_void_invoices_cannot_be_paid(
    client: TestClient,
    billing_db: LabDatabase,
    logs: JsonCapture,
    headers: dict[str, str],
    invoice_id: str,
    status: str,
) -> None:
    response = client.post(f"/v1/invoices/{invoice_id}/pay", headers=headers)

    assert response.status_code == 409
    assert response.json()["code"] == "INVOICE_NOT_PAYABLE"
    assert invoice_state(billing_db, invoice_id)[0] == status
    [rejected] = logs.events("payment.rejected")
    assert rejected["invoice_status"] == status


def test_another_tenants_invoice_cannot_be_paid(
    client: TestClient, billing_db: LabDatabase
) -> None:
    response = client.post(f"/v1/invoices/{OPEN_INVOICE}/pay", headers=KESTREL)

    assert response.status_code == 404
    assert invoice_state(billing_db, OPEN_INVOICE) == ("open", False, 0)


def test_a_failure_during_payment_rolls_back_everything(
    client: TestClient, billing_db: LabDatabase, logs: JsonCapture
) -> None:
    execute(
        billing_db,
        "ALTER TABLE billing.invoices ADD CONSTRAINT ck_test_refuse_payment"
        " CHECK (status <> 'paid') NOT VALID",
    )

    response = client.post(
        f"/v1/invoices/{OPEN_INVOICE}/pay", headers={**JUNIPER, "X-Request-Id": "rollback-1"}
    )

    assert response.status_code == 500
    assert response.json()["code"] == "INTERNAL_ERROR"
    assert response.json()["request_id"] == "rollback-1"
    assert invoice_state(billing_db, OPEN_INVOICE) == ("open", False, 0)
    assert logs.events("payment.recorded") == []


def test_lock_contention_returns_database_busy_within_the_lock_timeout(
    make_client: ClientFactory, billing_db: LabDatabase, logs: JsonCapture
) -> None:
    client = make_client(db_lock_timeout_ms=300)
    with psycopg.connect(billing_db.url("lab_admin")) as blocker:
        blocker.execute("SELECT id FROM billing.invoices WHERE id = %s FOR UPDATE", (OPEN_INVOICE,))
        started = time.monotonic()

        response = client.post(f"/v1/invoices/{OPEN_INVOICE}/pay", headers=JUNIPER)

        elapsed = time.monotonic() - started
        blocker.rollback()

    assert response.status_code == 503
    assert response.json()["code"] == "DATABASE_BUSY"
    assert response.headers["Retry-After"] == "5"
    assert elapsed < 2.5
    assert invoice_state(billing_db, OPEN_INVOICE) == ("open", False, 0)
    [timeout] = logs.events("db.lock_timeout")
    assert "lock timeout" in timeout["detail"]


def test_reads_are_not_blocked_by_a_row_lock(
    make_client: ClientFactory, billing_db: LabDatabase
) -> None:
    client = make_client(db_lock_timeout_ms=300)
    with psycopg.connect(billing_db.url("lab_admin")) as blocker:
        blocker.execute(
            "UPDATE billing.invoices SET updated_at = now() WHERE id = %s", (OPEN_INVOICE,)
        )

        response = client.get(f"/v1/invoices/{OPEN_INVOICE}", headers=JUNIPER)

        blocker.rollback()

    assert response.status_code == 200


def test_partial_commit_fault_leaves_a_payment_on_an_open_invoice(
    make_client: ClientFactory, billing_db: LabDatabase, logs: JsonCapture
) -> None:
    client = make_client(faults=frozenset({Fault.PAYMENT_PARTIAL_COMMIT}))

    first = client.post(
        f"/v1/invoices/{OPEN_INVOICE}/pay", headers={**JUNIPER, "X-Request-Id": "fault-1"}
    )
    retry = client.post(
        f"/v1/invoices/{OPEN_INVOICE}/pay", headers={**JUNIPER, "X-Request-Id": "fault-2"}
    )

    assert first.status_code == retry.status_code == 500
    assert first.json()["code"] == "INTERNAL_ERROR"
    assert first.json()["request_id"] == "fault-1"
    assert "payment" not in first.json()["detail"].lower()
    assert invoice_state(billing_db, OPEN_INVOICE) == ("open", False, 2)
    [enabled] = logs.events("lab.fault_enabled")
    assert enabled["fault"] == "payment_partial_commit"
    assert len(logs.events("payment.recorded")) == 2
    assert logs.events("invoice.paid") == []
    errors = logs.events("unhandled_exception")
    assert [error["error_type"] for error in errors] == ["InjectedFault", "InjectedFault"]
    assert [error["request_id"] for error in errors] == ["fault-1", "fault-2"]


def test_faults_are_off_by_default(client: TestClient, logs: JsonCapture) -> None:
    response = client.post(f"/v1/invoices/{OPEN_INVOICE}/pay", headers=JUNIPER)

    assert response.status_code == 200
    assert logs.events("lab.fault_enabled") == []
