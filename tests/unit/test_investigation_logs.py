import io
import json
from datetime import timedelta

import pytest

from supportops.investigation import collect
from supportops.investigation.collect import collect_logs, signature_of
from supportops.investigation.models import Entities
from supportops.logs.analysis import TimeWindow, summarize
from supportops.logs.parser import LogEvent, parse_line, read_logs
from tests.unit.investigation_support import (
    ACCESS_401,
    BASE,
    BILLING,
    NO_WINDOW,
    REQUEST,
    REVOKED,
    access,
    collect_entries,
    fixture,
    impact_for,
    info,
    reasons,
    rejected,
    spanning,
)


def test_fixture_trace_summary_entities_and_service_start() -> None:
    evidence = fixture("demo-401-revoked")

    assert evidence.trace.found
    assert [step.event.event_name for step in evidence.trace.steps] == [
        "auth.rejected",
        "http.request",
    ]
    assert evidence.request is not None
    assert (evidence.request.method, evidence.request.path, evidence.request.status) == (
        "GET",
        "/v1/account",
        401,
    )
    assert evidence.entities.key_prefix == "bk_juniper00"
    assert evidence.entities.account_id == "acct_juniper"
    assert evidence.service_start is not None
    assert evidence.service_start.extra["database_host"] == "postgres"
    assert [item.signature for item in evidence.impact] == [REVOKED, ACCESS_401]


def test_fixture_payment_failure_extracts_invoice_and_payment() -> None:
    evidence = fixture("demo-500-a")

    assert evidence.entities.invoice_id == "inv_juniper_1005"
    assert evidence.entities.payment_ids == ["pay_3f2a9c1e5b7d4a60"]
    assert evidence.entities.account_id == "acct_juniper"


def test_fixture_shared_signature_is_recurring_but_only_a_lower_bound() -> None:
    impact = fixture("demo-500-a").impact

    assert [item.signature for item in impact] == [
        "unhandled_exception: InjectedFault: Lab fault payment_partial_commit: payment <id> "
        "was committed, then marking the invoice as paid failed.",
        "http.request (500): POST /v1/invoices/<id>/pay",
    ]
    for item in impact:
        assert item.other_request_ids == ["demo-500-b"]
        assert item.scope == "recurring"
        assert item.coverage == "partial"
        assert item.lower_bound
        assert "logs_start_inside_window" in reasons(item)
        assert "logs_end_inside_window" in reasons(item)


def test_fixture_isolated_looking_request_is_undetermined_with_partial_logs() -> None:
    impact = impact_for(fixture("demo-401-revoked"))

    assert impact.other_requests == 0
    assert impact.coverage == "partial"
    assert impact.scope == "undetermined"


def test_invoice_id_is_taken_from_the_path() -> None:
    assert fixture("demo-404").entities.invoice_id == "inv_kestrel_2001"


def test_request_without_an_error_has_no_impact() -> None:
    evidence = fixture("ticket-4711")

    assert evidence.trace.found
    assert evidence.impact == []
    assert any("no error signature" in note for note in evidence.notes)


def test_unknown_request_id() -> None:
    evidence = fixture("does-not-exist")

    assert not evidence.trace.found
    assert evidence.request is None
    assert evidence.impact == []
    assert evidence.entities == Entities()


def test_isolated_needs_complete_coverage() -> None:
    impact = impact_for(collect_entries(spanning(rejected(0), access(0.01))))

    assert impact.coverage == "complete"
    assert impact.gaps == []
    assert impact.requests == 1
    assert impact.other_requests == 0
    assert impact.scope == "isolated"
    assert not impact.lower_bound


def test_counts_distinct_requests_not_events() -> None:
    entries = spanning(
        rejected(0),
        access(0.01),
        rejected(1, "retry-a"),
        rejected(1.1, "retry-a"),
        rejected(1.2, "retry-a"),
    )

    impact = impact_for(collect_entries(entries))

    assert impact.events == 4
    assert impact.requests == 2
    assert impact.other_requests == 1
    assert impact.other_request_ids == ["retry-a"]
    assert impact.scope == "recurring"


