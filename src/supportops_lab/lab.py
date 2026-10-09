import os
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Literal

import httpx
from pydantic import AnyHttpUrl, BaseModel, Field, SecretStr

from supportops.db.catalog import CHECKS
from supportops.db.runner import PlannedCheck, run_checks
from supportops.errors import ExitCode, SupportOpsError
from supportops.health import Verdict, run_health_check
from supportops.http_checks import create_client
from supportops.settings import Settings
from supportops.targets import BILLING
from supportops_lab.customer import send_requests
from supportops_lab.docker import (
    ContainerInfo,
    DockerClient,
    DockerError,
    Resources,
    UnsafeOperation,
)
from supportops_lab.ownership import (
    DEFAULT_PROJECT,
    ownership_problems,
    published_port,
    recovery_procedure,
    validate_project,
    verify_ownership,
)
from supportops_lab.paths import STATE_ROOT
from supportops_lab.scenarios import CustomerRequest, Scenario, get_scenario
from supportops_lab.state import LabState, SentRequest, StateStore, new_state

API_SERVICE = "billing-api"
DATABASE_SERVICE = "postgres"
BASELINE_CHECKS = (
    "db.connectivity",
    "billing.invoice_total_mismatch",
    "billing.paid_invoice_without_payment",
    "billing.payment_on_unpaid_invoice",
    "billing.duplicate_payments",
)


class ScenarioError(SupportOpsError):
    exit_code = ExitCode.PROBLEM


class Baseline(BaseModel):
    verdict: str
    diagnosis: str
    checks: dict[str, str]

    @property
    def clean(self) -> bool:
        return self.verdict == Verdict.HEALTHY and all(
            status == "pass" for status in self.checks.values()
        )


class ContainerStatus(BaseModel):
    name: str
    service: str | None
    status: str
    health: str | None


class LabReport(BaseModel):
    project: str
    state_file: str
    lab_id: str | None = None
    status: str
    ownership: Literal["owned", "absent", "unverified"]
    problems: list[str] = Field(default_factory=list)
    containers: list[ContainerStatus] = Field(default_factory=list)
    api_url: str | None = None
    database: str | None = None
    faults: list[str] = Field(default_factory=list)
    scenario: str | None = None
    requests: list[SentRequest] = Field(default_factory=list)
    supportops_env: str | None = None
    shell_overrides: list[str] = Field(default_factory=list)


class StartResult(BaseModel):
    scenario: str
    slug: str
    title: str
    customer_report: str
    simulation: str
    api_url: str
    requests: list[SentRequest]
    supportops_env: str
    investigate: str
    report: str

    @property
    def reproduced(self) -> bool:
        return all(item.status == item.expected_status for item in self.requests)


BaselineCheck = Callable[[Settings], Baseline]


def lab_settings(state: LabState) -> Settings:
    if state.api_port is None or state.database_port is None or state.api_container is None:
        raise ScenarioError(
            "The scenario lab isn't running.", hint="Start it with 'supportops-lab up'."
        )
    return Settings(
        api_url=AnyHttpUrl(f"http://127.0.0.1:{state.api_port}"),
        db_url=SecretStr(
            f"postgresql://supportops_ro:{state.supportops_ro_password}"
            f"@127.0.0.1:{state.database_port}/billing"
        ),
        log_source=f"docker:{state.api_container}",
    )


def verify_baseline(settings: Settings) -> Baseline:
    with create_client(settings) as client:
        health = run_health_check(settings, BILLING, client)
    if settings.db_url is None:
        raise ScenarioError("The scenario lab has no database URL.")
    report = run_checks(
        settings.db_url,
        [PlannedCheck(CHECKS[name]) for name in BASELINE_CHECKS],
        connect_timeout_seconds=settings.connect_timeout_seconds,
    )
    return Baseline(
        verdict=health.verdict,
        diagnosis=health.diagnosis,
        checks={result.name: result.status for result in report.results},
    )


