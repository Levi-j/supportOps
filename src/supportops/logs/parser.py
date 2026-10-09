import json
import re
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, BinaryIO, Literal

from pydantic import BaseModel, Field

from supportops.logs.sources import (
    MAX_LINE_CHARACTERS,
    LogSourceError,
    OpenedSource,
    Runner,
    open_source,
    parse_sources,
)

LEVEL_RANKS = {"TRACE": 5, "DEBUG": 10, "INFO": 20, "WARNING": 30, "ERROR": 40, "CRITICAL": 50}
LEVEL_ALIASES = {
    "WARN": "WARNING",
    "ERR": "ERROR",
    "FATAL": "CRITICAL",
    "CRIT": "CRITICAL",
    "INFORMATION": "INFO",
}
MAX_EXAMPLES = 5

_COMPOSE_PREFIX = re.compile(r"^[\w.-]+\s*\|\s?(?=\{)")
_ECS_MARKERS = ("@timestamp", "ecs.version", "ecs", "log.level")
_MISSING = object()

SkipReason = Literal["not_json", "not_an_object", "too_long"]


class LogEvent(BaseModel):
    source: str
    line: int
    format: Literal["flat", "ecs"]
    timestamp: datetime | None = None
    timestamp_text: str | None = None
    level: str | None = None
    service: str | None = None
    logger: str | None = None
    message: str | None = None
    event_name: str | None = None
    request_id: str | None = None
    method: str | None = None
    path: str | None = None
    status: int | None = None
    duration_ms: float | None = None
    account_id: str | None = None
    error_type: str | None = None
    error_message: str | None = None
    stack_trace: str | None = None
    extra: dict[str, Any] = Field(default_factory=dict)

    @property
    def sort_key(self) -> datetime:
        return self.timestamp or datetime.min.replace(tzinfo=UTC)


class InputStats(BaseModel):
    sources: list[str]
    lines: int = 0
    blank_lines: int = 0
    parsed: int = 0
    skipped: dict[str, int] = Field(default_factory=dict)
    skipped_examples: list[str] = Field(default_factory=list)
    naive_timestamps: int = 0
    outside_window: int = 0
    excluded_without_timestamp: int = 0
    truncated_sources: list[str] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)

    @property
    def skipped_total(self) -> int:
        return sum(self.skipped.values())


@dataclass
class LogInput:
    stats: InputStats
    events: Iterator[LogEvent]


@dataclass(frozen=True)
class _Field:
    name: str
    paths: tuple[str, ...]
    convert: Callable[[Any], Any]


def read_logs(
    source_texts: Sequence[str],
    *,
    since: datetime | None = None,
    stdin: BinaryIO | None = None,
    runner: Runner | None = None,
) -> LogInput:
    sources = parse_sources(source_texts)
    opened = [open_source(source, since=since, stdin=stdin, runner=runner) for source in sources]
    stats = InputStats(sources=[source.label for source in sources])
    return LogInput(stats=stats, events=_events(opened, stats))


def parse_line(text: str, source: str, line: int) -> LogEvent | SkipReason:
    if len(text) > MAX_LINE_CHARACTERS:
        return "too_long"
    text = _COMPOSE_PREFIX.sub("", text.strip(), count=1)
    try:
        data = json.loads(text)
    except ValueError:
        return "not_json"
    if not isinstance(data, dict):
        return "not_an_object"
    log_format: Literal["flat", "ecs"] = (
        "ecs" if any(_find(data, marker) is not _MISSING for marker in _ECS_MARKERS) else "flat"
    )
    remaining = dict(data)
    values: dict[str, Any] = {}
    for spec in _FIELDS:
        for path in spec.paths:
            found = _find(remaining, path)
            if found is _MISSING:
                continue
            converted = spec.convert(found)
            if converted is None:
                continue
            _pop(remaining, path)
            values[spec.name] = converted
            break
    if "duration_ms" not in values:
        nanoseconds = _find(remaining, "event.duration")
        if _is_number(nanoseconds):
            _pop(remaining, "event.duration")
            values["duration_ms"] = nanoseconds / 1_000_000
    timestamp_text = _take_text(remaining, ("timestamp", "@timestamp"))
    timestamp = parse_timestamp(timestamp_text) if timestamp_text is not None else None
    return LogEvent(
        source=source,
        line=line,
        format=log_format,
        timestamp=timestamp,
        timestamp_text=timestamp_text,
        extra=remaining,
        **values,
    )


