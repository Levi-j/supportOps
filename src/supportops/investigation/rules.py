from collections.abc import Callable, Collection
from dataclasses import dataclass, field

from supportops.db.runner import CheckResult
from supportops.http_checks import WRITE_METHODS
from supportops.investigation.live import (
    BLOCKING_CHECK,
    CONDITION_TEXT,
    DATABASE_DOWN_EVENT,
    INVOICE_CHECK,
    KEY_CHECK,
    LOCK_EVENTS,
    LONG_TRANSACTIONS_CHECK,
)
from supportops.investigation.models import (
    Confidence,
    Escalation,
    Finding,
    LiveEvidence,
    LogEvidence,
    Severity,
)
from supportops.investigation.text import describe_request, describe_session
from supportops.logs.analysis import event_kind, is_access_log, is_warning_or_error
from supportops.logs.parser import LogEvent

START_KEY = "start"
HEALTH_KEY = "health"
LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})
SIDE_EFFECT_EVENTS = ("customer.created", "invoice.created", "payment.recorded", "invoice.paid")
DO_NOT_RETRY = (
    "Ask the customer not to retry until it has been confirmed whether the first attempt took "
    "effect."
)
CURRENT_STATE_CAVEAT = (
    "Database results show the state when the investigation ran, not at the time of the request."
)

Entry = tuple[str, LogEvent]


def log_key(index: int) -> str:
    return f"log:{index}"


def check_key(name: str) -> str:
    return f"check:{name}"


@dataclass(frozen=True)
class Facts:
    logs: LogEvidence
    live: LiveEvidence
    slow_request_ms: float = 1000.0

    def entries(self, *names: str) -> list[Entry]:
        return [
            (log_key(index), step.event)
            for index, step in enumerate(self.logs.trace.steps)
            if step.event.event_name in names
        ]

    def matching(self, predicate: Callable[[LogEvent], bool]) -> list[Entry]:
        return [
            (log_key(index), step.event)
            for index, step in enumerate(self.logs.trace.steps)
            if predicate(step.event)
        ]

    def access(self) -> list[Entry]:
        return self.matching(is_access_log)

    def checked(self, name: str) -> CheckResult | None:
        result = self.live.check(name)
        return result if result is not None and result.status != "error" else None

    @property
    def method(self) -> str:
        return (self.logs.request.method or "").upper() if self.logs.request else ""


@dataclass
class Assessment:
    confidence: Confidence
    cites: list[str]
    contradictions: list[str] = field(default_factory=list)
    caveats: list[str] = field(default_factory=list)
    inferences: list[str] = field(default_factory=list)

    def cite(self, key: str) -> None:
        if key not in self.cites:
            self.cites.append(key)

    def cap(self, ceiling: Confidence) -> None:
        order = list(Confidence)
        if order.index(self.confidence) < order.index(ceiling):
            self.confidence = ceiling


def assess(facts: Facts, direct: str, expected: Collection[int]) -> Assessment:
    assessment = Assessment(Confidence.CONFIRMED, [direct])
    access = facts.access()
    if not access:
        assessment.cap(Confidence.LIKELY)
        assessment.caveats.append(
            "No access-log entry was found for this request, so its final HTTP status isn't "
            "known from the logs."
        )
        return assessment
    key, event = access[-1]
    assessment.cite(key)
    if event.status not in expected:
        wanted = " or ".join(str(status) for status in sorted(expected))
        assessment.contradictions.append(
            f"The access log shows HTTP {event.status}, not the {wanted} this cause produces."
        )
    if len(access) > 1:
        assessment.cap(Confidence.LIKELY)
        assessment.caveats.append(
            f"{len(access)} access-log entries carry this request ID, so the timeline may mix "
            "several requests."
        )
    return assessment


def finding(
    rule: str,
    title: str,
    summary: str,
    assessment: Assessment,
    *,
    next_steps: list[str] | None = None,
    escalation: Escalation | None = None,
) -> Finding:
    confidence = assessment.confidence
    if assessment.contradictions:
        confidence = confidence.downgraded()
    return Finding(
        rule=rule,
        title=title,
        confidence=confidence,
        summary=summary,
        evidence_ids=list(assessment.cites),
        inferences=assessment.inferences,
        contradictions=assessment.contradictions,
        caveats=assessment.caveats,
        next_steps=next_steps or [],
        escalation=escalation,
    )


