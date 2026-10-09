import json
from typing import Any

import httpx
import pytest
from typer.testing import CliRunner, Result

from supportops.cli.main import app
from supportops.investigation import live
from tests.integration.support import (
    JUNIPER_KEY,
    JUNIPER_REVOKED_KEY,
    PASSWORDS,
    ApiStarter,
    LabDatabase,
    LiveApi,
    bearer,
    execute,
    fetch_all,
    free_port,
    open_transaction,
)

pytestmark = pytest.mark.integration

runner = CliRunner()


def send(
    api: LiveApi,
    method: str,
    path: str,
    request_id: str,
    *,
    key: str | None = None,
    content: bytes | None = None,
) -> httpx.Response:
    headers = {"X-Request-Id": request_id}
    if key is not None:
        headers |= bearer(key)
    if content is not None:
        headers["Content-Type"] = "application/json"
    return httpx.request(method, f"{api.url}{path}", headers=headers, content=content, timeout=15)


def investigate(
    api: LiveApi, database: LabDatabase | None, request_id: str, *args: str
) -> tuple[Result, dict[str, Any]]:
    env = {"COLUMNS": "200", "SUPPORTOPS_API_URL": api.url, "SUPPORTOPS_API_KEY": JUNIPER_KEY}
    if database is not None:
        env["SUPPORTOPS_DB_URL"] = database.url("supportops_ro")
    result = runner.invoke(
        app, ["investigate", request_id, str(api.log_file), "--json", *args], env=env
    )
    return result, json.loads(result.stdout) if result.stdout.startswith("{") else {}


def requests_seen(api: LiveApi) -> list[tuple[str, str]]:
    return [
        (entry["method"], entry["path"])
        for entry in api.logs()
        if entry.get("event_name") == "http.request"
    ]


def rules(report: dict[str, Any]) -> list[str]:
    return [item["rule"] for item in report["findings"]]


def test_a_request_without_a_key(live_api: LiveApi, billing_db: LabDatabase) -> None:
    assert send(live_api, "GET", "/v1/account", "it-inv-401").status_code == 401
    before = requests_seen(live_api)

    result, report = investigate(live_api, billing_db, "it-inv-401")

    assert result.exit_code == 0, result.output
    assert rules(report) == ["auth_header_problem"]
    assert report["findings"][0]["confidence"] == "confirmed"
    assert "missing_header" in report["findings"][0]["summary"]
    assert requests_seen(live_api) == before
    assert JUNIPER_KEY not in result.output


def test_a_revoked_key_is_corroborated_by_the_database(
    live_api: LiveApi, billing_db: LabDatabase
) -> None:
    response = send(live_api, "GET", "/v1/account", "it-inv-revoked", key=JUNIPER_REVOKED_KEY)
    assert response.status_code == 401

    result, report = investigate(live_api, billing_db, "it-inv-revoked")

    finding = report["findings"][0]
    database = [item for item in report["evidence"] if item["source"] == "database"]
    assert result.exit_code == 0, result.output
    assert finding["rule"] == "key_revoked"
    assert finding["confidence"] == "confirmed"
    assert finding["contradictions"] == []
    assert [item["reference"] for item in database] == ["billing.api_key_status"]
    assert database[0]["current_state"] is True
    assert "Key bk_juniper00 is revoked" in database[0]["summary"]
    assert JUNIPER_REVOKED_KEY not in result.output
    assert PASSWORDS["supportops_ro"] not in result.output


def test_a_malformed_json_post(live_api: LiveApi, billing_db: LabDatabase) -> None:
    response = send(
        live_api,
        "POST",
        "/v1/customers",
        "it-inv-400",
        key=JUNIPER_KEY,
        content=b"{name:Acme,email:billing@acme.example}",
    )
    assert response.status_code == 400

    result, report = investigate(live_api, billing_db, "it-inv-400")

    finding = report["findings"][0]
    assert result.exit_code == 0, result.output
    assert finding["rule"] == "malformed_json"
    assert finding["confidence"] == "confirmed"
    assert "PowerShell 5.1" in finding["inferences"][0]
    assert "billing@acme.example" not in result.output


def payment_count(database: LabDatabase) -> int:
    return int(fetch_all(database, "SELECT count(*) FROM billing.payments")[0][0])


