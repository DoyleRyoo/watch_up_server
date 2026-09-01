from decimal import Decimal
from uuid import uuid4

import pytest

from app.repositories.paper_position import PositionRow
from app.schemas.paper import TradeRequest
from app.services.paper_trade import PaperTradeService
from tests.test_trade_buy import Accounts, Connection, Markets, Positions, Prices, Tx


@pytest.mark.asyncio
async def test_full_sell_zeroes_quantity_and_cost_and_preserves_realized_total() -> (
    None
):
    user_id = uuid4()
    connection = Connection()
    positions = Positions(
        PositionRow(
            user_id,
            "KRW-BTC",
            Decimal("0.1"),
            Decimal("100000.000000000000000000"),
            Decimal("50.000000000000000000"),
        )
    )
    service = PaperTradeService(
        Tx(connection), Markets(), Prices(), Accounts(), positions
    )  # type: ignore[arg-type]
    result = await service.trade(
        user_id,
        TradeRequest(marketCode="KRW-BTC", side="SELL", quantity="0.1"),
        uuid4(),
    )
    assert positions.updated["quantity"] == Decimal(0)
    assert positions.updated["cost_basis"] == Decimal(0)
    assert result.transaction.disposed_cost_basis_krw == "100000.000000000000000000"
    assert positions.updated["realized_pnl"] == Decimal("14130050.000000000000000000")


@pytest.mark.asyncio
async def test_partial_sell_disposed_plus_remaining_equals_prior_cost() -> None:
    user_id = uuid4()
    prior = Decimal("3.000000000000000000")
    connection = Connection()
    positions = Positions(
        PositionRow(user_id, "KRW-BTC", Decimal("3"), prior, Decimal(0))
    )
    service = PaperTradeService(
        Tx(connection), Markets(), Prices(), Accounts(), positions
    )  # type: ignore[arg-type]
    result = await service.trade(
        user_id, TradeRequest(marketCode="KRW-BTC", side="SELL", quantity="1"), uuid4()
    )
    disposed = Decimal(result.transaction.disposed_cost_basis_krw or "0")
    assert disposed + positions.updated["cost_basis"] == prior


@pytest.mark.parametrize(
    "body",
    [
        {"marketCode": "KRW-BTC", "side": "BUY", "quantity": "0.1"},
        {"marketCode": "KRW-BTC", "side": "SELL", "amountKrw": "1000"},
        {"marketCode": "KRW-BTC", "side": "SELL", "quantity": "0.1234567890123456789"},
        {"marketCode": "KRW-BTC", "side": "BUY", "amountKrw": 1000},
        {"marketCode": "KRW-BTC", "side": "BUY", "amountKrw": "1000", "price": "1"},
    ],
)
def test_trade_schema_rejects_wrong_side_numeric_and_override_fields(
    body: dict[str, object],
) -> None:
    with pytest.raises(ValueError):
        TradeRequest.model_validate(body)
