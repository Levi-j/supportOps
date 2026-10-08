import heapq
import json
import re
from collections import Counter
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

from pydantic import BaseModel, Field

from supportops.errors import ConfigError
from supportops.logs.parser import (
    LEVEL_RANKS,
    InputStats,
    LogEvent,
    LogInput,
    level_rank,
    normalize_level,
    parse_timestamp,
)
from supportops.redaction import redact_text

ACCESS_EVENT = "http.request"
TOP_ITEMS = 10
SLOWEST_REQUESTS = 5
EXAMPLE_REQUEST_IDS = 3
MAX_CATEGORY_LENGTH = 64

_RELATIVE_TIME = re.compile(r"(\d+)\s*([smhd])")
_TIME_UNITS = {"s": "seconds", "m": "minutes", "h": "hours", "d": "days"}
_STATUS_FILTER = re.compile(r"[1-5](?:\d\d|xx)")
_UUID = re.compile(
    r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b"
)
_PREFIXED_ID = re.compile(r"\b[A-Za-z]+_[A-Za-z0-9_]*\d[A-Za-z0-9_]*\b")
_HEX = re.compile(r"\b(?=[0-9a-fA-F]*\d)(?=[0-9a-fA-F]*[a-fA-F])[0-9a-fA-F]{8,}\b")
_NUMBER = re.compile(r"\b\d+(?:\.\d+)?\b")
_SKIP_REASONS = {
    "not_json": "not JSON",
    "not_an_object": "JSON but not an object",
    "too_long": "longer than the line limit",
}


class TimeWindow(BaseModel):
    since: datetime | None = None
    until: datetime | None = None

    @property
    def active(self) -> bool:
        return self.since is not None or self.until is not None

    def admits(self, event: LogEvent, stats: InputStats) -> bool:
        if not self.active:
            return True
        if event.timestamp is None:
            stats.excluded_without_timestamp += 1
            return False
        if (self.since is not None and event.timestamp < self.since) or (
            self.until is not None and event.timestamp >= self.until
        ):
            stats.outside_window += 1
            return False
        return True


class CountItem(BaseModel):
    name: str
    count: int


class RequestTiming(BaseModel):
    timestamp: datetime | None
    request_id: str | None
    method: str | None
    path: str | None
    status: int | None
    duration_ms: float | None
    source: str
    line: int


class ErrorPattern(BaseModel):
    pattern: str
    level: str | None
    event_name: str | None
    error_type: str | None
    category: str | None
    count: int
    first_seen: datetime | None = None
    last_seen: datetime | None = None
    request_ids: list[str] = Field(default_factory=list)


class LogSummary(BaseModel):
    input: InputStats
    window: TimeWindow
    entries: int
    first_seen: datetime | None
    last_seen: datetime | None
    levels: list[CountItem]
    event_names: list[CountItem]
    other_event_names: int
    access_logs: int
    statuses: list[CountItem]
    status_classes: list[CountItem]
    error_patterns: list[ErrorPattern]
    other_error_patterns: int
    slowest: list[RequestTiming]
    notes: list[str]


class SearchFilters(BaseModel):
    request_id: str | None = None
    min_level: str | None = None
    event_names: list[str] = Field(default_factory=list)
    statuses: list[str] = Field(default_factory=list)
    path: str | None = None
    text: str | None = None


class SearchResult(BaseModel):
    input: InputStats
    window: TimeWindow
    filters: SearchFilters
    matched: int
    limit: int
    events: list[LogEvent]
    notes: list[str]


class TraceStep(BaseModel):
    offset_ms: float | None
    kind: str | None
    event: LogEvent


class RequestTrace(BaseModel):
    request_id: str
    input: InputStats
    window: TimeWindow
    found: bool
    steps: list[TraceStep]
    first_seen: datetime | None
    last_seen: datetime | None
    elapsed_ms: float | None
    access_log: RequestTiming | None
    highlights: list[str]
    notes: list[str]


def parse_time(text: str, now: datetime) -> datetime:
    value = text.strip()
    relative = _RELATIVE_TIME.fullmatch(value.lower())
    if relative:
        amount, unit = relative.groups()
        return now - timedelta(**{_TIME_UNITS[unit]: int(amount)})
    parsed = parse_timestamp(value)
    if parsed is None:
        raise ConfigError(
            f"Can't read the time '{text}'.",
            hint="Use a duration such as 30s, 15m, 2h or 1d, or an ISO 8601 timestamp "
            "such as 2026-10-08T09:00:00Z.",
        )
    return parsed


