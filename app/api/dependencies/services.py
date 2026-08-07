"""Dependencies for lifespan-owned business services."""

from fastapi import Request

from app.services.chart import ChartService
from app.services.market_list import MarketListService
from app.services.price import PriceService
from app.services.watchlist import WatchlistService


def get_chart_service(request: Request) -> ChartService:
    service: ChartService | None = request.app.state.chart_service
    if service is None:
        raise RuntimeError("Chart service is not initialized")
    return service


def get_market_list_service(request: Request) -> MarketListService:
    service: MarketListService | None = request.app.state.market_list_service
    if service is None:
        raise RuntimeError("Market list service is not initialized")
    return service


def get_price_service(request: Request) -> PriceService:
    service: PriceService | None = request.app.state.price_service
    if service is None:
        raise RuntimeError("Price service is not initialized")
    return service


def get_watchlist_service() -> WatchlistService:
    return WatchlistService()


__all__ = [
    "get_chart_service",
    "get_market_list_service",
    "get_price_service",
    "get_watchlist_service",
]
