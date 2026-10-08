import logging
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from functools import partial
from importlib.metadata import version

from fastapi import FastAPI

from billing_api import database, routes
from billing_api.config import BillingSettings, ConfigurationError, load_settings
from billing_api.database import DatabaseStatus
from billing_api.errors import install_error_handlers
from billing_api.health import health_router
from billing_api.logging_setup import configure_logging
from billing_api.middleware import RequestContextMiddleware

logger = logging.getLogger("billing_api")


def create_app(
    settings: BillingSettings | None = None,
    check_database: Callable[[], DatabaseStatus] | None = None,
) -> FastAPI:
    settings = settings or _load_settings_or_log()
    configure_logging(settings.log_level, secrets=database.credentials(settings))
    checker = check_database or partial(database.check_database, settings)
    app_version = version("supportops-lab")
    faults = sorted(settings.faults)

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        logger.info(
            "Billing API started",
            extra={
                "event_name": "app.started",
                "version": app_version,
                "environment": settings.env,
                **database.describe_target(settings),
                "db_statement_timeout_ms": settings.db_statement_timeout_ms,
                "db_lock_timeout_ms": settings.db_lock_timeout_ms,
                "faults": faults,
            },
        )
        for fault in faults:
            logger.warning(
                "Lab fault enabled",
                extra={"event_name": "lab.fault_enabled", "fault": fault},
            )
        yield

    app = FastAPI(
        title="Billing API",
        summary="Demo invoicing API for the SupportOps troubleshooting lab.",
        version=app_version,
        lifespan=lifespan,
    )
    app.state.settings = settings
    app.add_middleware(RequestContextMiddleware)
    install_error_handlers(app)
    app.include_router(health_router(settings.env, checker))
    app.include_router(routes.router)
    return app


def _load_settings_or_log() -> BillingSettings:
    try:
        return load_settings()
    except ConfigurationError as exc:
        configure_logging("INFO")
        logger.critical(str(exc), extra={"event_name": "app.config_invalid"})
        raise