def test_a_partial_payment_is_attributed_only_to_its_own_invoice(
    start_api: ApiStarter, billing_db: LabDatabase
) -> None:
    execute(
        billing_db,
        "INSERT INTO billing.payments (id, invoice_id, account_id, amount_cents, status) "
        "VALUES ('pay_it_unrelated', 'inv_kestrel_2001', 'acct_kestrel', 39500, 'succeeded')",
    )
    api = start_api(billing_db.url("billing_app"), faults="payment_partial_commit")
    response = send(api, "POST", "/v1/invoices/inv_juniper_1003/pay", "it-inv-pay", key=JUNIPER_KEY)
    assert response.status_code == 500
    payments = payment_count(billing_db)

    result, report = investigate(api, billing_db, "it-inv-pay")

    assert result.exit_code == 0, result.output
    assert payment_count(billing_db) == payments
    assert rules(report) == ["payment_invoice_inconsistent", "unhandled_exception"]
    inconsistent, exception = report["findings"]
    assert inconsistent["confidence"] == "confirmed"
    assert "inv_juniper_1003" in inconsistent["summary"]
    assert "inv_kestrel_2001" not in inconsistent["summary"]
    assert (
        "billing.duplicate_payments also lists 1 other invoice (inv_kestrel_2001); it is not "
        "attributed to this request." in inconsistent["caveats"]
    )
    assert inconsistent["escalation"]["severity"] == "high"
    state = report["live"]["invoice"]
    assert state["conditions"] == ["billing.payment_on_unpaid_invoice"]
    assert state["corroborated_by"] == ["billing.payment_on_unpaid_invoice"]
    assert state["contradictions"] == []
    assert any("payment_partial_commit" in caveat for caveat in exception["caveats"])


def test_a_lock_timeout_points_at_the_session_holding_the_lock(
    start_api: ApiStarter, billing_db: LabDatabase, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(live, "LONG_TRANSACTION_SECONDS", 0)
    api = start_api(billing_db.url("billing_app"), db_lock_timeout_ms="500")
    with open_transaction(
        billing_db,
        "SELECT id FROM billing.invoices WHERE id = 'inv_juniper_1003' FOR UPDATE",
        "invoice-backfill",
    ):
        response = send(
            api, "POST", "/v1/invoices/inv_juniper_1003/pay", "it-inv-lock", key=JUNIPER_KEY
        )
        assert response.status_code == 503
        result, report = investigate(api, billing_db, "it-inv-lock")

    assert result.exit_code == 0, result.output
    assert rules(report) == ["lock_contention"]
    finding = report["findings"][0]
    assert finding["confidence"] == "confirmed"
    assert finding["escalation"]["severity"] == "high"
    assert "invoice-backfill" in finding["inferences"][0]
    assert "idle in transaction" in finding["inferences"][0]
    assert report["live"]["invoice"]["conditions"] == []


def test_an_api_that_cannot_reach_its_database(
    start_api: ApiStarter, billing_db: LabDatabase
) -> None:
    api = start_api(f"postgresql://billing_app:unused@127.0.0.1:{free_port()}/billing")
    response = send(api, "GET", "/v1/account", "it-inv-503", key=JUNIPER_KEY)
    assert response.status_code == 503
    before = requests_seen(api)

    result, report = investigate(api, billing_db, "it-inv-503")

    assert result.exit_code == 0, result.output
    finding = report["findings"][0]
    assert finding["rule"] == "api_cannot_reach_database"
    assert finding["confidence"] == "confirmed"
    assert finding["escalation"]["team"] == "Deployment owner / on-call"
    assert any("database host '127.0.0.1'" in caveat for caveat in finding["caveats"])
    assert report["live"]["health"]["diagnosis"] == "api_cannot_reach_database"
    added = requests_seen(api)[len(before) :]
    assert sorted(added) == [("GET", "/health"), ("GET", "/health/ready")]
    assert JUNIPER_KEY not in result.output


def test_a_report_draft_from_a_real_investigation(
    live_api: LiveApi, billing_db: LabDatabase, tmp_path: Any
) -> None:
    send(live_api, "GET", "/v1/account", "it-inv-report", key=JUNIPER_REVOKED_KEY)
    target = tmp_path / "reports" / "it-inv-report.md"

    result, _ = investigate(live_api, billing_db, "it-inv-report", "--report", str(target))

    text = target.read_text(encoding="utf-8")
    assert result.exit_code == 0, result.output
    assert text.startswith("# Internal investigation draft: request `it-inv-report`")
    assert "not for direct customer distribution" in text
    assert "### 1. The API key was revoked (confidence: confirmed)" in text
    assert "**Current state:** Database checks ran at" in text
    assert "API health check ran" not in text
    assert JUNIPER_REVOKED_KEY not in text
    assert PASSWORDS["supportops_ro"] not in text
