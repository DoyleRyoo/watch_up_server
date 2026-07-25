from fastapi import APIRouter

from app.schemas.common import SuccessResponse
from app.schemas.health import HealthData


router = APIRouter(tags=["health"])


@router.get("/health", response_model=SuccessResponse[HealthData])
async def health_check() -> SuccessResponse[HealthData]:
    """Report whether the application process can serve requests."""
    return SuccessResponse(data=HealthData())
