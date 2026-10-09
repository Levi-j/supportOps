import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from supportops.errors import ExitCode
from supportops.settings import load_settings
from supportops_lab.docker import UnsafeOperation
from supportops_lab.state import LabState, SentRequest, StateStore, new_state
from tests.unit.lab.support import PROJECT


def store(tmp_path: Path) -> StateStore:
    return StateStore(PROJECT, tmp_path / ".lab")


def test_missing_state_is_none(tmp_path: Path) -> None:
    assert store(tmp_path).load() is None


def test_state_round_trips_atomically(tmp_path: Path) -> None:
    states = store(tmp_path)
    state = new_state(PROJECT)
    state.faults = ["payment_partial_commit"]

    states.save(state)

    assert states.load() == state
    assert states.directory == tmp_path / ".lab" / PROJECT
    assert not list(states.directory.glob(".*.tmp"))


def test_generated_secrets_never_appear_in_repr() -> None:
    state = new_state(PROJECT)

    text = repr(state)

    assert all(password not in text for password in state.passwords)
    assert len(set(state.passwords)) == 3
    assert all(len(password) >= 24 for password in state.passwords)


@pytest.mark.parametrize("content", [b"{not json", b'{"version": 1}', b"\xff\xfe"])
def test_unreadable_state_fails_closed(tmp_path: Path, content: bytes) -> None:
    states = store(tmp_path)
    states.directory.mkdir(parents=True)
    states.state_file.write_bytes(content)

    with pytest.raises(UnsafeOperation) as excinfo:
        states.load()

    assert excinfo.value.exit_code == ExitCode.USAGE
    assert "Nothing was changed" in excinfo.value.message
    assert "delete" in (excinfo.value.hint or "")


def test_state_of_another_project_is_refused(tmp_path: Path) -> None:
    other = StateStore("supportops-scenario-other", tmp_path / ".lab")
    other.save(new_state("supportops-scenario-other"))
    misplaced = StateStore(PROJECT, tmp_path / ".lab")
    misplaced.directory.mkdir(parents=True)
    misplaced.state_file.write_bytes(other.state_file.read_bytes())

    with pytest.raises(UnsafeOperation, match="belongs to project supportops-scenario-other"):
        misplaced.load()


def test_compose_env_contains_only_scenario_settings(tmp_path: Path) -> None:
    states = store(tmp_path)
    state = new_state(PROJECT)
    state.faults = ["payment_partial_commit"]

    states.write_compose_env(state)

    lines = states.compose_env.read_text(encoding="utf-8").splitlines()
    assert [line.split("=", 1)[0] for line in lines] == [
        "SCENARIO_LAB_ID",
        "SCENARIO_LAB_ADMIN_DB_PASSWORD",
        "SCENARIO_BILLING_APP_DB_PASSWORD",
        "SCENARIO_SUPPORTOPS_RO_DB_PASSWORD",
        "SCENARIO_BILLING_FAULTS",
        "SCENARIO_API_DATABASE_HOST",
    ]
    assert lines[-2] == "SCENARIO_BILLING_FAULTS=payment_partial_commit"
    assert lines[-1] == "SCENARIO_API_DATABASE_HOST=postgres"


def test_the_api_database_host_is_restricted() -> None:
    state = new_state(PROJECT)
    data = state.model_dump()

    assert LabState.model_validate({**data, "api_database_host": "localhost"})
    with pytest.raises(ValidationError):
        LabState.model_validate({**data, "api_database_host": "10.0.0.5"})


def test_state_files_from_before_m9_still_load(tmp_path: Path) -> None:
    states = store(tmp_path)
    state = new_state(PROJECT)
    data = json.loads(state.model_dump_json())
    for name in ("api_database_host", "lock_wait"):
        del data[name]
    states.directory.mkdir(parents=True)
    states.state_file.write_text(json.dumps(data), encoding="utf-8")

    loaded = states.load()

    assert loaded is not None
    assert (loaded.api_database_host, loaded.lock_wait) == ("postgres", None)


@pytest.mark.parametrize(
    ("status", "code", "expected"),
    [(503, "DATABASE_BUSY", True), (503, "SERVICE_UNAVAILABLE", False), (500, None, False)],
)
def test_a_request_is_as_expected_only_with_the_expected_code(
    status: int, code: str | None, expected: bool
) -> None:
    sent = SentRequest(
        request_id="inc005-cust-01",
        method="POST",
        path="/v1/invoices/inv_kestrel_2002/pay",
        expected_status=503,
        expected_code="DATABASE_BUSY",
        status=status,
        problem_code=code,
    )

    assert sent.as_expected is expected


def test_supportops_env_targets_the_scenario_lab(tmp_path: Path) -> None:
    states = store(tmp_path)
    state = new_state(PROJECT)
    state.api_port, state.database_port = 55001, 55002
    state.api_container = f"{PROJECT}-billing-api-1"

    states.write_supportops_env(state)

    settings = load_settings(states.supportops_env).settings
    assert str(settings.api_url) == "http://127.0.0.1:55001/"
    assert settings.db_url is not None
    assert settings.db_url.get_secret_value() == (
        f"postgresql://supportops_ro:{state.supportops_ro_password}@127.0.0.1:55002/billing"
    )
    assert settings.log_source == f"docker:{PROJECT}-billing-api-1"
    assert settings.api_key is None


def test_supportops_env_needs_verified_ports(tmp_path: Path) -> None:
    with pytest.raises(UnsafeOperation):
        store(tmp_path).write_supportops_env(new_state(PROJECT))
