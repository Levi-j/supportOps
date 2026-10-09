from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlsplit

from supportops.errors import ConfigError, SupportOpsError
from supportops.investigation.models import (
    Escalation,
    Evidence,
    Finding,
    Investigation,
    SignatureImpact,
)
from supportops.investigation.text import (
    current_state_notes,
    describe_coverage,
    describe_database_status,
    describe_event,
    describe_health_status,
    describe_origin,
    describe_window,
    format_time,
    scope_meaning,
    shared_gaps,
)
from supportops.redaction import mask_api_key, redact_text
from supportops.settings import Settings

MIN_SECRET_LENGTH = 4
_SEVERITY_ORDER = ("high", "medium", "low")
BANNER = (
    "> **Internal investigation draft - not for direct customer distribution.** SupportOps "
    "generated this from logs and read-only checks for human review. It may contain sensitive "
    "operational details, internal identifiers and information about other accounts. Verify "
    "every statement against the cited evidence, and remove internal details before sharing "
    "anything with a customer. Nothing here has been sent to anyone."
)
CONFIDENCE_GUIDE = (
    "*confirmed* means the request's own server-side log entry names the cause, the logged "
    "HTTP status agrees and nothing contradicts it; *likely* means the cause is stated "
    "directly but the evidence is incomplete or only linked by time; *possible* means only an "
    "indirect pattern was seen. A contradiction lowers a finding by one level."
)
CLOSING = (
    "*Review this draft before sharing any part of it or using it for a customer-facing decision.*"
)


class ReportLeakError(SupportOpsError):
    pass


def ensure_new_report(path: Path) -> None:
    if path.exists():
        raise ConfigError(
            f"{path} already exists.",
            hint="Choose a new file name; SupportOps never overwrites a report draft.",
        )


def configured_secrets(settings: Settings) -> list[tuple[str, str]]:
    secrets = []
    if settings.api_key is not None:
        secrets.append(("SUPPORTOPS_API_KEY", settings.api_key.get_secret_value()))
    if settings.db_url is not None:
        try:
            password = urlsplit(settings.db_url.get_secret_value()).password
        except ValueError:
            password = None
        if password:
            secrets.append(("the password in SUPPORTOPS_DB_URL", password))
            secrets.append(("the password in SUPPORTOPS_DB_URL", unquote(password)))
    return list(dict.fromkeys((name, value) for name, value in secrets if value))


def maskable(secrets: list[tuple[str, str]]) -> list[tuple[str, str]]:
    return [(name, value) for name, value in secrets if len(value) >= MIN_SECRET_LENGTH]


def too_short(secrets: list[tuple[str, str]]) -> list[tuple[str, str]]:
    return [(name, value) for name, value in secrets if len(value) < MIN_SECRET_LENGTH]


def ensure_maskable(secrets: list[tuple[str, str]]) -> None:
    short = list(dict.fromkeys(name for name, _ in too_short(secrets)))
    if short:
        settings = " and ".join(short)
        raise ConfigError(
            f"A configured secret ({settings}) is too short to redact safely, so SupportOps "
            "stopped before producing any investigation output.",
            hint=f"Use a secret of at least {MIN_SECRET_LENGTH} characters, or remove it from "
            "the configuration for this run.",
        )


def leaked_secrets(text: str, secrets: list[tuple[str, str]]) -> list[str]:
    return list(dict.fromkeys(name for name, value in maskable(secrets) if value in text))


def mask_secrets(investigation: Investigation, secrets: list[tuple[str, str]]) -> Investigation:
    values = sorted({value for _, value in maskable(secrets)}, key=len, reverse=True)
    if not values:
        return investigation
    masked = _mask(investigation.model_dump(), values)
    return Investigation.model_validate(masked)


def _mask(value: Any, secrets: list[str]) -> Any:
    if isinstance(value, str):
        for secret in secrets:
            value = value.replace(secret, mask_api_key(secret))
        return value
    if isinstance(value, list):
        return [_mask(item, secrets) for item in value]
    if isinstance(value, dict):
        return {key: _mask(item, secrets) for key, item in value.items()}
    return value


