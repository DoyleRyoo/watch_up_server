from dataclasses import dataclass
from uuid import UUID

import asyncpg  # type: ignore[import-untyped]

from app.db.tx import DatabaseTransactionManager
from app.repositories.paper_account import (
    TRANSACTION_COLUMNS,
    PaperAccountRepository,
    transaction_from_row,
)
from app.schemas.paper import PaperTransaction


@dataclass(frozen=True, slots=True)
class PaperHistoryPage:
    transactions: tuple[PaperTransaction, ...]
    has_more: bool


class PaperHistoryService:
    def __init__(
        self,
        transactions: DatabaseTransactionManager,
        accounts: PaperAccountRepository | None = None,
    ) -> None:
        self._transactions = transactions
        self._accounts = accounts or PaperAccountRepository()

    async def get_trades(
        self, user_id: UUID, *, limit: int, before_id: int | None
    ) -> PaperHistoryPage:
        async def read(connection: asyncpg.Connection) -> PaperHistoryPage:
            await self._accounts.get_or_create(connection, user_id)
            fetch_limit = limit + 1
            if before_id is None:
                rows = await connection.fetch(
                    f"select {TRANSACTION_COLUMNS} from public.paper_transactions "
                    "where account_id = $1 order by id desc limit $2",
                    user_id,
                    fetch_limit,
                )
            else:
                rows = await connection.fetch(
                    f"select {TRANSACTION_COLUMNS} from public.paper_transactions "
                    "where account_id = $1 and id < $2 order by id desc limit $3",
                    user_id,
                    before_id,
                    fetch_limit,
                )
            has_more = len(rows) > limit
            return PaperHistoryPage(
                tuple(transaction_from_row(row) for row in rows[:limit]), has_more
            )

        return await self._transactions.run(user_id, read)