_AUTH_REASONS = {
    "revoked_key": ("key_revoked", "The API key was revoked"),
    "expired_key": ("key_expired", "The API key has expired"),
    "unknown_key": ("key_unknown", "The API key isn't recognised"),
    "missing_header": ("auth_header_problem", "No API key was sent"),
    "malformed_header": ("auth_header_problem", "The Authorization header was malformed"),
    "account_suspended": ("account_suspended", "The account is suspended"),
}
_AUTH_INFERENCES = {
    "revoked_key": "The key was probably rotated, and this integration still uses the old one.",
    "expired_key": "The key reached its expiry date; the integration needs a current key.",
    "unknown_key": "The key may be mistyped, cut off, or belong to a different environment.",
    "missing_header": "The client sent no Authorization header, often because the variable "
    "holding the key was empty or the header name was wrong.",
    "malformed_header": "The header may use the wrong scheme (it must be 'Bearer <key>'), or "
    "the key may contain quotes, spaces or extra characters.",
    "account_suspended": "Access is blocked by the account's status; this is an account "
    "decision, not a technical fault.",
}
_KEY_STATES = {"revoked_key": "revoked", "expired_key": "expired"}


def auth_rejection(facts: Facts) -> Finding | None:
    entries = facts.entries("auth.rejected")
    if not entries:
        return None
    key, event = entries[-1]
    reason = _text(event.extra.get("reason")) or "unknown"
    prefix = _text(event.extra.get("key_prefix"))
    rule, title = _AUTH_REASONS.get(reason, ("auth_rejected", "The API rejected the credentials"))
    assessment = assess(facts, key, {403} if reason == "account_suspended" else {401})
    if reason not in _AUTH_REASONS:
        assessment.cap(Confidence.LIKELY)
    summary = f"The API logged auth.rejected with reason {reason}"
    if prefix:
        summary += f" for key prefix {prefix}"
    if event.account_id:
        summary += f" (account {event.account_id})"
    summary += "."
    if reason in _AUTH_INFERENCES:
        assessment.inferences.append(_AUTH_INFERENCES[reason])
    if prefix:
        _cross_check_key(facts, assessment, reason, prefix)
    steps = _auth_next_steps(reason, prefix)
    escalation = (
        Escalation(
            team="Account management",
            severity="low",
            reason="Restoring access to a suspended account is an account decision.",
        )
        if reason == "account_suspended"
        else None
    )
    return finding(rule, title, summary, assessment, next_steps=steps, escalation=escalation)


def _cross_check_key(facts: Facts, assessment: Assessment, reason: str, prefix: str) -> None:
    result = facts.checked(KEY_CHECK)
    if result is None:
        assessment.caveats.append(
            f"The database record of key {prefix} wasn't checked ({_database_state(facts)})."
        )
        return
    assessment.cite(check_key(KEY_CHECK))
    row = result.rows[0] if result.rows else None
    if row is None:
        if reason != "unknown_key":
            assessment.contradictions.append(
                f"No API key with prefix {prefix} exists in the database now."
            )
        return
    assessment.caveats.append(CURRENT_STATE_CAVEAT)
    status = row.get("key_status")
    if reason in _KEY_STATES and status != _KEY_STATES[reason]:
        assessment.contradictions.append(
            f"The database now shows key {prefix} as {status}. It may have changed after the "
            "request, or the log refers to a different key."
        )
    if reason == "account_suspended" and row.get("account_status") == "active":
        assessment.contradictions.append(
            f"The database now shows account {row.get('account_id')} as active."
        )
    if reason == "unknown_key":
        assessment.inferences.append(
            f"A key with prefix {prefix} does exist ({status}, account {row.get('account_id')}), "
            "so the rest of the key the client sent doesn't match it."
        )


def _auth_next_steps(reason: str, prefix: str | None) -> list[str]:
    lookup = (
        [f"Re-check the key's record: supportops db run {KEY_CHECK} --param prefix={prefix}"]
        if prefix
        else []
    )
    if reason in ("revoked_key", "expired_key"):
        return [
            "Ask the customer to replace the key in their integration with a current one.",
            *lookup,
        ]
    if reason == "unknown_key":
        return [
            "Ask the customer to copy the key again from where it was issued, without quotes "
            "or spaces.",
            "Check a key's format without exposing it: supportops auth check --key-env VARIABLE",
            *lookup,
        ]
    if reason in ("missing_header", "malformed_header"):
        return [
            "Ask the customer how their client sets the header (without the key itself); it "
            "must be 'Authorization: Bearer <key>'.",
            "Check a key's format without exposing it: supportops auth check --key-env VARIABLE",
        ]
    if reason == "account_suspended":
        return ["Route the customer to account management; support can't lift a suspension."]
    return lookup


