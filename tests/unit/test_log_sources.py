import io
import shutil
import subprocess
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from supportops.errors import ConfigError, ExitCode
from supportops.logs import sources
from supportops.logs.sources import (
    LogSource,
    LogSourceError,
    decode_lines,
    detect_encoding,
    open_source,
    parse_source,
    parse_sources,
)

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "logs"
DOCKER = "C:/Program Files/Docker/docker.exe"


class FakeRunner:
    def __init__(
        self,
        stdout: bytes = b"",
        stderr: bytes = b"",
        returncode: int = 0,
        error: BaseException | None = None,
    ) -> None:
        self.result = subprocess.CompletedProcess[bytes]([], returncode, stdout, stderr)
        self.error = error
        self.commands: list[list[str]] = []

    def __call__(
        self, command: Sequence[str], timeout: float
    ) -> subprocess.CompletedProcess[bytes]:
        self.commands.append(list(command))
        if self.error is not None:
            raise self.error
        return self.result


@pytest.fixture
def docker_installed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(shutil, "which", lambda name: DOCKER if name == "docker" else None)


def lines(data: bytes) -> list[str | None]:
    return list(decode_lines(io.BytesIO(data)))


def docker_lines(runner: FakeRunner, since: datetime | None = None) -> list[str | None]:
    return list(open_source(LogSource("docker", "billing"), since=since, runner=runner).lines)


def test_sources_are_recognized_by_their_syntax() -> None:
    assert parse_source("-") == LogSource("stdin", "-")
    assert parse_source("docker:supportops-billing-api-1") == LogSource(
        "docker", "supportops-billing-api-1"
    )
    assert parse_source("logs/api.jsonl") == LogSource("file", "logs/api.jsonl")
    assert parse_source("docker:api").label == "docker:api"
    assert parse_source("-").label == "stdin"


@pytest.mark.parametrize(
    "text",
    [
        "docker:",
        "docker:-f",
        "docker:--help",
        "docker:api; rm -rf /",
        "docker:$(whoami)",
        "docker:api name",
        "docker:../etc",
        "   ",
    ],
)
def test_unsafe_or_empty_sources_are_rejected(text: str) -> None:
    with pytest.raises(ConfigError) as excinfo:
        parse_source(text)

    assert excinfo.value.exit_code == ExitCode.USAGE


def test_stdin_can_only_be_read_once() -> None:
    with pytest.raises(ConfigError, match="only be given once"):
        parse_sources(["-", "-"])


@pytest.mark.parametrize(
    ("start", "expected"),
    [
        (b"\xef\xbb\xbf{", ("utf-8", 3)),
        (b"\xff\xfe{\x00", ("utf-16-le", 2)),
        (b"\xfe\xff\x00{", ("utf-16-be", 2)),
        (b'{\x00"\x00', ("utf-16-le", 0)),
        (b'\x00{\x00"', ("utf-16-be", 0)),
        (b'{"a": 1}', ("utf-8", 0)),
        (b"", ("utf-8", 0)),
    ],
)
def test_encoding_detection(start: bytes, expected: tuple[str, int]) -> None:
    assert detect_encoding(start) == expected


@pytest.mark.parametrize(
    "data",
    [
        'é{"a": 1}\n{"b": 2}\n'.encode(),
        b"\xef\xbb\xbf" + 'é{"a": 1}\n{"b": 2}\n'.encode(),
        'é{"a": 1}\r\n{"b": 2}\r\n'.encode("utf-16"),
        'é{"a": 1}\n{"b": 2}'.encode("utf-16-le"),
        'é{"a": 1}\r{"b": 2}\r'.encode("utf-16-be"),
    ],
)
def test_every_supported_encoding_and_newline_gives_the_same_lines(data: bytes) -> None:
    assert lines(data) == ['é{"a": 1}', '{"b": 2}']


def test_powershell_utf16_fixture_is_decoded() -> None:
    decoded = list(open_source(LogSource("file", str(FIXTURES / "billing-api-utf16.jsonl"))).lines)

    assert len(decoded) == 5
    assert all(line is not None and line.startswith('{"timestamp"') for line in decoded)


def test_crlf_split_across_chunks_is_one_line_break(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sources, "CHUNK_BYTES", 3)

    assert lines(b"abc\r\ndef\r\n\r\nxyz") == ["abc", "def", "", "xyz"]


def test_blank_lines_and_a_missing_final_newline_are_kept() -> None:
    assert lines(b"one\n\n  \ntwo") == ["one", "", "  ", "two"]


def test_invalid_utf8_is_replaced_instead_of_failing() -> None:
    assert lines(b'{"a": "\xff"}\n') == ['{"a": "\ufffd"}']


def test_overlong_lines_are_flagged_and_skipped(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sources, "CHUNK_BYTES", 4)
    monkeypatch.setattr(sources, "MAX_LINE_CHARACTERS", 10)

    assert lines(b"short\n" + b"x" * 50 + b"\nafter\n") == ["short", None, "after"]


