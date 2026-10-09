from datetime import UTC, datetime
from typing import Any

import httpx
import pytest
from pydantic import SecretStr

from supportops import auth_checks
from supportops.auth_checks import (
    AuthCheckReport,
    DatabaseEvidence,
    check_authentication,
    inspect_key,
    lookup_key_record,
    read_credential,
)
from supportops.db.connection import DatabaseError, SessionInfo
from supportops.db.runner import CheckResult, DbReport
from supportops.errors import ConfigError, ExitCode
from supportops.http_checks import create_client
from supportops.settings import Settings
from supportops.targets import BILLING
from tests.unit.fake_api import FakeApi, respond

ACTIVE_KEY = "bk_juniper01_lab_only_not_a_real_key"
REVOKED_KEY = "bk_juniper00_lab_only_not_a_real_key"
DB_URL = "postgresql://supportops_ro:AuthTestPw@127.0.0.1:5433/billing"
ACCOUNT = {
    "id": "acct_juniper",
    "name": "Juniper Dental Group",
    "api_key": {"prefix": "bk_juniper01", "label": "Practice software"},
}
SESSION = SessionInfo(
    role="supportops_ro", database="billing", server_version="18.6", read_only=True, monitoring=True
)


def key_record(**overrides: Any) -> dict[str, Any]:
    record: dict[str, Any] = {
        "key_prefix": "bk_juniper01",
        "key_id": "key_juniper_main",
        "label": "Practice software",
        "account_id": "acct_juniper",
        "account_name": "Juniper Dental Group",
        "account_status": "active",
        "created_at": datetime(2026, 7, 9, tzinfo=UTC),
        "expires_at": None,
        "revoked_at": None,
        "key_status": "active",
    }
    record.update(overrides)
    return record


def stub_database(
    monkeypatch: pytest.MonkeyPatch, rows: list[dict[str, Any]] | None = None, **result: Any
) -> list[Any]:
    calls: list[Any] = []

    def fake_run_checks(dsn: SecretStr, plan: Any, **_kwargs: Any) -> DbReport:
        calls.append(plan)
        base: dict[str, Any] = {
            "name": "billing.api_key_status",
            "pack": "billing",
            "description": "x",
            "status": "info" if rows else "fail",
            "summary": "x",
            "rows": rows or [],
        }
        base.update(result)
        return DbReport(
            target="supportops_ro@127.0.0.1:5433/billing",
            session=SESSION,
            results=[CheckResult(**base)],
        )

    monkeypatch.setattr(auth_checks, "run_checks", fake_run_checks)
    return calls


def check(
    api: FakeApi, key: str | None = ACTIVE_KEY, db_url: str | None = DB_URL
) -> AuthCheckReport:
    settings = Settings(
        api_key=SecretStr(key) if key else None, db_url=SecretStr(db_url) if db_url else None
    )
    client = create_client(settings, transport=api.transport)
    return check_authentication(settings, BILLING, client)


def texts(report: AuthCheckReport, basis: str) -> str:
    return " ".join(finding.text for finding in report.findings if finding.basis == basis)


@pytest.mark.parametrize(
    ("raw", "problem"),
    [
        (f" {ACTIVE_KEY}", "start or end"),
        (f"{ACTIVE_KEY}\n", "line break"),
        (f'"{ACTIVE_KEY}"', "wrapped in quotes"),
        (f"'{ACTIVE_KEY}'", "wrapped in quotes"),
        ("bk_juniper01 lab_only_not_a_real_key", "contains spaces"),
        ("sk_live_0123456789abcdefghij", "doesn't start with bk_"),
        ("bk_juniper01", "expected shape"),
        ("bk_juniper01-lab-only-not-a-real-key", "expected shape"),
    ],
)
def test_key_hygiene_problems(raw: str, problem: str) -> None:
    hygiene = inspect_key(raw, "SUPPORTOPS_API_KEY", BILLING)

    assert any(problem in item for item in hygiene.problems)
    assert raw.strip() not in " ".join(hygiene.problems)


def test_a_well_formed_key_has_no_problems() -> None:
    hygiene = inspect_key(ACTIVE_KEY, "SUPPORTOPS_API_KEY", BILLING)

    assert hygiene.problems == []
    assert hygiene.masked == "bk_juniper01***"
    assert hygiene.prefix == "bk_juniper01"
    assert hygiene.sendable


def test_quoted_keys_still_have_a_usable_prefix() -> None:
    hygiene = inspect_key(f'"{ACTIVE_KEY}"', "x", BILLING)

    assert hygiene.prefix == "bk_juniper01"
    assert hygiene.sendable


