import json
import re
import shutil
import subprocess
from collections.abc import Sequence
from pathlib import Path

import pytest
from typer import rich_utils
from typer.testing import CliRunner, Result

from supportops.cli.main import app
from supportops.logs import sources

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "logs"
BILLING = str(FIXTURES / "billing-api.jsonl")
SECRETS = str(FIXTURES / "planted-secrets.jsonl")
DOCKER = "/usr/bin/docker"
ANSI_ESCAPE = re.compile(r"\x1b\[[0-9;]*m")
PLANTED_VALUES = [
    "Planted",
    "bk_planted0001_fake_secret_value",
    "bk_planted0002_fake_secret_value",
    "jane.doe@customer.example",
    "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJwbGFudGVkIn0.c2lnbmF0dXJl",
    "UGxhbnRlZEJhc2ljLTAwMDc=",
]

runner = CliRunner()


class RecordingDocker:
    def __init__(self, stdout: bytes = b"", stderr: bytes = b"", returncode: int = 0) -> None:
        self.result = subprocess.CompletedProcess[bytes]([], returncode, stdout, stderr)
        self.commands: list[list[str]] = []

    def __call__(
        self, command: Sequence[str], timeout: float
    ) -> subprocess.CompletedProcess[bytes]:
        self.commands.append(list(command))
        return self.result


@pytest.fixture
def docker(monkeypatch: pytest.MonkeyPatch) -> RecordingDocker:
    fake = RecordingDocker(stdout=Path(BILLING).read_bytes())
    monkeypatch.setattr(shutil, "which", lambda name: DOCKER)
    monkeypatch.setattr(sources, "run_subprocess", fake)
    return fake


def invoke(*args: str, stdin: bytes | None = None, **env: str) -> Result:
    return runner.invoke(app, list(args), input=stdin, env={"COLUMNS": "200", **env})


def flat(text: str) -> str:
    return " ".join(text.split())


def plain(text: str) -> str:
    return ANSI_ESCAPE.sub("", text)


@pytest.mark.parametrize(
    ("args", "expected"),
    [
        (["logs", "--help"], ["summary", "search", "trace"]),
        (["logs", "summary", "--help"], ["--since", "--until", "--top", "--json", "docker:"]),
        (
            ["logs", "search", "--help"],
            ["--request-id", "--level", "--event", "--status", "--path", "--text", "--limit"],
        ),
        (["logs", "trace", "--help"], ["REQUEST_ID", "SUPPORTOPS_LOG_SOURCE"]),
    ],
)
@pytest.mark.parametrize("styled", [False, True], ids=["plain", "styled"])
def test_help_describes_the_commands(
    monkeypatch: pytest.MonkeyPatch, args: list[str], expected: list[str], styled: bool
) -> None:
    monkeypatch.setattr(rich_utils, "FORCE_TERMINAL", styled)

    result = invoke(*args)

    help_text = plain(result.stdout).lower()
    assert result.exit_code == 0
    assert ("\x1b[" in result.stdout) is styled
    for text in expected:
        assert text.lower() in help_text


def test_summary_text_output() -> None:
    result = invoke("logs", "summary", BILLING)

    output = flat(result.stdout)
    assert result.exit_code == 0
    assert "Read 22 lines: 22 log entries, 0 skipped, 0 blank" in output
    assert "Levels: INFO 15 WARNING 4 ERROR 3" in output
    assert "From 12 access-log entries. By class: 2xx 4 4xx 5 5xx 3" in output
    assert "auth.rejected (revoked_key): API key rejected" in output
    assert "demo-500-a, demo-500-b" in output
    assert "4,012 ms" in output


def test_summary_json_output() -> None:
    result = invoke("logs", "summary", BILLING, "--json")

    report = json.loads(result.stdout)
    assert result.exit_code == 0
    assert report["entries"] == 22
    assert report["input"]["parsed"] == 22
    assert report["slowest"][0]["request_id"] == "demo-503"


def test_summary_of_an_empty_source_exits_1(tmp_path: Path) -> None:
    empty = tmp_path / "empty.jsonl"
    empty.write_bytes(b"")

    result = invoke("logs", "summary", str(empty))

    assert result.exit_code == 1
    assert "No structured log entries were found." in result.stdout


def test_unreadable_source_exits_3_without_a_traceback(tmp_path: Path) -> None:
    result = invoke("logs", "summary", str(tmp_path / "missing.jsonl"))

    assert result.exit_code == 3
    assert "Log file not found" in result.stderr
    assert "Traceback" not in result.output


def test_default_source_comes_from_the_settings(docker: RecordingDocker) -> None:
    result = invoke("logs", "summary")

    assert result.exit_code == 0
    assert "Log summary for docker:supportops-billing-api-1" in result.stdout
    assert docker.commands[0][-1] == "supportops-billing-api-1"


def test_log_source_setting_can_point_to_a_file(docker: RecordingDocker) -> None:
    result = invoke("logs", "summary", SUPPORTOPS_LOG_SOURCE=BILLING)

    assert result.exit_code == 0
    assert f"Log summary for {BILLING}" in result.stdout
    assert docker.commands == []


def test_explicit_sources_override_the_setting(docker: RecordingDocker) -> None:
    result = invoke(
        "logs", "summary", "docker:other-container", BILLING, SUPPORTOPS_LOG_SOURCE="ignored.jsonl"
    )

    assert result.exit_code == 0
    assert docker.commands[0][-1] == "other-container"
    assert "Read 44 lines" in result.stdout


