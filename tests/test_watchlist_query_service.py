from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import UUID, uuid4

import pytest

from app.core.errors import AppError, ErrorCode
from app.models.market import Market, MarketStatus
from app.models.price import PriceQuote, ResolvedPrice
from app.models.watchlist import WatchlistRow, WatchlistStatus
from app.repositories.watchlist import WatchlistRepositoryError
from app.services.watchlist import WatchlistService


USER_ID = uuid4()
OTHER_USER_ID = uuid4()
CLIENT = object()
BASE_TIME = datetime(2026, 7, 15, 12, tzinfo=UTC)


def row(
    row_id: int,
    market_code: str,
    *,
    korean_name: str,
    english_name: str,
    created_at: datetime | None = None,
) -> WatchlistRow:
    return WatchlistRow(
        id=row_id,
        user_id=USER_ID,
        market_code=market_code,
        korean_name=korean_name,
        english_name=english_name,
        created_at=created_at or BASE_TIME,
    )


def market(
    market_code: str,
    status: MarketStatus = MarketStatus.ACTIVE,
) -> Market:
    return Market(
        market_code=market_code,
        korean_name="최신 한글명",
        english_name="Latest English Name",
        status=status,
    )


def price(
    market_code: str,
    trade_price: int | float,
    change_rate: float,
    *,
    stale: bool = False,
) -> ResolvedPrice:
    return ResolvedPrice(
        quote=PriceQuote(
            market_code=market_code,
            trade_price=trade_price,
            signed_change_rate=change_rate,
        ),
        is_stale=stale,
    )


class FakeListRepository:
    def __init__(
        self,
        rows: list[WatchlistRow],
        *,
        error: Exception | None = None,
    ) -> None:
        self.rows = rows
        self.error = error
        self.calls: list[dict[str, object]] = []

    def list_by_user(self, **arguments: object) -> list[WatchlistRow]:
        self.calls.append(arguments)
        if self.error is not None:
            raise self.error
        return list(self.rows)


class FakeMarketListService:
    def __init__(
        self,
        markets: tuple[Market, ...] = (),
        *,
        error: BaseException | None = None,
    ) -> None:
        self.markets = markets
        self.error = error
        self.calls = 0

    async def get_markets(self) -> tuple[Market, ...]:
        self.calls += 1
        if self.error is not None:
            raise self.error
        return self.markets


class FakePriceService:
    def __init__(
        self,
        prices: dict[str, ResolvedPrice] | None = None,
        *,
        error: BaseException | None = None,
    ) -> None:
        self.prices = prices or {}
        self.error = error
        self.calls: list[tuple[str, ...]] = []

    async def get_prices(
        self,
        market_codes: tuple[str, ...],
    ) -> dict[str, ResolvedPrice]:
        self.calls.append(tuple(market_codes))
        if self.error is not None:
            raise self.error
        return dict(self.prices)


async def get_items(
    repository: FakeListRepository,
    market_service: FakeMarketListService,
    price_service: FakePriceService,
    *,
    user_id: UUID = USER_ID,
):
    service = WatchlistService(repository=repository)  # type: ignore[arg-type]
    return await service.get_items_for_user(
        client=CLIENT,  # type: ignore[arg-type]
        user_id=user_id,
        market_list_service=market_service,  # type: ignore[arg-type]
        price_service=price_service,  # type: ignore[arg-type]
    )


@pytest.mark.asyncio
async def test_empty_db_list_is_fast_path_before_market_or_price() -> None:
    repository = FakeListRepository([])
    market_service = FakeMarketListService(error=AssertionError("must not call"))
    price_service = FakePriceService(error=AssertionError("must not call"))

    items = await get_items(repository, market_service, price_service)

    assert items == []
    assert repository.calls == [{"client": CLIENT, "user_id": USER_ID}]
    assert market_service.calls == 0
    assert price_service.calls == []


