import socket
import sys

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr

from billing_api.config import BillingSettings
from billing_api.main import create_app
from tests.integration.support import LabDatabase

pytestmark = pytest.mark.integration


def readiness(database_url: str, timeout: int = 3) -> tuple[int, dict[str, object]]:
    settings = BillingSettings(
        database_url=SecretStr(database_url), env="lab", db_connect_timeout_seconds=timeout
    )
    response = TestClient(create_app(settings)).get("/health/ready")
    return response.status_code, response.json()["checks"]["database"]


def unused_local_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port: int = probe.getsockname()[1]
    return port


def test_ready_with_the_application_role(lab_database: LabDatabase) -> None:
    status, database = readiness(lab_database.url("billing_app"))

    assert status == 200
    assert database["status"] == "up"


def test_wrong_password_is_reported_as_authentication_failure(lab_database: LabDatabase) -> None:
    status, database = readiness(lab_database.url("billing_app", password="wrong"))

    assert status == 503
    assert database["error"] == "authentication_failed"


def test_unknown_database_is_reported(lab_database: LabDatabase) -> None:
    status, database = readiness(lab_database.url("billing_app", database="billing_prod"))

    assert status == 503
    assert database["error"] == "database_missing"


def test_unresolvable_host_is_reported_as_dns_failure() -> None:
    status, database = readiness("postgresql://billing_app:pw@billing-db.invalid:5432/billing")

    assert status == 503
    assert database["error"] == "dns_failure"


def test_closed_port_is_reported_as_unreachable() -> None:
    port = unused_local_port()
    expected = "timeout" if sys.platform == "win32" else "connection_refused"

    status, database = readiness(f"postgresql://billing_app:pw@127.0.0.1:{port}/billing", timeout=1)

    assert status == 503
    assert database["error"] == expected