def write_report(investigation: Investigation, path: Path, secrets: list[tuple[str, str]]) -> Path:
    ensure_maskable(secrets)
    text = redact_text(render_report(investigation))
    leaked = leaked_secrets(text, secrets)
    if leaked:
        raise ReportLeakError(
            f"The report draft still contained {', '.join(leaked)} after redaction, so it "
            "wasn't written.",
            hint="A log entry or check result probably contains the secret in an unusual "
            "format. Review the evidence and rotate the secret if it was exposed.",
        )
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with path.open("x", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
    except FileExistsError:
        raise ConfigError(
            f"{path} already exists.",
            hint="Choose a new file name; SupportOps never overwrites a report draft.",
        ) from None
    return path


def render_report(investigation: Investigation) -> str:
    sections = [
        [f"# Internal investigation draft: request `{investigation.request_id}`", "", BANNER],
        _summary(investigation),
        _findings(investigation),
        _timeline(investigation),
        _evidence(investigation.evidence),
        _impact(investigation),
        _next_steps(investigation.findings),
        _escalation(investigation.findings),
        _open_questions(investigation.open_questions),
        _sources(investigation),
        [CLOSING],
    ]
    return "\n\n".join("\n".join(section) for section in sections) + "\n"


def _summary(investigation: Investigation) -> list[str]:
    rows = [
        ("Request ID", _code(investigation.request_id)),
        ("Result", _cell(f"{investigation.verdict}: {investigation.summary}")),
    ]
    request = investigation.logs.request
    if request is not None:
        described = " ".join(part for part in (request.method, request.path) if part)
        if request.status is not None:
            described += f" -> {request.status}"
        timing = f" ({request.duration_ms:,.0f} ms)" if request.duration_ms is not None else ""
        rows.append(("Request", f"{_code(described)}{timing}"))
        rows.append(("Request time (UTC)", _code(format_time(request.timestamp))))
    entities = investigation.logs.entities
    for label, value in (
        ("Account", entities.account_id),
        ("API key prefix", entities.key_prefix),
        ("Invoice", entities.invoice_id),
        ("Payments", ", ".join(entities.payment_ids)),
        ("Order", entities.order_id),
        ("Product", entities.product_id),
    ):
        if value:
            rows.append((label, _code(value)))
    rows.append(("Report generated (UTC)", _code(format_time(investigation.generated_at))))
    return [
        "## Summary",
        "",
        "| Field | Details |",
        "| --- | --- |",
        *(f"| {label} | {value} |" for label, value in rows),
    ]


def _findings(investigation: Investigation) -> list[str]:
    lines = ["## Findings"]
    if not investigation.findings:
        lines += [
            "",
            "No rule matched, so this investigation is inconclusive. See the open "
            "questions and the timeline.",
        ]
        return lines
    for number, item in enumerate(investigation.findings, start=1):
        lines += _finding(number, item)
    return lines


def _finding(number: int, item: Finding) -> list[str]:
    citations = ", ".join(f"[{evidence_id}]" for evidence_id in item.evidence_ids)
    lines = [
        "",
        f"### {number}. {_escape(item.title)} (confidence: {item.confidence})",
        "",
        _escape(item.summary),
        "",
        f"Evidence: {citations}",
    ]
    for title, entries in (
        ("Interpretation (not verified)", item.inferences),
        ("Contradictions", item.contradictions),
        ("Evidence limits", item.caveats),
    ):
        if entries:
            lines += ["", f"**{title}**", "", *(f"- {_escape(entry)}" for entry in entries)]
    return lines


def _timeline(investigation: Investigation) -> list[str]:
    steps = investigation.logs.trace.steps
    if not steps:
        return ["## Timeline", "", "No log entries carry this request ID."]
    rows = [
        f"| {_offset(step.offset_ms)} | {_code(format_time(step.event.timestamp))} | "
        f"{_cell(describe_event(step.event))} |"
        for step in steps
    ]
    return ["## Timeline", "", "| Offset | Time (UTC) | Log entry |", "| --- | --- | --- |", *rows]


def _evidence(evidence: list[Evidence]) -> list[str]:
    lines = ["## Evidence"]
    if not evidence:
        return [*lines, "", "No evidence was cited."]
    lines.append("")
    for item in evidence:
        lines.append(
            f"- **{item.id}** ({describe_origin(item)}, `{item.reference}`): "
            f"{_escape(item.summary)}"
        )
        if item.details:
            fence = "````"
            lines += ["", f"  {fence}text"]
            lines += [f"  {line}" for line in item.details.rstrip().splitlines()]
            lines += [f"  {fence}", ""]
    return lines


def _impact(investigation: Investigation) -> list[str]:
    items = investigation.logs.impact
    lines = ["## Impact"]
    if not items:
        return [*lines, "", "Not measured: the request has no error signature to compare."]
    lines += [
        "",
        "Requests with the same error signature in the logs that were read, counted by "
        "distinct request ID rather than by log entry.",
        "",
        "| Error signature | Requests | Accounts | Log entries | Scope |",
        "| --- | --- | --- | --- | --- |",
        *(_impact_row(item, investigation.request_id) for item in items),
        "",
        f"**Window:** {describe_window(items)}.",
        "",
        *(
            f"**{scope.capitalize()}:** {_escape(scope_meaning(scope))}"
            for scope in dict.fromkeys(item.scope for item in items)
        ),
    ]
    shared = shared_gaps(items)
    for item in items:
        specific = [gap for gap in item.gaps if gap not in shared]
        if specific:
            lines += [
                "",
                f"Additional coverage gaps for {_code(item.signature)}:",
                "",
                *(f"- {_escape(gap.detail)}" for gap in specific),
            ]
    lines += ["", f"**Coverage:** {describe_coverage(items)}"]
    if shared:
        lines += ["", *(f"- {_escape(gap.detail)}" for gap in shared)]
    return lines


def _impact_row(item: SignatureImpact, request_id: str) -> str:
    bound = "At least " if item.lower_bound else ""
    requests = [request_id, *item.other_request_ids]
    more = ", ..." if item.other_requests > len(item.other_request_ids) else ""
    accounts = (
        f"{bound}{item.accounts} ({', '.join(item.account_ids)}"
        f"{', ...' if item.accounts > len(item.account_ids) else ''})"
        if item.accounts
        else "None identified"
    )
    cells = [
        _code(item.signature),
        _cell(f"{bound}{item.requests} ({', '.join(requests)}{more})"),
        _cell(accounts),
        f"{bound}{item.events}",
        item.scope.capitalize(),
    ]
    return "| " + " | ".join(cells) + " |"


def _next_steps(findings: list[Finding]) -> list[str]:
    title = "## Recommended next steps"
    steps = list(dict.fromkeys(step for item in findings for step in item.next_steps))
    if not steps:
        return [title, "", "The rules suggest no next steps for this request."]
    return [
        title,
        "",
        *(f"{number}. {_escape(step)}" for number, step in enumerate(steps, start=1)),
    ]


def _open_questions(questions: list[str]) -> list[str]:
    if not questions:
        return ["## Open questions", "", "The investigation recorded no open questions."]
    return ["## Open questions", "", *(f"- {_escape(question)}" for question in questions)]


def _escalation(findings: list[Finding]) -> list[str]:
    teams: dict[str, list[tuple[int, Finding, Escalation]]] = {}
    for number, item in enumerate(findings, start=1):
        if item.escalation is not None:
            teams.setdefault(item.escalation.team, []).append((number, item, item.escalation))
    if not teams:
        return ["## Escalation", "", "No escalation is indicated by the findings."]
    lines = ["## Escalation", ""]
    for team, entries in teams.items():
        severity = min((entry[2].severity for entry in entries), key=_SEVERITY_ORDER.index)
        lines.append(f"- **{team}**, {severity} severity")
        lines += [
            f"  - Finding {number} ({_escape(item.title)}): {_escape(escalation.reason)}"
            for number, item, escalation in entries
        ]
    return lines


def _sources(investigation: Investigation) -> list[str]:
    stats = investigation.logs.trace.input
    live = investigation.live
    lines = [
        f"- **Application logs:** {_escape(', '.join(investigation.sources))}; "
        f"{stats.lines:,} lines read, {stats.parsed:,} log entries parsed, "
        f"{stats.skipped_total:,} skipped.",
        f"- **Database:** {_escape(describe_database_status(live))}.",
        f"- **API health:** {_escape(describe_health_status(live))}.",
        *(f"- **Current state:** {note}" for note in current_state_notes(live)),
        f"- **Confidence:** {CONFIDENCE_GUIDE}",
    ]
    start = investigation.logs.service_start
    if start is not None and start.extra.get("environment") == "lab":
        faults = [str(fault) for fault in start.extra.get("faults") or []]
        enabled = f" with the fault(s) {', '.join(faults)} enabled" if faults else ""
        lines.append(
            f"- **Lab context:** the service reported environment 'lab'{enabled}. Problems may "
            "have been simulated deliberately; this draft doesn't claim a real defect."
        )
    lines += [f"- **Note:** {_escape(note)}" for note in investigation.notes]
    return ["## Sources and limitations", "", *lines]


def _offset(offset_ms: float | None) -> str:
    return "?" if offset_ms is None else f"+{offset_ms:,.0f} ms"


def _escape(text: str) -> str:
    return text.replace("\r", " ").replace("\n", " ").replace("<", "\\<")


def _cell(text: str) -> str:
    return _escape(text).replace("|", "\\|")


def _code(text: str) -> str:
    cleaned = text.replace("\r", " ").replace("\n", " ").replace("`", "'").replace("|", "\\|")
    return f"`{cleaned}`"
