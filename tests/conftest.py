import os
from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def isolated_environment(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    for name in list(os.environ):
        if name.upper().startswith("SUPPORTOPS_"):
            monkeypatch.delenv(name)
    monkeypatch.chdir(tmp_path)
    return tmp_path
