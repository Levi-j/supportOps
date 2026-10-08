import json
import logging
from collections.abc import Callable
from typing import Any

from fastapi import FastAPI

from billing_api.database import DatabaseStatus
from billing_api.logging_setup import JsonFormatter

FAKE_DB_PASSWORD = "UnitTestDbPw"

AppFactory = Callable[..., FastAPI]


class JsonCapture(logging.Handler):
    def __init__(self) -> None:
        super().__init__()
        self.setFormatter(JsonFormatter())
        self.entries: list[dict[str, Any]] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.entries.append(json.loads(self.format(record)))

    def events(self, name: str) -> list[dict[str, Any]]:
        return [entry for entry in self.entries if entry.get("event_name") == name]


HEALTHY = DatabaseStatus(up=True, latency_ms=2)
