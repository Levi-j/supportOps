from typing import Any

import pytest

from supportops.db.runner import CheckResult
from supportops.health import HealthReport, Verdict
from supportops.http_checks import HttpResult
from supportops.investigation.engine import evidence_candidates, number_evidence
from supportops.investigation.models import (
    Confidence,
    Finding,
    InvoiceState,
    LiveEvidence,
    LogEvidence,
    OtherInvoices,
)
from supportops.investigation.rules import Facts, apply_rules
from tests.unit.investigation_support import (
    access,
    check_result,
    collect_entries,
    event,
    info,
    invoice_row,
    key_row,
    live_evidence,
    rejected,
    spanning,
)

INVOICE = "inv_juniper_1003"
PAY_PATH = f"/v1/invoices/{INVOICE}/pay"


def run_rules(
    entries: list[dict[str, Any]],
    live: LiveEvidence | None = None,
    *,
    source: str = "api.jsonl",
    slow_request_ms: float = 1000,
) -> list[Finding]:
    logs = collect_entries(spanning(*entries), source=source)
    live = live or live_evidence(database="not_needed")
    findings, _ = number_evidence(
        apply_rules(Facts(logs, live, slow_request_ms)), evidence_candidates(logs, live)
    )
    return findings


def only(findings: list[Finding], rule: str) -> Finding:
    matching = [item for item in findings if item.rule == rule]
    assert len(matching) == 1, [item.rule for item in findings]
    return matching[0]


def with_checks(*results: CheckResult, **fields: Any) -> LiveEvidence:
    return live_evidence(checks=list(results), **fields)


@pytest.mark.parametrize(
    ("reason", "status", "rule"),
    [
        ("revoked_key", 401, "key_revoked"),
        ("expired_key", 401, "key_expired"),
        ("unknown_key", 401, "key_unknown"),
        ("missing_header", 401, "auth_header_problem"),
        ("malformed_header", 401, "auth_header_problem"),
        ("account_suspended", 403, "account_suspended"),
    ],
)
def test_auth_rejections_are_confirmed_by_the_log_and_status(
    reason: str, status: int, rule: str
) -> None:
    findings = run_rules([rejected(0, reason=reason), access(0.01, status=status)])

    item = only(findings, rule)
    assert item.confidence == Confidence.CONFIRMED
    assert item.evidence_ids == ["E1", "E2"]
    assert reason in item.summary
    assert item.inferences
    assert item.contradictions == []


def test_only_suspension_is_escalated() -> None:
    suspended = only(
        run_rules([rejected(0, reason="account_suspended"), access(0.01, status=403)]),
        "account_suspended",
    )
    revoked = only(run_rules([rejected(0), access(0.01)]), "key_revoked")

    assert suspended.escalation is not None
    assert suspended.escalation.team == "Account management"
    assert revoked.escalation is None


def test_auth_without_an_access_log_is_only_likely() -> None:
    item = only(run_rules([rejected(0)]), "key_revoked")

    assert item.confidence == Confidence.LIKELY
    assert any("No access-log entry" in caveat for caveat in item.caveats)


def test_auth_with_a_disagreeing_status_is_downgraded() -> None:
    item = only(run_rules([rejected(0), access(0.01, status=200)]), "key_revoked")

    assert item.confidence == Confidence.LIKELY
    assert "HTTP 200, not the 401" in item.contradictions[0]


def test_a_reused_request_id_is_never_confirmed() -> None:
    item = only(run_rules([rejected(0), access(0.01), access(3)]), "key_revoked")

    assert item.confidence == Confidence.LIKELY
    assert any("2 access-log entries" in caveat for caveat in item.caveats)


def test_database_corroboration_is_cited() -> None:
    live = with_checks(check_result("billing.api_key_status", [key_row()]))

    item = only(run_rules([rejected(0), access(0.01)], live), "key_revoked")

    assert item.confidence == Confidence.CONFIRMED
    assert item.evidence_ids == ["E1", "E2", "E3"]
    assert any("state when the investigation ran" in caveat for caveat in item.caveats)


def test_a_database_that_disagrees_downgrades_the_finding() -> None:
    live = with_checks(
        check_result("billing.api_key_status", [key_row(key_status="active", revoked_at=None)])
    )

    item = only(run_rules([rejected(0), access(0.01)], live), "key_revoked")

    assert item.confidence == Confidence.LIKELY
    assert "now shows key bk_juniper00 as active" in item.contradictions[0]


