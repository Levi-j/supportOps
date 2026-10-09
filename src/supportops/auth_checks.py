import os
import re
from typing import Any, Literal

import httpx
from pydantic import BaseModel, Field

from supportops.db.catalog import CHECKS
from supportops.db.connection import DatabaseError
from supportops.db.runner import PlannedCheck, run_checks
from supportops.errors import ConfigError, ExitCode
from supportops.http_checks import HttpResult, new_request_id, send
from supportops.redaction import mask_api_key
from supportops.settings import Settings
from supportops.targets import TargetProfile

Outcome = Literal[
    "authenticated",
    "rejected",
    "account_suspended",
    "forbidden",
    "server_error",
    "unexpected_response",
    "unreachable",
    "not_sent",
]
OUTCOME_LABELS: dict[str, str] = {
    "authenticated": "AUTHENTICATED",
    "rejected": "REJECTED (401)",
    "account_suspended": "ACCOUNT SUSPENDED (403)",
    "forbidden": "FORBIDDEN (403)",
    "server_error": "SERVER ERROR",
    "unexpected_response": "UNEXPECTED RESPONSE",
    "unreachable": "API UNREACHABLE",
    "not_sent": "NOT SENT",
}

_ENV_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


class KeyHygiene(BaseModel):
    source: str
    present: bool
    masked: str | None = None
    prefix: str | None = None
    problems: list[str] = Field(default_factory=list)
    sendable: bool = False


class AccountIdentity(BaseModel):
    account_id: str
    account_name: str | None = None
    key_prefix: str | None = None
    key_label: str | None = None


class DatabaseEvidence(BaseModel):
    status: Literal["found", "not_found", "unavailable", "not_checked"]
    record: dict[str, Any] | None = None
    detail: str | None = None


class Finding(BaseModel):
    basis: Literal["evidence", "inference"]
    text: str


class AuthCheckReport(BaseModel):
    api_url: str
    credential: KeyHygiene
    outcome: Outcome
    request: HttpResult | None = None
    account: AccountIdentity | None = None
    database: DatabaseEvidence
    findings: list[Finding] = Field(default_factory=list)
    next_steps: list[str] = Field(default_factory=list)

    @property
    def exit_code(self) -> ExitCode:
        if self.outcome == "authenticated":
            return ExitCode.OK
        if not self.credential.present:
            return ExitCode.USAGE
        if self.outcome == "unreachable":
            return ExitCode.INCOMPLETE
        return ExitCode.PROBLEM


def read_credential(settings: Settings, key_env: str | None) -> tuple[str | None, str]:
    if key_env is not None:
        if not _ENV_NAME.fullmatch(key_env):
            raise ConfigError("--key-env needs the name of an environment variable.")
        return os.environ.get(key_env), f"environment variable {key_env}"
    if settings.api_key is None:
        return None, "SUPPORTOPS_API_KEY"
    return settings.api_key.get_secret_value(), "SUPPORTOPS_API_KEY"


def inspect_key(raw: str | None, source: str, profile: TargetProfile) -> KeyHygiene:
    if not raw:
        return KeyHygiene(
            source=source,
            present=False,
            problems=[f"No API key is set in {source}."],
        )
    problems = []
    stripped = raw.strip()
    if stripped != raw:
        problems.append(
            "The key has spaces or line breaks at the start or end. They often come along when "
            "a key is copied from an email, a chat or a document."
        )
    if "\n" in raw or "\r" in raw:
        problems.append("The key contains a line break. Keys are always a single line.")
    core = stripped.strip("\"'")
    if core != stripped:
        problems.append(
            "The key is wrapped in quotes. Remove them; they're easy to copy in by accident, for "
            "example from a code snippet or a PowerShell variable."
        )
    if re.search(r"\s", core):
        problems.append("The key contains spaces. Keys never contain whitespace.")
    if not core.startswith(profile.api_key_prefix):
        problems.append(
            f"The key doesn't start with {profile.api_key_prefix}, which every key for this API "
            "does. It may be a key for a different service, or a placeholder."
        )
    elif not re.fullmatch(profile.api_key_pattern, core):
        problems.append(
            "The key doesn't have the expected shape: "
            f"{profile.api_key_prefix} followed by letters, digits or underscores "
            "(20 to 128 characters in total). It may be cut off or contain extra characters."
        )
    prefix = core[: profile.api_key_prefix_length]
    has_prefix = core.startswith(profile.api_key_prefix) and bool(
        re.fullmatch(r"[A-Za-z0-9_]+", prefix)
    )
    return KeyHygiene(
        source=source,
        present=True,
        masked=mask_api_key(core),
        prefix=prefix if has_prefix and len(prefix) == profile.api_key_prefix_length else None,
        problems=problems,
        sendable=not re.search(r"\s", raw),
    )


