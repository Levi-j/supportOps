from pathlib import Path

import pytest

from supportops_lab.scenarios import SCENARIOS, Scenario

ROOT = Path(__file__).resolve().parents[2]
FINDING_TITLES = {
    "key_revoked": "The API key was revoked",
    "malformed_json": "The request body wasn't valid JSON",
    "payment_invoice_inconsistent": "has inconsistent payment data",
    "unhandled_exception": "The API failed with an unhandled exception",
    "api_cannot_reach_database": "The API can't reach its database, but PostgreSQL is up",
    "lock_contention": "The request timed out waiting for a database lock",
}


@pytest.mark.parametrize("scenario", SCENARIOS, ids=lambda item: item.id)
def test_each_report_matches_its_scenario(scenario: Scenario) -> None:
    text = (ROOT / scenario.report).read_text(encoding="utf-8")

    assert text.startswith(f"# {scenario.id}: ")
    assert "**Simulated incident.**" in text
    assert f"supportops-lab start {scenario.id}" in text
    assert scenario.customer_report in text
    for request in scenario.requests:
        assert request.request_id in text
    for rule in scenario.expectation.rules:
        assert f"`{rule}`" in text
        assert FINDING_TITLES[rule] in text
    assert f"({scenario.expectation.confidence})" in text
    assert f"investigate {scenario.expectation.request_id}" in text
    for heading in (
        "## Customer report",
        "## Evidence",
        "## Root cause and confidence",
        "## Impact",
        "## Escalation",
        "## Customer update",
    ):
        assert heading in text


def test_reports_for_changed_labs_recover_with_the_scenario_reset() -> None:
    for scenario in SCENARIOS:
        if scenario.api_database_host is None and scenario.lock_holder is None:
            continue
        text = (ROOT / scenario.report).read_text(encoding="utf-8")
        assert "supportops-lab reset" in text
        if scenario.api_database_host is not None:
            assert f"database_host={scenario.api_database_host}" in text
        if scenario.lock_holder is not None:
            assert scenario.lock_holder.application_name in text
            assert "Lock:transactionid" in text
            assert "pg.blocking_sessions" in text


def test_the_index_links_every_report() -> None:
    index = (ROOT / "docs" / "incidents" / "README.md").read_text(encoding="utf-8")

    for scenario in SCENARIOS:
        assert f"({Path(scenario.report).name})" in index
    assert "simulated" in index.lower()