def test_a_missing_key_record_contradicts_a_revocation() -> None:
    live = with_checks(check_result("billing.api_key_status", []))

    item = only(run_rules([rejected(0), access(0.01)], live), "key_revoked")

    assert item.confidence == Confidence.LIKELY
    assert "No API key with prefix bk_juniper00" in item.contradictions[0]


def test_an_unknown_key_whose_prefix_exists_is_explained_not_contradicted() -> None:
    live = with_checks(
        check_result(
            "billing.api_key_status",
            [key_row(key_prefix="bk_juniper01", key_status="active", revoked_at=None)],
        )
    )

    item = only(
        run_rules([rejected(0, reason="unknown_key", prefix="bk_juniper01"), access(0.01)], live),
        "key_unknown",
    )

    assert item.confidence == Confidence.CONFIRMED
    assert item.contradictions == []
    assert any("does exist" in text for text in item.inferences)


def test_a_suspension_contradicted_by_an_active_account() -> None:
    live = with_checks(check_result("billing.api_key_status", [key_row(key_status="active")]))

    item = only(
        run_rules([rejected(0, reason="account_suspended"), access(0.01, status=403)], live),
        "account_suspended",
    )

    assert item.confidence == Confidence.LIKELY


def test_an_unchecked_key_record_is_a_caveat_not_a_contradiction() -> None:
    item = only(
        run_rules([rejected(0), access(0.01)], live_evidence(database="not_configured")),
        "key_revoked",
    )

    assert item.confidence == Confidence.CONFIRMED
    assert any("SUPPORTOPS_DB_URL is not set" in caveat for caveat in item.caveats)


def test_an_unrecognised_auth_reason_is_at_most_likely() -> None:
    item = only(run_rules([rejected(0, reason="rate_limited"), access(0.01)]), "auth_rejected")

    assert item.confidence == Confidence.LIKELY


def invalid_json(message: str) -> dict[str, Any]:
    return event(0, "request.invalid_json", error_message=message, error_position=1)


def test_malformed_json_with_stripped_quotes_suggests_the_client() -> None:
    item = only(
        run_rules(
            [
                invalid_json("Expecting property name enclosed in double quotes"),
                access(0.01, status=400, method="POST", path="/v1/customers"),
            ]
        ),
        "malformed_json",
    )

    assert item.confidence == Confidence.CONFIRMED
    assert "position 1" in item.summary
    assert "PowerShell 5.1" in item.inferences[0]
    assert item.escalation is None


def test_malformed_json_without_the_quote_pattern_makes_no_powershell_claim() -> None:
    item = only(
        run_rules([invalid_json("Expecting value"), access(0.01, status=400)]), "malformed_json"
    )

    assert "PowerShell" not in " ".join(item.inferences)


def test_validation_failure_lists_fields_without_values() -> None:
    item = only(
        run_rules(
            [
                event(
                    0,
                    "request.validation_failed",
                    fields=["body.email", "body.name"],
                    error_types=["missing", "string_too_long"],
                ),
                access(0.01, status=422),
            ]
        ),
        "validation_failed",
    )

    assert item.confidence == Confidence.CONFIRMED
    assert "body.email (missing), body.name (string_too_long)" in item.summary


def test_a_rejected_payment() -> None:
    item = only(
        run_rules(
            [
                event(0, "payment.rejected", invoice_id=INVOICE, invoice_status="paid"),
                access(0.01, status=409, method="POST", path=PAY_PATH),
            ]
        ),
        "invoice_not_payable",
    )

    assert item.confidence == Confidence.CONFIRMED
    assert "status was paid" in item.summary


def failed_payment(payment_id: str = "pay_1111aaaa2222bbbb") -> list[dict[str, Any]]:
    return [
        event(
            0,
            "payment.recorded",
            level="INFO",
            account_id="acct_juniper",
            invoice_id=INVOICE,
            payment_id=payment_id,
        ),
        event(
            0.01,
            "unhandled_exception",
            level="ERROR",
            error_type="InjectedFault",
            error_message="Lab fault payment_partial_commit",
            stack_trace="Traceback (most recent call last):\n  raise InjectedFault",
        ),
        access(0.02, status=500, method="POST", path=PAY_PATH, account="acct_juniper"),
    ]


