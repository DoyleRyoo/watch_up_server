"""Business-facing watchlist persistence and read orchestration service."""

import re
from decimal import Decimal
from typing import Final
from uuid import UUID

from supabase import Client

from app.core.errors import AppError, ErrorCode
from app.models.watchlist import (
    MARKET_CODE_PATTERN as MARKET_CODE_PATTERN_TEXT,
    WatchlistInsert,
    WatchlistItem,
    WatchlistRow,
    WatchlistStatus,
)
from app.repositories.watchlist import (
    WatchlistDuplicateError,
    WatchlistNotFoundError,
    WatchlistRepository,
    WatchlistRepositoryError,
)
from app.services.market_list import MarketListService
from app.services.price import PriceService


MAX_WATCHLIST_ITEMS: Final = 50
MARKET_CODE_MAX_LENGTH: Final = 20
MARKET_CODE_PATTERN: Final = re.compile(MARKET_CODE_PATTERN_TEXT)
CHANGE_RATE_PERCENT_MULTIPLIER: Final = Decimal("100")


class WatchlistService:
    """Keeps Supabase details and watchlist business rules behind one boundary."""

    def __init__(self, repository: WatchlistRepository | None = None) -> None:
        self._repository = repository or WatchlistRepository()

    def list_for_user(
        self,
        *,
        client: Client,
        user_id: UUID,
    ) -> list[WatchlistRow]:
        try:
            return self._repository.list_by_user(
                client=client,
                user_id=user_id,
            )
        except WatchlistRepositoryError:
            raise _internal_server_error() from None

    async def get_items_for_user(
        self,
        *,
        client: Client,
        user_id: UUID,
        market_list_service: MarketListService,
        price_service: PriceService,
    ) -> list[WatchlistItem]:
        rows = self.list_for_user(client=client, user_id=user_id)
        if not rows:
            return []

        markets = await market_list_service.get_markets()
        market_by_code = {market.market_code: market for market in markets}
        price_market_codes = tuple(
            dict.fromkeys(
                row.market_code for row in rows if row.market_code in market_by_code
            )
        )
        prices = (
            await price_service.get_prices(price_market_codes)
            if price_market_codes
            else {}
        )

        items: list[WatchlistItem] = []
        for row in rows:
            market = market_by_code.get(row.market_code)
            if market is None:
                items.append(
                    _without_price(row=row, status=WatchlistStatus.UNAVAILABLE)
                )
                continue

            resolved_price = prices.get(row.market_code)
            if resolved_price is None:
                items.append(
                    _without_price(row=row, status=WatchlistStatus.PRICE_ERROR)
                )
                continue

            items.append(
                WatchlistItem(
                    id=row.id,
                    market_code=row.market_code,
                    korean_name=row.korean_name,
                    english_name=row.english_name,
                    symbol=_symbol_from_market_code(row.market_code),
                    current_price=resolved_price.quote.trade_price,
                    signed_change_rate=(
                        resolved_price.quote.signed_change_rate
                        * CHANGE_RATE_PERCENT_MULTIPLIER
                    ),
                    status=WatchlistStatus(market.status.value),
                    is_stale=resolved_price.is_stale,
                    created_at=row.created_at,
                )
            )
        return items

    def count_for_user(
        self,
        *,
        client: Client,
        user_id: UUID,
    ) -> int:
        try:
            return self._repository.count_by_user(
                client=client,
                user_id=user_id,
            )
        except WatchlistRepositoryError:
            raise _internal_server_error() from None

    def is_registered(
        self,
        *,
        client: Client,
        user_id: UUID,
        market_code: str,
    ) -> bool:
        try:
            return self._repository.exists_by_user_and_market(
                client=client,
                user_id=user_id,
                market_code=market_code,
            )
        except WatchlistRepositoryError:
            raise _internal_server_error() from None

    def add_for_user(
        self,
        *,
        client: Client,
        user_id: UUID,
        market_code: str,
        korean_name: str,
        english_name: str,
    ) -> WatchlistRow:
        values = WatchlistInsert(
            user_id=user_id,
            market_code=market_code,
            korean_name=korean_name,
            english_name=english_name,
        )
        try:
            return self._repository.insert(client=client, values=values)
        except WatchlistDuplicateError:
            raise AppError(
                code=ErrorCode.WATCHLIST_DUPLICATED,
                message="이미 등록된 코인입니다.",
            ) from None
        except WatchlistRepositoryError:
            raise _internal_server_error() from None

    async def register_for_user(
        self,
        *,
        client: Client,
        user_id: UUID,
        market_code: str,
        market_list_service: MarketListService,
    ) -> WatchlistRow:
        _validate_market_code(market_code)

        market = await market_list_service.get_market_by_code(market_code)
        if market is None:
            raise _invalid_market_code()

        current_count = self.count_for_user(client=client, user_id=user_id)
        if current_count >= MAX_WATCHLIST_ITEMS:
            raise AppError(
                code=ErrorCode.WATCHLIST_LIMIT_EXCEEDED,
                message="관심 코인은 최대 50개까지 등록할 수 있습니다.",
            )

        if self.is_registered(
            client=client,
            user_id=user_id,
            market_code=market.market_code,
        ):
            raise AppError(
                code=ErrorCode.WATCHLIST_DUPLICATED,
                message="이미 등록된 코인입니다.",
            )

        return self.add_for_user(
            client=client,
            user_id=user_id,
            market_code=market.market_code,
            korean_name=market.korean_name,
            english_name=market.english_name,
        )

    def delete_for_user(
        self,
        *,
        client: Client,
        user_id: UUID,
        watchlist_id: int,
    ) -> int:
        try:
            return self._repository.delete_by_user_and_id(
                client=client,
                user_id=user_id,
                watchlist_id=watchlist_id,
            )
        except WatchlistNotFoundError:
            raise AppError(
                code=ErrorCode.WATCHLIST_NOT_FOUND,
                message="관심 코인을 찾을 수 없습니다.",
            ) from None
        except WatchlistRepositoryError:
            raise _internal_server_error() from None


def _without_price(
    *,
    row: WatchlistRow,
    status: WatchlistStatus,
) -> WatchlistItem:
    return WatchlistItem(
        id=row.id,
        market_code=row.market_code,
        korean_name=row.korean_name,
        english_name=row.english_name,
        symbol=_symbol_from_market_code(row.market_code),
        current_price=None,
        signed_change_rate=None,
        status=status,
        is_stale=False,
        created_at=row.created_at,
    )


def _symbol_from_market_code(market_code: str) -> str:
    return market_code.removeprefix("KRW-")


def _internal_server_error() -> AppError:
    return AppError(
        code=ErrorCode.INTERNAL_SERVER_ERROR,
        message="서버 내부 오류가 발생했습니다.",
    )


def _validate_market_code(market_code: str) -> None:
    if (
        len(market_code) > MARKET_CODE_MAX_LENGTH
        or MARKET_CODE_PATTERN.fullmatch(market_code) is None
    ):
        raise _invalid_market_code()


def _invalid_market_code() -> AppError:
    return AppError(
        code=ErrorCode.INVALID_MARKET_CODE,
        message="유효하지 않은 마켓 코드입니다.",
    )


__all__ = [
    "MARKET_CODE_MAX_LENGTH",
    "MAX_WATCHLIST_ITEMS",
    "WatchlistService",
]
