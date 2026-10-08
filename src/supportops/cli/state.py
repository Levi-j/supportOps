from dataclasses import dataclass
from pathlib import Path

import typer


@dataclass(frozen=True)
class AppState:
    env_file: Path | None = None
    debug: bool = False


def get_state(ctx: typer.Context) -> AppState:
    state = ctx.find_root().obj
    return state if isinstance(state, AppState) else AppState()