def invoice_live(
    conditions: list[str], record: dict[str, Any] | None = None, **state: Any
) -> LiveEvidence:
    lookup = check_result("billing.invoice_lookup", [record or invoice_row()])
    return live_evidence(
        checks=[
            lookup,
            check_result("billing.payment_on_unpaid_invoice", [{"invoice_id": INVOICE}]),
        ],
        invoice=InvoiceState(
            invoice_id=INVOICE,
            lookup=state.pop("lookup", "found"),
            record=record or invoice_row(),
            conditions=conditions,
            **state,
        ),
    )


def test_a_partial_payment_is_confirmed_and_escalated() -> None:
    live = invoice_live(
        ["billing.payment_on_unpaid_invoice"],
        invoice_row(succeeded_payments=1),
        corroborated_by=["billing.payment_on_unpaid_invoice"],
    )

    findings = run_rules(failed_payment(), live)

    inconsistent = only(findings, "payment_invoice_inconsistent")
    assert [item.rule for item in findings] == [
        "payment_invoice_inconsistent",
        "unhandled_exception",
    ]
    assert inconsistent.confidence == Confidence.CONFIRMED
    assert inconsistent.escalation is not None
    assert inconsistent.escalation.severity == "high"
    assert "pay_1111aaaa2222bbbb" in inconsistent.summary
    assert any("charged" in text for text in inconsistent.inferences)
    assert len(inconsistent.evidence_ids) == 3


def test_an_inconsistency_without_a_logged_payment_is_only_likely() -> None:
    live = invoice_live(
        ["billing.duplicate_payments"], invoice_row(status="paid", succeeded_payments=2)
    )

    item = only(
        run_rules([access(0, status=200, path=f"/v1/invoices/{INVOICE}")], live),
        "payment_invoice_inconsistent",
    )

    assert item.confidence == Confidence.LIKELY
    assert any("link between the request" in caveat for caveat in item.caveats)


def test_contradictory_invoice_data_downgrades() -> None:
    live = invoice_live(
        ["billing.payment_on_unpaid_invoice"],
        invoice_row(succeeded_payments=1),
        contradictions=["the logged payment isn't in the database"],
    )

    item = only(run_rules(failed_payment(), live), "payment_invoice_inconsistent")

    assert item.confidence == Confidence.LIKELY
    assert item.contradictions == ["the logged payment isn't in the database"]


def test_other_invoices_are_mentioned_but_not_attributed() -> None:
    live = invoice_live(
        [],
        elsewhere=[
            OtherInvoices(
                check="billing.duplicate_payments", count=200, at_least=True, examples=["inv_x"]
            )
        ],
    )

    findings = run_rules(failed_payment(), live)

    assert "payment_invoice_inconsistent" not in [item.rule for item in findings]


def test_elsewhere_counts_are_a_caveat_with_lower_bounds() -> None:
    live = invoice_live(
        ["billing.payment_on_unpaid_invoice"],
        invoice_row(succeeded_payments=1),
        elsewhere=[
            OtherInvoices(
                check="billing.duplicate_payments", count=200, at_least=True, examples=["inv_x"]
            )
        ],
    )

    item = only(run_rules(failed_payment(), live), "payment_invoice_inconsistent")

    assert any(
        "also lists at least 200 other invoices (including inv_x); they are not attributed "
        "to this request." in caveat
        for caveat in item.caveats
    )


def test_a_single_other_invoice_is_named_without_awkward_plurals() -> None:
    live = invoice_live(
        ["billing.payment_on_unpaid_invoice"],
        invoice_row(succeeded_payments=1),
        elsewhere=[
            OtherInvoices(
                check="billing.payment_on_unpaid_invoice",
                count=1,
                at_least=False,
                examples=["inv_other"],
            )
        ],
    )

    item = only(run_rules(failed_payment(), live), "payment_invoice_inconsistent")

    assert (
        "billing.payment_on_unpaid_invoice also lists 1 other invoice (inv_other); it is not "
        "attributed to this request." in item.caveats
    )
    assert not any("(s)" in caveat for caveat in item.caveats)


def test_payment_and_exception_findings_share_one_retry_warning() -> None:
    live = invoice_live(["billing.payment_on_unpaid_invoice"], invoice_row(succeeded_payments=1))

    findings = run_rules(failed_payment(), live)

    steps = [step for item in findings for step in item.next_steps]
    retry = [step for step in steps if "not to retry" in step]
    assert len(retry) == 2
    assert len(set(retry)) == 1


