import time
from dataclasses import dataclass, field
from enum import StrEnum

import httpx
from pydantic import BaseModel, Field

from supportops.db.connection import DatabaseProbe, probe_database
from supportops.http_checks import LOCALHOST_NOTE, Clock, HttpResult, send
from supportops.settings import Settings
from supportops.targets import TargetProfile

SLOW_CONNECTION_MS = 1000


class Verdict(StrEnum):
    HEALTHY = "HEALTHY"
    DEGRADED = "DEGRADED"
    DOWN = "DOWN"
    INCONCLUSIVE = "INCONCLUSIVE"


class ApiDatabaseView(BaseModel):
    status: str | None = None
    error: str | None = None


class HealthReport(BaseModel):
    target: str
    api_url: str
    verdict: Verdict
    diagnosis: str
    summary: str
    liveness: HttpResult
    readiness: HttpResult | None = None
    api_database: ApiDatabaseView | None = None
    database: DatabaseProbe | None = None
    notes: list[str] = Field(default_factory=list)
    next_steps: list[str] = Field(default_factory=list)


@dataclass(frozen=True)
class Assessment:
    verdict: Verdict
    diagnosis: str
    summary: str
    notes: list[str] = field(default_factory=list)
    next_steps: list[str] = field(default_factory=list)


def run_health_check(
    settings: Settings,
    profile: TargetProfile,
    client: httpx.Client,
    clock: Clock = time.perf_counter,
) -> HealthReport:
    liveness = send(client, "GET", profile.liveness_path, clock=clock)
    actuator = profile.health_style == "actuator"
    readiness = None
    api_database = None
    if liveness.failure is None and (
        liveness.status == 200 or (actuator and liveness.status == 404)
    ):
        readiness = send(client, "GET", profile.readiness_path, clock=clock)
        api_database = None if actuator else api_database_view(readiness)
    database = None
    if settings.db_url is not None:
        database = probe_database(settings.db_url, settings.connect_timeout_seconds)
    assessment = (
        assess_actuator(profile, liveness, readiness, database)
        if actuator
        else assess(profile, liveness, readiness, api_database, database)
    )
    return HealthReport(
        target=profile.name,
        api_url=str(settings.api_url),
        verdict=assessment.verdict,
        diagnosis=assessment.diagnosis,
        summary=assessment.summary,
        liveness=liveness,
        readiness=readiness,
        api_database=api_database,
        database=database,
        notes=[*assessment.notes, *_localhost_notes(settings, liveness, database)],
        next_steps=assessment.next_steps,
    )


def _localhost_notes(
    settings: Settings, liveness: HttpResult, database: DatabaseProbe | None
) -> list[str]:
    slow = []
    if (
        settings.api_url.host == "localhost"
        and liveness.failure is None
        and liveness.duration_ms >= SLOW_CONNECTION_MS
    ):
        slow.append("SUPPORTOPS_API_URL")
    if (
        database is not None
        and database.host == "localhost"
        and database.reachable
        and database.latency_ms >= SLOW_CONNECTION_MS
    ):
        slow.append("SUPPORTOPS_DB_URL")
    if not slow:
        return []
    verb = "use" if len(slow) > 1 else "uses"
    return [
        f"{' and '.join(slow)} {verb} 'localhost', and connecting took over a second. "
        + LOCALHOST_NOTE
    ]


def api_database_view(readiness: HttpResult) -> ApiDatabaseView | None:
    body = readiness.json_body()
    if not isinstance(body, dict):
        return None
    checks = body.get("checks")
    database = checks.get("database") if isinstance(checks, dict) else None
    if not isinstance(database, dict):
        return None
    status = database.get("status")
    error = database.get("error")
    return ApiDatabaseView(
        status=status if isinstance(status, str) else None,
        error=error if isinstance(error, str) else None,
    )


