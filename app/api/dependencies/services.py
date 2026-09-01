"""Dependencies for lifespan-owned business services."""

from fastapi import Request

from app.services.chart import ChartService
from app.services.market_list import MarketListService
from app.services.paper_account import PaperAccountService
from app.services.paper_history import PaperHistoryService
from app.services.paper_portfolio import PaperPortfolioService
from app.services.paper_trade import PaperTradeService
from app.services.price import PriceService


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


def get_paper_account_service(request: Request) -> PaperAccountService:
    service: PaperAccountService | None = request.app.state.paper_account_service
    if service is None:
        raise RuntimeError("Paper account service is not initialized")
    return service


def get_paper_history_service(request: Request) -> PaperHistoryService:
    service: PaperHistoryService | None = request.app.state.paper_history_service
    if service is None:
        raise RuntimeError("Paper history service is not initialized")
    return service


def get_paper_portfolio_service(request: Request) -> PaperPortfolioService:
    service: PaperPortfolioService | None = request.app.state.paper_portfolio_service
    if service is None:
        raise RuntimeError("Paper portfolio service is not initialized")
    return service


def get_paper_trade_service(request: Request) -> PaperTradeService:
    service: PaperTradeService | None = request.app.state.paper_trade_service
    if service is None:
        raise RuntimeError("Paper trade service is not initialized")
    return service


__all__ = [
    "get_chart_service",
    "get_market_list_service",
    "get_paper_history_service",
    "get_paper_account_service",
    "get_paper_portfolio_service",
    "get_paper_trade_service",
    "get_price_service",
]