def malformed_json(facts: Facts) -> Finding | None:
    entries = facts.entries("request.invalid_json")
    if not entries:
        return None
    key, event = entries[-1]
    assessment = assess(facts, key, {400})
    message = event.error_message or "no decoder message"
    position = event.extra.get("error_position")
    summary = f"The API couldn't parse the request body as JSON: {message}"
    if position is not None:
        summary += f" (at position {position})"
    summary += "."
    if "double quotes" in message.lower():
        assessment.inferences.append(
            "Property names without double quotes usually mean the client removed the quotes "
            "before sending. Windows PowerShell 5.1, for example, strips embedded double quotes "
            "when it passes a JSON string to a native program such as curl.exe."
        )
    else:
        assessment.inferences.append(
            "The body may be cut off, contain a trailing comma, or not be JSON at all."
        )
    return finding(
        "malformed_json",
        "The request body wasn't valid JSON",
        summary,
        assessment,
        next_steps=[
            "Ask the customer for the exact command or code that sends the request "
            "(without credentials).",
            "Suggest sending the body from a UTF-8 file instead of an inline string, for "
            "example curl.exe --data-binary @body.json.",
        ],
    )


def validation_failed(facts: Facts) -> Finding | None:
    entries = facts.entries("request.validation_failed")
    if not entries:
        return None
    key, event = entries[-1]
    assessment = assess(facts, key, {422})
    fields = _strings(event.extra.get("fields"))
    types = _strings(event.extra.get("error_types"))
    if fields and len(fields) == len(types):
        listed = ", ".join(f"{name} ({kind})" for name, kind in zip(fields, types, strict=True))
    else:
        listed = ", ".join(fields) or "fields not recorded"
    assessment.inferences.append(
        "The JSON was readable, but the request doesn't match what the API expects for these "
        "fields. The 422 response lists the same fields."
    )
    return finding(
        "validation_failed",
        "The request was rejected by validation",
        f"The API read the JSON but rejected these fields: {listed}.",
        assessment,
        next_steps=[
            "Share the field list with the customer and point them to the API reference for "
            "those fields."
        ],
    )


def invoice_not_payable(facts: Facts) -> Finding | None:
    entries = facts.entries("payment.rejected")
    if not entries:
        return None
    key, event = entries[-1]
    assessment = assess(facts, key, {409})
    invoice = _text(event.extra.get("invoice_id")) or "the invoice"
    status = _text(event.extra.get("invoice_status")) or "unknown"
    assessment.inferences.append(
        f"The invoice was {status} when the payment was attempted; the API rejects a payment "
        "before recording anything, so this request shouldn't have taken a payment."
    )
    state = facts.live.invoice
    if state is not None and state.lookup == "found" and state.record is not None:
        assessment.cite(check_key(INVOICE_CHECK))
        assessment.caveats.append(
            f"The database now shows {invoice} as {state.record.get('status')}. "
            + CURRENT_STATE_CAVEAT
        )
    return finding(
        "invoice_not_payable",
        "The invoice couldn't be paid in its current status",
        f"The API rejected a payment for {invoice} because its status was {status}.",
        assessment,
        next_steps=[
            "Tell the customer the invoice's status; only open invoices can be paid.",
            "If they believe it was paid by mistake, check its payments: supportops db run "
            f"{INVOICE_CHECK} --param id={invoice}",
        ],
    )


