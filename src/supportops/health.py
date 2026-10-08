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
    readiness = None
    api_database = None
    if liveness.failure is None and liveness.status == 200:
        readiness = send(client, "GET", profile.readiness_path, clock=clock)
        api_database = api_database_view(readiness)
    database = None
    if settings.db_url is not None:
        database = probe_database(settings.db_url, settings.connect_timeout_seconds)
    assessment = assess(profile, liveness, readiness, api_database, database)
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
    log_step = (
        "Find the API's database errors: "
        "docker compose logs billing-api | Select-String db.unavailable"
    )
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
                "After fixing the configuration, recreate the API: "
                "docker compose up -d billing-api",
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
