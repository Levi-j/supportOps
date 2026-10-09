from datetime import date, datetime
from typing import Annotated, Any

import typer

from supportops import render
from supportops.cli.state import get_state
from supportops.db.runner import (
    CatalogListing,
    CheckResult,
    DbReport,
    describe_checks,
    parse_param_options,
    plan_checks,
    run_checks,
)
from supportops.errors import ConfigError, ExitCode
from supportops.settings import load_settings

MAX_TEXT_ROWS = 20
STATUS_STYLES = {
    "pass": "green",
    "fail": "bold red",
    "warn": "bold yellow",
    "info": "cyan",
    "error": "bold magenta",
    "skipped": "dim",
}
BINDING_NOTE = (
    "%(name)s marks a parameter. Values given with --param are sent to PostgreSQL separately "
    "from the SQL text, so they can't change the query."
)

app = typer.Typer(
    help="Run read-only diagnostic checks against the PostgreSQL database.",
    no_args_is_help=True,
)

CheckNames = Annotated[
    list[str] | None,
    typer.Argument(metavar="[CHECK]...", help="Check names, for example pg.connections."),
]
ShowSql = Annotated[bool, typer.Option("--show-sql", help="Show the SQL each check runs.")]
JsonOutput = Annotated[bool, typer.Option("--json", help="Print machine-readable JSON.")]


@app.command("checks")
def checks_command(
    names: CheckNames = None,
    show_sql: ShowSql = False,
    json_output: JsonOutput = False,
) -> None:
    """List the available checks, their parameters and, with --show-sql, their SQL."""
    listing = describe_checks(names or [], show_sql=show_sql)
    if json_output:
        render.emit_json(listing)
        return
    _print_listing(listing)


@app.command("run")
def run_command(
    ctx: typer.Context,
    names: CheckNames = None,
    run_all: Annotated[
        bool, typer.Option("--all", help="Run every check that has the parameters it needs.")
    ] = False,
    param: Annotated[
        list[str] | None,
        typer.Option(
            "--param",
            metavar="NAME=VALUE",
            help="A value for a check parameter, for example prefix=bk_juniper01. Repeatable.",
        ),
    ] = None,
    show_sql: ShowSql = False,
    json_output: JsonOutput = False,
) -> None:
    """Run read-only diagnostic checks. Exit code 1 means a check found a problem."""
    plan = plan_checks(names or [], run_all=run_all, parameters=parse_param_options(param or []))
    settings = load_settings(get_state(ctx).env_file).settings
    if settings.db_url is None:
        raise ConfigError(
            "SUPPORTOPS_DB_URL is not set, so there is no database to check.",
            hint="Set it in .env, for example "
            "postgresql://supportops_ro:<password>@127.0.0.1:5433/billing",
        )
    report = run_checks(
        settings.db_url,
        plan,
        connect_timeout_seconds=settings.connect_timeout_seconds,
        show_sql=show_sql,
    )
    if json_output:
        render.emit_json(report)
    else:
        _print_report(report)
    if report.exit_code != ExitCode.OK:
        raise typer.Exit(int(report.exit_code))


def _print_listing(listing: CatalogListing) -> None:
    rows = []
    for check in listing.checks:
        parameters = ", ".join(
            f"{parameter.name}"
            + (" (required)" if parameter.required else "")
            + (f" (default {parameter.default})" if parameter.default is not None else "")
            for parameter in check.parameters
        )
        if check.requirement and not any(parameter.required for parameter in check.parameters):
            parameters = f"{parameters} (one of them)"
        rows.append([check.name, check.pack, check.description, parameters or "-"])
    render.emit_table("Diagnostic checks", ["Check", "Pack", "What it shows", "Parameters"], rows)
    if any(check.sql for check in listing.checks):
        for check in listing.checks:
            render.emit_line()
            render.emit_line(f"-- {check.name}", style="bold")
            for line in (check.sql or "").splitlines():
                render.emit_line(line)
        render.emit_line()
        render.emit_line(BINDING_NOTE, style="dim")
    else:
        render.emit_line("Add --show-sql to see the SQL each check runs.", style="dim")


def _print_report(report: DbReport) -> None:
    session = report.session
    render.emit_line(f"Database checks on {report.target}", style="bold")
    render.emit_line(
        f"Session: {session.role} on {session.database}, PostgreSQL {session.server_version}, "
        + ("read-only" if session.read_only else "NOT read-only")
    )
    width = max(len(result.name) for result in report.results) if report.results else 0
    render.emit_line()
    for result in report.results:
        duration = f"  ({result.duration_ms:,.0f} ms)" if result.duration_ms is not None else ""
        render.emit_line(
            f"{result.status.upper():<8} {result.name:<{width}}  {result.summary}{duration}",
            style=STATUS_STYLES[result.status],
        )
        if result.status in ("fail", "warn", "info") and result.rows:
            _print_rows(result)
        for note in result.notes:
            render.emit_line(
                f"         - {note}", style="yellow" if result.status == "warn" else None
            )
        if result.sql:
            _print_sql(result)
        details = result.notes or result.sql or (result.rows and result.status != "pass")
        if details and result is not report.results[-1]:
            render.emit_line()
    counts = ", ".join(f"{count} {status}" for status, count in report.counts.items())
    render.emit_line()
    render.emit_line(f"Result: {counts}", style="bold")
    if report.counts.get("skipped"):
        render.emit_line(
            "Skipped checks need a parameter; run them by name with --param.", style="dim"
        )


def _print_rows(result: CheckResult) -> None:
    if len(result.rows) == 1:
        width = max(len(column) for column in result.columns)
        for column in result.columns:
            render.emit_line(f"         {column:<{width}}  {_cell(result.rows[0][column])}")
        return
    shown = result.rows[:MAX_TEXT_ROWS]
    render.emit_table(
        result.name,
        result.columns,
        [[_cell(row[column]) for column in result.columns] for row in shown],
    )
    if len(result.rows) > len(shown):
        render.emit_line(
            f"         {len(result.rows) - len(shown)} more rows; use --json to see them all."
        )


def _print_sql(result: CheckResult) -> None:
    bound = ", ".join(f"{name}={value!r}" for name, value in result.parameters.items())
    render.emit_line("         SQL:", style="dim")
    for line in (result.sql or "").splitlines():
        render.emit_line(f"           {line}", style="dim")
    if bound:
        render.emit_line(
            f"         Bound parameters (sent separately, never inserted into the SQL): {bound}",
            style="dim",
        )


def _cell(value: Any) -> str:
    if value is None:
        return "-"
    if isinstance(value, bool):
        return "yes" if value else "no"
    if isinstance(value, datetime):
        return value.strftime("%Y-%m-%d %H:%M:%S %Z").strip()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, list):
        return ", ".join(str(item) for item in value) or "-"
    return str(value)