def time_window(since: str | None, until: str | None, now: datetime | None = None) -> TimeWindow:
    now = now or datetime.now(UTC)
    window = TimeWindow(
        since=parse_time(since, now) if since else None,
        until=parse_time(until, now) if until else None,
    )
    if window.since and window.until and window.until <= window.since:
        raise ConfigError("--until must be later than --since.")
    return window


def search_filters(
    *,
    request_id: str | None = None,
    level: str | None = None,
    event_names: list[str] | None = None,
    statuses: list[str] | None = None,
    path: str | None = None,
    text: str | None = None,
) -> SearchFilters:
    min_level = normalize_level(level) if level else None
    if min_level is not None and min_level not in LEVEL_RANKS:
        raise ConfigError(
            f"Unknown log level '{level}'.",
            hint="Use one of: " + ", ".join(LEVEL_RANKS) + " (WARN and FATAL are accepted too).",
        )
    normalized_statuses = [status.strip().lower() for status in statuses or []]
    for status in normalized_statuses:
        if not _STATUS_FILTER.fullmatch(status):
            raise ConfigError(
                f"Unknown status filter '{status}'.",
                hint="Use an exact status such as 404, or a class such as 4xx or 5xx.",
            )
    return SearchFilters(
        request_id=request_id,
        min_level=min_level,
        event_names=list(event_names or []),
        statuses=normalized_statuses,
        path=path,
        text=text,
    )


def is_access_log(event: LogEvent) -> bool:
    return event.event_name == ACCESS_EVENT or (
        event.method is not None and event.path is not None and event.status is not None
    )


def event_kind(event: LogEvent) -> str | None:
    name = event.event_name or ""
    if event.stack_trace or event.error_type or name == "unhandled_exception":
        return "exception"
    if name.startswith(("db.", "database.")):
        return "database"
    if name.startswith("auth."):
        return "authentication"
    if "validation" in name or name.startswith("request.invalid"):
        return "validation"
    if is_access_log(event):
        return "access"
    return None


def normalize_message(text: str) -> str:
    text = _UUID.sub("<uuid>", text)
    text = _PREFIXED_ID.sub("<id>", text)
    text = _HEX.sub("<hex>", text)
    return _NUMBER.sub(_number_placeholder, text)


def summarize(log_input: LogInput, window: TimeWindow, top: int = TOP_ITEMS) -> LogSummary:
    stats = log_input.stats
    levels: Counter[str] = Counter()
    names: Counter[str] = Counter()
    statuses: Counter[int] = Counter()
    patterns: dict[tuple[str | None, ...], ErrorPattern] = {}
    slowest: list[tuple[float, int, RequestTiming]] = []
    entries = access_logs = without_timestamp = 0
    first_seen: datetime | None = None
    last_seen: datetime | None = None
    for event in _windowed(log_input.events, window, stats):
        entries += 1
        levels[event.level or "(none)"] += 1
        if event.event_name:
            names[event.event_name] += 1
        if event.timestamp is None:
            without_timestamp += 1
        else:
            first_seen = min(first_seen or event.timestamp, event.timestamp)
            last_seen = max(last_seen or event.timestamp, event.timestamp)
        if is_access_log(event):
            access_logs += 1
            if event.status is not None:
                statuses[event.status] += 1
            if event.duration_ms is not None:
                heapq.heappush(slowest, (event.duration_ms, -entries, _timing(event)))
                if len(slowest) > SLOWEST_REQUESTS:
                    heapq.heappop(slowest)
        _record_pattern(patterns, event)
    ranked_patterns = sorted(patterns.values(), key=lambda item: -item.count)
    return LogSummary(
        input=stats,
        window=window,
        entries=entries,
        first_seen=first_seen,
        last_seen=last_seen,
        levels=_level_counts(levels),
        event_names=[CountItem(name=name, count=count) for name, count in names.most_common(top)],
        other_event_names=max(0, len(names) - top),
        access_logs=access_logs,
        statuses=[CountItem(name=str(code), count=statuses[code]) for code in sorted(statuses)],
        status_classes=_status_classes(statuses),
        error_patterns=ranked_patterns[:top],
        other_error_patterns=max(0, len(ranked_patterns) - top),
        slowest=[timing for _, _, timing in sorted(slowest, reverse=True)],
        notes=_summary_notes(entries, access_logs, without_timestamp, window) + _input_notes(stats),
    )


