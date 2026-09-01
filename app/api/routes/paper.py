from typing import Annotated

from fastapi import APIRouter, Depends, Header, Query, Response, status

from app.api.dependencies.auth import get_auth_context
from app.api.dependencies.services import get_paper_account_service
from app.api.dependencies.services import get_paper_history_service
from app.api.dependencies.services import get_paper_portfolio_service
from app.api.dependencies.services import get_paper_trade_service
from app.models.auth import AuthContext
from app.schemas.common import SuccessResponse
from app.schemas.paper import (
    BIGINT_MAX,
    HistoryMeta,
    PaperAccount,
    PaperHistoryResponse,
    PaperPortfolioResponse,
    PaperTransaction,
    PortfolioMeta,
    TopUpRequest,
    TradeRequest,
)
from app.services.idempotency import parse_idempotency_key
from app.services.paper_account import PaperAccountService
from app.services.paper_history import PaperHistoryService
from app.services.paper_portfolio import PaperPortfolioService
from app.services.paper_trade import PaperTradeService

router = APIRouter(prefix="/paper", tags=["paper"])


@router.get("/account", response_model=SuccessResponse[PaperAccount])
async def get_account(
    auth: Annotated[AuthContext, Depends(get_auth_context)],
    service: Annotated[PaperAccountService, Depends(get_paper_account_service)],
) -> SuccessResponse[PaperAccount]:
    return SuccessResponse(data=await service.get_account(auth.user_id))


@router.get("/portfolio", response_model=PaperPortfolioResponse)
async def get_portfolio(
    auth: Annotated[AuthContext, Depends(get_auth_context)],
    service: Annotated[PaperPortfolioService, Depends(get_paper_portfolio_service)],
) -> PaperPortfolioResponse:
    portfolio = await service.get_portfolio(auth.user_id)
    return PaperPortfolioResponse(
        data=portfolio, meta=PortfolioMeta(count=len(portfolio.holdings))
    )


@router.post(
    "/top-ups",
    response_model=SuccessResponse[PaperTransaction],
    status_code=status.HTTP_201_CREATED,
)
async def top_up(
    body: TopUpRequest,
    response: Response,
    auth: Annotated[AuthContext, Depends(get_auth_context)],
    service: Annotated[PaperAccountService, Depends(get_paper_account_service)],
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
) -> SuccessResponse[PaperTransaction]:
    result = await service.top_up(
        auth.user_id, body.amount_krw, parse_idempotency_key(idempotency_key)
    )
    if result.replayed:
        response.status_code = status.HTTP_200_OK
    return SuccessResponse(data=result.transaction)


@router.get("/trades", response_model=PaperHistoryResponse)
async def get_trades(
    auth: Annotated[AuthContext, Depends(get_auth_context)],
    service: Annotated[PaperHistoryService, Depends(get_paper_history_service)],
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
    before_id: Annotated[
        int | None, Query(alias="beforeId", ge=1, le=BIGINT_MAX)
    ] = None,
) -> PaperHistoryResponse:
    page = await service.get_trades(auth.user_id, limit=limit, before_id=before_id)
    items = list(page.transactions)
    return PaperHistoryResponse(
        data=items, meta=HistoryMeta(count=len(items), has_more=page.has_more)
    )


@router.post(
    "/trades",
    response_model=SuccessResponse[PaperTransaction],
    status_code=status.HTTP_201_CREATED,
)
async def trade(
    body: TradeRequest,
    response: Response,
    auth: Annotated[AuthContext, Depends(get_auth_context)],
    service: Annotated[PaperTradeService, Depends(get_paper_trade_service)],
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
) -> SuccessResponse[PaperTransaction]:
    result = await service.trade(
        auth.user_id, body, parse_idempotency_key(idempotency_key)
    )
    if result.replayed:
        response.status_code = status.HTTP_200_OK
    return SuccessResponse(data=result.transaction)
