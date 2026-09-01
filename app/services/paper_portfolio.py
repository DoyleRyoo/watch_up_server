from decimal import Decimal, ROUND_HALF_EVEN, localcontext
from uuid import UUID

import asyncpg  # type: ignore[import-untyped]

from app.clients.upbit_trade_price import NUMERIC_QUANTUM
from app.core.errors import AppError
from app.db.tx import DatabaseTransactionManager
from app.repositories.paper_account import PaperAccountRepository
from app.repositories.paper_position import PaperPositionRepository, PositionRow
from app.schemas.paper import (
    PaperHolding,
    PaperPortfolio,
    PortfolioPriceStatus,
    ValuationStatus,
)
from app.services.market_list import MarketListService
from app.services.price import PriceService


def _fixed(value: Decimal) -> str:
    return format(value.quantize(NUMERIC_QUANTUM, rounding=ROUND_HALF_EVEN), "f")


class PaperPortfolioService:
    def __init__(
        self,
        transactions: DatabaseTransactionManager,
        accounts: PaperAccountRepository,
        positions: PaperPositionRepository,
        markets: MarketListService,
        prices: PriceService,
    ) -> None:
        self._transactions = transactions
        self._accounts = accounts
        self._positions = positions
        self._markets = markets
        self._prices = prices

    async def get_portfolio(self, user_id: UUID) -> PaperPortfolio:
        async def read(
            connection: asyncpg.Connection,
        ) -> tuple[int, tuple[PositionRow, ...], Decimal]:
            await self._accounts.get_or_create(connection, user_id)
            account = await self._accounts.read_account(connection, user_id)
            holdings = await self._positions.list_positive(connection, user_id)
            realized = await self._positions.sum_realized(connection, user_id)
            return account.cash_balance_krw, holdings, realized

        cash, rows, total_realized = await self._transactions.run(user_id, read)
        market_map = {
            market.market_code: market for market in await self._markets.get_markets()
        }
        try:
            resolved = await self._prices.get_prices(
                tuple(row.market_code for row in rows)
            )
        except AppError:
            resolved = {}

        holdings: list[PaperHolding] = []
        any_stale = False
        partial = False
        total_value = Decimal(0)
        total_unrealized = Decimal(0)
        with localcontext() as context:
            context.prec = 80
            for row in rows:
                market = market_map.get(row.market_code)
                quote = resolved.get(row.market_code)
                avg = (
                    None
                    if row.cost_basis_krw == 0
                    else _fixed(row.cost_basis_krw / row.quantity)
                )
                current = value = unrealized = None
                if quote is None:
                    status = PortfolioPriceStatus.PRICE_ERROR
                    partial = True
                else:
                    status = (
                        PortfolioPriceStatus.STALE
                        if quote.is_stale
                        else PortfolioPriceStatus.FRESH
                    )
                    any_stale = any_stale or quote.is_stale
                    price = quote.quote.trade_price.quantize(
                        NUMERIC_QUANTUM, rounding=ROUND_HALF_EVEN
                    )
                    item_value = (price * row.quantity).quantize(
                        NUMERIC_QUANTUM, rounding=ROUND_HALF_EVEN
                    )
                    item_unrealized = (item_value - row.cost_basis_krw).quantize(
                        NUMERIC_QUANTUM, rounding=ROUND_HALF_EVEN
                    )
                    current, value, unrealized = (
                        _fixed(price),
                        _fixed(item_value),
                        _fixed(item_unrealized),
                    )
                    total_value += item_value
                    total_unrealized += item_unrealized
                holdings.append(
                    PaperHolding(
                        market_code=row.market_code,
                        korean_name=market.korean_name if market else row.market_code,
                        english_name=market.english_name if market else row.market_code,
                        quantity=_fixed(row.quantity),
                        cost_basis_krw=_fixed(row.cost_basis_krw),
                        avg_price_krw=avg,
                        current_price=current,
                        price_status=status,
                        unrealized_pnl_krw=unrealized,
                        value_krw=value,
                    )
                )

            if partial:
                return PaperPortfolio(
                    cash_balance_krw=str(cash),
                    holdings=holdings,
                    total_holdings_value_krw=None,
                    total_unrealized_pnl_krw=None,
                    total_realized_pnl_krw=_fixed(total_realized),
                    total_assets_krw=None,
                    total_pnl_krw=None,
                    total_return_rate=None,
                    valuation_status=ValuationStatus.PARTIAL,
                )
            assets = Decimal(cash) + total_value
            total_pnl = total_unrealized + total_realized
            capital = assets - total_pnl
            total_return = None if capital == 0 else _fixed(total_pnl / capital)
            return PaperPortfolio(
                cash_balance_krw=str(cash),
                holdings=holdings,
                total_holdings_value_krw=_fixed(total_value),
                total_unrealized_pnl_krw=_fixed(total_unrealized),
                total_realized_pnl_krw=_fixed(total_realized),
                total_assets_krw=_fixed(assets),
                total_pnl_krw=_fixed(total_pnl),
                total_return_rate=total_return,
                valuation_status=ValuationStatus.STALE
                if any_stale
                else ValuationStatus.FRESH,
            )
