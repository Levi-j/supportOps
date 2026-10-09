import re
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import NamedTuple

from supportops.db.catalog import CHECKS
from supportops.investigation.models import (
    CoverageGap,
    Entities,
    ImpactScope,
    LogEvidence,
    RequestSummary,
    SignatureImpact,
)
from supportops.logs.analysis import (
    ACCESS_EVENT,
    ErrorSignature,
    RequestTrace,
    TimeWindow,
    error_signature,
    is_access_log,
    is_warning_or_error,
    normalize_message,
    trace,
)
from supportops.logs.parser import InputStats, LogEvent, LogInput
from supportops.redaction import redact_text

IMPACT_WINDOW = timedelta(minutes=15)
MAX_OCCURRENCES = 20_000
MAX_STARTUPS = 20
MAX_EXAMPLES = 5
STARTUP_EVENT = "app.started"

_INVOICE_PATH = re.compile(r"/v1/invoices/([^/]+)")
_CUSTOMER_PATH = re.compile(r"/v1/customers/([^/]+)")
_IDENTIFIER = re.compile(r"[A-Za-z0-9_-]{1,64}")


def _catalog_pattern(check: str, parameter: str) -> re.Pattern[str]:
    declared = CHECKS[check].parameter(parameter)
    if declared is None or declared.pattern is None:
        raise LookupError(f"{check} has no pattern for {parameter}")
    return re.compile(declared.pattern)


_KEY_PREFIX = _catalog_pattern("billing.api_key_status", "prefix")
_INVOICE_ID = _catalog_pattern("billing.invoice_lookup", "id")


class Occurrence(NamedTuple):
    timestamp: datetime | None
    request_id: str | None
    account_id: str | None


@dataclass
class _Scan:
    matched: list[LogEvent] = field(default_factory=list)
    occurrences: dict[ErrorSignature, list[Occurrence]] = field(default_factory=dict)
    recorded: int = 0
    capped: bool = False
    startups: deque[LogEvent] = field(default_factory=lambda: deque(maxlen=MAX_STARTUPS))
    earliest: datetime | None = None
    latest: datetime | None = None
    undated: int = 0


def signature_of(event: LogEvent) -> ErrorSignature | None:
    if is_access_log(event) and event.status is not None and event.status >= 400:
        return access_signature(event)
    if is_warning_or_error(event):
        return error_signature(event)
    return None


def access_signature(event: LogEvent) -> ErrorSignature:
    path = normalize_message(redact_text(event.path)) if event.path else None
    return ErrorSignature(
        level=None,
        name=ACCESS_EVENT,
        error_type=None,
        category=str(event.status),
        detail=" ".join(part for part in (event.method, path) if part),
    )


def collect_logs(log_input: LogInput, request_id: str, window: TimeWindow) -> LogEvidence:
    scan = _scan(log_input, request_id, window)
    request_trace = trace(LogInput(log_input.stats, iter(scan.matched)), request_id, window)
    entities, notes = extract_entities(request_trace)
    signatures = request_signatures(request_trace)
    gaps = _coverage_gaps(log_input.stats, window, request_trace, scan)
    impact = [
        measure_impact(
            signature,
            scan.occurrences.get(signature, []),
            request_id=request_id,
            account_id=entities.account_id,
            anchor=request_trace.first_seen,
            gaps=gaps,
        )
        for signature in signatures
    ]
    if request_trace.found and not signatures:
        notes.append(
            "Impact wasn't measured: this request has no warning, error or HTTP 4xx/5xx "
            "entry, so there is no error signature to compare with other requests."
        )
    return LogEvidence(
        request_id=request_id,
        trace=request_trace,
        request=summarize_request(request_trace, entities),
        entities=entities,
        service_start=_service_start(request_trace, scan.startups),
        impact=impact,
        notes=notes,
    )


def _scan(log_input: LogInput, request_id: str, window: TimeWindow) -> _Scan:
    scan = _Scan()
    for event in log_input.events:
        if not window.admits(event, log_input.stats):
            continue
        if event.timestamp is None:
            scan.undated += 1
        else:
            scan.earliest = min(scan.earliest or event.timestamp, event.timestamp)
            scan.latest = max(scan.latest or event.timestamp, event.timestamp)
        if event.request_id == request_id:
            scan.matched.append(event)
        if event.event_name == STARTUP_EVENT:
            scan.startups.append(event)
        signature = signature_of(event)
        if signature is None:
            continue
        if scan.recorded >= MAX_OCCURRENCES:
            scan.capped = True
            continue
        scan.occurrences.setdefault(signature, []).append(
            Occurrence(event.timestamp, event.request_id, event.account_id)
        )
        scan.recorded += 1
    return scan


