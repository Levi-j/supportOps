import traceback
from pathlib import Path
from typing import Annotated, Any

import typer
from typer.core import TyperGroup

from supportops import __version__, render
from supportops.cli import api, auth, config, db, health, investigate, logs
from supportops.cli.state import AppState
from supportops.errors import ExitCode, SupportOpsError


class SupportOpsGroup(TyperGroup):
    def invoke(self, ctx: Any) -> Any:
        try:
            return super().invoke(ctx)
        except SupportOpsError as exc:
            render.error(exc.message, exc.hint)
            raise typer.Exit(int(exc.exit_code)) from None
        except (typer.Exit, typer.Abort, typer.TyperException):
            raise
        except Exception as exc:
            debug = isinstance(ctx.obj, AppState) and ctx.obj.debug
            if debug:
                render.debug_traceback(traceback.format_exc())
            render.error(
                f"Unexpected error ({type(exc).__name__}): {exc}",
                None if debug else "Re-run with --debug to see the full traceback.",
            )
            raise typer.Exit(int(ExitCode.INCOMPLETE)) from None


app = typer.Typer(
    cls=SupportOpsGroup,
    name="supportops",
    help="Troubleshooting and incident-diagnostics toolkit for a small SaaS backend.",
    no_args_is_help=True,
    add_completion=False,
    pretty_exceptions_enable=False,
)
app.add_typer(config.app, name="config")
app.add_typer(api.app, name="api")
app.add_typer(logs.app, name="logs")
app.add_typer(db.app, name="db")
app.add_typer(auth.app, name="auth")
app.command("health")(health.health)
app.command("investigate")(investigate.investigate_command)


def _show_version(value: bool) -> None:
    if value:
        typer.echo(f"supportops {__version__}")
        raise typer.Exit()


@app.callback()
def main(
    ctx: typer.Context,
    env_file: Annotated[
        Path | None,
        typer.Option(
            "--env-file",
            help="Read settings from this env file instead of ./.env.",
            dir_okay=False,
        ),
    ] = None,
    debug: Annotated[
        bool, typer.Option("--debug", help="Show full tracebacks for unexpected errors.")
    ] = False,
    version: Annotated[
        bool,
        typer.Option(
            "--version", help="Show the version and exit.", callback=_show_version, is_eager=True
        ),
    ] = False,
) -> None:
    ctx.obj = AppState(env_file=env_file, debug=debug)
