from dataclasses import replace

import psycopg
import pytest
from pydantic import SecretStr

from supportops.investigation.live import BLOCKING_CHECK, LONG_TRANSACTIONS_CHECK
from supportops_lab.contention import (
    API_APPLICATION_NAME,
    ActivityQuery,
    LockWaitNotObserved,
    activity_query,
    capture_lock_wait,
    holder_rows,
    wait_for_holder,
)
from supportops_lab.customer import send_requests
from supportops_lab.docker import DockerClient
from supportops_lab.scenarios import INVOICE_BACKFILL, CustomerRequest
from supportops_lab.state import LockWaitCapture
from tests.integration.support import KESTREL_KEY, ApiStarter, LabDatabase

pytestmark = pytest.mark.integration

INVOICE = INVOICE_BACKFILL.invoice_id
PAY = CustomerRequest(
    "it-lock-pay",
    "POST",
    f"/v1/invoices/{INVOICE}/pay",
    503,
    api_key=KESTREL_KEY,
    expected_code="DATABASE_BUSY",
)


@pytest.fixture
def database(billing_db: LabDatabase) -> LabDatabase:
    assert billing_db.container_id
    return replace(billing_db, host="127.0.0.1") if billing_db.host == "localhost" else billing_db


def start_holder(database: LabDatabase) -> None:
    DockerClient().start_lock_holder(
        database.container_id,
        sql=INVOICE_BACKFILL.sql,
        role=INVOICE_BACKFILL.role,
        application_name=INVOICE_BACKFILL.application_name,
        database=database.database,
    )


def activity(database: LabDatabase) -> ActivityQuery:
    return activity_query(SecretStr(database.url("supportops_ro")), connect_timeout_seconds=3)


def succeeded_payments(database: LabDatabase) -> int:
    with psycopg.connect(database.url("lab_admin")) as connection:
        row = connection.execute(
            "SELECT count(*) FROM billing.payments WHERE invoice_id = %s AND status = 'succeeded'",
            (INVOICE,),
        ).fetchone()
    assert row is not None
    return int(row[0])


def test_the_holder_is_an_idle_transaction_that_keeps_its_row_lock(database: LabDatabase) -> None:
    start_holder(database)
    query = activity(database)

    row = wait_for_holder(query, INVOICE_BACKFILL, min_age_seconds=0)

    assert row["application"] == "invoice-backfill"
    assert row["role"] == "billing_app"
    assert row["state"] == "idle in transaction"
    assert row["locks_held"] > 0
    assert "FOR UPDATE" in row["last_query"]
    again = holder_rows(query(LONG_TRANSACTIONS_CHECK), INVOICE_BACKFILL)
    assert [item["pid"] for item in again] == [row["pid"]]
    with psycopg.connect(database.url("billing_app"), autocommit=True) as connection:
        status = connection.execute(
            "SELECT status FROM billing.invoices WHERE id = %s", (INVOICE,)
        ).fetchone()
        assert status == ("open",)
        with pytest.raises(psycopg.errors.LockNotAvailable):
            connection.execute(
                "SELECT id FROM billing.invoices WHERE id = %s FOR UPDATE NOWAIT", (INVOICE,)
            )


def test_a_real_payment_wait_is_captured_with_the_exact_match(
    start_api: ApiStarter, database: LabDatabase
) -> None:
    api = start_api(database.url("billing_app"))
    start_holder(database)
    query = activity(database)
    holder_pid = int(wait_for_holder(query, INVOICE_BACKFILL, min_age_seconds=0)["pid"])
    captures: list[LockWaitCapture] = []

    sent = send_requests(
        api.url,
        [PAY],
        watched_request=PAY.request_id,
        watcher=lambda future: captures.append(
            capture_lock_wait(
                query, future, INVOICE_BACKFILL, holder_pid, request_id=PAY.request_id
            )
        ),
    )

    assert (sent[0].status, sent[0].problem_code, sent[0].retry_after) == (
        503,
        "DATABASE_BUSY",
        "5",
    )
    assert sent[0].as_expected
    [capture] = captures
    assert (capture.request_id, capture.holder_pid) == (PAY.request_id, holder_pid)
    [row] = capture.rows
    assert row["blocked_application"] == API_APPLICATION_NAME == "billing-api"
    assert row["blocked_role"] == "billing_app"
    assert row["blocking_pid"] == holder_pid
    assert row["blocking_application"] == "invoice-backfill"
    assert row["blocking_state"] == "idle in transaction"
    assert row["waiting_for"].startswith("Lock:")
    assert "FOR UPDATE" in row["blocked_query"]
    assert query(BLOCKING_CHECK) == []
    assert [
        item["pid"] for item in holder_rows(query(LONG_TRANSACTIONS_CHECK), INVOICE_BACKFILL)
    ] == [holder_pid]
    assert succeeded_payments(database) == 0
    events = [entry.get("event_name") for entry in api.logs()]
    assert "db.lock_timeout" in events


def test_no_capture_is_recorded_when_nothing_waits(
    start_api: ApiStarter, database: LabDatabase
) -> None:
    api = start_api(database.url("billing_app"))
    query = activity(database)
    captures: list[LockWaitCapture] = []

    with pytest.raises(LockWaitNotObserved, match="nothing was recorded"):
        send_requests(
            api.url,
            [PAY],
            watched_request=PAY.request_id,
            watcher=lambda future: captures.append(
                capture_lock_wait(query, future, INVOICE_BACKFILL, 0, request_id=PAY.request_id)
            ),
        )

    assert captures == []
    assert succeeded_payments(database) == 1
