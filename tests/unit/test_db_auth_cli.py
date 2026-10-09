import json
import re
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

import httpx
import pytest
from typer import rich_utils
from typer.testing import CliRunner, Result

from supportops import auth_checks
from supportops.cli import auth as auth_cli
from supportops.cli.main import app
from supportops.db import runner
from supportops.db.connection import DatabaseError, SessionInfo
from supportops.db.runner import CheckResult, DbReport
from supportops.http_checks import create_client
from supportops.settings import Settings
from tests.unit.fake_api import FakeApi, respond

ANSI_ESCAPE = re.compile(r"\x1b\[[0-9;]*m")
DB_PASSWORD = "CliDbPw-7731"
DB_URL = f"postgresql://supportops_ro:{DB_PASSWORD}@127.0.0.1:5433/billing"
API_KEY = "bk_juniper01_lab_only_not_a_real_key"
SESSION = SessionInfo(
    role="supportops_ro", database="billing", server_version="18.6", read_only=True, monitoring=True
)

runner_cli = CliRunner()


def invoke(*args: str, **env: str) -> Result:
    return runner_cli.invoke(app, list(args), env={"COLUMNS": "200", **env})


def plain(text: str) -> str:
    return ANSI_ESCAPE.sub("", text)


def flat(text: str) -> str:
    return " ".join(text.split())


class FakeCursor:
    def __init__(self, columns: list[str], rows: list[tuple[Any, ...]]) -> None:
        self.columns = columns
        self.rows = rows

    @property
    def description(self) -> list[Any]:
        return [type("Column", (), {"name": name}) for name in self.columns]

    def fetchmany(self, size: int) -> list[tuple[Any, ...]]:
        return self.rows[:size]


class FakeConnection:
    broken = False

    def __init__(self, answers: dict[str, FakeCursor]) -> None:
        self.answers = answers

    @contextmanager
    def transaction(self) -> Iterator[None]:
        yield

    def execute(self, sql: str, params: Any = None) -> FakeCursor:
        from supportops.db.catalog import CHECKS

        name = next(name for name, check in CHECKS.items() if check.sql == sql)
        return self.answers.get(name, FakeCursor(["value"], []))


@pytest.fixture
def database(monkeypatch: pytest.MonkeyPatch) -> dict[str, FakeCursor]:
    answers: dict[str, FakeCursor] = {}

    @contextmanager
    def session(*_args: object) -> Iterator[FakeConnection]:
        yield FakeConnection(answers)

    monkeypatch.setattr(runner, "read_only_session", session)
    monkeypatch.setattr(runner, "session_info", lambda _connection: SESSION)
    return answers


@pytest.mark.parametrize("styled", [False, True], ids=["plain", "styled"])
@pytest.mark.parametrize(
    ("args", "expected"),
    [
        (["db", "--help"], ["checks", "run"]),
        (["db", "checks", "--help"], ["--show-sql", "--json"]),
        (["db", "run", "--help"], ["--all", "--param", "--show-sql", "--json", "name=value"]),
        (["auth", "--help"], ["check"]),
        (["auth", "check", "--help"], ["--key-env", "--json"]),
    ],
)
def test_help_lists_the_options(
    monkeypatch: pytest.MonkeyPatch, args: list[str], expected: list[str], styled: bool
) -> None:
    monkeypatch.setattr(rich_utils, "FORCE_TERMINAL", styled)

    result = invoke(*args)

    assert result.exit_code == 0
    assert ("\x1b[" in result.stdout) is styled
    for text in expected:
        assert text.lower() in plain(result.stdout).lower()


def test_db_checks_lists_the_catalog_without_a_database() -> None:
    result = invoke("db", "checks")

    output = flat(result.stdout)
    assert result.exit_code == 0
    assert "billing.invoice_total_mismatch" in output
    assert "prefix (required)" in output
    assert "min_seconds (default 60)" in output
    assert "id, number (one of them)" in output


def test_db_checks_show_sql() -> None:
    result = invoke("db", "checks", "billing.api_key_status", "--show-sql")

    assert result.exit_code == 0
    assert "-- billing.api_key_status" in result.stdout
    assert "WHERE api_key.key_prefix = %(prefix)s" in result.stdout
    assert "key_hash" not in result.stdout
    assert "can't change the query" in flat(result.stdout)


def test_db_checks_json() -> None:
    listing = json.loads(invoke("db", "checks", "--json").stdout)

    assert len(listing["checks"]) == 14
    assert listing["checks"][0]["sql"] is None


