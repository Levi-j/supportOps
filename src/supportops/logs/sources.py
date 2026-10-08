import codecs
import io
import re
import shutil
import subprocess
import sys
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import BinaryIO, Literal

from supportops.errors import ConfigError, SupportOpsError

STDIN = "-"
DOCKER_PREFIX = "docker:"
DOCKER_TIMEOUT_SECONDS = 30
DOCKER_TAIL_LINES = 100_000
MAX_LINE_CHARACTERS = 1_000_000
CHUNK_BYTES = 64 * 1024

_CONTAINER_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}")
_NEWLINE = re.compile(r"\r\n|\r|\n")

Runner = Callable[[Sequence[str], float], subprocess.CompletedProcess[bytes]]
Lines = Iterator[str | None]


class LogSourceError(SupportOpsError):
    pass


@dataclass(frozen=True)
class LogSource:
    kind: Literal["file", "stdin", "docker"]
    target: str

    @property
    def label(self) -> str:
        if self.kind == "docker":
            return f"{DOCKER_PREFIX}{self.target}"
        return "stdin" if self.kind == "stdin" else self.target


@dataclass
class OpenedSource:
    source: LogSource
    lines: Lines
    notes: list[str] = field(default_factory=list)


def parse_source(text: str) -> LogSource:
    if text == STDIN:
        return LogSource("stdin", STDIN)
    if text.startswith(DOCKER_PREFIX):
        name = text[len(DOCKER_PREFIX) :]
        if not _CONTAINER_NAME.fullmatch(name):
            raise ConfigError(
                f"'{text}' is not a valid Docker source.",
                hint="Use docker:<container-name>, for example docker:supportops-billing-api-1. "
                "Container names contain only letters, digits, '_', '.' and '-'.",
            )
        return LogSource("docker", name)
    if not text.strip():
        raise ConfigError("A log source can't be empty.")
    return LogSource("file", text)


def parse_sources(texts: Sequence[str]) -> list[LogSource]:
    sources = [parse_source(text) for text in texts]
    if sum(source.kind == "stdin" for source in sources) > 1:
        raise ConfigError("'-' (stdin) can only be given once.")
    return sources


def open_source(
    source: LogSource,
    *,
    since: datetime | None = None,
    stdin: BinaryIO | None = None,
    runner: Runner | None = None,
) -> OpenedSource:
    if source.kind == "stdin":
        return OpenedSource(source, decode_lines(stdin or _console_stdin()))
    if source.kind == "docker":
        return _open_docker(source, since, runner or run_subprocess)
    return OpenedSource(source, _open_file(Path(source.target)))


def decode_lines(stream: BinaryIO) -> Lines:
    first = stream.read(CHUNK_BYTES)
    encoding, bom_length = detect_encoding(first)
    decoder = codecs.getincrementaldecoder(encoding)(errors="replace")
    pending = ""
    chunk = first[bom_length:]
    while chunk:
        pending += decoder.decode(chunk)
        complete, pending = _split_lines(pending)
        yield from complete
        if len(pending) > MAX_LINE_CHARACTERS:
            yield None
            pending = _skip_to_next_line(stream, decoder)
        chunk = stream.read(CHUNK_BYTES)
    pending += decoder.decode(b"", final=True)
    if pending:
        last = _NEWLINE.split(pending)
        yield from last if last[-1] else last[:-1]


def detect_encoding(start: bytes) -> tuple[str, int]:
    if start.startswith(codecs.BOM_UTF8):
        return "utf-8", len(codecs.BOM_UTF8)
    if start.startswith(codecs.BOM_UTF16_LE):
        return "utf-16-le", len(codecs.BOM_UTF16_LE)
    if start.startswith(codecs.BOM_UTF16_BE):
        return "utf-16-be", len(codecs.BOM_UTF16_BE)
    if len(start) >= 2 and start[0] != 0 and start[1] == 0:
        return "utf-16-le", 0
    if len(start) >= 2 and start[0] == 0 and start[1] != 0:
        return "utf-16-be", 0
    return "utf-8", 0


def run_subprocess(command: Sequence[str], timeout: float) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(  # noqa: S603
        list(command), capture_output=True, timeout=timeout, check=False
    )


def _split_lines(text: str) -> tuple[list[str], str]:
    carry = ""
    if text.endswith("\r"):
        text, carry = text[:-1], "\r"
    parts = _NEWLINE.split(text)
    return parts[:-1], parts[-1] + carry


