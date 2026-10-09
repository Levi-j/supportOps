from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from typing import Any

import httpx
import pytest

from supportops.errors import ExitCode
from supportops.health import HealthReport, Verdict
from supportops.http_checks import HttpResult
from supportops.investigation.live import BLOCKING_CHECK, LONG_TRANSACTIONS_CHECK
from supportops.settings import Settings
from supportops_lab.contention import ActivityQuery, ContentionError, LockWaitNotObserved
from supportops_lab.docker import (
    ContainerInfo,
    DockerError,
    NetworkInfo,
    PortBinding,
    Resources,
    UnsafeOperation,
)
from supportops_lab.lab import BASELINE_CHECKS, Baseline, ScenarioError, ScenarioLab
from supportops_lab.scenarios import (
    JUNIPER_KEY,
    KESTREL_KEY,
    KESTREL_REVOKED_KEY,
    POWERSHELL_STRIPPED_BODY,
)
from supportops_lab.state import StateStore, new_state
from tests.unit.lab.support import (
    PROJECT,
    FakeDocker,
    container,
    holder_row,
    lab_resources,
    labels,
    wait_row,
)

CLEAN = Baseline(
    verdict="HEALTHY",
    diagnosis="api_ready",
    checks={"db.connectivity": "pass", "billing.duplicate_payments": "pass"},
)
DIRTY = Baseline(verdict="DEGRADED", diagnosis="x", checks={"db.connectivity": "pass"})
STATUSES = {
    "inc001-cust-01": 401,
    "inc001-cust-02": 401,
    "inc002-cust-01": 400,
    "inc002-cust-02": 201,
    "inc003-cust-01": 500,
    "inc003-cust-02": 500,
    "inc004-cust-01": 503,
    "inc004-cust-02": 503,
    "inc005-cust-01": 503,
    "inc005-cust-02": 200,
    "inc005-cust-03": 503,
}
CODES = {
    "inc004-cust-01": "SERVICE_UNAVAILABLE",
    "inc004-cust-02": "SERVICE_UNAVAILABLE",
    "inc005-cust-01": "DATABASE_BUSY",
    "inc005-cust-03": "DATABASE_BUSY",
}


class FakeApi:
    def __init__(self, environment: str = "lab", statuses: dict[str, int] | None = None) -> None:
        self.environment = environment
        self.statuses = statuses or STATUSES
        self.requests: list[httpx.Request] = []

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if request.url.path == "/health":
            return httpx.Response(200, json={"status": "ok", "environment": self.environment})
        request_id = request.headers["X-Request-Id"]
        headers = {"X-Request-Id": request_id}
        code = CODES.get(request_id)
        if code is None:
            return httpx.Response(self.statuses[request_id], headers=headers)
        if code == "DATABASE_BUSY":
            headers["Retry-After"] = "5"
        return httpx.Response(
            self.statuses[request_id],
            headers=headers,
            json={"code": code, "request_id": request_id},
        )

    @property
    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handle)


def health_report(diagnosis: str = "api_cannot_reach_database") -> HealthReport:
    return HealthReport(
        target="billing",
        api_url="http://127.0.0.1:55003",
        verdict=Verdict.HEALTHY if diagnosis == "api_ready" else Verdict.DEGRADED,
        diagnosis=diagnosis,
        summary="The API is running but can't use its database.",
        liveness=HttpResult(
            method="GET",
            url="http://127.0.0.1:55003/health",
            request_id="supportops-test",
            duration_ms=1.0,
            status=200,
        ),
    )


class FakeActivity:
    def __init__(
        self,
        holders: list[dict[str, Any]] | None = None,
        waits: list[dict[str, Any]] | None = None,
    ) -> None:
        self.holders = [holder_row()] if holders is None else holders
        self.waits = [wait_row()] if waits is None else waits
        self.settings: list[Settings] = []
        self.names: list[str] = []

    def __call__(self, settings: Settings) -> ActivityQuery:
        self.settings.append(settings)

        def query(name: str) -> list[dict[str, Any]]:
            self.names.append(name)
            return self.holders if name == LONG_TRANSACTIONS_CHECK else self.waits

        return query


