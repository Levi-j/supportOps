import json
import logging
from typing import Any

from billing_api.logging_setup import JsonFormatter


class JsonCapture(logging.Handler):
    def __init__(self) -> None:
        super().__init__()
        self.setFormatter(JsonFormatter())
        self.entries: list[dict[str, Any]] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.entries.append(json.loads(self.format(record)))

    def events(self, name: str) -> list[dict[str, Any]]:
        return [entry for entry in self.entries if entry.get("event_name") == name]

    def service_entries(self) -> list[dict[str, Any]]:
        return [entry for entry in self.entries if str(entry["logger"]).startswith("billing_api")]
