from datetime import datetime
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, Field

from supportops.db.runner import CheckResult
from supportops.errors import ExitCode
from supportops.health import HealthReport
from supportops.logs.analysis import RequestTrace
from supportops.logs.parser import LogEvent

CoverageReason = Literal[
    "undated_request",
    "request_id_reused",
    "occurrence_cap",
    "docker_tail_limit",
    "skipped_lines",
    "undated_entries",
    "since_clips_window",
    "until_clips_window",
    "logs_start_inside_window",
    "logs_end_inside_window",
    "occurrences_without_request_id",
]
ImpactScope = Literal["isolated", "recurring", "widespread", "undetermined"]
DatabaseStatus = Literal["checked", "not_needed", "disabled", "not_configured", "unavailable"]
InvoiceLookup = Literal["found", "not_found", "other_account", "unavailable"]
EvidenceSource = Literal["log", "database", "api"]
Severity = Literal["high", "medium", "low"]
Verdict = Literal["FINDINGS", "INCONCLUSIVE"]


class Confidence(StrEnum):
    CONFIRMED = "confirmed"
    LIKELY = "likely"
    POSSIBLE = "possible"

    def downgraded(self) -> "Confidence":
        if self is Confidence.CONFIRMED:
            return Confidence.LIKELY
        return Confidence.POSSIBLE


class RequestSummary(BaseModel):
    method: str | None
    path: str | None
    status: int | None
    duration_ms: float | None
    timestamp: datetime | None
    account_id: str | None


class Entities(BaseModel):
    account_id: str | None = None
    key_prefix: str | None = None
    invoice_id: str | None = None
    customer_id: str | None = None
    payment_ids: list[str] = Field(default_factory=list)


class CoverageGap(BaseModel):
    reason: CoverageReason
    detail: str


class SignatureImpact(BaseModel):
    signature: str
    window_start: datetime | None
    window_end: datetime | None
    requests: int
    other_requests: int
    other_request_ids: list[str]
    accounts: int
    account_ids: list[str]
    events: int
    events_without_request_id: int
    first_seen: datetime | None
    last_seen: datetime | None
    coverage: Literal["complete", "partial"]
    gaps: list[CoverageGap]
    scope: ImpactScope

    @property
    def lower_bound(self) -> bool:
        return self.coverage == "partial"


class LogEvidence(BaseModel):
    request_id: str
    trace: RequestTrace
    request: RequestSummary | None
    entities: Entities
    service_start: LogEvent | None
    impact: list[SignatureImpact]
    notes: list[str]


class OtherInvoices(BaseModel):
    check: str
    count: int
    at_least: bool
    examples: list[str]


class InvoiceState(BaseModel):
    invoice_id: str
    lookup: InvoiceLookup
    record: dict[str, Any] | None = None
    conditions: list[str] = Field(default_factory=list)
    corroborated_by: list[str] = Field(default_factory=list)
    contradictions: list[str] = Field(default_factory=list)
    elsewhere: list[OtherInvoices] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)


class LiveEvidence(BaseModel):
    collected_at: datetime
    database: DatabaseStatus
    database_target: str | None = None
    database_detail: str | None = None
    checks: list[CheckResult] = Field(default_factory=list)
    invoice: InvoiceState | None = None
    health: HealthReport | None = None
    notes: list[str] = Field(default_factory=list)
    open_questions: list[str] = Field(default_factory=list)

    def check(self, name: str) -> CheckResult | None:
        return next((result for result in self.checks if result.name == name), None)


class Evidence(BaseModel):
    id: str
    source: EvidenceSource
    summary: str
    reference: str
    observed_at: datetime | None
    current_state: bool
    details: str | None = None


class Escalation(BaseModel):
    team: str
    severity: Severity
    reason: str


class Finding(BaseModel):
    rule: str
    title: str
    confidence: Confidence
    summary: str
    evidence_ids: list[str]
    inferences: list[str] = Field(default_factory=list)
    contradictions: list[str] = Field(default_factory=list)
    caveats: list[str] = Field(default_factory=list)
    next_steps: list[str] = Field(default_factory=list)
    escalation: Escalation | None = None


class Investigation(BaseModel):
    request_id: str
    generated_at: datetime
    verdict: Verdict
    summary: str
    sources: list[str]
    logs: LogEvidence
    live: LiveEvidence
    evidence: list[Evidence]
    findings: list[Finding]
    open_questions: list[str]
    notes: list[str]

    @property
    def exit_code(self) -> ExitCode:
        return ExitCode.OK if self.verdict == "FINDINGS" else ExitCode.PROBLEM
