from pathlib import Path
from typing import Any

import pytest

from supportops_lab.lab import ScenarioLab, StartResult
from supportops_lab.ownership import RESERVED_HOST_PORTS
from supportops_lab.scenarios import LAB_KEYS, SCENARIOS, Scenario
from tests.e2e.support import investigate, read, secrets_of, supportops

pytestmark = pytest.mark.e2e


def check(report: dict[str, Any], name: str) -> dict[str, Any]:
    return next(item for item in report["live"]["checks"] if item["name"] == name)


def listed(report: dict[str, Any], name: str) -> list[str]:
    return [row["invoice_id"] for row in check(report, name)["rows"]]


@pytest.mark.parametrize("scenario", SCENARIOS, ids=lambda item: item.id)
def test_incident_reproduces_and_is_diagnosed(
    scenario_lab: ScenarioLab, scenario: Scenario, tmp_path: Path
) -> None:
    result = scenario_lab.start(scenario.id)

    assert result.reproduced, result.requests
    assert [item.echoed_request_id for item in result.requests] == [
        item.request_id for item in scenario.requests
    ]

    expected = scenario.expectation
    draft = tmp_path / f"{scenario.id}.md"
    run, report = investigate(scenario_lab, expected.request_id, "--report", str(draft))

    assert run.exit_code == 0, run.output
    findings = report["findings"]
    assert [item["rule"] for item in findings] == list(expected.rules)
    assert {item["confidence"] for item in findings} == {expected.confidence}
    if expected.escalation is None:
        assert all(item["escalation"] is None for item in findings)
    else:
        assert findings[0]["escalation"]["team"] == expected.escalation
        assert findings[0]["escalation"]["severity"] == "high"
    for reference in expected.evidence:
        assert any(
            item["reference"] == reference or reference in item["summary"]
            for item in report["evidence"]
        ), reference
    assert all(not item["contradictions"] for item in findings)

    text = read(draft)
    assert text.startswith(f"# Internal investigation draft: request `{expected.request_id}`")
    everything = run.output + text
    for secret in [*secrets_of(scenario_lab), *LAB_KEYS]:
        assert secret not in everything

    _scenario_specific(scenario.id, report, result, scenario_lab)

    baseline = scenario_lab.reset()
    assert baseline.clean, baseline
    assert baseline.checks["pg.long_transactions"] == "pass"
    assert baseline.checks["pg.blocking_sessions"] == "pass"


def _scenario_specific(
    scenario_id: str, report: dict[str, Any], result: StartResult, lab: ScenarioLab
) -> None:
    summary = report["findings"][0]["summary"]
    if scenario_id == "INC-004":
        _db_misconfigured(report, result, lab)
    elif scenario_id == "INC-005":
        _blocked_writes(report, result)
    elif scenario_id == "INC-001":
        assert "revoked_key" in summary
        assert "bk_kestrel01" in summary
        key = check(report, "billing.api_key_status")["rows"][0]
        assert (key["key_status"], key["account_id"]) == ("revoked", "acct_kestrel")
    elif scenario_id == "INC-002":
        assert "double quotes" in summary
        assert "PowerShell 5.1" in report["findings"][0]["inferences"][0]
        assert report["live"]["database"] == "not_needed"
    elif scenario_id == "INC-003":
        state = report["live"]["invoice"]
        assert state["conditions"] == [
            "billing.payment_on_unpaid_invoice",
            "billing.duplicate_payments",
        ]
        assert state["corroborated_by"] == state["conditions"]
        assert state["record"]["succeeded_payments"] == 2
        assert state["record"]["status"] == "open"
        assert listed(report, "billing.payment_on_unpaid_invoice") == ["inv_juniper_1003"]
        assert listed(report, "billing.duplicate_payments") == ["inv_juniper_1003"]
        payments = check(report, "billing.duplicate_payments")["rows"][0]["payment_ids"]
        assert report["logs"]["entities"]["payment_ids"][0] in payments
        assert any("lab fault" in caveat for caveat in report["findings"][1]["caveats"])
    else:
        raise AssertionError(f"No scenario-specific checks for {scenario_id}")


def _db_misconfigured(report: dict[str, Any], result: StartResult, lab: ScenarioLab) -> None:
    assert [item.problem_code for item in result.requests] == ["SERVICE_UNAVAILABLE"] * 2
    assert result.api_database_host == "localhost"
    settings = lab.settings()
    assert settings.api_url.port not in RESERVED_HOST_PORTS

    health_run, health = supportops(lab, "health", "--json")
    assert health_run.exit_code == 1, health_run.output
    assert (health["verdict"], health["diagnosis"]) == ("DEGRADED", "api_cannot_reach_database")
    assert health["database"]["reachable"] is True
    assert health["readiness"]["status"] == 503
    assert not any("docker compose" in step for step in health["next_steps"])
    connectivity, _ = supportops(lab, "db", "run", "db.connectivity")
    assert connectivity.exit_code == 0, connectivity.output

    finding = report["findings"][0]
    assert "connection_refused" in finding["summary"]
    assert any("localhost" in item and "container" in item for item in finding["inferences"])
    assert any("database_host=localhost" in item["summary"] for item in report["evidence"])
    assert report["live"]["health"]["diagnosis"] == "api_cannot_reach_database"


def _blocked_writes(report: dict[str, Any], result: StartResult) -> None:
    assert [(item.status, item.problem_code, item.retry_after) for item in result.requests] == [
        (503, "DATABASE_BUSY", "5"),
        (200, None, None),
        (503, "DATABASE_BUSY", "5"),
    ]
    capture = result.lock_wait
    assert capture is not None
    assert capture.request_id == "inc005-cust-01"
    [wait] = capture.rows
    assert (wait["blocked_application"], wait["blocked_role"]) == ("billing-api", "billing_app")
    assert (wait["blocking_pid"], wait["blocking_application"], wait["blocking_state"]) == (
        capture.holder_pid,
        "invoice-backfill",
        "idle in transaction",
    )
    assert wait["waiting_for"].startswith("Lock:")
    assert "FOR UPDATE" in wait["blocked_query"]
    assert wait["blocking_transaction_seconds"] >= 10

    holders = check(report, "pg.long_transactions")["rows"]
    assert [(row["pid"], row["application"], row["state"]) for row in holders] == [
        (capture.holder_pid, "invoice-backfill", "idle in transaction")
    ]
    assert check(report, "pg.blocking_sessions")["rows"] == []
    invoice = check(report, "billing.invoice_lookup")["rows"][0]
    assert (invoice["status"], invoice["succeeded_payments"]) == ("open", 0)
    finding = report["findings"][0]
    assert "invoice-backfill" in finding["inferences"][0]
    assert "doesn't appear to have taken a payment" in finding["inferences"][1]
    assert finding["next_steps"][-1].startswith("Ask the customer not to repeat the payment")