def test_two_accounts_make_it_widespread() -> None:
    entries = spanning(rejected(0), access(0.01), rejected(2, "other", account="acct_kestrel"))

    impact = impact_for(collect_entries(entries))

    assert impact.accounts == 2
    assert impact.account_ids == ["acct_juniper", "acct_kestrel"]
    assert impact.scope == "widespread"


def test_window_is_fifteen_minutes_on_each_side_inclusive() -> None:
    entries = [
        info(-20),
        rejected(-15, "edge-before"),
        rejected(-15.02, "outside-before"),
        rejected(0),
        access(0.01),
        rejected(15, "edge-after"),
        rejected(15.02, "outside-after"),
        info(20),
    ]

    impact = impact_for(collect_entries(entries))

    assert impact.window_start == BASE - timedelta(minutes=15)
    assert impact.window_end == BASE + timedelta(minutes=15)
    assert impact.other_request_ids == ["edge-after", "edge-before"]


def test_other_signatures_are_not_counted() -> None:
    other = rejected(1, "other")
    other["reason"] = "unknown_key"

    impact = impact_for(collect_entries(spanning(rejected(0), access(0.01), other)))

    assert impact.other_requests == 0
    assert impact.scope == "isolated"


def test_access_signature_normalizes_ids_in_the_path() -> None:
    entries = spanning(
        access(0, status=404, path="/v1/invoices/inv_juniper_1001"),
        access(1, "other", status=404, path="/v1/invoices/inv_kestrel_2001"),
    )

    evidence = collect_entries(entries)

    assert [item.signature for item in evidence.impact] == [
        "http.request (404): GET /v1/invoices/<id>"
    ]
    assert evidence.impact[0].other_request_ids == ["other"]


def test_entries_without_request_id_make_coverage_partial() -> None:
    entries = spanning(rejected(0), access(0.01), rejected(1, request_id=None))

    impact = impact_for(collect_entries(entries))

    assert reasons(impact) == ["occurrences_without_request_id"]
    assert impact.events_without_request_id == 1
    assert impact.other_requests == 0
    assert impact.scope == "undetermined"


