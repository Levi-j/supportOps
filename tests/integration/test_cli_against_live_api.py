import json
import os
import socket
import subprocess
import sys
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
import pytest
from typer.testing import CliRunner, Result

from supportops.cli.main import app
from tests.integration.support import (
    JUNIPER_KEY,
    JUNIPER_REVOKED_KEY,
    LabDatabase,
    fetch_all,
)

pytestmark = pytest.mark.integration

runner = CliRunner()


@dataclass(frozen=True)
class LiveApi:
    url: str
    log_file: Path

    def logs(self) -> list[dict[str, Any]]:
        lines = self.log_file.read_text(encoding="utf-8", errors="replace").splitlines()
        return [json.loads(line) for line in lines if line.startswith("{")]


ApiStarter = Callable[[str], LiveApi]


def free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


@pytest.fixture
def start_api(tmp_path: Path) -> Iterator[ApiStarter]:
    processes: list[subprocess.Popen[bytes]] = []

    def start(database_url: str) -> LiveApi:
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
        _wait_until_live(process, api)
        return api

    yield start
    for process in processes:
        process.terminate()
        process.wait(timeout=10)


@pytest.fixture
def live_api(start_api: ApiStarter, billing_db: LabDatabase) -> LiveApi:
    return start_api(billing_db.url("billing_app"))


def _wait_until_live(process: subprocess.Popen[bytes], api: LiveApi) -> None:
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


def invoke(api_url: str, *args: str, **env: str) -> Result:
    return runner.invoke(
        app, list(args), env={"COLUMNS": "200", "SUPPORTOPS_API_URL": api_url, **env}
    )


def customer_count(database: LabDatabase) -> int:
    return int(fetch_all(database, "SELECT count(*) FROM billing.customers")[0][0])


def test_health_is_healthy_against_the_running_api(
    live_api: LiveApi, billing_db: LabDatabase
) -> None:
    result = invoke(live_api.url, "health", SUPPORTOPS_DB_URL=billing_db.url("supportops_ro"))

    assert result.exit_code == 0, result.output
    assert "HEALTHY" in result.stdout
    assert "read-only session" in result.stdout


def test_health_blames_the_api_side_when_postgres_answers_support(
    start_api: ApiStarter, billing_db: LabDatabase
) -> None:
    api = start_api(billing_db.url("billing_app", password="not-the-password"))

    result = invoke(api.url, "health", "--json", SUPPORTOPS_DB_URL=billing_db.url("supportops_ro"))

    report = json.loads(result.stdout)
    assert result.exit_code == 1
    assert report["verdict"] == "DEGRADED"
    assert report["diagnosis"] == "api_cannot_reach_database"
    assert report["api_database"]["error"] == "authentication_failed"
    assert report["database"]["reachable"] is True


def test_health_reports_a_likely_outage_when_nobody_reaches_postgres(
    start_api: ApiStarter,
) -> None:
    closed = f"127.0.0.1:{free_port()}"
    api = start_api(f"postgresql://billing_app:unused@{closed}/billing")

    result = invoke(
        api.url,
        "health",
        "--json",
        SUPPORTOPS_DB_URL=f"postgresql://supportops_ro:unused@{closed}/billing",
        SUPPORTOPS_CONNECT_TIMEOUT_SECONDS="1",
    )

    report = json.loads(result.stdout)
    assert result.exit_code == 1
    assert report["diagnosis"] == "database_outage"
    assert report["database"]["server_answered"] is False


def test_health_reports_down_when_nothing_listens() -> None:
    result = invoke(f"http://127.0.0.1:{free_port()}", "health", "--json")

    report = json.loads(result.stdout)
    assert result.exit_code == 1
    assert report["verdict"] == "DOWN"
    assert report["liveness"]["failure"]["category"] == "connection_refused"


def test_request_ids_are_echoed_by_the_running_api(live_api: LiveApi) -> None:
    result = invoke(
        live_api.url,
        "api",
        "request",
        "GET",
        "/v1/account",
        "--request-id",
        "it-account-1",
        SUPPORTOPS_API_KEY=JUNIPER_KEY,
    )

    assert result.exit_code == 0, result.output
    assert "Request ID: it-account-1 (echoed by the server)" in result.stdout
    assert "acct_juniper" in result.stdout
    assert JUNIPER_KEY not in result.output


def test_a_rejected_key_can_be_traced_in_the_api_logs(live_api: LiveApi) -> None:
    result = invoke(
        live_api.url,
        "api",
        "request",
        "GET",
        "/v1/account",
        "--key-env",
        "OLD_KEY",
        "--request-id",
        "it-revoked-1",
        OLD_KEY=JUNIPER_REVOKED_KEY,
    )

    assert result.exit_code == 1
    assert "401 Unauthorized" in result.stdout
    assert "bk_juniper00***" in result.stdout
    assert JUNIPER_REVOKED_KEY not in result.output
    rejected = [entry for entry in live_api.logs() if entry.get("event_name") == "auth.rejected"]
    assert [(entry["request_id"], entry["reason"]) for entry in rejected] == [
        ("it-revoked-1", "revoked_key")
    ]


def test_writes_need_yes_against_the_running_api(
    live_api: LiveApi, billing_db: LabDatabase, tmp_path: Path
) -> None:
    body = tmp_path / "customer.json"
    body.write_bytes(b'{"name": "Integration Test Ltd", "email": "it@integration.example"}')
    before = customer_count(billing_db)
    request = ("api", "request", "POST", "/v1/customers", "--data-file", str(body))

    refused = invoke(live_api.url, *request, SUPPORTOPS_API_KEY=JUNIPER_KEY)
    unchanged = customer_count(billing_db)
    created = invoke(live_api.url, *request, "--yes", SUPPORTOPS_API_KEY=JUNIPER_KEY)

    assert refused.exit_code == 2
    assert unchanged == before
    assert created.exit_code == 0, created.output
    assert "201 Created" in created.stdout
    assert customer_count(billing_db) == before + 1


def test_json_mangled_by_powershell_is_rejected_by_the_api_when_sent_raw(
    live_api: LiveApi,
) -> None:
    result = invoke(
        live_api.url,
        "api",
        "request",
        "POST",
        "/v1/customers",
        "--data",
        "{name:Acme,email:billing@acme.example}",
        "--raw",
        "--yes",
        SUPPORTOPS_API_KEY=JUNIPER_KEY,
    )

    assert result.exit_code == 1
    assert "400 Bad Request" in result.stdout
    assert "Problem: MALFORMED_REQUEST" in result.stdout


def test_latency_against_the_running_api(live_api: LiveApi) -> None:
    result = invoke(
        live_api.url,
        "api",
        "latency",
        "/v1/invoices",
        "-n",
        "5",
        "--json",
        SUPPORTOPS_API_KEY=JUNIPER_KEY,
    )

    report = json.loads(result.stdout)
    assert result.exit_code == 0, result.output
    assert report["successful"] == 5
    assert report["status_counts"] == {"200": 5}
    logged = {
        entry["request_id"]
        for entry in live_api.logs()
        if entry.get("event_name") == "http.request"
        and entry["request_id"].startswith("supportops-latency-")
    }
    assert logged == {sample["request_id"] for sample in report["samples"]}