def request_signatures(request_trace: RequestTrace) -> list[ErrorSignature]:
    signatures = (signature_of(step.event) for step in request_trace.steps)
    return list(dict.fromkeys(signature for signature in signatures if signature is not None))


def extract_entities(request_trace: RequestTrace) -> tuple[Entities, list[str]]:
    found: dict[str, list[str]] = {
        "account": [],
        "API key prefix": [],
        "invoice": [],
        "customer": [],
        "payment": [],
    }
    rejected: set[str] = set()
    for step in request_trace.steps:
        event = step.event
        _add(found, rejected, "account", event.account_id, _IDENTIFIER)
        _add(found, rejected, "API key prefix", event.extra.get("key_prefix"), _KEY_PREFIX)
        _add(found, rejected, "invoice", event.extra.get("invoice_id"), _INVOICE_ID)
        _add(found, rejected, "invoice", _path_id(_INVOICE_PATH, event.path), _INVOICE_ID)
        _add(found, rejected, "customer", event.extra.get("customer_id"), _IDENTIFIER)
        _add(found, rejected, "customer", _path_id(_CUSTOMER_PATH, event.path), _IDENTIFIER)
        _add(found, rejected, "payment", event.extra.get("payment_id"), _IDENTIFIER)
    notes = [
        f"Ignored a value for the {label} that doesn't have the expected format."
        for label in sorted(rejected)
    ]
    for label in ("account", "API key prefix", "invoice", "customer"):
        if len(found[label]) > 1:
            notes.append(
                f"The entries for this request name more than one {label} "
                f"({', '.join(found[label])}), so none of them is used."
            )
    return (
        Entities(
            account_id=_single(found["account"]),
            key_prefix=_single(found["API key prefix"]),
            invoice_id=_single(found["invoice"]),
            customer_id=_single(found["customer"]),
            payment_ids=found["payment"],
        ),
        notes,
    )


def summarize_request(request_trace: RequestTrace, entities: Entities) -> RequestSummary | None:
    timing = request_trace.access_log
    if timing is None:
        return None
    return RequestSummary(
        method=timing.method,
        path=timing.path,
        status=timing.status,
        duration_ms=timing.duration_ms,
        timestamp=timing.timestamp,
        account_id=entities.account_id,
    )


def _coverage_gaps(
    stats: InputStats, window: TimeWindow, request_trace: RequestTrace, scan: _Scan
) -> list[CoverageGap]:
    gaps = []
    anchor = request_trace.first_seen
    if anchor is None:
        gaps.append(
            CoverageGap(
                reason="undated_request",
                detail="The request's log entries have no usable timestamp, so there is no "
                "time window to compare against; every entry that was read was counted.",
            )
        )
    access_logs = sum(is_access_log(step.event) for step in request_trace.steps)
    if access_logs > 1:
        gaps.append(
            CoverageGap(
                reason="request_id_reused",
                detail=f"{access_logs} access-log entries carry this request ID, so the "
                "request's own signature may mix several requests.",
            )
        )
    if scan.capped:
        gaps.append(
            CoverageGap(
                reason="occurrence_cap",
                detail=f"More than {MAX_OCCURRENCES:,} warning and error entries were read; "
                "the ones after that weren't counted.",
            )
        )
    if stats.truncated_sources:
        gaps.append(
            CoverageGap(
                reason="docker_tail_limit",
                detail="Only the most recent lines of "
                + ", ".join(stats.truncated_sources)
                + " were read, so older entries are missing.",
            )
        )
    if stats.skipped_total:
        gaps.append(
            CoverageGap(
                reason="skipped_lines",
                detail=f"{stats.skipped_total:,} lines weren't usable log entries and "
                "couldn't be checked.",
            )
        )
    undated = scan.undated + stats.excluded_without_timestamp
    if undated and anchor is not None:
        gaps.append(
            CoverageGap(
                reason="undated_entries",
                detail=f"{undated:,} entries have no usable timestamp, so they couldn't be "
                "placed inside or outside the window.",
            )
        )
    if anchor is not None:
        gaps.extend(_window_gaps(window, anchor - IMPACT_WINDOW, anchor + IMPACT_WINDOW, scan))
    return gaps


