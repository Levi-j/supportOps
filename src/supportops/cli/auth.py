from typing import Annotated

import typer

from supportops import render
from supportops.auth_checks import OUTCOME_LABELS, AuthCheckReport, check_authentication
from supportops.cli.state import get_state
from supportops.errors import ExitCode
from supportops.http_checks import create_client
from supportops.settings import load_settings
from supportops.targets import get_target

OUTCOME_STYLES = {
    "authenticated": "bold green",
    "unreachable": "bold magenta",
    "not_sent": "bold yellow",
}

app = typer.Typer(help="Troubleshoot API authentication.", no_args_is_help=True)


@app.command("check")
def check_command(
    ctx: typer.Context,
    key_env: Annotated[
        str | None,
        typer.Option("--key-env", help="Check the key in this environment variable instead."),
    ] = None,
    json_output: Annotated[
        bool, typer.Option("--json", help="Print machine-readable JSON.")
    ] = False,
) -> None:
    """Check an API key: its format, a live request to the API, and its database record."""
    settings = load_settings(get_state(ctx).env_file).settings
    profile = get_target(settings.target)
    with create_client(settings) as client:
        report = check_authentication(settings, profile, client, key_env=key_env)
    if json_output:
        render.emit_json(report)
    else:
        _print_report(report)
    if report.exit_code != ExitCode.OK:
        raise typer.Exit(int(report.exit_code))


def _print_report(report: AuthCheckReport) -> None:
    credential = report.credential
    render.emit_line(f"API key check for {report.api_url}", style="bold")
    if credential.present:
        render.emit_line(f"Key: {credential.masked} from {credential.source}")
        render.emit_line(
            "Format: " + ("looks like a valid key" if not credential.problems else "problems found")
        )
    else:
        render.emit_line(f"Key: none ({credential.source} is not set)")
    request = report.request
    if request is not None:
        took = f" in {request.duration_ms:,.0f} ms"
        answer = (
            f"{request.status} {request.reason}{took}"
            if request.status is not None
            else f"no response ({request.failure.category if request.failure else 'unknown'})"
        )
        render.emit_line(f"Live request: GET {request.url} -> {answer}")
        render.emit_line(f"Request ID: {request.request_id}")
    render.emit_line(
        f"Result: {OUTCOME_LABELS[report.outcome]}",
        style=OUTCOME_STYLES.get(report.outcome, "bold red"),
    )
    _print_list("Evidence", [f.text for f in report.findings if f.basis == "evidence"])
    _print_list("Interpretation", [f.text for f in report.findings if f.basis == "inference"])
    if report.database.status == "not_checked" and report.database.detail:
        _print_list("Database", [report.database.detail])
    _print_list("Next steps", report.next_steps)


def _print_list(title: str, items: list[str]) -> None:
    if not items:
        return
    render.emit_line()
    render.emit_line(title, style="bold")
    for item in items:
        render.emit_line(f"  - {item}")
