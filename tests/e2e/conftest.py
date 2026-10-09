import os
from collections.abc import Iterator
from pathlib import Path

import pytest

from supportops_lab.lab import ScenarioLab
from supportops_lab.ownership import RESERVED_HOST_PORTS
from tests.e2e.support import E2E_PROJECT


@pytest.fixture(scope="session")
def scenario_lab(tmp_path_factory: pytest.TempPathFactory) -> Iterator[ScenarioLab]:
    configured = os.environ.get("SCENARIO_LAB_STATE_DIR")
    state_root = Path(configured) if configured else tmp_path_factory.mktemp("scenario-lab")
    lab = ScenarioLab(E2E_PROJECT, state_root)
    try:
        lab.up(build=True)
        _assert_isolated(lab)
        yield lab
    finally:
        lab.down()


def _assert_isolated(lab: ScenarioLab) -> None:
    settings = lab.settings()
    assert settings.api_url.host == "127.0.0.1"
    assert settings.api_url.port not in RESERVED_HOST_PORTS
    assert settings.db_url is not None
    database = settings.db_url.get_secret_value()
    assert all(f":{port}/" not in database for port in RESERVED_HOST_PORTS)
    assert settings.log_source == f"docker:{E2E_PROJECT}-billing-api-1"
    assert lab.store.directory.parent != Path.cwd() / ".lab"
