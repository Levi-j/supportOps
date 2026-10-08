import logging
from dataclasses import dataclass
from time import perf_counter
from typing import Any

import psycopg
from psycopg.conninfo import conninfo_to_dict

from billing_api.config import BillingSettings

APPLICATION_NAME = "billing-api"

logger = logging.getLogger("billing_api.database")


@dataclass(frozen=True)
class DatabaseStatus:
    up: bool
    latency_ms: int
    error: str | None = None


def connection_kwargs(settings: BillingSettings) -> dict[str, Any]:
    return {
        "connect_timeout": settings.db_connect_timeout_seconds,
        "application_name": APPLICATION_NAME,
        "options": (
            f"-c statement_timeout={settings.db_statement_timeout_ms} "
            f"-c lock_timeout={settings.db_lock_timeout_ms}"
        ),
    }


def describe_target(settings: BillingSettings) -> dict[str, str]:
    parts = conninfo_to_dict(settings.database_url.get_secret_value())
    return {
        "database_host": str(parts.get("host", "")),
        "database_port": str(parts.get("port", "5432")),
        "database_name": str(parts.get("dbname", "")),
        "database_user": str(parts.get("user", "")),
    }


def check_database(settings: BillingSettings) -> DatabaseStatus:
    started = perf_counter()
    try:
        with psycopg.connect(
            settings.database_url.get_secret_value(), **connection_kwargs(settings)
        ) as connection:
            connection.execute("SELECT 1")
    except psycopg.Error as exc:
        status = DatabaseStatus(
            up=False, latency_ms=_elapsed_ms(started), error=classify_error(exc)
        )
        logger.warning(
            "Database unavailable",
            extra={
                "event_name": "db.unavailable",
                "error": status.error,
                "detail": _first_line(exc),
            },
        )
        return status
    return DatabaseStatus(up=True, latency_ms=_elapsed_ms(started))


def classify_error(exc: psycopg.Error) -> str:
    message = str(exc).lower()
    if "password authentication failed" in message or (
        'role "' in message and "does not exist" in message
    ):
        return "authentication_failed"
    if 'database "' in message and "does not exist" in message:
        return "database_missing"
    if "connection refused" in message:
        return "connection_refused"
    if message.startswith("failed to resolve host") or "could not translate host name" in message:
        return "dns_failure"
    if "timeout" in message:
        return "timeout"
    return "error"


def _elapsed_ms(started: float) -> int:
    return round((perf_counter() - started) * 1000)


def _first_line(exc: Exception) -> str:
    lines = str(exc).strip().splitlines()
    return lines[0][:300] if lines else type(exc).__name__
