import json
import socket
import subprocess
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, LiteralString

import httpx
import psycopg
from fastapi.testclient import TestClient

SQL_DIR = Path(__file__).resolve().parents[2] / "lab" / "sql"
POSTGRES_IMAGE = "postgres:18"
DATABASE = "billing"
TEMPLATE_DATABASE = "billing_template"
PASSWORDS = {
    "lab_admin": "it_admin_password",
    "billing_app": "it_app_password",
    "supportops_ro": "it_readonly_password",
}

JUNIPER_KEY = "bk_juniper01_lab_only_not_a_real_key"
JUNIPER_REVOKED_KEY = "bk_juniper00_lab_only_not_a_real_key"
KESTREL_KEY = "bk_kestrel01_lab_only_not_a_real_key"
SUSPENDED_ALDER_KEY = "bk_alderfin1_lab_only_not_a_real_key"
LAB_KEYS = {
    "key_juniper_old": JUNIPER_REVOKED_KEY,
    "key_juniper_main": JUNIPER_KEY,
    "key_kestrel_main": KESTREL_KEY,
    "key_alder_main": SUSPENDED_ALDER_KEY,
}

ClientFactory = Callable[..., TestClient]


@dataclass(frozen=True)
class LabDatabase:
    host: str
    port: int
    database: str = DATABASE
    container_id: str = ""

    def url(self, role: str, *, password: str | None = None, database: str | None = None) -> str:
        secret = PASSWORDS[role] if password is None else password
        return f"postgresql://{role}:{secret}@{self.host}:{self.port}/{database or self.database}"


def bearer(api_key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {api_key}"}


def fetch_all(
    database: LabDatabase, query: LiteralString, params: tuple[Any, ...] = ()
) -> list[tuple[Any, ...]]:
    with psycopg.connect(database.url("lab_admin")) as connection:
        return connection.execute(query, params).fetchall()


def execute(database: LabDatabase, statement: LiteralString, params: tuple[Any, ...] = ()) -> None:
    with psycopg.connect(database.url("lab_admin")) as connection:
        connection.execute(statement, params)


@contextmanager
def open_transaction(
    database: LabDatabase, statement: LiteralString, application: str
) -> Iterator[psycopg.Connection[Any]]:
    connection = psycopg.connect(database.url("lab_admin"), application_name=application)
    try:
        connection.execute(statement)
        yield connection
    finally:
        connection.rollback()
        connection.close()


@dataclass(frozen=True)
class LiveApi:
    url: str
    log_file: Path

    def logs(self) -> list[dict[str, Any]]:
        lines = self.log_file.read_text(encoding="utf-8", errors="replace").splitlines()
        return [json.loads(line) for line in lines if line.startswith("{")]


ApiStarter = Callable[..., LiveApi]


def free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def wait_until_live(process: subprocess.Popen[bytes], api: LiveApi) -> None:
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError(f"billing-api exited early:\n{api.log_file.read_text()}")
        try:
            if httpx.get(f"{api.url}/health", timeout=1).status_code == 200:
                return
        except httpx.TransportError:
            pass
        time.sleep(0.2)
    raise RuntimeError("billing-api did not start within 30 seconds")
