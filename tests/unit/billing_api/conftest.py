import pytest
from fastapi import FastAPI
from pydantic import SecretStr

from billing_api.config import BillingSettings
from billing_api.database import DatabaseStatus
from billing_api.main import create_app
from tests.log_capture import JsonCapture
from tests.unit.billing_api.support import FAKE_DB_PASSWORD, HEALTHY, AppFactory


@pytest.fixture
def settings() -> BillingSettings:
    return BillingSettings(
        database_url=SecretStr(
            f"postgresql://billing_app:{FAKE_DB_PASSWORD}@postgres:5432/billing"
        ),
        env="lab",
    )


@pytest.fixture
def make_app(settings: BillingSettings, logs: JsonCapture) -> AppFactory:
    def factory(status: DatabaseStatus = HEALTHY) -> FastAPI:
        return create_app(settings, check_database=lambda: status)

    return factory
