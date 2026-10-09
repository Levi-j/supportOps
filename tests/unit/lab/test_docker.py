import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest

from supportops.errors import ExitCode
from supportops_lab import docker as docker_module
from supportops_lab.docker import (
    DockerClient,
    DockerError,
    compose_command,
    run_subprocess,
    sanitized_env,
)
from supportops_lab.paths import COMPOSE_FILE, REPO_ROOT
from tests.unit.lab.support import (
    DOCKER,
    PROJECT,
    ScriptedRunner,
    container,
    done,
    inspect_payload,
    json_reply,
)


@pytest.fixture(autouse=True)
def docker_installed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(shutil, "which", lambda name: DOCKER if name == "docker" else None)


def test_compose_commands_are_always_fully_scoped(tmp_path: Path) -> None:
    env_file = tmp_path / "scenario.env"

    command = compose_command(DOCKER, PROJECT, env_file, ["down"])

    assert command == [
        DOCKER,
        "compose",
        "--project-name",
        PROJECT,
        "--project-directory",
        str(REPO_ROOT),
        "--file",
        str(COMPOSE_FILE),
        "--env-file",
        str(env_file),
        "down",
    ]
    assert COMPOSE_FILE.name == "compose.scenario.yaml"
    assert not any(
        part.endswith(("compose.yaml", ".env")) and part != str(env_file) for part in command[1:]
    )


def test_inherited_compose_and_scenario_settings_are_removed() -> None:
    environ = {
        "PATH": "/usr/bin",
        "DOCKER_HOST": "unix:///var/run/docker.sock",
        "COMPOSE_PROJECT_NAME": "supportops",
        "COMPOSE_FILE": "compose.yaml",
        "compose_profiles": "x",
        "SCENARIO_LAB_ID": "forged",
        "SCENARIO_BILLING_FAULTS": "payment_partial_commit",
    }

    assert sanitized_env(environ) == {
        "PATH": "/usr/bin",
        "DOCKER_HOST": "unix:///var/run/docker.sock",
    }


def test_subprocesses_get_the_sanitized_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, Any] = {}

    def fake_run(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[bytes]:
        seen.update(kwargs, command=command)
        return done()

    monkeypatch.setenv("COMPOSE_PROJECT_NAME", "supportops")
    monkeypatch.setattr(subprocess, "run", fake_run)

    run_subprocess(["docker", "ps"], 5)

    assert seen["command"] == ["docker", "ps"]
    assert "COMPOSE_PROJECT_NAME" not in seen["env"]
    assert seen["check"] is False
    assert "shell" not in seen


def test_resources_are_read_for_the_exact_project_only() -> None:
    api = container("billing-api", port="55001")
    runner = ScriptedRunner(
        {
            "ps --all": done("id-billing-api\n"),
            "network ls": done("net-1\n"),
            "volume ls": done(""),
            "inspect --type": json_reply([inspect_payload(api)]),
            "network inspect": json_reply(
                [{"Id": "net-1", "Name": f"{PROJECT}_default", "Labels": {"a": "b"}}]
            ),
        }
    )

    resources = DockerClient(runner).resources(PROJECT)

    selector = f"label=com.docker.compose.project={PROJECT}"
    assert all(selector in command for command in runner.commands[:3])
    assert resources.containers == (api,)
    assert resources.networks[0].name == f"{PROJECT}_default"
    assert resources.volumes == ()
    assert resources.service("billing-api") == api


def test_no_resources_means_no_inspect_calls() -> None:
    runner = ScriptedRunner()

    resources = DockerClient(runner).resources(PROJECT)

    assert resources.empty
    assert len(runner.commands) == 3


def test_failures_are_docker_errors_without_inspect_output() -> None:
    runner = ScriptedRunner(
        {"ps --all": done(returncode=1, stderr=b"warning\nCannot connect to the Docker daemon")}
    )

    with pytest.raises(DockerError) as excinfo:
        DockerClient(runner).resources(PROJECT)

    assert excinfo.value.exit_code == ExitCode.INCOMPLETE
    assert "Cannot connect to the Docker daemon" in excinfo.value.message


def test_missing_docker(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(shutil, "which", lambda name: None)

    with pytest.raises(DockerError, match="isn't installed"):
        DockerClient(ScriptedRunner()).resources(PROJECT)


def test_timeouts_are_reported(tmp_path: Path) -> None:
    def slow(command: list[str], input: bytes | None) -> Any:
        raise subprocess.TimeoutExpired(command, 1)

    runner = ScriptedRunner({"compose --project-name": slow})

    with pytest.raises(DockerError, match="took longer than"):
        DockerClient(runner).compose(PROJECT, tmp_path / "x.env", "up", timeout=1)


def test_scenario_sql_runs_only_inside_the_given_container() -> None:
    runner = ScriptedRunner()

    DockerClient(runner).exec_sql("id-postgres", "SELECT 1;")

    command = runner.commands[0]
    assert command[:4] == [DOCKER, "exec", "--interactive", "id-postgres"]
    assert "psql" in command
    assert "--single-transaction" in command
    assert not any("5433" in part or "127.0.0.1" in part for part in command)
    assert runner.inputs[0] == b"SELECT 1;"


def test_the_runner_is_module_level_and_replaceable() -> None:
    assert docker_module.run_subprocess is run_subprocess
