from pathlib import Path
from typing import Annotated

import typer

from supportops import render
from supportops.cli.state import get_state
from supportops.errors import ExitCode
from supportops.http_checks import create_client
from supportops.investigation.engine import investigate, validate_request_id
from supportops.investigation.models import Finding, Investigation
from supportops.investigation.reporting import (
    configured_secrets,
    ensure_maskable,
    ensure_new_report,
    mask_secrets,
    write_report,
)
from supportops.investigation.text import (
    current_state_notes,
    describe_coverage,
    describe_database_status,
    describe_event,
    describe_health_status,
    describe_impact,
    describe_origin,
    describe_scope,
    describe_window,
    format_time,
    shared_gaps,
)
from supportops.logs.analysis import time_window
from supportops.logs.parser import read_logs
from supportops.settings import load_settings
from supportops.targets import get_target

CONFIDENCE_STYLES = {"confirmed": "bold green", "likely": "bold yellow", "possible": "bold magenta"}
KIND_STYLES = {
    "exception": "red",
    "database": "red",
    "authentication": "yellow",
    "validation": "yellow",
}


def investigate_command(
    ctx: typer.Context,
    request_id: Annotated[
        str,
        typer.Argument(
            help="The request ID to investigate, from the X-Request-Id response header or the "
            "request_id in an error body."
        ),
    ],
    sources: Annotated[
        list[str] | None,
        typer.Argument(
            metavar="[SOURCE]...",
            help="Where to read logs: a file path, '-' for standard input, or "
            "docker:<container-name>. Defaults to SUPPORTOPS_LOG_SOURCE.",
            show_default=False,
        ),
    ] = None,
    since: Annotated[
        str | None,
        typer.Option(
            "--since",
            help="Only read entries at or after this time: a duration such as 2h or an ISO "
            "8601 timestamp.",
        ),
    ] = None,
    until: Annotated[
        str | None,
        typer.Option("--until", help="Only read entries before this time (same formats)."),
    ] = None,
    no_db: Annotated[
        bool,
        typer.Option("--no-db", help="Skip the database checks even if SUPPORTOPS_DB_URL is set."),
    ] = False,
    report: Annotated[
        Path | None,
        typer.Option(
            "--report",
            metavar="FILE",
            dir_okay=False,
            help="Also write a redacted Markdown report draft to this new file.",
        ),
    ] = None,
    json_output: Annotated[
        bool, typer.Option("--json", help="Print machine-readable JSON.")
    ] = False,
) -> None:
    """Investigate one request: trace its logs, run only read-only checks, explain the cause.

    Exit code 0 means at least one finding; 1 means the investigation was inconclusive.
    """
    validate_request_id(request_id)
    if report is not None:
        ensure_new_report(report)
    settings = load_settings(get_state(ctx).env_file).settings
    secrets = configured_secrets(settings)
    ensure_maskable(secrets)
    profile = get_target(settings.target)
    window = time_window(since, until)
    log_input = read_logs(sources or [settings.log_source], since=window.since)
    with create_client(settings) as client:
        result = investigate(
            log_input, request_id, window, settings, profile, client, use_database=not no_db
        )
    result = mask_secrets(result, secrets)
    if json_output:
        render.emit_json(result)
    else:
        _print_investigation(result, suggest_report=report is None)
    if report is not None:
        written = write_report(result, report, secrets)
        message = f"Report draft written to {written}"
        if json_output:
            typer.echo(message, err=True)
        else:
            render.emit_line()
            render.emit_line(message, style="bold")
    if result.exit_code != ExitCode.OK:
        raise typer.Exit(int(result.exit_code))


