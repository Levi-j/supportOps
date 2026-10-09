from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from supportops.db.catalog import CHECKS
from supportops.db.runner import PlannedCheck
from supportops.errors import ConfigError
from supportops.http_checks import create_client
from supportops.investigation import live as live_module
from supportops.investigation.collect import collect_logs
from supportops.investigation.engine import RULE_SETS, evidence_candidates, investigate
from supportops.investigation.live import collect_live, order_state, plan_checks_for
from supportops.investigation.models import Confidence, Finding, LiveEvidence, LogEvidence
from supportops.investigation.reporting import render_report
from supportops.investigation.rules import FALLBACK_RULES, RULES, Facts, apply_rules
from supportops.logs.parser import LogEvent, read_logs
from supportops.settings import Settings
from supportops.targets import BILLING, ORDERFLOW
from tests.unit.investigation_support import (
    NO_WINDOW,
    FakeChecks,
    access,
    check_result,
    collect_entries,
    live_evidence,
)

LOG = str(Path(__file__).resolve().parents[1] / "fixtures" / "logs" / "orderflow-ecs.jsonl")
DB_URL = "postgresql://supportops_orderflow_ro:FixturePw-5521@127.0.0.1:5432/orderflow"
ORDERFLOW_SETTINGS = Settings(target="orderflow", db_url=DB_URL)
CONSISTENCY = [
    "orderflow.order_total_mismatch",
    "orderflow.orders_without_items",
    "orderflow.inventory_mismatch",
]


def logs_for(request_id: str) -> LogEvidence:
    return collect_logs(read_logs([LOG]), request_id, NO_WINDOW)


def order_row(order_id: int = 3, **overrides: Any) -> dict[str, Any]:
    row: dict[str, Any] = {
        "order_id": order_id,
        "customer_id": 1,
        "status": "PENDING",
        "total_amount": 20,
        "version": 0,
        "item_count": 1,
        "items_total": 20,
        "units_ordered": 2,
        "units_reserved": 2,
        "units_restored": 0,
        "has_idempotency_key": False,
        "created_at": datetime(2026, 10, 8, 8, 0, tzinfo=UTC),
        "updated_at": datetime(2026, 10, 8, 8, 0, tzinfo=UTC),
    }
    row.update(overrides)
    return row


def order_live(row: dict[str, Any], *global_results: Any) -> LiveEvidence:
    results = [check_result("orderflow.order_lookup", [row]), *global_results]
    live = live_evidence(checks=results)
    live.order = order_state(str(row["order_id"]), live)
    return live


def run(request_id: str, live: LiveEvidence) -> list[Finding]:
    return apply_rules(Facts(logs_for(request_id), live), *RULE_SETS["orderflow"])


def only(findings: list[Finding], rule: str) -> Finding:
    matching = [item for item in findings if item.rule == rule]
    assert len(matching) == 1, [item.rule for item in findings]
    return matching[0]


def test_every_fixture_line_is_parsed_as_ecs_with_its_duration() -> None:
    log_input = read_logs([LOG])
    events: list[LogEvent] = list(log_input.events)

    assert log_input.stats.parsed == log_input.stats.lines == 13
    assert {event.format for event in events} == {"ecs"}
    access_logs = [event for event in events if event.event_name == "http.request"]
    assert all(event.duration_ms is not None for event in access_logs)
    error = next(event for event in events if event.level == "ERROR")
    assert error.error_type == "java.lang.IllegalStateException"
    assert error.stack_trace is not None


@pytest.mark.parametrize(
    ("request_id", "order", "product"),
    [
        ("of-place-201", "1", None),
        ("of-cancel-200", "2", None),
        ("of-confirm-500", "3", None),
        ("of-read-200", "3", None),
        ("of-stock-409", None, "2"),
        ("of-token-401", None, None),
    ],
)
def test_order_and_product_ids_come_from_paths_and_events(
    request_id: str, order: str | None, product: str | None
) -> None:
    entities = logs_for(request_id).entities

    assert (entities.order_id, entities.product_id) == (order, product)
    assert entities.invoice_id is None


def names(plan: list[PlannedCheck]) -> list[str]:
    return [item.check.name for item in plan]


def test_a_read_only_plans_the_order_lookup() -> None:
    plan = plan_checks_for(logs_for("of-read-200"), ORDERFLOW)

    assert names(plan) == ["orderflow.order_lookup"]
    assert plan[0].parameters == {"id": 3}


