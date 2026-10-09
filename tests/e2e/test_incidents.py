from pathlib import Path
from typing import Any

import pytest

from supportops_lab.lab import ScenarioLab
from supportops_lab.scenarios import LAB_KEYS, SCENARIOS, Scenario
from tests.e2e.support import investigate, read, secrets_of

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

    _scenario_specific(scenario.id, report)

    baseline = scenario_lab.reset()
    assert baseline.clean, baseline


def _scenario_specific(scenario_id: str, report: dict[str, Any]) -> None:
    summary = report["findings"][0]["summary"]
    if scenario_id == "INC-001":
        assert "revoked_key" in summary
        assert "bk_kestrel01" in summary
        key = check(report, "billing.api_key_status")["rows"][0]
        assert (key["key_status"], key["account_id"]) == ("revoked", "acct_kestrel")
    elif scenario_id == "INC-002":
        assert "double quotes" in summary
        assert "PowerShell 5.1" in report["findings"][0]["inferences"][0]
        assert report["live"]["database"] == "not_needed"
    else:
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
