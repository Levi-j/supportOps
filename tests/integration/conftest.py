import os
import subprocess
import sys
import time
import uuid
from collections.abc import Iterator
from dataclasses import replace
from pathlib import Path
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
    ApiStarter,
    ClientFactory,
    LabDatabase,
    LiveApi,
    free_port,
    wait_until_live,
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


@pytest.fixture
def start_api(tmp_path: Path) -> Iterator[ApiStarter]:
    processes: list[subprocess.Popen[bytes]] = []

    def start(database_url: str, **settings: str) -> LiveApi:
        port = free_port()
        api = LiveApi(url=f"http://127.0.0.1:{port}", log_file=tmp_path / f"api-{port}.log")
        environment = {
            name: value
            for name, value in os.environ.items()
            if not name.upper().startswith("BILLING_")
        }
        environment |= {
            "BILLING_DATABASE_URL": database_url,
            "BILLING_ENV": "lab",
            "BILLING_DB_CONNECT_TIMEOUT_SECONDS": "1",
            **{f"BILLING_{name.upper()}": value for name, value in settings.items()},
        }
        command = [
            sys.executable,
            "-m",
            "uvicorn",
            "--factory",
            "billing_api.main:create_app",
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
            "--no-access-log",
        ]
        with api.log_file.open("wb") as output:
            process = subprocess.Popen(  # noqa: S603
                command, env=environment, stdout=output, stderr=subprocess.STDOUT
            )
        processes.append(process)
        wait_until_live(process, api)
        return api

    yield start
    for process in processes:
        process.terminate()
        process.wait(timeout=10)


@pytest.fixture
def live_api(start_api: ApiStarter, billing_db: LabDatabase) -> LiveApi:
    return start_api(billing_db.url("billing_app"))


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
