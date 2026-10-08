import os
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from pydantic import AnyHttpUrl, BaseModel, Field, SecretStr, ValidationError, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from supportops.errors import ConfigError
from supportops.redaction import mask_api_key, redact_dsn

ENV_PREFIX = "SUPPORTOPS_"
DEFAULT_ENV_FILE = Path(".env")
_UTF16_BOMS = (b"\xff\xfe", b"\xfe\xff")
_SECRET_FIELDS = frozenset({"api_key", "db_url"})

ConfigSource = Literal["environment variable", "env file", "default"]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix=ENV_PREFIX,
        env_file_encoding="utf-8-sig",
        env_ignore_empty=True,
        extra="ignore",
    )

    target: Literal["billing"] = "billing"
    api_url: AnyHttpUrl = AnyHttpUrl("http://localhost:8001")
    api_key: SecretStr | None = None
    db_url: SecretStr | None = None
    http_timeout_seconds: float = Field(default=5.0, gt=0, le=300)

    @field_validator("db_url")
    @classmethod
    def _require_postgres_url(cls, value: SecretStr | None) -> SecretStr | None:
        if value is not None and not value.get_secret_value().startswith(
            ("postgresql://", "postgres://")
        ):
            raise ValueError(
                "must be a PostgreSQL URL such as postgresql://user:password@host:5432/dbname"
            )
        return value


@dataclass(frozen=True)
class LoadedSettings:
    settings: Settings
    env_file: Path | None


class ConfigEntry(BaseModel):
    setting: str
    value: str
    source: ConfigSource
    secret: bool


class ConfigReport(BaseModel):
    env_file: str | None
    entries: list[ConfigEntry]


def load_settings(env_file: Path | None = None) -> LoadedSettings:
    if env_file is not None and not env_file.is_file():
        raise ConfigError(
            f"Env file not found: {env_file}", hint="Check the path given to --env-file."
        )
    path = env_file
    if path is None and DEFAULT_ENV_FILE.is_file():
        path = DEFAULT_ENV_FILE
    if path is not None:
        _reject_utf16(path)
    try:
        settings = Settings(_env_file=path)
    except ValidationError as exc:
        raise ConfigError(
            _summarize(exc),
            hint="Fix the value in the environment or the env file, "
            "then run 'supportops config show'.",
        ) from None
    return LoadedSettings(settings=settings, env_file=path)


def describe_settings(loaded: LoadedSettings) -> ConfigReport:
    environment = {name.upper() for name, value in os.environ.items() if value}
    entries = []
    for name in Settings.model_fields:
        variable = f"{ENV_PREFIX}{name.upper()}"
        source: ConfigSource = "default"
        if variable in environment:
            source = "environment variable"
        elif name in loaded.settings.model_fields_set:
            source = "env file"
        entries.append(
            ConfigEntry(
                setting=variable,
                value=_display(name, getattr(loaded.settings, name)),
                source=source,
                secret=name in _SECRET_FIELDS,
            )
        )
    env_file = str(loaded.env_file) if loaded.env_file is not None else None
    return ConfigReport(env_file=env_file, entries=entries)


def _display(name: str, value: object) -> str:
    if value is None:
        return "(not set)"
    if isinstance(value, SecretStr):
        raw = value.get_secret_value()
        return redact_dsn(raw) if name == "db_url" else mask_api_key(raw)
    return str(value)


def _reject_utf16(path: Path) -> None:
    with path.open("rb") as handle:
        start = handle.read(2)
    if start in _UTF16_BOMS:
        raise ConfigError(
            f"Env file {path} is UTF-16 encoded.",
            hint="Save it as UTF-8. Windows PowerShell 5.1 writes UTF-16 "
            "with '>' and Out-File by default.",
        )


def _summarize(exc: ValidationError) -> str:
    problems = []
    for error in exc.errors(include_url=False, include_input=False):
        field = str(error["loc"][0]) if error["loc"] else "settings"
        problems.append(f"{ENV_PREFIX}{field.upper()}: {error['msg']}")
    return "Invalid configuration. " + "; ".join(problems)
