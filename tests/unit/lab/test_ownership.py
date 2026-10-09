import pytest

from supportops.errors import ExitCode
from supportops_lab.docker import NetworkInfo, PortBinding, Resources, UnsafeOperation
from supportops_lab.ownership import (
    ownership_problems,
    published_port,
    recovery_procedure,
    validate_project,
    verify_ownership,
)
from tests.unit.lab.support import LAB_ID, PROJECT, container, labels, owned_resources


@pytest.mark.parametrize(
    "name", ["supportops-scenario", "supportops-scenario-e2e", "supportops-scenario-2"]
)
def test_scenario_projects_are_accepted(name: str) -> None:
    assert validate_project(name) == name


@pytest.mark.parametrize(
    "name",
    [
        "supportops",
        "supportops-lab",
        "orderflow-backend",
        "supportops-scenario-E2E",
        "supportops-scenario-",
        "supportops-scenario-a-b",
        "supportops-scenario_x",
        "x-supportops-scenario",
        "",
    ],
)
def test_other_projects_are_refused(name: str) -> None:
    with pytest.raises(UnsafeOperation) as excinfo:
        validate_project(name)

    assert excinfo.value.exit_code == ExitCode.USAGE
    assert "won't touch it" in excinfo.value.message


def test_owned_resources_pass() -> None:
    verify_ownership(owned_resources(), PROJECT, LAB_ID)
    assert ownership_problems(owned_resources(), LAB_ID) == []


@pytest.mark.parametrize(
    ("resources", "fragment"),
    [
        (
            Resources(containers=(container("postgres", owner=False),)),
            "wasn't created by supportops-lab",
        ),
        (
            Resources(containers=(container("postgres", lab_id="another"),)),
            "belongs to a different scenario lab (lab ID another",
        ),
        (
            Resources(containers=(container("postgres", lab_id=None),)),
            "lab ID missing",
        ),
        (
            Resources(containers=(container("redis"),)),
            "unexpected service (redis)",
        ),
        (
            Resources(networks=(NetworkInfo("n", "n", labels(owner=False)),)),
            "Network n wasn't created",
        ),
        (Resources(volumes=("supportops-scenario-test_data",)), "never creates volumes"),
    ],
)
def test_unverified_resources_are_refused(resources: Resources, fragment: str) -> None:
    with pytest.raises(UnsafeOperation) as excinfo:
        verify_ownership(resources, PROJECT, LAB_ID)

    assert fragment in excinfo.value.message
    assert "Nothing was changed" in (excinfo.value.hint or "")


def test_one_foreign_resource_blocks_everything() -> None:
    mixed = Resources(
        containers=(*owned_resources().containers, container("postgres", owner=False))
    )

    with pytest.raises(UnsafeOperation):
        verify_ownership(mixed, PROJECT, LAB_ID)


def test_recovery_lists_only_labelled_resources() -> None:
    resources = Resources(
        containers=(container("postgres"), container("billing-api", owner=False)),
        networks=(NetworkInfo("n", "scenario_default", labels()),),
    )

    text = recovery_procedure(resources, PROJECT)

    assert f"docker rm --force {PROJECT}-postgres-1" in text
    assert f"{PROJECT}-billing-api-1" not in text.split("docker rm --force")[1].splitlines()[0]
    assert "docker network rm scenario_default" in text
    assert "must not be removed with this procedure" in text
    assert "won't remove them" in text


def test_published_ports_are_checked() -> None:
    assert published_port(container("billing-api", port="55001"), 8000) == 55001


@pytest.mark.parametrize(
    ("port", "host_ip"),
    [
        ("8001", "127.0.0.1"),
        ("5433", "127.0.0.1"),
        ("80", "127.0.0.1"),
        ("", "127.0.0.1"),
        ("55001", "0.0.0.0"),  # noqa: S104
    ],
)
def test_reserved_foreign_or_missing_ports_are_refused(port: str, host_ip: str) -> None:
    with pytest.raises(UnsafeOperation):
        published_port(container("billing-api", port=port, host_ip=host_ip), 8000)


def test_two_bindings_are_ambiguous() -> None:
    item = container("billing-api")
    doubled = type(item)(
        id=item.id,
        name=item.name,
        labels=item.labels,
        status=item.status,
        ports={"8000/tcp": (PortBinding("127.0.0.1", "55001"), PortBinding("127.0.0.1", "55003"))},
    )

    with pytest.raises(UnsafeOperation, match="exactly one"):
        published_port(doubled, 8000)