class ScenarioLab:
    def __init__(
        self,
        project: str = DEFAULT_PROJECT,
        state_root: Path = STATE_ROOT,
        *,
        docker: DockerClient | None = None,
        transport: httpx.BaseTransport | None = None,
        baseline: BaselineCheck | None = None,
    ) -> None:
        self.project = validate_project(project)
        self.store = StateStore(self.project, state_root)
        self.docker = docker or DockerClient()
        self.transport = transport
        self.baseline_check = baseline or verify_baseline

    def up(self, *, build: bool = True, faults: Sequence[str] = ()) -> LabState:
        state = self.store.load()
        resources = self.docker.resources(self.project)
        if state is None:
            if not resources.empty:
                raise UnsafeOperation(
                    f"Project {self.project} already has Docker resources, but there is no "
                    "supportops-lab state for it. Nothing was changed.",
                    hint=recovery_procedure(resources, self.project),
                )
            state = new_state(self.project)
        elif not resources.empty:
            verify_ownership(resources, self.project, state.lab_id)
        state.status = "starting"
        state.faults = sorted(faults)
        state.scenario = None
        state.requests = []
        state.api_port = state.database_port = None
        state.api_container = None
        self.store.save(state)
        self.store.remove_supportops_env()
        self.store.write_compose_env(state)
        arguments = ["up", "--detach", "--wait"] + (["--build"] if build else [])
        self.docker.compose(self.project, self.store.compose_env, *arguments)
        resources = self._verified(state)
        api, database = self._services(resources)
        state.api_port = published_port(api, 8000)
        state.database_port = published_port(database, 5432)
        state.api_container = api.name
        state.status = "running"
        self.store.save(state)
        self.store.write_supportops_env(state)
        return state

    def down(self) -> bool:
        state = self.store.load()
        resources = self.docker.resources(self.project)
        if state is None:
            if resources.empty:
                return False
            raise UnsafeOperation(
                f"Project {self.project} has Docker resources, but there is no supportops-lab "
                "state for it. Nothing was removed.",
                hint=recovery_procedure(resources, self.project),
            )
        stopped = not resources.empty
        if stopped:
            verify_ownership(resources, self.project, state.lab_id)
            self.store.write_compose_env(state)
            self.docker.compose(self.project, self.store.compose_env, "down")
            remaining = self.docker.resources(self.project)
            if not remaining.empty:
                names = [
                    *(item.name for item in remaining.containers),
                    *(item.name for item in remaining.networks),
                    *remaining.volumes,
                ]
                raise DockerError(
                    "docker compose down left these scenario lab resources behind: "
                    + ", ".join(names)
                )
        state.status = "stopped"
        state.scenario = None
        state.requests = []
        state.api_port = state.database_port = None
        state.api_container = None
        self.store.save(state)
        self.store.remove_supportops_env()
        return stopped

    def reset(self) -> Baseline:
        self.down()
        self.up(build=False)
        return self.baseline()

    def baseline(self) -> Baseline:
        state = self._running_state()
        self._verified(state)
        return self.baseline_check(lab_settings(state))

    def start(self, scenario_id: str) -> StartResult:
        scenario = get_scenario(scenario_id)
        self.down()
        state = self.up(build=False, faults=scenario.faults)
        baseline = self.baseline_check(lab_settings(state))
        if not baseline.clean:
            raise ScenarioError(
                "The fresh scenario lab isn't healthy and consistent, so the scenario wasn't "
                f"applied (health {baseline.verdict}, checks {baseline.checks}).",
                hint="Run 'supportops-lab status' and 'supportops-lab reset'.",
            )
        if scenario.setup_sql is not None:
            self._run_sql(state, scenario.setup_sql)
        state.scenario = scenario.id
        state.requests = self._send(state, scenario.requests)
        self.store.save(state)
        return self._start_result(scenario, state)

    def status(self) -> LabReport:
        problems: list[str] = []
        state: LabState | None = None
        try:
            state = self.store.load()
        except UnsafeOperation as exc:
            problems.append(exc.message)
        resources = self.docker.resources(self.project)
        if resources.empty:
            ownership: Literal["owned", "absent", "unverified"] = "absent"
        else:
            problems.extend(ownership_problems(resources, state.lab_id if state else None))
            if state is None and not problems:
                problems.append("The project has resources but no supportops-lab state.")
            ownership = "unverified" if problems else "owned"
        return LabReport(
            project=self.project,
            state_file=str(self.store.state_file),
            lab_id=state.lab_id if state else None,
            status=state.status if state else "absent",
            ownership=ownership,
            problems=problems,
            containers=[
                ContainerStatus(
                    name=item.name, service=item.service, status=item.status, health=item.health
                )
                for item in resources.containers
            ],
            api_url=f"http://127.0.0.1:{state.api_port}" if state and state.api_port else None,
            database=f"127.0.0.1:{state.database_port}" if state and state.database_port else None,
            faults=state.faults if state else [],
            scenario=state.scenario if state else None,
            requests=state.requests if state else [],
            supportops_env=str(self.store.supportops_env)
            if self.store.supportops_env.exists()
            else None,
            shell_overrides=sorted(
                name for name in os.environ if name.upper().startswith("SUPPORTOPS_")
            ),
        )

    def settings(self) -> Settings:
        return lab_settings(self._running_state())

    def _running_state(self) -> LabState:
        state = self.store.load()
        if state is None or state.status != "running":
            raise ScenarioError(
                "The scenario lab isn't running.", hint="Start it with 'supportops-lab up'."
            )
        return state

    def _verified(self, state: LabState) -> Resources:
        resources = self.docker.resources(self.project)
        if resources.empty:
            raise ScenarioError(
                f"Project {self.project} has no running containers.",
                hint="Start the scenario lab with 'supportops-lab up'.",
            )
        verify_ownership(resources, self.project, state.lab_id)
        api, database = self._services(resources)
        if api.status != "running" or database.status != "running":
            raise ScenarioError(
                "The scenario lab's containers aren't running.",
                hint="Run 'supportops-lab reset'.",
            )
        return resources

    def _services(self, resources: Resources) -> tuple[ContainerInfo, ContainerInfo]:
        api = resources.service(API_SERVICE)
        database = resources.service(DATABASE_SERVICE)
        if api is None or database is None:
            raise ScenarioError(
                f"Project {self.project} is missing its {API_SERVICE} or {DATABASE_SERVICE} "
                "container.",
                hint="Run 'supportops-lab reset'.",
            )
        return api, database

    def _run_sql(self, state: LabState, sql: str) -> None:
        resources = self._verified(state)
        _, database = self._services(resources)
        if published_port(database, 5432) != state.database_port:
            raise UnsafeOperation("The scenario database changed since it was verified.")
        self.docker.exec_sql(database.id, sql)

    def _send(self, state: LabState, requests: Sequence[CustomerRequest]) -> list[SentRequest]:
        resources = self._verified(state)
        api, _ = self._services(resources)
        port = published_port(api, 8000)
        if port != state.api_port:
            raise UnsafeOperation(
                "The scenario API's port changed since it was verified, so no customer "
                "request was sent.",
                hint="Run 'supportops-lab reset'.",
            )
        return send_requests(f"http://127.0.0.1:{port}", requests, self.transport)

    def _start_result(self, scenario: Scenario, state: LabState) -> StartResult:
        env_file = display_path(self.store.supportops_env)
        return StartResult(
            scenario=scenario.id,
            slug=scenario.slug,
            title=scenario.title,
            customer_report=scenario.customer_report,
            simulation=scenario.simulation,
            api_url=f"http://127.0.0.1:{state.api_port}",
            requests=state.requests,
            supportops_env=env_file,
            investigate=f"uv run supportops --env-file {env_file} investigate "
            f"{scenario.expectation.request_id}",
            report=scenario.report,
        )


def display_path(path: Path) -> str:
    try:
        return str(path.relative_to(Path.cwd()))
    except ValueError:
        return str(path)
