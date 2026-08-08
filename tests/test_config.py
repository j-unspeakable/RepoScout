from pathlib import Path

import pytest
from pydantic import ValidationError

from app.config import AppEnvironment, Settings, get_settings

DATABASE_ENVIRONMENT_VARIABLES = (
    "GITHUB_TOKEN",
    "PGHOST",
    "PGDATABASE",
    "PGUSER",
    "LAKEBASE_ENDPOINT",
)


def _clear_settings_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in DATABASE_ENVIRONMENT_VARIABLES:
        monkeypatch.delenv(name, raising=False)
    get_settings.cache_clear()


def test_test_environment_needs_no_external_configuration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _clear_settings_environment(monkeypatch)
    monkeypatch.setenv("APP_ENV", "test")

    settings = get_settings()

    assert settings.app_env is AppEnvironment.TEST
    assert settings.github_token is None
    assert settings.pghost is None


def test_local_environment_loads_optional_dotenv(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _clear_settings_environment(monkeypatch)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("APP_ENV", "local")
    (tmp_path / ".env").write_text(
        "\n".join(
            (
                "GITHUB_TOKEN=local-token",
                "DATABRICKS_CONFIG_PROFILE=reposcout",
                "PGHOST=lakebase.example",
                "PGDATABASE=reposcout",
                "PGUSER=developer@example.com",
                "LAKEBASE_ENDPOINT=projects/p/branches/b/endpoints/e",
            )
        )
    )

    settings = get_settings()

    assert settings.github_token is not None
    assert settings.github_token.get_secret_value() == "local-token"
    assert settings.databricks_config_profile == "reposcout"
    assert settings.pghost == "lakebase.example"


def test_test_environment_ignores_dotenv(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _clear_settings_environment(monkeypatch)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("APP_ENV", "test")
    (tmp_path / ".env").write_text("GITHUB_TOKEN=must-not-load\nPGHOST=must-not-load\n")

    settings = get_settings()

    assert settings.github_token is None
    assert settings.pghost is None


def test_unsupported_and_missing_environment_fail_clearly(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _clear_settings_environment(monkeypatch)
    monkeypatch.setenv("APP_ENV", "production")
    with pytest.raises(ValueError, match="Unsupported APP_ENV"):
        get_settings()

    get_settings.cache_clear()
    monkeypatch.delenv("APP_ENV")
    with pytest.raises(ValueError, match="APP_ENV is required"):
        get_settings()


def test_non_test_environment_requires_external_configuration() -> None:
    with pytest.raises(ValidationError, match="Missing required configuration"):
        Settings(app_env=AppEnvironment.DATABRICKS)


def test_pool_size_validation() -> None:
    with pytest.raises(ValidationError, match="DB_POOL_MIN_SIZE"):
        Settings(app_env=AppEnvironment.TEST, db_pool_min_size=5, db_pool_max_size=2)