def parse_timestamp(text: str) -> datetime | None:
    value = text.strip()
    if re.fullmatch(r"\d{10}(\.\d+)?", value):
        return datetime.fromtimestamp(float(value), tz=UTC)
    if re.fullmatch(r"\d{13}", value):
        return datetime.fromtimestamp(int(value) / 1000, tz=UTC)
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def normalize_level(value: str) -> str:
    upper = value.strip().upper()
    return LEVEL_ALIASES.get(upper, upper)


def level_rank(level: str | None) -> int | None:
    return LEVEL_RANKS.get(level) if level is not None else None


def _events(opened: list[OpenedSource], stats: InputStats) -> Iterator[LogEvent]:
    for source in opened:
        stats.notes.extend(source.notes)
        label = source.source.label
        if source.truncated:
            stats.truncated_sources.append(label)
        try:
            for number, line in enumerate(source.lines, start=1):
                stats.lines += 1
                if line is not None and not line.strip():
                    stats.blank_lines += 1
                    continue
                result = "too_long" if line is None else parse_line(line, label, number)
                if isinstance(result, str):
                    stats.skipped[result] = stats.skipped.get(result, 0) + 1
                    if len(stats.skipped_examples) < MAX_EXAMPLES:
                        stats.skipped_examples.append(f"{label}:{number}")
                    continue
                stats.parsed += 1
                if result.timestamp_text is not None and _is_naive(result.timestamp_text):
                    stats.naive_timestamps += 1
                yield result
        except OSError as exc:
            raise LogSourceError(f"Reading {label} failed: {exc.strerror or exc}") from None


def _is_naive(text: str) -> bool:
    try:
        return datetime.fromisoformat(text.strip()).tzinfo is None
    except ValueError:
        return False


def _find(data: dict[str, Any], path: str) -> Any:
    if path in data:
        return data[path]
    head, _, rest = path.partition(".")
    child = data.get(head)
    if not rest or not isinstance(child, dict):
        return _MISSING
    return _find(child, rest)


def _pop(data: dict[str, Any], path: str) -> None:
    if path in data:
        del data[path]
        return
    head, _, rest = path.partition(".")
    child = dict(data[head])
    _pop(child, rest)
    if child:
        data[head] = child
    else:
        del data[head]


def _take_text(data: dict[str, Any], paths: tuple[str, ...]) -> str | None:
    for path in paths:
        value = _find(data, path)
        if isinstance(value, str):
            _pop(data, path)
            return value
        if _is_number(value):
            _pop(data, path)
            return str(value)
    return None


def _is_number(value: Any) -> bool:
    return isinstance(value, int | float) and not isinstance(value, bool)


def _text(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None


def _level(value: Any) -> str | None:
    return normalize_level(value) if isinstance(value, str) and value.strip() else None


def _status(value: Any) -> int | None:
    if isinstance(value, int) and not isinstance(value, bool):
        return value if 100 <= value <= 599 else None
    if isinstance(value, str) and value.isdigit():
        return _status(int(value))
    return None


def _duration(value: Any) -> float | None:
    return float(value) if _is_number(value) and value >= 0 else None


_FIELDS = (
    _Field("level", ("level", "log.level"), _level),
    _Field("service", ("service", "service.name"), _text),
    _Field("logger", ("logger", "log.logger"), _text),
    _Field("message", ("message",), _text),
    _Field("event_name", ("event_name", "eventName"), _text),
    _Field("request_id", ("request_id", "requestId"), _text),
    _Field("method", ("method", "http.request.method"), _text),
    _Field("path", ("path", "url.path"), _text),
    _Field("status", ("status", "http.response.status_code"), _status),
    _Field("duration_ms", ("duration_ms",), _duration),
    _Field("account_id", ("account_id", "accountId"), _text),
    _Field("error_type", ("error_type", "error.type"), _text),
    _Field("error_message", ("error_message", "error.message"), _text),
    _Field("stack_trace", ("stack_trace", "error.stack_trace"), _text),
)
