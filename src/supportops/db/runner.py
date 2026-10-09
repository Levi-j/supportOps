import textwrap
import time
from collections import Counter
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import timedelta
from decimal import Decimal
from typing import Any, Literal

import psycopg
from pydantic import BaseModel, Field, SecretStr

from supportops.db.catalog import CATALOG, Check, ParameterValue, check_for_target, get_check
from supportops.db.connection import (
    STATEMENT_TIMEOUT_MS,
    Connection,
    DatabaseError,
    SessionInfo,
    classify_query_error,
    describe_dsn,
    read_only_session,
    session_info,
)
from supportops.errors import ConfigError, ExitCode
from supportops.targets import get_target

MAX_ROWS = 200
STATUS_ORDER: tuple["CheckStatus", ...] = ("pass", "fail", "warn", "info", "error", "skipped")
MONITORING_NOTE = (
    "This role isn't a member of pg_monitor, so PostgreSQL hides details of other roles' "
    "sessions. Activity checks can only see part of what is happening."
)

CheckStatus = Literal["pass", "fail", "warn", "info", "error", "skipped"]


class CheckResult(BaseModel):
    name: str
    pack: str
    description: str
    status: CheckStatus
    summary: str
    parameters: dict[str, ParameterValue] = Field(default_factory=dict)
    columns: list[str] = Field(default_factory=list)
    rows: list[dict[str, Any]] = Field(default_factory=list)
    row_count: int = 0
    truncated: bool = False
    complete: bool = True
    duration_ms: float | None = None
    error: str | None = None
    notes: list[str] = Field(default_factory=list)
    sql: str | None = None


class DbReport(BaseModel):
    target: str
    session: SessionInfo
    results: list[CheckResult]

    @property
    def counts(self) -> dict[str, int]:
        counts = Counter(result.status for result in self.results)
        return {status: counts[status] for status in STATUS_ORDER if counts[status]}

    @property
    def exit_code(self) -> ExitCode:
        statuses = {result.status for result in self.results}
        if "error" in statuses:
            return ExitCode.INCOMPLETE
        if statuses & {"fail", "warn"}:
            return ExitCode.PROBLEM
        return ExitCode.OK


@dataclass(frozen=True)
class PlannedCheck:
    check: Check
    parameters: dict[str, ParameterValue] = field(default_factory=dict)
    skip_reason: str | None = None


def parse_param_options(items: Sequence[str]) -> dict[str, str]:
    values: dict[str, str] = {}
    for item in items:
        name, separator, value = item.partition("=")
        name = name.strip()
        if not separator or not name:
            raise ConfigError(
                "--param must look like name=value, for example --param prefix=bk_juniper01."
            )
        if name in values and values[name] != value:
            raise ConfigError(f"--param {name} was given twice with different values.")
        values[name] = value
    return values


def plan_checks(
    names: Sequence[str],
    *,
    run_all: bool,
    parameters: dict[str, str],
    packs: frozenset[str] | None = None,
    target: str = "billing",
) -> list[PlannedCheck]:
    if names and run_all:
        raise ConfigError("Name the checks to run or use --all, not both.")
    if not names and not run_all:
        raise ConfigError(
            "Name at least one check, or use --all.",
            hint="List the available checks with 'supportops db checks'.",
        )
    allowed = packs if packs is not None else get_target(target).check_packs
    if run_all:
        selected = [check for check in CATALOG if check.pack in allowed]
    else:
        selected = [check_for_target(name, allowed, target) for name in dict.fromkeys(names)]
    declared = {parameter.name for check in selected for parameter in check.parameters}
    unknown = sorted(set(parameters) - declared)
    if unknown:
        raise ConfigError(
            f"Unknown parameter '{unknown[0]}' for the selected checks.",
            hint="'supportops db checks' lists the parameters each check accepts.",
        )
    return [_plan(check, parameters, strict=not run_all) for check in selected]


