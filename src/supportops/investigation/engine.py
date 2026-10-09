from collections.abc import Callable
from datetime import UTC, datetime

import httpx

from supportops.errors import ConfigError
from supportops.http_checks import REQUEST_ID_PATTERN
from supportops.investigation.collect import collect_logs
from supportops.investigation.live import collect_live
from supportops.investigation.models import (
    Evidence,
    Finding,
    Investigation,
    LiveEvidence,
    LogEvidence,
)
from supportops.investigation.orderflow import ORDERFLOW_FALLBACK_RULES, ORDERFLOW_RULES
from supportops.investigation.rules import (
    FALLBACK_RULES,
    HEALTH_KEY,
    RULES,
    START_KEY,
    Facts,
    Rule,
    apply_rules,
    check_key,
    log_key,
)
from supportops.investigation.text import describe_check, describe_event, describe_health
from supportops.logs.analysis import TimeWindow, is_warning_or_error
from supportops.logs.parser import LogEvent, LogInput
from supportops.settings import Settings
from supportops.targets import TargetProfile, get_target

RULE_SETS: dict[str, tuple[tuple[Rule, ...], tuple[Rule, ...]]] = {
    "billing": (RULES, FALLBACK_RULES),
    "orderflow": (ORDERFLOW_RULES, ORDERFLOW_FALLBACK_RULES),
}


def validate_request_id(request_id: str) -> str:
    if not REQUEST_ID_PATTERN.fullmatch(request_id):
        raise ConfigError(
            "That doesn't look like a request ID.",
            hint="Request IDs are 1 to 64 letters, digits, '-' or '_'. Copy it from the "
            "X-Request-Id response header or the request_id in the error body.",
        )
    return request_id


def investigate(
    log_input: LogInput,
    request_id: str,
    window: TimeWindow,
    settings: Settings,
    profile: TargetProfile,
    client: httpx.Client,
    *,
    use_database: bool = True,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> Investigation:
    validate_request_id(request_id)
    logs = collect_logs(log_input, request_id, window)
    live = collect_live(logs, settings, profile, client, use_database=use_database, now=now)
    drafts = apply_rules(
        Facts(logs, live, settings.slow_request_ms), *RULE_SETS[profile.check_pack]
    )
    findings, evidence = number_evidence(drafts, evidence_candidates(logs, live))
    return Investigation(
        request_id=request_id,
        generated_at=live.collected_at,
        verdict="FINDINGS" if findings else "INCONCLUSIVE",
        summary=_summary(logs, findings),
        sources=list(log_input.stats.sources),
        logs=logs,
        live=live,
        evidence=evidence,
        findings=findings,
        open_questions=_open_questions(logs, live, findings),
        notes=_notes(logs, live),
    )


def evidence_candidates(logs: LogEvidence, live: LiveEvidence) -> dict[str, Evidence]:
    candidates: dict[str, Evidence] = {}
    for index, step in enumerate(logs.trace.steps):
        candidates[log_key(index)] = _log_evidence(step.event)
    if logs.service_start is not None:
        candidates[START_KEY] = _log_evidence(logs.service_start)
    for result in live.checks:
        candidates[check_key(result.name)] = Evidence(
            id="",
            source="database",
            summary=describe_check(
                result, logs.entities.invoice_id, order_id=logs.entities.order_id
            ),
            reference=result.name,
            observed_at=live.collected_at,
            current_state=True,
        )
    if live.health is not None:
        target = get_target(live.health.target)
        candidates[HEALTH_KEY] = Evidence(
            id="",
            source="api",
            summary=describe_health(live.health),
            reference=f"GET {target.liveness_path}, GET {target.readiness_path} and a "
            "PostgreSQL probe",
            observed_at=live.collected_at,
            current_state=True,
        )
    return candidates


def number_evidence(
    drafts: list[Finding], candidates: dict[str, Evidence]
) -> tuple[list[Finding], list[Evidence]]:
    numbered: dict[str, Evidence] = {}
    findings = []
    for draft in drafts:
        if not draft.evidence_ids:
            raise LookupError(f"Finding {draft.rule} cites no evidence.")
        ids = []
        for key in draft.evidence_ids:
            if key not in candidates:
                raise LookupError(f"Finding {draft.rule} cites unknown evidence {key}.")
            if key not in numbered:
                numbered[key] = candidates[key].model_copy(update={"id": f"E{len(numbered) + 1}"})
            ids.append(numbered[key].id)
        findings.append(draft.model_copy(update={"evidence_ids": ids}))
    return findings, list(numbered.values())


def _log_evidence(event: LogEvent) -> Evidence:
    return Evidence(
        id="",
        source="log",
        summary=describe_event(event),
        reference=f"{event.source}:{event.line}",
        observed_at=event.timestamp,
        current_state=False,
        details=event.stack_trace,
    )


def _summary(logs: LogEvidence, findings: list[Finding]) -> str:
    if not logs.trace.found:
        return f"No log entries carry request ID {logs.request_id} in the logs that were read."
    if not findings:
        return "The request was found in the logs, but no rule explains what happened."
    first = findings[0]
    text = f"{first.title} ({first.confidence})."
    more = len(findings) - 1
    if more == 1:
        text += " One more finding follows."
    elif more > 1:
        text += f" {more} more findings follow."
    return text


def _open_questions(logs: LogEvidence, live: LiveEvidence, findings: list[Finding]) -> list[str]:
    questions = list(live.open_questions)
    if not logs.trace.found:
        questions.append(
            "Did the request reach this service, and is the request ID exactly right? Ask the "
            "customer for the X-Request-Id response header or the request_id in the error body."
        )
    elif not findings:
        unexplained = [
            step.event.event_name or step.event.logger or "an unnamed entry"
            for step in logs.trace.steps
            if is_warning_or_error(step.event)
        ]
        questions.append(
            "No rule matched this request. Review the timeline"
            + (f", especially: {', '.join(dict.fromkeys(unexplained))}." if unexplained else ".")
        )
    if any(item.scope == "undetermined" for item in logs.impact):
        questions.append(
            "Are other requests affected? The logs that were read don't fully cover the 15 "
            "minutes before and after this request (see Impact)."
        )
    state = live.invoice
    if state is not None and state.lookup in ("other_account", "unavailable"):
        questions.extend(state.notes)
    if (
        state is not None
        and state.contradictions
        and not any(item.rule == "payment_invoice_inconsistent" for item in findings)
    ):
        questions.extend(state.contradictions)
    return list(dict.fromkeys(questions))


def _notes(logs: LogEvidence, live: LiveEvidence) -> list[str]:
    notes = [*logs.trace.notes, *logs.notes, *live.notes]
    if live.invoice is not None and live.invoice.lookup in ("found", "not_found"):
        notes.extend(live.invoice.notes)
    return list(dict.fromkeys(notes))
