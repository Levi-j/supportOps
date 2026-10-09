import json
import re
from pathlib import Path
from typing import Any

import httpx
import pytest
from typer import rich_utils
from typer.testing import CliRunner, Result

from supportops import health
from supportops.cli import investigate as investigate_cli
from supportops.cli.main import app
from supportops.db.connection import DatabaseProbe
from supportops.http_checks import create_client
from supportops.investigation import live
from supportops.settings import Settings
from tests.unit.fake_api import FakeApi, lab_health, respond
from tests.unit.investigation_support import (
    API_KEY,
    BILLING,
    DB_PASSWORD,
    DB_URL,
    FakeChecks,
    access,
    check_result,
    event,
    invoice_row,
    key_row,
    rejected,
    spanning,
    write_log,
)

ANSI_ESCAPE = re.compile(r"\x1b\[[0-9;]*m")
runner = CliRunner()


def invoke(*args: str, input: bytes | None = None, **env: str) -> Result:
    return runner.invoke(app, ["investigate", *args], env={"COLUMNS": "200", **env}, input=input)


def plain(text: str) -> str:
    return ANSI_ESCAPE.sub("", text)


@pytest.fixture
def api(monkeypatch: pytest.MonkeyPatch) -> FakeApi:
    fake = (
        FakeApi()
        .on("GET", "/health", lab_health())
        .on(
            "GET",
            "/health/ready",
            respond(503, {"status": "not_ready", "checks": {"database": {"status": "down"}}}),
        )
    )

    def client(settings: Settings, transport: httpx.BaseTransport | None = None) -> httpx.Client:
        return create_client(settings, transport=fake.transport)

    monkeypatch.setattr(investigate_cli, "create_client", client)
    monkeypatch.setattr(
        health,
        "probe_database",
        lambda *_args, **_kwargs: DatabaseProbe(
            target="supportops_ro@127.0.0.1:5433/billing",
            reachable=True,
            server_answered=True,
            latency_ms=3,
        ),
    )
    return fake


@pytest.fixture
def database(monkeypatch: pytest.MonkeyPatch) -> FakeChecks:
    fake = FakeChecks(
        {"billing.api_key_status": check_result("billing.api_key_status", [key_row()])}
    )
    monkeypatch.setattr(live, "run_checks", fake)
    return fake


@pytest.mark.parametrize("styled", [False, True], ids=["plain", "styled"])
def test_help_lists_the_options(monkeypatch: pytest.MonkeyPatch, styled: bool) -> None:
    monkeypatch.setattr(rich_utils, "FORCE_TERMINAL", styled)

    result = invoke("--help")

    assert result.exit_code == 0
    assert ("\x1b[" in result.stdout) is styled
    text = plain(result.stdout)
    for option in ("--since", "--until", "--no-db", "--report", "--json", "SOURCE"):
        assert option in text


def test_root_help_lists_investigate() -> None:
    result = runner.invoke(app, ["--help"], env={"COLUMNS": "200"})

    assert "investigate" in plain(result.stdout)


def test_a_revoked_key_is_explained(api: FakeApi) -> None:
    result = invoke("demo-401-revoked", BILLING, "--no-db")

    output = result.stdout
    assert result.exit_code == 0, result.output
    assert "FINDINGS  The API key was revoked (confirmed)." in output
    assert "[CONFIRMED]" in output
    assert "E1  log entry at 2026-10-08T09:02:00.001Z" in output
    assert "Coverage: partial for every signature above" in output
    assert output.count("The logs that were read begin") == 1
    assert api.requests == []


def test_an_offline_investigation_makes_no_current_state_claim(api: FakeApi) -> None:
    result = invoke("demo-400", BILLING, "--no-db")

    output = result.stdout
    assert result.exit_code == 0, result.output
    assert "Database: not needed for this request" in output
    assert "API health: not checked" in output
    assert "not when the request was made" not in output
    assert "Database and API results" not in output
    assert output.rstrip().endswith("Use --report FILE to write an internal Markdown draft.")