def lookup_key_record(settings: Settings, prefix: str) -> DatabaseEvidence:
    if settings.db_url is None:
        return DatabaseEvidence(
            status="not_checked",
            detail="SUPPORTOPS_DB_URL is not set, so the key's database record wasn't checked.",
        )
    plan = [PlannedCheck(CHECKS["billing.api_key_status"], {"prefix": prefix})]
    try:
        report = run_checks(
            settings.db_url, plan, connect_timeout_seconds=settings.connect_timeout_seconds
        )
    except DatabaseError as exc:
        return DatabaseEvidence(status="unavailable", detail=exc.message)
    result = report.results[0]
    if result.status == "error":
        return DatabaseEvidence(status="unavailable", detail=result.summary)
    if not result.rows:
        return DatabaseEvidence(
            status="not_found", detail=f"No key with prefix {prefix} in {report.target}."
        )
    return DatabaseEvidence(status="found", record=result.rows[0], detail=report.target)


def check_authentication(
    settings: Settings,
    profile: TargetProfile,
    client: httpx.Client,
    *,
    key_env: str | None = None,
) -> AuthCheckReport:
    raw, source = read_credential(settings, key_env)
    hygiene = inspect_key(raw, source, profile)
    result = None
    if raw and hygiene.sendable:
        result = send(
            client,
            "GET",
            profile.account_path,
            headers={"Authorization": f"{profile.auth_scheme} {raw}"},
            request_id=new_request_id("supportops-auth"),
        )
    outcome, account = _outcome(result)
    database = (
        lookup_key_record(settings, hygiene.prefix)
        if hygiene.prefix is not None
        else DatabaseEvidence(
            status="not_checked",
            detail="The key has no recognizable prefix, so there was nothing to look up.",
        )
    )
    findings = _findings(hygiene, result, outcome, account, database, profile)
    return AuthCheckReport(
        api_url=str(settings.api_url),
        credential=hygiene,
        outcome=outcome,
        request=result,
        account=account,
        database=database,
        findings=findings,
        next_steps=_next_steps(hygiene, result, outcome, database),
    )


def _outcome(result: HttpResult | None) -> tuple[Outcome, AccountIdentity | None]:
    if result is None:
        return "not_sent", None
    if result.failure is not None or result.status is None:
        return "unreachable", None
    if result.status == 200:
        body = result.json_body()
        if isinstance(body, dict) and isinstance(body.get("id"), str):
            nested = body.get("api_key")
            key: dict[str, Any] = nested if isinstance(nested, dict) else {}
            return "authenticated", AccountIdentity(
                account_id=body["id"],
                account_name=_text(body.get("name")),
                key_prefix=_text(key.get("prefix")),
                key_label=_text(key.get("label")),
            )
        return "unexpected_response", None
    if result.status == 401:
        return "rejected", None
    if result.status == 403:
        code = result.problem.code if result.problem else None
        return ("account_suspended" if code == "ACCOUNT_SUSPENDED" else "forbidden"), None
    if result.status >= 500:
        return "server_error", None
    return "unexpected_response", None


def _findings(
    hygiene: KeyHygiene,
    result: HttpResult | None,
    outcome: Outcome,
    account: AccountIdentity | None,
    database: DatabaseEvidence,
    profile: TargetProfile,
) -> list[Finding]:
    findings = [Finding(basis="evidence", text=problem) for problem in hygiene.problems]
    findings += _api_findings(hygiene, result, outcome, account, profile)
    findings += _database_findings(hygiene, database)
    findings += _inferences(hygiene, outcome, database)
    return findings


def _api_findings(
    hygiene: KeyHygiene,
    result: HttpResult | None,
    outcome: Outcome,
    account: AccountIdentity | None,
    profile: TargetProfile,
) -> list[Finding]:
    if result is None:
        if not hygiene.present:
            return []
        return [
            Finding(
                basis="evidence",
                text="The key wasn't sent to the API because it contains whitespace, "
                "which can't be sent in a header unchanged.",
            )
        ]
    if outcome == "unreachable" and result.failure is not None:
        return [
            Finding(basis="evidence", text=f"No response from the API. {result.failure.summary}")
        ]
    status = f"{result.status} {result.reason or ''}".strip()
    if outcome == "authenticated" and account is not None:
        name = (
            f"{account.account_name} ({account.account_id})"
            if account.account_name
            else account.account_id
        )
        prefix = f" for key prefix {account.key_prefix}" if account.key_prefix else ""
        return [
            Finding(
                basis="evidence",
                text=f"The API accepted the key and identified the account as {name}{prefix}.",
            )
        ]
    if outcome == "rejected":
        return [
            Finding(
                basis="evidence",
                text="The API answered 401 Unauthorized. It gives the same answer for a "
                "missing, malformed, unknown, revoked or expired key, so the response alone "
                "doesn't say which.",
            )
        ]
    if outcome == "account_suspended":
        return [
            Finding(
                basis="evidence",
                text="The API accepted the key but reports the account as suspended "
                "(403 ACCOUNT_SUSPENDED).",
            )
        ]
    if outcome == "forbidden":
        return [
            Finding(
                basis="evidence",
                text=f"The API answered {status}: the credentials are valid but not allowed "
                f"to call {profile.account_path}.",
            )
        ]
    if outcome == "server_error":
        return [
            Finding(
                basis="evidence",
                text=f"The API failed with {status}, so it neither accepted nor rejected the key.",
            )
        ]
    return [
        Finding(
            basis="evidence",
            text=f"The API answered {status} for {profile.account_path}, which isn't a response "
            "this check expects. SUPPORTOPS_API_URL may point to a different service.",
        )
    ]


