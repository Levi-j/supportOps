import re
from collections.abc import Mapping

from supportops_lab.docker import (
    LAB_ID_LABEL,
    OWNER_LABEL,
    ContainerInfo,
    Resources,
    UnsafeOperation,
)

DEFAULT_PROJECT = "supportops-scenario"
PROJECT_PATTERN = re.compile(r"supportops-scenario(?:-[a-z0-9]{1,20})?")
EXPECTED_SERVICES = frozenset({"postgres", "billing-api"})
RESERVED_HOST_PORTS = frozenset({8001, 5433})
LOOPBACK = "127.0.0.1"


def validate_project(name: str) -> str:
    if not PROJECT_PATTERN.fullmatch(name):
        raise UnsafeOperation(
            f"'{name}' isn't a scenario lab project, so supportops-lab won't touch it.",
            hint="supportops-lab only manages projects named supportops-scenario or "
            "supportops-scenario-<suffix> (lowercase letters and digits). It never manages "
            "the supportops lab.",
        )
    return name


def ownership_problems(resources: Resources, lab_id: str | None) -> list[str]:
    problems = []
    for container in resources.containers:
        problems.extend(_labelled("Container", container.name, container.labels, lab_id))
        if container.service not in EXPECTED_SERVICES:
            problems.append(
                f"Container {container.name} runs an unexpected service "
                f"({container.service or 'none'})."
            )
    for network in resources.networks:
        problems.extend(_labelled("Network", network.name, network.labels, lab_id))
    problems.extend(
        f"Volume {volume} belongs to the project, but the scenario lab never creates volumes."
        for volume in resources.volumes
    )
    return problems


def verify_ownership(resources: Resources, project: str, lab_id: str) -> None:
    problems = ownership_problems(resources, lab_id)
    if problems:
        raise UnsafeOperation(
            f"supportops-lab won't change project {project}, because some of its resources "
            "aren't verified as this scenario lab's: " + " ".join(problems),
            hint="Nothing was changed. Run 'supportops-lab status' to see the resources, and "
            "remove anything you don't recognise yourself after checking it.",
        )


def recovery_procedure(resources: Resources, project: str) -> str:
    owned = [
        container.name
        for container in resources.containers
        if container.labels.get(OWNER_LABEL) == "true"
    ]
    networks = [
        network.name for network in resources.networks if network.labels.get(OWNER_LABEL) == "true"
    ]
    foreign = len(resources.containers) + len(resources.networks) - len(owned) - len(networks)
    steps = [
        f"Project {project} has Docker resources but no readable supportops-lab state, so "
        "supportops-lab can't prove they belong to this checkout and won't remove them.",
        "Check them with 'docker ps --all' and 'docker network ls'. If they are a scenario lab "
        "you no longer need, remove them yourself:",
    ]
    if owned:
        steps.append("  docker rm --force " + " ".join(owned))
    if networks:
        steps.append("  docker network rm " + " ".join(networks))
    if foreign or resources.volumes:
        steps.append(
            "Some resources don't carry the supportops-lab label; they aren't listed above and "
            "must not be removed with this procedure."
        )
    steps.append("Then run 'supportops-lab up' to create a new scenario lab.")
    return "\n".join(steps)


def published_port(container: ContainerInfo, container_port: int) -> int:
    bindings = container.ports.get(f"{container_port}/tcp", ())
    if len(bindings) != 1 or bindings[0].host_ip != LOOPBACK:
        raise UnsafeOperation(
            f"Container {container.name} doesn't publish port {container_port} on exactly one "
            f"{LOOPBACK} address, so supportops-lab can't target it safely."
        )
    text = bindings[0].host_port
    port = int(text) if text.isdigit() else 0
    if not 1024 <= port <= 65535 or port in RESERVED_HOST_PORTS:
        raise UnsafeOperation(
            f"Container {container.name} was given host port {text or 'none'}, which "
            "supportops-lab doesn't accept for a scenario lab (8001 and 5433 belong to the "
            "supportops lab).",
            hint="Run 'supportops-lab reset' to get new ports.",
        )
    return port


def _labelled(kind: str, name: str, labels: Mapping[str, str], lab_id: str | None) -> list[str]:
    if labels.get(OWNER_LABEL) != "true":
        return [f"{kind} {name} wasn't created by supportops-lab (no {OWNER_LABEL} label)."]
    found = labels.get(LAB_ID_LABEL)
    if lab_id is not None and found != lab_id:
        return [
            f"{kind} {name} belongs to a different scenario lab (lab ID {found or 'missing'}, "
            f"expected {lab_id})."
        ]
    return []
