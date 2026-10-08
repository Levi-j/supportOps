import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from supportops.errors import ConfigError
from supportops.logs.analysis import (
    SearchResult,
    TimeWindow,
    event_kind,
    normalize_message,
    parse_time,
    search,
    search_filters,
    summarize,
    time_window,
    trace,
)
from supportops.logs.parser import LogInput, parse_line, read_logs

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "logs"
BILLING = str(FIXTURES / "billing-api.jsonl")
NOW = datetime(2026, 10, 8, 9, 10, tzinfo=UTC)
NO_WINDOW = TimeWindow()


def billing() -> LogInput:
    return read_logs([BILLING])


def find(filters: dict[str, Any], limit: int = 50) -> SearchResult:
    return search(billing(), search_filters(**filters), NO_WINDOW, limit)


def ids(result: SearchResult) -> list[str | None]:
    return [event.request_id for event in result.events]


def write_lines(path: Path, entries: list[dict[str, object]]) -> str:
    path.write_bytes(("\n".join(json.dumps(entry) for entry in entries) + "\n").encode())
    return str(path)


def test_summary_counts_levels_events_and_statuses() -> None:
    summary = summarize(billing(), NO_WINDOW)

    assert summary.entries == 22
    assert summary.first_seen == datetime(2026, 10, 8, 9, 0, tzinfo=UTC)
    assert summary.last_seen == datetime(2026, 10, 8, 9, 9, 5, 120000, tzinfo=UTC)
    assert [(item.name, item.count) for item in summary.levels] == [
        ("INFO", 15),
        ("WARNING", 4),
        ("ERROR", 3),
    ]
    assert summary.event_names[0].name == "http.request"
    assert summary.event_names[0].count == 12
    assert summary.access_logs == 12
    assert [(item.name, item.count) for item in summary.statuses] == [
        ("200", 4),
        ("400", 1),
        ("401", 2),
        ("404", 1),
        ("422", 1),
        ("500", 2),
        ("503", 1),
    ]
    assert [(item.name, item.count) for item in summary.status_classes] == [
        ("2xx", 4),
        ("4xx", 5),
        ("5xx", 3),
    ]
    assert summary.notes == []


def test_slowest_requests_are_ordered() -> None:
    summary = summarize(billing(), NO_WINDOW)

    assert [(item.request_id, item.duration_ms) for item in summary.slowest] == [
        ("demo-503", 4012),
        ("demo-slow", 2350),
        ("demo-500-b", 51),
        ("demo-500-a", 48),
        ("ticket-4711", 14),
    ]


def test_repeated_errors_are_grouped_into_one_pattern() -> None:
    patterns = summarize(billing(), NO_WINDOW).error_patterns
    by_pattern = {pattern.pattern: pattern for pattern in patterns}

    fault = next(pattern for pattern in patterns if pattern.error_type == "InjectedFault")
    assert fault.count == 2
    assert fault.request_ids == ["demo-500-a", "demo-500-b"]
    assert fault.pattern == (
        "unhandled_exception: InjectedFault: Lab fault payment_partial_commit: payment <id> "
        "was committed, "
        "then marking the invoice as paid failed."
    )
    assert by_pattern["auth.rejected (revoked_key): API key rejected"].count == 1
    assert by_pattern["auth.rejected (missing_header): API key rejected"].count == 1
    assert by_pattern["db.unavailable (dns_failure): Database unavailable"].level == "ERROR"
    assert len(patterns) == 6


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Invoice inv_kestrel_2001 not found.", "Invoice <id> not found."),
        ("request 2cd27ed2-d036-4d44-9027-823303667267 failed", "request <uuid> failed"),
        ("token 3f2a9c1e5b7d4a60 expired", "token <hex> expired"),
        ("retry 3 of 5 after 250.5 ms", "retry <n> of <n> after <n> ms"),
        ("upstream answered 503, then 404", "upstream answered 503, then 404"),
        ("connection_refused", "connection_refused"),
        ("decade accepted", "decade accepted"),
    ],
)
def test_message_normalization(text: str, expected: str) -> None:
    assert normalize_message(text) == expected


def test_summary_of_empty_input_says_so(tmp_path: Path) -> None:
    empty = tmp_path / "empty.jsonl"
    empty.write_bytes(b"")

    summary = summarize(read_logs([str(empty)]), NO_WINDOW)

    assert summary.entries == 0
    assert summary.notes == ["No structured log entries were found."]


def test_summary_without_access_logs_explains_missing_figures(tmp_path: Path) -> None:
    source = write_lines(
        tmp_path / "app.jsonl", [{"timestamp": "2026-10-08T09:00:00Z", "message": "hello"}]
    )

    summary = summarize(read_logs([source]), NO_WINDOW)

    assert summary.statuses == []
    assert summary.slowest == []
    assert any("No HTTP access-log entries" in note for note in summary.notes)


