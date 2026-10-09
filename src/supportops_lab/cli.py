from dataclasses import dataclass
from pathlib import Path
from typing import Annotated

import typer

from supportops import render
from supportops.cli.main import SupportOpsGroup
from supportops.errors import ExitCode
from supportops_lab.lab import Baseline, LabReport, ScenarioLab, StartResult, display_path
from supportops_lab.ownership import DEFAULT_PROJECT
from supportops_lab.paths import STATE_ROOT
from supportops_lab.scenarios import SCENARIOS

app = typer.Typer(
    cls=SupportOpsGroup,
    name="supportops-lab",
    help="Run reproducible incident scenarios in a disposable lab. It never touches the "
    "supportops lab started from compose.yaml.",
    no_args_is_help=True,
    add_completion=False,
    pretty_exceptions_enable=False,
)


@dataclass(frozen=True)
class LabOptions:
    project: str
    state_root: Path


def make_lab(options: LabOptions) -> ScenarioLab:
    return ScenarioLab(options.project, options.state_root)


@app.callback()
def main(
    ctx: typer.Context,
    project: Annotated[
        str,
        typer.Option(
            "--project",
            help="Scenario lab Compose project: supportops-scenario or supportops-scenario-<x>.",
        ),
    ] = DEFAULT_PROJECT,
    state_dir: Annotated[
        Path | None,
        typer.Option(
            "--state-dir",
            file_okay=False,
            help="Directory for the generated lab state (default: .lab in the checkout).",
        ),
    ] = None,
) -> None:
    ctx.obj = LabOptions(project=project, state_root=state_dir or STATE_ROOT)


def _lab(ctx: typer.Context) -> ScenarioLab:
    options = ctx.find_root().obj
    if not isinstance(options, LabOptions):
        options = LabOptions(DEFAULT_PROJECT, STATE_ROOT)
    return make_lab(options)


@app.command("up")
def up_command(
    ctx: typer.Context,
    no_build: Annotated[
        bool, typer.Option("--no-build", help="Reuse the existing billing-api image.")
    ] = False,
) -> None:
    """Start the disposable scenario lab and check that it is healthy and consistent."""
    lab = _lab(ctx)
    lab.up(build=not no_build)
    baseline = lab.baseline()
    _print_status(lab.status())
    _print_baseline(baseline)
    _exit_unless(baseline.clean)


@app.command("down")
def down_command(ctx: typer.Context) -> None:
    """Remove the scenario lab's containers and network. Its database is never kept."""
    lab = _lab(ctx)
    stopped = lab.down()
    if stopped:
        render.emit_line(f"Removed scenario lab {lab.project}.", style="bold")
    else:
        render.emit_line(f"Scenario lab {lab.project} wasn't running.")


@app.command("reset")
def reset_command(ctx: typer.Context) -> None:
    """Recreate the scenario lab from scratch and check the clean baseline."""
    lab = _lab(ctx)
    baseline = lab.reset()
    render.emit_line(f"Scenario lab {lab.project} was recreated with a fresh database.")
    _print_baseline(baseline)
    _exit_unless(baseline.clean)


@app.command("status")
def status_command(
    ctx: typer.Context,
    json_output: Annotated[
        bool, typer.Option("--json", help="Print machine-readable JSON.")
    ] = False,
) -> None:
    """Show the scenario lab's containers, ports, ownership and active scenario."""
    report = _lab(ctx).status()
    if json_output:
        render.emit_json(report)
    else:
        _print_status(report)
    _exit_unless(report.ownership != "unverified")


@app.command("scenarios")
def scenarios_command() -> None:
    """List the incident scenarios."""
    render.emit_table(
        "Incident scenarios",
        ["ID", "Slug", "Title", "Report"],
        [[item.id, item.slug, item.title, item.report] for item in SCENARIOS],
    )
    render.emit_line("Start one with: uv run supportops-lab start INC-001", style="dim")


@app.command("start")
def start_command(
    ctx: typer.Context,
    scenario: Annotated[str, typer.Argument(help="Scenario ID or slug, e.g. INC-001.")],
) -> None:
    """Recreate the scenario lab, apply one incident and replay the customer's requests."""
    result = _lab(ctx).start(scenario)
    _print_start(result)
    _exit_unless(result.reproduced)


def _print_status(report: LabReport) -> None:
    render.emit_line(f"Scenario lab {report.project}: {report.status}", style="bold")
    render.emit_line(f"Ownership: {report.ownership}")
    if report.lab_id:
        render.emit_line(f"Lab ID: {report.lab_id}")
    for container in report.containers:
        health = f", {container.health}" if container.health else ""
        render.emit_line(f"  {container.name}: {container.status}{health}")
    if report.api_url:
        render.emit_line(f"API: {report.api_url}")
    if report.database:
        render.emit_line(f"PostgreSQL: {report.database}")
    if report.faults:
        render.emit_line(f"Lab faults: {', '.join(report.faults)}")
    if report.scenario:
        render.emit_line(f"Active scenario: {report.scenario}")
    if report.supportops_env:
        env_file = display_path(Path(report.supportops_env))
        render.emit_line(f"Investigate with: uv run supportops --env-file {env_file} ...")
    for problem in report.problems:
        render.emit_line(f"Problem: {problem}", style="bold red")
    if report.shell_overrides:
        render.emit_line(
            "Warning: these variables are set in this shell and override --env-file for "
            "supportops: " + ", ".join(report.shell_overrides),
            style="yellow",
        )


def _print_baseline(baseline: Baseline) -> None:
    checks = ", ".join(f"{name} {status}" for name, status in baseline.checks.items())
    style = "bold green" if baseline.clean else "bold red"
    render.emit_line(
        f"Baseline: {'clean' if baseline.clean else 'NOT clean'} (health {baseline.verdict}; "
        f"{checks})",
        style=style,
    )


def _print_start(result: StartResult) -> None:
    render.emit_line(f"{result.scenario}: {result.title}", style="bold")
    render.emit_line(f'Customer report: "{result.customer_report}"')
    render.emit_line(f"Simulation: {result.simulation}", style="dim")
    render.emit_line()
    render.emit_line(f"Customer requests sent to the scenario lab at {result.api_url}:")
    for item in result.requests:
        outcome = str(item.status) if item.status is not None else f"failed ({item.error})"
        marker = "" if item.status == item.expected_status else "  UNEXPECTED"
        render.emit_line(
            f"  {item.request_id}  {item.method} {item.path} -> {outcome} "
            f"(expected {item.expected_status}){marker}",
            style=None if marker == "" else "bold red",
        )
    render.emit_line()
    render.emit_line("Investigate:", style="bold")
    render.emit_line(f"  {result.investigate}")
    render.emit_line(f"Incident report: {result.report}", style="dim")


def _exit_unless(condition: bool) -> None:
    if not condition:
        raise typer.Exit(int(ExitCode.PROBLEM))
