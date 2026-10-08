import json
from datetime import datetime
from typing import Annotated, Any

import typer

from supportops import render
from supportops.cli.state import get_state
from supportops.errors import ExitCode
from supportops.logs.analysis import (
    LogSummary,
    RequestTiming,
    RequestTrace,
    SearchResult,
    TimeWindow,
    event_kind,
    is_access_log,
    search,
    search_filters,
    summarize,
    time_window,
    trace,
)
from supportops.logs.parser import LogEvent, LogInput, read_logs
from supportops.redaction import redact_value
from supportops.settings import load_settings

MAX_LIMIT = 1000
MAX_DETAIL_CHARACTERS = 200
KIND_STYLES = {
    "exception": "red",
    "database": "red",
    "authentication": "yellow",
    "validation": "yellow",
}

app = typer.Typer(
    help="Read structured JSON logs: summarize them, search them, or trace one request.",
    no_args_is_help=True,
)

Sources = Annotated[
    list[str] | None,
    typer.Argument(
        metavar="[SOURCE]...",
        help="Where to read logs: a file path, '-' for standard input, or "
        "docker:<container-name>. Defaults to SUPPORTOPS_LOG_SOURCE.",
        show_default=False,
    ),
]
Since = Annotated[
    str | None,
    typer.Option(
        "--since",
        help="Only entries at or after this time: a duration such as 15m, 2h or 1d, "
        "or an ISO 8601 timestamp.",
    ),
]
Until = Annotated[
    str | None,
    typer.Option("--until", help="Only entries before this time (same formats as --since)."),
]
JsonOutput = Annotated[bool, typer.Option("--json", help="Print machine-readable JSON.")]


@app.command("summary")
def summary_command(
    ctx: typer.Context,
    sources: Sources = None,
    since: Since = None,
    until: Until = None,
    top: Annotated[
        int,
        typer.Option("--top", min=1, max=50, help="How many events and error patterns to list."),
    ] = 10,
    json_output: JsonOutput = False,
) -> None:
    """Summarize log entries: levels, events, HTTP statuses, error patterns and slow requests."""
    log_input, window = _read(ctx, sources, since, until)
    report = summarize(log_input, window, top)
    if json_output:
        render.emit_json(report)
    else:
        _print_summary(report)
    if report.entries == 0:
        raise typer.Exit(int(ExitCode.PROBLEM))


@app.command("search")
def search_command(
    ctx: typer.Context,
    sources: Sources = None,
    request_id: Annotated[
        str | None, typer.Option("--request-id", help="Only entries with this request ID.")
    ] = None,
    level: Annotated[
        str | None,
        typer.Option("--level", help="Only entries at this level or more severe, e.g. WARNING."),
    ] = None,
    event: Annotated[
        list[str] | None,
        typer.Option(
            "--event", help="Only this event name; 'auth.*' matches a prefix. Repeatable."
        ),
    ] = None,
    status: Annotated[
        list[str] | None,
        typer.Option("--status", help="Only this HTTP status (404) or class (4xx). Repeatable."),
    ] = None,
    path: Annotated[
        str | None,
        typer.Option("--path", help="Only this request path and the paths below it."),
    ] = None,
    text: Annotated[
        str | None,
        typer.Option("--text", help="Only entries containing this text (case-insensitive)."),
    ] = None,
    since: Since = None,
    until: Until = None,
    limit: Annotated[
        int,
        typer.Option(
            "--limit", min=1, max=MAX_LIMIT, help="Show at most this many of the newest matches."
        ),
    ] = 50,
    json_output: JsonOutput = False,
) -> None:
    """Find log entries by request ID, level, event, status, path, text or time."""
    filters = search_filters(
        request_id=request_id,
        level=level,
        event_names=event,
        statuses=status,
        path=path,
        text=text,
    )
    log_input, window = _read(ctx, sources, since, until)
    result = search(log_input, filters, window, limit)
    if json_output:
        render.emit_json(result)
    else:
        _print_search(result)
    if result.matched == 0:
        raise typer.Exit(int(ExitCode.PROBLEM))


@app.command("trace")
def trace_command(
    ctx: typer.Context,
    request_id: Annotated[str, typer.Argument(help="The request ID to follow.")],
    sources: Sources = None,
    since: Since = None,
    until: Until = None,
    json_output: JsonOutput = False,
) -> None:
    """Show every log entry for one request ID as a timeline."""
    log_input, window = _read(ctx, sources, since, until)
    report = trace(log_input, request_id, window)
    if json_output:
        render.emit_json(report)
    else:
        _print_trace(report)
    if not report.found:
        raise typer.Exit(int(ExitCode.PROBLEM))


