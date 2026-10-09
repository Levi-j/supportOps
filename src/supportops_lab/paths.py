from pathlib import Path

from supportops.errors import ConfigError

REPO_ROOT = Path(__file__).resolve().parents[2]
COMPOSE_FILE = REPO_ROOT / "compose.scenario.yaml"
STATE_ROOT = REPO_ROOT / ".lab"


def compose_file() -> Path:
    if not COMPOSE_FILE.is_file() or not (REPO_ROOT / "pyproject.toml").is_file():
        raise ConfigError(
            f"The scenario Compose file wasn't found at {COMPOSE_FILE}.",
            hint="Run supportops-lab from a SupportOps checkout (uv run supportops-lab ...).",
        )
    return COMPOSE_FILE