def assess(
    profile: TargetProfile,
    liveness: HttpResult,
    readiness: HttpResult | None,
    api_database: ApiDatabaseView | None,
    database: DatabaseProbe | None,
) -> Assessment:
    notes = _support_database_notes(database, api_ready=_is_ready(readiness))
    if liveness.failure is not None:
        return Assessment(
            Verdict.DOWN,
            "api_unreachable",
            f"The API could not be reached. {liveness.failure.summary}",
            notes + _context_when_api_is_down(database),
            [
                liveness.failure.hint,
                "In the lab, check the containers with 'docker compose ps'.",
                "Read the API's recent logs: docker compose logs billing-api --tail 50",
            ],
        )
    if liveness.status != 200:
        return Assessment(
            Verdict.INCONCLUSIVE,
            "unexpected_liveness_response",
            f"Something answered at {liveness.url}, but with HTTP {liveness.status} "
            "instead of 200.",
            [
                *notes,
                "A running billing API always answers its liveness check with 200. "
                "SUPPORTOPS_API_URL may point to a different service, or the API itself "
                "is failing.",
            ],
            [
                "Check SUPPORTOPS_API_URL (the lab API is http://127.0.0.1:8001).",
                f"Look at the response: supportops api request GET {profile.liveness_path}",
            ],
        )
    if readiness is None or readiness.failure is not None:
        failure = readiness.failure if readiness else None
        return Assessment(
            Verdict.DEGRADED,
            "readiness_unanswered",
            "The API is running, but its readiness check didn't answer."
            + (f" {failure.summary}" if failure else ""),
            notes + _database_context(database),
            ([failure.hint] if failure else [])
            + ["Read the API's recent logs: docker compose logs billing-api --tail 50"],
        )
    if readiness.status == 200:
        return Assessment(
            Verdict.HEALTHY, "api_ready", "The API is running and ready to serve requests.", notes
        )
    if readiness.status != 503:
        return Assessment(
            Verdict.INCONCLUSIVE,
            "unexpected_readiness_response",
            f"The readiness check answered HTTP {readiness.status}, which the billing API "
            "doesn't normally return.",
            notes,
            [f"Look at the response: supportops api request GET {profile.readiness_path}"],
        )
    if api_database is not None and api_database.status == "up":
        return Assessment(
            Verdict.INCONCLUSIVE,
            "contradictory_readiness",
            "The readiness check returned 503 but reported its database as up.",
            notes,
            [f"Look at the response: supportops api request GET {profile.readiness_path}"],
        )
    reported = (
        f" (the API reports: {api_database.error})" if api_database and api_database.error else ""
    )
    log_step = "Find the API's database errors: supportops logs search --event db.unavailable"
    if database is None:
        return Assessment(
            Verdict.DEGRADED,
            "readiness_failing_cause_unknown",
            f"The API is running but not ready: it can't use its database{reported}.",
            notes,
            [
                "Set SUPPORTOPS_DB_URL so SupportOps can check PostgreSQL directly and tell an "
                "API-side problem from a database outage.",
                log_step,
            ],
        )
    if database.server_answered:
        return Assessment(
            Verdict.DEGRADED,
            "api_cannot_reach_database",
            f"The API is running but can't use its database{reported}, while PostgreSQL "
            "answers from this machine. This points to the API's database configuration or "
            "to the network path between the API and PostgreSQL.",
            notes,
            [
                log_step,
                "Check the API's database host, port, name and credentials. Inside a container, "
                "localhost means the container itself, not the database.",
                "After the API's database settings are corrected, whoever operates the service "
                "must restart or redeploy it; then run 'supportops health' again.",
            ],
        )
    if database.error == "error":
        return Assessment(
            Verdict.DEGRADED,
            "readiness_failing_cause_unknown",
            f"The API is running but not ready: it can't use its database{reported}.",
            notes,
            [
                "SupportOps' own database check failed for a reason it couldn't classify, so it "
                "can't tell an API-side problem from a database outage. See the error details.",
                log_step,
            ],
        )
    return Assessment(
        Verdict.DEGRADED,
        "database_outage",
        f"Neither the API{reported} nor this machine can reach PostgreSQL "
        f"({database.error}). A database outage is likely.",
        notes,
        [
            "Check the database container: docker compose ps postgres",
            "Read its recent logs: docker compose logs postgres --tail 50",
            "If someone else operates the database, escalate to them with this output.",
        ],
    )


