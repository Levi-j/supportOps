import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr

from billing_api.config import BillingSettings
from billing_api.main import create_app
from tests.integration.support import LabDatabase

pytestmark = pytest.mark.integration

WRONG_ENCODED_PASSWORD = "Wr%25ong%40Pass%22word"
WRONG_DECODED_PASSWORD = 'Wr%ong@Pass"word'


def test_rejected_login_is_classified_without_logging_the_password(
    billing_db: LabDatabase, capsys: pytest.CaptureFixture[str]
) -> None:
    settings = BillingSettings(
        database_url=SecretStr(billing_db.url("billing_app", password=WRONG_ENCODED_PASSWORD)),
        env="lab",
    )

    with TestClient(create_app(settings)) as client:
        response = client.get("/health/ready")

    logged = capsys.readouterr().out
    unavailable = [
        entry
        for entry in map(json.loads, logged.splitlines())
        if entry.get("event_name") == "db.unavailable"
    ]
    assert response.status_code == 503
    assert response.json()["checks"]["database"]["error"] == "authentication_failed"
    assert [entry["error"] for entry in unavailable] == ["authentication_failed"]
    assert "Wr%" not in logged
    assert "Wr%" not in response.text


def test_server_startup_with_an_unparseable_url_prints_no_password(tmp_path: Path) -> None:
    environment = {
        name: value for name, value in os.environ.items() if not name.startswith("BILLING_")
    }
    environment["BILLING_DATABASE_URL"] = "postgresql://billing_app:Fake%zzPw@127.0.0.1:1/billing"
    command = [sys.executable, "-m", "uvicorn", "--factory", "billing_api.main:create_app"]

    finished = subprocess.run(  # noqa: S603
        command, env=environment, cwd=tmp_path, capture_output=True, timeout=60, check=False
    )

    output = (finished.stdout + finished.stderr).decode("utf-8", errors="replace")
    assert finished.returncode != 0
    assert "BILLING_DATABASE_URL" in output
    assert "percent-encode special characters" in output
    assert "Fake" not in output