def search(
    log_input: LogInput, filters: SearchFilters, window: TimeWindow, limit: int
) -> SearchResult:
    newest: list[tuple[datetime, int, LogEvent]] = []
    matched = 0
    for event in _windowed(log_input.events, window, log_input.stats):
        if not _matches(event, filters):
            continue
        matched += 1
        heapq.heappush(newest, (event.sort_key, matched, event))
        if len(newest) > limit:
            heapq.heappop(newest)
    notes = []
    if matched == 0:
        notes.append("No log entries matched the filters.")
    elif matched > limit:
        notes.append(
            f"Showing the latest {limit} of {matched:,} matching entries. "
            "Use --limit or narrower filters to see others."
        )
    return SearchResult(
        input=log_input.stats,
        window=window,
        filters=filters,
        matched=matched,
        limit=limit,
        events=[event for _, _, event in sorted(newest, key=lambda item: item[:2])],
        notes=notes + _input_notes(log_input.stats),
    )


def trace(log_input: LogInput, request_id: str, window: TimeWindow) -> RequestTrace:
    matched = [
        event
        for event in _windowed(log_input.events, window, log_input.stats)
        if event.request_id == request_id
    ]
    ordered = [
        event
        for _, event in sorted(
            enumerate(matched),
            key=lambda pair: (pair[1].timestamp is None, pair[1].sort_key, pair[0]),
        )
    ]
    timestamps = [event.timestamp for event in ordered if event.timestamp is not None]
    first_seen = timestamps[0] if timestamps else None
    last_seen = timestamps[-1] if timestamps else None
    steps = [
        TraceStep(
            offset_ms=_offset(first_seen, event.timestamp),
            kind=event_kind(event),
            event=event,
        )
        for event in ordered
    ]
    access_logs = [event for event in ordered if is_access_log(event)]
    highlights = list(
        dict.fromkeys(step.kind for step in steps if step.kind not in (None, "access"))
    )
    return RequestTrace(
        request_id=request_id,
        input=log_input.stats,
        window=window,
        found=bool(steps),
        steps=steps,
        first_seen=first_seen,
        last_seen=last_seen,
        elapsed_ms=_offset(first_seen, last_seen),
        access_log=_timing(access_logs[-1]) if access_logs else None,
        highlights=[kind for kind in highlights if kind is not None],
        notes=_trace_notes(request_id, steps, access_logs, window) + _input_notes(log_input.stats),
    )


def _windowed(
    events: Iterator[LogEvent], window: TimeWindow, stats: InputStats
) -> Iterator[LogEvent]:
    return (event for event in events if window.admits(event, stats))


def _matches(event: LogEvent, filters: SearchFilters) -> bool:
    if filters.request_id is not None and event.request_id != filters.request_id:
        return False
    if filters.min_level is not None:
        rank = level_rank(event.level)
        if rank is None or rank < LEVEL_RANKS[filters.min_level]:
            return False
    if filters.event_names and not any(
        _name_matches(pattern, event.event_name) for pattern in filters.event_names
    ):
        return False
    if filters.statuses and not any(
        _status_matches(pattern, event.status) for pattern in filters.statuses
    ):
        return False
    if filters.path is not None and not _path_matches(filters.path, event.path):
        return False
    return filters.text is None or filters.text.lower() in _searchable(event)


def _name_matches(pattern: str, name: str | None) -> bool:
    if name is None:
        return False
    if pattern.endswith("*"):
        return name.startswith(pattern[:-1])
    return name == pattern


def _status_matches(pattern: str, status: int | None) -> bool:
    if status is None:
        return False
    if pattern.endswith("xx"):
        return status // 100 == int(pattern[0])
    return status == int(pattern)


def _path_matches(prefix: str, path: str | None) -> bool:
    if path is None:
        return False
    base = prefix.rstrip("/") or "/"
    return path == base or path.startswith(base.rstrip("/") + "/")


def _searchable(event: LogEvent) -> str:
    parts = [
        event.message,
        event.event_name,
        event.logger,
        event.error_type,
        event.error_message,
        event.stack_trace,
        event.path,
        event.request_id,
        json.dumps(event.extra, default=str) if event.extra else None,
    ]
    return "\n".join(part for part in parts if part).lower()


