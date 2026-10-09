import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from pydantic import SecretStr

from supportops.db.catalog import CHECKS
from supportops.db.connection import DatabaseError, SessionInfo
from supportops.db.runner import CheckResult, DbReport, PlannedCheck
from supportops.investigation.collect import collect_logs
from supportops.investigation.models import LiveEvidence, LogEvidence, SignatureImpact
from supportops.logs.analysis import TimeWindow
from supportops.logs.parser import InputStats, LogEvent, LogInput, parse_line, read_logs

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"
BILLING = str(FIXTURES / "logs" / "billing-api.jsonl")
NO_WINDOW = TimeWindow()
BASE = datetime(2026, 10, 8, 12, 0, tzinfo=UTC)
NOW = datetime(2026, 10, 8, 12, 30, tzinfo=UTC)
REQUEST = "req-1"
REVOKED = "auth.rejected (revoked_key): API key rejected"
ACCESS_401 = "http.request (401): GET /v1/account"
DB_PASSWORD = "InvestigateDbPw-4417"
DB_URL = f"postgresql://supportops_ro:{DB_PASSWORD}@127.0.0.1:5433/billing"
API_KEY = "bk_juniper01_lab_only_not_a_real_key"
SESSION = SessionInfo(
    role="supportops_ro", database="billing", server_version="18.6", read_only=True, monitoring=True
)


def at(minutes: float) -> str:
    return (BASE + timedelta(minutes=minutes)).isoformat().replace("+00:00", "Z")


def rejected(
    minutes: float | None,
    request_id: str | None = REQUEST,
    account: str | None = "acct_juniper",
    reason: str = "revoked_key",
    prefix: str | None = "bk_juniper00",
) -> dict[str, Any]:
    entry: dict[str, Any] = {
        "level": "WARNING",
        "event_name": "auth.rejected",
        "message": "API key rejected",
        "reason": reason,
    }
    if prefix is not None:
        entry["key_prefix"] = prefix
    if minutes is not None:
        entry["timestamp"] = at(minutes)
    if request_id is not None:
        entry["request_id"] = request_id
    if account is not None:
        entry["account_id"] = account
    return entry


def access(
    minutes: float | None,
    request_id: str = REQUEST,
    status: int = 401,
    path: str = "/v1/account",
    method: str = "GET",
    duration_ms: float = 7,
    account: str | None = None,
) -> dict[str, Any]:
    entry: dict[str, Any] = {
        "level": "INFO",
        "event_name": "http.request",
        "message": "HTTP request",
        "request_id": request_id,
        "method": method,
        "path": path,
        "status": status,
        "duration_ms": duration_ms,
    }
    if minutes is not None:
        entry["timestamp"] = at(minutes)
    if account is not None:
        entry["account_id"] = account
    return entry


def event(
    minutes: float, name: str, level: str = "WARNING", request_id: str = REQUEST, **fields: Any
) -> dict[str, Any]:
    return {
        "timestamp": at(minutes),
        "level": level,
        "event_name": name,
        "message": name,
        "request_id": request_id,
        **fields,
    }


def info(minutes: float, **fields: Any) -> dict[str, Any]:
    return {"timestamp": at(minutes), "level": "INFO", "message": "tick", **fields}


def spanning(*entries: dict[str, Any]) -> list[dict[str, Any]]:
    return [info(-20), *entries, info(20)]


def log_input(entries: list[dict[str, Any]], source: str = "api.jsonl", **stats: Any) -> LogInput:
    events = []
    for number, entry in enumerate(entries, start=1):
        parsed = parse_line(json.dumps(entry), source, number)
        assert isinstance(parsed, LogEvent)
        events.append(parsed)
    return LogInput(InputStats(sources=[source], **stats), iter(events))


def write_log(path: Path, entries: list[dict[str, Any]]) -> str:
    path.write_bytes(("\n".join(json.dumps(entry) for entry in entries) + "\n").encode())
    return str(path)


def collect_entries(
    entries: list[dict[str, Any]],
    request_id: str = REQUEST,
    window: TimeWindow = NO_WINDOW,
    source: str = "api.jsonl",
    **stats: Any,
) -> LogEvidence:
    return collect_logs(log_input(entries, source=source, **stats), request_id, window)


def impact_for(evidence: LogEvidence, signature: str = REVOKED) -> SignatureImpact:
    return next(item for item in evidence.impact if item.signature == signature)


def reasons(impact: SignatureImpact) -> list[str]:
    return [gap.reason for gap in impact.gaps]


def fixture(request_id: str) -> LogEvidence:
    return collect_logs(read_logs([BILLING]), request_id, NO_WINDOW)


def check_result(
    name: str,
    rows: list[dict[str, Any]] | None = None,
    *,
    status: str | None = None,
    truncated: bool = False,
    summary: str | None = None,
) -> CheckResult:
    check = CHECKS[name]
    rows = rows or []
    if status is None:
        if check.kind == "lookup":
            status = "info" if rows else "fail"
        else:
            status = "fail" if rows else "pass"
    return CheckResult(
        name=name,
        pack=check.pack,
        description=check.description,
        status=status,
        summary=summary or f"{name} summary.",
        columns=list(rows[0]) if rows else [],
        rows=rows,
        row_count=len(rows),
        truncated=truncated,
    )


def key_row(**overrides: Any) -> dict[str, Any]:
    row: dict[str, Any] = {
        "key_prefix": "bk_juniper00",
        "key_id": "key_juniper_old",
        "label": "Old integration",
        "account_id": "acct_juniper",
        "account_name": "Juniper Dental Group",
        "account_status": "active",
        "created_at": datetime(2026, 1, 5, tzinfo=UTC),
        "expires_at": None,
        "revoked_at": datetime(2026, 7, 10, tzinfo=UTC),
        "key_status": "revoked",
    }
    row.update(overrides)
    return row


def invoice_row(**overrides: Any) -> dict[str, Any]:
    row: dict[str, Any] = {
        "invoice_id": "inv_juniper_1003",
        "number": "INV-1003",
        "account_id": "acct_juniper",
        "account_name": "Juniper Dental Group",
        "customer_id": "cus_juniper_main",
        "status": "open",
        "currency": "EUR",
        "total_cents": 14900,
        "line_total_cents": 14900,
        "succeeded_payments": 0,
        "failed_payments": 1,
        "due_date": None,
        "created_at": datetime(2026, 9, 28, tzinfo=UTC),
        "paid_at": None,
    }
    row.update(overrides)
    return row


class FakeChecks:
    def __init__(
        self,
        results: dict[str, CheckResult] | None = None,
        *,
        error: DatabaseError | None = None,
        read_only: bool = True,
    ) -> None:
        self.results = results or {}
        self.error = error
        self.read_only = read_only
        self.plans: list[list[PlannedCheck]] = []

    def __call__(self, dsn: SecretStr, plan: list[PlannedCheck], **_kwargs: Any) -> DbReport:
        self.plans.append(list(plan))
        if self.error is not None:
            raise self.error
        return DbReport(
            target="supportops_ro@127.0.0.1:5433/billing",
            session=SESSION.model_copy(update={"read_only": self.read_only}),
            results=[
                self.results.get(item.check.name, check_result(item.check.name)) for item in plan
            ],
        )

    @property
    def names(self) -> list[str]:
        return [item.check.name for plan in self.plans for item in plan]


def live_evidence(**fields: Any) -> LiveEvidence:
    values: dict[str, Any] = {"collected_at": NOW, "database": "checked"}
    values.update(fields)
    return LiveEvidence(**values)