def make_lab(
    tmp_path: Path,
    docker: FakeDocker,
    api: FakeApi | None = None,
    baseline: Baseline = CLEAN,
    *,
    health: HealthReport | None = None,
    activity: FakeActivity | None = None,
    on_baseline: Callable[[], None] | None = None,
) -> tuple[ScenarioLab, list[Settings]]:
    seen: list[Settings] = []

    def check(settings: Settings) -> Baseline:
        seen.append(settings)
        if on_baseline is not None:
            on_baseline()
        return baseline

    def check_health(settings: Settings) -> HealthReport:
        seen.append(settings)
        return health or health_report()

    lab = ScenarioLab(
        PROJECT,
        tmp_path / ".lab",
        docker=docker,  # type: ignore[arg-type]
        transport=(api or FakeApi()).transport,
        baseline=check,
        health=check_health,
        activity=activity or FakeActivity(),
    )
    return lab, seen


def test_up_creates_state_ports_and_the_supportops_target(tmp_path: Path) -> None:
    docker = FakeDocker()
    lab, _ = make_lab(tmp_path, docker)

    state = lab.up()

    assert docker.compose_calls == [("up", "--detach", "--wait", "--build")]
    assert (state.status, state.api_port, state.database_port) == ("running", 55001, 55002)
    assert state.api_container == f"{PROJECT}-billing-api-1"
    assert docker.env["SCENARIO_LAB_ID"] == state.lab_id
    assert docker.env["SCENARIO_BILLING_FAULTS"] == ""
    text = lab.store.supportops_env.read_text(encoding="utf-8")
    assert "SUPPORTOPS_API_URL=http://127.0.0.1:55001" in text
    assert "@127.0.0.1:55002/billing" in text
    assert "8001" not in text
    assert "5433" not in text


def test_up_again_keeps_the_same_lab(tmp_path: Path) -> None:
    docker = FakeDocker()
    lab, _ = make_lab(tmp_path, docker)

    first = lab.up()
    second = lab.up(build=False)

    assert second.lab_id == first.lab_id
    assert docker.compose_calls[-1] == ("up", "--detach", "--wait")


def test_up_refuses_resources_it_has_no_state_for(tmp_path: Path) -> None:
    docker = FakeDocker(lab_resources(PROJECT, "someone-elses-lab"))
    lab, _ = make_lab(tmp_path, docker)

    with pytest.raises(UnsafeOperation) as excinfo:
        lab.up()

    assert docker.compose_calls == []
    assert "docker rm --force" in (excinfo.value.hint or "")
    assert not lab.store.state_file.exists()


def test_up_refuses_a_different_lab_id(tmp_path: Path) -> None:
    lab, _ = make_lab(tmp_path, FakeDocker())
    lab.store.save(new_state(PROJECT))
    lab.docker = FakeDocker(lab_resources(PROJECT, "another-lab"))  # type: ignore[assignment]

    with pytest.raises(UnsafeOperation, match="different scenario lab"):
        lab.up()


def test_up_refuses_a_reserved_port_and_writes_no_target(tmp_path: Path) -> None:
    docker = FakeDocker(api_port="8001")
    lab, _ = make_lab(tmp_path, docker)

    with pytest.raises(UnsafeOperation, match="8001"):
        lab.up()

    assert not lab.store.supportops_env.exists()
    assert lab.store.load() is not None


def test_down_uses_plain_compose_down(tmp_path: Path) -> None:
    docker = FakeDocker()
    lab, _ = make_lab(tmp_path, docker)
    lab.up()

    assert lab.down() is True

    assert docker.compose_calls[-1] == ("down",)
    state = lab.store.load()
    assert state is not None
    assert (state.status, state.api_port, state.scenario) == ("stopped", None, None)
    assert not lab.store.supportops_env.exists()


def test_down_without_anything_is_a_no_op(tmp_path: Path) -> None:
    docker = FakeDocker()
    lab, _ = make_lab(tmp_path, docker)

    assert lab.down() is False
    assert docker.compose_calls == []


