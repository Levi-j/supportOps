import json
from pathlib import Path

import pytest
from typer.testing import CliRunner, Result

from supportops_lab import cli
from supportops_lab.cli import LabOptions, app
from supportops_lab.docker import Resources
from supportops_lab.lab import ScenarioLab
from tests.unit.lab.support import PROJECT, FakeDocker, container
from tests.unit.lab.test_lab import CLEAN, DIRTY, FakeActivity, FakeApi, health_report

runner = CliRunner()


def invoke(*args: str) -> Result:
    return runner.invoke(app, ["--project", PROJECT, *args], env={"COLUMNS": "200"})


@pytest.fixture
def docker(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> FakeDocker:
    fake = FakeDocker()

    def make(options: LabOptions) -> ScenarioLab:
        return ScenarioLab(
            options.project,
            tmp_path / ".lab",
            docker=fake,  # type: ignore[arg-type]
            transport=FakeApi().transport,
            baseline=lambda settings: CLEAN,
        )

    monkeypatch.setattr(cli, "make_lab", make)
    return fake


def test_help_lists_the_commands() -> None:
    result = runner.invoke(app, ["--help"], env={"COLUMNS": "200"})

    for command in ("up", "down", "reset", "status", "scenarios", "start"):
        assert command in result.stdout
    assert "never touches the supportops lab" in " ".join(result.stdout.split())


def test_the_persistent_lab_project_is_refused_before_docker(tmp_path: Path) -> None:
    result = runner.invoke(app, ["--project", "supportops", "down"])

    assert result.exit_code == 2
    assert "won't touch it" in " ".join(result.stderr.split())


def test_up_status_and_down(docker: FakeDocker) -> None:
    up = invoke("up")
    status = invoke("status", "--json")
    down = invoke("down")

    report = json.loads(status.stdout)
    assert up.exit_code == 0, up.output
    assert "Baseline: clean" in up.stdout
    assert "Ownership: owned" in up.stdout
    assert report["ownership"] == "owned"
    assert report["api_url"] == "http://127.0.0.1:55001"
    assert "password" not in status.stdout.lower()
    assert down.exit_code == 0
    assert "Removed scenario lab" in down.stdout


def test_a_dirty_baseline_exits_1(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    fake = FakeDocker()
    monkeypatch.setattr(
        cli,
        "make_lab",
        lambda options: ScenarioLab(
            options.project,
            tmp_path / ".lab",
            docker=fake,  # type: ignore[arg-type]
            baseline=lambda settings: DIRTY,
        ),
    )

    result = invoke("up")

    assert result.exit_code == 1
    assert "Baseline: NOT clean" in result.stdout


def test_start_prints_the_customer_report_and_next_command(docker: FakeDocker) -> None:
    result = invoke("start", "INC-001")

    output = " ".join(result.stdout.split())
    assert result.exit_code == 0, result.output
    assert "Our integration suddenly started returning 401 responses" in output
    assert "inc001-cust-01 GET /v1/invoices -> 401 (expected 401)" in output
    assert "investigate inc001-cust-01" in output
    assert "docs/incidents/INC-001-revoked-api-key.md" in output
    assert "lab_only_not_a_real_key" not in result.output


def test_unknown_scenario_exits_2(docker: FakeDocker) -> None:
    result = invoke("start", "INC-404")

    assert result.exit_code == 2
    assert "Unknown scenario" in result.stderr
    assert docker.compose_calls == []


def test_unsafe_down_exits_2_with_a_recovery_hint(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    fake = FakeDocker(Resources(containers=(container("postgres"),)))
    monkeypatch.setattr(
        cli,
        "make_lab",
        lambda options: ScenarioLab(options.project, tmp_path / ".lab", docker=fake),  # type: ignore[arg-type]
    )

    result = invoke("down")

    assert result.exit_code == 2
    assert "Nothing was removed" in result.stderr
    assert "docker rm --force" in result.stderr
    assert fake.compose_calls == []


def test_status_of_unverified_resources_exits_1(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    fake = FakeDocker(Resources(containers=(container("postgres", owner=False),)))
    monkeypatch.setattr(
        cli,
        "make_lab",
        lambda options: ScenarioLab(options.project, tmp_path / ".lab", docker=fake),  # type: ignore[arg-type]
    )

    result = invoke("status")

    assert result.exit_code == 1
    assert "Ownership: unverified" in result.stdout
    assert "wasn't created by supportops-lab" in result.stdout


def test_scenarios_lists_all_five() -> None:
    result = runner.invoke(app, ["scenarios"], env={"COLUMNS": "200"})

    for scenario in ("INC-001", "INC-002", "INC-003", "INC-004", "INC-005"):
        assert scenario in result.stdout


@pytest.fixture
def scenario_docker(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> FakeDocker:
    fake = FakeDocker()

    def make(options: LabOptions) -> ScenarioLab:
        return ScenarioLab(
            options.project,
            tmp_path / ".lab",
            docker=fake,  # type: ignore[arg-type]
            transport=FakeApi().transport,
            baseline=lambda settings: CLEAN,
            health=lambda settings: health_report(),
            activity=FakeActivity(),
        )

    monkeypatch.setattr(cli, "make_lab", make)
    return fake


def test_start_db_misconfigured_prints_the_change_and_safe_recovery(
    scenario_docker: FakeDocker,
) -> None:
    result = invoke("start", "INC-004")
    status = invoke("status")

    output = " ".join(result.stdout.split())
    assert result.exit_code == 0, result.output
    assert "recreated with database host localhost" in output
    assert "Health check after the change: DEGRADED (api_cannot_reach_database)" in output
    assert (
        "inc004-cust-01 GET /v1/invoices -> 503 SERVICE_UNAVAILABLE "
        "(expected 503 SERVICE_UNAVAILABLE)" in output
    )
    assert "supportops.env health" in output
    assert "Recover with 'uv run supportops-lab reset'" in output
    assert "docker compose" not in output
    assert "API database host: localhost" in " ".join(status.stdout.split())


def test_start_blocked_writes_prints_the_captured_lock_wait(scenario_docker: FakeDocker) -> None:
    result = invoke("start", "INC-005")
    status = invoke("status", "--json")

    output = " ".join(result.stdout.split())
    assert result.exit_code == 0, result.output
    assert "-> 503 DATABASE_BUSY, Retry-After 5 (expected 503 DATABASE_BUSY)" in output
    assert "while inc005-cust-01 was waiting" in output
    assert (
        "session 90 (billing-api, billing_app) waiting 1 s for Lock:transactionid, blocked by "
        "session 77 (invoice-backfill, idle in transaction, transaction open 13 s)" in output
    )
    assert "db run pg.long_transactions pg.blocking_sessions" in output
    assert json.loads(status.stdout)["lock_wait"]["holder_pid"] == 77
