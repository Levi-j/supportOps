import json
import os
import shutil
import subprocess
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from supportops.errors import ConfigError, SupportOpsError
from supportops_lab.paths import REPO_ROOT, compose_file

PROJECT_LABEL = "com.docker.compose.project"
SERVICE_LABEL = "com.docker.compose.service"
OWNER_LABEL = "io.supportops.scenario-lab"
LAB_ID_LABEL = "io.supportops.lab-id"
COMMAND_TIMEOUT_SECONDS = 60.0
COMPOSE_TIMEOUT_SECONDS = 900.0
MAX_ERROR_CHARACTERS = 300
_SANITIZED_PREFIXES = ("COMPOSE_", "SCENARIO_")

Runner = Callable[[Sequence[str], float, bytes | None], subprocess.CompletedProcess[bytes]]


class UnsafeOperation(ConfigError):
    pass


class DockerError(SupportOpsError):
    pass


@dataclass(frozen=True)
class PortBinding:
    host_ip: str
    host_port: str


@dataclass(frozen=True)
class ContainerInfo:
    id: str
    name: str
    labels: Mapping[str, str]
    status: str
    health: str | None = None
    ports: Mapping[str, tuple[PortBinding, ...]] = field(default_factory=dict)

    @property
    def service(self) -> str | None:
        return self.labels.get(SERVICE_LABEL)


@dataclass(frozen=True)
class NetworkInfo:
    id: str
    name: str
    labels: Mapping[str, str]


@dataclass(frozen=True)
class Resources:
    containers: tuple[ContainerInfo, ...] = ()
    networks: tuple[NetworkInfo, ...] = ()
    volumes: tuple[str, ...] = ()

    @property
    def empty(self) -> bool:
        return not (self.containers or self.networks or self.volumes)

    def service(self, name: str) -> ContainerInfo | None:
        return next((item for item in self.containers if item.service == name), None)


def sanitized_env(environ: Mapping[str, str] | None = None) -> dict[str, str]:
    source = os.environ if environ is None else environ
    return {
        name: value
        for name, value in source.items()
        if not name.upper().startswith(_SANITIZED_PREFIXES)
    }


def run_subprocess(
    command: Sequence[str], timeout: float, input: bytes | None = None
) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(  # noqa: S603
        list(command),
        capture_output=True,
        timeout=timeout,
        input=input,
        env=sanitized_env(),
        check=False,
    )


def compose_command(
    docker: str, project: str, env_file: Path, arguments: Sequence[str]
) -> list[str]:
    return [
        docker,
        "compose",
        "--project-name",
        project,
        "--project-directory",
        str(REPO_ROOT),
        "--file",
        str(compose_file()),
        "--env-file",
        str(env_file),
        *arguments,
    ]


class DockerClient:
    def __init__(self, runner: Runner | None = None) -> None:
        self.runner = runner

    def resources(self, project: str) -> Resources:
        selector = f"label={PROJECT_LABEL}={project}"
        container_ids = self._ids(["ps", "--all", "--no-trunc", "--filter", selector])
        network_ids = self._ids(["network", "ls", "--no-trunc", "--filter", selector])
        volumes = self._lines(["volume", "ls", "--filter", selector, "--format", "{{.Name}}"])
        containers = (
            tuple(
                _container(item)
                for item in self._inspect(["inspect", "--type", "container"], container_ids)
            )
            if container_ids
            else ()
        )
        networks = (
            tuple(_network(item) for item in self._inspect(["network", "inspect"], network_ids))
            if network_ids
            else ()
        )
        return Resources(containers=containers, networks=networks, volumes=tuple(volumes))

    def compose(
        self,
        project: str,
        env_file: Path,
        *arguments: str,
        timeout: float = COMPOSE_TIMEOUT_SECONDS,
    ) -> None:
        command = compose_command(self._docker(), project, env_file, arguments)
        self._run(command, timeout=timeout, what=f"docker compose {arguments[0]}")

    def exec_sql(self, container_id: str, sql: str) -> None:
        command = [
            self._docker(),
            "exec",
            "--interactive",
            container_id,
            "psql",
            "--username",
            "lab_admin",
            "--dbname",
            "billing",
            "--no-psqlrc",
            "--quiet",
            "--single-transaction",
            "--set",
            "ON_ERROR_STOP=1",
            "--file",
            "-",
        ]
        self._run(command, input=sql.encode(), what="the scenario SQL")

    def _ids(self, arguments: list[str]) -> list[str]:
        return self._lines([*arguments, "--format", "{{.ID}}"])

    def _lines(self, arguments: list[str]) -> list[str]:
        result = self._run([self._docker(), *arguments], what=f"docker {arguments[0]}")
        return [line.strip() for line in result.stdout.decode().splitlines() if line.strip()]

    def _inspect(self, arguments: list[str], ids: list[str]) -> list[dict[str, Any]]:
        result = self._run([self._docker(), *arguments, *ids], what=f"docker {arguments[0]}")
        try:
            data = json.loads(result.stdout.decode())
        except ValueError:
            raise DockerError("Docker returned output that isn't valid JSON.") from None
        if not isinstance(data, list):
            raise DockerError("Docker returned an unexpected inspect result.")
        return [item for item in data if isinstance(item, dict)]

    def _run(
        self,
        command: list[str],
        *,
        what: str,
        timeout: float = COMMAND_TIMEOUT_SECONDS,
        input: bytes | None = None,
    ) -> subprocess.CompletedProcess[bytes]:
        runner = self.runner or run_subprocess
        try:
            result = runner(command, timeout, input)
        except subprocess.TimeoutExpired:
            raise DockerError(f"Running {what} took longer than {timeout:.0f} seconds.") from None
        except OSError as exc:
            raise DockerError(f"Couldn't run Docker: {exc.strerror or exc}") from None
        if result.returncode != 0:
            detail = result.stderr.decode(errors="replace").strip().splitlines()
            message = detail[-1][:MAX_ERROR_CHARACTERS] if detail else f"exit {result.returncode}"
            raise DockerError(
                f"Running {what} failed: {message}",
                hint="Check that Docker Desktop is running ('docker ps').",
            )
        return result

    def _docker(self) -> str:
        docker = shutil.which("docker")
        if docker is None:
            raise DockerError(
                "Docker isn't installed or isn't on PATH.",
                hint="Install Docker Desktop and check that 'docker version' works.",
            )
        return docker


def _container(data: dict[str, Any]) -> ContainerInfo:
    config = data.get("Config") or {}
    state = data.get("State") or {}
    health = state.get("Health") or {}
    settings = data.get("NetworkSettings") or {}
    ports: dict[str, tuple[PortBinding, ...]] = {}
    for port, bindings in (settings.get("Ports") or {}).items():
        ports[str(port)] = tuple(
            PortBinding(str(item.get("HostIp", "")), str(item.get("HostPort", "")))
            for item in bindings or []
            if isinstance(item, dict)
        )
    return ContainerInfo(
        id=str(data.get("Id", "")),
        name=str(data.get("Name", "")).lstrip("/"),
        labels={str(key): str(value) for key, value in (config.get("Labels") or {}).items()},
        status=str(state.get("Status", "unknown")),
        health=str(health["Status"]) if "Status" in health else None,
        ports=ports,
    )


def _network(data: dict[str, Any]) -> NetworkInfo:
    return NetworkInfo(
        id=str(data.get("Id", "")),
        name=str(data.get("Name", "")),
        labels={str(key): str(value) for key, value in (data.get("Labels") or {}).items()},
    )
