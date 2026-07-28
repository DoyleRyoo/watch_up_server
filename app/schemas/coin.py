"""Public coin-search response schemas."""

from app.models.market import MarketStatus
from app.schemas.base import APIModel


class CoinSearchItem(APIModel):
    market_code: str
    korean_name: str
    english_name: str
    status: MarketStatus


__all__ = ["CoinSearchItem"]