def _record_pattern(patterns: dict[tuple[str | None, ...], ErrorPattern], event: LogEvent) -> None:
    rank = level_rank(event.level)
    if not ((rank is not None and rank >= LEVEL_RANKS["WARNING"]) or event.error_type):
        return
    category = _category(event)
    detail = normalize_message(redact_text(event.error_message or event.message or ""))
    key = (event.level, event.event_name or event.logger, event.error_type, category, detail)
    pattern = patterns.get(key)
    if pattern is None:
        label = event.event_name or event.logger or "(no event name)"
        if category:
            label += f" ({category})"
        description = f"{event.error_type}: {detail}" if event.error_type else detail
        pattern = ErrorPattern(
            pattern=f"{label}: {description}" if description else label,
            level=event.level,
            event_name=event.event_name,
            error_type=event.error_type,
            category=category,
            count=0,
        )
        patterns[key] = pattern
    pattern.count += 1
    if event.timestamp is not None:
        pattern.first_seen = min(pattern.first_seen or event.timestamp, event.timestamp)
        pattern.last_seen = max(pattern.last_seen or event.timestamp, event.timestamp)
    if (
        event.request_id
        and event.request_id not in pattern.request_ids
        and len(pattern.request_ids) < EXAMPLE_REQUEST_IDS
    ):
        pattern.request_ids.append(event.request_id)


def _category(event: LogEvent) -> str | None:
    for key in ("reason", "error"):
        value = event.extra.get(key)
        if isinstance(value, str) and 0 < len(value) <= MAX_CATEGORY_LENGTH:
            return value
    return None


def _number_placeholder(match: re.Match[str]) -> str:
    number = match.group(0)
    if number.isdigit() and len(number) == 3 and 100 <= int(number) <= 599:
        return number
    return "<n>"


def _timing(event: LogEvent) -> RequestTiming:
    return RequestTiming(
        timestamp=event.timestamp,
        request_id=event.request_id,
        method=event.method,
        path=event.path,
        status=event.status,
        duration_ms=event.duration_ms,
        source=event.source,
        line=event.line,
    )


def _offset(start: datetime | None, moment: datetime | None) -> float | None:
    if start is None or moment is None:
        return None
    return round((moment - start).total_seconds() * 1000, 1)


def _level_counts(levels: Counter[str]) -> list[CountItem]:
    order = sorted(levels, key=lambda level: (LEVEL_RANKS.get(level, 100), level))
    return [CountItem(name=level, count=levels[level]) for level in order]


def _status_classes(statuses: Counter[int]) -> list[CountItem]:
    classes: Counter[str] = Counter()
    for code, count in statuses.items():
        classes[f"{code // 100}xx"] += count
    return [CountItem(name=name, count=classes[name]) for name in sorted(classes)]


def _summary_notes(
    entries: int, access_logs: int, without_timestamp: int, window: TimeWindow
) -> list[str]:
    if entries == 0:
        where = " in this time window" if window.active else ""
        return [f"No structured log entries were found{where}."]
    notes = []
    if without_timestamp:
        notes.append(
            f"Entries without a usable timestamp: {without_timestamp:,}. The time range "
            "above doesn't include them."
        )
    if access_logs == 0:
        notes.append(
            "No HTTP access-log entries were found, so there are no status or duration figures."
        )
    return notes


def _trace_notes(
    request_id: str, steps: list[TraceStep], access_logs: list[LogEvent], window: TimeWindow
) -> list[str]:
    if not steps:
        notes = [
            f"No log entries carry request ID '{request_id}'. IDs must match exactly, "
            "including case.",
            "Check that the request went to the service whose logs you read.",
        ]
        if window.active:
            notes.append("Only entries inside the time window were searched; try a wider --since.")
        return notes
    notes = []
    if not access_logs:
        notes.append(
            "No access-log entry was found for this request, so its final status and duration "
            "aren't known from these logs."
        )
    elif len(access_logs) > 1:
        notes.append(
            f"Access-log entries with this request ID: {len(access_logs)}. The ID was "
            "probably reused by more than one request, so this timeline may mix them."
        )
    undated = sum(step.offset_ms is None for step in steps)
    if undated:
        notes.append(
            f"Entries without a usable timestamp: {undated}. They're listed last, in the "
            "order they were read, without offsets."
        )
    return notes


def _input_notes(stats: InputStats) -> list[str]:
    notes = list(stats.notes)
    if stats.skipped_total:
        breakdown = ", ".join(
            f"{count:,} {_SKIP_REASONS.get(reason, reason)}"
            for reason, count in sorted(stats.skipped.items())
        )
        notes.append(
            f"Skipped lines that weren't usable log entries: {stats.skipped_total:,} "
            f"({breakdown}). The first was {stats.skipped_examples[0]}."
        )
    if stats.naive_timestamps:
        notes.append(f"Timestamps without a time zone, read as UTC: {stats.naive_timestamps:,}.")
    if stats.excluded_without_timestamp:
        notes.append(
            "Entries left out because they have no usable timestamp and a time window was "
            f"given: {stats.excluded_without_timestamp:,}."
        )
    return notes
