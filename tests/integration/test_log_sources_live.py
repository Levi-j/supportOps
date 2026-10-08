import json
import os
import subprocess
import sys
import time
import uuid
from pathlib import Path

import pytest
from testcontainers.core.container import DockerContainer

from supportops.logs.parser import LogEvent, read_logs
from supportops.logs.sources import LogSourceError
from tests.integration.support import POSTGRES_IMAGE

pytestmark = pytest.mark.integration

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "logs"
JSON_LINE = (
    '{"timestamp": "2026-10-08T09:00:00Z", "level": "INFO", '
    '"request_id": "it-docker-1", "message": "hello from docker"}'
)


def test_docker_source_reads_a_real_container() -> None:
    script = f"echo '{JSON_LINE}'; echo 'plain text line'; sleep 60"
    container = DockerContainer(POSTGRES_IMAGE).with_command(["sh", "-c", script])
    with container:
        name = container.get_wrapped_container().name
        events: list[LogEvent] = []
        deadline = time.monotonic() + 30
        while not events and time.monotonic() < deadline:
            log_input = read_logs([f"docker:{name}"])
            events = list(log_input.events)
            if not events:
                time.sleep(0.5)

    assert [event.request_id for event in events] == ["it-docker-1"]
    assert log_input.stats.sources == [f"docker:{name}"]
    assert log_input.stats.skipped == {"not_json": 1}


def test_docker_source_reports_a_missing_container() -> None:
    with pytest.raises(LogSourceError, match="has no container named"):
        read_logs([f"docker:supportops-missing-{uuid.uuid4().hex[:8]}"])


def test_stdin_in_a_real_process_accepts_powershell_utf16(tmp_path: Path) -> None:
    environment = {
        name: value
        for name, value in os.environ.items()
        if not name.upper().startswith("SUPPORTOPS_")
    }

    finished = subprocess.run(
        [sys.executable, "-m", "supportops", "logs", "summary", "-", "--json"],
        input=(FIXTURES / "billing-api-utf16.jsonl").read_bytes(),
        capture_output=True,
        cwd=tmp_path,
        env=environment,
        timeout=60,
        check=False,
    )

    report = json.loads(finished.stdout)
    assert finished.returncode == 0
    assert report["entries"] == 5
    assert report["input"]["sources"] == ["stdin"]