def payment_invoice_inconsistent(facts: Facts) -> Finding | None:
    state = facts.live.invoice
    if state is None or state.lookup != "found" or not state.conditions or state.record is None:
        return None
    record = state.record
    invoice = state.invoice_id
    recorded = [
        entry
        for entry in facts.entries("payment.recorded")
        if entry[1].extra.get("invoice_id") == invoice
    ]
    paid = [
        entry
        for entry in facts.entries("invoice.paid")
        if entry[1].extra.get("invoice_id") == invoice
    ]
    assessment = Assessment(
        Confidence.CONFIRMED if recorded and not paid else Confidence.LIKELY,
        [check_key(INVOICE_CHECK)],
    )
    for name in state.corroborated_by:
        assessment.cite(check_key(name))
    for key, _event in recorded:
        assessment.cite(key)
    assessment.contradictions.extend(state.contradictions)
    assessment.caveats.append(CURRENT_STATE_CAVEAT)
    if not recorded:
        assessment.caveats.append(
            "This request's logs don't show a payment for the invoice, so the link between the "
            "request and the inconsistency is an inference."
        )
    for other in state.elsewhere:
        amount = f"at least {other.count}" if other.at_least else str(other.count)
        if other.count == 1 and not other.at_least:
            listed, verb = f"1 other invoice ({other.examples[0]})", "it is"
        else:
            listed = f"{amount} other invoices (including {', '.join(other.examples)})"
            verb = "they are"
        assessment.caveats.append(
            f"{other.check} also lists {listed}; {verb} not attributed to this request."
        )
    conditions = state.conditions
    if "billing.payment_on_unpaid_invoice" in conditions and recorded and not paid:
        assessment.inferences.append(
            "The payment was committed, but the invoice wasn't marked as paid. The customer may "
            "have been charged while the invoice still shows as unpaid, and a retry could "
            "create another payment."
        )
    if "billing.duplicate_payments" in conditions:
        assessment.inferences.append("The customer was probably charged more than once.")
    if "billing.invoice_total_mismatch" in conditions:
        assessment.inferences.append("The amount charged may not match the invoice's lines.")
    if "billing.paid_invoice_without_payment" in conditions:
        assessment.inferences.append("The invoice shows as paid although no payment succeeded.")
    summary = (
        f"Invoice {invoice} is inconsistent in the database: "
        + "; ".join(CONDITION_TEXT[name] for name in conditions)
        + f". Current record: status {record.get('status')}; total "
        f"{record.get('total_cents')} cents; line total {record.get('line_total_cents')} cents; "
        f"{record.get('succeeded_payments')} successful and {record.get('failed_payments')} "
        "failed payments."
    )
    if recorded:
        payments = ", ".join(_text(event.extra.get("payment_id")) or "?" for _, event in recorded)
        summary += f" This request's logs record payment {payments} for the invoice" + (
            "." if paid else " and no 'invoice.paid' event."
        )
    return finding(
        "payment_invoice_inconsistent",
        f"Invoice {invoice} has inconsistent payment data",
        summary,
        assessment,
        next_steps=[
            DO_NOT_RETRY,
            "Escalate to engineering with the request ID, the invoice ID and the cited "
            "evidence; correcting billing data requires a reviewed fix.",
            f"After any fix, re-check the invoice: supportops db run {INVOICE_CHECK} "
            f"--param id={invoice}",
        ],
        escalation=Escalation(
            team="Engineering",
            severity="high",
            reason="Billing data is inconsistent; correcting it needs engineering and probably a "
            "code fix.",
        ),
    )


def unhandled_exception(facts: Facts) -> Finding | None:
    entries = facts.matching(lambda event: event_kind(event) == "exception")
    if not entries:
        return None
    key, event = entries[0]
    assessment = assess(facts, key, {500})
    error = ": ".join(part for part in (event.error_type, event.error_message) if part)
    summary = f"The API failed with an unhandled exception: {_sentence(error or 'no error detail')}"
    if event.stack_trace:
        summary += " The stack trace is included in the evidence."
    effects = [
        (entry_key, entry)
        for entry_key, entry in facts.entries(*SIDE_EFFECT_EVENTS)
        if entry.sort_key <= event.sort_key
    ]
    write = facts.method in WRITE_METHODS
    if effects:
        names = ", ".join(sorted({entry.event_name or "" for _, entry in effects}))
        for entry_key, _entry in effects:
            assessment.cite(entry_key)
        assessment.inferences.append(
            f"The request had already logged {names} before it failed, so it may have partly "
            "succeeded. Retrying it could repeat that effect."
        )
    elif write:
        assessment.inferences.append(
            "It was a write request; check whether anything was saved before the failure."
        )
    _note_lab_faults(facts, assessment)
    steps = ["Include the request ID, its time and the stack trace in the engineering handoff."]
    if write or effects:
        steps.insert(0, DO_NOT_RETRY)
    return finding(
        "unhandled_exception",
        "The API failed with an unhandled exception",
        summary,
        assessment,
        next_steps=steps,
        escalation=Escalation(
            team="Engineering",
            severity="high" if write or effects else "medium",
            reason="An unhandled exception is a defect or an unexpected condition that only "
            "engineering can fix.",
        ),
    )


