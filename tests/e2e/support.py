import json
from pathlib import Path
from typing import Any

from typer.testing import CliRunner, Result

from supportops.cli.main import app
from supportops_lab.lab import ScenarioLab

E2E_PROJECT = "supportops-scenario-e2e"

runner = CliRunner()


def supportops(lab: ScenarioLab, *args: str) -> tuple[Result, dict[str, Any]]:
    result = runner.invoke(
        app, ["--env-file", str(lab.store.supportops_env), *args], env={"COLUMNS": "200"}
    )
    data = json.loads(result.stdout) if result.stdout.startswith("{") else {}
    return result, data


def investigate(lab: ScenarioLab, request_id: str, *args: str) -> tuple[Result, dict[str, Any]]:
    return supportops(lab, "investigate", request_id, "--json", *args)


def secrets_of(lab: ScenarioLab) -> list[str]:
    state = lab.store.load()
    assert state is not None
    return list(state.passwords)


def read(path: Path) -> str:
    return path.read_text(encoding="utf-8")