def test_whitespace_keys_are_not_sent() -> None:
    assert not inspect_key(f"{ACTIVE_KEY}\r\n", "x", BILLING).sendable


@pytest.mark.parametrize("raw", [None, ""])
def test_missing_keys(raw: str | None) -> None:
    hygiene = inspect_key(raw, "SUPPORTOPS_API_KEY", BILLING)

    assert not hygiene.present
    assert hygiene.problems == ["No API key is set in SUPPORTOPS_API_KEY."]


def test_key_env_reads_another_variable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CUSTOMER_KEY", REVOKED_KEY)

    assert read_credential(Settings(), "CUSTOMER_KEY") == (
        REVOKED_KEY,
        "environment variable CUSTOMER_KEY",
    )


def test_key_env_must_be_a_variable_name() -> None:
    with pytest.raises(ConfigError, match="--key-env"):
        read_credential(Settings(), "bk_juniper01-not-a-name")


def test_authenticated_request(monkeypatch: pytest.MonkeyPatch) -> None:
    stub_database(monkeypatch, [key_record()])
    api = FakeApi().on("GET", "/v1/account", respond(200, ACCOUNT))

    report = check(api)

    sent = api.requests[0]
    assert report.outcome == "authenticated"
    assert report.exit_code == ExitCode.OK
    assert report.account is not None
    assert report.account.account_id == "acct_juniper"
    assert sent.headers["Authorization"] == f"Bearer {ACTIVE_KEY}"
    assert sent.headers["X-Request-Id"].startswith("supportops-auth-")
    assert api.methods == ["GET"]
    assert "Juniper Dental Group (acct_juniper)" in texts(report, "evidence")
    assert texts(report, "inference") == ""
    assert report.next_steps == []


def test_revoked_key_is_explained_without_overclaiming(monkeypatch: pytest.MonkeyPatch) -> None:
    stub_database(
        monkeypatch,
        [
            key_record(
                key_prefix="bk_juniper00",
                revoked_at=datetime(2026, 7, 10, tzinfo=UTC),
                key_status="revoked",
            )
        ],
    )
    api = FakeApi().on(
        "GET", "/v1/account", respond(401, {"code": "UNAUTHENTICATED"}, problem=True)
    )

    report = check(api, REVOKED_KEY)

    inference = texts(report, "inference")
    assert report.outcome == "rejected"
    assert report.exit_code == ExitCode.PROBLEM
    assert "same answer for a missing, malformed, unknown, revoked or expired key" in texts(
        report, "evidence"
    )
    assert "revoked 2026-07-10" in texts(report, "evidence")
    assert "suggests a possible explanation" in inference
    assert "doesn't prove the rest of the key matches" in inference
    assert "didn't read the API's logs" in inference
    assert "Trace the request ID to confirm the actual reason" in inference
    assert "confirms it" not in inference
    assert f"supportops logs trace {report.request.request_id}" in report.next_steps[0]  # type: ignore[union-attr]


def test_expired_key(monkeypatch: pytest.MonkeyPatch) -> None:
    stub_database(
        monkeypatch, [key_record(key_status="expired", expires_at=datetime(2026, 1, 1, tzinfo=UTC))]
    )
    api = FakeApi().on(
        "GET", "/v1/account", respond(401, {"code": "UNAUTHENTICATED"}, problem=True)
    )

    report = check(api)

    assert "would be explained by it being expired" in texts(report, "inference")
    assert "should show reason expired_key" in texts(report, "inference")


def test_active_prefix_with_a_401_points_at_the_rest_of_the_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stub_database(monkeypatch, [key_record()])
    api = FakeApi().on(
        "GET", "/v1/account", respond(401, {"code": "UNAUTHENTICATED"}, problem=True)
    )

    report = check(api, "bk_juniper01_but_the_rest_is_wrong")

    assert "rest of the key probably differs" in texts(report, "inference")


def test_unknown_prefix(monkeypatch: pytest.MonkeyPatch) -> None:
    stub_database(monkeypatch, [])
    api = FakeApi().on(
        "GET", "/v1/account", respond(401, {"code": "UNAUTHENTICATED"}, problem=True)
    )

    report = check(api, "bk_nosuchkey_lab_only_not_a_real_key")

    assert report.database.status == "not_found"
    assert "No key with prefix bk_nosuchkey" in texts(report, "evidence")
    assert "probably mistyped" in texts(report, "inference")


def test_suspended_account(monkeypatch: pytest.MonkeyPatch) -> None:
    stub_database(monkeypatch, [key_record(account_status="suspended")])
    api = FakeApi().on(
        "GET", "/v1/account", respond(403, {"code": "ACCOUNT_SUSPENDED"}, problem=True)
    )

    report = check(api)

    assert report.outcome == "account_suspended"
    assert "suspended (403 ACCOUNT_SUSPENDED)" in texts(report, "evidence")
    assert any("account decision" in step for step in report.next_steps)