def _print_investigation(result: Investigation, *, suggest_report: bool) -> None:
    render.emit_line(f"Investigation of request {result.request_id}", style="bold")
    stats = result.logs.trace.input
    render.emit_line(
        f"Logs: {', '.join(result.sources)} ({stats.lines:,} lines, {stats.parsed:,} entries)"
    )
    request = result.logs.request
    if request is not None:
        described = " ".join(part for part in (request.method, request.path) if part)
        if request.status is not None:
            described += f" -> {request.status}"
        if request.duration_ms is not None:
            described += f" in {request.duration_ms:,.0f} ms"
        account = f", account {request.account_id}" if request.account_id else ""
        render.emit_line(f"Request: {described} at {format_time(request.timestamp)}{account}")
    render.emit_line(f"Database: {describe_database_status(result.live)}")
    render.emit_line(f"API health: {describe_health_status(result.live)}")
    render.emit_line()
    style = "bold" if result.verdict == "FINDINGS" else "bold magenta"
    render.emit_line(f"{result.verdict}  {result.summary}", style=style)
    shown: set[str] = set()
    for number, item in enumerate(result.findings, start=1):
        _print_finding(number, item, shown)
    _print_evidence(result)
    _print_timeline(result)
    _print_impact(result)
    _print_list("Open questions", result.open_questions)
    _print_list("Notes", result.notes)
    footer = current_state_notes(result.live)
    if suggest_report:
        footer.append("Use --report FILE to write an internal Markdown draft.")
    if footer:
        render.emit_line()
        for line in footer:
            render.emit_line(line, style="dim")


def _print_finding(number: int, item: Finding, shown: set[str]) -> None:
    render.emit_line()
    render.emit_line(
        f"{number}. {item.title}  [{item.confidence.upper()}]",
        style=CONFIDENCE_STYLES[item.confidence],
    )
    render.emit_line(f"   {item.summary}")
    render.emit_line(f"   Evidence: {', '.join(item.evidence_ids)}", style="dim")
    steps = [step for step in item.next_steps if step not in shown]
    shown.update(steps)
    for title, entries in (
        ("Interpretation (not verified)", item.inferences),
        ("Contradictions", item.contradictions),
        ("Caveats", item.caveats),
        ("Next steps", steps),
    ):
        if entries:
            render.emit_line(
                f"   {title}:", style="bold red" if title == "Contradictions" else None
            )
            for entry in entries:
                render.emit_line(f"     - {entry}")
    if item.escalation is not None:
        render.emit_line(
            f"   Escalate to {item.escalation.team} ({item.escalation.severity}): "
            f"{item.escalation.reason}",
            style="bold",
        )


def _print_evidence(result: Investigation) -> None:
    if not result.evidence:
        return
    render.emit_line()
    render.emit_line("Evidence", style="bold")
    for item in result.evidence:
        render.emit_line(f"  {item.id}  {describe_origin(item)}, {item.reference}")
        render.emit_line(f"      {item.summary}", style="dim")


def _print_timeline(result: Investigation) -> None:
    steps = result.logs.trace.steps
    if not steps:
        return
    render.emit_line()
    render.emit_line("Timeline", style="bold")
    for step in steps:
        offset = "?" if step.offset_ms is None else f"+{step.offset_ms:,.0f} ms"
        render.emit_line(
            f"  {offset:>10}  {describe_event(step.event)}", style=KIND_STYLES.get(step.kind or "")
        )
        if step.event.stack_trace:
            for line in step.event.stack_trace.rstrip().splitlines():
                render.emit_line(f"{'':14}{line}", style="dim")


def _print_impact(result: Investigation) -> None:
    items = result.logs.impact
    if not items:
        return
    render.emit_line()
    render.emit_line(
        f"Impact (same error signature, {describe_window(items)}, counted by request ID)",
        style="bold",
    )
    shared = shared_gaps(items)
    for item in items:
        render.emit_line(f"  - {item.signature}")
        render.emit_line(f"    {describe_impact(item)}")
        render.emit_line(f"    {describe_scope(item)}")
        for gap in item.gaps:
            if gap not in shared:
                render.emit_line(f"    Coverage gap: {gap.detail}", style="dim")
    partial = any(item.coverage == "partial" for item in items)
    render.emit_line(f"  Coverage: {describe_coverage(items)}", style="yellow" if partial else None)
    for gap in shared:
        render.emit_line(f"    - {gap.detail}", style="dim")


def _print_list(title: str, items: list[str]) -> None:
    if not items:
        return
    render.emit_line()
    render.emit_line(title, style="bold")
    for item in items:
        render.emit_line(f"  - {item}")
