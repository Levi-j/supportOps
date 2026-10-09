import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from urllib.parse import unquote, urlsplit

import psycopg
from psycopg.conninfo import conninfo_to_dict
from psycopg.rows import TupleRow
from pydantic import BaseModel, SecretStr

from supportops.errors import SupportOpsError
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


def connect_read_only(
    dsn: str, connect_timeout_seconds: float, statement_timeout_ms: int = STATEMENT_TIMEOUT_MS
) -> Connection:
    connection = psycopg.connect(
        dsn,
        connect_timeout=max(1, round(connect_timeout_seconds)),
        application_name=APPLICATION_NAME,
        options=f"-c default_transaction_read_only=on -c statement_timeout={statement_timeout_ms}",
    )
    connection.read_only = True
    return connection


class DatabaseError(SupportOpsError):
    pass


class SessionInfo(BaseModel):
    role: str
    database: str
    server_version: str
    read_only: bool
    monitoring: bool


@contextmanager
def read_only_session(
    dsn: SecretStr,
    connect_timeout_seconds: float,
    statement_timeout_ms: int = STATEMENT_TIMEOUT_MS,
) -> Iterator[Connection]:
    raw = dsn.get_secret_value()
    try:
        connection = connect_read_only(raw, connect_timeout_seconds, statement_timeout_ms)
    except psycopg.Error as exc:
        raise connection_error(exc, raw) from None
    try:
        connection.autocommit = True
        yield connection
    finally:
        connection.close()


def session_info(connection: Connection) -> SessionInfo:
    with connection.transaction():
        row = connection.execute(
            "SELECT current_user, current_database(), current_setting('server_version'),"
            " current_setting('transaction_read_only'),"
            " role.rolsuper OR pg_has_role(current_user, 'pg_monitor', 'USAGE')"
            " FROM pg_roles AS role WHERE role.rolname = current_user"
        ).fetchone()
    if row is None:
        raise DatabaseError("PostgreSQL didn't describe the current session.")
    role, database, version, read_only, monitoring = row
    return SessionInfo(
        role=role,
        database=database,
        server_version=version,
        read_only=read_only == "on",
        monitoring=bool(monitoring),
    )


def connection_error(exc: psycopg.Error, dsn: str) -> DatabaseError:
    target = describe_dsn(dsn)
    category = classify_error(exc)
    if category == "authentication_failed":
        return DatabaseError(
            f"PostgreSQL rejected the login for {target}.",
            hint="Check the user name and password in SUPPORTOPS_DB_URL. "
            "The lab's support role is supportops_ro.",
        )
    if category == "database_missing":
        return DatabaseError(
            f"The database in SUPPORTOPS_DB_URL doesn't exist ({target}).",
            hint="Check the database name at the end of SUPPORTOPS_DB_URL (the lab uses billing).",
        )
    if category in ("connection_refused", "timeout"):
        return DatabaseError(
            f"PostgreSQL at {target} didn't accept the connection ({category}).",
            hint="Check that PostgreSQL is running ('docker compose ps postgres') and that the "
            "host and port in SUPPORTOPS_DB_URL are right (the lab uses 127.0.0.1:5433).",
        )
    if category == "dns_failure":
        return DatabaseError(
            f"The database host name in SUPPORTOPS_DB_URL couldn't be resolved ({target}).",
            hint="Check the host name for typos.",
        )
    return DatabaseError(
        f"Couldn't connect to PostgreSQL at {target}: {_without_password(_first_line(exc), dsn)}"
    )


def classify_query_error(exc: psycopg.Error) -> tuple[str, str]:
    if isinstance(exc, psycopg.errors.QueryCanceled):
        return (
            "statement_timeout",
            "The query was cancelled by the statement timeout. Another session may be holding "
            "locks on these tables; run pg.blocking_sessions to check.",
        )
    if isinstance(exc, psycopg.errors.InsufficientPrivilege):
        return (
            "permission_denied",
            "The database role isn't allowed to read what this check needs.",
        )
    if isinstance(
        exc,
        psycopg.errors.UndefinedTable
        | psycopg.errors.UndefinedColumn
        | psycopg.errors.InvalidSchemaName,
    ):
        return (
            "missing_object",
            "This database doesn't have the tables this check expects. "
            "Check that SUPPORTOPS_DB_URL points to the billing database.",
        )
    return "error", _first_line(exc)


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