def test_since_is_passed_to_docker(docker: RecordingDocker) -> None:
    invoke("logs", "summary", "--since", "2026-10-08T09:05:00Z")

    assert docker.commands[0][-3:] == [
        "--since",
        "2026-10-08T09:05:00Z",
        "supportops-billing-api-1",
    ]


def test_shell_syntax_in_a_docker_source_is_refused(docker: RecordingDocker) -> None:
    result = invoke("logs", "summary", "docker:api; rm -rf /")

    assert result.exit_code == 2
    assert "not a valid Docker source" in result.stderr
    assert docker.commands == []


def test_docker_that_is_not_running_is_explained(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(shutil, "which", lambda name: DOCKER)
    monkeypatch.setattr(
        sources,
        "run_subprocess",
        RecordingDocker(
            returncode=1, stderr=b"error during connect: open //./pipe/docker_engine\n"
        ),
    )

    result = invoke("logs", "trace", "abc")

    assert result.exit_code == 3
    assert "Docker isn't running" in result.stderr
    assert "Start Docker Desktop" in flat(result.stderr)
    assert "Traceback" not in result.output


def test_stdin_with_powershell_utf16_encoding() -> None:
    data = Path(FIXTURES / "billing-api-utf16.jsonl").read_bytes()

    result = invoke("logs", "summary", "-", "--json", stdin=data)

    report = json.loads(result.stdout)
    assert result.exit_code == 0
    assert report["input"]["sources"] == ["stdin"]
    assert report["entries"] == 5


def test_search_text_output() -> None:
    result = invoke("logs", "search", BILLING, "--status", "4xx", "--path", "/v1/customers")

    lines = [line for line in result.stdout.splitlines() if line.startswith("2026-")]
    assert result.exit_code == 0
    assert "2 matching entries" in result.stdout
    assert "Filters: status 4xx, path /v1/customers" in result.stdout
    assert len(lines) == 2
    assert "request_id=demo-400" in lines[0]
    assert "POST /v1/customers -> 400 (3 ms)" in lines[0]


def test_search_without_matches_exits_1() -> None:
    result = invoke("logs", "search", BILLING, "--request-id", "nope")

    assert result.exit_code == 1
    assert "0 matching entries" in result.stdout


@pytest.mark.parametrize(
    "args",
    [["--status", "4x"], ["--level", "loud"], ["--since", "later"], ["--limit", "0"]],
)
def test_bad_search_options_exit_2(args: list[str]) -> None:
    result = invoke("logs", "search", BILLING, *args)

    assert result.exit_code == 2
    assert "Traceback" not in result.output


def test_search_json_output() -> None:
    result = invoke("logs", "search", BILLING, "--event", "auth.*", "--json")

    report = json.loads(result.stdout)
    assert report["matched"] == 2
    assert [event["extra"]["reason"] for event in report["events"]] == [
        "revoked_key",
        "missing_header",
    ]


def test_trace_text_output() -> None:
    result = invoke("logs", "trace", "demo-500-a", BILLING)

    output = result.stdout
    assert result.exit_code == 0
    assert "3 log entries for request ID demo-500-a" in output
    assert "Access log: POST /v1/invoices/inv_juniper_1005/pay -> 500 in 48 ms" in output
    assert "Highlights: exception" in output
    assert "+0 ms" in output
    assert "+7 ms" in output
    assert "+9 ms" in output
    assert "[exception]" in output
    assert "Stack trace:" in output
    assert "billing_api.faults.InjectedFault" in output


def test_trace_of_a_401_shows_the_internal_reason() -> None:
    result = invoke("logs", "trace", "demo-401-revoked", BILLING)

    assert result.exit_code == 0
    assert "reason=revoked_key key_prefix=bk_juniper00" in result.stdout
    assert "[authentication]" in result.stdout


def test_trace_header_survives_secret_sounding_request_ids() -> None:
    result = invoke("logs", "trace", "demo-secrets", SECRETS)

    assert "3 log entries for request ID demo-secrets" in result.stdout


def test_trace_not_found_exits_1() -> None:
    result = invoke("logs", "trace", "missing-id", BILLING)

    assert result.exit_code == 1
    assert "No log entries for request ID missing-id" in result.stdout
    assert "IDs must match exactly" in flat(result.stdout)


def test_trace_json_output() -> None:
    result = invoke("logs", "trace", "demo-401-missing", BILLING, "--json")

    report = json.loads(result.stdout)
    assert report["found"] is True
    assert [step["offset_ms"] for step in report["steps"]] == [0, 2]
    assert report["access_log"]["status"] == 401


@pytest.mark.parametrize(
    "args",
    [
        ["logs", "summary", SECRETS],
        ["logs", "summary", SECRETS, "--json"],
        ["logs", "search", SECRETS],
        ["logs", "search", SECRETS, "--json"],
        ["logs", "search", SECRETS, "--text", "planted"],
        ["logs", "trace", "demo-secrets", SECRETS],
        ["logs", "trace", "demo-secrets", SECRETS, "--json"],
    ],
)
def test_planted_secrets_never_reach_the_output(args: list[str]) -> None:
    result = invoke(*args)

    assert result.exit_code == 0
    for value in PLANTED_VALUES:
        assert value not in result.output
    if "--json" in args:
        json.loads(result.stdout)


def test_secret_fields_are_masked_but_still_visible_as_evidence() -> None:
    result = invoke("logs", "trace", "demo-secrets", SECRETS, "--json")

    first = json.loads(result.stdout)["steps"][0]["event"]
    assert first["extra"]["password"] == "***"
    assert first["extra"]["nested"]["db_password"] == "***"
    assert first["extra"]["nested"]["contact"] == "j***@customer.example"
    assert first["extra"]["dsn"] == "postgresql://billing_app:***@db:5432/billing"
