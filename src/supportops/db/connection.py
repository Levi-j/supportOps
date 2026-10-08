import time
from collections.abc import Callable
from urllib.parse import unquote, urlsplit

import psycopg
from psycopg.conninfo import conninfo_to_dict
from psycopg.rows import TupleRow
from pydantic import BaseModel, SecretStr

from supportops.redaction import redact_dsn

APPLICATION_NAME = "supportops"
STATEMENT_TIMEOUT_MS = 5000
SERVER_ANSWERED_ERRORS = frozenset({"authentication_failed", "database_missing"})

Connection = psycopg.Connection[TupleRow]


class DatabaseProbe(BaseModel):
    target: str
    host: str | None = None
    reachable: bool
    server_answered: bool
    latency_ms: int
    error: str | None = None
    detail: str | None = None
    user: str | None = None
    server_version: str | None = None
    read_only: bool | None = None


def describe_dsn(dsn: str) -> str:
    try:
        parts = conninfo_to_dict(dsn)
    except psycopg.Error:
        return redact_dsn(dsn)
    user = parts.get("user") or "?"
    host = parts.get("host") or "localhost"
    port = parts.get("port") or "5432"
    database = parts.get("dbname") or "?"
    return f"{user}@{host}:{port}/{database}"


def dsn_host(dsn: str) -> str | None:
    try:
        host = conninfo_to_dict(dsn).get("host")
    except psycopg.Error:
        return None
    return str(host) if host else None


def connect_read_only(dsn: str, connect_timeout_seconds: float) -> Connection:
    connection = psycopg.connect(
        dsn,
        connect_timeout=max(1, round(connect_timeout_seconds)),
        application_name=APPLICATION_NAME,
        options=f"-c default_transaction_read_only=on -c statement_timeout={STATEMENT_TIMEOUT_MS}",
    )
    connection.read_only = True
    return connection


def probe_database(
    dsn: SecretStr, connect_timeout_seconds: float, clock: Callable[[], float] = time.perf_counter
) -> DatabaseProbe:
    raw = dsn.get_secret_value()
    target = describe_dsn(raw)
    host = dsn_host(raw)
    started = clock()
    try:
        with connect_read_only(raw, connect_timeout_seconds) as connection:
            row = connection.execute(
                "SELECT current_user, current_setting('server_version'),"
                " current_setting('transaction_read_only')"
            ).fetchone()
    except psycopg.Error as exc:
        error = classify_error(exc)
        return DatabaseProbe(
            target=target,
            host=host,
            reachable=False,
            server_answered=error in SERVER_ANSWERED_ERRORS,
            latency_ms=_elapsed_ms(clock, started),
            error=error,
            detail=_without_password(_first_line(exc), raw),
        )
    user, version, read_only = row if row is not None else (None, None, None)
    return DatabaseProbe(
        target=target,
        host=host,
        reachable=True,
        server_answered=True,
        latency_ms=_elapsed_ms(clock, started),
        user=user,
        server_version=version,
        read_only=read_only == "on" if read_only is not None else None,
    )


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


def _elapsed_ms(clock: Callable[[], float], started: float) -> int:
    return round((clock() - started) * 1000)


def _first_line(exc: Exception) -> str:
    lines = str(exc).strip().splitlines()
    return lines[0][:300] if lines else type(exc).__name__


def _without_password(text: str, dsn: str) -> str:
    try:
        password = urlsplit(dsn).password
    except ValueError:
        return text
    if not password:
        return text
    return text.replace(password, "***").replace(unquote(password), "***")