def run_checks(
    dsn: SecretStr,
    plan: Sequence[PlannedCheck],
    *,
    connect_timeout_seconds: float,
    statement_timeout_ms: int = STATEMENT_TIMEOUT_MS,
    show_sql: bool = False,
    clock: Callable[[], float] = time.perf_counter,
) -> DbReport:
    with read_only_session(dsn, connect_timeout_seconds, statement_timeout_ms) as connection:
        try:
            session = session_info(connection)
        except psycopg.Error as exc:
            raise DatabaseError(
                f"Connected, but couldn't describe the session: {classify_query_error(exc)[1]}"
            ) from None
        results = [_run(connection, item, session, show_sql, clock) for item in plan]
    return DbReport(target=describe_dsn(dsn.get_secret_value()), session=session, results=results)


def display_sql(check: Check) -> str:
    return textwrap.dedent(check.sql).strip()


def _plan(check: Check, given: dict[str, str], *, strict: bool) -> PlannedCheck:
    values: dict[str, ParameterValue] = {
        parameter.name: parameter.parse(given[parameter.name])
        if parameter.name in given
        else parameter.default
        for parameter in check.parameters
    }
    missing = False
    if check.one_of:
        provided = [name for name in check.one_of if values.get(name) is not None]
        if len(provided) > 1:
            choices = " or ".join(f"--param {name}" for name in check.one_of)
            raise ConfigError(f"{check.name} takes {choices}, not both.")
        missing = not provided
    else:
        missing = any(
            parameter.required and values[parameter.name] is None for parameter in check.parameters
        )
    if missing and strict:
        raise ConfigError(f"{check.name} needs a parameter.", hint=check.usage())
    return PlannedCheck(check, values, check.usage() if missing else None)


def _run(
    connection: Connection,
    item: PlannedCheck,
    session: SessionInfo,
    show_sql: bool,
    clock: Callable[[], float],
) -> CheckResult:
    check = item.check
    base: dict[str, Any] = {
        "name": check.name,
        "pack": check.pack,
        "description": check.description,
        "parameters": item.parameters,
        "sql": display_sql(check) if show_sql else None,
    }
    if item.skip_reason is not None:
        return CheckResult(**base, status="skipped", summary=f"Skipped. {item.skip_reason}")
    started = clock()
    try:
        with connection.transaction():
            cursor = (
                connection.execute(check.sql, item.parameters)
                if check.parameters
                else connection.execute(check.sql)
            )
            columns = [column.name for column in cursor.description or []]
            fetched = cursor.fetchmany(MAX_ROWS + 1)
    except psycopg.Error as exc:
        if connection.broken:
            raise DatabaseError(
                f"The database connection was lost while running {check.name}."
            ) from None
        category, explanation = classify_query_error(exc)
        return CheckResult(
            **base,
            status="error",
            summary=explanation,
            error=category,
            duration_ms=_elapsed(clock, started),
        )
    rows = [dict(zip(columns, (_plain(value) for value in row), strict=True)) for row in fetched]
    status, summary, notes, complete = _evaluate(check, rows[:MAX_ROWS], item.parameters, session)
    if len(rows) > MAX_ROWS:
        notes.append(f"Only the first {MAX_ROWS} rows are shown.")
    return CheckResult(
        **base,
        status=status,
        summary=summary,
        columns=columns,
        rows=rows[:MAX_ROWS],
        row_count=min(len(rows), MAX_ROWS),
        truncated=len(rows) > MAX_ROWS,
        complete=complete,
        duration_ms=_elapsed(clock, started),
        notes=notes,
    )


def _evaluate(
    check: Check,
    rows: list[dict[str, Any]],
    parameters: dict[str, ParameterValue],
    session: SessionInfo,
) -> tuple[CheckStatus, str, list[str], bool]:
    values: dict[str, Any] = {
        name: value for name, value in parameters.items() if value is not None
    }
    values["count"] = len(rows)
    values["lookup"] = " or ".join(
        f"{name} {values[name]}" for name in check.one_of if name in values
    )
    if check.kind == "connectivity":
        return _connectivity(rows)
    if check.kind == "consistency":
        if rows:
            return "fail", check.problem_message.format(**values), [], True
        return "pass", check.ok_message.format(**values), [], True
    if check.kind == "activity":
        if rows:
            notes = [] if session.monitoring else [MONITORING_NOTE]
            return "fail", check.problem_message.format(**values), notes, session.monitoring
        if not session.monitoring:
            return (
                "info",
                "Nothing found among the sessions this role can see.",
                [MONITORING_NOTE],
                False,
            )
        return "pass", check.ok_message.format(**values), [], True
    if check.kind == "inventory":
        total = sum(int(row.get("sessions", 0)) for row in rows)
        notes = [] if session.monitoring else [MONITORING_NOTE]
        return "info", f"Other sessions on this database: {total}.", notes, session.monitoring
    if rows:
        return "info", check.ok_message.format(**values), [], True
    return "fail", check.problem_message.format(**values), [], True


