from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

import httpx
import pytest
from pydantic import SecretStr

from supportops import health
from supportops.db import runner
from supportops.db.catalog import CATALOG, CHECKS
from supportops.db.connection import DatabaseError, DatabaseProbe
from supportops.http_checks import create_client
from supportops.investigation import live
from supportops.investigation.live import (
    CONSISTENCY_CHECKS,
    LONG_TRANSACTION_SECONDS,
    collect_live,
    derive_conditions,
    plan_checks_for,
)
from supportops.investigation.models import LiveEvidence, LogEvidence
from supportops.settings import Settings
from supportops.targets import BILLING
from tests.unit.fake_api import FakeApi, lab_health, respond
from tests.unit.investigation_support import (
    DB_URL,
    NOW,
    SESSION,
    FakeChecks,
    access,
    check_result,
    collect_entries,
    event,
    invoice_row,
    rejected,
    spanning,
)

INVOICE = "inv_juniper_1003"
PAY_PATH = f"/v1/invoices/{INVOICE}/pay"


def revoked_trace() -> LogEvidence:
    return collect_entries(spanning(rejected(0), access(0.01)))


def payment_trace(payment_id: str = "pay_1111aaaa2222bbbb") -> LogEvidence:
    return collect_entries(
        spanning(
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
                error_message="boom",
            ),
            access(0.02, status=500, path=PAY_PATH, method="POST", account="acct_juniper"),
        )
    )


def lookup_trace() -> LogEvidence:
    return collect_entries(
        spanning(access(0, status=404, path=f"/v1/invoices/{INVOICE}", account="acct_juniper"))
    )


def lock_trace() -> LogEvidence:
    return collect_entries(
        spanning(
            event(0, "db.lock_timeout", detail="canceling statement due to lock timeout"),
            access(0.01, status=503, path=PAY_PATH, method="POST", account="acct_juniper"),
        )
    )


def outage_trace() -> LogEvidence:
    return collect_entries(
        spanning(
            event(0, "db.unavailable", level="ERROR", error="dns_failure", detail="no host"),
            access(0.01, status=503, path="/v1/invoices"),
        )
    )


def settings(db_url: str | None = DB_URL) -> Settings:
    return Settings(db_url=SecretStr(db_url) if db_url else None)


def run_live(
    logs: LogEvidence,
    monkeypatch: pytest.MonkeyPatch,
    fake: FakeChecks | None = None,
    *,
    api: FakeApi | None = None,
    use_database: bool = True,
    db_url: str | None = DB_URL,
) -> LiveEvidence:
    if fake is not None:
        monkeypatch.setattr(live, "run_checks", fake)
    api = api or FakeApi()
    configured = settings(db_url)
    with create_client(configured, transport=api.transport) as client:
        return collect_live(
            logs, configured, BILLING, client, use_database=use_database, now=lambda: NOW
        )


def planned(logs: LogEvidence) -> list[tuple[str, dict[str, Any]]]:
    return [(item.check.name, dict(item.parameters)) for item in plan_checks_for(logs)]


def test_a_key_prefix_plans_only_the_key_lookup() -> None:
    assert planned(revoked_trace()) == [("billing.api_key_status", {"prefix": "bk_juniper00"})]


def test_an_invoice_read_plans_only_the_scoped_lookup() -> None:
    assert planned(lookup_trace()) == [("billing.invoice_lookup", {"id": INVOICE, "number": None})]


def test_a_payment_failure_plans_the_lookup_and_all_four_consistency_checks() -> None:
    assert [name for name, _ in planned(payment_trace())] == [
        "billing.invoice_lookup",
        *CONSISTENCY_CHECKS,
    ]
    assert len(CONSISTENCY_CHECKS) == 4


def test_a_lock_timeout_plans_the_activity_checks() -> None:
    assert planned(lock_trace()) == [
        ("billing.invoice_lookup", {"id": INVOICE, "number": None}),
        *[(name, {}) for name in CONSISTENCY_CHECKS],
        ("pg.long_transactions", {"min_seconds": LONG_TRANSACTION_SECONDS}),
        ("pg.blocking_sessions", {}),
    ]


