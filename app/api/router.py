from fastapi import APIRouter

from app.api.routes.coins import router as coins_router
from app.api.routes.health import router as health_router


api_router = APIRouter()
api_router.include_router(health_router)
api_router.include_router(coins_router)
