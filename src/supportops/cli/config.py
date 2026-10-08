from typing import Annotated

import typer

from supportops import render
from supportops.cli.state import get_state
from supportops.settings import describe_settings, load_settings

app = typer.Typer(help="Inspect the SupportOps configuration.", no_args_is_help=True)


@app.command("show")
def show(
    ctx: typer.Context,
    json_output: Annotated[
        bool, typer.Option("--json", help="Print machine-readable JSON.")
    ] = False,
) -> None:
    """Show the effective settings, where each value came from, with secrets masked."""
    report = describe_settings(load_settings(get_state(ctx).env_file))
    if json_output:
        render.emit_json(report)
        return
    render.emit_table(
        f"Configuration (env file: {report.env_file or 'none'})",
        ["Setting", "Value", "Source"],
        [[entry.setting, entry.value, entry.source] for entry in report.entries],
    )