def assess_actuator(
    profile: TargetProfile,
    liveness: HttpResult,
    health: HttpResult | None,
    database: DatabaseProbe | None,
) -> Assessment:
    notes = _actuator_database_notes(database, service_up=_is_ready(health))
    find_errors = "Look for errors in the service's logs: supportops logs search --level ERROR"
    show_health = f"Look at the response: supportops api request GET {profile.readiness_path}"
    if liveness.failure is not None:
        return Assessment(
            Verdict.DOWN,
            "api_unreachable",
            f"The service could not be reached. {liveness.failure.summary}",
            notes,
            [
                liveness.failure.hint,
                "Check that the service is running in its own environment, for example with "
                "'docker ps' on the machine that hosts it.",
            ],
        )
    if liveness.status == 404:
        aggregate = (
            f" {profile.readiness_path} answered {health.status} for reference."
            if health is not None and health.status is not None
            else ""
        )
        return Assessment(
            Verdict.INCONCLUSIVE,
            "liveness_endpoint_unavailable",
            f"The service answered, but {profile.liveness_path} returned 404, so its liveness "
            f"probe group appears to be disabled in this deployment.{aggregate}",
            notes,
            [show_health, "Ask the service owner whether the Actuator probe groups are enabled."],
        )
    if liveness.status != 200:
        return Assessment(
            Verdict.INCONCLUSIVE,
            "unexpected_liveness_response",
            f"Something answered at {liveness.url}, but with HTTP {liveness.status} "
            "instead of 200.",
            [
                *notes,
                "SUPPORTOPS_API_URL may point to a different service, or the service itself is "
                "failing.",
            ],
            [f"Look at the response: supportops api request GET {profile.liveness_path}"],
        )
    if health is None or health.failure is not None:
        failure = health.failure if health else None
        return Assessment(
            Verdict.DEGRADED,
            "readiness_unanswered",
            "The service is live, but its health endpoint didn't answer."
            + (f" {failure.summary}" if failure else ""),
            notes,
            ([failure.hint] if failure else []) + [find_errors],
        )
    if health.status == 200:
        return Assessment(
            Verdict.HEALTHY, "api_ready", "The service is live and reports its health as UP.", notes
        )
    if health.status != 503:
        return Assessment(
            Verdict.INCONCLUSIVE,
            "unexpected_readiness_response",
            f"{profile.readiness_path} answered HTTP {health.status}, which a health endpoint "
            "doesn't normally return.",
            notes,
            [show_health],
        )
    unnamed = (
        "The service reports DOWN, but its health response doesn't say which component failed."
    )
    ask_owner = "Ask the service owner which health component reports DOWN."
    if database is None:
        return Assessment(
            Verdict.DEGRADED,
            "health_down_database_not_checked",
            f"{unnamed} PostgreSQL wasn't checked because SUPPORTOPS_DB_URL is not set.",
            notes,
            [
                "Set SUPPORTOPS_DB_URL to a read-only database role to compare with PostgreSQL "
                "directly.",
                find_errors,
                ask_owner,
            ],
        )
    if database.server_answered:
        return Assessment(
            Verdict.DEGRADED,
            "health_down_database_reachable",
            f"{unnamed} PostgreSQL answers from this machine, so a database outage is unlikely "
            "from here. The cause may be the service's own database connection or settings, or "
            "another health component.",
            notes,
            [find_errors, ask_owner, show_health],
        )
    return Assessment(
        Verdict.DEGRADED,
        "health_down_database_unreachable",
        f"{unnamed} PostgreSQL doesn't answer from this machine either ({database.error}). Both "
        "observations are consistent with a database problem, but the health response doesn't "
        "name the failing component, and this machine's network path to PostgreSQL may differ "
        "from the service's. This is not a confirmed database outage.",
        notes,
        [
            find_errors,
            "Check that SUPPORTOPS_DB_URL points at the service's database.",
            "If someone else operates the database, share this output with them.",
        ],
    )


def _actuator_database_notes(database: DatabaseProbe | None, *, service_up: bool) -> list[str]:
    if database is None:
        return ["Database check skipped: SUPPORTOPS_DB_URL is not set."]
    if database.reachable:
        return []
    if database.server_answered:
        return [
            f"PostgreSQL answered, but SupportOps' own connection was refused ({database.error}). "
            "Check SUPPORTOPS_DB_URL; this says nothing about the service's own connection."
        ]
    if service_up:
        return [
            f"SupportOps couldn't reach PostgreSQL from this machine ({database.error}), although "
            "the service reports UP. Check SUPPORTOPS_DB_URL."
        ]
    return []


def _is_ready(readiness: HttpResult | None) -> bool:
    return readiness is not None and readiness.status == 200


def _support_database_notes(database: DatabaseProbe | None, *, api_ready: bool) -> list[str]:
    if database is None:
        return ["Database check skipped: SUPPORTOPS_DB_URL is not set."]
    if database.reachable:
        return []
    if database.server_answered:
        return [
            f"PostgreSQL answered, but SupportOps' own connection was refused ({database.error}). "
            "Check SUPPORTOPS_DB_URL; this says nothing about the API's own connection."
        ]
    if api_ready:
        return [
            f"SupportOps couldn't reach PostgreSQL from this machine ({database.error}), although "
            "the API can. Check SUPPORTOPS_DB_URL and the published port (the lab uses "
            "127.0.0.1:5433)."
        ]
    return []


def _context_when_api_is_down(database: DatabaseProbe | None) -> list[str]:
    if database is None:
        return []
    if database.server_answered:
        return [
            "PostgreSQL answers from this machine, so the problem is limited to the API "
            "process or the port it is published on."
        ]
    return [
        "PostgreSQL isn't reachable from this machine either. The whole lab, Docker, or this "
        "machine's network may be down."
    ]


def _database_context(database: DatabaseProbe | None) -> list[str]:
    if database is None or database.server_answered:
        return []
    return [
        f"PostgreSQL isn't reachable from this machine either ({database.error}), which makes "
        "a database problem more likely, but the evidence isn't conclusive."
    ]