@pytest.mark.parametrize(
    ("args", "message"),
    [
        (["db", "run"], "Name at least one check"),
        (["db", "run", "pg.connections", "--all"], "not both"),
        (["db", "run", "pg.terminate_everything"], "Unknown check"),
        (["db", "run", "pg.connections", "--param", "prefix=bk_juniper01"], "Unknown parameter"),
        (["db", "run", "billing.api_key_status"], "needs a parameter"),
        (["db", "run", "billing.api_key_status", "--param", f"prefix={API_KEY}"], "invalid value"),
        (["db", "run", "pg.long_transactions", "--param", "min_seconds=-5"], "whole number"),
        (["db", "run", "pg.connections", "--param", "DROP TABLE x"], "name=value"),
    ],
)
def test_db_run_usage_errors_exit_2_before_connecting(
    database: dict[str, FakeCursor], args: list[str], message: str
) -> None:
    result = invoke(*args, SUPPORTOPS_DB_URL=DB_URL)

    assert result.exit_code == 2
    assert message in flat(result.stderr)
    assert API_KEY not in result.output
    assert "Traceback" not in result.output


def test_db_run_needs_a_database_url() -> None:
    result = invoke("db", "run", "pg.connections")

    assert result.exit_code == 2
    assert "SUPPORTOPS_DB_URL is not set" in result.stderr


def test_db_run_all_passing(database: dict[str, FakeCursor]) -> None:
    database["db.connectivity"] = FakeCursor(
        [
            "role",
            "database",
            "server_version",
            "transaction_read_only",
            "superuser",
            "can_create_roles",
            "can_create_databases",
            "bypasses_row_security",
            "replication",
            "can_monitor",
            "writable_tables",
        ],
        [("supportops_ro", "billing", "18.6", "on", False, False, False, False, False, True, [])],
    )

    result = invoke("db", "run", "--all", SUPPORTOPS_DB_URL=DB_URL)

    output = flat(result.stdout)
    assert result.exit_code == 0
    assert "Database checks on supportops_ro@127.0.0.1:5433/billing" in output
    assert "PASS billing.invoice_total_mismatch" in output
    assert "SKIPPED billing.api_key_status Skipped. Needs --param prefix" in output
    assert "Result:" in output
    assert DB_PASSWORD not in result.output


def test_db_run_detected_problem_exits_1(database: dict[str, FakeCursor]) -> None:
    database["billing.duplicate_payments"] = FakeCursor(
        ["invoice_id", "succeeded_payments"], [("inv_juniper_1005", 2), ("inv_kestrel_2001", 3)]
    )

    result = invoke("db", "run", "billing.duplicate_payments", SUPPORTOPS_DB_URL=DB_URL)

    assert result.exit_code == 1
    assert "FAIL" in result.stdout
    assert "inv_kestrel_2001" in result.stdout


def test_db_run_json(database: dict[str, FakeCursor]) -> None:
    database["billing.invoice_lookup"] = FakeCursor(
        ["invoice_id", "status"], [("inv_juniper_1003", "open")]
    )

    result = invoke(
        "db",
        "run",
        "billing.invoice_lookup",
        "--param",
        "id=inv_juniper_1003",
        "--json",
        SUPPORTOPS_DB_URL=DB_URL,
    )

    report = json.loads(result.stdout)
    assert result.exit_code == 0
    assert report["results"][0]["status"] == "info"
    assert report["results"][0]["rows"] == [{"invoice_id": "inv_juniper_1003", "status": "open"}]
    assert report["results"][0]["parameters"] == {"id": "inv_juniper_1003", "number": None}


def test_db_run_show_sql_lists_bound_values_separately(database: dict[str, FakeCursor]) -> None:
    result = invoke(
        "db",
        "run",
        "billing.api_key_status",
        "--param",
        "prefix=bk_juniper00",
        "--show-sql",
        SUPPORTOPS_DB_URL=DB_URL,
    )

    assert "WHERE api_key.key_prefix = %(prefix)s" in result.stdout
    assert "prefix='bk_juniper00'" in result.stdout
    assert "never inserted into the SQL" in result.stdout


def test_secrets_in_rows_are_masked(database: dict[str, FakeCursor]) -> None:
    database["pg.long_transactions"] = FakeCursor(
        ["pid", "last_query"],
        [
            (1, "UPDATE billing.customers SET email = 'ann.lee@juniper-dental.example'"),
            (2, "ALTER ROLE app PASSWORD 'LeakedPw-123'; -- password=LeakedPw-123"),
        ],
    )

    text = invoke("db", "run", "pg.long_transactions", SUPPORTOPS_DB_URL=DB_URL)
    as_json = invoke("db", "run", "pg.long_transactions", "--json", SUPPORTOPS_DB_URL=DB_URL)

    for result in (text, as_json):
        assert result.exit_code == 1
        assert "ann.lee@juniper-dental.example" not in result.output
        assert "password=LeakedPw-123" not in result.output


