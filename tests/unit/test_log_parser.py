import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from supportops.logs.parser import LogEvent, normalize_level, parse_line, read_logs

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "logs"


def events_from(name: str) -> list[LogEvent]:
    return list(read_logs([str(FIXTURES / name)]).events)


def parsed(data: dict[str, object]) -> LogEvent:
    result = parse_line(json.dumps(data), "test", 1)
    assert isinstance(result, LogEvent)
    return result


def test_billing_api_access_log_is_normalized() -> None:
    event = next(
        event for event in events_from("billing-api.jsonl") if event.request_id == "demo-404"
    )

    assert event.format == "flat"
    assert event.timestamp == datetime(2026, 10, 8, 9, 4, 0, 10000, tzinfo=UTC)
    assert event.timestamp_text == "2026-10-08T09:04:00.010Z"
    assert event.level == "INFO"
    assert event.service == "billing-api"
    assert event.logger == "billing_api.http"
    assert event.event_name == "http.request"
    assert (event.method, event.path, event.status) == ("GET", "/v1/invoices/inv_kestrel_2001", 404)
    assert event.duration_ms == 10
    assert event.account_id == "acct_juniper"
    assert event.extra == {"user_agent": "supportops/0.1.0"}
    assert (event.source, event.line) == (str(FIXTURES / "billing-api.jsonl"), 12)


def test_unmapped_fields_stay_as_original_evidence() -> None:
    event = next(
        event
        for event in events_from("billing-api.jsonl")
        if event.request_id == "demo-401-revoked" and event.event_name == "auth.rejected"
    )

    assert event.level == "WARNING"
    assert event.extra == {"reason": "revoked_key", "key_prefix": "bk_juniper00"}


def test_exception_details_are_kept() -> None:
    event = next(
        event
        for event in events_from("billing-api.jsonl")
        if event.event_name == "unhandled_exception"
    )

    assert event.error_type == "InjectedFault"
    assert event.error_message is not None
    assert "payment pay_3f2a9c1e5b7d4a60 was committed" in event.error_message
    assert event.stack_trace is not None
    assert event.stack_trace.startswith("Traceback (most recent call last):")


def test_ecs_nested_and_dotted_fields_are_normalized() -> None:
    events = events_from("orders-ecs.jsonl")
    started, warning, access, error, checkout = events

    assert {event.format for event in events} == {"ecs"}
    assert started.level == "INFO"
    assert started.service == "orders-api"
    assert started.logger == "com.example.orders.OrdersApplication"
    assert started.extra == {"ecs": {"version": "8.11"}}
    assert warning.level == "WARNING"
    assert warning.request_id == "ord-req-1"
    assert warning.event_name == "order.validation_failed"
    assert (access.method, access.path, access.status) == ("POST", "/api/v1/orders", 400)
    assert access.duration_ms == 12
    assert error.timestamp == datetime(2026, 10, 8, 9, 10, 6, 250000, tzinfo=UTC)
    assert error.timestamp_text == "2026-10-08T11:10:06.250+02:00"
    assert error.error_type == "java.lang.IllegalStateException"
    assert error.error_message == "Order 42 has no items"
    assert error.stack_trace is not None
    assert "OrderService.java:88" in error.stack_trace
    assert (checkout.status, checkout.duration_ms) == (500, 153)


@pytest.mark.parametrize(
    ("line", "reason"),
    [
        ("INFO:     Started server process [1]", "not_json"),
        ('{"timestamp": "2026-10-08T09:20:01.000Z", "message": "truncated', "not_json"),
        ("[1, 2, 3]", "not_an_object"),
        ("42", "not_an_object"),
        ('"just a string"', "not_an_object"),
        ("null", "not_an_object"),
    ],
)
def test_lines_that_are_not_log_objects_are_reported(line: str, reason: str) -> None:
    assert parse_line(line, "test", 1) == reason


def test_compose_prefix_is_removed() -> None:
    result = parse_line('billing-api-1  | {"message": "hi", "request_id": "r1"}', "test", 1)

    assert isinstance(result, LogEvent)
    assert result.request_id == "r1"


def test_wrong_types_are_kept_as_evidence_instead_of_guessed() -> None:
    event = parsed(
        {
            "timestamp": "yesterday",
            "level": 5,
            "status": "abc",
            "duration_ms": "fast",
            "message": "x",
        }
    )

    assert event.timestamp is None
    assert event.timestamp_text == "yesterday"
    assert event.level is None
    assert event.status is None
    assert event.duration_ms is None
    assert event.extra == {"level": 5, "status": "abc", "duration_ms": "fast"}


def test_missing_fields_are_simply_absent() -> None:
    event = parsed({"message": "no timestamp or level"})

    assert event.timestamp is None
    assert event.timestamp_text is None
    assert event.level is None
    assert event.request_id is None
    assert event.extra == {}


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("404", 404),
        (503, 503),
        (99, None),
        (600, None),
        (True, None),
    ],
)
def test_status_values(value: object, expected: int | None) -> None:
    assert parsed({"status": value}).status == expected


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("2026-10-08T09:00:00Z", datetime(2026, 10, 8, 9, 0, tzinfo=UTC)),
        ("2026-10-08T11:00:00+02:00", datetime(2026, 10, 8, 9, 0, tzinfo=UTC)),
        ("2026-10-08 09:00:00", datetime(2026, 10, 8, 9, 0, tzinfo=UTC)),
        (1791450000, datetime(2026, 10, 8, 9, 0, tzinfo=UTC)),
        (1791450000000, datetime(2026, 10, 8, 9, 0, tzinfo=UTC)),
    ],
)
def test_timestamps_become_timezone_aware_utc(value: object, expected: datetime) -> None:
    assert parsed({"timestamp": value}).timestamp == expected


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("warn", "WARNING"),
        ("WARNING", "WARNING"),
        ("Error", "ERROR"),
        ("FATAL", "CRITICAL"),
        ("trace", "TRACE"),
        ("notice", "NOTICE"),
    ],
)
def test_level_normalization(value: str, expected: str) -> None:
    assert normalize_level(value) == expected


def test_read_statistics_count_every_kind_of_line() -> None:
    log_input = read_logs([str(FIXTURES / "malformed.jsonl")])
    events = list(log_input.events)
    stats = log_input.stats

    assert [event.request_id for event in events] == [
        "demo-mixed",
        "demo-prefixed",
        "demo-odd",
        "demo-mixed",
        None,
    ]
    assert stats.lines == 12
    assert stats.blank_lines == 2
    assert stats.parsed == 5
    assert stats.skipped == {"not_json": 2, "not_an_object": 3}
    assert stats.skipped_examples[0] == f"{FIXTURES / 'malformed.jsonl'}:2"
    assert stats.naive_timestamps == 1


def test_sources_are_read_in_order() -> None:
    log_input = read_logs(
        [str(FIXTURES / "orders-ecs.jsonl"), str(FIXTURES / "billing-api-utf16.jsonl")]
    )
    events = list(log_input.events)

    assert [event.format for event in events] == ["ecs"] * 5 + ["flat"] * 5
    assert log_input.stats.parsed == 10
