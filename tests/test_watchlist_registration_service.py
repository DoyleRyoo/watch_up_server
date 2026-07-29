from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest

from app.core.errors import AppError, ErrorCode
from app.models.market import Market, MarketStatus
from app.models.watchlist import WatchlistInsert, WatchlistRow
from app.repositories.watchlist import (
    WatchlistDuplicateError,
    WatchlistRepositoryError,
)
from app.services.watchlist import WatchlistService


USER_ID = uuid4()
OTHER_USER_ID = uuid4()
CLIENT = object()
MARKET = Market(
    market_code="KRW-BTC",
    korean_name="비트코인",
    english_name="Bitcoin",
    status=MarketStatus.ACTIVE,
)
CREATED_AT = datetime(2026, 7, 15, 12, tzinfo=UTC)


class FakeMarketListService:
    def __init__(
        self,
        result: Market | None = MARKET,
        *,
        error: BaseException | None = None,
        events: list[str] | None = None,
    ) -> None:
        self.result = result
        self.error = error
        self.calls: list[str] = []
        self.events = events

    async def get_market_by_code(self, market_code: str) -> Market | None:
        self.calls.append(market_code)
        if self.events is not None:
            self.events.append("market")
        if self.error is not None:
            raise self.error
        return self.result


class RecordingRepository:
    def __init__(
        self,
        *,
        count: int = 0,
        exists: bool = False,
        error_at: str | None = None,
        insert_error: Exception | None = None,
        events: list[str] | None = None,
    ) -> None:
        self.count = count
        self.exists = exists
        self.error_at = error_at
        self.insert_error = insert_error
        self.events = events
        self.calls: list[tuple[str, dict[str, object]]] = []

    def _record(self, name: str, arguments: dict[str, object]) -> None:
        self.calls.append((name, arguments))
        if self.events is not None:
            self.events.append(name)
        if self.error_at == name:
            raise WatchlistRepositoryError(f"private {name} failure")

    def count_by_user(self, **arguments: object) -> int:
        self._record("count", arguments)
        return self.count

    def exists_by_user_and_market(self, **arguments: object) -> bool:
        self._record("exists", arguments)
        return self.exists

    def insert(self, **arguments: object) -> WatchlistRow:
        self._record("insert", arguments)
        if self.insert_error is not None:
            raise self.insert_error
        values = arguments["values"]
        assert isinstance(values, WatchlistInsert)
        return WatchlistRow(
            id=37,
            user_id=values.user_id,
            market_code=values.market_code,
            korean_name=values.korean_name,
            english_name=values.english_name,
            created_at=CREATED_AT,
        )


def make_service(repository: RecordingRepository) -> WatchlistService:
    return WatchlistService(repository=repository)  # type: ignore[arg-type]


async def register(
    repository: RecordingRepository,
    market_service: FakeMarketListService,
    *,
    market_code: str = "KRW-BTC",
    user_id: UUID = USER_ID,
) -> WatchlistRow:
    return await make_service(repository).register_for_user(
        client=CLIENT,  # type: ignore[arg-type]
        user_id=user_id,
        market_code=market_code,
        market_list_service=market_service,  # type: ignore[arg-type]
    )


@pytest.mark.parametrize(
    "market_code",
    [
        "",
        "   ",
        "BTC",
        "USD-BTC",
        "krw-btc",
        "KRW-",
        "KRW-BTC ",
        " KRW-BTC",
        "KRW-B_TC",
        "KRW-BTC!",
        "KRW-ABCDEFGHIJKLMNOPQ",
    ],
)
@pytest.mark.asyncio
async def test_invalid_market_code_stops_before_market_or_database(
    market_code: str,
) -> None:
    repository = RecordingRepository()
    market_service = FakeMarketListService()

    with pytest.raises(AppError) as captured:
        await register(repository, market_service, market_code=market_code)

    assert captured.value.code is ErrorCode.INVALID_MARKET_CODE
    assert captured.value.status_code == 400
    assert market_service.calls == []
    assert repository.calls == []


