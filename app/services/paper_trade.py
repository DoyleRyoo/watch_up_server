from dataclasses import dataclass
from decimal import Decimal, ROUND_FLOOR, ROUND_HALF_EVEN, localcontext
from uuid import UUID

import asyncpg  # type: ignore[import-untyped]

from app.clients.upbit_trade_price import NUMERIC_QUANTUM, TradePriceService
from app.core.errors import AppError, ErrorCode, INVALID_REQUEST_MESSAGE
from app.db.tx import DatabaseTransactionManager
from app.models.market import MarketStatus
from app.repositories.paper_account import (
    PaperAccountRepository,
    TRANSACTION_COLUMNS,
    transaction_from_row,
)
from app.repositories.paper_position import PaperPositionRepository
from app.schemas.paper import PaperTransaction, TradeRequest, TradeSide
from app.services.idempotency import ensure_matching_fingerprint, request_fingerprint
from app.services.market_list import MarketListService


@dataclass(frozen=True, slots=True)
class TradeResult:
    transaction: PaperTransaction
    replayed: bool


class PaperTradeService:
    def __init__(
        self,
        transactions: DatabaseTransactionManager,
        markets: MarketListService,
        prices: TradePriceService,
        accounts: PaperAccountRepository | None = None,
        positions: PaperPositionRepository | None = None,
    ) -> None:
        self._transactions = transactions
        self._markets = markets
        self._prices = prices
        self._accounts = accounts or PaperAccountRepository()
        self._positions = positions or PaperPositionRepository()

    async def trade(
        self, user_id: UUID, request: TradeRequest, key: UUID
    ) -> TradeResult:
        body = {"marketCode": request.market_code, "side": request.side.value}
        if request.side is TradeSide.BUY:
            assert request.amount_krw is not None
            body["amountKrw"] = request.amount_krw
        else:
            assert request.quantity is not None
            body["quantity"] = request.quantity
        fingerprint = request_fingerprint("/api/paper/trades", body)

        async def precheck(connection: asyncpg.Connection) -> TradeResult | None:
            row = await self._accounts.find_transaction(connection, user_id, key)
            if row is None:
                return None
            ensure_matching_fingerprint(row["request_fingerprint"], fingerprint)
            return TradeResult(transaction_from_row(row), True)

        replay = await self._transactions.run(user_id, precheck)
        if replay is not None:
            return replay

        market = await self._markets.get_market_by_code(request.market_code)
        if market is None:
            raise AppError(
                code=ErrorCode.INVALID_MARKET_CODE,
                message="유효하지 않은 마켓 코드입니다.",
            )
        if market.status is MarketStatus.UNAVAILABLE:
            raise AppError(
                code=ErrorCode.MARKET_NOT_TRADABLE,
                message="현재 거래할 수 없는 마켓입니다.",
            )
        price, quoted_at = await self._prices.fetch(request.market_code)

        async def operation(connection: asyncpg.Connection) -> TradeResult:
            existing = await self._accounts.find_transaction(connection, user_id, key)
            if existing is not None:
                ensure_matching_fingerprint(
                    existing["request_fingerprint"], fingerprint
                )
                return TradeResult(transaction_from_row(existing), True)
            account = await self._accounts.get_or_create(connection, user_id)
            position = await self._positions.lock(
                connection,
                user_id,
                request.market_code,
                create=request.side is TradeSide.BUY,
            )
            if position is None:
                raise self._holding_error()
            with localcontext() as context:
                context.prec = 80
                if request.side is TradeSide.BUY:
                    assert request.amount_krw is not None
                    amount = int(request.amount_krw)
                    if amount > account.cash_balance_krw:
                        raise AppError(
                            code=ErrorCode.INSUFFICIENT_CASH_BALANCE,
                            message="보유 현금이 부족합니다.",
                        )
                    quantity = (Decimal(amount) / price).quantize(
                        NUMERIC_QUANTUM, rounding=ROUND_FLOOR
                    )
                    debit = int(
                        (price * quantity).to_integral_value(rounding=ROUND_FLOOR)
                    )
                    if quantity <= 0 or debit <= 0:
                        raise AppError(
                            code=ErrorCode.INVALID_REQUEST,
                            message=INVALID_REQUEST_MESSAGE,
                        )
                    new_quantity = (position.quantity + quantity).quantize(
                        NUMERIC_QUANTUM, rounding=ROUND_HALF_EVEN
                    )
                    new_cost = (position.cost_basis_krw + Decimal(debit)).quantize(
                        NUMERIC_QUANTUM, rounding=ROUND_HALF_EVEN
                    )
                    disposed = None
                    realized = None
                    cash_delta = -debit
                else:
                    assert request.quantity is not None
                    quantity = Decimal(request.quantity).quantize(
                        NUMERIC_QUANTUM, rounding=ROUND_HALF_EVEN
                    )
                    if position.quantity <= 0 or quantity > position.quantity:
                        raise self._holding_error()
                    proceeds = int(
                        (price * quantity).to_integral_value(rounding=ROUND_FLOOR)
                    )
                    if proceeds < 1:
                        raise AppError(
                            code=ErrorCode.INVALID_REQUEST,
                            message=INVALID_REQUEST_MESSAGE,
                        )
                    new_quantity = (position.quantity - quantity).quantize(
                        NUMERIC_QUANTUM, rounding=ROUND_HALF_EVEN
                    )
                    if quantity == position.quantity:
                        disposed = position.cost_basis_krw
                        new_quantity = Decimal(0)
                        new_cost = Decimal(0)
                    else:
                        disposed = (
                            position.cost_basis_krw * quantity / position.quantity
                        ).quantize(NUMERIC_QUANTUM, rounding=ROUND_HALF_EVEN)
                        new_cost = position.cost_basis_krw - disposed
                    realized = (Decimal(proceeds) - disposed).quantize(
                        NUMERIC_QUANTUM, rounding=ROUND_HALF_EVEN
                    )
                    cash_delta = proceeds
                balance = account.cash_balance_krw + cash_delta
                await connection.execute(
                    "update public.paper_accounts set cash_balance_krw = $2 where user_id = $1",
                    user_id,
                    balance,
                )
                await self._positions.update(
                    connection,
                    position,
                    quantity=new_quantity,
                    cost_basis=new_cost,
                    realized_pnl=position.realized_pnl_krw + (realized or Decimal(0)),
                )
                row = await connection.fetchrow(
                    f"insert into public.paper_transactions (account_id, type, asset_class, market_code, execution_price, quantity, cash_delta_krw, balance_after_krw, disposed_cost_basis_krw, realized_pnl_krw, quoted_at, idempotency_key, request_fingerprint) values ($1, $2, 'CRYPTO', $3, $4, $5, $6, $7, $8, $9, $10, $11, $12) returning {TRANSACTION_COLUMNS}",
                    user_id,
                    request.side.value,
                    request.market_code,
                    price,
                    quantity,
                    cash_delta,
                    balance,
                    disposed,
                    realized,
                    quoted_at,
                    key,
                    fingerprint,
                )
                if row is None:
                    raise RuntimeError("trade insert failed")
                return TradeResult(transaction_from_row(row), False)

        return await self._transactions.run(user_id, operation)

    @staticmethod
    def _holding_error() -> AppError:
        return AppError(
            code=ErrorCode.INSUFFICIENT_HOLDING_QUANTITY,
            message="보유 수량이 부족합니다.",
        )