def lock_contention(facts: Facts) -> Finding | None:
    entries = facts.entries(*LOCK_EVENTS)
    if not entries:
        return None
    key, event = entries[-1]
    assessment = assess(facts, key, {503})
    if event.event_name == "db.lock_timeout":
        title = "The request timed out waiting for a database lock"
        summary = "The request waited too long for a database lock and was cancelled."
    else:
        title = "A database query hit the statement timeout"
        summary = "A database query ran longer than the statement timeout and was cancelled."
    long = facts.checked(LONG_TRANSACTIONS_CHECK)
    blocking = facts.checked(BLOCKING_CHECK)
    holders = [row for row in (long.rows if long else []) if row.get("locks_held")]
    write = facts.method in WRITE_METHODS
    severity: Severity = "medium"
    if long is None and blocking is None:
        assessment.caveats.append(
            f"Long transactions and blocking sessions weren't checked ({_database_state(facts)})."
        )
    elif holders or (blocking is not None and blocking.rows):
        severity = "high"
        for name, result in ((LONG_TRANSACTIONS_CHECK, long), (BLOCKING_CHECK, blocking)):
            if result is not None and result.rows:
                assessment.cite(check_key(name))
        assessment.inferences.append(
            "Most likely blocker: "
            + "; ".join(
                describe_session(row) for row in holders or (blocking.rows if blocking else [])
            )
            + ". This is the database's current state, linked to the request only by time."
        )
        assessment.caveats.append(CURRENT_STATE_CAVEAT)
        if holders and blocking is not None and not blocking.rows:
            assessment.caveats.append(
                f"{BLOCKING_CHECK} shows no session waiting now. A lock wait ends when the "
                "request times out, so the wait itself can't be seen after the fact."
            )
    else:
        for name, result in ((LONG_TRANSACTIONS_CHECK, long), (BLOCKING_CHECK, blocking)):
            if result is not None:
                assessment.cite(check_key(name))
        assessment.inferences.append(
            "No long transaction holds locks now, so the blocking session has probably "
            "finished. The problem may come back."
        )
    if write:
        _note_blocked_write(facts, assessment)
    return finding(
        "lock_contention",
        title,
        summary,
        assessment,
        next_steps=[
            "Check the impact below for other requests that failed the same way.",
            "Ask engineering or the DBA to identify and end the blocking session; support's "
            "read-only role can't and mustn't do that.",
            _blocked_write_step(facts)
            if write
            else "Advise the customer to retry later rather than in a tight loop.",
        ],
        escalation=Escalation(
            team="Engineering / DBA",
            severity=severity,
            reason="Database sessions holding locks can only be handled by engineering or a DBA.",
        ),
    )


def database_unavailable(facts: Facts) -> Finding | None:
    entries = facts.entries(DATABASE_DOWN_EVENT)
    if not entries:
        return None
    key, event = entries[-1]
    assessment = assess(facts, key, {503})
    error = _text(event.extra.get("error")) or "unclassified"
    detail = _text(event.extra.get("detail"))
    summary = f"The API couldn't use its database while handling the request (error: {error})"
    summary += f": {_sentence(detail)}" if detail else "."
    rule, title = "database_unavailable", "The API couldn't reach its database"
    escalation = Escalation(
        team="Engineering on-call",
        severity="medium",
        reason="Database connectivity problems need the service owners.",
    )
    health = facts.live.health
    if health is None:
        assessment.caveats.append("The API's readiness wasn't checked during the investigation.")
    else:
        assessment.cite(HEALTH_KEY)
        assessment.caveats.append(
            "The health check shows the state when the investigation ran, not at the time of "
            "the request."
        )
        if health.diagnosis == "api_cannot_reach_database":
            rule = "api_cannot_reach_database"
            title = "The API can't reach its database, but PostgreSQL is up"
            escalation = Escalation(
                team="Deployment owner / on-call",
                severity="high",
                reason="The API can't use a database that answers support, which points to the "
                "API's configuration or network path.",
            )
        elif health.diagnosis == "database_outage":
            rule, title = "database_outage", "The database appears to be down"
            escalation = Escalation(
                team="Database / infrastructure on-call",
                severity="high",
                reason="Neither the API nor support can reach PostgreSQL.",
            )
        elif health.diagnosis == "api_ready":
            assessment.inferences.append(
                "The API is ready now, so the database problem appears to have been temporary."
            )
        assessment.inferences.append(f"Health check now: {health.summary}")
    _note_loopback_database(facts, assessment, event)
    return finding(
        rule,
        title,
        summary,
        assessment,
        next_steps=[
            "Run 'supportops health' to see whether the problem is still happening.",
            "Check the API's database settings and the network path to PostgreSQL.",
        ],
        escalation=escalation,
    )