@pytest.mark.parametrize("request_id", ["of-cancel-200", "of-confirm-500", "of-place-201"])
def test_writes_and_failures_also_plan_the_consistency_checks(request_id: str) -> None:
    plan = plan_checks_for(logs_for(request_id), ORDERFLOW)

    assert names(plan) == ["orderflow.order_lookup", *CONSISTENCY]


def test_requests_without_an_order_plan_nothing() -> None:
    assert plan_checks_for(logs_for("of-token-401"), ORDERFLOW) == []


def test_targets_never_plan_each_others_checks() -> None:
    billing_payment = collect_entries(
        [access(0, status=500, method="POST", path="/v1/invoices/inv_juniper_1003/pay")]
    )

    assert plan_checks_for(billing_payment, ORDERFLOW) == []
    assert plan_checks_for(logs_for("of-cancel-200"), BILLING) == []
    assert all(
        name.startswith("billing.") or not name.startswith("orderflow.")
        for name in names(plan_checks_for(billing_payment, BILLING))
    )


def test_a_foreign_check_in_an_investigation_plan_is_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        live_module,
        "_orderflow_plan",
        lambda logs: [PlannedCheck(CHECKS["billing.duplicate_payments"])],
    )

    with pytest.raises(ConfigError, match="can't run against the 'orderflow' target"):
        plan_checks_for(logs_for("of-cancel-200"), ORDERFLOW)