@pytest.mark.asyncio
async def test_combines_all_states_preserves_db_order_names_and_created_time() -> None:
    db_rows = [
        row(
            1,
            "KRW-BTC",
            korean_name="저장 비트코인",
            english_name="Stored Bitcoin",
            created_at=BASE_TIME,
        ),
        row(
            2,
            "KRW-OLD",
            korean_name="저장 올드",
            english_name="Stored Old",
            created_at=BASE_TIME,
        ),
        row(
            3,
            "KRW-ETH",
            korean_name="저장 이더리움",
            english_name="Stored Ethereum",
            created_at=BASE_TIME + timedelta(seconds=1),
        ),
        row(
            4,
            "KRW-XRP",
            korean_name="저장 리플",
            english_name="Stored XRP",
            created_at=BASE_TIME + timedelta(seconds=2),
        ),
    ]
    markets = (
        market("KRW-BTC"),
        market("KRW-ETH", MarketStatus.CAUTION),
        market("KRW-XRP"),
    )
    prices = {
        "KRW-BTC": price("KRW-BTC", 142_300_000, 0.0125),
        "KRW-ETH": price("KRW-ETH", 4_321_000.25, -0.005, stale=True),
    }
    market_service = FakeMarketListService(markets)
    price_service = FakePriceService(prices)

    items = await get_items(
        FakeListRepository(db_rows),
        market_service,
        price_service,
    )

    assert [item.id for item in items] == [1, 2, 3, 4]
    assert [item.status for item in items] == [
        WatchlistStatus.ACTIVE,
        WatchlistStatus.UNAVAILABLE,
        WatchlistStatus.CAUTION,
        WatchlistStatus.PRICE_ERROR,
    ]
    assert [item.symbol for item in items] == ["BTC", "OLD", "ETH", "XRP"]
    assert items[0].current_price == Decimal("142300000")
    assert items[0].signed_change_rate == Decimal("1.2500")
    assert items[2].current_price == Decimal("4321000.25")
    assert items[2].signed_change_rate == Decimal("-0.500")
    assert items[2].is_stale is True
    assert items[1].current_price is None
    assert items[3].signed_change_rate is None
    assert items[0].korean_name == "저장 비트코인"
    assert items[0].english_name == "Stored Bitcoin"
    assert items[0].created_at == BASE_TIME
    assert market_service.calls == 1
    assert price_service.calls == [("KRW-BTC", "KRW-ETH", "KRW-XRP")]


@pytest.mark.asyncio
async def test_duplicate_market_codes_are_priced_once_but_rows_are_all_kept() -> None:
    rows = [
        row(1, "KRW-BTC", korean_name="비트코인1", english_name="Bitcoin 1"),
        row(2, "KRW-BTC", korean_name="비트코인2", english_name="Bitcoin 2"),
    ]
    price_service = FakePriceService({"KRW-BTC": price("KRW-BTC", 100, 0.001)})

    items = await get_items(
        FakeListRepository(rows),
        FakeMarketListService((market("KRW-BTC"),)),
        price_service,
    )

    assert [item.id for item in items] == [1, 2]
    assert price_service.calls == [("KRW-BTC",)]


@pytest.mark.asyncio
async def test_all_unavailable_skips_price_service() -> None:
    rows = [
        row(1, "KRW-OLD", korean_name="올드", english_name="Old"),
    ]
    price_service = FakePriceService(error=AssertionError("must not call"))

    items = await get_items(
        FakeListRepository(rows),
        FakeMarketListService(()),
        price_service,
    )

    assert items[0].status is WatchlistStatus.UNAVAILABLE
    assert price_service.calls == []


@pytest.mark.asyncio
async def test_market_list_error_is_not_converted_to_unavailable() -> None:
    failure = AppError(
        code=ErrorCode.CACHE_REFRESH_IN_PROGRESS,
        message="safe market failure",
    )
    price_service = FakePriceService()

    with pytest.raises(AppError) as captured:
        await get_items(
            FakeListRepository(
                [row(1, "KRW-BTC", korean_name="비트코인", english_name="Bitcoin")]
            ),
            FakeMarketListService(error=failure),
            price_service,
        )

    assert captured.value is failure
    assert price_service.calls == []


@pytest.mark.asyncio
async def test_price_infrastructure_error_is_not_converted_to_price_error() -> None:
    failure = AppError(
        code=ErrorCode.UPBIT_UNAVAILABLE,
        message="safe ticker failure",
    )

    with pytest.raises(AppError) as captured:
        await get_items(
            FakeListRepository(
                [row(1, "KRW-BTC", korean_name="비트코인", english_name="Bitcoin")]
            ),
            FakeMarketListService((market("KRW-BTC"),)),
            FakePriceService(error=failure),
        )

    assert captured.value is failure


@pytest.mark.asyncio
async def test_db_error_is_safe_500_not_empty_list() -> None:
    repository = FakeListRepository(
        [],
        error=WatchlistRepositoryError("private database details"),
    )

    with pytest.raises(AppError) as captured:
        await get_items(
            repository,
            FakeMarketListService(),
            FakePriceService(),
            user_id=OTHER_USER_ID,
        )

    assert captured.value.code is ErrorCode.INTERNAL_SERVER_ERROR
    assert "private" not in captured.value.message
    assert repository.calls == [{"client": CLIENT, "user_id": OTHER_USER_ID}]