def test_database_only_checks_get_a_database_note(api: FakeApi, database: FakeChecks) -> None:
    result = invoke("demo-401-revoked", BILLING, SUPPORTOPS_DB_URL=DB_URL)

    assert "Database checks ran at" in result.stdout
    assert "API health check ran" not in result.stdout
    assert "API health: not checked" in result.stdout


def test_health_only_checks_get_a_health_note(api: FakeApi) -> None:
    result = invoke("demo-503", BILLING, "--no-db")

    assert result.exit_code == 0, result.output
    assert "Database: not needed for this request" in result.stdout
    assert "API health: checked at" in result.stdout
    assert "The API health check ran at" in result.stdout
    assert "Database checks ran at" not in result.stdout


def test_combined_checks_get_both_notes(api: FakeApi, database: FakeChecks, tmp_path: Path) -> None:
    source = write_log(
        tmp_path / "combined.jsonl",
        spanning(
            event(0, "db.unavailable", level="ERROR", error="dns_failure", detail="no host"),
            access(0.01, status=503, path="/v1/invoices/inv_juniper_1003"),
        ),
    )

    result = invoke("req-1", source, SUPPORTOPS_DB_URL=DB_URL)

    assert result.exit_code == 0, result.output
    assert database.names[0] == "billing.invoice_lookup"
    assert "Database checks ran at" in result.stdout
    assert "The API health check ran at" in result.stdout


def test_repeated_next_steps_are_shown_once(api: FakeApi, monkeypatch: pytest.MonkeyPatch) -> None:
    record = invoice_row(
        invoice_id="inv_juniper_1005", number="INV-1005", total_cents=12000, line_total_cents=12000
    )
    record.update(succeeded_payments=1, failed_payments=0)
    monkeypatch.setattr(
        live,
        "run_checks",
        FakeChecks(
            {
                "billing.invoice_lookup": check_result("billing.invoice_lookup", [record]),
                "billing.payment_on_unpaid_invoice": check_result(
                    "billing.payment_on_unpaid_invoice", [{"invoice_id": "inv_juniper_1005"}]
                ),
            }
        ),
    )

    result = invoke("demo-500-a", BILLING, SUPPORTOPS_DB_URL=DB_URL)

    assert result.exit_code == 0, result.output
    assert "payment data  [CONFIRMED]" in result.stdout
    assert "unhandled exception  [CONFIRMED]" in result.stdout
    assert result.stdout.count("not to retry") == 1


def test_database_evidence_is_cited(api: FakeApi, database: FakeChecks) -> None:
    result = invoke("demo-401-revoked", BILLING, SUPPORTOPS_DB_URL=DB_URL)

    assert result.exit_code == 0, result.output
    assert database.names == ["billing.api_key_status"]
    assert "Evidence: E1, E2, E3" in result.stdout
    assert "E3  database check at " in result.stdout
    assert ", billing.api_key_status" in result.stdout
    assert "Database: checked as supportops_ro@127.0.0.1:5433/billing at " in result.stdout
    assert DB_PASSWORD not in result.output


def test_no_db_never_touches_the_database(api: FakeApi, monkeypatch: pytest.MonkeyPatch) -> None:
    def forbidden(*_args: Any, **_kwargs: Any) -> None:
        raise AssertionError("run_checks must not be called with --no-db")

    monkeypatch.setattr(live, "run_checks", forbidden)

    result = invoke("demo-401-revoked", BILLING, "--no-db", SUPPORTOPS_DB_URL=DB_URL)

    assert result.exit_code == 0, result.output
    assert "Database: skipped (--no-db)" in result.stdout