def test_live_collection_derives_the_order_state(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakeChecks(
        {
            "orderflow.order_lookup": check_result("orderflow.order_lookup", [order_row(2)]),
        }
    )
    monkeypatch.setattr(live_module, "run_checks", fake)

    with create_client(ORDERFLOW_SETTINGS) as client:
        live = collect_live(
            logs_for("of-cancel-200"), ORDERFLOW_SETTINGS, ORDERFLOW, client, use_database=True
        )

    assert fake.names == ["orderflow.order_lookup", *CONSISTENCY]
    assert live.invoice is None
    assert live.order is not None
    assert (live.order.lookup, live.order.conditions) == ("found", [])


def test_without_a_database_url_the_order_check_is_an_open_question() -> None:
    settings = Settings(target="orderflow")

    with create_client(settings) as client:
        live = collect_live(
            logs_for("of-confirm-500"), settings, ORDERFLOW, client, use_database=True
        )

    assert live.database == "not_configured"
    assert live.order is not None
    assert live.order.lookup == "unavailable"
    assert "orderflow.order_lookup" in live.open_questions[0]
    assert run("of-confirm-500", live)[0].rule == "unhandled_exception"


def test_insufficient_stock_is_confirmed_without_escalation() -> None:
    item = only(run("of-stock-409", live_evidence(database="not_needed")), "insufficient_stock")

    assert item.confidence == Confidence.CONFIRMED
    assert item.escalation is None
    assert "product 2" in item.summary
    assert "-60" in item.summary


def test_a_failed_login_is_confirmed() -> None:
    item = only(run("of-login-401", live_evidence(database="not_needed")), "login_failed")

    assert item.confidence == Confidence.CONFIRMED
    assert all(rule.rule != "credentials_rejected" for rule in run("of-login-401", live_evidence()))


def test_a_rejected_token_is_only_possible() -> None:
    item = only(run("of-token-401", live_evidence(database="not_needed")), "credentials_rejected")

    assert item.confidence == Confidence.POSSIBLE
    assert "doesn't log why it rejected a bearer token" in item.caveats[0]
    assert any("supportops auth check" in step for step in item.next_steps)


def test_a_corroborated_inconsistency_on_a_failed_write_is_confirmed_current_state() -> None:
    row = order_row(3, total_amount=99)
    live = order_live(
        row,
        check_result("orderflow.order_total_mismatch", [{"order_id": 3}]),
        check_result("orderflow.orders_without_items", []),
    )

    findings = run("of-confirm-500", live)

    item = only(findings, "order_inconsistent")
    assert [finding.rule for finding in findings] == ["order_inconsistent", "unhandled_exception"]
    assert item.confidence == Confidence.CONFIRMED
    assert "inconsistent in the database now" in item.summary
    assert "its total differs from the sum of its items" in item.summary
    cause = item.inferences[0]
    assert cause.startswith("Cause, possible association only")
    assert "should have changed nothing" in cause
    assert "probably" not in cause.lower()
    assert "likely" not in cause.lower()
    assert item.escalation is not None
    assert (item.escalation.team, item.escalation.severity) == ("Engineering / data owner", "high")
    assert item.next_steps[0].startswith("Don't correct order or stock data")
    assert "check:orderflow.order_total_mismatch" in item.evidence_ids
    assert any(key.startswith("log:") for key in item.evidence_ids)


def test_an_uncorroborated_inconsistency_is_only_likely() -> None:
    live = order_live(order_row(3, total_amount=99))

    item = only(run("of-confirm-500", live), "order_inconsistent")

    assert item.confidence == Confidence.LIKELY
    assert any("didn't corroborate" in caveat for caveat in item.caveats)


def test_a_contradicting_global_check_downgrades_the_finding() -> None:
    live = order_live(
        order_row(3, total_amount=99),
        check_result("orderflow.order_total_mismatch", []),
        check_result("orderflow.orders_without_items", []),
    )

    item = only(run("of-confirm-500", live), "order_inconsistent")

    assert item.confidence == Confidence.LIKELY
    assert item.contradictions


def test_movement_conditions_rest_on_the_lookup_and_a_normal_event_proves_nothing() -> None:
    row = order_row(2, status="CANCELLED", units_restored=1)
    live = order_live(
        row,
        check_result("orderflow.order_total_mismatch", []),
        check_result("orderflow.orders_without_items", []),
    )

    item = only(run("of-cancel-200", live), "order_inconsistent")

    assert item.confidence == Confidence.CONFIRMED
    assert "ORDER_CANCELLED stock movements don't match its status" in item.summary
    cause = item.inferences[0]
    assert cause.startswith("Cause not established")
    assert "normal order.cancelled" in cause
    assert "at least as plausible" in cause


def test_a_read_cannot_have_caused_the_inconsistency() -> None:
    live = order_live(order_row(3, units_reserved=0))

    item = only(run("of-read-200", live), "order_inconsistent")

    assert "only read order 3" in item.inferences[0]


def test_a_consistent_order_raises_no_finding() -> None:
    live = order_live(order_row(3))

    findings = run("of-read-200", live)

    assert [finding.rule for finding in findings] == ["request_succeeded"]


def test_billing_keeps_its_rule_set() -> None:
    assert RULE_SETS["billing"] == (RULES, FALLBACK_RULES)
    assert not {rule.__name__ for rule in RULE_SETS["orderflow"][0]} & {
        "auth_rejection",
        "payment_invoice_inconsistent",
        "lock_contention",
        "database_unavailable",
    }


def test_a_full_orderflow_investigation_renders_without_billing_evidence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = FakeChecks(
        {
            "orderflow.order_lookup": check_result(
                "orderflow.order_lookup", [order_row(3, total_amount=99)]
            ),
            "orderflow.order_total_mismatch": check_result(
                "orderflow.order_total_mismatch", [{"order_id": 3}]
            ),
        }
    )
    monkeypatch.setattr(live_module, "run_checks", fake)

    with create_client(ORDERFLOW_SETTINGS) as client:
        result = investigate(
            read_logs([LOG]),
            "of-confirm-500",
            NO_WINDOW,
            ORDERFLOW_SETTINGS,
            ORDERFLOW,
            client,
        )

    report = render_report(result)
    assert [item.rule for item in result.findings] == ["order_inconsistent", "unhandled_exception"]
    assert "| Order | `3` |" in report
    assert "billing." not in report
    assert "FixturePw-5521" not in report
    assert {item.reference for item in result.evidence} >= {
        "orderflow.order_lookup",
        "orderflow.order_total_mismatch",
    }


def test_the_health_evidence_names_the_targets_endpoints() -> None:
    from supportops.health import HealthReport, Verdict
    from supportops.http_checks import HttpResult

    health = HealthReport(
        target="orderflow",
        api_url="http://127.0.0.1:8080/",
        verdict=Verdict.DEGRADED,
        diagnosis="health_down_database_reachable",
        summary="The service reports DOWN.",
        liveness=HttpResult(
            method="GET", url="http://127.0.0.1:8080/x", request_id="r", duration_ms=1, status=200
        ),
    )
    live = live_evidence(database="not_needed", health=health)

    candidates = evidence_candidates(logs_for("of-read-200"), live)

    assert candidates["health"].reference == (
        "GET /actuator/health/liveness, GET /actuator/health and a PostgreSQL probe"
    )
