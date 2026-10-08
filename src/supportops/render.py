import typer
from pydantic import BaseModel
from rich.console import Console
from rich.markup import escape
from rich.table import Table

from supportops.redaction import redact_text


def _console(*, stderr: bool = False) -> Console:
    return Console(stderr=stderr, highlight=False)


def emit_json(model: BaseModel) -> None:
    typer.echo(redact_text(model.model_dump_json(indent=2)))


def emit_table(title: str, columns: list[str], rows: list[list[str]]) -> None:
    table = Table(title=escape(redact_text(title)))
    for column in columns:
        table.add_column(column)
    for row in rows:
        table.add_row(*(escape(redact_text(cell)) for cell in row))
    _console().print(table)


def error(message: str, hint: str | None = None) -> None:
    console = _console(stderr=True)
    console.print(f"[bold red]Error:[/bold red] {escape(redact_text(message))}")
    if hint:
        console.print(f"[dim]Hint:[/dim] {escape(redact_text(hint))}")


def debug_traceback(text: str) -> None:
    typer.echo(redact_text(text), err=True)
