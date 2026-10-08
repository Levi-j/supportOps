from typing import Annotated, Literal, Self

from pydantic import Field, SecretStr, ValidationError, field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

from billing_api.faults import Fault


class ConfigurationError(RuntimeError):
    pass


class BillingSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="BILLING_", extra="ignore")

    database_url: SecretStr
    env: Literal["lab", "production"] = "production"
    faults: Annotated[frozenset[Fault], NoDecode] = frozenset()
    db_connect_timeout_seconds: int = Field(default=3, ge=1, le=30)
    db_statement_timeout_ms: int = Field(default=10_000, gt=0)
    db_lock_timeout_ms: int = Field(default=3_000, gt=0)
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"

    @field_validator("database_url")
    @classmethod
    def _require_postgres_url(cls, value: SecretStr) -> SecretStr:
        if not value.get_secret_value().startswith(("postgresql://", "postgres://")):
            raise ValueError("must be a PostgreSQL URL")
        return value

    @field_validator("faults", mode="before")
    @classmethod
    def _split_fault_list(cls, value: object) -> object:
        if isinstance(value, str):
            return frozenset(name.strip() for name in value.split(",") if name.strip())
        return value

    @model_validator(mode="after")
    def _allow_faults_only_in_the_lab(self) -> Self:
        if self.faults and self.env != "lab":
            raise ValueError("BILLING_FAULTS can only be enabled when BILLING_ENV=lab")
        return self


def load_settings() -> BillingSettings:
    try:
        return BillingSettings()
    except ValidationError as exc:
        raise ConfigurationError(describe_validation_error(exc)) from None


def describe_validation_error(exc: ValidationError) -> str:
    problems = []
    for error in exc.errors(include_url=False, include_input=False, include_context=False):
        if error["loc"]:
            problems.append(f"BILLING_{str(error['loc'][0]).upper()}: {error['msg']}")
        else:
            problems.append(error["msg"])
    return "Invalid billing-api configuration. " + "; ".join(problems)
