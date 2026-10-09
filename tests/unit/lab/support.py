import json
import subprocess
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

from supportops_lab.docker import (
    LAB_ID_LABEL,
    OWNER_LABEL,
    PROJECT_LABEL,
    SERVICE_LABEL,
    ContainerInfo,
    NetworkInfo,
    PortBinding,
    Resources,
)

DOCKER = "C:/Program Files/Docker/docker.exe"
PROJECT = "supportops-scenario-test"
LAB_ID = "0123456789abcdef0123456789abcdef"

Reply = subprocess.CompletedProcess[bytes] | Callable[[list[str], bytes | None], Any]


def done(
    stdout: bytes | str = b"", returncode: int = 0, stderr: bytes = b""
) -> subprocess.CompletedProcess[bytes]:
    data = stdout.encode() if isinstance(stdout, str) else stdout
    return subprocess.CompletedProcess([], returncode, data, stderr)


class ScriptedRunner:
    def __init__(self, replies: dict[str, Any] | None = None) -> None:
        self.replies = replies or {}
        self.commands: list[list[str]] = []
        self.inputs: list[bytes | None] = []

    def __call__(
        self, command: Sequence[str], timeout: float, input: bytes | None = None
    ) -> subprocess.CompletedProcess[bytes]:
        command = list(command)
        self.commands.append(command)
        self.inputs.append(input)
        key = " ".join(command[1:3])
        reply = self.replies.get(key, done())
        if callable(reply):
            reply = reply(command, input)
        assert isinstance(reply, subprocess.CompletedProcess)
        return reply


def labels(
    service: str | None = None,
    *,
    owner: bool = True,
    lab_id: str | None = LAB_ID,
    project: str = PROJECT,
) -> dict[str, str]:
    values = {PROJECT_LABEL: project}
    if service is not None:
        values[SERVICE_LABEL] = service
    if owner:
        values[OWNER_LABEL] = "true"
    if lab_id is not None:
        values[LAB_ID_LABEL] = lab_id
    return values


def container(
    service: str,
    *,
    port: str = "55001",
    host_ip: str = "127.0.0.1",
    status: str = "running",
    health: str | None = "healthy",
    **label_options: Any,
) -> ContainerInfo:
    internal = "8000/tcp" if service == "billing-api" else "5432/tcp"
    return ContainerInfo(
        id=f"id-{service}",
        name=f"{PROJECT}-{service}-1",
        labels=labels(service, **label_options),
        status=status,
        health=health,
        ports={internal: (PortBinding(host_ip, port),)},
    )


def owned_resources(api_port: str = "55001", database_port: str = "55002") -> Resources:
    return Resources(
        containers=(
            container("postgres", port=database_port),
            container("billing-api", port=api_port),
        ),
        networks=(NetworkInfo("net-1", f"{PROJECT}_default", labels()),),
    )


def inspect_payload(item: ContainerInfo) -> dict[str, Any]:
    return {
        "Id": item.id,
        "Name": f"/{item.name}",
        "Config": {"Labels": dict(item.labels), "Env": ["POSTGRES_PASSWORD=never-shown"]},
        "State": {"Status": item.status, "Health": {"Status": item.health}},
        "NetworkSettings": {
            "Ports": {
                port: [{"HostIp": b.host_ip, "HostPort": b.host_port} for b in bindings]
                for port, bindings in item.ports.items()
            }
        },
    }


def json_reply(data: Any) -> Any:
    return done(json.dumps(data))


def read_env(path: Path) -> dict[str, str]:
    pairs = (line.split("=", 1) for line in path.read_text(encoding="utf-8").splitlines() if line)
    return {name: value for name, value in pairs}


def holder_row(**overrides: Any) -> dict[str, Any]:
    return {
        "pid": 77,
        "role": "billing_app",
        "application": "invoice-backfill",
        "state": "idle in transaction",
        "transaction_seconds": 12,
        "locks_held": 3,
        "last_query": "BEGIN; SELECT id, status FROM billing.invoices WHERE id = 'x' FOR UPDATE;",
        **overrides,
    }