def test_database_errors_exit_3_without_a_traceback(monkeypatch: pytest.MonkeyPatch) -> None:
    @contextmanager
    def unavailable(*_args: object) -> Iterator[None]:
        raise DatabaseError(
            "PostgreSQL at supportops_ro@127.0.0.1:5433/billing didn't accept the connection "
            "(connection_refused).",
            hint="Check that PostgreSQL is running.",
        )
        yield

    monkeypatch.setattr(runner, "read_only_session", unavailable)

    result = invoke("db", "run", "--all", SUPPORTOPS_DB_URL=DB_URL)

    assert result.exit_code == 3
    assert "didn't accept the connection" in result.stderr
    assert DB_PASSWORD not in result.output
    assert "Traceback" not in result.output


def test_query_errors_exit_3(
    monkeypatch: pytest.MonkeyPatch, database: dict[str, FakeCursor]
) -> None:
    def fail(*_args: Any, **_kwargs: Any) -> DbReport:
        return DbReport(
            target="x",
            session=SESSION,
            results=[
                CheckResult(
                    name="pg.connections",
                    pack="generic",
                    description="x",
                    status="error",
                    summary="The query was cancelled by the statement timeout.",
                    error="statement_timeout",
                )
            ],
        )

    monkeypatch.setattr("supportops.cli.db.run_checks", fail)

    result = invoke("db", "run", "pg.connections", SUPPORTOPS_DB_URL=DB_URL)

    assert result.exit_code == 3
    assert "ERROR" in result.stdout
    assert "statement timeout" in result.stdout


@pytest.fixture
def fake_api(monkeypatch: pytest.MonkeyPatch) -> FakeApi:
    api = FakeApi()

    def factory(settings: Settings) -> httpx.Client:
        return create_client(settings, transport=api.transport)

    monkeypatch.setattr(auth_cli, "create_client", factory)
    monkeypatch.setattr(
        auth_checks,
        "lookup_key_record",
        lambda _settings, _prefix: auth_checks.DatabaseEvidence(
            status="not_checked", detail="SUPPORTOPS_DB_URL is not set."
        ),
    )
    return api


def test_auth_check_success(fake_api: FakeApi) -> None:
    fake_api.on(
        "GET", "/v1/account", respond(200, {"id": "acct_juniper", "name": "Juniper Dental Group"})
    )

    result = invoke("auth", "check", SUPPORTOPS_API_KEY=API_KEY)

    assert result.exit_code == 0
    assert "Key: bk_juniper01*** from SUPPORTOPS_API_KEY" in result.stdout
    assert "Result: AUTHENTICATED" in result.stdout
    assert API_KEY not in result.output


def test_auth_check_rejection(fake_api: FakeApi, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OLD_KEY", "bk_juniper00_lab_only_not_a_real_key")
    fake_api.on("GET", "/v1/account", respond(401, {"code": "UNAUTHENTICATED"}, problem=True))

    text = invoke("auth", "check", "--key-env", "OLD_KEY")
    as_json = invoke("auth", "check", "--key-env", "OLD_KEY", "--json")

    report = json.loads(as_json.stdout)
    assert text.exit_code == 1
    assert "Result: REJECTED (401)" in text.stdout
    assert "supportops logs trace supportops-auth-" in text.stdout
    assert report["outcome"] == "rejected"
    assert report["request"]["request_id"].startswith("supportops-auth-")
    for result in (text, as_json):
        assert "bk_juniper00_lab_only_not_a_real_key" not in result.output


def test_auth_check_without_a_key_exits_2(fake_api: FakeApi) -> None:
    result = invoke("auth", "check")

    assert result.exit_code == 2
    assert "Key: none" in result.stdout
    assert fake_api.requests == []


def test_auth_check_unreachable_api_exits_3(fake_api: FakeApi) -> None:
    fake_api.on("GET", "/v1/account", httpx.ConnectTimeout("timed out"))

    result = invoke("auth", "check", SUPPORTOPS_API_KEY=API_KEY)

    assert result.exit_code == 3
    assert "Result: API UNREACHABLE" in result.stdout
    assert "Traceback" not in result.output
