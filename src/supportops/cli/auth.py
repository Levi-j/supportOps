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

TOKEN_STRUCTURES = {
    "missing": "no token",
    "not_jwt": "not a JWT",
    "malformed": "a JWT that can't be decoded",
    "jwt": "a decodable JWT (signature not checked)",
}

app = typer.Typer(help="Troubleshoot API authentication.", no_args_is_help=True)


@app.command("check")
def check_command(
    ctx: typer.Context,
    key_env: Annotated[
        str | None,
        typer.Option(
            "--key-env",
            help="Check the API key or bearer token in this environment variable instead.",
        ),
    ] = None,
    json_output: Annotated[
        bool, typer.Option("--json", help="Print machine-readable JSON.")
    ] = False,
) -> None:
    """Check the configured credential: its format, a live request, and supporting records.

    For API keys this includes the key's database record. For bearer tokens (JWTs) it decodes
    the claims locally without checking the signature, so they are reported as unverified.
    """
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
    token = report.token
    noun = "Token" if token is not None else "Key"
    render.emit_line(
        f"{'Bearer token' if token is not None else 'API key'} check for {report.api_url}",
        style="bold",
    )
    if credential.present:
        render.emit_line(f"{noun}: {credential.masked} from {credential.source}")
        if token is not None:
            render.emit_line(f"Format: {TOKEN_STRUCTURES[token.structure]}")
        else:
            render.emit_line(
                "Format: "
                + ("looks like a valid key" if not credential.problems else "problems found")
            )
    elif token is not None:
        render.emit_line(f"No bearer token is set in {credential.source}.")
    else:
        render.emit_line(f"{noun}: none ({credential.source} is not set)")
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
    _print_list(
        "Token claims (decoded locally, unverified and untrusted)",
        [f.text for f in report.findings if f.basis == "unverified_claim"],
    )
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
