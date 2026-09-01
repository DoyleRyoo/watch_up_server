from datetime import UTC, datetime
from decimal import Decimal
from uuid import uuid4

import pytest

from app.models.market import Market, MarketStatus
from app.repositories.paper_account import AccountRow
from app.repositories.paper_position import PositionRow
from app.schemas.paper import TradeRequest
from app.services.paper_trade import PaperTradeService


class Connection:
    def __init__(self) -> None:
        self.executions: list[tuple[str, tuple[object, ...]]] = []

    async def execute(self, sql: str, *args: object) -> str:
        self.executions.append((sql, args))
        return "OK"

    async def fetchrow(self, sql: str, *args: object):
        self.executions.append((sql, args))
        return {
            "id": 7,
            "type": args[1],
            "asset_class": "CRYPTO",
            "market_code": args[2],
            "execution_price": args[3],
            "quantity": args[4],
            "cash_delta_krw": args[5],
            "balance_after_krw": args[6],
            "disposed_cost_basis_krw": args[7],
            "realized_pnl_krw": args[8],
            "quoted_at": args[9],
            "created_at": datetime(2026, 9, 1, tzinfo=UTC),
            "request_fingerprint": args[11],
        }


class Tx:
    def __init__(self, connection: Connection) -> None:
        self.connection = connection

    async def run(self, user_id, operation):
        return await operation(self.connection)


class Accounts:
    async def find_transaction(self, connection, user_id, key):
        return None

    async def get_or_create(self, connection, user_id):
        return AccountRow(user_id, 1_000_000, 0)


class Positions:
    def __init__(self, position):
        self.position = position
        self.updated = None
        self.created = None

    async def lock(self, connection, user_id, market_code, *, create):
        self.created = create
        return self.position

    async def update(self, connection, position, **values):
        self.updated = values


class Markets:
    async def get_market_by_code(self, code):
        return Market(
            market_code=code,
            korean_name="비트코인",
            english_name="Bitcoin",
            status=MarketStatus.ACTIVE,
        )


class Prices:
    async def fetch(self, code):
        return Decimal("142300000.000000000000000000"), datetime(2026, 9, 1, tzinfo=UTC)


@pytest.mark.asyncio
async def test_buy_floors_quantity_then_debit_and_keeps_leftover() -> None:
    user_id = uuid4()
    connection = Connection()
    positions = Positions(
        PositionRow(user_id, "KRW-BTC", Decimal(0), Decimal(0), Decimal(0))
    )
    service = PaperTradeService(
        Tx(connection), Markets(), Prices(), Accounts(), positions
    )  # type: ignore[arg-type]
    result = await service.trade(
        user_id,
        TradeRequest(marketCode="KRW-BTC", side="BUY", amountKrw="100000"),
        uuid4(),
    )
    assert result.transaction.quantity == "0.000702740688685874"
    assert result.transaction.cash_delta_krw == "-99999"
    assert result.transaction.balance_after_krw == "900001"
    assert positions.updated["cost_basis"] == Decimal("99999.000000000000000000")