def test_parse_time_accepts_durations_and_timestamps() -> None:
    assert parse_time("15m", NOW) == datetime(2026, 10, 8, 8, 55, tzinfo=UTC)
    assert parse_time("2h", NOW) == datetime(2026, 10, 8, 7, 10, tzinfo=UTC)
    assert parse_time("1d", NOW) == datetime(2026, 10, 7, 9, 10, tzinfo=UTC)
    assert parse_time("30s", NOW) == datetime(2026, 10, 8, 9, 9, 30, tzinfo=UTC)
    assert parse_time("2026-10-08T09:03:00Z", NOW) == datetime(2026, 10, 8, 9, 3, tzinfo=UTC)


@pytest.mark.parametrize(("since", "until"), [("soon", None), (None, "15 minutes"), ("5m", "10m")])
def test_bad_time_windows_are_usage_errors(since: str | None, until: str | None) -> None:
    with pytest.raises(ConfigError):
        time_window(since, until, now=NOW)


def test_since_is_inclusive_and_until_is_exclusive() -> None:
    window = time_window("2026-10-08T09:02:00.001Z", "2026-10-08T09:02:30.004Z", now=NOW)
    log_input = billing()

    result = search(log_input, search_filters(), window, 50)

    assert ids(result) == ["demo-401-revoked", "demo-401-revoked", "demo-401-missing"]
    assert log_input.stats.outside_window == 19


def test_entries_without_timestamps_are_left_out_of_a_window(tmp_path: Path) -> None:
    source = write_lines(
        tmp_path / "app.jsonl",
        [{"message": "no time"}, {"timestamp": "2026-10-08T09:05:00Z", "message": "timed"}],
    )
    log_input = read_logs([source])

    result = search(log_input, search_filters(), time_window("10m", None, now=NOW), 50)

    assert [event.message for event in result.events] == ["timed"]
    assert log_input.stats.excluded_without_timestamp == 1
    assert any("no usable timestamp" in note for note in result.notes)


@pytest.mark.parametrize(
    ("filters", "expected"),
    [
        ({"request_id": "demo-400"}, ["demo-400", "demo-400"]),
        ({"level": "error"}, ["demo-500-a", "demo-500-b", "demo-503"]),
        (
            {"level": "warn"},
            [
                "demo-401-revoked",
                "demo-401-missing",
                "demo-400",
                "demo-422",
                "demo-500-a",
                "demo-500-b",
                "demo-503",
            ],
        ),
        ({"event_names": ["auth.rejected"]}, ["demo-401-revoked", "demo-401-missing"]),
        ({"event_names": ["request.*"]}, ["demo-400", "demo-422"]),
        (
            {"event_names": ["db.unavailable", "payment.recorded"]},
            ["demo-500-a", "demo-500-b", "demo-503"],
        ),
        ({"statuses": ["401"]}, ["demo-401-revoked", "demo-401-missing"]),
        ({"statuses": ["5XX"]}, ["demo-500-a", "demo-500-b", "demo-503"]),
        ({"statuses": ["400", "422"]}, ["demo-400", "demo-422"]),
        ({"path": "/v1/customers/"}, ["demo-400", "demo-422"]),
        (
            {"path": "/v1/invoices"},
            ["demo-401-missing", "demo-404", "demo-500-a", "demo-500-b", "demo-503", "demo-slow"],
        ),
        ({"text": "REVOKED_KEY"}, ["demo-401-revoked"]),
        ({"text": "_record_payment_then_fail"}, ["demo-500-a", "demo-500-b"]),
        ({"statuses": ["4xx"], "path": "/v1/customers"}, ["demo-400", "demo-422"]),
        ({"level": "warning", "event_names": ["auth.*"], "text": "missing"}, ["demo-401-missing"]),
    ],
)
def test_search_filters(filters: dict[str, Any], expected: list[str]) -> None:
    result = find(filters)

    assert ids(result) == expected
    assert result.matched == len(expected)


def test_path_filter_does_not_match_similar_prefixes(tmp_path: Path) -> None:
    source = write_lines(
        tmp_path / "app.jsonl",
        [
            {"timestamp": "2026-10-08T09:00:00Z", "path": "/v1/invoices", "request_id": "a"},
            {"timestamp": "2026-10-08T09:00:01Z", "path": "/v1/invoicesx", "request_id": "b"},
        ],
    )

    result = search(read_logs([source]), search_filters(path="/v1/invoices"), NO_WINDOW, 50)

    assert ids(result) == ["a"]


def test_search_keeps_the_newest_matches_in_time_order() -> None:
    result = find({"statuses": ["2xx"]}, limit=2)

    assert result.matched == 4
    assert ids(result) == ["demo-slow", "54ddec43-570e-4817-b354-96f064b4566c"]
    assert result.notes == [
        "Showing the latest 2 of 4 matching entries. Use --limit or narrower filters to see others."
    ]


