from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal
from typing import Any
from uuid import UUID

import asyncpg  # type: ignore[import-untyped]

from app.schemas.paper import INITIAL_GRANT_KRW, PaperAccount, PaperTransaction


@dataclass(frozen=True, slots=True)
class AccountRow:
    user_id: UUID
    cash_balance_krw: int
    lifetime_top_up_krw: int


def _account(row: Mapping[str, Any]) -> AccountRow:
    return AccountRow(
        row["user_id"], row["cash_balance_krw"], row["lifetime_top_up_krw"]
    )


def _decimal(value: Decimal | None) -> str | None:
    return None if value is None else format(value, "f")


def transaction_from_row(row: Mapping[str, Any]) -> PaperTransaction:
    return PaperTransaction(
        id=str(row["id"]),
        type=row["type"],
        asset_class=row["asset_class"],
        market_code=row["market_code"],
        execution_price=_decimal(row["execution_price"]),
        quantity=_decimal(row["quantity"]),
        cash_delta_krw=str(row["cash_delta_krw"]),
        balance_after_krw=str(row["balance_after_krw"]),
        disposed_cost_basis_krw=_decimal(row["disposed_cost_basis_krw"]),
        realized_pnl_krw=_decimal(row["realized_pnl_krw"]),
        quoted_at=row["quoted_at"],
        created_at=row["created_at"],
    )


TRANSACTION_COLUMNS = "id, type, asset_class, market_code, execution_price, quantity, cash_delta_krw, balance_after_krw, disposed_cost_basis_krw, realized_pnl_krw, quoted_at, created_at, request_fingerprint"


class PaperAccountRepository:
    async def find_transaction(
        self, connection: asyncpg.Connection, user_id: UUID, key: UUID
    ) -> Mapping[str, Any] | None:
        return await connection.fetchrow(
            f"select {TRANSACTION_COLUMNS} from public.paper_transactions where account_id = $1 and idempotency_key = $2",
            user_id,
            key,
        )

    async def get_or_create(
        self, connection: asyncpg.Connection, user_id: UUID
    ) -> AccountRow:
        row = await connection.fetchrow(
            "select user_id, cash_balance_krw, lifetime_top_up_krw from public.paper_accounts where user_id = $1 for update",
            user_id,
        )
        if row is not None:
            return _account(row)
        inserted = await connection.fetchrow(
            "insert into public.paper_accounts (user_id, cash_balance_krw, lifetime_top_up_krw) values ($1, $2, 0) on conflict (user_id) do nothing returning user_id, cash_balance_krw, lifetime_top_up_krw",
            user_id,
            INITIAL_GRANT_KRW,
        )
        row = inserted or await connection.fetchrow(
            "select user_id, cash_balance_krw, lifetime_top_up_krw from public.paper_accounts where user_id = $1 for update",
            user_id,
        )
        if inserted is not None:
            await connection.execute(
                "insert into public.paper_transactions (account_id, type, asset_class, market_code, cash_delta_krw, balance_after_krw) values ($1, 'INITIAL_GRANT', null, null, $2, $2)",
                user_id,
                INITIAL_GRANT_KRW,
            )
        if row is None:
            raise RuntimeError("paper account creation failed")
        return _account(row)

    async def read_account(
        self, connection: asyncpg.Connection, user_id: UUID
    ) -> AccountRow:
        row = await connection.fetchrow(
            "select user_id, cash_balance_krw, lifetime_top_up_krw from public.paper_accounts where user_id = $1",
            user_id,
        )
        if row is None:
            raise RuntimeError("paper account missing")
        return _account(row)

    async def update_for_top_up(
        self, connection: asyncpg.Connection, account: AccountRow, amount: int
    ) -> AccountRow:
        row = await connection.fetchrow(
            "update public.paper_accounts set cash_balance_krw = $2, lifetime_top_up_krw = $3 where user_id = $1 returning user_id, cash_balance_krw, lifetime_top_up_krw",
            account.user_id,
            account.cash_balance_krw + amount,
            account.lifetime_top_up_krw + amount,
        )
        if row is None:
            raise RuntimeError("paper account update failed")
        return _account(row)

    async def insert_top_up(
        self,
        connection: asyncpg.Connection,
        account: AccountRow,
        amount: int,
        key: UUID,
        fingerprint: str,
    ) -> PaperTransaction:
        row = await connection.fetchrow(
            f"insert into public.paper_transactions (account_id, type, cash_delta_krw, balance_after_krw, idempotency_key, request_fingerprint) values ($1, 'TOP_UP', $2, $3, $4, $5) returning {TRANSACTION_COLUMNS}",
            account.user_id,
            amount,
            account.cash_balance_krw,
            key,
            fingerprint,
        )
        if row is None:
            raise RuntimeError("top-up insert failed")
        return transaction_from_row(row)

    @staticmethod
    def serialize_account(account: AccountRow) -> PaperAccount:
        return PaperAccount(
            cash_balance_krw=str(account.cash_balance_krw),
            lifetime_top_up_krw=str(account.lifetime_top_up_krw),
        )
