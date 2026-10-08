import time
import uuid
from collections.abc import Iterator
from dataclasses import replace
from typing import Any, LiteralString

import psycopg
import pytest
from fastapi.testclient import TestClient
from psycopg import sql
from pydantic import SecretStr
from testcontainers.core.container import DockerContainer

from billing_api.config import BillingSettings
from billing_api.main import create_app
from tests.integration.support import (
    DATABASE,
    PASSWORDS,
    POSTGRES_IMAGE,
    SQL_DIR,
    TEMPLATE_DATABASE,
    ClientFactory,
    LabDatabase,
)


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
        _admin(database, "CREATE DATABASE {} TEMPLATE {}", TEMPLATE_DATABASE, DATABASE)
        yield database


@pytest.fixture
def billing_db(lab_database: LabDatabase) -> Iterator[LabDatabase]:
    name = f"billing_{uuid.uuid4().hex[:12]}"
    _admin(lab_database, "CREATE DATABASE {} TEMPLATE {}", name, TEMPLATE_DATABASE)
    _admin(lab_database, "REVOKE ALL ON DATABASE {} FROM PUBLIC", name)
    _admin(lab_database, "GRANT CONNECT ON DATABASE {} TO billing_app, supportops_ro", name)
    yield replace(lab_database, database=name)
    _admin(lab_database, "DROP DATABASE {} WITH (FORCE)", name)


@pytest.fixture
def make_client(billing_db: LabDatabase) -> Iterator[ClientFactory]:
    clients: list[TestClient] = []

    def factory(**overrides: Any) -> TestClient:
        settings = BillingSettings(
            database_url=SecretStr(billing_db.url("billing_app")), env="lab", **overrides
        )
        client = TestClient(create_app(settings))
        client.__enter__()
        clients.append(client)
        return client

    yield factory
    for client in clients:
        client.__exit__(None, None, None)


@pytest.fixture
def client(make_client: ClientFactory) -> TestClient:
    return make_client()


def _admin(database: LabDatabase, template: LiteralString, *names: str) -> None:
    statement = sql.SQL(template).format(*(sql.Identifier(name) for name in names))
    with psycopg.connect(
        database.url("lab_admin", database="postgres"), autocommit=True
    ) as connection:
        connection.execute(statement)


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
