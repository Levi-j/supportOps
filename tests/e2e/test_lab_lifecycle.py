import pytest

from supportops.errors import ExitCode
from supportops_lab.docker import UnsafeOperation
from supportops_lab.lab import ScenarioLab
from supportops_lab.ownership import RESERVED_HOST_PORTS
from tests.e2e.support import E2E_PROJECT

pytestmark = pytest.mark.e2e


def test_status_shows_a_verified_isolated_lab(scenario_lab: ScenarioLab) -> None:
    report = scenario_lab.status()

    assert report.ownership == "owned"
    assert report.problems == []
    assert sorted(item.name for item in report.containers) == [
        f"{E2E_PROJECT}-billing-api-1",
        f"{E2E_PROJECT}-postgres-1",
    ]
    assert all(item.health == "healthy" for item in report.containers)
    state = scenario_lab.store.load()
    assert state is not None
    assert state.api_port not in RESERVED_HOST_PORTS
    assert state.database_port not in RESERVED_HOST_PORTS


def test_starting_the_same_scenario_twice_is_deterministic(scenario_lab: ScenarioLab) -> None:
    first = scenario_lab.start("INC-002")
    second = scenario_lab.start("INC-002")

    assert [item.status for item in first.requests] == [400, 201]
    assert [item.status for item in second.requests] == [400, 201]
    assert scenario_lab.reset().clean


def test_up_after_the_misconfiguration_restores_a_clean_baseline(
    scenario_lab: ScenarioLab,
) -> None:
    started = scenario_lab.start("INC-004")
    assert started.reproduced
    misconfigured = scenario_lab.store.load()
    assert misconfigured is not None
    assert misconfigured.api_database_host == "localhost"

    state = scenario_lab.up(build=False)

    assert state.api_database_host == "postgres"
    assert state.api_port not in RESERVED_HOST_PORTS
    baseline = scenario_lab.baseline()
    assert baseline.clean, baseline


def test_a_foreign_lab_id_blocks_every_destructive_step(scenario_lab: ScenarioLab) -> None:
    original = scenario_lab.store.state_file.read_bytes()
    state = scenario_lab.store.load()
    assert state is not None
    state.lab_id = "f" * 32
    scenario_lab.store.save(state)
    try:
        for action in (scenario_lab.down, lambda: scenario_lab.start("INC-001")):
            with pytest.raises(UnsafeOperation) as excinfo:
                action()
            assert excinfo.value.exit_code == ExitCode.USAGE
            assert "different scenario lab" in excinfo.value.message
        assert scenario_lab.status().ownership == "unverified"
    finally:
        scenario_lab.store.state_file.write_bytes(original)

    assert scenario_lab.status().ownership == "owned"
    assert scenario_lab.baseline().clean


def test_missing_state_never_deletes_the_lab(scenario_lab: ScenarioLab) -> None:
    original = scenario_lab.store.state_file.read_bytes()
    scenario_lab.store.state_file.unlink()
    try:
        with pytest.raises(UnsafeOperation) as excinfo:
            scenario_lab.down()
        assert "docker rm --force" in (excinfo.value.hint or "")
    finally:
        scenario_lab.store.state_file.write_bytes(original)

    assert len(scenario_lab.status().containers) == 2
    assert scenario_lab.baseline().clean


def test_the_persistent_lab_project_cannot_be_targeted() -> None:
    with pytest.raises(UnsafeOperation):
        ScenarioLab("supportops")