@pytest.mark.parametrize("lookup", ["not_found", "other_account", "unavailable"])
def test_no_inconsistency_finding_without_the_invoices_own_record(lookup: str) -> None:
    live = invoice_live(["billing.payment_on_unpaid_invoice"], lookup=lookup)

    findings = run_rules(failed_payment(), live)

    assert "payment_invoice_inconsistent" not in [item.rule for item in findings]


def test_an_exception_after_a_payment_warns_about_retries() -> None:
    item = only(run_rules(failed_payment()), "unhandled_exception")

    assert item.confidence == Confidence.CONFIRMED
    assert item.escalation is not None
    assert item.escalation.severity == "high"
    assert any("partly succeeded" in text for text in item.inferences)
    assert item.next_steps[0].startswith("Ask the customer not to retry")


def test_an_exception_on_a_read_is_medium() -> None:
    item = only(
        run_rules(
            [
                event(0, "unhandled_exception", level="ERROR", error_type="KeyError"),
                access(0.01, status=500),
            ]
        ),
        "unhandled_exception",
    )

    assert item.escalation is not None
    assert item.escalation.severity == "medium"


def test_enabled_lab_faults_are_cited() -> None:
    start = info(-5, event_name="app.started", faults=["payment_partial_commit"])

    item = only(run_rules([start, *failed_payment()]), "unhandled_exception")

    assert any("lab fault(s) enabled" in caveat for caveat in item.caveats)


def lock_entries() -> list[dict[str, Any]]:
    return [
        event(0, "db.lock_timeout", detail="canceling statement due to lock timeout"),
        access(0.01, status=503, method="POST", path=PAY_PATH),
    ]


def test_a_lock_timeout_with_a_visible_blocker() -> None:
    holder = {
        "pid": 4242,
        "application": "invoice-backfill",
        "state": "idle in transaction",
        "transaction_seconds": 300,
        "locks_held": 3,
    }
    live = with_checks(
        check_result("pg.long_transactions", [holder]), check_result("pg.blocking_sessions", [])
    )

    item = only(run_rules(lock_entries(), live), "lock_contention")

    assert item.confidence == Confidence.CONFIRMED
    assert item.escalation is not None
    assert item.escalation.severity == "high"
    assert "invoice-backfill" in item.inferences[0]
    assert "linked to the request only by time" in item.inferences[0]


def test_a_lock_timeout_without_a_current_blocker() -> None:
    live = with_checks(
        check_result("pg.long_transactions", []), check_result("pg.blocking_sessions", [])
    )

    item = only(run_rules(lock_entries(), live), "lock_contention")

    assert item.escalation is not None
    assert item.escalation.severity == "medium"
    assert "probably finished" in item.inferences[0]


def test_a_lock_timeout_without_database_access() -> None:
    item = only(run_rules(lock_entries(), live_evidence(database="disabled")), "lock_contention")

    assert any("--no-db" in caveat for caveat in item.caveats)


def health_report(diagnosis: str, verdict: Verdict = Verdict.DEGRADED) -> HealthReport:
    liveness = HttpResult(
        method="GET", url="http://127.0.0.1:8001/health", request_id="x", duration_ms=1, status=200
    )
    return HealthReport(
        target="billing",
        api_url="http://127.0.0.1:8001/",
        verdict=verdict,
        diagnosis=diagnosis,
        summary=f"{diagnosis} summary",
        liveness=liveness,
    )


def outage_entries() -> list[dict[str, Any]]:
    return [
        event(0, "db.unavailable", level="ERROR", error="connection_refused", detail="refused"),
        access(0.01, status=503, path="/v1/invoices"),
    ]


@pytest.mark.parametrize(
    ("diagnosis", "rule", "team"),
    [
        ("api_cannot_reach_database", "api_cannot_reach_database", "Deployment owner / on-call"),
        ("database_outage", "database_outage", "Database / infrastructure on-call"),
        ("api_ready", "database_unavailable", "Engineering on-call"),
    ],
)
def test_database_unavailability_uses_the_health_diagnosis(
    diagnosis: str, rule: str, team: str
) -> None:
    live = live_evidence(database="not_needed", health=health_report(diagnosis))

    item = only(run_rules(outage_entries(), live), rule)

    assert item.confidence == Confidence.CONFIRMED
    assert item.escalation is not None
    assert item.escalation.team == team
    assert "E3" in item.evidence_ids