def resource_not_found(facts: Facts) -> Finding | None:
    access = facts.access()
    if not access or access[-1][1].status != 404:
        return None
    key, event = access[-1]
    assessment = Assessment(Confidence.POSSIBLE, [key])
    summary = f"The API answered 404 Not Found for {describe_request(event)}."
    assessment.inferences.append(
        "The ID may be mistyped, or the resource may belong to another account: the API answers "
        "404 for both, by design."
    )
    state = facts.live.invoice
    if state is not None and state.lookup in ("not_found", "other_account", "found"):
        assessment.cite(check_key(INVOICE_CHECK))
        assessment.caveats.append(CURRENT_STATE_CAVEAT)
        if state.lookup == "not_found":
            assessment.confidence = Confidence.LIKELY
            summary += f" No invoice with ID {state.invoice_id} exists in the database now."
        elif state.lookup == "other_account" and state.record is not None:
            assessment.confidence = Confidence.LIKELY
            summary += (
                f" Invoice {state.invoice_id} exists but belongs to account "
                f"{state.record.get('account_id')}, not to the requesting account."
            )
            assessment.caveats.append(
                "Internal only: don't tell the customer that the resource exists or which "
                "account owns it."
            )
        else:
            assessment.contradictions.append(
                f"Invoice {state.invoice_id} exists in the requesting account now; it may have "
                "been created after the request."
            )
    return finding(
        "resource_not_found",
        "The requested resource wasn't found for this account",
        summary,
        assessment,
        next_steps=["Ask the customer to confirm the ID and which API key (account) they used."],
    )


def request_succeeded(facts: Facts) -> Finding | None:
    access = facts.access()
    if not access:
        return None
    key, event = access[-1]
    if event.status is None or event.status >= 400:
        return None
    if facts.matching(lambda entry: not is_access_log(entry) and is_warning_or_error(entry)):
        return None
    assessment = Assessment(Confidence.CONFIRMED, [key])
    if len(access) > 1:
        assessment.cap(Confidence.LIKELY)
        assessment.caveats.append(
            f"{len(access)} access-log entries carry this request ID, so the timeline may mix "
            "several requests."
        )
    title = "The request succeeded"
    duration = event.duration_ms
    if duration is not None and duration >= facts.slow_request_ms:
        title = "The request succeeded, but slowly"
        assessment.caveats.append(
            f"It took {duration:,.0f} ms, more than the {facts.slow_request_ms:,.0f} ms "
            "slow-request threshold (SUPPORTOPS_SLOW_REQUEST_MS)."
        )
    assessment.inferences.append(
        "If the customer saw an error, it happened outside this service (in their client, a "
        "proxy or the network), or under a different request ID."
    )
    return finding(
        "request_succeeded",
        title,
        f"The API handled the request successfully: {describe_request(event)}"
        + (f" in {duration:,.0f} ms." if duration is not None else "."),
        assessment,
        next_steps=[
            "Ask the customer for the exact error they saw, when, and the request ID from that "
            "response."
        ],
    )


def http_error_unexplained(facts: Facts) -> Finding | None:
    access = facts.access()
    if not access:
        return None
    key, event = access[-1]
    if event.status is None or event.status < 400:
        return None
    assessment = Assessment(Confidence.POSSIBLE, [key])
    for entry_key, _entry in facts.matching(
        lambda entry: not is_access_log(entry) and is_warning_or_error(entry)
    ):
        assessment.cite(entry_key)
    assessment.caveats.append(
        "Only the HTTP status is known; no log entry for this request names a cause that "
        "SupportOps recognises."
    )
    server_error = event.status >= 500
    return finding(
        "http_error_unexplained",
        f"The API answered HTTP {event.status} without a recorded cause",
        f"The API answered {describe_request(event)}, but its logs for this request don't say why.",
        assessment,
        next_steps=[
            "Read the full timeline below and the service's logs around that time.",
            "Reproduce the request in the lab with 'supportops api request' if it's safe to do.",
        ],
        escalation=Escalation(
            team="Engineering",
            severity="medium",
            reason="A server error without a logged cause needs engineering to investigate.",
        )
        if server_error
        else None,
    )


