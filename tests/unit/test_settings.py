from pathlib import Path

import pytest

from supportops.errors import ConfigError, ExitCode
from supportops.settings import describe_settings, load_settings

FAKE_KEY = "bk_testsecret0123456789abcdefghij"
FAKE_DB_PASSWORD = "Sup3rS3cretPw"


def write_env(path: Path, content: str, encoding: str = "utf-8") -> Path:
    path.write_text(content, encoding=encoding)
    return path


def test_defaults_apply_without_env_file() -> None:
    loaded = load_settings()

    assert loaded.env_file is None
    assert loaded.settings.target == "billing"
    assert str(loaded.settings.api_url) == "http://127.0.0.1:8001/"
    assert loaded.settings.api_key is None
    assert loaded.settings.db_url is None
    assert loaded.settings.http_timeout_seconds == 5.0


def test_reads_dot_env_in_current_directory(isolated_environment: Path) -> None:
    write_env(isolated_environment / ".env", "SUPPORTOPS_API_URL=http://127.0.0.1:9000\n")

    loaded = load_settings()

    assert loaded.env_file == Path(".env")
    assert str(loaded.settings.api_url) == "http://127.0.0.1:9000/"


def test_environment_variable_wins_over_env_file(
    isolated_environment: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    write_env(isolated_environment / ".env", "SUPPORTOPS_API_URL=http://127.0.0.1:9000\n")
    monkeypatch.setenv("SUPPORTOPS_API_URL", "http://127.0.0.1:9100")

    assert str(load_settings().settings.api_url) == "http://127.0.0.1:9100/"


def test_explicit_env_file_is_used(isolated_environment: Path) -> None:
    custom = write_env(
        isolated_environment / "orderflow.env", "SUPPORTOPS_HTTP_TIMEOUT_SECONDS=12\n"
    )

    loaded = load_settings(custom)

    assert loaded.env_file == custom
    assert loaded.settings.http_timeout_seconds == 12.0


def test_empty_values_fall_back_to_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SUPPORTOPS_API_KEY", "")

    assert load_settings().settings.api_key is None


def test_missing_explicit_env_file_is_a_usage_error() -> None:
    with pytest.raises(ConfigError, match="Env file not found") as excinfo:
        load_settings(Path("missing.env"))

    assert excinfo.value.exit_code == ExitCode.USAGE


def test_invalid_url_names_the_variable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SUPPORTOPS_API_URL", "not a url")

    with pytest.raises(ConfigError, match="SUPPORTOPS_API_URL"):
        load_settings()


def test_invalid_timeout_names_the_variable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SUPPORTOPS_HTTP_TIMEOUT_SECONDS", "0")

    with pytest.raises(ConfigError, match="SUPPORTOPS_HTTP_TIMEOUT_SECONDS"):
        load_settings()


def test_invalid_db_url_never_echoes_the_value(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SUPPORTOPS_DB_URL", f"mysql://root:{FAKE_DB_PASSWORD}@db/billing")

    with pytest.raises(ConfigError) as excinfo:
        load_settings()

    assert "SUPPORTOPS_DB_URL" in excinfo.value.message
    assert FAKE_DB_PASSWORD not in excinfo.value.message


def test_utf16_env_file_is_rejected_with_a_hint(isolated_environment: Path) -> None:
    write_env(isolated_environment / ".env", "SUPPORTOPS_TARGET=billing\n", encoding="utf-16")

    with pytest.raises(ConfigError, match="UTF-16") as excinfo:
        load_settings()

    assert excinfo.value.hint is not None
    assert "PowerShell" in excinfo.value.hint


def test_utf8_env_file_with_bom_is_accepted(isolated_environment: Path) -> None:
    write_env(
        isolated_environment / ".env", "SUPPORTOPS_HTTP_TIMEOUT_SECONDS=7\n", encoding="utf-8-sig"
    )

    assert load_settings().settings.http_timeout_seconds == 7.0


def test_describe_reports_sources_and_masks_secrets(
    isolated_environment: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    write_env(
        isolated_environment / ".env",
        f"SUPPORTOPS_API_KEY={FAKE_KEY}\n"
        f"SUPPORTOPS_DB_URL=postgresql://supportops_ro:{FAKE_DB_PASSWORD}@localhost:5433/billing\n",
    )
    monkeypatch.setenv("SUPPORTOPS_HTTP_TIMEOUT_SECONDS", "9")

    report = describe_settings(load_settings())
    entries = {entry.setting: entry for entry in report.entries}

    assert report.env_file == ".env"
    assert entries["SUPPORTOPS_TARGET"].source == "default"
    assert entries["SUPPORTOPS_API_KEY"].source == "env file"
    assert entries["SUPPORTOPS_HTTP_TIMEOUT_SECONDS"].source == "environment variable"
    assert entries["SUPPORTOPS_API_KEY"].value == "bk_testsecre***"
    assert entries["SUPPORTOPS_API_KEY"].secret
    assert entries["SUPPORTOPS_DB_URL"].value == (
        "postgresql://supportops_ro:***@localhost:5433/billing"
    )
    assert not entries["SUPPORTOPS_API_URL"].secret
    assert FAKE_KEY not in report.model_dump_json()
    assert FAKE_DB_PASSWORD not in report.model_dump_json()


def test_describe_marks_unset_secrets() -> None:
    entries = {entry.setting: entry for entry in describe_settings(load_settings()).entries}

    assert entries["SUPPORTOPS_API_KEY"].value == "(not set)"
    assert entries["SUPPORTOPS_DB_URL"].value == "(not set)"
