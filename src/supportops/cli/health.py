from typing import Annotated

import httpx
import typer

from supportops import render
from supportops.cli.state import get_state
from supportops.errors import ExitCode
from supportops.health import HealthReport, Verdict, run_health_check
from supportops.http_checks import HttpResult, create_client
from supportops.settings import load_settings
from supportops.targets import get_target

VERDICT_STYLES = {
    Verdict.HEALTHY: "bold green",
    Verdict.DEGRADED: "bold yellow",
    Verdict.DOWN: "bold red",
    Verdict.INCONCLUSIVE: "bold magenta",
}


def health(
    ctx: typer.Context,
    json_output: Annotated[
        bool, typer.Option("--json", help="Print machine-readable JSON.")
    ] = False,
) -> None:
    """Check whether the API is live and ready, and whether PostgreSQL answers from here."""
    settings = load_settings(get_state(ctx).env_file).settings
    profile = get_target(settings.target)
    with create_client(settings) as client:
        report = run_health_check(settings, profile, client)
    if json_output:
        render.emit_json(report)
    else:
        _print_report(report)
    if report.verdict is not Verdict.HEALTHY:
        raise typer.Exit(int(ExitCode.PROBLEM))


def _print_report(report: HealthReport) -> None:
    render.emit_line(f"Health check for {report.api_url}")
    render.emit_line()
    render.emit_line(f"{report.verdict}  {report.summary}", style=VERDICT_STYLES[report.verdict])
    render.emit_line()
    rows = [
        ["API liveness", _http_outcome(report.liveness), *_timing(report.liveness)],
        ["API readiness", _readiness_outcome(report), *_timing(report.readiness)],
        ["PostgreSQL", *_database_columns(report)],
    ]
    render.emit_table("Checks", ["Check", "Result", "Time", "Request ID"], rows)
    _print_list("Error details", _error_details(report))
    _print_list("Notes", report.notes)
    _print_list("Next steps", report.next_steps)


def _error_details(report: HealthReport) -> list[str]:
    details = [
        f"{name}: {result.failure.detail}"
        for name, result in (("API liveness", report.liveness), ("API readiness", report.readiness))
        if result is not None and result.failure is not None
    ]
    database = report.database
    if database is not None and not database.reachable and database.detail:
        details.append(f"PostgreSQL: {database.detail}")
    return details


def _http_outcome(result: HttpResult) -> str:
    path = httpx.URL(result.url).path
    if result.failure is not None:
        return f"GET {path} failed: {result.failure.category}"
    return f"GET {path} -> {result.status} {result.reason}"


def _readiness_outcome(report: HealthReport) -> str:
    if report.readiness is None:
        return "not checked (the API isn't live)"
    outcome = _http_outcome(report.readiness)
    view = report.api_database
    if view is not None and view.status:
        outcome += f" (database: {view.status}" + (f", {view.error})" if view.error else ")")
    return outcome


def _timing(result: HttpResult | None) -> list[str]:
    if result is None:
        return ["", ""]
    return [f"{result.duration_ms:.0f} ms", result.request_id]


def _database_columns(report: HealthReport) -> list[str]:
    database = report.database
    if database is None:
        return ["skipped (SUPPORTOPS_DB_URL is not set)", "", ""]
    if database.reachable:
        details = ", ".join(
            part
            for part in (
                f"PostgreSQL {database.server_version}" if database.server_version else "",
                "read-only session" if database.read_only else "",
            )
            if part
        )
        outcome = f"reachable as {database.target}" + (f" ({details})" if details else "")
    else:
        outcome = f"{database.error} for {database.target}"
    return [outcome, f"{database.latency_ms} ms", ""]


def _print_list(title: str, items: list[str]) -> None:
    if not items:
        return
    render.emit_line()
    render.emit_line(title, style="bold")
    for item in items:
        render.emit_line(f"  - {item}")