@pytest.mark.parametrize(
    "resources",
    [
        lab_resources(PROJECT, "abc"),
        Resources(containers=(container("postgres", owner=False),)),
    ],
    ids=["labelled", "foreign"],
)
def test_down_without_state_never_deletes(tmp_path: Path, resources: Resources) -> None:
    docker = FakeDocker(resources)
    lab, _ = make_lab(tmp_path, docker)

    with pytest.raises(UnsafeOperation) as excinfo:
        lab.down()

    assert excinfo.value.exit_code == ExitCode.USAGE
    assert docker.compose_calls == []
    assert "Nothing was removed" in excinfo.value.message


def test_down_with_corrupted_state_never_deletes(tmp_path: Path) -> None:
    docker = FakeDocker()
    lab, _ = make_lab(tmp_path, docker)
    lab.up()
    lab.store.state_file.write_text("{broken", encoding="utf-8")

    with pytest.raises(UnsafeOperation, match="can't be read"):
        lab.down()

    assert docker.compose_calls == [("up", "--detach", "--wait", "--build")]


def test_down_refuses_unexpected_resources(tmp_path: Path) -> None:
    docker = FakeDocker()
    lab, _ = make_lab(tmp_path, docker)
    state = lab.up()
    owned = lab_resources(PROJECT, state.lab_id)
    docker.current = Resources(
        containers=owned.containers,
        networks=(*owned.networks, NetworkInfo("x", "foreign", labels(owner=False))),
    )

    with pytest.raises(UnsafeOperation, match="Network foreign wasn't created"):
        lab.down()

    assert docker.compose_calls[-1][0] == "up"


def test_down_reports_leftovers(tmp_path: Path) -> None:
    docker = FakeDocker(leave_behind=True)
    lab, _ = make_lab(tmp_path, docker)
    lab.up()

    with pytest.raises(DockerError, match="left these scenario lab resources behind"):
        lab.down()


def test_reset_recreates_and_checks_the_baseline(tmp_path: Path) -> None:
    docker = FakeDocker()
    lab, seen = make_lab(tmp_path, docker)
    lab.up()

    baseline = lab.reset()

    assert baseline.clean
    assert [call[0] for call in docker.compose_calls] == ["up", "down", "up"]
    assert str(seen[-1].api_url) == "http://127.0.0.1:55001/"


def test_start_revoked_key_scenario(tmp_path: Path) -> None:
    docker = FakeDocker()
    api = FakeApi()
    lab, _ = make_lab(tmp_path, docker, api)

    result = lab.start("INC-001")

    assert result.reproduced
    assert [call[0] for call in docker.compose_calls] == ["up"]
    assert docker.sql[0][0] == "id-postgres"
    assert "bk_kestrel01" in docker.sql[0][1]
    assert [request.url.path for request in api.requests] == [
        "/health",
        "/v1/invoices",
        "/v1/account",
    ]
    assert api.requests[1].headers["Authorization"] == f"Bearer {KESTREL_REVOKED_KEY}"
    assert api.requests[1].url.host == "127.0.0.1"
    assert result.investigate.endswith("investigate inc001-cust-01")
    state = lab.store.load()
    assert state is not None
    assert state.scenario == "INC-001"
    assert [item.status for item in state.requests] == [401, 401]


def test_start_powershell_scenario_sends_the_exact_bytes(tmp_path: Path) -> None:
    api = FakeApi()
    lab, _ = make_lab(tmp_path, FakeDocker(), api)

    lab.start("powershell-malformed-json")

    assert api.requests[1].content == POWERSHELL_STRIPPED_BODY
    assert api.requests[1].headers["Content-Type"] == "application/json"
    assert api.requests[2].headers["Authorization"] == f"Bearer {JUNIPER_KEY}"


def test_start_payment_scenario_enables_the_fault_before_starting(tmp_path: Path) -> None:
    docker = FakeDocker()
    lab, _ = make_lab(tmp_path, docker)

    result = lab.start("INC-003")

    assert docker.env["SCENARIO_BILLING_FAULTS"] == "payment_partial_commit"
    assert docker.sql == []
    assert result.reproduced


def test_start_always_begins_with_a_fresh_lab(tmp_path: Path) -> None:
    docker = FakeDocker()
    lab, _ = make_lab(tmp_path, docker)
    lab.start("INC-001")

    lab.start("INC-001")

    assert [call[0] for call in docker.compose_calls] == ["up", "down", "up"]


