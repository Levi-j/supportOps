import json

import typer
from pydantic import BaseModel
from rich.console import Console
from rich.markup import escape
from rich.table import Table
from rich.text import Text

from supportops.redaction import redact_text, redact_value


def _console(*, stderr: bool = False) -> Console:
    return Console(stderr=stderr, highlight=False)


def emit_json(model: BaseModel) -> None:
    data = redact_value(model.model_dump(mode="json"))
    typer.echo(redact_text(json.dumps(data, indent=2, ensure_ascii=False)))


def emit_line(text: str = "", style: str | None = None) -> None:
    _console().print(Text(redact_text(text), style=style or ""), soft_wrap=True)


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