RULES: tuple[Callable[[Facts], Finding | None], ...] = (
    auth_rejection,
    malformed_json,
    validation_failed,
    invoice_not_payable,
    payment_invoice_inconsistent,
    unhandled_exception,
    lock_contention,
    database_unavailable,
    resource_not_found,
    request_succeeded,
)
FALLBACK_RULES: tuple[Callable[[Facts], Finding | None], ...] = (http_error_unexplained,)


Rule = Callable[[Facts], Finding | None]


def apply_rules(
    facts: Facts,
    rules: tuple[Rule, ...] = RULES,
    fallback: tuple[Rule, ...] = FALLBACK_RULES,
) -> list[Finding]:
    findings = [result for rule in rules if (result := rule(facts)) is not None]
    if findings:
        return findings
    return [result for rule in fallback if (result := rule(facts)) is not None]


def _note_lab_faults(facts: Facts, assessment: Assessment) -> None:
    start = facts.logs.service_start
    faults = _strings(start.extra.get("faults")) if start is not None else []
    if faults:
        assessment.cite(START_KEY)
        assessment.caveats.append(
            f"The service started with lab fault(s) enabled ({', '.join(faults)}), which inject "
            "failures deliberately."
        )


def _note_blocked_write(facts: Facts, assessment: Assessment) -> None:
    state = facts.live.invoice
    if state is None or state.lookup != "found" or state.record is None:
        return
    invoice = state.invoice_id
    assessment.cite(check_key(INVOICE_CHECK))
    if CURRENT_STATE_CAVEAT not in assessment.caveats:
        assessment.caveats.append(CURRENT_STATE_CAVEAT)
    recorded = [
        entry
        for entry in facts.entries("payment.recorded")
        if entry[1].extra.get("invoice_id") == invoice
    ]
    if recorded:
        assessment.inferences.append(
            f"This request logged payment.recorded for {invoice} before the timeout; check that "
            "payment before the customer tries again."
        )
        return
    assessment.inferences.append(
        f"Invoice {invoice} is still {state.record.get('status')} with "
        f"{state.record.get('succeeded_payments')} successful payments, and this request logged "
        "no payment.recorded event, so this attempt doesn't appear to have taken a payment."
    )


def _blocked_write_step(facts: Facts) -> str:
    path = (facts.logs.request.path or "") if facts.logs.request else ""
    attempt = "payment" if path.endswith("/pay") else "request"
    return (
        f"Ask the customer not to repeat the {attempt} until the blocking session has been dealt "
        "with; each attempt waits for the same lock."
    )


def _note_loopback_database(facts: Facts, assessment: Assessment, event: LogEvent) -> None:
    start = facts.logs.service_start
    host = _text(start.extra.get("database_host")) if start is not None else None
    if host is None or host.lower() not in LOOPBACK_HOSTS:
        return
    assessment.cite(START_KEY)
    if event.source.startswith("docker:"):
        assessment.inferences.append(
            f"The API started with database host '{host}', and its logs come from a Docker "
            f"container. Inside a container, {host} is the container itself, not the database, "
            "which produces exactly this error."
        )
    else:
        assessment.caveats.append(
            f"The API started with database host '{host}'. If it runs in a container, that "
            "address points at the container itself, not at the database."
        )


def _database_state(facts: Facts) -> str:
    return {
        "disabled": "database checks were skipped with --no-db",
        "not_configured": "SUPPORTOPS_DB_URL is not set",
        "unavailable": "the database couldn't be reached",
        "checked": "the check didn't run",
        "not_needed": "the check didn't run",
    }[facts.live.database]


def _sentence(text: str) -> str:
    return text.rstrip().rstrip(".") + "."


def _text(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


def _strings(value: object) -> list[str]:
    if isinstance(value, list):
        return [str(item) for item in value if isinstance(item, str | int)]
    return []