def test_occurrence_cap(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(collect, "MAX_OCCURRENCES", 2)
    entries = spanning(rejected(0), access(0.01), rejected(1, "late"))

    impact = impact_for(collect_entries(entries))

    assert reasons(impact) == ["occurrence_cap"]
    assert impact.other_requests == 0
    assert impact.scope == "undetermined"


def test_request_is_counted_even_when_its_entries_were_not_recorded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(collect, "MAX_OCCURRENCES", 1)
    entries = spanning(rejected(-1, "early"), rejected(0), access(0.01))

    impact = impact_for(collect_entries(entries))

    assert impact.requests == 2
    assert impact.other_request_ids == ["early"]
    assert impact.lower_bound


def test_docker_tail_limit() -> None:
    impact = impact_for(
        collect_entries(spanning(rejected(0), access(0.01)), truncated_sources=["docker:billing"])
    )

    assert reasons(impact) == ["docker_tail_limit"]
    assert "docker:billing" in impact.gaps[0].detail
    assert impact.scope == "undetermined"


def test_skipped_lines() -> None:
    impact = impact_for(
        collect_entries(
            spanning(rejected(0), access(0.01)),
            skipped={"not_json": 3},
            skipped_examples=["api.jsonl:2"],
        )
    )

    assert reasons(impact) == ["skipped_lines"]
    assert impact.scope == "undetermined"


def test_undated_entries_without_a_window() -> None:
    undated = {"level": "INFO", "message": "no time"}

    impact = impact_for(collect_entries(spanning(rejected(0), access(0.01), undated)))

    assert reasons(impact) == ["undated_entries"]


def test_undated_entries_excluded_by_a_window() -> None:
    entries = spanning(rejected(0), access(0.01), {"level": "INFO", "message": "no time"})
    window = TimeWindow(since=BASE - timedelta(hours=1))

    impact = impact_for(collect_entries(entries, window=window))

    assert reasons(impact) == ["undated_entries"]


def test_logs_starting_inside_the_window() -> None:
    impact = impact_for(collect_entries([rejected(0), access(0.01), info(20)]))

    assert reasons(impact) == ["logs_start_inside_window"]
    assert "15 min" in impact.gaps[0].detail


def test_logs_ending_inside_the_window() -> None:
    impact = impact_for(collect_entries([info(-20), rejected(0), access(0.01), info(3)]))

    assert reasons(impact) == ["logs_end_inside_window"]
    assert "12 min" in impact.gaps[0].detail


def test_since_clipping_the_window() -> None:
    window = TimeWindow(since=BASE - timedelta(minutes=5))

    impact = impact_for(collect_entries(spanning(rejected(0), access(0.01)), window=window))

    assert reasons(impact) == ["since_clips_window"]
    assert "10 min" in impact.gaps[0].detail


def test_until_clipping_the_window() -> None:
    window = TimeWindow(until=BASE + timedelta(minutes=5))

    impact = impact_for(collect_entries(spanning(rejected(0), access(0.01)), window=window))

    assert reasons(impact) == ["until_clips_window"]


def test_undated_request_counts_everything_and_is_partial() -> None:
    entries = spanning(rejected(None), access(None), rejected(60, "much-later"))

    impact = impact_for(collect_entries(entries))

    assert reasons(impact) == ["undated_request"]
    assert impact.window_start is None
    assert impact.other_request_ids == ["much-later"]
    assert impact.lower_bound


def test_reused_request_id_is_not_a_reliable_signature() -> None:
    entries = spanning(rejected(0), access(0.01), access(5))

    impact = impact_for(collect_entries(entries))

    assert reasons(impact) == ["request_id_reused"]
    assert impact.scope == "undetermined"


def test_stdin_is_read_once() -> None:
    data = "\n".join(json.dumps(entry) for entry in spanning(rejected(0), access(0.01)))
    stdin = io.BytesIO(data.encode())

    evidence = collect_logs(read_logs(["-"], stdin=stdin), REQUEST, NO_WINDOW)

    assert evidence.trace.found
    assert impact_for(evidence).scope == "isolated"
    assert stdin.read() == b""


def test_conflicting_entities_are_not_used() -> None:
    entries = [
        info(0, request_id=REQUEST, invoice_id="inv_a"),
        info(0.1, request_id=REQUEST, invoice_id="inv_b"),
        info(0.2, request_id=REQUEST, key_prefix="bk_short"),
    ]

    entities, notes = collect.extract_entities(collect_entries(entries).trace)

    assert entities.invoice_id is None
    assert entities.key_prefix is None
    assert any("more than one invoice (inv_a, inv_b)" in note for note in notes)
    assert any("API key prefix" in note and "expected format" in note for note in notes)


def test_service_start_is_the_latest_one_before_the_request() -> None:
    entries = [
        info(-30, event_name="app.started", database_host="postgres"),
        info(-10, event_name="app.started", database_host="localhost"),
        rejected(0),
        access(0.01),
        info(10, event_name="app.started", database_host="db"),
    ]

    evidence = collect_entries(entries)

    assert evidence.service_start is not None
    assert evidence.service_start.extra["database_host"] == "localhost"


def test_signature_redacts_secrets() -> None:
    parsed = parse_line(
        json.dumps({"level": "ERROR", "message": "connect failed password=hunter2"}), "x", 1
    )
    assert isinstance(parsed, LogEvent)

    signature = signature_of(parsed)

    assert signature is not None
    assert "hunter2" not in signature.describe()


def test_summary_patterns_match_investigation_signatures() -> None:
    patterns = {item.pattern for item in summarize(read_logs([BILLING]), NO_WINDOW).error_patterns}

    assert REVOKED in patterns
    assert impact_for(fixture("demo-401-revoked")).signature in patterns
