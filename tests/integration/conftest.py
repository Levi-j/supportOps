import time
from collections.abc import Iterator

import psycopg
import pytest
from testcontainers.core.container import DockerContainer

from tests.integration.support import DATABASE, PASSWORDS, POSTGRES_IMAGE, SQL_DIR, LabDatabase


@pytest.fixture(scope="session")
def lab_database() -> Iterator[LabDatabase]:
    container = (
        DockerContainer(POSTGRES_IMAGE)
        .with_env("POSTGRES_DB", DATABASE)
        .with_env("POSTGRES_USER", "lab_admin")
        .with_env("POSTGRES_PASSWORD", PASSWORDS["lab_admin"])
        .with_env("BILLING_APP_DB_PASSWORD", PASSWORDS["billing_app"])
        .with_env("SUPPORTOPS_RO_DB_PASSWORD", PASSWORDS["supportops_ro"])
        .with_volume_mapping(str(SQL_DIR), "/docker-entrypoint-initdb.d", "ro")
        .with_exposed_ports(5432)
    )
    with container:
        database = LabDatabase(
            host=container.get_container_host_ip(), port=int(container.get_exposed_port(5432))
        )
        _wait_until_initialised(database)
        yield database


def _wait_until_initialised(database: LabDatabase, timeout_seconds: float = 90) -> None:
    deadline = time.monotonic() + timeout_seconds
    while True:
        try:
            with psycopg.connect(database.url("lab_admin"), connect_timeout=2) as connection:
                connection.execute("SELECT count(*) FROM billing.payments")
                return
        except psycopg.Error:
            if time.monotonic() > deadline:
                raise
            time.sleep(0.5)