def wait_row(**overrides: Any) -> dict[str, Any]:
    return {
        "blocked_pid": 90,
        "blocked_role": "billing_app",
        "blocked_application": "billing-api",
        "waiting_seconds": 1,
        "waiting_for": "Lock:transactionid",
        "blocked_query": "SELECT id, status FROM billing.invoices WHERE x FOR UPDATE",
        "blocking_pid": 77,
        "blocking_application": "invoice-backfill",
        "blocking_state": "idle in transaction",
        "blocking_transaction_seconds": 13,
        **overrides,
    }


def lab_resources(
    project: str, lab_id: str, api_port: str = "55001", database_port: str = "55002"
) -> Resources:
    def make(service: str, internal: str, port: str) -> ContainerInfo:
        return ContainerInfo(
            id=f"id-{service}",
            name=f"{project}-{service}-1",
            labels=labels(service, lab_id=lab_id, project=project),
            status="running",
            health="healthy",
            ports={internal: (PortBinding("127.0.0.1", port),)},
        )

    return Resources(
        containers=(
            make("postgres", "5432/tcp", database_port),
            make("billing-api", "8000/tcp", api_port),
        ),
        networks=(
            NetworkInfo("net-1", f"{project}_default", labels(lab_id=lab_id, project=project)),
        ),
    )


class FakeDocker:
    def __init__(
        self,
        existing: Resources | None = None,
        *,
        api_port: str = "55001",
        database_port: str = "55002",
        recreated_api_port: str = "55003",
        leave_behind: bool = False,
    ) -> None:
        self.current = existing or Resources()
        self.api_port = api_port
        self.database_port = database_port
        self.recreated_api_port = recreated_api_port
        self.leave_behind = leave_behind
        self.after_recreate: Callable[[Resources], Resources] | None = None
        self.calls: list[tuple[str, ...]] = []
        self.sql: list[tuple[str, str]] = []
        self.holders: list[dict[str, str]] = []
        self.env: dict[str, str] = {}

    def resources(self, project: str) -> Resources:
        self.calls.append(("resources", project))
        return self.current

    def compose(self, project: str, env_file: Path, *arguments: str, timeout: float = 0) -> None:
        self.calls.append(("compose", *arguments))
        self.env = read_env(env_file)
        if arguments[0] == "up" and "--no-deps" in arguments:
            self._recreate_api(project)
        elif arguments[0] == "up":
            self.current = lab_resources(
                project, self.env["SCENARIO_LAB_ID"], self.api_port, self.database_port
            )
        elif arguments[0] == "down" and not self.leave_behind:
            self.current = Resources()

    def exec_sql(self, container_id: str, sql: str) -> None:
        self.calls.append(("exec_sql", container_id))
        self.sql.append((container_id, sql))

    def start_lock_holder(
        self,
        container_id: str,
        *,
        sql: str,
        role: str,
        application_name: str,
        database: str = "billing",
    ) -> None:
        self.calls.append(("start_lock_holder", container_id))
        self.holders.append(
            {
                "container_id": container_id,
                "sql": sql,
                "role": role,
                "application_name": application_name,
                "database": database,
            }
        )

    def _recreate_api(self, project: str) -> None:
        database = self.current.service("postgres")
        assert database is not None
        api = ContainerInfo(
            id="id-billing-api-recreated",
            name=f"{project}-billing-api-1",
            labels=labels("billing-api", lab_id=self.env["SCENARIO_LAB_ID"], project=project),
            status="running",
            health="healthy",
            ports={"8000/tcp": (PortBinding("127.0.0.1", self.recreated_api_port),)},
        )
        self.current = Resources(containers=(database, api), networks=self.current.networks)
        if self.after_recreate is not None:
            self.current = self.after_recreate(self.current)

    @property
    def compose_calls(self) -> list[tuple[str, ...]]:
        return [call[1:] for call in self.calls if call[0] == "compose"]