def test_json_output(api: FakeApi) -> None:
    result = invoke("demo-400", BILLING, "--no-db", "--json")

    report = json.loads(result.stdout)
    assert result.exit_code == 0
    assert report["verdict"] == "FINDINGS"
    assert report["findings"][0]["rule"] == "malformed_json"
    assert report["findings"][0]["confidence"] == "confirmed"
    assert report["findings"][0]["evidence_ids"] == ["E1", "E2"]
    assert [item["id"] for item in report["evidence"]] == ["E1", "E2"]
    assert report["logs"]["impact"][0]["coverage"] == "partial"
    assert report["live"]["database"] == "not_needed"


def test_an_unknown_request_is_inconclusive(api: FakeApi) -> None:
    result = invoke("no-such-request", BILLING, "--no-db")

    assert result.exit_code == 1
    assert "INCONCLUSIVE" in result.stdout
    assert "Did the request reach this service" in result.stdout


def test_an_invalid_request_id_is_a_usage_error(api: FakeApi, tmp_path: Path) -> None:
    result = invoke("bad id;rm", str(tmp_path / "missing.jsonl"))

    assert result.exit_code == 2
    assert "doesn't look like a request ID" in result.stderr
    assert "Traceback" not in result.output


def test_an_unreadable_source_exits_3(api: FakeApi, tmp_path: Path) -> None:
    result = invoke("demo-400", str(tmp_path / "missing.jsonl"))

    assert result.exit_code == 3
    assert "Log file not found" in result.stderr


def test_logs_can_come_from_stdin(api: FakeApi) -> None:
    data = Path(BILLING).read_bytes()

    result = invoke("demo-422", "-", "--no-db", input=data)

    assert result.exit_code == 0, result.output
    assert "The request was rejected by validation" in result.stdout


def test_a_database_error_investigation_sends_only_unauthenticated_gets(api: FakeApi) -> None:
    result = invoke("demo-503", BILLING, SUPPORTOPS_API_KEY=API_KEY, SUPPORTOPS_DB_URL=DB_URL)

    assert result.exit_code == 0, result.output
    assert api.methods == ["GET", "GET"]
    assert [request.url.path for request in api.requests] == ["/health", "/health/ready"]
    assert all("authorization" not in request.headers for request in api.requests)
    assert "The API can't reach its database, but PostgreSQL is up  [CONFIRMED]" in result.stdout
    assert "Escalate to Deployment owner / on-call (high)" in result.stdout
    assert API_KEY not in result.output


def test_the_report_is_written_once(api: FakeApi, tmp_path: Path) -> None:
    target = tmp_path / "reports" / "demo-400.md"

    first = invoke("demo-400", BILLING, "--no-db", "--report", str(target))
    content = target.read_bytes()
    second = invoke("demo-400", BILLING, "--no-db", "--report", str(target))

    assert first.exit_code == 0, first.output
    assert f"Report draft written to {target}" in first.stdout
    assert "Use --report FILE" not in first.stdout
    assert content.startswith(b"# Internal investigation draft: request `demo-400`\n")
    assert b"not for direct customer distribution" in content
    assert b"\r\n" not in content
    assert not content.startswith(b"\xef\xbb\xbf")
    assert second.exit_code == 2
    assert "already exists" in second.stderr
    assert target.read_bytes() == content


def test_an_existing_report_is_refused_before_reading_logs(api: FakeApi, tmp_path: Path) -> None:
    target = tmp_path / "existing.md"
    target.write_text("keep me")

    result = invoke("demo-400", str(tmp_path / "missing.jsonl"), "--report", str(target))

    assert result.exit_code == 2
    assert target.read_text() == "keep me"


def test_json_and_report_keep_stdout_machine_readable(api: FakeApi, tmp_path: Path) -> None:
    target = tmp_path / "draft.md"

    result = invoke("demo-400", BILLING, "--no-db", "--json", "--report", str(target))

    assert result.exit_code == 0
    assert json.loads(result.stdout)["request_id"] == "demo-400"
    assert "Report draft written" in result.stderr


