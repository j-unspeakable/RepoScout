import os
from enum import StrEnum
from functools import lru_cache

from pydantic import Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class AppEnvironment(StrEnum):
    LOCAL = "local"
    TEST = "test"
    DATABRICKS = "databricks"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        case_sensitive=False,
        env_file=None,
        extra="ignore",
    )

    app_env: AppEnvironment
    databricks_config_profile: str | None = None
    github_token: SecretStr | None = None
    github_api_url: str = "https://api.github.com"
    github_api_version: str = "2026-03-10"
    github_timeout_seconds: float = Field(default=30.0, gt=0)
    github_readme_concurrency: int = Field(default=8, ge=1, le=20)
    github_retry_attempts: int = Field(default=2, ge=0, le=5)

    pghost: str | None = None
    pgport: int = Field(default=5432, ge=1, le=65535)
    pgdatabase: str | None = None
    pguser: str | None = None
    pgsslmode: str = "require"
    lakebase_endpoint: str | None = None

    db_pool_min_size: int = Field(default=1, ge=0, le=20)
    db_pool_max_size: int = Field(default=5, ge=1, le=50)
    db_pool_max_lifetime_seconds: float = Field(default=3300.0, gt=0, le=3540)
    db_pool_timeout_seconds: float = Field(default=30.0, gt=0)

    search_min_similarity: float = Field(default=0.25, ge=-1.0, le=1.0)
    llm_api_base_url: str = "https://openrouter.ai/api/v1"
    llm_api_key: SecretStr | None = None
    llm_model_name: str = Field(default="openrouter/free", min_length=1)
    llm_request_timeout: float = Field(default=45.0, gt=0)
    llm_max_output_tokens: int = Field(default=2000, ge=600, le=8192)

    @model_validator(mode="after")
    def validate_environment_configuration(self) -> "Settings":
        if self.db_pool_min_size > self.db_pool_max_size:
            raise ValueError("DB_POOL_MIN_SIZE cannot exceed DB_POOL_MAX_SIZE")

        if self.app_env is AppEnvironment.TEST:
            return self

        required = {
            "GITHUB_TOKEN": self.github_token,
            "PGHOST": self.pghost,
            "PGDATABASE": self.pgdatabase,
            "PGUSER": self.pguser,
            "LAKEBASE_ENDPOINT": self.lakebase_endpoint,
        }
        missing = [name for name, value in required.items() if value is None or value == ""]
        if missing:
            names = ", ".join(sorted(missing))
            raise ValueError(f"Missing required configuration for APP_ENV={self.app_env}: {names}")
        return self


def _environment_from_process() -> AppEnvironment:
    value = os.getenv("APP_ENV")
    if value is None:
        raise ValueError("APP_ENV is required and must be one of: local, test, databricks")
    try:
        return AppEnvironment(value.lower())
    except ValueError as exc:
        raise ValueError(
            f"Unsupported APP_ENV={value!r}; expected one of: local, test, databricks"
        ) from exc


@lru_cache
def get_settings() -> Settings:
    environment = _environment_from_process()
    env_file = ".env" if environment is AppEnvironment.LOCAL else None
    return Settings(_env_file=env_file)
