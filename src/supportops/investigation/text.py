import json
from datetime import datetime
from typing import Any

from supportops.db.runner import CheckResult
from supportops.health import HealthReport
from supportops.investigation.models import (
    CoverageGap,
    Evidence,
    LiveEvidence,
    SignatureImpact,
)
from supportops.logs.analysis import is_access_log
from supportops.logs.parser import LogEvent
from supportops.redaction import redact_value

MAX_DETAIL_CHARACTERS = 200
MAX_SESSIONS = 5
_HIDDEN_FIELDS = frozenset({"user_agent", "service", "version", "color_message", "process", "ecs"})
_SCOPE_TEXT = {
    "isolated": "no other request in the window shows this error.",
    "recurring": "other requests show this error, from at most one known account.",
    "widespread": "requests from two or more accounts show this error.",
    "undetermined": "no other request shows this error in the logs that were read, but "
    "coverage is incomplete, so it can't be called isolated.",
}
_EVIDENCE_ORIGIN = {
    "log": "log entry",
    "database": "database check",
    "api": "API health check",
}


def format_time(moment: datetime | None) -> str:
    if moment is None:
        return "-"
    return moment.strftime("%Y-%m-%dT%H:%M:%S.") + f"{moment.microsecond // 1000:03d}Z"


def describe_request(event: LogEvent) -> str:
    request = " ".join(part for part in (event.method, event.path) if part) or "the request"
    return f"{request} -> {event.status}" if event.status is not None else request


def describe_event(event: LogEvent) -> str:
    name = event.event_name or event.logger or "log entry"
    if is_access_log(event):
        text = describe_request(event)
        if event.duration_ms is not None:
            text += f" ({event.duration_ms:,.0f} ms)"
    else:
        text = event.message or ""
        error = ": ".join(part for part in (event.error_type, event.error_message) if part)
        if error:
            text = f"{text} - {error}" if text else error
    details = event_details(event)
    return f"{event.level or '-'} {name}: {text}" + (f" [{details}]" if details else "")


def event_details(event: LogEvent) -> str:
    fields: dict[str, Any] = {
        key: value for key, value in event.extra.items() if key not in _HIDDEN_FIELDS
    }
    if event.account_id:
        fields = {"account_id": event.account_id, **fields}
    redacted: dict[str, Any] = redact_value(fields)
    return " ".join(
        f"{key}={_compact(value)}" for key, value in redacted.items() if value is not None
    )


def describe_check(
    result: CheckResult, invoice_id: str | None, *, order_id: str | None = None
) -> str:
    if result.status == "error":
        return f"{result.name} couldn't run: {result.summary}"
    if result.name == "orderflow.order_lookup" and result.rows:
        row = result.rows[0]
        return (
            f"Order {row.get('order_id')} (customer {row.get('customer_id')}): status "
            f"{row.get('status')}, total {row.get('total_amount')}, items total "
            f"{row.get('items_total')} across {row.get('item_count')} items, units ordered "
            f"{row.get('units_ordered')}, reserved {row.get('units_reserved')}, restored "
            f"{row.get('units_restored')}."
        )
    if result.pack == "orderflow" and result.rows and "order_id" in result.columns:
        includes = any(str(row.get("order_id")) == order_id for row in result.rows)
        text = result.summary
        if order_id is not None:
            text += f" It {'lists' if includes else 'does not list'} order {order_id}."
        if result.truncated:
            text += " Only the first rows were returned."
        return text
    if result.name == "billing.api_key_status" and result.rows:
        row = result.rows[0]
        return (
            f"Key {row.get('key_prefix')} is {row.get('key_status')}; account "
            f"{row.get('account_id')} is {row.get('account_status')}; created "
            f"{_day(row.get('created_at'))}, expires {_day(row.get('expires_at'))}, revoked "
            f"{_day(row.get('revoked_at'))}."
        )
    if result.name == "billing.invoice_lookup":
        rows = [row for row in result.rows if row.get("invoice_id") == invoice_id]
        if not rows:
            return result.summary
        row = rows[0]
        return (
            f"Invoice {row.get('invoice_id')} ({row.get('number')}, account "
            f"{row.get('account_id')}): status {row.get('status')}, total "
            f"{row.get('total_cents')} {row.get('currency')} cents, sum of lines "
            f"{row.get('line_total_cents')}, successful payments {row.get('succeeded_payments')}, "
            f"failed payments {row.get('failed_payments')}."
        )
    if result.pack == "billing" and result.rows and "invoice_id" in result.columns:
        listed = [row for row in result.rows if row.get("invoice_id") == invoice_id]
        text = result.summary
        if invoice_id is not None:
            text += f" It {'lists' if listed else 'does not list'} {invoice_id}."
        if result.truncated:
            text += " Only the first rows were returned."
        if listed and listed[0].get("payment_ids"):
            text += f" Payments: {', '.join(str(item) for item in listed[0]['payment_ids'])}."
        return text
    if result.name in ("pg.long_transactions", "pg.blocking_sessions") and result.rows:
        sessions = "; ".join(describe_session(row) for row in result.rows[:MAX_SESSIONS])
        return f"{result.summary} {sessions}."
    return result.summary