@pytest.mark.asyncio
async def test_missing_exact_market_stops_before_database() -> None:
    repository = RecordingRepository()
    market_service = FakeMarketListService(result=None)

    with pytest.raises(AppError) as captured:
        await register(repository, market_service)

    assert captured.value.code is ErrorCode.INVALID_MARKET_CODE
    assert market_service.calls == ["KRW-BTC"]
    assert repository.calls == []


@pytest.mark.parametrize("status", [MarketStatus.ACTIVE, MarketStatus.CAUTION])
@pytest.mark.parametrize("count", [0, 49])
@pytest.mark.asyncio
async def test_registration_uses_server_market_values_and_fixed_order(
    status: MarketStatus,
    count: int,
) -> None:
    events: list[str] = []
    market = MARKET.model_copy(update={"status": status})
    market_service = FakeMarketListService(market, events=events)
    repository = RecordingRepository(count=count, events=events)

    row = await register(repository, market_service)

    assert row.id == 37
    assert row.created_at == CREATED_AT
    assert events == ["market", "count", "exists", "insert"]
    assert [name for name, _ in repository.calls] == ["count", "exists", "insert"]
    _, insert_arguments = repository.calls[-1]
    values = insert_arguments["values"]
    assert isinstance(values, WatchlistInsert)
    assert values.to_db_payload() == {
        "user_id": str(USER_ID),
        "market_code": "KRW-BTC",
        "korean_name": "비트코인",
        "english_name": "Bitcoin",
    }
    assert set(values.to_db_payload()) == {
        "user_id",
        "market_code",
        "korean_name",
        "english_name",
    }


@pytest.mark.parametrize("count", [50, 51, 100])
@pytest.mark.asyncio
async def test_limit_stops_before_duplicate_and_insert(count: int) -> None:
    repository = RecordingRepository(count=count, exists=True)

    with pytest.raises(AppError) as captured:
        await register(repository, FakeMarketListService())

    assert captured.value.code is ErrorCode.WATCHLIST_LIMIT_EXCEEDED
    assert captured.value.status_code == 400
    assert [name for name, _ in repository.calls] == ["count"]


@pytest.mark.asyncio
async def test_duplicate_stops_before_insert_and_uses_both_keys() -> None:
    repository = RecordingRepository(count=4, exists=True)

    with pytest.raises(AppError) as captured:
        await register(repository, FakeMarketListService())

    assert captured.value.code is ErrorCode.WATCHLIST_DUPLICATED
    assert captured.value.status_code == 409
    assert [name for name, _ in repository.calls] == ["count", "exists"]
    assert repository.calls[-1][1] == {
        "client": CLIENT,
        "user_id": USER_ID,
        "market_code": "KRW-BTC",
    }


@pytest.mark.parametrize("operation", ["count", "exists"])
@pytest.mark.asyncio
async def test_database_read_error_is_not_treated_as_safe_default(
    operation: str,
) -> None:
    repository = RecordingRepository(error_at=operation)

    with pytest.raises(AppError) as captured:
        await register(repository, FakeMarketListService())

    assert captured.value.code is ErrorCode.INTERNAL_SERVER_ERROR
    assert "private" not in captured.value.message
    assert "insert" not in [name for name, _ in repository.calls]


@pytest.mark.asyncio
async def test_unique_race_is_mapped_to_duplicate() -> None:
    repository = RecordingRepository(
        insert_error=WatchlistDuplicateError("private constraint details")
    )

    with pytest.raises(AppError) as captured:
        await register(repository, FakeMarketListService())

    assert captured.value.code is ErrorCode.WATCHLIST_DUPLICATED
    assert captured.value.status_code == 409
    assert [name for name, _ in repository.calls] == ["count", "exists", "insert"]
    assert "private" not in captured.value.message


@pytest.mark.asyncio
async def test_different_users_can_register_the_same_market() -> None:
    first = RecordingRepository()
    second = RecordingRepository()

    first_row = await register(first, FakeMarketListService(), user_id=USER_ID)
    second_row = await register(
        second,
        FakeMarketListService(),
        user_id=OTHER_USER_ID,
    )

    assert first_row.user_id == USER_ID
    assert second_row.user_id == OTHER_USER_ID
