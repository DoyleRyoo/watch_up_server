from typing import Any

from fastapi import APIRouter


router = APIRouter(tags=["health"])


@router.get("/health")
async def health_check() -> dict[str, Any]:
    """Report whether the application process can serve requests."""
    return {"data": {"status": "ok"}, "meta": None}
