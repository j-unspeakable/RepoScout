from typing import Literal

from pydantic import BaseModel

from app.config import AppEnvironment


class HealthResponse(BaseModel):
    status: Literal["ok"] = "ok"
    environment: AppEnvironment