def test_missing_file_is_a_source_error(tmp_path: Path) -> None:
    with pytest.raises(LogSourceError, match="Log file not found") as excinfo:
        open_source(LogSource("file", str(tmp_path / "missing.jsonl")))

    assert excinfo.value.exit_code == ExitCode.INCOMPLETE


def test_directory_is_a_source_error(tmp_path: Path) -> None:
    with pytest.raises(LogSourceError, match="is a directory"):
        open_source(LogSource("file", str(tmp_path)))


def test_stdin_is_read_from_the_given_stream() -> None:
    opened = open_source(LogSource("stdin", "-"), stdin=io.BytesIO('{"a": 1}\r\n'.encode("utf-16")))

    assert list(opened.lines) == ['{"a": 1}']


def test_docker_logs_runs_without_a_shell(docker_installed: None) -> None:
    runner = FakeRunner(stdout=b'{"a": 1}\n', stderr=b"container stderr\n")

    result = docker_lines(runner, since=datetime(2026, 10, 8, 9, 15, 30, 500000, tzinfo=UTC))

    assert runner.commands == [
        [DOCKER, "logs", "--tail", "100000", "--since", "2026-10-08T09:15:30Z", "billing"]
    ]
    assert result == ['{"a": 1}', "container stderr"]


def test_docker_without_since_reads_the_tail(docker_installed: None) -> None:
    runner = FakeRunner()

    assert docker_lines(runner) == []
    assert runner.commands == [[DOCKER, "logs", "--tail", "100000", "billing"]]


def test_docker_tail_limit_is_reported(
    docker_installed: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sources, "DOCKER_TAIL_LINES", 2)

    opened = open_source(LogSource("docker", "billing"), runner=FakeRunner(stdout=b"a\nb\n"))

    assert len(opened.notes) == 1
    assert opened.notes[0].startswith("Only the last 2 lines of docker:billing were read.")


def test_missing_docker_executable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(shutil, "which", lambda name: None)
    runner = FakeRunner()

    with pytest.raises(LogSourceError, match="Docker isn't installed"):
        docker_lines(runner)

    assert runner.commands == []


@pytest.mark.parametrize(
    ("runner", "message"),
    [
        (FakeRunner(error=FileNotFoundError(2, "No such file")), "Docker isn't installed"),
        (FakeRunner(error=subprocess.TimeoutExpired(["docker"], 30)), "didn't finish within 30"),
        (FakeRunner(error=PermissionError(13, "Access is denied")), "Couldn't run Docker"),
        (
            FakeRunner(
                returncode=1, stderr=b"Error response from daemon: No such container: billing\n"
            ),
            "Docker has no container named 'billing'",
        ),
        (
            FakeRunner(
                returncode=1,
                stderr=b'error during connect: Get "http://%2F%2F.%2Fpipe%2FdockerDesktopLinuxEngine'
                b'/v1.47/containers/billing/logs": open //./pipe/dockerDesktopLinuxEngine: '
                b"The system cannot find the file specified.\n",
            ),
            "Docker isn't running",
        ),
        (
            FakeRunner(
                returncode=1,
                stderr=b"Cannot connect to the Docker daemon at unix:///var/run/docker.sock. "
                b"Is the docker daemon running?\n",
            ),
            "Docker isn't running",
        ),
        (
            FakeRunner(
                returncode=1,
                stderr=b"permission denied while trying to connect to the Docker daemon socket\n",
            ),
            "Permission denied",
        ),
        (FakeRunner(returncode=125, stderr=b"unexpected failure\n"), "exit code 125"),
    ],
)
def test_docker_failures_are_explained(
    docker_installed: None, runner: FakeRunner, message: str
) -> None:
    with pytest.raises(LogSourceError, match=message) as excinfo:
        docker_lines(runner)

    assert excinfo.value.exit_code == ExitCode.INCOMPLETE
    assert excinfo.value.hint


def test_unknown_docker_errors_show_one_short_line(docker_installed: None) -> None:
    runner = FakeRunner(returncode=1, stderr=b"first line " + b"x" * 500 + b"\nsecond line\n")

    with pytest.raises(LogSourceError) as excinfo:
        docker_lines(runner)

    assert excinfo.value.hint is not None
    assert "second line" not in excinfo.value.hint
    assert len(excinfo.value.hint) < 230


def test_default_runner_never_uses_a_shell(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[dict[str, Any]] = []

    def fake_run(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[bytes]:
        calls.append({"command": command, **kwargs})
        return subprocess.CompletedProcess[bytes](command, 0, b"", b"")

    monkeypatch.setattr(subprocess, "run", fake_run)

    sources.run_subprocess(["docker", "logs", "api"], 30)

    assert calls == [
        {
            "command": ["docker", "logs", "api"],
            "capture_output": True,
            "timeout": 30,
            "check": False,
        }
    ]