@pytest.mark.parametrize(
    ("responder", "outcome", "exit_code"),
    [
        (respond(403, {"code": "FORBIDDEN"}, problem=True), "forbidden", ExitCode.PROBLEM),
        (respond(500, {"code": "INTERNAL_ERROR"}, problem=True), "server_error", ExitCode.PROBLEM),
        (
            respond(404, {"code": "RESOURCE_NOT_FOUND"}, problem=True),
            "unexpected_response",
            ExitCode.PROBLEM,
        ),
        (respond(200, text="<html>not an API</html>"), "unexpected_response", ExitCode.PROBLEM),
        (httpx.ConnectTimeout("timed out"), "unreachable", ExitCode.INCOMPLETE),
    ],
)
def test_other_outcomes(
    monkeypatch: pytest.MonkeyPatch, responder: Any, outcome: str, exit_code: ExitCode
) -> None:
    stub_database(monkeypatch, [key_record()])

    report = check(FakeApi().on("GET", "/v1/account", responder))

    assert report.outcome == outcome
    assert report.exit_code == exit_code


def test_wrong_target_is_pointed_out(monkeypatch: pytest.MonkeyPatch) -> None:
    stub_database(monkeypatch, [key_record()])

    report = check(FakeApi().on("GET", "/v1/account", respond(404, text="Not Found")))

    assert "SUPPORTOPS_API_URL may point to a different service" in texts(report, "evidence")


def test_api_and_database_disagreeing(monkeypatch: pytest.MonkeyPatch) -> None:
    stub_database(monkeypatch, [key_record(key_status="revoked")])

    report = check(FakeApi().on("GET", "/v1/account", respond(200, ACCOUNT)))

    assert "different environments" in texts(report, "inference")


def test_missing_key_sends_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = stub_database(monkeypatch, [])
    api = FakeApi()

    report = check(api, key=None)

    assert report.outcome == "not_sent"
    assert report.exit_code == ExitCode.USAGE
    assert api.requests == []
    assert calls == []
    assert report.database.status == "not_checked"


def test_whitespace_key_is_not_sent_but_still_looked_up(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = stub_database(monkeypatch, [key_record()])
    api = FakeApi()

    report = check(api, key=f"{ACTIVE_KEY} ")

    assert api.requests == []
    assert report.outcome == "not_sent"
    assert report.exit_code == ExitCode.PROBLEM
    assert len(calls) == 1
    assert "wasn't sent" in texts(report, "evidence")


def test_without_a_database_url(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = stub_database(monkeypatch, [])

    report = check(
        FakeApi().on("GET", "/v1/account", respond(401, {"code": "UNAUTHENTICATED"}, problem=True)),
        db_url=None,
    )

    assert calls == []
    assert report.database.status == "not_checked"
    assert "the API's log is the only place" in texts(report, "inference")


def test_unavailable_database_is_reported_not_guessed(monkeypatch: pytest.MonkeyPatch) -> None:
    def fail(*_args: Any, **_kwargs: Any) -> DbReport:
        raise DatabaseError("PostgreSQL at x didn't accept the connection (timeout).")

    monkeypatch.setattr(auth_checks, "run_checks", fail)

    evidence = lookup_key_record(Settings(db_url=SecretStr(DB_URL)), "bk_juniper01")

    assert evidence == DatabaseEvidence(
        status="unavailable",
        detail="PostgreSQL at x didn't accept the connection (timeout).",
    )


def test_query_errors_make_the_record_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    stub_database(monkeypatch, status="error", summary="The query was cancelled.")

    evidence = lookup_key_record(Settings(db_url=SecretStr(DB_URL)), "bk_juniper01")

    assert evidence.status == "unavailable"


def test_lookup_uses_the_catalog_check_with_a_bound_prefix(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = stub_database(monkeypatch, [key_record()])

    lookup_key_record(Settings(db_url=SecretStr(DB_URL)), "bk_juniper01")

    planned = calls[0][0]
    assert planned.check.name == "billing.api_key_status"
    assert planned.parameters == {"prefix": "bk_juniper01"}


def test_report_never_contains_the_key(monkeypatch: pytest.MonkeyPatch) -> None:
    stub_database(monkeypatch, [key_record()])
    api = FakeApi().on(
        "GET", "/v1/account", respond(401, {"code": "UNAUTHENTICATED"}, problem=True)
    )

    report = check(api, f'"{ACTIVE_KEY}"')

    assert ACTIVE_KEY not in report.model_dump_json()
