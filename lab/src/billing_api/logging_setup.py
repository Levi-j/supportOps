import json
import logging
import sys
from collections.abc import Iterable
from contextvars import ContextVar
from datetime import UTC, datetime
from typing import Any

SERVICE_NAME = "billing-api"
MASK = "***"

request_id_var: ContextVar[str | None] = ContextVar("request_id", default=None)

_STANDARD_ATTRIBUTES = frozenset(
    logging.LogRecord("", logging.INFO, "", 0, "", None, None).__dict__
) | {"message", "asctime", "color_message"}


class JsonFormatter(logging.Formatter):
    def __init__(self, secrets: Iterable[str] = ()) -> None:
        super().__init__()
        self.secrets = sorted({secret for secret in secrets if secret}, key=len, reverse=True)

    def format(self, record: logging.LogRecord) -> str:
        timestamp = datetime.fromtimestamp(record.created, tz=UTC)
        entry: dict[str, Any] = {
            "timestamp": timestamp.isoformat(timespec="milliseconds").replace("+00:00", "Z"),
            "level": record.levelname,
            "service": SERVICE_NAME,
            "logger": record.name,
            "message": record.getMessage(),
        }
        request_id = request_id_var.get()
        if request_id is not None:
            entry["request_id"] = request_id
        for key, value in record.__dict__.items():
            if key not in _STANDARD_ATTRIBUTES and not key.startswith("_"):
                entry[key] = value
        if record.exc_info and record.exc_info[0] is not None:
            entry["error_type"] = record.exc_info[0].__name__
            entry["error_message"] = str(record.exc_info[1])
            entry["stack_trace"] = self.formatException(record.exc_info)
        return json.dumps(self._hide_secrets(entry))

    def _hide_secrets(self, value: Any) -> Any:
        if isinstance(value, dict):
            return {key: self._hide_secrets(item) for key, item in value.items()}
        if isinstance(value, list | tuple):
            return [self._hide_secrets(item) for item in value]
        if value is None or isinstance(value, bool | int | float):
            return value
        text = str(value)
        for secret in self.secrets:
            text = text.replace(secret, MASK)
        return text


class JsonLogHandler(logging.Handler):
    def emit(self, record: logging.LogRecord) -> None:
        try:
            sys.stdout.write(self.format(record) + "\n")
            sys.stdout.flush()
        except Exception:
            self.handleError(record)


def configure_logging(level: str, secrets: Iterable[str] = ()) -> None:
    root = logging.getLogger()
    for handler in [h for h in root.handlers if isinstance(h, JsonLogHandler)]:
        root.removeHandler(handler)
    handler = JsonLogHandler()
    handler.setFormatter(JsonFormatter(secrets))
    root.addHandler(handler)
    root.setLevel(level)
    for name in ("uvicorn", "uvicorn.error"):
        uvicorn_logger = logging.getLogger(name)
        uvicorn_logger.handlers.clear()
        uvicorn_logger.propagate = True
    uvicorn_access = logging.getLogger("uvicorn.access")
    uvicorn_access.handlers.clear()
    uvicorn_access.propagate = False
