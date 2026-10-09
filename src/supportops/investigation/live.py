from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

import httpx

from supportops.db.catalog import CHECKS
from supportops.db.connection import DatabaseError
from supportops.db.runner import MAX_ROWS, PlannedCheck, run_checks
from supportops.health import run_health_check
from supportops.http_checks import WRITE_METHODS
from supportops.investigation.models import (
    Entities,
    InvoiceState,
    LiveEvidence,
    LogEvidence,
    OtherInvoices,
)
from supportops.logs.analysis import event_kind
from supportops.settings import Settings
from supportops.targets import TargetProfile

KEY_CHECK = "billing.api_key_status"
INVOICE_CHECK = "billing.invoice_lookup"
CONSISTENCY_CHECKS = (
    "billing.invoice_total_mismatch",
    "billing.paid_invoice_without_payment",
    "billing.payment_on_unpaid_invoice",
    "billing.duplicate_payments",
)
CONDITION_TEXT = {
    "billing.invoice_total_mismatch": "its stored total differs from the sum of its lines",
    "billing.paid_invoice_without_payment": "it is marked paid but has no successful payment",
    "billing.payment_on_unpaid_invoice": "it has a successful payment but isn't marked paid",
    "billing.duplicate_payments": "it has more than one successful payment",
}
LONG_TRANSACTIONS_CHECK = "pg.long_transactions"
BLOCKING_CHECK = "pg.blocking_sessions"
LONG_TRANSACTION_SECONDS = 10
LOCK_EVENTS = frozenset({"db.lock_timeout", "db.statement_timeout"})
DATABASE_DOWN_EVENT = "db.unavailable"
PAYMENT_EVENTS = frozenset({"payment.recorded", "payment.rejected", "invoice.paid"})
MAX_EXAMPLES = 5