def test_start_refuses_an_api_that_is_not_a_lab(tmp_path: Path) -> None:
    api = FakeApi(environment="production")
    lab, _ = make_lab(tmp_path, FakeDocker(), api)

    with pytest.raises(UnsafeOperation, match="didn't confirm it is a lab service"):
        lab.start("INC-002")

    assert [request.url.path for request in api.requests] == ["/health"]


def test_start_stops_before_any_change_when_the_baseline_is_dirty(tmp_path: Path) -> None:
    docker = FakeDocker()
    api = FakeApi()
    lab, _ = make_lab(tmp_path, docker, api, baseline=DIRTY)

    with pytest.raises(ScenarioError, match="isn't healthy and consistent"):
        lab.start("INC-001")

    assert docker.sql == []
    assert api.requests == []


def test_start_refuses_a_port_that_changed_after_verification(tmp_path: Path) -> None:
    docker = FakeDocker()
    api = FakeApi()
    lab, _ = make_lab(tmp_path, docker, api)
    original = docker.exec_sql

    def swap_ports(container_id: str, sql: str) -> None:
        original(container_id, sql)
        state = lab.store.load()
        assert state is not None
        docker.current = lab_resources(PROJECT, state.lab_id, api_port="55009")

    docker.exec_sql = swap_ports  # type: ignore[method-assign]

    with pytest.raises(UnsafeOperation, match="port changed"):
        lab.start("INC-001")

    assert api.requests == []


def test_an_unexpected_status_is_reported_not_hidden(tmp_path: Path) -> None:
    api = FakeApi(statuses={**STATUSES, "inc001-cust-01": 200})
    lab, _ = make_lab(tmp_path, FakeDocker(), api)

    result = lab.start("INC-001")

    assert not result.reproduced


def test_unknown_scenarios_change_nothing(tmp_path: Path) -> None:
    docker = FakeDocker()
    lab, _ = make_lab(tmp_path, docker)

    with pytest.raises(Exception, match="Unknown scenario"):
        lab.start("INC-009")

    assert docker.compose_calls == []