@pytest.mark.parametrize(
    "logs", [revoked_trace, payment_trace, lookup_trace, lock_trace, outage_trace]
)
def test_only_catalog_checks_are_ever_planned(logs: Any) -> None:
    for item in plan_checks_for(logs()):
        assert any(item.check is check for check in CATALOG)
        assert set(item.parameters) == {parameter.name for parameter in item.check.parameters}


def test_nothing_to_check_means_no_database_access(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakeChecks()
    logs = collect_entries(spanning(access(0, status=200)))

    evidence = run_live(logs, monkeypatch, fake)

    assert evidence.database == "not_needed"
    assert fake.plans == []
    assert evidence.health is None


class RecordingConnection:
    broken = False

    def __init__(self) -> None:
        self.statements: list[str] = []
        self.transactions = 0

    @contextmanager
    def transaction(self) -> Iterator[None]:
        self.transactions += 1
        yield

    def execute(self, sql: str, params: Any = None) -> Any:
        self.statements.append(sql)
        columns = ["invoice_id"]
        return type(
            "Cursor",
            (),
            {
                "description": [type("Column", (), {"name": name}) for name in columns],
                "fetchmany": lambda self, size: [],
            },
        )()


def test_only_catalog_sql_reaches_the_database(monkeypatch: pytest.MonkeyPatch) -> None:
    connection = RecordingConnection()

    @contextmanager
    def session(*_args: object) -> Iterator[RecordingConnection]:
        yield connection

    monkeypatch.setattr(runner, "read_only_session", session)
    monkeypatch.setattr(runner, "session_info", lambda _connection: SESSION)

    evidence = run_live(lock_trace(), monkeypatch)

    catalog_sql = {check.sql for check in CHECKS.values()}
    assert evidence.database == "checked"
    assert connection.statements
    assert all(statement in catalog_sql for statement in connection.statements)
    assert connection.transactions == len(connection.statements)


@pytest.mark.parametrize(
    ("overrides", "expected"),
    [
        ({}, []),
        ({"total_cents": 15000}, ["billing.invoice_total_mismatch"]),
        ({"status": "paid", "succeeded_payments": 0}, ["billing.paid_invoice_without_payment"]),
        ({"succeeded_payments": 1}, ["billing.payment_on_unpaid_invoice"]),
        ({"status": "paid", "succeeded_payments": 2}, ["billing.duplicate_payments"]),
        (
            {"succeeded_payments": 2, "total_cents": 1},
            [
                "billing.invoice_total_mismatch",
                "billing.payment_on_unpaid_invoice",
                "billing.duplicate_payments",
            ],
        ),
        ({"status": "paid", "succeeded_payments": 1}, []),
    ],
)
def test_conditions_come_from_the_invoices_own_record(
    overrides: dict[str, Any], expected: list[str]
) -> None:
    assert derive_conditions(invoice_row(**overrides)) == expected


def test_a_corroborated_inconsistency(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakeChecks(
        {
            "billing.invoice_lookup": check_result(
                "billing.invoice_lookup", [invoice_row(succeeded_payments=1)]
            ),
            "billing.payment_on_unpaid_invoice": check_result(
                "billing.payment_on_unpaid_invoice", [{"invoice_id": INVOICE}]
            ),
        }
    )

    state = run_live(payment_trace(), monkeypatch, fake).invoice

    assert state is not None
    assert state.lookup == "found"
    assert state.conditions == ["billing.payment_on_unpaid_invoice"]
    assert state.corroborated_by == ["billing.payment_on_unpaid_invoice"]
    assert state.contradictions == []
    assert state.elsewhere == []


def test_other_invoices_are_counted_but_never_attributed(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakeChecks(
        {
            "billing.invoice_lookup": check_result("billing.invoice_lookup", [invoice_row()]),
            "billing.duplicate_payments": check_result(
                "billing.duplicate_payments",
                [{"invoice_id": "inv_kestrel_2001"}, {"invoice_id": "inv_juniper_1001"}],
            ),
        }
    )

    state = run_live(payment_trace(), monkeypatch, fake).invoice

    assert state is not None
    assert state.conditions == []
    assert state.corroborated_by == []
    assert state.contradictions == []
    assert [(item.check, item.count, item.at_least) for item in state.elsewhere] == [
        ("billing.duplicate_payments", 2, False)
    ]
    assert state.elsewhere[0].examples == ["inv_juniper_1001", "inv_kestrel_2001"]


def test_a_truncated_global_check_never_clears_or_blames_the_invoice(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    others = [{"invoice_id": f"inv_other_{number:04d}"} for number in range(200)]
    fake = FakeChecks(
        {
            "billing.invoice_lookup": check_result(
                "billing.invoice_lookup", [invoice_row(succeeded_payments=1)]
            ),
            "billing.payment_on_unpaid_invoice": check_result(
                "billing.payment_on_unpaid_invoice", others, truncated=True
            ),
        }
    )

    state = run_live(payment_trace(), monkeypatch, fake).invoice

    assert state is not None
    assert state.conditions == ["billing.payment_on_unpaid_invoice"]
    assert state.contradictions == []
    assert state.corroborated_by == []
    assert any("first 200 rows" in note for note in state.notes)
    assert state.elsewhere[0].count == 200
    assert state.elsewhere[0].at_least is True


def test_a_truncated_check_without_the_condition_does_not_create_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    others = [{"invoice_id": f"inv_other_{number:04d}"} for number in range(200)]
    fake = FakeChecks(
        {
            "billing.invoice_lookup": check_result("billing.invoice_lookup", [invoice_row()]),
            "billing.invoice_total_mismatch": check_result(
                "billing.invoice_total_mismatch", others, truncated=True
            ),
        }
    )

    state = run_live(payment_trace(), monkeypatch, fake).invoice

    assert state is not None
    assert state.conditions == []
    assert state.contradictions == []


def test_a_complete_global_check_that_disagrees_is_a_contradiction(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = FakeChecks(
        {
            "billing.invoice_lookup": check_result(
                "billing.invoice_lookup", [invoice_row(succeeded_payments=1)]
            ),
            "billing.invoice_total_mismatch": check_result(
                "billing.invoice_total_mismatch", [{"invoice_id": INVOICE}]
            ),
        }
    )

    state = run_live(payment_trace(), monkeypatch, fake).invoice

    assert state is not None
    contradictions = " ".join(state.contradictions)
    assert "billing.payment_on_unpaid_invoice doesn't list" in contradictions
    assert "billing.invoice_total_mismatch lists" in contradictions


def test_logged_payment_ids_are_compared_with_the_database(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = FakeChecks(
        {
            "billing.invoice_lookup": check_result(
                "billing.invoice_lookup", [invoice_row(status="paid", succeeded_payments=2)]
            ),
            "billing.duplicate_payments": check_result(
                "billing.duplicate_payments",
                [{"invoice_id": INVOICE, "payment_ids": ["pay_a", "pay_b"]}],
            ),
        }
    )

    state = run_live(payment_trace("pay_not_in_db"), monkeypatch, fake).invoice

    assert state is not None
    assert state.corroborated_by == ["billing.duplicate_payments"]
    assert any("pay_not_in_db" in item for item in state.contradictions)


def test_an_invoice_of_another_account_is_not_attributed(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakeChecks(
        {
            "billing.invoice_lookup": check_result(
                "billing.invoice_lookup",
                [invoice_row(account_id="acct_kestrel", succeeded_payments=3)],
            ),
        }
    )

    state = run_live(payment_trace(), monkeypatch, fake).invoice

    assert state is not None
    assert state.lookup == "other_account"
    assert state.conditions == []
    assert state.record is not None
    assert "acct_kestrel" in state.notes[0]


def test_a_missing_invoice(monkeypatch: pytest.MonkeyPatch) -> None:
    state = run_live(lookup_trace(), monkeypatch, FakeChecks()).invoice

    assert state is not None
    assert state.lookup == "not_found"
    assert state.conditions == []


def test_a_failed_lookup_attributes_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakeChecks(
        {
            "billing.invoice_lookup": check_result(
                "billing.invoice_lookup", status="error", summary="Permission denied."
            ),
            "billing.payment_on_unpaid_invoice": check_result(
                "billing.payment_on_unpaid_invoice", [{"invoice_id": INVOICE}]
            ),
        }
    )

    evidence = run_live(payment_trace(), monkeypatch, fake)

    assert evidence.invoice is not None
    assert evidence.invoice.lookup == "unavailable"
    assert evidence.invoice.conditions == []
    assert any("billing.invoice_lookup couldn't run" in item for item in evidence.open_questions)


def test_no_db_skips_every_database_check(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakeChecks()

    evidence = run_live(payment_trace(), monkeypatch, fake, use_database=False)

    assert fake.plans == []
    assert evidence.database == "disabled"
    assert evidence.invoice is not None
    assert evidence.invoice.lookup == "unavailable"
    assert "--no-db" in evidence.notes[0]


def test_missing_database_url_is_an_open_question(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakeChecks()

    evidence = run_live(revoked_trace(), monkeypatch, fake, db_url=None)

    assert fake.plans == []
    assert evidence.database == "not_configured"
    assert "SUPPORTOPS_DB_URL is not set" in evidence.open_questions[0]


def test_an_unreachable_database_does_not_stop_the_investigation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = FakeChecks(error=DatabaseError("PostgreSQL at x didn't accept the connection."))

    evidence = run_live(revoked_trace(), monkeypatch, fake)

    assert evidence.database == "unavailable"
    assert evidence.database_detail == "PostgreSQL at x didn't accept the connection."
    assert evidence.checks == []
    assert "couldn't be checked" in evidence.open_questions[0]


def test_a_writable_session_is_flagged(monkeypatch: pytest.MonkeyPatch) -> None:
    evidence = run_live(revoked_trace(), monkeypatch, FakeChecks(read_only=False))

    assert any("NOT read-only" in note for note in evidence.notes)


def readiness_api() -> FakeApi:
    return (
        FakeApi()
        .on("GET", "/health", lab_health())
        .on(
            "GET",
            "/health/ready",
            respond(
                503,
                {"status": "not_ready", "checks": {"database": {"status": "down", "error": "x"}}},
            ),
        )
    )


def probe(reachable: bool = True) -> DatabaseProbe:
    return DatabaseProbe(
        target="supportops_ro@127.0.0.1:5433/billing",
        host="127.0.0.1",
        reachable=reachable,
        server_answered=reachable,
        latency_ms=4,
        error=None if reachable else "connection_refused",
    )


def test_database_errors_trigger_a_get_only_health_check(monkeypatch: pytest.MonkeyPatch) -> None:
    api = readiness_api()
    monkeypatch.setattr(health, "probe_database", lambda *_args, **_kwargs: probe())

    evidence = run_live(outage_trace(), monkeypatch, FakeChecks(), api=api)

    assert evidence.health is not None
    assert evidence.health.diagnosis == "api_cannot_reach_database"
    assert api.methods == ["GET", "GET"]
    assert all("authorization" not in request.headers for request in api.requests)


def test_no_db_keeps_the_health_check_off_the_database(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[object] = []
    monkeypatch.setattr(health, "probe_database", lambda *args, **_kwargs: calls.append(args))

    evidence = run_live(outage_trace(), monkeypatch, api=readiness_api(), use_database=False)

    assert evidence.health is not None
    assert evidence.health.database is None
    assert calls == []


@pytest.mark.parametrize(
    ("logs", "expected"),
    [
        (revoked_trace, False),
        (lock_trace, False),
        (outage_trace, True),
        (lambda: collect_entries(spanning(access(0, status=503))), True),
    ],
)
def test_health_is_checked_only_for_database_problems(
    monkeypatch: pytest.MonkeyPatch, logs: Any, expected: bool
) -> None:
    monkeypatch.setattr(health, "probe_database", lambda *_args, **_kwargs: probe())
    api = readiness_api()

    evidence = run_live(logs(), monkeypatch, FakeChecks(), api=api)

    assert (evidence.health is not None) is expected
    assert set(api.methods) <= {"GET"}


def test_unreachable_api_is_reported_not_raised(monkeypatch: pytest.MonkeyPatch) -> None:
    api = FakeApi().on("GET", "/health", httpx.ConnectError("refused"))
    monkeypatch.setattr(health, "probe_database", lambda *_args, **_kwargs: probe(False))

    evidence = run_live(outage_trace(), monkeypatch, FakeChecks(), api=api)

    assert evidence.health is not None
    assert evidence.health.verdict == "DOWN"