def _read(
    ctx: typer.Context, sources: list[str] | None, since: str | None, until: str | None
) -> tuple[LogInput, TimeWindow]:
    settings = load_settings(get_state(ctx).env_file).settings
    window = time_window(since, until)
    return read_logs(sources or [settings.log_source], since=window.since), window


def _print_summary(report: LogSummary) -> None:
    stats = report.input
    render.emit_line(f"Log summary for {', '.join(stats.sources)}", style="bold")
    render.emit_line(
        f"Read {stats.lines:,} lines: {stats.parsed:,} log entries, "
        f"{stats.skipped_total:,} skipped, {stats.blank_lines:,} blank"
    )
    window = _describe_window(report.window)
    render.emit_line(
        f"{report.entries:,} entries{window}"
        + (
            f", from {_time(report.first_seen)} to {_time(report.last_seen)}"
            if report.first_seen
            else ""
        )
    )
    if report.entries:
        render.emit_line(
            "Levels: " + "   ".join(f"{item.name} {item.count:,}" for item in report.levels)
        )
    if report.event_names:
        render.emit_line()
        render.emit_table(
            "Events",
            ["Event", "Count"],
            [[item.name, f"{item.count:,}"] for item in report.event_names],
        )
        if report.other_event_names:
            render.emit_line(f"... and {report.other_event_names} other event names")
    if report.statuses:
        render.emit_line()
        render.emit_table(
            "HTTP statuses",
            ["Status", "Count"],
            [[item.name, f"{item.count:,}"] for item in report.statuses],
        )
        classes = "   ".join(f"{item.name} {item.count:,}" for item in report.status_classes)
        render.emit_line(f"From {report.access_logs:,} access-log entries. By class: {classes}")
    if report.error_patterns:
        render.emit_line()
        render.emit_table(
            "Warning and error patterns",
            ["Count", "Level", "Pattern", "Last seen", "Example request IDs"],
            [
                [
                    f"{item.count:,}",
                    item.level or "-",
                    item.pattern,
                    _time(item.last_seen),
                    ", ".join(item.request_ids),
                ]
                for item in report.error_patterns
            ],
        )
        if report.other_error_patterns:
            render.emit_line(f"... and {report.other_error_patterns} other patterns")
    if report.slowest:
        render.emit_line()
        render.emit_table(
            "Slowest requests",
            ["Duration", "Status", "Request", "Request ID", "Time"],
            [
                [
                    f"{item.duration_ms:,.0f} ms",
                    str(item.status or "-"),
                    _request(item),
                    item.request_id or "-",
                    _time(item.timestamp),
                ]
                for item in report.slowest
            ],
        )
    _print_notes(report.notes)


def _print_search(result: SearchResult) -> None:
    stats = result.input
    shown = len(result.events)
    matched = _count(result.matched, "matching entry", "matching entries")
    render.emit_line(
        f"{matched} in {', '.join(stats.sources)}"
        + (f" (showing the latest {shown})" if shown < result.matched else ""),
        style="bold",
    )
    conditions = _describe_filters(result)
    if conditions:
        render.emit_line(f"Filters: {conditions}")
    if result.events:
        render.emit_line()
    for event in result.events:
        render.emit_line(_event_line(event), style=_style(event))
    _print_notes(result.notes)


def _print_trace(report: RequestTrace) -> None:
    sources = ", ".join(report.input.sources)
    if not report.found:
        render.emit_line(
            f"No log entries for request ID {report.request_id} in {sources}", style="bold"
        )
        render.emit_line(f"Searched {_count(report.input.parsed, 'log entry', 'log entries')}.")
        _print_notes(report.notes)
        return
    render.emit_line(
        f"{_count(len(report.steps), 'log entry', 'log entries')} for request ID "
        f"{report.request_id} in {sources}",
        style="bold",
    )
    if report.access_log is not None:
        render.emit_line(f"Access log: {_request(report.access_log)}{_took(report.access_log)}")
    if report.first_seen is not None:
        span = f", spanning {report.elapsed_ms:,.0f} ms" if len(report.steps) > 1 else ""
        render.emit_line(f"First entry at {_time(report.first_seen)}{span}")
    if report.highlights:
        render.emit_line("Highlights: " + ", ".join(report.highlights), style="bold yellow")
    render.emit_line()
    width = max(len(_name(step.event)) for step in report.steps)
    for step in report.steps:
        event = step.event
        kind = f"   [{step.kind}]" if step.kind not in (None, "access") else ""
        render.emit_line(
            f"  {_offset(step.offset_ms):>10}  {event.level or '-':<8} "
            f"{_name(event):<{width}}  {_description(event)}{kind}",
            style=_style(event),
        )
        details = _details(event)
        if details:
            render.emit_line(f"{'':14}{details}", style="dim")
        if event.stack_trace:
            render.emit_line(f"{'':14}Stack trace:", style="red")
            for line in event.stack_trace.rstrip().splitlines():
                render.emit_line(f"{'':16}{line}", style="dim")
    _print_notes(report.notes)