def planted_entries() -> list[dict[str, Any]]:
    return spanning(
        rejected(0),
        event(
            0.001,
            "debug.dump",
            message=f"Authorization: Bearer {API_KEY} for jane.doe@example.com",
            dsn="postgresql://billing_app:PlantedDsnPw77@postgres:5432/billing",
            password="PlantedFieldPw88",
            note=f"login failed for {DB_PASSWORD}",
        ),
        access(0.01),
    )


@pytest.mark.parametrize("json_output", [False, True], ids=["text", "json"])
def test_planted_secrets_never_reach_any_output(
    api: FakeApi, tmp_path: Path, json_output: bool
) -> None:
    source = write_log(tmp_path / "planted.jsonl", planted_entries())
    target = tmp_path / f"planted-{json_output}.md"
    args = ["req-1", source, "--no-db", "--report", str(target)]

    result = invoke(
        *args,
        *(["--json"] if json_output else []),
        SUPPORTOPS_API_KEY=API_KEY,
        SUPPORTOPS_DB_URL=DB_URL,
    )

    everything = result.output + target.read_text(encoding="utf-8")
    assert result.exit_code == 0, result.output
    for secret in (
        API_KEY,
        DB_PASSWORD,
        "PlantedDsnPw77",
        "PlantedFieldPw88",
        "jane.doe",
        "lab_only_not_a_real_key",
    ):
        assert secret not in everything
    assert "bk_juniper01***" in everything


SHORT_PASSWORD = "x9Q"
SHORT_DB_URL = f"postgresql://supportops_ro:{SHORT_PASSWORD}@127.0.0.1:5433/billing"


def short_secret_log(tmp_path: Path) -> str:
    return write_log(
        tmp_path / "short.jsonl",
        spanning(
            rejected(0),
            event(0.001, "debug.dump", message=f"login failed for {SHORT_PASSWORD}"),
            access(0.01),
        ),
    )


@pytest.mark.parametrize("output", ["text", "json", "report"])
@pytest.mark.parametrize("in_evidence", [True, False], ids=["in-evidence", "absent"])
def test_a_short_secret_fails_closed_for_every_output(
    api: FakeApi, tmp_path: Path, output: str, in_evidence: bool
) -> None:
    source = short_secret_log(tmp_path) if in_evidence else BILLING
    request_id = "req-1" if in_evidence else "demo-400"
    target = tmp_path / "short.md"
    extra = {"text": [], "json": ["--json"], "report": ["--report", str(target)]}[output]

    result = invoke(request_id, source, "--no-db", *extra, SUPPORTOPS_DB_URL=SHORT_DB_URL)

    assert result.exit_code == 2
    assert result.stdout == ""
    assert "too short to redact safely" in result.stderr
    assert "stopped before producing any investigation output" in result.stderr
    assert SHORT_PASSWORD not in result.output
    assert not target.exists()
    assert api.requests == []


def test_a_short_api_key_fails_closed_before_reading_logs(api: FakeApi, tmp_path: Path) -> None:
    result = invoke("req-1", str(tmp_path / "missing.jsonl"), "--json", SUPPORTOPS_API_KEY="k7")

    assert result.exit_code == 2
    assert result.stdout == ""
    assert "SUPPORTOPS_API_KEY" in result.stderr
    assert "k7" not in result.output
    assert "Log file not found" not in result.stderr


def test_a_four_character_secret_is_still_masked(api: FakeApi, tmp_path: Path) -> None:
    password = "x9Qz"
    source = write_log(
        tmp_path / "four.jsonl",
        spanning(
            rejected(0),
            event(0.001, "debug.dump", message=f"login failed for {password}"),
            access(0.01),
        ),
    )
    target = tmp_path / "four.md"

    result = invoke(
        "req-1",
        source,
        "--no-db",
        "--report",
        str(target),
        SUPPORTOPS_DB_URL=f"postgresql://supportops_ro:{password}@127.0.0.1:5433/billing",
    )

    assert result.exit_code == 0, result.output
    assert password not in result.output + target.read_text(encoding="utf-8")
    assert "login failed for ***" in result.stdout
