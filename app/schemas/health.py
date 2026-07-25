from typing import Literal

from app.schemas.base import APIModel


class HealthData(APIModel):
    status: Literal["ok"] = "ok"