def test_search_without_matches() -> None:
    result = find({"request_id": "nope"})

    assert result.matched == 0
    assert result.events == []
    assert result.notes == ["No log entries matched the filters."]


@pytest.mark.parametrize(
    "filters", [{"statuses": ["4x"]}, {"statuses": ["600"]}, {"level": "loud"}]
)
def test_invalid_filters_are_usage_errors(filters: dict[str, Any]) -> None:
    with pytest.raises(ConfigError):
        search_filters(**filters)


def test_trace_orders_events_and_measures_offsets() -> None:
    result = trace(billing(), "demo-500-a", NO_WINDOW)

    assert result.found
    assert [step.event.event_name for step in result.steps] == [
        "payment.recorded",
        "unhandled_exception",
        "http.request",
    ]
    assert [step.offset_ms for step in result.steps] == [0, 7, 9]
    assert [step.kind for step in result.steps] == [None, "exception", "access"]
    assert result.elapsed_ms == 9
    assert result.highlights == ["exception"]
    assert result.access_log is not None
    assert (result.access_log.status, result.access_log.duration_ms) == (500, 48)
    assert result.notes == []


def test_trace_sorts_events_from_several_sources(tmp_path: Path) -> None:
    first = write_lines(
        tmp_path / "a.jsonl",
        [
            {
                "timestamp": "2026-10-08T09:00:00.030Z",
                "request_id": "r1",
                "event_name": "http.request",
                "method": "GET",
                "path": "/x",
                "status": 503,
                "duration_ms": 30,
            },
            {
                "timestamp": "2026-10-08T09:00:00.000Z",
                "request_id": "r1",
                "event_name": "request.start",
            },
        ],
    )
    second = write_lines(
        tmp_path / "b.jsonl",
        [
            {
                "timestamp": "2026-10-08T09:00:00.012Z",
                "request_id": "r1",
                "event_name": "db.unavailable",
                "level": "ERROR",
                "error": "timeout",
            }
        ],
    )

    result = trace(read_logs([first, second]), "r1", NO_WINDOW)

    assert [step.event.event_name for step in result.steps] == [
        "request.start",
        "db.unavailable",
        "http.request",
    ]
    assert [step.offset_ms for step in result.steps] == [0, 12, 30]
    assert result.highlights == ["database"]


def test_trace_that_finds_nothing() -> None:
    result = trace(billing(), "DEMO-400", NO_WINDOW)

    assert not result.found
    assert result.steps == []
    assert "IDs must match exactly, including case." in result.notes[0]


def test_trace_with_missing_timestamps_and_no_access_log(tmp_path: Path) -> None:
    source = write_lines(
        tmp_path / "app.jsonl",
        [
            {"request_id": "r1", "message": "undated"},
            {
                "timestamp": "2026-10-08T09:00:00Z",
                "request_id": "r1",
                "event_name": "auth.rejected",
                "level": "WARNING",
            },
        ],
    )

    result = trace(read_logs([source]), "r1", NO_WINDOW)

    assert [step.event.message for step in result.steps] == [None, "undated"]
    assert [step.offset_ms for step in result.steps] == [0, None]
    assert result.access_log is None
    assert result.highlights == ["authentication"]
    assert any("No access-log entry" in note for note in result.notes)
    assert any("listed last" in note for note in result.notes)


def test_trace_warns_about_reused_request_ids(tmp_path: Path) -> None:
    access = {"event_name": "http.request", "method": "GET", "path": "/", "status": 200}
    source = write_lines(
        tmp_path / "app.jsonl",
        [
            {"timestamp": "2026-10-08T09:00:00Z", "request_id": "r1", **access},
            {"timestamp": "2026-10-08T09:01:00Z", "request_id": "r1", **access},
        ],
    )

    result = trace(read_logs([source]), "r1", NO_WINDOW)

    assert any("reused" in note for note in result.notes)


def test_ecs_trace_uses_normalized_utc_times() -> None:
    result = trace(read_logs([str(FIXTURES / "orders-ecs.jsonl")]), "ord-req-2", NO_WINDOW)

    assert [step.offset_ms for step in result.steps] == [0, 10]
    assert [step.kind for step in result.steps] == ["exception", "access"]


@pytest.mark.parametrize(
    ("data", "kind"),
    [
        ({"event_name": "unhandled_exception"}, "exception"),
        ({"stack_trace": "Traceback"}, "exception"),
        ({"event_name": "db.lock_timeout"}, "database"),
        ({"event_name": "auth.rejected"}, "authentication"),
        ({"event_name": "request.invalid_json"}, "validation"),
        ({"event_name": "request.validation_failed"}, "validation"),
        ({"method": "GET", "path": "/", "status": 200}, "access"),
        ({"event_name": "invoice.paid"}, None),
    ],
)
def test_event_kinds(data: dict[str, object], kind: str | None) -> None:
    event = parse_line(json.dumps(data), "test", 1)

    assert not isinstance(event, str)
    assert event_kind(event) == kind
