from concurrent.futures import Future
from typing import Any

import pytest

from billing_api.database import APPLICATION_NAME
from supportops.errors import ExitCode
from supportops.investigation.live import BLOCKING_CHECK, LONG_TRANSACTIONS_CHECK
from supportops_lab.contention import (
    API_APPLICATION_NAME,
    ContentionError,
    LockWaitNotObserved,
    capture_lock_wait,
    holder_rows,
    matching_waits,
    wait_for_holder,
)
from supportops_lab.scenarios import INVOICE_BACKFILL
from supportops_lab.state import SentRequest
from tests.unit.lab.support import holder_row, wait_row

HOLDER = INVOICE_BACKFILL


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds


class Replies:
    def __init__(self, *replies: list[dict[str, Any]]) -> None:
        self.replies = list(replies)
        self.names: list[str] = []

    def __call__(self, name: str) -> list[dict[str, Any]]:
        self.names.append(name)
        return self.replies.pop(0) if len(self.replies) > 1 else self.replies[0]


def test_the_api_application_name_matches_the_billing_api() -> None:
    assert API_APPLICATION_NAME == APPLICATION_NAME


@pytest.mark.parametrize(
    "override",
    [
        {"application": "billing-api"},
        {"role": "lab_admin"},
        {"state": "active"},
        {"locks_held": 0},
        {"last_query": "SELECT 1"},
    ],
    ids=["application", "role", "state", "no-locks", "no-for-update"],
)
def test_only_the_exact_holder_matches(override: dict[str, Any]) -> None:
    assert holder_rows([holder_row()], HOLDER)
    assert holder_rows([holder_row(**override)], HOLDER) == []


def test_waiting_for_the_holder_returns_once_it_is_old_enough() -> None:
    clock = FakeClock()
    query = Replies(
        [],
        [holder_row(transaction_seconds=3)],
        [holder_row(transaction_seconds=10)],
        [holder_row(transaction_seconds=11)],
    )

    row = wait_for_holder(query, HOLDER, min_age_seconds=10, clock=clock, sleep=clock.sleep)

    assert row["transaction_seconds"] == 11
    assert set(query.names) == {LONG_TRANSACTIONS_CHECK}
    assert clock.now == pytest.approx(1.5)


def test_a_holder_that_never_appears_fails_without_waiting_for_its_age() -> None:
    clock = FakeClock()

    with pytest.raises(ContentionError, match="didn't appear") as excinfo:
        wait_for_holder(Replies([]), HOLDER, min_age_seconds=10, clock=clock, sleep=clock.sleep)

    assert excinfo.value.exit_code == ExitCode.PROBLEM
    assert clock.now == pytest.approx(10.0)


def test_a_holder_that_never_ages_fails_at_the_deadline() -> None:
    clock = FakeClock()

    with pytest.raises(ContentionError, match="didn't reach 10 seconds"):
        wait_for_holder(
            Replies([holder_row(transaction_seconds=1)]),
            HOLDER,
            min_age_seconds=10,
            clock=clock,
            sleep=clock.sleep,
        )

    assert clock.now == pytest.approx(30.0)


def test_two_holders_are_ambiguous() -> None:
    with pytest.raises(ContentionError, match="2 sessions named invoice-backfill"):
        wait_for_holder(Replies([holder_row(), holder_row(pid=78)]), HOLDER, min_age_seconds=10)


def test_a_holder_that_changes_pid_is_refused() -> None:
    clock = FakeClock()
    query = Replies([holder_row(transaction_seconds=1)], [holder_row(pid=99)])

    with pytest.raises(ContentionError, match="changed"):
        wait_for_holder(query, HOLDER, min_age_seconds=10, clock=clock, sleep=clock.sleep)


@pytest.mark.parametrize(
    "override",
    [
        {"blocking_pid": 78},
        {"blocking_application": "psql"},
        {"blocking_state": "active"},
        {"blocked_application": "supportops"},
        {"blocked_role": "lab_admin"},
        {"waiting_for": "Client:ClientRead"},
        {"blocked_query": "UPDATE billing.invoices SET status = 'paid'"},
    ],
)
def test_only_the_exact_wait_matches(override: dict[str, Any]) -> None:
    assert matching_waits([wait_row()], HOLDER, 77)
    assert matching_waits([wait_row(**override)], HOLDER, 77) == []


def pending() -> Future[SentRequest]:
    return Future()


def finished() -> Future[SentRequest]:
    future: Future[SentRequest] = Future()
    future.set_result(
        SentRequest(request_id="r", method="POST", path="/x", expected_status=503, status=503)
    )
    return future


def test_a_matching_wait_is_captured() -> None:
    query = Replies([], [wait_row(), wait_row(blocking_pid=1)])

    capture = capture_lock_wait(
        query, pending(), HOLDER, 77, request_id="inc005-cust-01", poll_seconds=0
    )

    assert (capture.request_id, capture.holder_pid, capture.check) == (
        "inc005-cust-01",
        77,
        BLOCKING_CHECK,
    )
    assert capture.rows == [wait_row()]
    assert set(query.names) == {BLOCKING_CHECK}


def test_a_request_that_finishes_first_records_nothing() -> None:
    with pytest.raises(LockWaitNotObserved, match="nothing was recorded") as excinfo:
        capture_lock_wait(Replies([]), finished(), HOLDER, 77, request_id="inc005-cust-01")

    assert excinfo.value.exit_code == ExitCode.PROBLEM


def test_a_wait_that_never_appears_stops_at_the_deadline() -> None:
    clock = FakeClock()
    query = Replies([])

    def counting(name: str) -> list[dict[str, Any]]:
        clock.now += 1
        return query(name)

    with pytest.raises(LockWaitNotObserved):
        capture_lock_wait(
            counting,
            pending(),
            HOLDER,
            77,
            request_id="inc005-cust-01",
            deadline_seconds=5,
            poll_seconds=0,
            clock=clock,
        )

    assert clock.now == 5
