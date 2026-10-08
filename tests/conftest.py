import logging
import os
from collections.abc import Iterator
from pathlib import Path

import pytest

from tests.log_capture import JsonCapture


@pytest.fixture(autouse=True)
def isolated_environment(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    for name in list(os.environ):
        if name.upper().startswith("SUPPORTOPS_"):
            monkeypatch.delenv(name)
    monkeypatch.chdir(tmp_path)
    return tmp_path


@pytest.fixture
def logs() -> Iterator[JsonCapture]:
    capture = JsonCapture()
    root = logging.getLogger()
    root.addHandler(capture)
    yield capture
    root.removeHandler(capture)
