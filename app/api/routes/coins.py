"""Authenticated coin search and daily chart routes."""

from typing import Annotated

from fastapi import APIRouter, Depends, Path, Query

from app.api.dependencies.auth import get_auth_context
from app.api.dependencies.services import get_chart_service, get_market_list_service
from app.models.auth import AuthContext
from app.schemas.chart import (
    ChartCandleResponse,
    ChartDataResponse,
    ChartResponse,
)
from app.schemas.coin import CoinSearchItem
from app.schemas.common import ListMeta, ListResponse
from app.services.chart import ChartService
from app.services.market_list import MarketListService


router = APIRouter(prefix="/coins", tags=["coins"])


@router.get("/search", response_model=ListResponse[CoinSearchItem])
async def search_coins(
    query: Annotated[str, Query()],
    auth_context: Annotated[AuthContext, Depends(get_auth_context)],
    service: Annotated[MarketListService, Depends(get_market_list_service)],
) -> ListResponse[CoinSearchItem]:
    del auth_context
    markets = await service.search(query)
    items = [
        CoinSearchItem(
            market_code=market.market_code,
            korean_name=market.korean_name,
            english_name=market.english_name,
            status=market.status,
        )
        for market in markets
    ]
    return ListResponse(data=items, meta=ListMeta(count=len(items)))


@router.get("/{marketCode}/chart", response_model=ChartResponse)
async def get_coin_chart(
    market_code: Annotated[str, Path(alias="marketCode")],
    auth_context: Annotated[AuthContext, Depends(get_auth_context)],
    service: Annotated[ChartService, Depends(get_chart_service)],
) -> ChartResponse:
    del auth_context
    snapshot = await service.get_chart(market_code)
    candles = [
        ChartCandleResponse(
            date=candle.date,
            closing_price=candle.closing_price,
        )
        for candle in snapshot.candles
    ]
    return ChartResponse(
        data=ChartDataResponse(
            market_code=snapshot.market_code,
            period=snapshot.period,
            candles=candles,
        ),
        meta=ListMeta(count=len(candles)),
    )


__all__ = ["router"]
