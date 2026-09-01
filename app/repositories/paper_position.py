from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal
from typing import Any
from uuid import UUID

import asyncpg  # type: ignore[import-untyped]


@dataclass(frozen=True, slots=True)
class PositionRow:
    account_id: UUID
    market_code: str
    quantity: Decimal
    cost_basis_krw: Decimal
    realized_pnl_krw: Decimal


def _position(row: Mapping[str, Any]) -> PositionRow:
    return PositionRow(
        row["account_id"],
        row["market_code"],
        row["quantity"],
        row["cost_basis_krw"],
        row["realized_pnl_krw"],
    )


class PaperPositionRepository:
    async def lock(
        self,
        connection: asyncpg.Connection,
        user_id: UUID,
        market_code: str,
        *,
        create: bool,
    ) -> PositionRow | None:
        if create:
            await connection.execute(
                "insert into public.paper_positions (account_id, asset_class, market_code, quantity, cost_basis_krw) values ($1, 'CRYPTO', $2, 0, 0) on conflict (account_id, asset_class, market_code) do nothing",
                user_id,
                market_code,
            )
        row = await connection.fetchrow(
            "select account_id, market_code, quantity, cost_basis_krw, realized_pnl_krw from public.paper_positions where account_id = $1 and asset_class = 'CRYPTO' and market_code = $2 for update",
            user_id,
            market_code,
        )
        return None if row is None else _position(row)

    async def update(
        self,
        connection: asyncpg.Connection,
        position: PositionRow,
        *,
        quantity: Decimal,
        cost_basis: Decimal,
        realized_pnl: Decimal,
    ) -> None:
        await connection.execute(
            "update public.paper_positions set quantity = $3, cost_basis_krw = $4, realized_pnl_krw = $5 where account_id = $1 and asset_class = 'CRYPTO' and market_code = $2",
            position.account_id,
            position.market_code,
            quantity,
            cost_basis,
            realized_pnl,
        )

    async def list_positive(
        self, connection: asyncpg.Connection, user_id: UUID
    ) -> tuple[PositionRow, ...]:
        rows = await connection.fetch(
            "select account_id, market_code, quantity, cost_basis_krw, realized_pnl_krw "
            "from public.paper_positions where account_id = $1 and quantity > 0 "
            "order by market_code asc",
            user_id,
        )
        return tuple(_position(row) for row in rows)

    async def sum_realized(
        self, connection: asyncpg.Connection, user_id: UUID
    ) -> Decimal:
        value = await connection.fetchval(
            "select coalesce(sum(realized_pnl_krw), 0) from public.paper_positions "
            "where account_id = $1",
            user_id,
        )
        return Decimal(value)