def describe_session(row: dict[str, Any]) -> str:
    application = row.get("application") or row.get("blocking_application") or "(none)"
    state = row.get("state") or row.get("blocking_state") or "unknown state"
    seconds = row.get("transaction_seconds") or row.get("blocking_transaction_seconds")
    pid = row.get("pid") or row.get("blocking_pid")
    text = f"session {pid} ({application}, {state}"
    if seconds is not None:
        text += f", transaction open {seconds} s"
    if row.get("locks_held") is not None:
        text += f", {row['locks_held']} locks held"
    return text + ")"


def describe_health(report: HealthReport) -> str:
    text = f"{report.verdict} ({report.diagnosis}): {report.summary}"
    if report.readiness is not None and report.readiness.status is not None:
        text += f" Readiness answered {report.readiness.status}."
    if report.api_database is not None and report.api_database.status:
        text += f" The API reports its database as {report.api_database.status}"
        text += f" ({report.api_database.error})." if report.api_database.error else "."
    if report.database is not None:
        reach = (
            "reachable" if report.database.reachable else f"not reachable ({report.database.error})"
        )
        text += f" PostgreSQL from this machine: {reach}."
    return text


def describe_origin(item: Evidence) -> str:
    return f"{_EVIDENCE_ORIGIN[item.source]} at {format_time(item.observed_at)}"


def describe_database_status(live: LiveEvidence) -> str:
    return {
        "checked": f"checked as {live.database_target} at {format_time(live.collected_at)}",
        "not_needed": "not needed for this request",
        "disabled": "skipped (--no-db)",
        "not_configured": "not checked (SUPPORTOPS_DB_URL is not set)",
        "unavailable": f"unavailable ({live.database_detail})",
    }[live.database]


def describe_health_status(live: LiveEvidence) -> str:
    if live.health is None:
        return "not checked"
    return f"checked at {format_time(live.collected_at)}"


def current_state_notes(live: LiveEvidence) -> list[str]:
    when = format_time(live.collected_at)
    notes = []
    if live.database == "checked":
        notes.append(
            f"Database checks ran at {when}. They show the database at that time, not when the "
            "request was made."
        )
    if live.health is not None:
        notes.append(
            f"The API health check ran at {when}. It shows the service at that time, not when "
            "the request was made."
        )
    return notes


def describe_window(items: list[SignatureImpact]) -> str:
    windows = {(item.window_start, item.window_end) for item in items}
    if len(windows) != 1:
        return "around the request"
    start, end = windows.pop()
    if start is None or end is None:
        return "anywhere in the logs that were read (the request has no timestamp)"
    return f"from {format_time(start)} to {format_time(end)}"


def describe_impact(item: SignatureImpact) -> str:
    requests = f"{item.requests} {_plural(item.requests, 'request')}"
    others = item.other_request_ids
    if not item.other_requests:
        requests += " (this one only)"
    elif item.other_requests == 1 and others:
        requests += f" (this one and {others[0]})"
    elif item.other_requests <= len(others):
        requests += f" (this one and {item.other_requests} others: {', '.join(others)})"
    else:
        requests += f" (this one and {item.other_requests} others, including {', '.join(others)})"
    if item.accounts:
        more = ", ..." if item.accounts > len(item.account_ids) else ""
        accounts = (
            f"{item.accounts} {_plural(item.accounts, 'account')} "
            f"({', '.join(item.account_ids)}{more})"
        )
    else:
        accounts = "no identified account"
    entries = f"{item.events} log {_plural(item.events, 'entry', 'entries')}"
    prefix = "Lower bounds" if item.lower_bound else "Counts"
    return f"{prefix}: {requests}, {accounts}, {entries}."


def scope_meaning(scope: str) -> str:
    text = _SCOPE_TEXT[scope]
    return text[:1].upper() + text[1:]


def describe_scope(item: SignatureImpact) -> str:
    return f"Scope: {item.scope} - {_SCOPE_TEXT[item.scope]}"


def shared_gaps(items: list[SignatureImpact]) -> list[CoverageGap]:
    if not items:
        return []
    return [gap for gap in items[0].gaps if all(gap in item.gaps for item in items[1:])]


def describe_coverage(items: list[SignatureImpact]) -> str:
    if all(item.coverage == "complete" for item in items):
        return "complete for the logs that were read."
    if shared_gaps(items):
        scope = "every signature above" if len(items) > 1 else "this signature"
        return f"partial for {scope}, so the counts are lower bounds:"
    return "partial where noted above, so those counts are lower bounds."


def _plural(count: int, singular: str, plural: str | None = None) -> str:
    return singular if count == 1 else plural or f"{singular}s"


def _compact(value: Any) -> str:
    if isinstance(value, list):
        text = ",".join(str(item) for item in value)
    elif isinstance(value, dict):
        text = json.dumps(value, separators=(",", ":"), default=str)
    else:
        text = str(value)
    if len(text) > MAX_DETAIL_CHARACTERS:
        text = text[:MAX_DETAIL_CHARACTERS] + "..."
    return text


def _day(value: Any) -> str:
    if value is None:
        return "never"
    if isinstance(value, datetime):
        return value.strftime("%Y-%m-%d")
    return str(value)[:10]