def _print_notes(notes: list[str]) -> None:
    if not notes:
        return
    render.emit_line()
    render.emit_line("Notes", style="bold")
    for note in notes:
        render.emit_line(f"  - {note}")


def _event_line(event: LogEvent) -> str:
    when = _time(event.timestamp) if event.timestamp else (event.timestamp_text or "(no time)")
    parts = [when, f"{event.level or '-':<8}", _name(event)]
    if event.request_id:
        parts.append(f"request_id={event.request_id}")
    parts.append(_description(event))
    details = _details(event)
    if details:
        parts.append(details)
    return "  ".join(part for part in parts if part)


def _name(event: LogEvent) -> str:
    return event.event_name or event.logger or "-"


def _description(event: LogEvent) -> str:
    if is_access_log(event):
        return _request(event) + (
            f" ({event.duration_ms:,.0f} ms)" if event.duration_ms is not None else ""
        )
    text = event.message or ""
    if event.error_type or event.error_message:
        error = ": ".join(part for part in (event.error_type, event.error_message) if part)
        text = f"{text} - {error}" if text else error
    return text


def _details(event: LogEvent) -> str:
    fields: dict[str, Any] = redact_value(dict(event.extra))
    if event.account_id:
        fields = {"account_id": event.account_id, **fields}
    return " ".join(
        f"{key}={_compact(value)}" for key, value in fields.items() if value is not None
    )


def _compact(value: Any) -> str:
    if isinstance(value, list):
        text = ",".join(str(item) for item in value)
    elif isinstance(value, dict):
        text = json.dumps(value, separators=(",", ":"), default=str)
    else:
        text = str(value)
    if len(text) > MAX_DETAIL_CHARACTERS:
        text = text[:MAX_DETAIL_CHARACTERS] + "..."
    return text


def _request(item: LogEvent | RequestTiming) -> str:
    request = " ".join(part for part in (item.method, item.path) if part) or "request"
    return f"{request} -> {item.status}" if item.status is not None else request


def _took(item: RequestTiming) -> str:
    return f" in {item.duration_ms:,.0f} ms" if item.duration_ms is not None else ""


def _style(event: LogEvent) -> str | None:
    kind = event_kind(event)
    if kind in KIND_STYLES:
        return KIND_STYLES[kind]
    if event.status is not None and event.status >= 500:
        return "red"
    if event.status is not None and event.status >= 400:
        return "yellow"
    return None


def _describe_window(window: TimeWindow) -> str:
    if window.since and window.until:
        return f" between {_time(window.since)} and {_time(window.until)}"
    if window.since:
        return f" since {_time(window.since)}"
    if window.until:
        return f" before {_time(window.until)}"
    return ""


def _describe_filters(result: SearchResult) -> str:
    filters = result.filters
    parts = []
    if filters.request_id:
        parts.append(f"request ID {filters.request_id}")
    if filters.min_level:
        parts.append(f"level {filters.min_level} or above")
    if filters.event_names:
        parts.append("event " + " or ".join(filters.event_names))
    if filters.statuses:
        parts.append("status " + " or ".join(filters.statuses))
    if filters.path:
        parts.append(f"path {filters.path}")
    if filters.text:
        parts.append(f"text '{filters.text}'")
    window = _describe_window(result.window).strip()
    if window:
        parts.append(window)
    return ", ".join(parts)


def _count(number: int, singular: str, plural: str) -> str:
    return f"{number:,} {singular if number == 1 else plural}"


def _offset(offset_ms: float | None) -> str:
    return "?" if offset_ms is None else f"+{offset_ms:,.0f} ms"


def _time(moment: datetime | None) -> str:
    if moment is None:
        return "-"
    return moment.strftime("%Y-%m-%dT%H:%M:%S.") + f"{moment.microsecond // 1000:03d}Z"
