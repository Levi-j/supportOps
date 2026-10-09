import time
from collections.abc import Callable
from concurrent.futures import Future, wait
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, LiteralString

from pydantic import SecretStr

from supportops.db.catalog import CHECKS
from supportops.db.runner import PlannedCheck, run_checks
from supportops.errors import ExitCode, SupportOpsError
from supportops.investigation.live import BLOCKING_CHECK, LONG_TRANSACTIONS_CHECK
from supportops_lab.state import LockWaitCapture, SentRequest

API_APPLICATION_NAME = "billing-api"
API_ROLE = "billing_app"
IDLE_IN_TRANSACTION = "idle in transaction"
HOLDER_POLL_SECONDS = 0.5
HOLDER_APPEAR_SECONDS = 10.0
HOLDER_DEADLINE_SECONDS = 30.0
WAIT_POLL_SECONDS = 0.1
WAIT_DEADLINE_SECONDS = 20.0

Row = dict[str, Any]
ActivityQuery = Callable[[str], list[Row]]
Clock = Callable[[], float]


class ContentionError(SupportOpsError):
    exit_code = ExitCode.PROBLEM


class LockWaitNotObserved(ContentionError):
    pass


@dataclass(frozen=True)
class LockHolder:
    application_name: str
    role: str
    invoice_id: str
    watched_request: str
    sql: LiteralString


def activity_query(dsn: SecretStr, connect_timeout_seconds: float) -> ActivityQuery:
    def query(name: str) -> list[Row]:
        parameters: dict[str, Any] = {"min_seconds": 0} if name == LONG_TRANSACTIONS_CHECK else {}
        report = run_checks(
            dsn,
            [PlannedCheck(CHECKS[name], parameters)],
            connect_timeout_seconds=connect_timeout_seconds,
        )
        result = report.results[0]
        if result.status == "error" or not result.complete:
            raise ContentionError(
                f"{name} couldn't see the scenario database's sessions: {result.summary}"
            )
        return result.rows

    return query


def holder_rows(rows: list[Row], holder: LockHolder) -> list[Row]:
    return [
        row
        for row in rows
        if row.get("application") == holder.application_name
        and row.get("role") == holder.role
        and row.get("state") == IDLE_IN_TRANSACTION
        and int(row.get("locks_held") or 0) > 0
        and "FOR UPDATE" in str(row.get("last_query") or "")
    ]


def wait_for_holder(
    query: ActivityQuery,
    holder: LockHolder,
    *,
    min_age_seconds: int,
    appear_seconds: float = HOLDER_APPEAR_SECONDS,
    deadline_seconds: float = HOLDER_DEADLINE_SECONDS,
    poll_seconds: float = HOLDER_POLL_SECONDS,
    clock: Clock = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> Row:
    started = clock()
    pid: object = None
    while True:
        matches = holder_rows(query(LONG_TRANSACTIONS_CHECK), holder)
        if len(matches) > 1:
            raise ContentionError(
                f"{len(matches)} sessions named {holder.application_name} hold locks, so the "
                "scenario can't tell which one it started. No customer request was sent.",
                hint="Run 'supportops-lab reset'.",
            )
        if matches:
            row = matches[0]
            if pid is not None and row.get("pid") != pid:
                raise ContentionError(
                    f"The {holder.application_name} session changed while the scenario was "
                    "waiting for it. No customer request was sent.",
                    hint="Run 'supportops-lab reset'.",
                )
            pid = row.get("pid")
            if int(row.get("transaction_seconds") or 0) > min_age_seconds:
                return row
        elapsed = clock() - started
        if pid is None and elapsed >= appear_seconds:
            raise ContentionError(
                f"The {holder.application_name} session didn't appear as '{IDLE_IN_TRANSACTION}' "
                f"holding locks within {appear_seconds:.0f} seconds. No customer request was sent.",
                hint="Run 'supportops-lab reset' and try again.",
            )
        if elapsed >= deadline_seconds:
            raise ContentionError(
                f"The {holder.application_name} transaction didn't reach {min_age_seconds} "
                f"seconds within {deadline_seconds:.0f} seconds. No customer request was sent.",
                hint="Run 'supportops-lab reset' and try again.",
            )
        sleep(poll_seconds)


def matching_waits(rows: list[Row], holder: LockHolder, holder_pid: int) -> list[Row]:
    return [
        row
        for row in rows
        if row.get("blocking_pid") == holder_pid
        and row.get("blocking_application") == holder.application_name
        and row.get("blocking_state") == IDLE_IN_TRANSACTION
        and row.get("blocked_application") == API_APPLICATION_NAME
        and row.get("blocked_role") == API_ROLE
        and str(row.get("waiting_for") or "").startswith("Lock:")
        and "FOR UPDATE" in str(row.get("blocked_query") or "")
    ]


def capture_lock_wait(
    query: ActivityQuery,
    request: Future[SentRequest],
    holder: LockHolder,
    holder_pid: int,
    *,
    request_id: str,
    deadline_seconds: float = WAIT_DEADLINE_SECONDS,
    poll_seconds: float = WAIT_POLL_SECONDS,
    clock: Clock = time.monotonic,
) -> LockWaitCapture:
    started = clock()
    while True:
        rows = matching_waits(query(BLOCKING_CHECK), holder, holder_pid)
        if rows:
            return LockWaitCapture(
                request_id=request_id,
                holder_pid=holder_pid,
                captured_at=datetime.now(UTC),
                rows=rows,
            )
        if request.done() or clock() - started >= deadline_seconds:
            raise LockWaitNotObserved(
                f"No API session was seen waiting for the {holder.application_name} session's "
                f"lock while {request_id} was in flight, so the lock wait wasn't observed and "
                "nothing was recorded.",
                hint="Run 'supportops-lab start' again; if it keeps happening, check "
                "'supportops-lab status'.",
            )
        wait([request], timeout=poll_seconds)