def _window_gaps(
    window: TimeWindow, start: datetime, end: datetime, scan: _Scan
) -> list[CoverageGap]:
    gaps = []
    if window.since is not None and window.since > start:
        gaps.append(
            CoverageGap(
                reason="since_clips_window",
                detail=f"--since starts {_duration(window.since - start)} after the "
                "start of the 30-minute impact window.",
            )
        )
    elif scan.earliest is not None and scan.earliest > start:
        gaps.append(
            CoverageGap(
                reason="logs_start_inside_window",
                detail=f"The logs that were read begin {_duration(scan.earliest - start)} "
                "after the start of the 30-minute impact window.",
            )
        )
    if window.until is not None and window.until <= end:
        gaps.append(
            CoverageGap(
                reason="until_clips_window",
                detail=f"--until stops {_duration(end - window.until)} before the end of "
                "the 30-minute impact window.",
            )
        )
    elif scan.latest is not None and scan.latest < end:
        gaps.append(
            CoverageGap(
                reason="logs_end_inside_window",
                detail=f"The logs that were read end {_duration(end - scan.latest)} before "
                "the end of the 30-minute impact window; later occurrences aren't counted.",
            )
        )
    return gaps


def measure_impact(
    signature: ErrorSignature,
    occurrences: list[Occurrence],
    *,
    request_id: str,
    account_id: str | None,
    anchor: datetime | None,
    gaps: list[CoverageGap],
) -> SignatureImpact:
    start = anchor - IMPACT_WINDOW if anchor is not None else None
    end = anchor + IMPACT_WINDOW if anchor is not None else None
    selected = [
        occurrence
        for occurrence in occurrences
        if start is None
        or end is None
        or (occurrence.timestamp is not None and start <= occurrence.timestamp <= end)
    ]
    request_ids = {item.request_id for item in selected if item.request_id} | {request_id}
    others = sorted(request_ids - {request_id})
    accounts = {item.account_id for item in selected if item.account_id}
    if account_id:
        accounts.add(account_id)
    without_id = sum(item.request_id is None for item in selected)
    all_gaps = list(gaps)
    if without_id:
        all_gaps.append(
            CoverageGap(
                reason="occurrences_without_request_id",
                detail=f"{without_id:,} matching entries have no request ID, so they can't "
                "be attributed to a request.",
            )
        )
    timestamps = [item.timestamp for item in selected if item.timestamp is not None]
    return SignatureImpact(
        signature=signature.describe(),
        window_start=start,
        window_end=end,
        requests=len(request_ids),
        other_requests=len(others),
        other_request_ids=others[:MAX_EXAMPLES],
        accounts=len(accounts),
        account_ids=sorted(accounts)[:MAX_EXAMPLES],
        events=len(selected),
        events_without_request_id=without_id,
        first_seen=min(timestamps) if timestamps else None,
        last_seen=max(timestamps) if timestamps else None,
        coverage="partial" if all_gaps else "complete",
        gaps=all_gaps,
        scope=_scope(len(others), len(accounts), complete=not all_gaps),
    )


def _scope(other_requests: int, accounts: int, *, complete: bool) -> ImpactScope:
    if other_requests == 0:
        return "isolated" if complete else "undetermined"
    return "widespread" if accounts >= 2 else "recurring"


def _service_start(request_trace: RequestTrace, startups: deque[LogEvent]) -> LogEvent | None:
    if not request_trace.steps:
        return None
    source = request_trace.steps[0].event.source
    anchor = request_trace.first_seen
    candidates = [
        event
        for event in startups
        if event.source == source
        and (anchor is None or (event.timestamp is not None and event.timestamp <= anchor))
    ]
    return candidates[-1] if candidates else None


def _add(
    found: dict[str, list[str]],
    rejected: set[str],
    label: str,
    value: object,
    pattern: re.Pattern[str],
) -> None:
    if not isinstance(value, str) or not value:
        return
    if not pattern.fullmatch(value):
        rejected.add(label)
    elif value not in found[label]:
        found[label].append(value)


def _path_id(pattern: re.Pattern[str], path: str | None) -> str | None:
    match = pattern.match(path) if path else None
    return match.group(1) if match else None


def _single(values: list[str]) -> str | None:
    return values[0] if len(values) == 1 else None


def _duration(delta: timedelta) -> str:
    seconds = int(delta.total_seconds())
    if seconds < 120:
        return f"{seconds} s"
    return f"{seconds // 60} min"
