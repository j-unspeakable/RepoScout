from fastapi import APIRouter

from app.dependencies import SettingsDep
from app.schemas.health import HealthResponse

router = APIRouter(tags=["health"])


@router.get("/health")
async def health(settings: SettingsDep) -> HealthResponse:
    return HealthResponse(environment=settings.app_env)
