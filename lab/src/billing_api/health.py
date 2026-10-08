from collections.abc import Callable
from typing import Literal

from fastapi import APIRouter, Response
from pydantic import BaseModel

from billing_api.database import DatabaseStatus


class LivenessResponse(BaseModel):
    status: Literal["ok"]
    environment: str


class DependencyCheck(BaseModel):
    status: Literal["up", "down"]
    latency_ms: int
    error: str | None = None


class ReadinessResponse(BaseModel):
    status: Literal["ready", "not_ready"]
    checks: dict[str, DependencyCheck]


def health_router(environment: str, check_database: Callable[[], DatabaseStatus]) -> APIRouter:
    router = APIRouter(tags=["health"])

    @router.get("/health", summary="Liveness: the process is running")
    def liveness() -> LivenessResponse:
        return LivenessResponse(status="ok", environment=environment)

    @router.get(
        "/health/ready",
        summary="Readiness: the service can reach its database",
        response_model_exclude_none=True,
        responses={503: {"model": ReadinessResponse, "description": "Not ready"}},
    )
    def readiness(response: Response) -> ReadinessResponse:
        database = check_database()
        if not database.up:
            response.status_code = 503
        return ReadinessResponse(
            status="ready" if database.up else "not_ready",
            checks={
                "database": DependencyCheck(
                    status="up" if database.up else "down",
                    latency_ms=database.latency_ms,
                    error=database.error,
                )
            },
        )

    return router
