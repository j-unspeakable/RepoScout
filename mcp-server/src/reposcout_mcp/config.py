from functools import lru_cache

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class McpSettings(BaseSettings):
    model_config = SettingsConfigDict(case_sensitive=False, extra="ignore")

    reposcout_api_app_url: str | None = None
    reposcout_app_name: str | None = None
    reposcout_api_timeout_seconds: float = Field(default=45.0, gt=0, le=120)
    mcp_port: int = Field(default=8001, ge=1, le=65535)

    @model_validator(mode="after")
    def validate_target(self) -> "McpSettings":
        if self.reposcout_api_app_url:
            url = self.reposcout_api_app_url.rstrip("/")
            if not url.startswith(("http://", "https://")):
                raise ValueError("REPOSCOUT_API_APP_URL must be an HTTP(S) URL")
            self.reposcout_api_app_url = url
            return self
        if not self.reposcout_app_name:
            raise ValueError("Configure REPOSCOUT_API_APP_URL or REPOSCOUT_APP_NAME")
        self.reposcout_app_name = self.reposcout_app_name.strip()
        if not self.reposcout_app_name:
            raise ValueError("REPOSCOUT_APP_NAME must not be blank")
        return self


@lru_cache
def get_settings() -> McpSettings:
    return McpSettings()
