import json
import logging
import sys

from billing_api.logging_setup import JsonFormatter, configure_logging, request_id_var


def make_record(message: str = "Something happened", **extra: object) -> logging.LogRecord:
    record = logging.LogRecord(
        "billing_api.test", logging.WARNING, __file__, 1, message, None, None
    )
    record.__dict__.update(extra)
    return record


def test_formats_one_json_object_with_standard_fields() -> None:
    entry = json.loads(JsonFormatter().format(make_record()))

    assert entry["level"] == "WARNING"
    assert entry["service"] == "billing-api"
    assert entry["logger"] == "billing_api.test"
    assert entry["message"] == "Something happened"
    assert entry["timestamp"].endswith("Z")
    assert "request_id" not in entry


def test_includes_extra_fields_and_the_current_request_id() -> None:
    token = request_id_var.set("req-123")
    try:
        entry = json.loads(
            JsonFormatter().format(make_record(event_name="auth.rejected", reason="unknown_key"))
        )
    finally:
        request_id_var.reset(token)

    assert entry["request_id"] == "req-123"
    assert entry["event_name"] == "auth.rejected"
    assert entry["reason"] == "unknown_key"


def test_exceptions_become_structured_fields() -> None:
    try:
        raise ValueError("boom")
    except ValueError:
        record = logging.LogRecord(
            "billing_api.test", logging.ERROR, __file__, 1, "Failed", None, sys.exc_info()
        )

    entry = json.loads(JsonFormatter().format(record))

    assert entry["error_type"] == "ValueError"
    assert entry["error_message"] == "boom"
    assert "Traceback" in entry["stack_trace"]


def test_drops_uvicorn_colour_codes() -> None:
    entry = json.loads(JsonFormatter().format(make_record(color_message="[36mhi[0m")))

    assert "color_message" not in entry


def test_uvicorn_access_log_is_replaced_by_the_middleware_access_log() -> None:
    configure_logging("INFO")

    assert logging.getLogger("uvicorn.access").propagate is False
    assert logging.getLogger("uvicorn.error").propagate is True
