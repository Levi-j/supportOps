import json
from pathlib import Path
from typing import NoReturn

import pytest
from typer.testing import CliRunner

from supportops import __version__
from supportops.cli.main import app

FAKE_KEY = "bk_clisecret0123456789abcdefghijk"
FAKE_DB_PASSWORD = "CliS3cretPw"
WIDE = {"COLUMNS": "200"}

runner = CliRunner()


def write_env(directory: Path) -> None:
    (directory / ".env").write_text(
        f"SUPPORTOPS_API_KEY={FAKE_KEY}\n"
        f"SUPPORTOPS_DB_URL=postgresql://supportops_ro:{FAKE_DB_PASSWORD}@localhost:5433/billing\n",
        encoding="utf-8",
    )


def fail_with_secret(*_args: object, **_kwargs: object) -> NoReturn:
    raise RuntimeError(f"connection failed for postgresql://app:{FAKE_DB_PASSWORD}@db/billing")


def test_help_lists_the_config_command() -> None:
    result = runner.invoke(app, ["--help"], env=WIDE)

    assert result.exit_code == 0
    assert "config" in result.stdout


def test_version_option() -> None:
    result = runner.invoke(app, ["--version"])

    assert result.exit_code == 0
    assert result.stdout.strip() == f"supportops {__version__}"


def test_config_show_masks_secrets(isolated_environment: Path) -> None:
    write_env(isolated_environment)

    result = runner.invoke(app, ["config", "show"], env=WIDE)

    assert result.exit_code == 0
    assert "SUPPORTOPS_API_URL" in result.stdout
    assert "bk_clisecret***" in result.stdout
    assert "postgresql://supportops_ro:***@localhost:5433/billing" in result.stdout
    assert FAKE_KEY not in result.output
    assert FAKE_DB_PASSWORD not in result.output


def test_config_show_json(isolated_environment: Path) -> None:
    write_env(isolated_environment)

    result = runner.invoke(app, ["config", "show", "--json"])

    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    entries = {entry["setting"]: entry for entry in payload["entries"]}
    assert payload["env_file"] == ".env"
    assert entries["SUPPORTOPS_API_KEY"] == {
        "setting": "SUPPORTOPS_API_KEY",
        "value": "bk_clisecret***",
        "source": "env file",
        "secret": True,
    }
    assert FAKE_KEY not in result.output
    assert FAKE_DB_PASSWORD not in result.output


def test_env_file_option_selects_another_file(isolated_environment: Path) -> None:
    (isolated_environment / "other.env").write_text(
        "SUPPORTOPS_API_URL=http://127.0.0.1:9000\n", encoding="utf-8"
    )

    result = runner.invoke(app, ["--env-file", "other.env", "config", "show", "--json"])

    assert result.exit_code == 0
    entries = {entry["setting"]: entry for entry in json.loads(result.stdout)["entries"]}
    assert entries["SUPPORTOPS_API_URL"]["value"] == "http://127.0.0.1:9000/"


def test_invalid_config_exits_2_without_traceback() -> None:
    result = runner.invoke(app, ["config", "show"], env={**WIDE, "SUPPORTOPS_API_URL": "not a url"})

    assert result.exit_code == 2
    assert "SUPPORTOPS_API_URL" in result.stderr
    assert "Traceback" not in result.output


def test_missing_env_file_exits_2() -> None:
    result = runner.invoke(app, ["--env-file", "missing.env", "config", "show"], env=WIDE)

    assert result.exit_code == 2
    assert "Env file not found" in result.stderr


def test_unexpected_error_is_summarised_and_redacted(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("supportops.cli.config.load_settings", fail_with_secret)

    result = runner.invoke(app, ["config", "show"], env=WIDE)

    assert result.exit_code == 3
    assert "Unexpected error (RuntimeError)" in result.stderr
    assert "--debug" in result.stderr
    assert "Traceback" not in result.output
    assert FAKE_DB_PASSWORD not in result.output


def test_debug_shows_a_redacted_traceback(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("supportops.cli.config.load_settings", fail_with_secret)

    result = runner.invoke(app, ["--debug", "config", "show"], env=WIDE)

    assert result.exit_code == 3
    assert "Traceback" in result.stderr
    assert FAKE_DB_PASSWORD not in result.output