def test_localhost_inside_a_container_is_explained() -> None:
    start = info(-5, event_name="app.started", database_host="localhost", environment="lab")

    item = only(
        run_rules([start, *outage_entries()], source="docker:supportops-billing-api-1"),
        "database_unavailable",
    )

    assert any("Inside a container, localhost" in text for text in item.inferences)


def test_localhost_outside_docker_is_only_a_caveat() -> None:
    start = info(-5, event_name="app.started", database_host="127.0.0.1")

    item = only(run_rules([start, *outage_entries()]), "database_unavailable")

    assert not any("Inside a container" in text for text in item.inferences)
    assert any("If it runs in a container" in caveat for caveat in item.caveats)


def test_not_found_alone_is_only_possible() -> None:
    item = only(
        run_rules([access(0, status=404, path="/v1/customers/cus_x")]), "resource_not_found"
    )

    assert item.confidence == Confidence.POSSIBLE


def test_not_found_for_another_accounts_invoice() -> None:
    record = invoice_row(account_id="acct_kestrel")
    live = live_evidence(
        checks=[check_result("billing.invoice_lookup", [record])],
        invoice=InvoiceState(invoice_id=INVOICE, lookup="other_account", record=record),
    )

    item = only(
        run_rules(
            [access(0, status=404, path=f"/v1/invoices/{INVOICE}", account="acct_juniper")], live
        ),
        "resource_not_found",
    )

    assert item.confidence == Confidence.LIKELY
    assert "acct_kestrel" in item.summary
    assert any(caveat.startswith("Internal only:") for caveat in item.caveats)
    assert item.next_steps
    assert not any("acct_kestrel" in step for step in item.next_steps)


def test_a_success_is_confirmed_and_slowness_is_noted() -> None:
    item = only(
        run_rules([access(0, status=200, duration_ms=2500)], slow_request_ms=1000),
        "request_succeeded",
    )

    assert item.confidence == Confidence.CONFIRMED
    assert item.title == "The request succeeded, but slowly"


def test_a_success_with_warnings_is_not_called_a_success() -> None:
    findings = run_rules([event(0, "cache.miss_storm"), access(0.01, status=200)])

    assert findings == []


def test_an_unexplained_error_status_is_only_possible() -> None:
    item = only(run_rules([access(0, status=500)]), "http_error_unexplained")

    assert item.confidence == Confidence.POSSIBLE
    assert item.escalation is not None


def test_a_401_without_an_auth_event_is_only_possible() -> None:
    item = only(run_rules([access(0, status=401)]), "http_error_unexplained")

    assert item.confidence == Confidence.POSSIBLE
    assert item.escalation is None


def test_no_entries_means_no_findings() -> None:
    assert run_rules([], live_evidence(database="not_needed")) == []


def test_unrecognised_events_without_a_status_mean_no_findings() -> None:
    assert run_rules([event(0, "something.odd")]) == []


@pytest.mark.parametrize(
    "entries",
    [
        [rejected(0)],
        [rejected(0), access(0.01, status=500)],
        [rejected(0), access(0.01), access(1)],
        [access(0, status=401)],
        [access(0, status=404)],
        [invalid_json("x")],
    ],
)
def test_weak_or_conflicting_evidence_is_never_confirmed(entries: list[dict[str, Any]]) -> None:
    findings = run_rules(entries)

    assert findings
    assert all(item.confidence != Confidence.CONFIRMED for item in findings)


def test_every_finding_cites_evidence_that_exists() -> None:
    logs: LogEvidence = collect_entries(spanning(*failed_payment()))
    live = invoice_live(["billing.payment_on_unpaid_invoice"], invoice_row(succeeded_payments=1))

    findings, evidence = number_evidence(
        apply_rules(Facts(logs, live)), evidence_candidates(logs, live)
    )

    ids = {item.id for item in evidence}
    assert ids == {f"E{number}" for number in range(1, len(evidence) + 1)}
    for item in findings:
        assert item.evidence_ids
        assert set(item.evidence_ids) <= ids


def test_citing_unknown_evidence_is_a_bug() -> None:
    draft = Finding(rule="x", title="x", confidence="likely", summary="x", evidence_ids=["log:99"])

    with pytest.raises(LookupError):
        number_evidence([draft], {})