def _connectivity(rows: list[dict[str, Any]]) -> tuple[CheckStatus, str, list[str], bool]:
    if not rows:
        return "error", "PostgreSQL returned no information about the current role.", [], False
    row = rows[0]
    read_only = row["transaction_read_only"] == "on"
    summary = (
        f"Connected to {row['database']} as {row['role']} (PostgreSQL {row['server_version']}). "
        + ("The session is read-only." if read_only else "The session is NOT read-only.")
    )
    warnings = privilege_warnings(row)
    notes = warnings + ([] if row["can_monitor"] else [MONITORING_NOTE])
    return ("warn" if warnings else "pass"), summary, notes, True


def privilege_warnings(row: dict[str, Any]) -> list[str]:
    warnings = []
    if row["transaction_read_only"] != "on":
        warnings.append(
            "The diagnostic transaction is not read-only. Stop and report this as a SupportOps "
            "problem."
        )
    if row["superuser"]:
        warnings.append(
            "Connected as a PostgreSQL superuser. Use a read-only support role (supportops_ro in "
            "the lab) instead. SupportOps still runs every check in a read-only transaction, but "
            "anyone reusing these credentials could change or delete anything."
        )
    else:
        extra = [
            label
            for key, label in (
                ("can_create_roles", "create roles"),
                ("can_create_databases", "create databases"),
                ("bypasses_row_security", "bypass row-level security"),
                ("replication", "start replication"),
            )
            if row[key]
        ]
        if extra:
            warnings.append(
                "The role has administrative attributes it doesn't need for diagnostics: it can "
                + ", ".join(extra)
                + "."
            )
    writable = list(row["writable_tables"] or [])
    if writable:
        shown = ", ".join(writable[:5]) + (", ..." if len(writable) > 5 else "")
        warnings.append(
            f"Tables this role could change outside SupportOps: {len(writable)} ({shown}). "
            "A support role should only be able to read."
        )
    return warnings


def _plain(value: Any) -> Any:
    if isinstance(value, Decimal):
        return int(value) if value == value.to_integral_value() else float(value)
    if isinstance(value, timedelta):
        return value.total_seconds()
    if isinstance(value, list):
        return [_plain(item) for item in value]
    if isinstance(value, bytes | memoryview):
        return "<binary>"
    return value


def _elapsed(clock: Callable[[], float], started: float) -> float:
    return round((clock() - started) * 1000, 1)


class ParameterInfo(BaseModel):
    name: str
    kind: str
    required: bool
    default: ParameterValue
    description: str
    example: str


class CheckInfo(BaseModel):
    name: str
    pack: str
    description: str
    parameters: list[ParameterInfo]
    requirement: str | None
    sql: str | None = None


class CatalogListing(BaseModel):
    checks: list[CheckInfo]


def describe_checks(names: Sequence[str], *, show_sql: bool) -> CatalogListing:
    selected = [get_check(name) for name in dict.fromkeys(names)] if names else list(CATALOG)
    return CatalogListing(
        checks=[
            CheckInfo(
                name=check.name,
                pack=check.pack,
                description=check.description,
                parameters=[
                    ParameterInfo(
                        name=parameter.name,
                        kind=parameter.kind,
                        required=parameter.required,
                        default=parameter.default,
                        description=parameter.description,
                        example=parameter.example,
                    )
                    for parameter in check.parameters
                ],
                requirement=check.usage()
                if check.one_of or any(parameter.required for parameter in check.parameters)
                else None,
                sql=display_sql(check) if show_sql else None,
            )
            for check in selected
        ]
    )