def collect_live(
    logs: LogEvidence,
    settings: Settings,
    profile: TargetProfile,
    client: httpx.Client,
    *,
    use_database: bool,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> LiveEvidence:
    live = LiveEvidence(collected_at=now(), database="not_needed")
    plan = plan_checks_for(logs)
    if plan:
        _run_database_checks(live, plan, settings, use_database=use_database)
    invoice_id = logs.entities.invoice_id
    if invoice_id is not None:
        live.invoice = (
            invoice_state(invoice_id, logs.entities, live)
            if live.database == "checked"
            else InvoiceState(invoice_id=invoice_id, lookup="unavailable")
        )
    if needs_health_check(logs):
        health_settings = settings if use_database else settings.model_copy(update={"db_url": None})
        live.health = run_health_check(health_settings, profile, client)
    return live


def event_names(logs: LogEvidence) -> set[str]:
    return {step.event.event_name for step in logs.trace.steps if step.event.event_name}


def touches_payments(logs: LogEvidence) -> bool:
    request = logs.request
    if request is not None and (
        (request.method or "").upper() in WRITE_METHODS
        or (request.status is not None and request.status >= 500)
    ):
        return True
    if event_names(logs) & PAYMENT_EVENTS:
        return True
    return any(event_kind(step.event) == "exception" for step in logs.trace.steps)


def needs_health_check(logs: LogEvidence) -> bool:
    names = event_names(logs)
    if DATABASE_DOWN_EVENT in names:
        return True
    status = logs.request.status if logs.request is not None else None
    return status == 503 and not names & LOCK_EVENTS


def plan_checks_for(logs: LogEvidence) -> list[PlannedCheck]:
    entities = logs.entities
    plan = []
    if entities.key_prefix is not None:
        plan.append(PlannedCheck(CHECKS[KEY_CHECK], {"prefix": entities.key_prefix}))
    if entities.invoice_id is not None:
        plan.append(
            PlannedCheck(CHECKS[INVOICE_CHECK], {"id": entities.invoice_id, "number": None})
        )
        if touches_payments(logs):
            plan.extend(PlannedCheck(CHECKS[name]) for name in CONSISTENCY_CHECKS)
    if event_names(logs) & LOCK_EVENTS:
        plan.append(
            PlannedCheck(CHECKS[LONG_TRANSACTIONS_CHECK], {"min_seconds": LONG_TRANSACTION_SECONDS})
        )
        plan.append(PlannedCheck(CHECKS[BLOCKING_CHECK]))
    return plan


def _run_database_checks(
    live: LiveEvidence, plan: list[PlannedCheck], settings: Settings, *, use_database: bool
) -> None:
    names = ", ".join(item.check.name for item in plan)
    if not use_database:
        live.database = "disabled"
        live.notes.append(f"Database checks were skipped (--no-db): {names}.")
        return
    if settings.db_url is None:
        live.database = "not_configured"
        live.open_questions.append(
            f"SUPPORTOPS_DB_URL is not set, so these read-only checks weren't run: {names}. "
            "Set it to cross-check the logs against the database."
        )
        return
    try:
        report = run_checks(
            settings.db_url, plan, connect_timeout_seconds=settings.connect_timeout_seconds
        )
    except DatabaseError as exc:
        live.database = "unavailable"
        live.database_detail = exc.message
        live.open_questions.append(
            f"The database couldn't be checked ({exc.message}), so these checks didn't run: "
            f"{names}."
        )
        return
    live.database = "checked"
    live.database_target = report.target
    live.checks = report.results
    if not report.session.read_only:
        live.notes.append(
            "The database session was NOT read-only. Report this as a SupportOps problem."
        )
    for result in report.results:
        if result.status == "error":
            live.open_questions.append(f"{result.name} couldn't run: {result.summary}")
        elif not result.complete:
            live.notes.extend(f"{result.name}: {note}" for note in result.notes)


def derive_conditions(record: dict[str, Any]) -> list[str]:
    status = record.get("status")
    total = record.get("total_cents")
    line_total = record.get("line_total_cents")
    succeeded = int(record.get("succeeded_payments") or 0)
    conditions = []
    if total is not None and line_total is not None and total != line_total:
        conditions.append("billing.invoice_total_mismatch")
    if status == "paid" and succeeded == 0:
        conditions.append("billing.paid_invoice_without_payment")
    if status != "paid" and succeeded > 0:
        conditions.append("billing.payment_on_unpaid_invoice")
    if succeeded > 1:
        conditions.append("billing.duplicate_payments")
    return conditions


def invoice_state(invoice_id: str, entities: Entities, live: LiveEvidence) -> InvoiceState:
    lookup = live.check(INVOICE_CHECK)
    if lookup is None or lookup.status == "error":
        return InvoiceState(
            invoice_id=invoice_id,
            lookup="unavailable",
            notes=[f"{INVOICE_CHECK} didn't run, so nothing is known about {invoice_id}."],
        )
    rows = [row for row in lookup.rows if row.get("invoice_id") == invoice_id]
    if not rows:
        return InvoiceState(
            invoice_id=invoice_id,
            lookup="not_found",
            notes=[f"No invoice with ID {invoice_id} exists in the database now."],
        )
    record = rows[0]
    if entities.account_id is not None and record.get("account_id") != entities.account_id:
        return InvoiceState(
            invoice_id=invoice_id,
            lookup="other_account",
            record=record,
            notes=[
                f"{invoice_id} belongs to {record.get('account_id')}, not to the request's "
                f"account {entities.account_id}, so its state isn't attributed to this request."
            ],
        )
    state = InvoiceState(
        invoice_id=invoice_id, lookup="found", record=record, conditions=derive_conditions(record)
    )
    for name in CONSISTENCY_CHECKS:
        _compare_with_global_check(state, live, name)
    _compare_payment_ids(state, live, entities.payment_ids)
    return state


def _compare_with_global_check(state: InvoiceState, live: LiveEvidence, name: str) -> None:
    result = live.check(name)
    if result is None:
        return
    if result.status == "error":
        state.notes.append(f"{name} couldn't run, so it can't corroborate the invoice's record.")
        return
    listed = [row.get("invoice_id") for row in result.rows]
    included = state.invoice_id in listed
    derived = name in state.conditions
    if included and derived:
        state.corroborated_by.append(name)
    elif included:
        state.contradictions.append(
            f"{name} lists {state.invoice_id}, but the invoice's own record doesn't show that "
            "condition. The data may have changed between the two queries."
        )
    elif derived and result.truncated:
        state.notes.append(
            f"{name} returned only its first {MAX_ROWS} rows, which don't include "
            f"{state.invoice_id}; the invoice's own record decides."
        )
    elif derived:
        state.contradictions.append(
            f"{name} doesn't list {state.invoice_id}, although the invoice's own record shows "
            "that condition. The data may have changed between the two queries."
        )
    others = sorted({str(invoice) for invoice in listed if invoice and invoice != state.invoice_id})
    if others:
        state.elsewhere.append(
            OtherInvoices(
                check=name,
                count=len(others),
                at_least=result.truncated,
                examples=others[:MAX_EXAMPLES],
            )
        )


def _compare_payment_ids(state: InvoiceState, live: LiveEvidence, logged: list[str]) -> None:
    result = live.check("billing.duplicate_payments")
    if result is None or result.status == "error" or not logged:
        return
    row = next((row for row in result.rows if row.get("invoice_id") == state.invoice_id), None)
    if row is None:
        return
    stored = {str(payment) for payment in row.get("payment_ids") or []}
    missing = [payment for payment in logged if payment not in stored]
    if missing:
        state.contradictions.append(
            f"The request's logs record payment {', '.join(missing)}, which isn't among the "
            f"successful payments the database lists for {state.invoice_id}."
        )