def _skip_to_next_line(stream: BinaryIO, decoder: codecs.IncrementalDecoder) -> str:
    while chunk := stream.read(CHUNK_BYTES):
        text = decoder.decode(chunk)
        match = _NEWLINE.search(text)
        if match:
            return text[match.end() :]
    return ""


def _console_stdin() -> BinaryIO:
    if sys.stdin is None or sys.stdin.isatty():
        raise ConfigError(
            "'-' reads logs from standard input, but nothing was piped in.",
            hint="Pipe logs in, for example: docker compose logs --no-log-prefix billing-api "
            "| uv run supportops logs summary -",
        )
    return sys.stdin.buffer


def _open_file(path: Path) -> Lines:
    if path.is_dir():
        raise LogSourceError(f"{path} is a directory, not a log file.")
    try:
        handle = path.open("rb")
    except FileNotFoundError:
        raise LogSourceError(
            f"Log file not found: {path}",
            hint="Check the path, or read a container directly with docker:<container-name>.",
        ) from None
    except PermissionError:
        raise LogSourceError(f"Permission denied reading {path}.") from None
    except OSError as exc:
        raise LogSourceError(f"Couldn't open {path}: {exc.strerror or exc}") from None
    return _file_lines(handle)


def _file_lines(handle: BinaryIO) -> Lines:
    with handle:
        yield from decode_lines(handle)


def _open_docker(source: LogSource, since: datetime | None, runner: Runner) -> OpenedSource:
    docker = shutil.which("docker")
    if docker is None:
        raise _docker_missing(source)
    command = [docker, "logs", "--tail", str(DOCKER_TAIL_LINES)]
    if since is not None:
        command += ["--since", since.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")]
    command.append(source.target)
    try:
        finished = runner(command, DOCKER_TIMEOUT_SECONDS)
    except FileNotFoundError:
        raise _docker_missing(source) from None
    except subprocess.TimeoutExpired:
        raise LogSourceError(
            f"'docker logs' for {source.label} didn't finish within "
            f"{DOCKER_TIMEOUT_SECONDS} seconds.",
            hint="Check that Docker responds ('docker ps'), or narrow the window with --since.",
        ) from None
    except OSError as exc:
        raise LogSourceError(
            f"Couldn't run Docker: {exc.strerror or exc}",
            hint="Check that 'docker version' works in this terminal.",
        ) from None
    if finished.returncode != 0:
        raise _docker_failure(source, finished)
    lines = [*decode_lines(io.BytesIO(finished.stdout)), *decode_lines(io.BytesIO(finished.stderr))]
    while lines and not lines[-1]:
        lines.pop()
    notes = []
    if len(lines) >= DOCKER_TAIL_LINES:
        notes.append(
            f"Only the last {DOCKER_TAIL_LINES:,} lines of {source.label} were read. "
            "Use --since to look at a shorter period."
        )
    return OpenedSource(source, iter(lines), notes)


def _docker_missing(source: LogSource) -> LogSourceError:
    return LogSourceError(
        f"Docker isn't installed or isn't on PATH, so {source.label} can't be read.",
        hint="Install Docker, or save the logs to a file and pass the file path instead.",
    )


def _docker_failure(
    source: LogSource, finished: subprocess.CompletedProcess[bytes]
) -> LogSourceError:
    message = finished.stderr.decode("utf-8", errors="replace").strip()
    lowered = message.lower()
    if "no such container" in lowered:
        return LogSourceError(
            f"Docker has no container named '{source.target}'.",
            hint="List containers with 'docker ps -a'. In the lab, 'docker compose ps' shows "
            "the API container, normally supportops-billing-api-1.",
        )
    if any(
        phrase in lowered
        for phrase in (
            "cannot connect to the docker daemon",
            "error during connect",
            "is the docker daemon running",
        )
    ):
        return LogSourceError(
            f"Docker isn't running, so {source.label} can't be read.",
            hint="Start Docker Desktop (or the Docker service) and try again.",
        )
    if "permission denied" in lowered:
        return LogSourceError(
            f"Permission denied while asking Docker for {source.label}.",
            hint="On Linux, add your user to the 'docker' group or run the command with sudo.",
        )
    first_line = message.splitlines()[0][:200] if message else "no error message"
    return LogSourceError(
        f"'docker logs' failed for {source.label} (exit code {finished.returncode}).",
        hint=f"Docker said: {first_line}",
    )