def test_status_reports_without_secrets(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SUPPORTOPS_DB_URL", "postgresql://x")
    lab, _ = make_lab(tmp_path, FakeDocker())
    state = lab.up()

    report = lab.status()

    dumped = report.model_dump_json()
    assert report.ownership == "owned"
    assert report.lab_id == state.lab_id
    assert report.api_url == "http://127.0.0.1:55001"
    assert report.shell_overrides == ["SUPPORTOPS_DB_URL"]
    assert all(password not in dumped for password in state.passwords)


@pytest.mark.parametrize(
    ("resources", "ownership"),
    [
        (Resources(), "absent"),
        (Resources(containers=(container("postgres", owner=False),)), "unverified"),
        (lab_resources(PROJECT, "abc"), "unverified"),
    ],
)
def test_status_without_state(tmp_path: Path, resources: Resources, ownership: str) -> None:
    lab, _ = make_lab(tmp_path, FakeDocker(resources))

    report = lab.status()

    assert report.ownership == ownership
    assert report.status == "absent"


def test_baseline_needs_a_running_lab(tmp_path: Path) -> None:
    lab, _ = make_lab(tmp_path, FakeDocker())

    with pytest.raises(ScenarioError, match="isn't running"):
        lab.baseline()


def test_the_state_directory_is_per_project(tmp_path: Path) -> None:
    store: Any = StateStore(PROJECT, tmp_path)

    assert store.directory.name == PROJECT


def test_the_baseline_also_proves_no_session_holds_or_waits_for_locks() -> None:
    assert BASELINE_CHECKS[-2:] == (LONG_TRANSACTIONS_CHECK, BLOCKING_CHECK)


def test_start_db_misconfigured_recreates_only_the_api(tmp_path: Path) -> None:
    docker = FakeDocker()
    api = FakeApi()
    lab, seen = make_lab(tmp_path, docker, api)

    result = lab.start("INC-004")

    assert docker.compose_calls == [
        ("up", "--detach", "--wait"),
        ("up", "--detach", "--wait", "--no-deps", "billing-api"),
    ]
    assert docker.env["SCENARIO_API_DATABASE_HOST"] == "localhost"
    assert docker.sql == []
    assert docker.holders == []
    state = lab.store.load()
    assert state is not None
    assert (state.status, state.api_port, state.database_port) == ("running", 55003, 55002)
    assert state.api_database_host == "localhost"
    target = lab.store.supportops_env.read_text(encoding="utf-8")
    assert "SUPPORTOPS_API_URL=http://127.0.0.1:55003" in target
    assert "55001" not in target
    assert str(seen[-1].api_url) == "http://127.0.0.1:55003/"
    assert {request.url.port for request in api.requests} == {55003}
    assert result.reproduced
    assert [(item.status, item.problem_code) for item in result.requests] == [
        (503, "SERVICE_UNAVAILABLE"),
        (503, "SERVICE_UNAVAILABLE"),
    ]
    assert result.health is not None
    assert result.health.startswith("DEGRADED (api_cannot_reach_database)")
    assert any(command.endswith(" health") for command in result.follow_up)


def _change(resources: Resources, service: str, **changes: Any) -> Resources:
    return Resources(
        containers=tuple(
            replace(item, **changes) if item.service == service else item
            for item in resources.containers
        ),
        networks=resources.networks,
    )


def _second_api(resources: Resources) -> Resources:
    api = resources.service("billing-api")
    assert api is not None
    extra: ContainerInfo = replace(api, id="id-extra", name="extra-billing-api")
    return Resources(containers=(*resources.containers, extra), networks=resources.networks)


@pytest.mark.parametrize(
    ("change", "message"),
    [
        (lambda r: _change(r, "postgres", id="id-postgres-new"), "database container changed"),
        (
            lambda r: _change(
                r, "postgres", ports={"5432/tcp": (PortBinding("127.0.0.1", "55099"),)}
            ),
            "database container changed",
        ),
        (lambda r: _change(r, "billing-api", id="id-billing-api"), "wasn't recreated"),
        (lambda r: _change(r, "billing-api", status="exited"), "aren't running|wasn't recreated"),
        (
            lambda r: _change(r, "billing-api", labels=labels("billing-api", owner=False)),
            "wasn't created by supportops-lab",
        ),
        (
            lambda r: _change(r, "billing-api", labels=labels("billing-api", lab_id="other")),
            "different scenario lab",
        ),
        (_second_api, "2 billing-api containers"),
        (
            lambda r: _change(
                r, "billing-api", ports={"8000/tcp": (PortBinding("127.0.0.1", "8001"),)}
            ),
            "8001",
        ),
        (
            lambda r: _change(
                r, "billing-api", ports={"8000/tcp": (PortBinding("192.0.2.10", "55003"),)}
            ),
            "exactly one 127.0.0.1",
        ),
    ],
    ids=[
        "database-replaced",
        "database-port-changed",
        "api-not-recreated",
        "api-not-running",
        "api-unlabelled",
        "api-other-lab",
        "second-api",
        "reserved-port",
        "non-loopback",
    ],
)
def test_the_recreated_api_is_adopted_only_after_full_verification(
    tmp_path: Path, change: Callable[[Resources], Resources], message: str
) -> None:
    docker = FakeDocker()
    docker.after_recreate = change
    api = FakeApi()
    lab, seen = make_lab(tmp_path, docker, api)

    with pytest.raises((UnsafeOperation, ScenarioError), match=message):
        lab.start("INC-004")

    assert api.requests == []
    assert len(seen) == 1
    state = lab.store.load()
    assert state is not None
    assert (state.status, state.api_port, state.api_container) == ("starting", None, None)
    assert state.api_database_host == "localhost"
    assert not lab.store.supportops_env.exists()


def test_the_api_is_not_recreated_when_the_lab_changed_after_the_baseline(
    tmp_path: Path,
) -> None:
    docker = FakeDocker()

    def move_the_api() -> None:
        state = lab.store.load()
        assert state is not None
        docker.current = lab_resources(PROJECT, state.lab_id, api_port="55009")

    lab, _ = make_lab(tmp_path, docker, on_baseline=move_the_api)

    with pytest.raises(UnsafeOperation, match="ports changed"):
        lab.start("INC-004")

    assert docker.compose_calls == [("up", "--detach", "--wait")]


def test_no_customer_request_is_sent_unless_readiness_fails_as_expected(tmp_path: Path) -> None:
    api = FakeApi()
    lab, _ = make_lab(tmp_path, FakeDocker(), api, health=health_report("api_ready"))

    with pytest.raises(ScenarioError, match="instead of api_cannot_reach_database"):
        lab.start("INC-004")

    assert api.requests == []


def test_up_after_the_misconfiguration_restores_the_database_host(tmp_path: Path) -> None:
    docker = FakeDocker()
    lab, _ = make_lab(tmp_path, docker)
    lab.start("INC-004")

    state = lab.up(build=False)

    assert state.api_database_host == "postgres"
    assert docker.env["SCENARIO_API_DATABASE_HOST"] == "postgres"
    assert state.api_port == 55001


def test_start_blocked_writes_holds_the_lock_and_captures_the_wait(tmp_path: Path) -> None:
    docker = FakeDocker()
    api = FakeApi()
    activity = FakeActivity()
    lab, _ = make_lab(tmp_path, docker, api, activity=activity)

    result = lab.start("INC-005")

    assert docker.compose_calls == [("up", "--detach", "--wait")]
    [holder] = docker.holders
    assert holder["container_id"] == "id-postgres"
    assert (holder["role"], holder["application_name"]) == ("billing_app", "invoice-backfill")
    assert "FOR UPDATE" in holder["sql"]
    assert activity.settings[0].db_url is not None
    assert "@127.0.0.1:55002/billing" in activity.settings[0].db_url.get_secret_value()
    assert activity.names[0] == LONG_TRANSACTIONS_CHECK
    assert BLOCKING_CHECK in activity.names
    assert [request.url.path for request in api.requests] == [
        "/health",
        "/v1/invoices/inv_kestrel_2002/pay",
        "/v1/invoices/inv_kestrel_2002",
        "/v1/invoices/inv_kestrel_2002/pay",
    ]
    assert api.requests[1].headers["Authorization"] == f"Bearer {KESTREL_KEY}"
    assert result.reproduced
    assert [(item.status, item.problem_code, item.retry_after) for item in result.requests] == [
        (503, "DATABASE_BUSY", "5"),
        (200, None, None),
        (503, "DATABASE_BUSY", "5"),
    ]
    assert result.lock_wait is not None
    assert (result.lock_wait.request_id, result.lock_wait.holder_pid) == ("inc005-cust-01", 77)
    assert result.lock_wait.rows == [wait_row()]
    state = lab.store.load()
    assert state is not None
    assert state.lock_wait == result.lock_wait
    assert lab.status().lock_wait == result.lock_wait


def test_the_lock_holder_needs_verified_ownership(tmp_path: Path) -> None:
    docker = FakeDocker()

    def take_over() -> None:
        docker.current = lab_resources(PROJECT, "someone-elses-lab")

    lab, _ = make_lab(tmp_path, docker, on_baseline=take_over)

    with pytest.raises(UnsafeOperation, match="different scenario lab"):
        lab.start("INC-005")

    assert docker.holders == []


def test_an_ambiguous_holder_sends_no_customer_request(tmp_path: Path) -> None:
    api = FakeApi()
    activity = FakeActivity(holders=[holder_row(), holder_row(pid=78)])
    lab, _ = make_lab(tmp_path, FakeDocker(), api, activity=activity)

    with pytest.raises(ContentionError, match="2 sessions"):
        lab.start("INC-005")

    assert api.requests == []


def test_an_unobserved_lock_wait_fails_and_records_nothing(tmp_path: Path) -> None:
    lab, _ = make_lab(tmp_path, FakeDocker(), activity=FakeActivity(waits=[]))

    with pytest.raises(LockWaitNotObserved):
        lab.start("INC-005")

    state = lab.store.load()
    assert state is not None
    assert (state.scenario, state.requests, state.lock_wait) == ("INC-005", [], None)


def test_status_shows_the_scenario_changes(tmp_path: Path) -> None:
    lab, _ = make_lab(tmp_path, FakeDocker())
    lab.start("INC-004")

    report = lab.status()

    assert report.api_database_host == "localhost"
    assert report.api_url == "http://127.0.0.1:55003"
    assert report.lock_wait is None
