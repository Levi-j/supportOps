import os
import secrets
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from supportops_lab.docker import UnsafeOperation

LabStatus = Literal["starting", "running", "stopped"]


class SentRequest(BaseModel):
    request_id: str
    method: str
    path: str
    expected_status: int
    status: int | None = None
    echoed_request_id: str | None = None
    error: str | None = None


class LabState(BaseModel):
    model_config = ConfigDict(extra="forbid")

    version: Literal[1] = 1
    project: str
    lab_id: str
    created_at: datetime
    status: LabStatus = "starting"
    lab_admin_password: str = Field(repr=False)
    billing_app_password: str = Field(repr=False)
    supportops_ro_password: str = Field(repr=False)
    faults: list[str] = Field(default_factory=list)
    api_port: int | None = None
    database_port: int | None = None
    api_container: str | None = None
    scenario: str | None = None
    requests: list[SentRequest] = Field(default_factory=list)

    @property
    def passwords(self) -> tuple[str, str, str]:
        return self.lab_admin_password, self.billing_app_password, self.supportops_ro_password


def new_state(project: str) -> LabState:
    return LabState(
        project=project,
        lab_id=uuid.uuid4().hex,
        created_at=datetime.now(UTC),
        lab_admin_password=secrets.token_urlsafe(24),
        billing_app_password=secrets.token_urlsafe(24),
        supportops_ro_password=secrets.token_urlsafe(24),
    )


class StateStore:
    def __init__(self, project: str, root: Path) -> None:
        self.project = project
        self.directory = root / project
        self.state_file = self.directory / "state.json"
        self.compose_env = self.directory / "scenario.env"
        self.supportops_env = self.directory / "supportops.env"

    def load(self) -> LabState | None:
        if not self.state_file.exists():
            return None
        try:
            state = LabState.model_validate_json(self.state_file.read_bytes())
        except (OSError, ValidationError, ValueError):
            raise UnsafeOperation(
                f"The scenario lab state in {self.state_file} can't be read, so supportops-lab "
                "can't prove which Docker resources it owns. Nothing was changed.",
                hint="Run 'supportops-lab status' to see the project's resources. Once you have "
                f"removed any you no longer need, delete {self.directory} yourself and run "
                "'supportops-lab up'.",
            ) from None
        if state.project != self.project:
            raise UnsafeOperation(
                f"{self.state_file} belongs to project {state.project}, not {self.project}. "
                "Nothing was changed."
            )
        return state

    def save(self, state: LabState) -> None:
        self._write(self.state_file, state.model_dump_json(indent=2) + "\n")

    def write_compose_env(self, state: LabState) -> None:
        self._write(
            self.compose_env,
            "".join(
                f"{name}={value}\n"
                for name, value in (
                    ("SCENARIO_LAB_ID", state.lab_id),
                    ("SCENARIO_LAB_ADMIN_DB_PASSWORD", state.lab_admin_password),
                    ("SCENARIO_BILLING_APP_DB_PASSWORD", state.billing_app_password),
                    ("SCENARIO_SUPPORTOPS_RO_DB_PASSWORD", state.supportops_ro_password),
                    ("SCENARIO_BILLING_FAULTS", ",".join(state.faults)),
                )
            ),
        )

    def write_supportops_env(self, state: LabState) -> None:
        if state.api_port is None or state.database_port is None or not state.api_container:
            raise UnsafeOperation("The scenario lab has no verified ports yet.")
        self._write(
            self.supportops_env,
            "".join(
                f"{name}={value}\n"
                for name, value in (
                    ("SUPPORTOPS_TARGET", "billing"),
                    ("SUPPORTOPS_API_URL", f"http://127.0.0.1:{state.api_port}"),
                    (
                        "SUPPORTOPS_DB_URL",
                        f"postgresql://supportops_ro:{state.supportops_ro_password}"
                        f"@127.0.0.1:{state.database_port}/billing",
                    ),
                    ("SUPPORTOPS_LOG_SOURCE", f"docker:{state.api_container}"),
                )
            ),
        )

    def remove_supportops_env(self) -> None:
        self.supportops_env.unlink(missing_ok=True)

    def _write(self, path: Path, text: str) -> None:
        self.directory.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(f".{path.name}.tmp")
        with temporary.open("w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
        os.replace(temporary, path)
