"""Authenticated watchlist registration and current-price list routes."""

from json import JSONDecodeError
from typing import Annotated, TypeAlias

from fastapi import APIRouter, Depends, Request, status
from fastapi.exceptions import RequestValidationError
from pydantic import ValidationError
from supabase import Client

from app.api.dependencies.auth import get_auth_context, get_supabase_client
from app.api.dependencies.services import (
    get_market_list_service,
    get_price_service,
    get_watchlist_service,
)
from app.models.auth import AuthContext
from app.models.watchlist import WatchlistItem
from app.schemas.common import ListMeta, ListResponse, SuccessResponse
from app.schemas.watchlist import (
    WatchlistCreateRequest,
    WatchlistCreatedItem,
    WatchlistItemResponse,
)
from app.services.market_list import MarketListService
from app.services.price import PriceService
from app.services.watchlist import WatchlistService


router = APIRouter(prefix="/watchlist", tags=["watchlist"])
AuthenticatedWatchlistCreate: TypeAlias = tuple[
    AuthContext,
    WatchlistCreateRequest,
]


async def get_authenticated_watchlist_create(
    request: Request,
    auth_context: Annotated[AuthContext, Depends(get_auth_context)],
) -> AuthenticatedWatchlistCreate:
    try:
        raw_payload = await request.json()
    except (JSONDecodeError, UnicodeDecodeError):
        raise RequestValidationError([]) from None
    try:
        payload = WatchlistCreateRequest.model_validate(raw_payload)
    except ValidationError as exc:
        raise RequestValidationError(exc.errors(), body=raw_payload) from None
    return auth_context, payload


@router.get("", response_model=ListResponse[WatchlistItemResponse])
async def get_watchlist(
    auth_context: Annotated[AuthContext, Depends(get_auth_context)],
    supabase_client: Annotated[Client, Depends(get_supabase_client)],
    market_list_service: Annotated[
        MarketListService,
        Depends(get_market_list_service),
    ],
    price_service: Annotated[PriceService, Depends(get_price_service)],
    watchlist_service: Annotated[
        WatchlistService,
        Depends(get_watchlist_service),
    ],
) -> ListResponse[WatchlistItemResponse]:
    items = await watchlist_service.get_items_for_user(
        client=supabase_client,
        user_id=auth_context.user_id,
        market_list_service=market_list_service,
        price_service=price_service,
    )
    response_items = [_to_response_item(item) for item in items]
    return ListResponse(
        data=response_items,
        meta=ListMeta(count=len(response_items)),
    )


@router.post(
    "",
    status_code=status.HTTP_201_CREATED,
    response_model=SuccessResponse[WatchlistCreatedItem],
    openapi_extra={
        "requestBody": {
            "required": True,
            "content": {
                "application/json": {
                    "schema": WatchlistCreateRequest.model_json_schema(by_alias=True)
                }
            },
        }
    },
)
async def create_watchlist_item(
    registration: Annotated[
        AuthenticatedWatchlistCreate,
        Depends(get_authenticated_watchlist_create),
    ],
    supabase_client: Annotated[Client, Depends(get_supabase_client)],
    market_list_service: Annotated[
        MarketListService,
        Depends(get_market_list_service),
    ],
    watchlist_service: Annotated[
        WatchlistService,
        Depends(get_watchlist_service),
    ],
) -> SuccessResponse[WatchlistCreatedItem]:
    auth_context, payload = registration
    row = await watchlist_service.register_for_user(
        client=supabase_client,
        user_id=auth_context.user_id,
        market_code=payload.market_code,
        market_list_service=market_list_service,
    )
    return SuccessResponse(
        data=WatchlistCreatedItem(
            id=row.id,
            market_code=row.market_code,
            korean_name=row.korean_name,
            english_name=row.english_name,
            created_at=row.created_at,
        ),
        meta=None,
    )


def _to_response_item(item: WatchlistItem) -> WatchlistItemResponse:
    return WatchlistItemResponse(
        id=item.id,
        market_code=item.market_code,
        korean_name=item.korean_name,
        english_name=item.english_name,
        symbol=item.symbol,
        current_price=item.current_price,
        signed_change_rate=item.signed_change_rate,
        status=item.status,
        is_stale=item.is_stale,
        created_at=item.created_at,
    )


__all__ = ["router"]