def _database_findings(hygiene: KeyHygiene, database: DatabaseEvidence) -> list[Finding]:
    record = database.record
    if database.status == "found" and record is not None:
        dates = f"created {_day(record.get('created_at'))}"
        if record.get("revoked_at"):
            dates += f", revoked {_day(record['revoked_at'])}"
        if record.get("expires_at"):
            dates += f", expires {_day(record['expires_at'])}"
        return [
            Finding(
                basis="evidence",
                text=f"The database has a key with prefix {record.get('key_prefix')}: "
                f"'{record.get('label')}' for {record.get('account_name')} "
                f"({record.get('account_id')}, account {record.get('account_status')}), {dates}. "
                f"Its stored state is {record.get('key_status')}.",
            )
        ]
    if database.status == "not_found":
        return [
            Finding(
                basis="evidence",
                text=f"No key with prefix {hygiene.prefix} exists in the database that was "
                "checked.",
            )
        ]
    if database.status == "unavailable":
        return [
            Finding(
                basis="evidence",
                text=f"The key's database record couldn't be checked: {database.detail}",
            )
        ]
    return []


def _inferences(hygiene: KeyHygiene, outcome: Outcome, database: DatabaseEvidence) -> list[Finding]:
    record = database.record or {}
    key_status = record.get("key_status")
    if outcome == "authenticated":
        if database.status == "found" and key_status != "active":
            return [
                Finding(
                    basis="inference",
                    text=f"The API accepted the key although the database you checked marks it "
                    f"{key_status}. The API and SUPPORTOPS_DB_URL probably point to different "
                    "environments.",
                )
            ]
        return []
    if outcome != "rejected":
        return []
    if hygiene.problems:
        return [
            Finding(
                basis="inference",
                text="The credential problems listed above are a likely cause of the 401.",
            )
        ]
    if database.status == "not_found":
        return [
            Finding(
                basis="inference",
                text="No stored key even starts like this one, so the key is probably mistyped, "
                "made up, or from another environment.",
            )
        ]
    if database.status == "found" and key_status in ("revoked", "expired"):
        return [
            Finding(
                basis="inference",
                text=f"The database record suggests a possible explanation: if the configured key "
                f"is this stored key, the 401 would be explained by it being {key_status}. A "
                "matching prefix doesn't prove the rest of the key matches, and this check "
                "didn't read the API's logs. Trace the request ID to confirm the actual reason: "
                f"the auth.rejected entry should show reason {key_status}_key.",
            )
        ]
    if database.status == "found":
        return [
            Finding(
                basis="inference",
                text="A key with this prefix is active, yet the API rejected the request. The rest "
                "of the key probably differs from the stored one (for example a copying mistake "
                "after the first 12 characters), or the API uses a different database. The "
                "auth.rejected log entry tells which.",
            )
        ]
    return [
        Finding(
            basis="inference",
            text="Without the key's database record, the API's log is the only place that "
            "records the exact reason for the 401.",
        )
    ]


def _next_steps(
    hygiene: KeyHygiene,
    result: HttpResult | None,
    outcome: Outcome,
    database: DatabaseEvidence,
) -> list[str]:
    steps = []
    if hygiene.problems:
        steps.append(f"Fix the value in {hygiene.source} and run 'supportops auth check' again.")
    if result is not None and outcome not in ("authenticated", "unreachable"):
        steps.append(
            f"Read the reason the API logged for this request: "
            f"supportops logs trace {result.request_id}"
        )
    if outcome == "unreachable" and result is not None and result.failure is not None:
        steps.append(result.failure.hint)
        steps.append("Check the service with 'supportops health'.")
    key_status = (database.record or {}).get("key_status")
    if outcome == "rejected" and key_status in ("revoked", "expired"):
        steps.append(
            "If the logs confirm it, the client needs a current key. Only the account owner or "
            "an administrator can issue one; support should never reactivate a revoked key."
        )
    if outcome == "account_suspended":
        steps.append(
            "Suspension is an account decision, not a key problem. Route the case to the team "
            "that manages accounts and billing."
        )
    if outcome == "unexpected_response":
        steps.append("Check SUPPORTOPS_API_URL; it should point to the billing API.")
    return steps


def _text(value: Any) -> str | None:
    return value if isinstance(value, str) else None


def _day(value: Any) -> str:
    return value.date().isoformat() if hasattr(value, "date") else str(value)
